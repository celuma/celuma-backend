from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4
from sqlalchemy import CheckConstraint
from sqlmodel import SQLModel, Field
from .base import BaseModel, TimestampMixin, TenantMixin


class StudyType(BaseModel, TimestampMixin, TenantMixin, table=True):
    """Study type model for categorizing laboratory studies/services"""
    __tablename__ = "study_type"
    __table_args__ = (
        CheckConstraint("removed_at IS NULL OR is_active = false", name="ck_study_type_removed_inactive"),
        CheckConstraint("code_released_at IS NULL OR removed_at IS NOT NULL", name="ck_study_type_code_release_removed"),
    )
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    code: str = Field(max_length=50, index=True)  # e.g., "BIOPSIA", "CITOLOGIA"
    name: str = Field(max_length=255)  # e.g., "Biopsia de tejido"
    description: Optional[str] = Field(default=None)
    is_active: bool = Field(default=True)
    default_report_template_id: Optional[UUID] = Field(foreign_key="report_template.id", default=None)
    # Céluma 1.5 (#94): removal from the catalog (a retained tombstone, distinct from deactivation). A removed
    # type keeps its row, id, code and every reference (orders, prices, invoices, numbering), so historical
    # records still resolve it; it is hidden from the ordinary catalog and refused for new records. Removal
    # implies `is_active = false` (database check `ck_study_type_removed_inactive`). Unlike orders/samples
    # (#11) there is NO global ORM filter: `Session.get` must keep resolving it for historical reads.
    # Contract: celuma-engineering/specs/laboratory/catalog-type-removal.md.
    removed_at: Optional[datetime] = Field(default=None)
    removed_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    removal_reason: Optional[str] = Field(default=None, max_length=500)
    # Céluma 1.5 (#94, owner decision 2026-11-25): set by the removal only when no order (withdrawn ones
    # included) references the type; its code may then be used by a DIFFERENT study type. The row keeps it.
    code_released_at: Optional[datetime] = Field(default=None)
