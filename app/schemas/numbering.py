"""Schemas of the monthly numbering configuration and preview API (issue #8)."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, StrictInt, field_validator

from app.schemas.iso_input import require_iso_date_input


class NumberingConfigResponse(BaseModel):
    entity_kind: str
    #: Deliberate activation; false = compatibility behaviour (orders `<code>-<n>`, samples typed by hand).
    active: bool
    #: Legacy manual-code policy, kept readable. Since Céluma 1.4 registration is automatic and this
    #: value no longer affects it (see `registration` and `manual_policy_in_effect`).
    manual_policy: str
    format: str
    sequence_min_width: int
    start_value: int
    scope: str
    #: IANA zone of the month of tenant-wide counters (a branch-scoped counter uses its branch's zone).
    timezone: str
    #: true when the tenant has no stored row (never configured, therefore not activated).
    is_default: bool
    #: Always "automatic": no registration form or route accepts a typed order/sample code.
    registration: str = "automatic"
    #: Always false: the stored manual policy has no effect on registration any more.
    manual_policy_in_effect: bool = False
    #: True when the stored policy is the retired `required` (kept untouched; shown so an administrator can see it).
    legacy_manual_policy: bool = False
    activated_at: Optional[str] = None


class NumberingConfigsResponse(BaseModel):
    order: NumberingConfigResponse
    sample: NumberingConfigResponse


class NumberingConfigUpdate(BaseModel):
    active: bool
    #: Optional since 1.4: omitted keeps the stored value. `required` can no longer be newly set (registration
    #: cannot demand a typed code that no longer exists); an already stored `required` is preserved untouched.
    manual_policy: Optional[str] = None
    format: str
    sequence_min_width: StrictInt
    start_value: StrictInt = 1
    scope: str = "tenant"
    timezone: str = "America/Mexico_City"


class NumberingPreviewRequest(BaseModel):
    entity_kind: str
    #: Candidate configuration; omitted fields fall back to the stored/default ones.
    active: Optional[bool] = None
    format: Optional[str] = None
    sequence_min_width: Optional[int] = None
    start_value: Optional[int] = None
    scope: Optional[str] = None
    timezone: Optional[str] = None
    #: Study type (orders) or sample type (samples) id; enables the estimate.
    scope_id: Optional[str] = None
    #: Branch whose timezone defines the period and whose code feeds `{BRANCH}`.
    branch_id: Optional[str] = None
    #: SAMPLES only (#33): the receipt date the registration form shows (date-only `YYYY-MM-DD`, a
    #: timezone-less datetime or an ISO datetime with offset). It defines the period shown, exactly as
    #: the allocation will; without it the period is the current month in the numbering zone and
    #: `date_basis` says so. Orders ignore it. Never consumes a number.
    reference_date: Optional[datetime] = None

    @field_validator("reference_date", mode="before")
    @classmethod
    def _strict_reference_date(cls, value):
        """Omission (null) stays allowed; a PROVIDED value has the same strict ISO shape as `received_at`."""
        return None if value is None else require_iso_date_input(value, message="reference_date must be a date (YYYY-MM-DD) or ISO date-time")


class NumberingExample(BaseModel):
    sequence: int
    code: str


class NumberingEstimate(BaseModel):
    sequence: int
    code: str
    #: Always false: a preview never reserves or consumes a number.
    reserved: bool = False
    skipped_occupied: int = 0


class NumberingPreviewResponse(BaseModel):
    entity_kind: str
    valid: bool
    errors: List[str]
    format: str
    sequence_min_width: int
    scheme: str = "monthly"
    #: The zone that defined the period shown (laboratory zone, or the branch's for a branch-scoped counter).
    timezone: str = ""
    period_year: int
    period_month: int
    #: What defined the period: "receipt_date" (a sample preview given `reference_date`) or
    #: "allocation_instant" (orders, and samples previewed without a date: the current month).
    date_basis: str = "allocation_instant"
    base_code: str
    examples: List[NumberingExample]
    estimate: Optional[NumberingEstimate] = None
    notice: str
