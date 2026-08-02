"""Report rendering snapshot contract (Céluma 1.3, Fase 2, Bloque B — Historia B1).

`ReportRenderingSnapshotV2` is the strict contract for everything a report
needs, besides its clinical content, to be reconstructed identically forever:
clinical structure (`template`, intentionally opaque — same untyped shape the
backend has always accepted for `Report.template`/`report.report`) plus
presentation and branding (`presentation`, strictly validated here because it
is new surface area with real security/consistency requirements).

This module also defines the request/response schemas for the
`ReportTemplateVersion` endpoints (Historia B3). See
report-template-snapshot-contract.md for the full rationale, and
report-template-version-contract.md for the entity built from this schema.
"""
import json
import re
from datetime import datetime
from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# ---------------------------------------------------------------------------
# Shared limits (documented in report-template-snapshot-contract.md)
# ---------------------------------------------------------------------------

MIN_MARGIN_CM = 0.5
MAX_MARGIN_CM = 4.0
MAX_TEMPLATE_BYTES = 500_000  # 500 KB — sanity bound on the opaque clinical structure blob
_HEX_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
_PHONE_PATTERN = re.compile(r"^[0-9+()\-.\s]{1,50}$")
_FORBIDDEN_MARKUP_SNIPPETS = ("<", ">", "javascript:", "onerror=", "onload=", "data:text/html")


def _reject_markup(value: Optional[str]) -> Optional[str]:
    """Reject free-text fields that carry HTML/JS markup.

    These fields are rendered into a PDF/preview surface; nothing here may
    carry arbitrary HTML, CSS, or script, per Céluma1.3-Fase2.md §9.
    """
    if value is None:
        return value
    lowered = value.lower()
    if any(snippet in lowered for snippet in _FORBIDDEN_MARKUP_SNIPPETS):
        raise ValueError("must not contain HTML/JS/CSS markup")
    return value


# ---------------------------------------------------------------------------
# Presentation sub-contracts
# ---------------------------------------------------------------------------

class ReportMarginsCm(BaseModel):
    """Page margins in centimeters. Bounds keep a safe printable content area
    on LETTER paper (21.59cm x 27.94cm)."""
    model_config = ConfigDict(extra="forbid")

    top: float = Field(ge=MIN_MARGIN_CM, le=MAX_MARGIN_CM)
    right: float = Field(ge=MIN_MARGIN_CM, le=MAX_MARGIN_CM)
    bottom: float = Field(ge=MIN_MARGIN_CM, le=MAX_MARGIN_CM)
    left: float = Field(ge=MIN_MARGIN_CM, le=MAX_MARGIN_CM)


class ReportPaperConfig(BaseModel):
    """Only LETTER/PORTRAIT are supported in this block (Céluma1.3-Fase2.md §9)."""
    model_config = ConfigDict(extra="forbid")

    size: Literal["LETTER"] = "LETTER"
    orientation: Literal["PORTRAIT"] = "PORTRAIT"
    margins_cm: ReportMarginsCm
    # Cuarta remediación post-Fase 2 (paridad Legacy, aditivo/opcional):
    # relleno interior SUPERIOR de la caja de contenido, dentro del área
    # paginable (`box-sizing: border-box`, igual que Legacy). `None` = 0mm,
    # que es exactamente el comportamiento actual de V2 — Legacy usa 4mm.
    # Es distinto de `header.content_gap_mm`: el gap desplaza el borde
    # superior del cuerpo (y por tanto reduce su altura antes del redondeo
    # a píxeles), mientras que este relleno vive DENTRO de la caja. Ambos
    # existen porque solo así se reproduce la aritmética exacta de Legacy y
    # la de los snapshots V2 históricos sin desviarse un píxel en ninguna
    # de las dos. Ver v2-legacy-parity-capabilities.md.
    body_padding_top_mm: Optional[float] = Field(default=None, ge=0.0, le=40.0)


def _valid_storage_id(v: Optional[str]) -> Optional[str]:
    if v is None:
        return v
    try:
        import uuid as _uuid

        _uuid.UUID(v)
    except (ValueError, AttributeError, TypeError):
        raise ValueError("must be a valid StorageObject UUID")
    return v


# ---------------------------------------------------------------------------
# Segunda remediación post-Fase 2 (UX): extensión aditiva del contrato de
# presentación para paridad visual con el membrete Legacy. Todos los campos
# son opcionales con defaults que reproducen EXACTAMENTE el comportamiento
# actual de `VersionedReportRendererV2` (Arial 10pt, línea única de 1px en
# el color primario bajo el header y sobre el footer, sin logo de pie) — un
# snapshot V2 ya persistido, sin estos campos, debe seguir renderizando
# idéntico. Ver legacy-parity-contract.md.
# ---------------------------------------------------------------------------

