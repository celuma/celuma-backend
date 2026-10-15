"""Sample-type catalog schemas (Céluma 1.4, issue #17).

Contract: `celuma-engineering/specs/laboratory/sample-types.md` and
`celuma-engineering/specs/laboratory/catalog-transfer.md`.
"""
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, field_validator

from app.schemas.study_type import TemplateRef

#: Longest description the catalog accepts. `study_type.description` has no
#: database limit; sample types (and therefore every import/copy) are bounded.
MAX_DESCRIPTION_LENGTH = 2000


class SampleTypeRef(BaseModel):
    """A sample's type as list/detail/report-data consumers receive it.

    `is_active` is false for a deactivated type that samples still reference:
    it stays readable, it just cannot be chosen for new samples.
    """
    id: str
    code: str
    name: str
    is_active: bool


def _clean_code(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError('Code cannot be empty')
    if len(v) > 50:
        raise ValueError('Code cannot exceed 50 characters')
    return v.upper()


def _clean_name(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError('Name cannot be empty')
    if len(v) > 255:
        raise ValueError('Name cannot exceed 255 characters')
    return v


def _clean_description(v: Optional[str]) -> Optional[str]:
    if v is not None and len(v) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(f'Description cannot exceed {MAX_DESCRIPTION_LENGTH} characters')
    return v


class SampleTypeCreate(BaseModel):
    code: str
    name: str
    description: Optional[str] = None
    is_active: Optional[bool] = True
    default_report_template_id: Optional[str] = None

    @field_validator('code')
    @classmethod
    def _code(cls, v):
        return _clean_code(v)

    @field_validator('name')
    @classmethod
    def _name(cls, v):
        return _clean_name(v)

    @field_validator('description')
    @classmethod
    def _description(cls, v):
        return _clean_description(v)


class SampleTypeUpdate(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    default_report_template_id: Optional[str] = None

    @field_validator('code')
    @classmethod
    def _code(cls, v):
        return None if v is None else _clean_code(v)

    @field_validator('name')
    @classmethod
    def _name(cls, v):
        return None if v is None else _clean_name(v)

    @field_validator('description')
    @classmethod
    def _description(cls, v):
        return _clean_description(v)


class SampleTypeResponse(BaseModel):
    id: str
    tenant_id: str
    code: str
    name: str
    description: Optional[str] = None
    is_active: bool
    created_at: datetime
    default_report_template_id: Optional[str] = None
    default_template: Optional[TemplateRef] = None


class SampleTypesListResponse(BaseModel):
    sample_types: List[SampleTypeResponse]


# ---------------------------------------------------------------------------
# JSON import / direct copy
# ---------------------------------------------------------------------------

ItemStatus = Literal["ready", "code_exists", "duplicate_in_source", "invalid"]


class TransferIssue(BaseModel):
    code: str
    message: str
    field: Optional[str] = None


class TransferItem(BaseModel):
    """One source record as mapped onto the sample-type catalog."""
    index: int
    code: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: bool = True
    status: ItemStatus
    #: "none" (no reference), "mapped" (kept: a template of THIS laboratory),
    #: "dropped" (not resolvable here; the type is created without one).
    template_reference: Literal["none", "mapped", "dropped"] = "none"
    source_id: Optional[str] = None
    existing_sample_type_id: Optional[str] = None
    issues: List[TransferIssue] = []
    warnings: List[TransferIssue] = []


class TransferPreviewResponse(BaseModel):
    source_kind: Literal["study_types", "sample_types"]
    origin: Literal["file", "copy"]
    total: int
    ready: int
    conflicts: int
    invalid: int
    #: Opaque digest of this plan. Confirmation must echo it: if the plan
    #: changed in between (a code was taken, a template removed…) the
    #: confirmation is refused with 409 and the fresh preview.
    plan_digest: str
    items: List[TransferItem]


class ImportRequest(BaseModel):
    """`document` is the parsed export file."""
    document: Dict[str, Any]


class ImportConfirmRequest(ImportRequest):
    selected_indexes: List[int]
    on_conflict: Literal["abort", "skip"] = "abort"
    plan_digest: str


class CopyRequest(BaseModel):
    study_type_ids: List[str]


class CopyConfirmRequest(CopyRequest):
    on_conflict: Literal["abort", "skip"] = "abort"
    plan_digest: str


class SkippedItem(BaseModel):
    index: int
    code: Optional[str] = None
    reason: str


class TransferResult(BaseModel):
    operation_id: str
    created: List[SampleTypeResponse]
    skipped: List[SkippedItem]
