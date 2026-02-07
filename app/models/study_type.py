from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field
from .base import BaseModel, TimestampMixin, TenantMixin


class StudyType(BaseModel, TimestampMixin, TenantMixin, table=True):
    """Study type model for categorizing laboratory studies/services"""
    __tablename__ = "study_type"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    code: str = Field(max_length=50, index=True)  # e.g., "BIOPSIA", "CITOLOGIA"
    name: str = Field(max_length=255)  # e.g., "Biopsia de tejido"
    description: Optional[str] = Field(default=None)
    is_active: bool = Field(default=True)
    default_report_template_id: Optional[UUID] = Field(foreign_key="report_template.id", default=None)
