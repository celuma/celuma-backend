from pydantic import BaseModel, field_validator, model_validator
from typing import Optional, List, Dict, Any
from datetime import datetime
from app.schemas.report import ReportMetaResponse, ReportDetailResponse
from app.schemas.patient import PatientFullResponse
from app.schemas.sample_type import SampleTypeRef


def _reject_retired_type_field(data):
    """The `type` enum was retired in Céluma 1.4 (issue #17): a request that
    still sends it is refused explicitly instead of silently ignored."""
    if isinstance(data, dict) and "type" in data:
        raise ValueError("'type' was removed in Céluma 1.4; send 'sample_type_id' (an active sample type of your laboratory)")
    return data


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


class RequestingPhysicianRef(BaseModel):
    """Minimal requesting physician reference for lists and details."""
    id: str
    full_name: str
    physician_code: str
    specialty: Optional[str] = None
    institution: Optional[str] = None
    email: Optional[str] = None


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
    patient_id: Optional[str] = None
    requesting_physician_id: Optional[str] = None
    study_type_id: str
    order_code: Optional[str] = None
    requested_by: Optional[str] = None
    notes: Optional[str] = None
    created_by: Optional[str] = None


class OrderResponse(BaseModel):
    """Schema for order response"""
    id: str
    order_code: str
    status: str
    patient_id: Optional[str] = None
    tenant_id: str
    branch_id: str


class OrderDetailResponse(BaseModel):
    """Schema for detailed order response"""
    id: str
    order_code: str
    status: str
    patient_id: Optional[str] = None
    requesting_physician_id: Optional[str] = None
    tenant_id: str
    branch_id: str
    requested_by: Optional[str] = None
    requesting_physician: Optional[RequestingPhysicianRef] = None
    notes: Optional[str] = None
    billed_lock: Optional[bool] = None
    report_id: Optional[str] = None
    invoice_id: Optional[str] = None
    study_type_id: Optional[str] = None
    assignees: Optional[List[UserRef]] = None
    reviewers: Optional[List[ReviewerWithStatus]] = None
    labels: Optional[List[LabelResponse]] = None


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


class AssignableUsersListResponse(BaseModel):
    """Active tenant users, paginated by their stable UUID identity."""
    users: List[UserMentionItem]
    next_after: Optional[str] = None


class SampleCreate(BaseModel):
    """Schema for creating a sample"""
    tenant_id: str
    branch_id: str
    order_id: str
    #: Optional (Céluma 1.4, #8): omitted/blank = generated monthly code; supplied = manual override.
    sample_code: Optional[str] = None
    sample_type_id: str  # required: every new sample has an active catalog type
    notes: Optional[str] = None
    collected_at: Optional[datetime] = None
    received_at: Optional[datetime] = None

    @model_validator(mode="before")
    @classmethod
    def _no_legacy_type(cls, data):
        return _reject_retired_type_field(data)

class SampleResponse(BaseModel):
    """Schema for sample response"""
    id: str
    sample_code: str
    #: Catalog type; null for a legacy sample ("Sin tipo").
    sample_type: Optional[SampleTypeRef] = None
    state: str
    order_id: str
    tenant_id: str
    branch_id: str
    collected_at: Optional[datetime] = None
    received_at: Optional[datetime] = None
    assignees: Optional[List[UserRef]] = None
    labels: Optional[List[LabelWithInheritance]] = None


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
    #: Optional (Céluma 1.4, #8): omitted/blank = generated monthly code; supplied = manual override.
    sample_code: Optional[str] = None
    sample_type_id: str
    notes: Optional[str] = None
    collected_at: Optional[datetime] = None
    received_at: Optional[datetime] = None

    @model_validator(mode="before")
    @classmethod
    def _no_legacy_type(cls, data):
        return _reject_retired_type_field(data)


class OrderUnifiedCreate(BaseModel):
    """Unified create: create lab order and one or more samples in one operation."""
    tenant_id: str
    branch_id: str
    patient_id: Optional[str] = None
    requesting_physician_id: Optional[str] = None
    study_type_id: str  # Obligatorio
    order_code: Optional[str] = None  # Optional, generated automatically
    requested_by: Optional[str] = None
    notes: Optional[str] = None
    created_by: Optional[str] = None
    samples: List[UnifiedSampleCreate]


