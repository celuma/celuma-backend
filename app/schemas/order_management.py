"""Schemas of order edits, sample-code correction and erroneous-capture removal (Céluma 1.4, #9/#10/#11/#16)."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.services import order_management

#: The explicit allowlist of `PATCH /laboratory/orders/{id}`. Everything else is refused by name.
ORDER_EDITABLE_FIELDS = ("requesting_physician_ids", "expediente", "notes")

#: Fields that exist on an order but are NOT editable through this endpoint (each needs its own
#: decision: identity, numbering, billing, assignment or report effects).
_NOT_EDITABLE = (
    "patient_id", "study_type_id", "branch_id", "tenant_id", "order_code", "status", "billed_lock", "report_id",
    "invoice_id", "created_by", "requested_by", "requesting_physician_id", "assignees", "reviewers", "labels",
    "total", "subtotal", "samples", "sample_code",
)


class OrderUpdate(BaseModel):
    """Controlled basic order edit. Only the fields present in the request change; a field sent as
    `null` clears it (`expediente`, `notes`); `requesting_physician_ids` replaces the whole set."""
    requesting_physician_ids: Optional[List[str]] = None
    expediente: Optional[str] = None
    notes: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _allowlist(cls, data):
        if isinstance(data, dict):
            extra = [k for k in data if k not in ORDER_EDITABLE_FIELDS]
            if extra:
                known = [k for k in extra if k in _NOT_EDITABLE]
                raise PydanticCustomError(
                    "order_field_not_editable",
                    "Not editable through this endpoint: {fields}. Editable fields: {allowed}",
                    {"fields": ", ".join(sorted(extra)) + ("" if known else " (unknown)"),
                     "allowed": ", ".join(ORDER_EDITABLE_FIELDS)},
                )
        return data

    @field_validator("requesting_physician_ids")
    @classmethod
    def _ids(cls, v):
        return order_management.normalize_physician_ids(v)

    @field_validator("expediente")
    @classmethod
    def _expediente(cls, v):
        return order_management.normalize_expediente(v)

    @model_validator(mode="after")
    def _something(self):
        if not self.model_fields_set:
            raise ValueError("Nothing to update; send at least one of: " + ", ".join(ORDER_EDITABLE_FIELDS))
        if "requesting_physician_ids" in self.model_fields_set and self.requesting_physician_ids is None:
            raise ValueError("requesting_physician_ids must be a list (an empty list removes every physician)")
        return self


class SampleCodeCorrectionRequest(BaseModel):
    sample_code: str
    reason: str


class RemovalRequest(BaseModel):
    reason: str


class BlockerOut(BaseModel):
    code: str
    message: str
    report_id: Optional[str] = None


class ActionState(BaseModel):
    #: The caller holds the capability (a UI hint; the API enforces it on every request).
    permitted: bool
    #: The lifecycle/evidence/dependency rules allow it right now (a read, not a reservation).
    allowed: bool
    blockers: List[BlockerOut] = []


class OrderManagementState(BaseModel):
    order_id: str
    order_code: str
    status: str
    edit: ActionState
    removal: ActionState
    editable_fields: List[str] = list(ORDER_EDITABLE_FIELDS)
    sample_count: int
    has_report: bool
    #: Plain-language consequences of removal, for the confirmation dialog.
    removal_consequences: List[str] = []


class SampleManagementState(BaseModel):
    sample_id: str
    sample_code: str
    order_id: str
    order_code: str
    correction: ActionState
    removal: ActionState
    removal_consequences: List[str] = []


class SampleCodeCorrectionResponse(BaseModel):
    sample_id: str
    old_code: str
    sample_code: str
    unchanged: bool


class RemovalResponse(BaseModel):
    kind: str
    id: str
    code: str
    order_id: str
    order_code: str
    already_removed: bool
    removed_at: Optional[datetime] = None
    reason: Optional[str] = None
    samples_removed: int = 0
    reports_hidden: int = 0
    invoice_retained: bool = False
