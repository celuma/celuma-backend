"""ReportReview model for tracking individual review decisions on lab orders"""
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4
from sqlmodel import Field
from .base import BaseModel, TenantMixin
from .enums import ReviewStatus


class ReportReview(BaseModel, TenantMixin, table=True):
    """
    ReportReview model for tracking individual review decisions on lab orders.
    
    Each reviewer gets their own record per order, allowing tracking of who approved/rejected
    and when. The order's report global status is determined by the review decisions
    (MVP rule: ≥1 approved = report approved).
    
    Reviews are tied to orders, and optionally to specific reports.
    Reviewers can be assigned before a report exists (report_id will be NULL).
    """
    __tablename__ = "report_review"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    
    # Link to the order (NOT NULL - reviews are per order)
    order_id: UUID = Field(foreign_key="order.id", index=True)
    
    # Link to the specific report (NULLABLE - can be assigned before report exists)
    report_id: Optional[UUID] = Field(foreign_key="report.id", index=True, default=None)
    
    # Who is reviewing
    reviewer_user_id: UUID = Field(foreign_key="app_user.id", index=True)
    
    # Who assigned them as reviewer (optional)
    assigned_by_user_id: Optional[UUID] = Field(
        foreign_key="app_user.id", 
        default=None
    )
    
    # Timestamps
    assigned_at: datetime = Field(default_factory=datetime.utcnow)
    decision_at: Optional[datetime] = Field(default=None)
    
    # Review status and decision
    status: ReviewStatus = Field(default=ReviewStatus.PENDING, index=True)
