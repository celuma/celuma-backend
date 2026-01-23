"""Schemas for worklist, assignments, and report reviews"""
from typing import Optional, List, Literal
from datetime import datetime
from pydantic import BaseModel


# === User Reference Schemas ===

class UserRef(BaseModel):
    """Reference to a user with basic info"""
    id: str
    name: str
    email: str
    avatar_url: Optional[str] = None


# === Assignment Schemas ===

class AssignmentCreate(BaseModel):
    """Create a new assignment"""
    item_type: Literal["lab_order", "sample", "report"]
    item_id: str
    assignee_user_id: str


class AssignmentResponse(BaseModel):
    """Response for a single assignment"""
    id: str
    tenant_id: str
    item_type: str
    item_id: str
    assignee_user_id: str
    assigned_by_user_id: Optional[str] = None
    assigned_at: datetime
    unassigned_at: Optional[datetime] = None
    # Enriched user info
    assignee: Optional[UserRef] = None
    assigned_by: Optional[UserRef] = None


class AssignmentsListResponse(BaseModel):
    """Response for listing assignments"""
    assignments: List[AssignmentResponse]


class AssigneesUpdate(BaseModel):
    """Update assignees for an item (replaces all)"""
    assignee_ids: List[str]


class ReviewersUpdate(BaseModel):
    """Update reviewers for an order (replaces all)"""
    reviewer_ids: List[str]


# === Report Review Schemas ===

class ReportReviewCreate(BaseModel):
    """Create a new report review assignment"""
    reviewer_user_id: str


class ReportReviewDecision(BaseModel):
    """Make a decision on a report review"""
    decision: Literal["approved", "rejected"]
    comment: Optional[str] = None


class ReportReviewResponse(BaseModel):
    """Response for a single report review"""
    id: str
    tenant_id: str
    order_id: str
    reviewer_user_id: str
    assigned_by_user_id: Optional[str] = None
    assigned_at: datetime
    decision_at: Optional[datetime] = None
    status: str  # PENDING, APPROVED, REJECTED
    # Enriched user info
    reviewer: Optional[UserRef] = None
    assigned_by: Optional[UserRef] = None


class ReportReviewsListResponse(BaseModel):
    """Response for listing report reviews"""
    reviews: List[ReportReviewResponse]


# === Worklist Schemas ===

class WorklistItemResponse(BaseModel):
    """
    Unified worklist item - represents either an assignment or a review.
    
    For assignments (kind='assignment'):
    - item_type: 'lab_order' | 'sample' | 'report'
    - item_status: status of the item (order status, sample state, etc.)
    
    For reviews (kind='review'):
    - item_type: always 'report'
    - item_status: status of the review (PENDING, APPROVED, REJECTED)
    """
    id: str  # Assignment ID or ReportReview ID
    kind: Literal["assignment", "review"]
    item_type: str  # lab_order, sample, report
    item_id: str
    
    # Display information
    display_id: str  # order_code, sample_code, or report title
    item_status: str  # Order/Sample/Report status or Review status
    
    # Timestamps
    assigned_at: datetime
    
    # Context information
    patient_name: Optional[str] = None
    patient_code: Optional[str] = None
    order_code: Optional[str] = None  # For samples/reports, the parent order
    
    # Tags/Labels (if any)
    tags: Optional[List[str]] = None
    
    # Frontend navigation link
    link: str  # e.g., /orders/{id}, /samples/{id}, /reports/{id}


class WorklistResponse(BaseModel):
    """Response for the unified worklist"""
    items: List[WorklistItemResponse]
    total: int
    page: int
    page_size: int
    has_more: bool
