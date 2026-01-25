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
    diagnosis_text: Optional[str] = Field(default=None)  # Quick extract; PDF is canonical
    published_at: Optional[datetime] = Field(default=None)
    created_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    
    # Basic relationships only
    versions: List["ReportVersion"] = Relationship(back_populates="report")

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
