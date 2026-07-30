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
        if v is None:
            return v
        try:
            import uuid as _uuid

            _uuid.UUID(v)
        except (ValueError, AttributeError, TypeError):
            raise ValueError("logo_storage_id must be a valid StorageObject UUID")
        return v


class ReportFooterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    custom_text: Optional[str] = Field(default=None, max_length=1000)
    show_page_number: bool = True

    @field_validator("custom_text")
    @classmethod
    def _no_markup(cls, v: Optional[str]) -> Optional[str]:
        return _reject_markup(v)


class ReportStyleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_color: str = Field(default="#4A4A4A")

    @field_validator("primary_color")
    @classmethod
    def _valid_hex_color(cls, v: str) -> str:
        if not _HEX_COLOR_PATTERN.match(v):
            raise ValueError("primary_color must be a 6-digit hex color, e.g. #4A4A4A")
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
