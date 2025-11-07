from pydantic import BaseModel
from typing import Optional, Dict, Any, List
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
    report: Optional[Dict[str, Any]] = None  # JSON body to be uploaded to S3

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
    version_no: int | None = None
    status: str
    order_id: str
    tenant_id: str
    branch_id: str
    title: Optional[str] = None
    diagnosis_text: Optional[str] = None
    published_at: Optional[datetime] = None
    created_by: Optional[str] = None
    signed_by: Optional[str] = None
    signed_at: Optional[datetime] = None
    report: Optional[Dict[str, Any]] = None  # reconstructed JSON from S3

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


class ReportMetaResponse(BaseModel):
    """Lightweight report metadata for case listings."""
    id: str
    status: str
    title: Optional[str] = None
    published_at: Optional[datetime] = None
    version_no: Optional[int] = None
    has_pdf: bool = False

# Schemas for enriched list responses
class BranchRef(BaseModel):
    """Reference to a branch with basic info"""
    id: str
    name: str
    code: Optional[str] = None

class PatientRef(BaseModel):
    """Reference to a patient with basic info"""
    id: str
    full_name: str
    patient_code: str

class OrderRef(BaseModel):
    """Reference to an order with basic info"""
    id: str
    order_code: str
    status: str
    requested_by: Optional[str] = None
    patient: Optional[PatientRef] = None

class ReportListItem(BaseModel):
    """Enriched report item for list view"""
    id: str
    status: str
    tenant_id: str
    branch: BranchRef
    order: OrderRef
    title: Optional[str] = None
    diagnosis_text: Optional[str] = None
    published_at: Optional[datetime] = None
    created_at: Optional[str] = None
    created_by: Optional[str] = None
    signed_by: Optional[str] = None
    signed_at: Optional[datetime] = None
    version_no: Optional[int] = None
    has_pdf: bool = False

class ReportsListResponse(BaseModel):
    """Response schema for reports list"""
    reports: List[ReportListItem]

# Schemas for report state transitions
class ReportStatusUpdate(BaseModel):
    """Schema for updating report status"""
    changelog: Optional[str] = None

class ReportSignRequest(BaseModel):
    """Schema for signing a report"""
    changelog: Optional[str] = None

class ReportReviewComment(BaseModel):
    """Schema for review comments"""
    comment: str
    request_changes: bool = False

class ReportActionResponse(BaseModel):
    """Generic response for report actions"""
    id: str
    status: str
    message: str
