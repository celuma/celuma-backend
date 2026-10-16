"""Schemas of the monthly numbering configuration and preview API (issue #8)."""
from typing import List, Optional

from pydantic import BaseModel


class NumberingConfigResponse(BaseModel):
    entity_kind: str
    format: str
    sequence_min_width: int
    mode: str
    #: True when the tenant has no stored row and the defaults apply.
    is_default: bool


class NumberingConfigsResponse(BaseModel):
    order: NumberingConfigResponse
    sample: NumberingConfigResponse


class NumberingConfigUpdate(BaseModel):
    format: str
    sequence_min_width: int
    mode: str


class NumberingPreviewRequest(BaseModel):
    entity_kind: str
    #: Candidate configuration; omitted fields fall back to the stored/default ones.
    format: Optional[str] = None
    sequence_min_width: Optional[int] = None
    #: Study type (orders) or sample type (samples) id; enables the estimate.
    scope_id: Optional[str] = None
    #: Branch whose timezone defines the period (default timezone when omitted).
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
    period_year: int
    period_month: int
    base_code: str
    examples: List[NumberingExample]
    estimate: Optional[NumberingEstimate] = None
    notice: str
