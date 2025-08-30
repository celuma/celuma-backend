from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class ReportCreate(BaseModel):
    """Schema for creating a report"""
    tenant_id: str
    branch_id: str
    order_id: str
    title: Optional[str] = None
    diagnosis_text: Optional[str] = None
    created_by: Optional[str] = None
    published_at: Optional[datetime] = None

class ReportResponse(BaseModel):
    """Schema for report response"""
    id: str
    status: str
    order_id: str
    tenant_id: str
    branch_id: str

class ReportDetailResponse(BaseModel):
    """Schema for detailed report response"""
    id: str
    status: str
    order_id: str
    tenant_id: str
    branch_id: str
    title: Optional[str] = None
    diagnosis_text: Optional[str] = None
    published_at: Optional[datetime] = None

class ReportVersionCreate(BaseModel):
    """Schema for creating a report version"""
    report_id: str
    version_no: int
    pdf_storage_id: str
    html_storage_id: Optional[str] = None
    changelog: Optional[str] = None
    authored_by: Optional[str] = None
    authored_at: Optional[datetime] = None

class ReportVersionResponse(BaseModel):
    """Schema for report version response"""
    id: str
    version_no: int
    report_id: str
    is_current: bool
