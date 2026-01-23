"""Assignment model for tracking user assignments to orders, samples, and reports"""
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4
from sqlmodel import Field, Column
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from .base import BaseModel, TenantMixin
from .enums import AssignmentItemType


class Assignment(BaseModel, TenantMixin, table=True):
    """
    Assignment model for tracking user assignments to various items.
    
    Supports both assignees (is_reviewer=False) and reviewers (is_reviewer=True).
    When is_reviewer=True and item_type='lab_order', these represent reviewers
    that will be synced to report_review when the report is created.
    """
    __tablename__ = "assignment"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    
    # What type of item this assignment is for
    item_type: AssignmentItemType = Field(
        sa_column=Column(
            PG_ENUM('lab_order', 'sample', 'report', name='assignmentitemtype', create_type=False),
            nullable=False,
            index=True
        )
    )
    item_id: UUID = Field(index=True)
    
    # Who is assigned
    assignee_user_id: UUID = Field(foreign_key="app_user.id", index=True)
    
    # Who assigned them (optional)
    assigned_by_user_id: Optional[UUID] = Field(
        foreign_key="app_user.id", 
        default=None
    )
    
    # Timestamps
    assigned_at: datetime = Field(default_factory=datetime.utcnow)
    unassigned_at: Optional[datetime] = Field(default=None)
    
    # Distinguishes between assignees and reviewers
    # When True and item_type='lab_order', these are order reviewers
    # that will be synced to report_review when report is created
    is_reviewer: bool = Field(default=False, index=True)
