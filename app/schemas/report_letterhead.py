"""Request/response schemas for the shared, tenant-owned letterhead
("membrete") domain — post-Fase-2 remediation.

`ReportLetterheadVersionCreate.configuration` reuses
`ReportPresentationSnapshotV2` verbatim from
`app/schemas/report_template_version.py` (the same model already embedded
in `ReportRenderingSnapshotV2.presentation`) so the two contracts can never
diverge — see report-letterhead-version-contract.md.
"""
from datetime import datetime
from typing import Dict, Any, List, Optional

from pydantic import BaseModel, field_validator

from app.schemas.report_template_version import ReportPresentationSnapshotV2


# ---------------------------------------------------------------------------
# Cuarta remediación post-Fase 2 — descripción opcional (Observación 2).
#
# `ReportLetterhead.description` es opcional en TODAS las operaciones y debe
# poder quedar vacía. La normalización es única y compartida para que
# create/update/import/export no puedan divergir:
#
#     None      -> None      (limpiar, o "no había nada")
#     ""        -> None
#     "   "     -> None
#     " Texto " -> "Texto"
#
# Lo que NO normaliza este validador es la diferencia entre "campo omitido"
# y "campo enviado como null": eso solo se puede distinguir con
# `model_fields_set` en el endpoint PUT/PATCH (ver
# `update_letterhead` en app/api/v1/report_letterheads.py) —
# ver optional-letterhead-description-contract.md.
# ---------------------------------------------------------------------------

def normalize_optional_description(value: Optional[str]) -> Optional[str]:
    """Blank/whitespace-only descriptions collapse to ``None``."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


# ---------------------------------------------------------------------------
# ReportLetterhead (shell) schemas
# ---------------------------------------------------------------------------

class ReportLetterheadCreate(BaseModel):
    name: str
    description: Optional[str] = None

    @field_validator("description")
    @classmethod
    def _normalize_description(cls, v: Optional[str]) -> Optional[str]:
        return normalize_optional_description(v)


class ReportLetterheadUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("description")
    @classmethod
    def _normalize_description(cls, v: Optional[str]) -> Optional[str]:
        return normalize_optional_description(v)


class ReportLetterheadResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: Optional[str] = None
    is_default: bool
    is_active: bool
    created_at: datetime
    # Tercera remediación post-Fase 2: la UI debe mostrar SOLO las acciones
    # válidas ("Eliminar" únicamente cuando es seguro; "Desactivar" cuando
    # hay historial que conservar), en vez de ofrecer Eliminar siempre y
    # dejar que el backend responda 409 después del clic. Ver
    # letterhead-delete-deactivate-contract.md.
    has_active_version: bool = False
    can_hard_delete: bool = False
    # Motivos legibles por los que NO puede borrarse físicamente; vacío
    # cuando `can_hard_delete` es true.
    blocking_references: List[str] = []


class ReportLetterheadDetailResponse(ReportLetterheadResponse):
    created_by: Optional[str] = None


class ReportLetterheadsListResponse(BaseModel):
    letterheads: List[ReportLetterheadResponse]


# ---------------------------------------------------------------------------
# ReportLetterheadVersion (immutable, published presentation) schemas
# ---------------------------------------------------------------------------

class ReportLetterheadVersionCreate(BaseModel):
    """Payload to publish a new, immutable letterhead version."""
    configuration: ReportPresentationSnapshotV2


class ReportLetterheadVersionResponse(BaseModel):
    """Lightweight version metadata — never includes `configuration`."""
    id: str
    tenant_id: str
    report_letterhead_id: str
    version_number: int
    schema_version: int
    status: str
    created_by: Optional[str] = None
    published_at: datetime
    activated_at: Optional[datetime] = None
    archived_at: Optional[datetime] = None


class ReportLetterheadVersionDetailResponse(ReportLetterheadVersionResponse):
    """Full version detail, including the immutable configuration.

    Tercera remediación post-Fase 2: `resolved_resources` acompaña ahora a
    `configuration` con las URLs efímeras de los logos referenciados por
    `header.logo_storage_id`/`footer.logo_storage_id`. Sin esto el editor
    no tenía forma de previsualizar un logo ya persistido al reabrirse, y
    siempre caía al logo neutral de Céluma (problemas B y C del brief).
    `None` cuando no hay ningún logo configurado — mismo contrato que
    `ReportDetailResponse.resolved_resources`.
    """
    configuration: Dict[str, Any]
    resolved_resources: Optional[Dict[str, Any]] = None


class ReportLetterheadVersionsListResponse(BaseModel):
    versions: List[ReportLetterheadVersionResponse]


# ---------------------------------------------------------------------------
# Letterhead logo upload response — same shape as the template-logo one,
# defined separately so the two domains stay independently versionable.
# ---------------------------------------------------------------------------

class ReportLetterheadLogoUploadResponse(BaseModel):
    storage_object_id: str
    url: str
    content_type: str
    size_bytes: int


# ---------------------------------------------------------------------------
# Archivo portable `.cell`/`.clm`/`.celuma` — post-Fase-2 remediation,
# R12/R13; extensión y formato v2 en la segunda remediación (UX).
#
# Deliberately excludes tenant_id, StorageObject id, bucket/key, and any
# presigned/public URL — the envelope must be portable between tenants and
# reveal nothing about the exporting tenant's internal identifiers. Logos
# (if any) travel as base64 bytes + a sha256 hash for corruption detection;
# import re-validates the decoded bytes through ManagedTenantImageService
# exactly like a fresh upload (never trusts the embedded
# content_type/hash blindly). See cell-file-format-v2.md.
#
# `format_version` 1: `assets.logo` (single asset) -> `header.logo_storage_id`
# only — the original shape, still importable forever (`.celuma` legacy).
# `format_version` 2: `assets.header_logo`/`assets.footer_logo` (both
# independently optional) -> `header.logo_storage_id`/`footer.logo_storage_id`
# — needed since a membrete can now carry a footer logo too (paridad
# Legacy). Export always writes the CURRENT version (2); `.clm` is an
# import-only extension alias, never an export target.
# ---------------------------------------------------------------------------

CELUMA_FORMAT = "celuma-letterhead"
CELUMA_FORMAT_VERSION = 2  # version written on export
CELUMA_SUPPORTED_FORMAT_VERSIONS = frozenset({1, 2})  # versions accepted on import
MAX_CELUMA_LOGO_BYTES = 5 * 1024 * 1024  # matches ManagedTenantImageService's cap


class CelumaLetterheadAsset(BaseModel):
    media_type: str
    sha256: str
    data_base64: str


class CelumaLetterheadSource(BaseModel):
    product: str = "Céluma"
    schema_version: int = 2


class CelumaLetterheadPayload(BaseModel):
    name: str
    description: Optional[str] = None
    presentation: ReportPresentationSnapshotV2

    @field_validator("description")
    @classmethod
    def _normalize_description(cls, v: Optional[str]) -> Optional[str]:
        # Cuarta remediación: un `.cell` puede traer `"description": null`,
        # `""` o solo espacios; el round-trip debe conservar la MISMA
        # semántica ("sin descripción") en los tres casos.
        return normalize_optional_description(v)


class CelumaLetterheadEnvelope(BaseModel):
    """The `.celuma` file's JSON shape (envelope['format'] must equal
    CELUMA_FORMAT; envelope['source']['format_version'] must be a version
    this backend understands — currently only 1)."""
    format: str
    format_version: int
    exported_at: str
    source: CelumaLetterheadSource
    letterhead: CelumaLetterheadPayload
    assets: Dict[str, CelumaLetterheadAsset] = {}
