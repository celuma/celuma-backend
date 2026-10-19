"""Céluma 1.4 (#13): an amendment of a signed report.

An amendment is a new emission of the SAME report: a cycle of `ReportVersion`
rows (`ReportVersion.amendment_id`) that starts from the delivered (latest
signed) emission, travels the ordinary review/approval cycle and, once signed,
supersedes it. The amended version is a complete re-emission — corrections and
supplements such as IHC alike (owner decision D1). The source version, its JSON,
PDF and signature evidence are never modified.

At most one amendment per report is OPEN (partial unique index). A PUBLISHED
amendment records the version that republished the report; a CANCELLED one
restores the report to its delivered emission. See celuma-engineering
`specs/reports/lifecycle.md` and `adr/0018-report-amendments-as-emissions.md`.
"""
from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import JSON
from sqlmodel import Field

from .base import BaseModel, TenantMixin, TimestampMixin

AMENDMENT_STATE_OPEN = "OPEN"
AMENDMENT_STATE_PUBLISHED = "PUBLISHED"
AMENDMENT_STATE_CANCELLED = "CANCELLED"

#: The reason category (D1): a correction, or a supplement such as IHC. Both
#: produce the same complete, superseding emission.
AMENDMENT_CATEGORY_CORRECTION = "CORRECTION"
AMENDMENT_CATEGORY_SUPPLEMENT = "SUPPLEMENT"
AMENDMENT_CATEGORIES = (AMENDMENT_CATEGORY_CORRECTION, AMENDMENT_CATEGORY_SUPPLEMENT)


class ReportAmendment(BaseModel, TimestampMixin, TenantMixin, table=True):
    __tablename__ = "report_amendment"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    report_id: UUID = Field(foreign_key="report.id")
    #: 1..n per report, in opening order (cancelled ones keep their number).
    amendment_no: int
    #: The signed emission this amendment amends (the delivered one at opening).
    source_version_id: UUID = Field(foreign_key="report_version.id")
    category: str = Field(max_length=20)
    reason: str = Field(max_length=1000)
    state: str = Field(default=AMENDMENT_STATE_OPEN, max_length=20)

    requested_by: UUID = Field(foreign_key="app_user.id")
    requested_at: datetime = Field(default_factory=datetime.utcnow)
    #: The review decisions as they stood when the amendment opened, so a
    #: cancellation restores the delivered emission's state truthfully.
    reviews_at_opening: Optional[Any] = Field(default=None, sa_type=JSON)

    published_version_id: Optional[UUID] = Field(foreign_key="report_version.id", default=None)
    published_at: Optional[datetime] = Field(default=None)

    cancelled_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    cancelled_at: Optional[datetime] = Field(default=None)
    cancel_reason: Optional[str] = Field(max_length=1000, default=None)
