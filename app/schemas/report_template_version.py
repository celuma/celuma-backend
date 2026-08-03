"""Report rendering snapshot contract (Céluma 1.3, Phase 2, Block B — Story B1).

`ReportRenderingSnapshotV2` is the strict contract for everything a report
needs, besides its clinical content, to be reconstructed identically forever:
clinical structure (`template`, intentionally opaque — same untyped shape the
backend has always accepted for `Report.template`/`report.report`) plus
presentation and branding (`presentation`, strictly validated here because it
is new surface area with real security/consistency requirements).

This module also defines the request/response schemas for the
`ReportTemplateVersion` endpoints (Story B3). See
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
    # Fourth post-Phase-2 remediation (Legacy parity, additive/optional):
    # SUPERIOR inner padding of the content box, inside the pageable area
    # (`box-sizing: border-box`, same as Legacy). `None` = 0mm, which is
    # exactly current V2 behavior — Legacy uses 4mm.
    # Distinct from `header.content_gap_mm`: the gap shifts the body's top
    # edge (and therefore reduces its height before pixel rounding), while
    # this padding lives INSIDE the box. Both exist because only then is
    # Legacy's exact arithmetic and that of historical V2 snapshots
    # reproduced without shifting a pixel in either. See
    # v2-legacy-parity-capabilities.md.
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
# Second post-Phase-2 remediation (UX): additive extension of the
# presentation contract for visual parity with the Legacy letterhead. All
# fields are optional with defaults that reproduce EXACTLY the current
# behavior of `VersionedReportRendererV2` (Arial 10pt, single 1px line in
# the primary color under the header and above the footer, no footer logo)
# — an already-persisted V2 snapshot without these fields must keep
# rendering identically. See legacy-parity-contract.md.
# ---------------------------------------------------------------------------

class DividerConfig(BaseModel):
    """Divider line under the header / above the footer. The default
    reproduces the solid 1px primary-color line the renderer already draws
    unconditionally today (`border-bottom`/`border-top`).
    `style="DOUBLE"` adds a second line (needed for Legacy parity with a
    double rule) separated by `gap_mm`."""
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
    """Defaults copied from the current fixed typography of
    `VersionedReportRendererV2`: Arial throughout, body/header at 10pt
    (institution in bold), footer at 7pt.

    Fourth post-Phase-2 remediation — header weights and secondary size.
    All `Optional` with `None` = "keep the per-line behavior the renderer
    already had" (institution 700 / rest 400; subtitle 8pt, address and
    contact 7pt; footer 400). Only when an explicit value is sent does the
    renderer unify that property across ALL band lines — exactly what the
    Legacy footer (7pt bold) and its header (4 identical 8pt bold lines)
    require. Free-form CSS is not accepted: weight is a closed enum."""
    model_config = ConfigDict(extra="forbid")

    font_family: Literal["ARIAL", "HELVETICA", "TIMES", "CALIBRI"] = "ARIAL"
    base_font_size_pt: float = Field(default=10.0, ge=6.0, le=24.0)
    header_font_size_pt: float = Field(default=10.0, ge=6.0, le=32.0)
    footer_font_size_pt: float = Field(default=7.0, ge=6.0, le=18.0)
    # Fourth remediation (additive/optional — `None` = current behavior):
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
    # Second remediation UX — Legacy parity (all optional/additive):
    logo_position: Literal["LEFT", "CENTER", "RIGHT"] = "LEFT"
    content_alignment: Literal["TOP", "CENTER", "BOTTOM"] = "CENTER"
    height_mm: Optional[float] = Field(default=None, ge=5.0, le=100.0)
    divider: DividerConfig = Field(default_factory=DividerConfig)
    # ------------------------------------------------------------------
    # Fourth post-Phase-2 remediation — Legacy parity (additive/optional).
    #
    # `logo_mode` controls whether the header draws an image and whether it
    # reserves space for it:
    #   NONE           -> no image, no reserved space
    #   CUSTOM         -> the resolved logo from `logo_storage_id`; if it
    #                     does not resolve, nothing is drawn (never a
    #                     substitute)
    #   CELUMA_DEFAULT -> Céluma's neutral isotype
    #   None (absent)  -> COMPATIBILITY: exactly what the renderer did
    #                     before this remediation, i.e.
    #                     "resolved logo if present, neutral isotype if not".
    #                     Already-persisted V2 snapshots lack this field
    #                     and must keep rendering the same.
    # The Legacy letterhead exports NONE (its logo lives in the footer),
    # and every new letterhead created in the editor writes an explicit
    # value. See v2-legacy-parity-capabilities.md, "logo_mode" section.
    logo_mode: Optional[Literal["NONE", "CUSTOM", "CELUMA_DEFAULT"]] = None
    # Distance from the page top edge to the band top edge.
    # `None` = `paper.margins_cm.top` (current behavior). Legacy uses 0.
    offset_mm: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    # Gap between the band and the body top edge. `None` = 4mm
    # (current behavior). Legacy uses 0 and compensates with
    # `paper.body_padding_top_mm`.
    content_gap_mm: Optional[float] = Field(default=None, ge=0.0, le=40.0)
    # Band `padding-bottom`. `None` = 3mm (current). Legacy uses 4mm.
    padding_mm: Optional[float] = Field(default=None, ge=0.0, le=40.0)
    # Where institutional signer credentials are printed:
    #   RIGHT  -> own block on the right (current, and `None` reproduces it)
    #   INLINE -> as extra lines of the left institutional block, with the
    #             same typography — the Legacy header shape
    #   HIDDEN -> not printed in the header
    signer_placement: Optional[Literal["RIGHT", "INLINE", "HIDDEN"]] = None
    # Header logo box. `None` = band height − 6mm and max width
    # 32mm (current).
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
    # Second remediation UX — Legacy parity (all optional/additive):
    # Legacy places its logo in the FOOTER, not the header — hence this
    # field's value (see legacy-parity-contract.md).
    logo_storage_id: Optional[str] = Field(default=None, max_length=64)
    logo_position: Literal["LEFT", "CENTER", "RIGHT"] = "LEFT"
    content_alignment: Literal["LEFT", "CENTER", "RIGHT"] = "CENTER"
    height_mm: Optional[float] = Field(default=None, ge=5.0, le=100.0)
    divider: DividerConfig = Field(default_factory=DividerConfig)
    # ------------------------------------------------------------------
    # Fourth post-Phase-2 remediation — Legacy parity (additive/optional).
    #
    # `logo_mode`: same enum as the header. `None` (absent) reproduces
    # current FOOTER behavior, which is NOT the header's: the footer never
    # fell back to the neutral isotype, so absent = "resolved logo if
    # present, nothing if not".
    logo_mode: Optional[Literal["NONE", "CUSTOM", "CELUMA_DEFAULT"]] = None
    # `SPLIT` places logo and text as direct siblings separated by
    # `justify-content: space-between` (the Legacy footer shape). `None` /
    # `GROUPED` keeps the current grouping (logo and text together in a
    # box with `gap`, aligned per `content_alignment`).
    layout: Optional[Literal["GROUPED", "SPLIT"]] = None
    offset_mm: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    content_gap_mm: Optional[float] = Field(default=None, ge=0.0, le=40.0)
    # Band `padding-top`. `None` = 2mm (current). Legacy uses 0.
    padding_mm: Optional[float] = Field(default=None, ge=0.0, le=40.0)
    # Footer logo box. `None` = band height − 6mm and max width
    # 28mm (current). Legacy: band height − 4mm and max width 35%.
    logo_height_mm: Optional[float] = Field(default=None, ge=1.0, le=100.0)
    logo_max_width_pct: Optional[float] = Field(default=None, ge=1.0, le=100.0)
    # Max width of the text block, as % of the band. `None` = no cap
    # (current). Legacy: 65%.
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
    # Second remediation UX — Legacy parity (optional/additive): None
    # keeps the current single-color behavior.
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
    — see phase-2-block-b-architecture-decision.md, "Why a single object".
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
# ReportTemplateVersion request/response schemas (Story B3)
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
# Template logo upload response (Céluma 1.3, Phase 2, Block D — Story D2)
# ---------------------------------------------------------------------------

class ReportTemplateLogoUploadResponse(BaseModel):
    """Returned after uploading a template logo. `storage_object_id` is what
    the editor should send back as `presentation.header.logo_storage_id`
    when publishing a version — never a raw URL."""

    storage_object_id: str
    url: str
    content_type: str
    size_bytes: int
