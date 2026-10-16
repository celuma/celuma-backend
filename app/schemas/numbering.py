"""Schemas of the monthly numbering configuration and preview API (issue #8)."""
from typing import List, Optional

from pydantic import BaseModel, StrictInt


class NumberingConfigResponse(BaseModel):
    entity_kind: str
    #: Deliberate activation; false = compatibility behaviour (orders `<code>-<n>`, samples typed by hand).
    active: bool
    manual_policy: str
    format: str
    sequence_min_width: int
    start_value: int
    scope: str
    #: IANA zone of the month of tenant-wide counters (a branch-scoped counter uses its branch's zone).
    timezone: str
    #: true when the tenant has no stored row (never configured, therefore not activated).
    is_default: bool
    #: How the CALLER should be offered the code input: optional | required | hidden | blocked.
    manual_entry: str
    activated_at: Optional[str] = None


class NumberingConfigsResponse(BaseModel):
    order: NumberingConfigResponse
    sample: NumberingConfigResponse


class NumberingConfigUpdate(BaseModel):
    active: bool
    manual_policy: str
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
    base_code: str
    examples: List[NumberingExample]
    estimate: Optional[NumberingEstimate] = None
    notice: str
