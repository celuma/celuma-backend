"""Schemas of order edits, sample-code correction and erroneous-capture removal (Céluma 1.4, #9/#10/#11/#16)."""
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.schemas.iso_input import require_iso_date_input
from app.services import order_management

#: The explicit allowlist of `PATCH /laboratory/orders/{id}`. Everything else is refused by name.
ORDER_EDITABLE_FIELDS = ("patient_id", "requesting_physician_ids", "expediente", "notes")

#: Fields that exist on an order but are NOT editable through this endpoint (each needs its own
#: decision: identity, numbering, billing, assignment or report effects).
_NOT_EDITABLE = (
    "study_type_id", "branch_id", "tenant_id", "order_code", "status", "billed_lock", "report_id",
    "invoice_id", "created_by", "requested_by", "requesting_physician_id", "assignees", "reviewers", "labels",
    "total", "subtotal", "samples", "sample_code",
)


class OrderUpdate(BaseModel):
    """Controlled basic order edit. Only the fields present in the request change; a field sent as
    `null` clears it (`patient_id`, `expediente`, `notes`); `requesting_physician_ids` replaces the whole set.
    An order keeps at least a patient or a requesting physician."""
    patient_id: Optional[str] = None
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

    @field_validator("patient_id")
    @classmethod
    def _patient(cls, v):
        v = (v or "").strip()
        if not v:
            return None
        try:
            return str(UUID(v))
        except ValueError:
            raise ValueError("patient_id must be a patient id") from None

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
    #: Receipt/collection date edit (same lifecycle window as the code correction).
    dates: ActionState
    #: Moving the receipt date into another month regenerates the code (it was issued by monthly numbering).
    code_follows_receipt_month: bool = False
    removal: ActionState
    removal_consequences: List[str] = []


class SampleDatesRequest(BaseModel):
    """Only the fields present change; `collected_at` may be `null` (cleared), `received_at` may not."""
    received_at: Optional[datetime] = None
    collected_at: Optional[datetime] = None
    reason: str

    @field_validator("received_at", "collected_at", mode="before")
    @classmethod
    def _iso(cls, value):
        # Same strict ISO date/date-time input as registration; an explicit null is handled by the service.
        return None if value is None else require_iso_date_input(value, message="Send an ISO date (YYYY-MM-DD)")

    @model_validator(mode="after")
    def _something(self):
        if not ({"received_at", "collected_at"} & self.model_fields_set):
            raise ValueError("Nothing to update; send received_at and/or collected_at")
        return self


class SampleDatesResponse(BaseModel):
    sample_id: str
    sample_code: str
    #: Set when the receipt month changed and the code was regenerated.
    old_code: Optional[str] = None
    received_at: Optional[datetime] = None
    collected_at: Optional[datetime] = None
    unchanged: bool


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
