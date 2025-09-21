from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime
from app.schemas.report import ReportMetaResponse
from app.schemas.patient import PatientFullResponse

class LabOrderCreate(BaseModel):
    """Schema for creating a laboratory order"""
    tenant_id: str
    branch_id: str
    patient_id: str
    order_code: str
    requested_by: Optional[str] = None
    notes: Optional[str] = None
    created_by: Optional[str] = None

class LabOrderResponse(BaseModel):
    """Schema for laboratory order response"""
    id: str
    order_code: str
    status: str
    patient_id: str
    tenant_id: str
    branch_id: str

class LabOrderDetailResponse(BaseModel):
    """Schema for detailed laboratory order response"""
    id: str
    order_code: str
    status: str
    patient_id: str
    tenant_id: str
    branch_id: str
    requested_by: Optional[str] = None
    notes: Optional[str] = None
    billed_lock: Optional[bool] = None

class SampleCreate(BaseModel):
    """Schema for creating a sample"""
    tenant_id: str
    branch_id: str
    order_id: str
    sample_code: str
    type: str
    notes: Optional[str] = None
    collected_at: Optional[datetime] = None
    received_at: Optional[datetime] = None

class SampleResponse(BaseModel):
    """Schema for sample response"""
    id: str
    sample_code: str
    type: str
    state: str
    order_id: str
    tenant_id: str
    branch_id: str


class SampleImageInfo(BaseModel):
    """Schema for a sample image with URLs to renditions."""
    id: str
    label: Optional[str] = None
    is_primary: bool
    created_at: str
    urls: Dict[str, str]


class SampleImagesListResponse(BaseModel):
    """Schema for list of sample images."""
    sample_id: str
    images: List[SampleImageInfo]


class SampleImageUploadResponse(BaseModel):
    """Schema for upload image response, similar to experiments mapping."""
    message: str
    sample_image_id: str
    filename: str
    is_raw: bool
    file_size: int
    urls: Dict[str, str]


class UnifiedSampleCreate(BaseModel):
    """Input schema for creating a sample as part of unified order creation."""
    sample_code: str
    type: str
    notes: Optional[str] = None
    collected_at: Optional[datetime] = None
    received_at: Optional[datetime] = None


class LabOrderUnifiedCreate(BaseModel):
    """Unified create: create lab order and one or more samples in one operation."""
    tenant_id: str
    branch_id: str
    patient_id: str
    order_code: str
    requested_by: Optional[str] = None
    notes: Optional[str] = None
    created_by: Optional[str] = None
    samples: List[UnifiedSampleCreate]


class LabOrderUnifiedResponse(BaseModel):
    """Response for unified create, returning order and samples created."""
    order: LabOrderResponse
    samples: List[SampleResponse]


class LabOrderFullDetailResponse(BaseModel):
    """Complete detail for a lab order: order, patient, and samples."""
    order: LabOrderDetailResponse
    patient: PatientFullResponse
    samples: List[SampleResponse]
    report: Optional[ReportMetaResponse] = None


class PatientCaseSummary(BaseModel):
    """Summary of a case (order) for a given patient."""
    id: str
    order_code: str
    status: str
    tenant_id: str
    branch_id: str
    created_at: Optional[str] = None


class PatientCaseDetail(BaseModel):
    """Detailed case info: order + samples + report meta (if any)."""
    order: LabOrderDetailResponse
    samples: List[SampleResponse]
    report: Optional[ReportMetaResponse] = None


class PatientCasesListResponse(BaseModel):
    """List of cases (orders) for a patient, including full patient profile."""
    patient: PatientFullResponse
    patient_id: str
    cases: List[PatientCaseDetail]


class PatientOrderSummary(BaseModel):
    """Enriched summary of an order for list endpoint."""
    id: str
    order_code: str
    status: str
    tenant_id: str
    branch_id: str
    patient_id: str
    requested_by: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[str] = None
    sample_count: int
    has_report: bool


class PatientOrdersListResponse(BaseModel):
    """List of orders for a patient (summary) with full patient profile."""
    patient: PatientFullResponse
    orders: List[PatientOrderSummary]
