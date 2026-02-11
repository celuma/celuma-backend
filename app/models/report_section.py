from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field
from .base import BaseModel, TimestampMixin, TenantMixin


class ReportSection(BaseModel, TimestampMixin, TenantMixin, table=True):
    """Report section model for cataloging report sections"""
    __tablename__ = "report_section"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    created_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    section: str = Field(max_length=255)  # Section name
    description: Optional[str] = Field(default=None)
    predefined_text: Optional[str] = Field(default=None)  # Optional predefined text for the section