class DividerConfig(BaseModel):
    """Línea divisoria bajo el header / sobre el footer. El default
    reproduce la línea sólida de 1px en el color primario que el renderer
    ya dibuja hoy incondicionalmente (`border-bottom`/`border-top`).
    `style="DOUBLE"` agrega una segunda línea (necesario para paridad
    Legacy con doble filete) separada por `gap_mm`."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    style: Literal["SINGLE", "DOUBLE"] = "SINGLE"
    primary_width_px: float = Field(default=1.0, ge=0.25, le=8.0)
    secondary_width_px: float = Field(default=1.0, ge=0.25, le=8.0)
    gap_mm: float = Field(default=1.0, ge=0.0, le=20.0)
    color: Optional[str] = Field(default=None, max_length=7)

    @field_validator("color")
    @classmethod
    def _valid_hex_color(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _HEX_COLOR_PATTERN.match(v):
            raise ValueError("color must be a 6-digit hex color, e.g. #4A4A4A")
        return v


class ReportTypographyConfig(BaseModel):
    """Defaults calcados de la tipografía fija actual de
    `VersionedReportRendererV2`: Arial en todo el documento, cuerpo/header a
    10pt (institución en negrita), pie a 7pt.

    Cuarta remediación post-Fase 2 — pesos y tamaño secundario del header.
    Todos `Optional` con `None` = "conserva el comportamiento por línea que
    el renderer ya tenía" (institución 700 / resto 400; subtítulo 8pt,
    dirección y contacto 7pt; pie 400). Solo cuando se envía un valor
    explícito el renderer unifica esa propiedad en TODAS las líneas de la
    banda — que es justo lo que exige el pie Legacy (7pt en negrita) y su
    encabezado (4 líneas idénticas a 8pt en negrita). No se admite CSS
    libre: el peso es un enum cerrado."""
    model_config = ConfigDict(extra="forbid")

    font_family: Literal["ARIAL", "HELVETICA", "TIMES", "CALIBRI"] = "ARIAL"
    base_font_size_pt: float = Field(default=10.0, ge=6.0, le=24.0)
    header_font_size_pt: float = Field(default=10.0, ge=6.0, le=32.0)
    footer_font_size_pt: float = Field(default=7.0, ge=6.0, le=18.0)
    # Cuarta remediación (aditivos/opcionales — `None` = comportamiento actual):
    header_secondary_font_size_pt: Optional[float] = Field(default=None, ge=6.0, le=32.0)
    header_font_weight: Optional[Literal[400, 500, 600, 700]] = None
    footer_font_weight: Optional[Literal[400, 500, 600, 700]] = None
    body_font_weight: Optional[Literal[400, 500, 600, 700]] = None
    line_height: Optional[float] = Field(default=None, ge=0.8, le=3.0)


class ReportHeaderConfig(BaseModel):
    """Institutional header/branding. `logo_storage_id` references a
    `StorageObject` already owned by Céluma — never an arbitrary external URL."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    logo_storage_id: Optional[str] = Field(default=None, max_length=64)
    institution_name: Optional[str] = Field(default=None, max_length=255)
    subtitle: Optional[str] = Field(default=None, max_length=255)
    address: Optional[str] = Field(default=None, max_length=500)
    phone: Optional[str] = Field(default=None, max_length=50)
    email: Optional[EmailStr] = None
    # Segunda remediación UX — paridad Legacy (todos opcionales/aditivos):
    logo_position: Literal["LEFT", "CENTER", "RIGHT"] = "LEFT"
    content_alignment: Literal["TOP", "CENTER", "BOTTOM"] = "CENTER"
    height_mm: Optional[float] = Field(default=None, ge=5.0, le=100.0)
    divider: DividerConfig = Field(default_factory=DividerConfig)
    # ------------------------------------------------------------------
    # Cuarta remediación post-Fase 2 — paridad Legacy (aditivos/opcionales).
    #
    # `logo_mode` gobierna si el encabezado dibuja una imagen y si reserva
    # espacio para ella:
    #   NONE           -> ninguna imagen, ningún espacio reservado
    #   CUSTOM         -> el logo resuelto de `logo_storage_id`; si no se
    #                     resuelve, no se dibuja nada (nunca un sustituto)
    #   CELUMA_DEFAULT -> el isotipo neutral de Céluma
    #   None (ausente) -> COMPATIBILIDAD: exactamente lo que el renderer
    #                     hacía antes de esta remediación, es decir
    #                     "logo resuelto si lo hay, isotipo neutral si no".
    #                     Los snapshots V2 ya persistidos no llevan este
    #                     campo y deben seguir renderizando igual.
    # El membrete Legacy exporta NONE (su logo vive en el pie), y todo
    # membrete nuevo creado en el editor escribe un valor explícito.
    # Ver v2-legacy-parity-capabilities.md, sección "logo_mode".
    logo_mode: Optional[Literal["NONE", "CUSTOM", "CELUMA_DEFAULT"]] = None
    # Distancia del borde superior de la hoja al borde superior de la banda.
    # `None` = `paper.margins_cm.top` (comportamiento actual). Legacy usa 0.
    offset_mm: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    # Separación entre la banda y el borde superior del cuerpo. `None` = 4mm
    # (comportamiento actual). Legacy usa 0 y compensa con
    # `paper.body_padding_top_mm`.
    content_gap_mm: Optional[float] = Field(default=None, ge=0.0, le=40.0)
    # `padding-bottom` de la banda. `None` = 3mm (actual). Legacy usa 4mm.
    padding_mm: Optional[float] = Field(default=None, ge=0.0, le=40.0)
    # Dónde se imprimen las credenciales del firmante institucional:
    #   RIGHT  -> bloque propio a la derecha (actual, y `None` lo reproduce)
    #   INLINE -> como líneas más del bloque institucional izquierdo, con la
    #             misma tipografía — la forma del encabezado Legacy
    #   HIDDEN -> no se imprimen en el encabezado
    signer_placement: Optional[Literal["RIGHT", "INLINE", "HIDDEN"]] = None
    # Caja del logo del encabezado. `None` = alto de banda − 6mm y ancho
    # máximo 32mm (actual).
    logo_height_mm: Optional[float] = Field(default=None, ge=1.0, le=100.0)
    logo_max_width_mm: Optional[float] = Field(default=None, ge=1.0, le=200.0)

    @field_validator("institution_name", "subtitle", "address")
    @classmethod
    def _no_markup(cls, v: Optional[str]) -> Optional[str]:
        return _reject_markup(v)

    @field_validator("phone")
    @classmethod
    def _valid_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _PHONE_PATTERN.fullmatch(v):
            raise ValueError("phone contains unsupported characters")
        return v

    @field_validator("logo_storage_id")
    @classmethod
    def _valid_logo_reference(cls, v: Optional[str]) -> Optional[str]:
        return _valid_storage_id(v)


class ReportFooterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    custom_text: Optional[str] = Field(default=None, max_length=1000)
    show_page_number: bool = True
    # Segunda remediación UX — paridad Legacy (todos opcionales/aditivos):
    # Legacy coloca su logo en el PIE, no en el header — de ahí el valor de
    # este campo (ver legacy-parity-contract.md).
    logo_storage_id: Optional[str] = Field(default=None, max_length=64)
    logo_position: Literal["LEFT", "CENTER", "RIGHT"] = "LEFT"
    content_alignment: Literal["LEFT", "CENTER", "RIGHT"] = "CENTER"
    height_mm: Optional[float] = Field(default=None, ge=5.0, le=100.0)
    divider: DividerConfig = Field(default_factory=DividerConfig)
    # ------------------------------------------------------------------
    # Cuarta remediación post-Fase 2 — paridad Legacy (aditivos/opcionales).
    #
    # `logo_mode`: mismo enum que el encabezado. `None` (ausente) reproduce
    # el comportamiento actual del PIE, que NO es el del encabezado: el pie
    # nunca cayó al isotipo neutral, así que ausente = "logo resuelto si lo
    # hay, nada si no".
    logo_mode: Optional[Literal["NONE", "CUSTOM", "CELUMA_DEFAULT"]] = None
    # `SPLIT` coloca logo y texto como hermanos directos separados por
    # `justify-content: space-between` (la forma del pie Legacy). `None` /
    # `GROUPED` conserva la agrupación actual (logo y texto juntos en una
    # caja con `gap`, alineada según `content_alignment`).
    layout: Optional[Literal["GROUPED", "SPLIT"]] = None
    offset_mm: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    content_gap_mm: Optional[float] = Field(default=None, ge=0.0, le=40.0)
    # `padding-top` de la banda. `None` = 2mm (actual). Legacy usa 0.
    padding_mm: Optional[float] = Field(default=None, ge=0.0, le=40.0)
    # Caja del logo del pie. `None` = alto de banda − 6mm y ancho máximo
    # 28mm (actual). Legacy: alto de banda − 4mm y ancho máximo 35%.
    logo_height_mm: Optional[float] = Field(default=None, ge=1.0, le=100.0)
    logo_max_width_pct: Optional[float] = Field(default=None, ge=1.0, le=100.0)
    # Ancho máximo del bloque de texto, en % de la banda. `None` = sin tope
    # (actual). Legacy: 65%.
    text_max_width_pct: Optional[float] = Field(default=None, ge=1.0, le=100.0)

    @field_validator("custom_text")
    @classmethod
    def _no_markup(cls, v: Optional[str]) -> Optional[str]:
        return _reject_markup(v)

    @field_validator("logo_storage_id")
    @classmethod
    def _valid_logo_reference(cls, v: Optional[str]) -> Optional[str]:
        return _valid_storage_id(v)


class ReportStyleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_color: str = Field(default="#4A4A4A")
    # Segunda remediación UX — paridad Legacy (opcional/aditivo): None
    # conserva el comportamiento actual de un solo color.
    secondary_color: Optional[str] = Field(default=None, max_length=7)
    typography: ReportTypographyConfig = Field(default_factory=ReportTypographyConfig)

    @field_validator("primary_color")
    @classmethod
    def _valid_hex_color(cls, v: str) -> str:
        if not _HEX_COLOR_PATTERN.match(v):
            raise ValueError("primary_color must be a 6-digit hex color, e.g. #4A4A4A")
        return v

    @field_validator("secondary_color")
    @classmethod
    def _valid_secondary_hex_color(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _HEX_COLOR_PATTERN.match(v):
            raise ValueError("secondary_color must be a 6-digit hex color, e.g. #4A4A4A")
        return v


class ReportSignerSnapshot(BaseModel):
    """Minimal, extensible signer identity snapshot (Céluma1.3-Fase2.md §B7).

    Deliberately small: `AppUser` has no `specialty`/`professional_license`
    fields yet, so those remain free-text here until a later block adds
    structured fields and UI to configure them (see block-c-dependencies.md).
    """
    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = Field(default=None, max_length=255)
    specialty: Optional[str] = Field(default=None, max_length=255)
    license_number: Optional[str] = Field(default=None, max_length=100)
    affiliation: Optional[str] = Field(default=None, max_length=255)

    @field_validator("display_name", "specialty", "license_number", "affiliation")
    @classmethod
    def _no_markup(cls, v: Optional[str]) -> Optional[str]:
        return _reject_markup(v)


class ReportPresentationSnapshotV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper: ReportPaperConfig
    header: ReportHeaderConfig
    footer: ReportFooterConfig
    style: ReportStyleConfig = Field(default_factory=ReportStyleConfig)
    signer: Optional[ReportSignerSnapshot] = None


# ---------------------------------------------------------------------------
# Top-level snapshot contract
# ---------------------------------------------------------------------------

class ReportRenderingSnapshotV2(BaseModel):
    """The single, versioned object that fully describes how to reconstruct
    a V2 report: clinical structure + presentation + branding, together.

    A unified object (rather than three independently-stored snapshots) was
    chosen specifically to avoid two sources of truth for the same document
    — see phase-2-block-b-architecture-decision.md, "Por qué un único objeto".
    """
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    template: Dict[str, Any]
    presentation: ReportPresentationSnapshotV2

    @field_validator("template")
    @classmethod
    def _validate_template(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(v, dict):
            raise ValueError("template must be a JSON object")
        encoded_size = len(json.dumps(v, ensure_ascii=False).encode("utf-8"))
        if encoded_size > MAX_TEMPLATE_BYTES:
            raise ValueError(
                f"template exceeds the maximum allowed size ({MAX_TEMPLATE_BYTES} bytes)"
            )
        return v


# ---------------------------------------------------------------------------
# ReportTemplateVersion request/response schemas (Historia B3)
# ---------------------------------------------------------------------------

class ReportTemplateVersionCreate(BaseModel):
    """Payload to publish a new, immutable report-template version."""
    configuration: ReportRenderingSnapshotV2


class ReportTemplateVersionResponse(BaseModel):
    """Lightweight version metadata — never includes `configuration`."""
    id: str
    tenant_id: str
    report_template_id: str
    version_number: int
    schema_version: int
    status: str
    created_by: Optional[str] = None
    published_at: datetime
    activated_at: Optional[datetime] = None
    archived_at: Optional[datetime] = None


class ReportTemplateVersionDetailResponse(ReportTemplateVersionResponse):
    """Full version detail, including the immutable configuration."""
    configuration: Dict[str, Any]


class ReportTemplateVersionsListResponse(BaseModel):
    versions: List[ReportTemplateVersionResponse]


# ---------------------------------------------------------------------------
# Template logo upload response (Céluma 1.3, Fase 2, Bloque D — Historia D2)
# ---------------------------------------------------------------------------

class ReportTemplateLogoUploadResponse(BaseModel):
    """Returned after uploading a template logo. `storage_object_id` is what
    the editor should send back as `presentation.header.logo_storage_id`
    when publishing a version — never a raw URL."""

    storage_object_id: str
    url: str
    content_type: str
    size_bytes: int
