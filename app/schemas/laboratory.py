from pydantic import BaseModel, field_validator
from typing import Optional, List, Dict, Any
from datetime import datetime
from app.schemas.report import ReportMetaResponse
from app.schemas.patient import PatientFullResponse


# --- User Reference for Collaboration (defined early) ---

class UserRef(BaseModel):
    """Minimal user reference for assignees/reviewers"""
    id: str
    name: str
    email: str
    avatar_url: Optional[str] = None


class ReviewerWithStatus(UserRef):
    """User reference with review status for reviewers"""
    status: str  # "pending", "approved", "rejected"
    review_id: Optional[str] = None


# --- Label Schemas (defined early) ---

class LabelResponse(BaseModel):
    """Schema for label response"""
    id: str
    name: str
    color: str
    tenant_id: str
    created_at: datetime

class LabelWithInheritance(BaseModel):
    """Schema for label with inheritance flag (for samples)"""
    id: str
    name: str
    color: str
    inherited: bool  # True if inherited from order, False if own label


class OrderCreate(BaseModel):
    """Schema for creating an order"""
    tenant_id: str
    branch_id: str
    patient_id: str
    order_code: str
    requested_by: Optional[str] = None
    notes: Optional[str] = None
    created_by: Optional[str] = None

# Alias for backwards compatibility
LabOrderCreate = OrderCreate

class OrderResponse(BaseModel):
    """Schema for order response"""
    id: str
    order_code: str
    status: str
    patient_id: str
    tenant_id: str
    branch_id: str

# Alias for backwards compatibility
LabOrderResponse = OrderResponse

class OrderDetailResponse(BaseModel):
    """Schema for detailed order response"""
    id: str
    order_code: str
    status: str
    patient_id: str
    tenant_id: str
    branch_id: str
    requested_by: Optional[str] = None
    notes: Optional[str] = None
    billed_lock: Optional[bool] = None
    assignees: Optional[List[UserRef]] = None
    reviewers: Optional[List[ReviewerWithStatus]] = None
    labels: Optional[List[LabelResponse]] = None

# Alias for backwards compatibility
LabOrderDetailResponse = OrderDetailResponse


class OrderNotesUpdate(BaseModel):
    """Schema for updating order notes/description"""
    notes: Optional[str] = None


# --- Collaboration: Labels ---

class LabelCreate(BaseModel):
    """Schema for creating a label"""
    name: str
    color: str
    
    @field_validator('name')
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError('Name cannot be empty')
        if len(v) > 100:
            raise ValueError('Name cannot exceed 100 characters')
        return v
    
    @field_validator('color')
    @classmethod
    def validate_color(cls, v: str) -> str:
        # Validate hex color format
        if not v.startswith('#') or len(v) != 7:
            raise ValueError('Color must be in hex format (#RRGGBB)')
        try:
            int(v[1:], 16)
        except ValueError:
            raise ValueError('Invalid hex color')
        return v.upper()

class LabelsListResponse(BaseModel):
    """Schema for labels list response"""
    labels: List[LabelResponse]


# --- Collaboration: Assignees and Reviewers ---

class AssigneesUpdate(BaseModel):
    """Schema for updating assignees"""
    assignee_ids: List[str]

class ReviewersUpdate(BaseModel):
    """Schema for updating reviewers"""
    reviewer_ids: List[str]

class LabelsUpdate(BaseModel):
    """Schema for updating labels"""
    label_ids: List[str]


# --- Order Comments ---

class MentionedUser(BaseModel):
    """User information for mentions"""
    user_id: str
    username: Optional[str] = None
    name: str
    avatar: Optional[str] = None

