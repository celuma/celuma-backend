from datetime import datetime
from typing import Optional, List, Dict, Any
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import JSON
from .base import BaseModel, TimestampMixin, TenantMixin, BranchMixin
from .enums import ReportStatus

class Report(BaseModel, TimestampMixin, TenantMixin, BranchMixin, table=True):
    """Report model for laboratory reports"""
    __tablename__ = "report"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    branch_id: UUID = Field(foreign_key="branch.id")
    order_id: UUID = Field(foreign_key="order.id")
    status: ReportStatus = Field(default=ReportStatus.DRAFT)
    title: Optional[str] = Field(max_length=500, default=None)
    template: Optional[Dict[str, Any]] = Field(sa_type=JSON, default=None)
    published_at: Optional[datetime] = Field(default=None)
    created_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    
    # Basic relationships only
    versions: List["ReportVersion"] = Relationship(back_populates="report")
    # No Report.order relationship: use Report.order_id + session.get(Order, report.order_id)
    # to avoid AmbiguousForeignKeysError with Order.report_id

class ReportVersion(BaseModel, TimestampMixin, table=True):
    """Report version model for versioning reports"""
    __tablename__ = "report_version"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    report_id: UUID = Field(foreign_key="report.id")
    version_no: int  # 1..N
    pdf_storage_id: Optional[UUID] = Field(foreign_key="storage_object.id", default=None)
    json_storage_id: Optional[UUID] = Field(foreign_key="storage_object.id", default=None)
    html_storage_id: Optional[UUID] = Field(foreign_key="storage_object.id", default=None)
    changelog: Optional[str] = Field(default=None)
    authored_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    authored_at: datetime = Field(default_factory=datetime.utcnow)
    is_current: bool = Field(default=False)
    signed_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    signed_at: Optional[datetime] = Field(default=None)

    # Céluma 1.3 Phase 2, Block B: V2 report metadata. Nullable/additive —
    # existing (legacy) rows are never backfilled. See
    # report-schema-versioning.md and phase-2-block-b-architecture-decision.md.
    # `rendering_snapshot` itself is NOT a column here: it is embedded in the
    # JSON body already stored via `json_storage_id`, to avoid two sources of
    # truth for the same document (see B4 decision).
    schema_version: Optional[int] = Field(default=None)
    template_version_id: Optional[UUID] = Field(
        foreign_key="report_template_version.id", default=None
    )
    generated_by_renderer_version: Optional[str] = Field(max_length=100, default=None)

    # Céluma 1.3 Phase 2, Block E: official PDF artifact metadata. Nullable/
    # additive — existing rows are never backfilled. NULL
    # `pdf_generation_status` means "no generation attempt has ever run
    # through ReportPdfGenerationService" (distinct from a PDF that may
    # already sit behind `pdf_storage_id` via the legacy manual upload
    # endpoints, which never set these fields). See
    # pdf-generation-contract.md and pdf-storage-integrity-contract.md.
    pdf_generation_status: Optional[str] = Field(max_length=20, default=None)
    pdf_generation_started_at: Optional[datetime] = Field(default=None)
    pdf_generated_at: Optional[datetime] = Field(default=None)
    pdf_sha256: Optional[str] = Field(max_length=64, default=None)
    pdf_size_bytes: Optional[int] = Field(default=None)
    pdf_page_count: Optional[int] = Field(default=None)
    pdf_generator_version: Optional[str] = Field(max_length=100, default=None)
    pdf_error_code: Optional[str] = Field(max_length=50, default=None)
    pdf_error_message: Optional[str] = Field(max_length=500, default=None)

    # Post-Phase-2 remediation: administrative/audit twin of
    # `template_version_id` — records which letterhead version produced
    # this report version's `presentation` block. Nullable/additive,
    # never backfilled. NOT the source of truth for rendering: that
    # remains the embedded `rendering_snapshot.presentation` in the JSON
    # body (see report-letterhead-version-contract.md).
    letterhead_version_id: Optional[UUID] = Field(
        foreign_key="report_letterhead_version.id", default=None
    )

    # Post-Phase-2 UX remediation: lightweight claim guarding sign-and-publish
    # against double-firma/double-Chromium/concurrent-publish. Mirrors the
    # pdf_generation_started_at staleness pattern. Cleared on both success
    # and failure; a stale (crashed) claim is recoverable, never a permanent
    # lock. See signed-pdf-publication-workflow.md.
    publish_started_at: Optional[datetime] = Field(default=None)
    publish_started_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)

    # Basic relationships only
    report: Report = Relationship(back_populates="versions")


class ReportTemplate(BaseModel, TimestampMixin, TenantMixin, table=True):
    """Template model for storing report templates in JSON format"""
    __tablename__ = "report_template"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    name: str = Field(max_length=255)
    description: Optional[str] = Field(default=None)
    template_json: Dict[str, Any] = Field(sa_type=JSON, default={})
    created_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    is_active: bool = Field(default=True)

    # Post-Phase-2 remediation: administrative preference, not ownership.
    # When a report is created from this clinical template, this is the
    # letterhead version preselected (before falling back to the tenant's
    # default letterhead) — see template-letterhead-association-contract.md.
    # A NULL value does not mean "no branding"; it means "resolve the
    # tenant default at creation time."
    preferred_letterhead_version_id: Optional[UUID] = Field(
        foreign_key="report_letterhead_version.id", default=None
    )

    # Second post-Phase-2 remediation (UX): the logical letterhead preferred
    # for this template, not a specific version. This is the field the app
    # writes going forward — `preferred_letterhead_version_id` above becomes
    # read-only, kept only so old rows keep resolving. See
    # template-simplification-contract.md and
    # report-letterhead-selection-ux.md for the full resolution order.
    preferred_letterhead_id: Optional[UUID] = Field(
        foreign_key="report_letterhead.id", default=None
    )
