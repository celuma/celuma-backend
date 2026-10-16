from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field

from .base import BaseModel, TimestampMixin, TenantMixin


class SampleType(BaseModel, TimestampMixin, TenantMixin, table=True):
    """Tenant-owned catalog of sample types (Céluma 1.4, issue #17).

    Persisted-field parity with `StudyType`; a separate table with its own
    code namespace per tenant. It replaces the retired `SampleType` enum as
    the source of a sample's type.

    `default_report_template_id` mirrors the study-type column for parity and
    import/copy fidelity only. Report-default resolution reads the *study
    type* of the order and never this column
    (`celuma-engineering/specs/laboratory/sample-types.md`).
    """
    __tablename__ = "sample_type"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_sample_type_tenant_code"),
        Index("ix_sample_type_tenant_active", "tenant_id", "is_active"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    code: str = Field(max_length=50, index=True)
    name: str = Field(max_length=255)
    description: Optional[str] = Field(default=None)
    is_active: bool = Field(default=True)
    default_report_template_id: Optional[UUID] = Field(foreign_key="report_template.id", default=None)