class CommentCreate(BaseModel):
    """Create new comment"""
    text: str
    mentions: List[str] = []  # List of user_id UUIDs
    metadata: Optional[Dict[str, Any]] = None
    
    @field_validator('text')
    @classmethod
    def validate_text(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError('Text cannot be empty')
        if len(v) > 5000:
            raise ValueError('Text cannot exceed 5000 characters')
        return v

class CommentResponse(BaseModel):
    """Comment response with resolved mentions"""
    id: str
    tenant_id: str
    branch_id: str
    order_id: str
    created_at: datetime
    created_by: str
    created_by_name: Optional[str] = None
    created_by_avatar: Optional[str] = None
    text: str
    mentions: List[str]
    mentioned_users: List[MentionedUser]
    metadata: Optional[Dict[str, Any]] = None
    edited_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None

class PageInfo(BaseModel):
    """Pagination information"""
    has_more: bool
    next_before: Optional[str] = None
    next_after: Optional[str] = None

class CommentsListResponse(BaseModel):
    """Paginated comments list"""
    items: List[CommentResponse]
    page_info: PageInfo


class UserMentionItem(BaseModel):
    """Schema for user mention suggestion"""
    id: str
    name: str
    username: Optional[str] = None
    email: str
    avatar_url: Optional[str] = None


class UserMentionListResponse(BaseModel):
    """Schema for user mention list response"""
    users: List[UserMentionItem]


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


class SampleStateUpdate(BaseModel):
    """Schema for updating sample state"""
    state: str  # RECEIVED, PROCESSING, READY


class SampleNotesUpdate(BaseModel):
    """Schema for updating sample notes/description"""
    notes: Optional[str] = None


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


class OrderUnifiedCreate(BaseModel):
    """Unified create: create lab order and one or more samples in one operation."""
    tenant_id: str
    branch_id: str
    patient_id: str
    order_code: str
    requested_by: Optional[str] = None
    notes: Optional[str] = None
    created_by: Optional[str] = None
    samples: List[UnifiedSampleCreate]


class OrderUnifiedResponse(BaseModel):
    """Response for unified create, returning order and samples created."""
    order: LabOrderResponse
    samples: List[SampleResponse]


class OrderFullDetailResponse(BaseModel):
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


# --- Shared small refs for enriched list endpoints ---

class BranchRef(BaseModel):
    """Minimal branch reference with id and human-readable name (and optional code)."""
    id: str
    name: str
    code: Optional[str] = None


class PatientRef(BaseModel):
    """Minimal patient reference for lists with full name and code."""
    id: str
    full_name: str
    patient_code: str


class OrderSlim(BaseModel):
    """Small order reference for sample lists/details."""
    id: str
    order_code: str
    status: str
    requested_by: Optional[str] = None
    patient: Optional[PatientRef] = None


class OrderListItem(BaseModel):
    """Order item for GET /laboratory/orders/ with enriched references."""
    id: str
    order_code: str
    status: str
    tenant_id: str
    branch: BranchRef
    patient: PatientRef
    requested_by: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[str] = None
    sample_count: int
    has_report: bool
    labels: Optional[List[LabelResponse]] = None


class OrdersListResponse(BaseModel):
    """Response for GET /laboratory/orders/ with enriched items."""
    orders: List[OrderListItem]


class SampleListItem(BaseModel):
    """Sample item with enriched branch and order objects (tenant_id kept as id)."""
    id: str
    sample_code: str
    type: str
    state: str
    tenant_id: str
    branch: BranchRef
    order: OrderSlim
    labels: Optional[List[LabelResponse]] = None


class SamplesListResponse(BaseModel):
    """Response for GET /laboratory/samples/ with enriched items."""
    samples: List[SampleListItem]


class SampleDetailResponse(BaseModel):
    """Complete detail for a sample including branch, order, and patient references."""
    id: str
    sample_code: str
    type: str
    state: str
    collected_at: Optional[str] = None
    received_at: Optional[str] = None
    notes: Optional[str] = None
    tenant_id: str
    branch: BranchRef
    order: OrderSlim
    patient: PatientRef
    assignees: Optional[List[UserRef]] = None
    labels: Optional[List[LabelWithInheritance]] = None  # Includes inherited + own labels
