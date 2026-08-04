from typing import Optional
from uuid import UUID, uuid4
from sqlmodel import Field
from .base import BaseModel, TimestampMixin, TenantMixin


class ReportLetterhead(BaseModel, TimestampMixin, TenantMixin, table=True):
    """Logical identity of a shared, tenant-owned letterhead ("membrete").

    Post-Phase-2 remediation: separates page presentation (logo, header,
    footer, margins, color, institutional signer) from clinical structure
    (`ReportTemplate`). A letterhead is a mutable shell (name/description/
    default flag) — analogous to `ReportTemplate` — never the presentation
    content itself, which lives in immutable `ReportLetterheadVersion` rows.

    Letterheads are shared tenant resources: the same letterhead (and a
    specific published version of it) can be used as the starting point or
    active selection for multiple `ReportTemplate`s. See
    docs/celuma-1.3/post-phase-2-remediation/report-letterhead-domain-contract.md.
    """
    __tablename__ = "report_letterhead"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    name: str = Field(max_length=255)
    description: Optional[str] = Field(default=None)
    is_default: bool = Field(default=False)
    is_active: bool = Field(default=True)
    created_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
