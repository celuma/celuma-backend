from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Index, text
from sqlmodel import Field

from .base import BaseModel, TimestampMixin, TenantMixin


class SampleType(BaseModel, TimestampMixin, TenantMixin, table=True):
    """Tenant-owned catalog of sample types (Céluma 1.4, issue #17).

    Persisted-field parity with `StudyType`; a separate table with its own
    code namespace per tenant. It replaces the retired `SampleType` enum as
    the source of a sample's type.

    `default_report_template_id` (Céluma 1.4.1): the report template this type
    suggests. A new report of an order uses it when the order has no explicit
    choice and its samples agree on one template; it outranks the study type's
    (`app/services/order_report_template.py`,
    `celuma-engineering/specs/laboratory/order-report-template.md`). Until
    1.4.1 the column was mirrored for parity only and had no effect.
    """
    __tablename__ = "sample_type"
    __table_args__ = (
        # Céluma 1.5 (#94): codes are unique among the rows that RESERVE them. A removed type with no samples
        # releases its code (`code_released_at`) but keeps it in its own row; until 1.5 this was the full
        # `uq_sample_type_tenant_code` constraint.
        Index("uq_sample_type_tenant_code_reserved", "tenant_id", "code", unique=True,
              postgresql_where=text("code_released_at IS NULL")),
        Index("ix_sample_type_tenant_active", "tenant_id", "is_active"),
        CheckConstraint("removed_at IS NULL OR is_active = false", name="ck_sample_type_removed_inactive"),
        CheckConstraint("code_released_at IS NULL OR removed_at IS NOT NULL", name="ck_sample_type_code_release_removed"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    code: str = Field(max_length=50, index=True)
    name: str = Field(max_length=255)
    description: Optional[str] = Field(default=None)
    is_active: bool = Field(default=True)
    default_report_template_id: Optional[UUID] = Field(foreign_key="report_template.id", default=None)
    # Céluma 1.5 (#94): removal from the catalog, a retained tombstone distinct from deactivation (see
    # `StudyType.removed_at`). `code_released_at` is set by the removal only when no sample (withdrawn ones
    # included) references the type: its code may then be used by a DIFFERENT type; the row keeps it.
    removed_at: Optional[datetime] = Field(default=None)
    removed_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    removal_reason: Optional[str] = Field(default=None, max_length=500)
    code_released_at: Optional[datetime] = Field(default=None)
