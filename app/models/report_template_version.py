from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field
from sqlalchemy import JSON, Column, String
from .base import BaseModel, TimestampMixin, TenantMixin


class ReportTemplateVersionStatus(str, Enum):
    """Lifecycle of an immutable, published report-template configuration.

    Céluma 1.3 Fase 2, Bloque B. There is no DRAFT state in this block: a
    version is born PUBLISHED (immutable from the moment it is inserted).
    Only `status` may transition afterwards, exclusively via the dedicated
    /activate and /archive endpoints — never a generic PUT/PATCH. See
    report-template-version-contract.md for the full state machine.
    """
    PUBLISHED = "PUBLISHED"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ReportTemplateVersion(BaseModel, TimestampMixin, TenantMixin, table=True):
    """Append-only, immutable snapshot of a report template's rendering
    configuration (clinical structure + presentation + branding).

    `configuration` is validated against `ReportRenderingSnapshotV2`
    (app/schemas/report_template_version.py) before insert and is never
    updated afterwards — corrections require creating a new version. This
    entity is the *administrative/audit* record of what was published; it is
    NOT the source of truth used to reconstruct an already-created report.
    That source of truth is the immutable snapshot embedded in the report's
    own JSON body at the time it was created (see
    phase-2-block-b-architecture-decision.md). `VersionedReportRendererV2`
    must never query this table to render an existing report.
    """
    __tablename__ = "report_template_version"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    report_template_id: UUID = Field(foreign_key="report_template.id")
    version_number: int
    schema_version: int = Field(default=2)
    configuration: Dict[str, Any] = Field(sa_type=JSON)
    status: ReportTemplateVersionStatus = Field(
        default=ReportTemplateVersionStatus.PUBLISHED,
        sa_column=Column(String(20), nullable=False, server_default="PUBLISHED"),
    )
    created_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    published_at: datetime = Field(default_factory=datetime.utcnow)
    activated_at: Optional[datetime] = Field(default=None)
    archived_at: Optional[datetime] = Field(default=None)
