from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any
from uuid import UUID, uuid4
from sqlmodel import Field
from sqlalchemy import JSON, Column, String
from .base import BaseModel, TimestampMixin, TenantMixin


class ReportLetterheadVersionStatus(str, Enum):
    """Lifecycle of an immutable, published letterhead presentation.

    Identical semantics to `ReportTemplateVersionStatus`: born PUBLISHED
    (immutable from insert), `status` transitions only via dedicated
    /activate and /archive endpoints — never a generic PUT/PATCH.
    """
    PUBLISHED = "PUBLISHED"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ReportLetterheadVersion(BaseModel, TimestampMixin, TenantMixin, table=True):
    """Append-only, immutable snapshot of a letterhead's presentation
    configuration (paper, margins, header, footer, logo, color, signer).

    `configuration` is validated against `ReportPresentationSnapshotV2`
    (app/schemas/report_template_version.py — the same Pydantic model
    already used inside `ReportRenderingSnapshotV2.presentation`, reused
    verbatim here to guarantee the two contracts never diverge) and is
    never updated afterwards — corrections require creating a new version.

    `configuration` never contains a `template` key: clinical structure is
    exclusively `ReportTemplate`/`ReportTemplateVersion`'s concern.
    """
    __tablename__ = "report_letterhead_version"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    report_letterhead_id: UUID = Field(foreign_key="report_letterhead.id")
    version_number: int
    schema_version: int = Field(default=2)
    configuration: Dict[str, Any] = Field(sa_type=JSON)
    status: ReportLetterheadVersionStatus = Field(
        default=ReportLetterheadVersionStatus.PUBLISHED,
        sa_column=Column(String(20), nullable=False, server_default="PUBLISHED"),
    )
    created_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    published_at: datetime = Field(default_factory=datetime.utcnow)
    activated_at: Optional[datetime] = Field(default=None)
    archived_at: Optional[datetime] = Field(default=None)
