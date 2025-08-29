from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime

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
