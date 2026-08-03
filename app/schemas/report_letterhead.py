"""Request/response schemas for the shared, tenant-owned letterhead
("membrete") domain — post-Phase-2 remediation.

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
# Fourth post-Phase-2 remediation — optional description (Observation 2).
#
# `ReportLetterhead.description` is optional in ALL operations and must be
# allowed to be empty. Normalization is single and shared so
# create/update/import/export cannot diverge:
#
#     None      -> None      (clear, or "there was nothing")
#     ""        -> None
#     "   "     -> None
#     " Texto " -> "Texto"
#
# What this validator does NOT normalize is the difference between "field
# omitted" and "field sent as null": that can only be distinguished with
# `model_fields_set` in the PUT/PATCH endpoint (see `update_letterhead` in
# app/api/v1/report_letterheads.py) — see
# optional-letterhead-description-contract.md.
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
    # Third post-Phase-2 remediation: the UI must show ONLY the valid
    # actions ("Eliminar" only when safe; "Desactivar" when there is
    # history to keep), instead of always offering Delete and letting the
    # backend respond 409 after the click. See
    # letterhead-delete-deactivate-contract.md.
    has_active_version: bool = False
    can_hard_delete: bool = False
    # Human-readable reasons why it cannot be physically deleted; empty
    # when `can_hard_delete` is true.
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

    Third post-Phase-2 remediation: `resolved_resources` now accompanies
    `configuration` with ephemeral URLs for logos referenced by
    `header.logo_storage_id`/`footer.logo_storage_id`. Without this the
    editor had no way to preview an already-persisted logo on reopen, and
    always fell back to Céluma's neutral logo (brief problems B and C).
    `None` when no logo is configured — same contract as
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
# Portable `.cell`/`.clm`/`.celuma` file — post-Phase-2 remediation,
# R12/R13; v2 extension and format in the second remediation (UX).
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
# — needed since a letterhead can now carry a footer logo too (Legacy
# parity). Export always writes the CURRENT version (2); `.clm` is an
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
        # Fourth remediation: a `.cell` may carry `"description": null`,
        # `""`, or whitespace only; the round-trip must keep the SAME
        # semantics ("no description") in all three cases.
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
