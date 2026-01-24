from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from datetime import datetime

# Import ReviewerWithStatus from worklist schema
class ReviewerWithStatus(BaseModel):
    """User with review status"""
    id: str
    name: str
    email: str
    avatar_url: Optional[str] = None
    status: str  # pending, approved, rejected
    review_id: Optional[str] = None

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
    reviewers: Optional[List[ReviewerWithStatus]] = None

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


# Report Template Schemas
class ReportTemplateCreate(BaseModel):
    """Schema for creating a report template"""
    name: str
    description: Optional[str] = None
    template_json: Dict[str, Any]


class ReportTemplateUpdate(BaseModel):
    """Schema for updating a report template"""
    name: Optional[str] = None
    description: Optional[str] = None
    template_json: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None


class ReportTemplateResponse(BaseModel):
    """Schema for basic report template response"""
    id: str
    tenant_id: str
    name: str
    description: Optional[str] = None
    is_active: bool
    created_at: datetime


class ReportTemplateDetailResponse(BaseModel):
    """Schema for detailed report template response with full JSON"""
    id: str
    tenant_id: str
    name: str
    description: Optional[str] = None
    template_json: Dict[str, Any]
    created_by: Optional[str] = None
    is_active: bool
    created_at: datetime


class ReportTemplatesListResponse(BaseModel):
    """Response schema for report templates list"""
    templates: List[ReportTemplateResponse]