class OrderUnifiedResponse(BaseModel):
    """Response for unified create, returning order and samples created."""
    order: OrderResponse
    samples: List[SampleResponse]


class OrderFullDetailResponse(BaseModel):
    """Complete detail for a lab order: order, optional patient, and samples."""
    order: OrderDetailResponse
    patient: Optional[PatientFullResponse] = None
    samples: List[SampleResponse]
    report: Optional[ReportMetaResponse] = None


class ReportFullDetailResponse(BaseModel):
    """Full detail for a report: order, optional patient, samples, and complete report data."""
    order: OrderDetailResponse
    patient: Optional[PatientFullResponse] = None
    samples: List[SampleResponse]
    report: ReportDetailResponse


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
    order: OrderDetailResponse
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
    patient_id: Optional[str] = None
    requested_by: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[str] = None
    report_id: Optional[str] = None
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
    requesting_physician: Optional[RequestingPhysicianRef] = None
    patient: Optional[PatientRef] = None


class OrderListItem(BaseModel):
    """Order item for GET /laboratory/orders/ with enriched references."""
    id: str
    order_code: str
    status: str
    tenant_id: str
    branch: BranchRef
    patient: Optional[PatientRef] = None
    requesting_physician: Optional[RequestingPhysicianRef] = None
    requested_by: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[str] = None
    report_id: Optional[str] = None
    invoice_id: Optional[str] = None
    study_type_id: Optional[str] = None
    sample_count: int
    has_report: bool
    has_invoice: bool
    labels: Optional[List[LabelResponse]] = None
    assignees: Optional[List[UserRef]] = None


class OrdersListResponse(BaseModel):
    """Response for GET /laboratory/orders/ with enriched items.

    Céluma 1.4: `total` is the number of orders matching the request's
    search/filters across the whole tenant. `page`/`page_size` are set only in
    paged mode (the request carried `page` or `page_size`); without them the
    response is the whole filtered collection, as before 1.4.
    """
    orders: List[OrderListItem]
    total: Optional[int] = None
    page: Optional[int] = None
    page_size: Optional[int] = None


class SampleListItem(BaseModel):
    """Sample item with enriched branch and order objects (tenant_id kept as id)."""
    id: str
    sample_code: str
    sample_type: Optional[SampleTypeRef] = None
    state: str
    tenant_id: str
    branch: BranchRef
    order: OrderSlim
    labels: Optional[List[LabelResponse]] = None
    assignees: Optional[List[UserRef]] = None


class SamplesListResponse(BaseModel):
    """Response for GET /laboratory/samples/ with enriched items.

    `total`/`page`/`page_size` follow `OrdersListResponse` (Céluma 1.4).
    """
    samples: List[SampleListItem]
    total: Optional[int] = None
    page: Optional[int] = None
    page_size: Optional[int] = None


class FilterOption(BaseModel):
    """One choice of a list column filter (Céluma 1.4)."""
    value: str
    text: str
    color: Optional[str] = None


class SampleFilterOptionsResponse(BaseModel):
    """Filter catalogs for the samples list, over the tenant's whole collection.

    Each catalog holds the values actually present among the tenant's samples,
    which is what the pre-1.4 client derived from the full downloaded list.
    """
    states: List[str]
    #: Types referenced by the tenant's samples (deactivated ones included).
    sample_types: List[FilterOption]
    #: Whether any sample has no type: the list then offers "Sin tipo"
    #: (filter value `none`).
    has_untyped_samples: bool
    orders: List[FilterOption]
    patients: List[FilterOption]
    labels: List[FilterOption]
    assignees: List[FilterOption]


class OrderFilterOptionsResponse(BaseModel):
    """Filter catalogs for the orders list, over the tenant's whole collection."""
    statuses: List[str]
    patients: List[FilterOption]
    requesting_physicians: List[FilterOption]
    labels: List[FilterOption]
    assignees: List[FilterOption]
    #: Whether any order names a requester (catalog physician or free text);
    #: the list shows its "Solicitante" column only then.
    has_requester: bool


class SampleDetailResponse(BaseModel):
    """Complete detail for a sample including branch, order, and patient references."""
    id: str
    sample_code: str
    sample_type: Optional[SampleTypeRef] = None
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
