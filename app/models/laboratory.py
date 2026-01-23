from datetime import datetime
from typing import Optional, List, Dict, Any
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field, Relationship, Column
from sqlalchemy import JSON
from .base import BaseModel, TimestampMixin, TenantMixin, BranchMixin
from .enums import OrderStatus, SampleType, SampleState

class LabOrder(BaseModel, TimestampMixin, TenantMixin, BranchMixin, table=True):
    """Laboratory order model
    
    Note: Assignees and reviewers are now stored in the 'assignment' table.
    Use the Assignment model to query/manage assignees and reviewers.
    """
    __tablename__ = "lab_order"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    branch_id: UUID = Field(foreign_key="branch.id")
    patient_id: UUID = Field(foreign_key="patient.id")
    order_code: str = Field(max_length=100)  # Visible in UI, unique per branch
    status: OrderStatus = Field(default=OrderStatus.RECEIVED)
    requested_by: Optional[str] = Field(max_length=255, default=None)  # External requesting physician
    notes: Optional[str] = Field(default=None)
    billed_lock: bool = Field(default=False)  # Lock release if no payment
    created_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    # NOTE: assignees and reviewers columns removed - now in 'assignment' table
    
    # Basic relationships only
    samples: List["Sample"] = Relationship(back_populates="order")

class Sample(BaseModel, TenantMixin, BranchMixin, table=True):
    """Sample model for laboratory samples
    
    Note: Assignees are now stored in the 'assignment' table.
    Use the Assignment model to query/manage assignees.
    """
    __tablename__ = "sample"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    branch_id: UUID = Field(foreign_key="branch.id")
    order_id: UUID = Field(foreign_key="lab_order.id")
    sample_code: str = Field(max_length=100)  # Unique per order or branch
    type: SampleType
    state: SampleState = Field(default=SampleState.RECEIVED)
    collected_at: Optional[datetime] = Field(default=None)
    received_at: Optional[datetime] = Field(default=None)
    notes: Optional[str] = Field(default=None)
    # NOTE: assignees column removed - now in 'assignment' table
    
    # Basic relationships only
    order: LabOrder = Relationship(back_populates="samples")
    images: List["SampleImage"] = Relationship(back_populates="sample")

class SampleImage(BaseModel, TimestampMixin, TenantMixin, BranchMixin, table=True):
    """Sample image model linking samples to storage objects"""
    __tablename__ = "sample_image"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    branch_id: UUID = Field(foreign_key="branch.id")
    sample_id: UUID = Field(foreign_key="sample.id")
    storage_id: UUID = Field(foreign_key="storage_object.id")
    label: Optional[str] = Field(max_length=255, default=None)  # macro/micro, staining, etc.
    is_primary: bool = Field(default=False)
    created_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    
    # Basic relationships only
    sample: Sample = Relationship(back_populates="images")
    renditions: List["SampleImageRendition"] = Relationship(back_populates="sample_image")

class OrderComment(BaseModel, TimestampMixin, TenantMixin, BranchMixin, table=True):
    """Order comment model for normalized conversation"""
    __tablename__ = "order_comment"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    branch_id: UUID = Field(foreign_key="branch.id")
    order_id: UUID = Field(foreign_key="lab_order.id")
    # created_at inherited from TimestampMixin
    created_by: UUID = Field(foreign_key="app_user.id")
    text: str = Field(max_length=5000)
    comment_metadata: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    edited_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)

class OrderCommentMention(BaseModel, table=True):
    """Mention relationship for order comments"""
    __tablename__ = "order_comment_mention"
    
    comment_id: UUID = Field(foreign_key="order_comment.id", primary_key=True)
    user_id: UUID = Field(foreign_key="app_user.id", primary_key=True)

class Label(BaseModel, TimestampMixin, TenantMixin, table=True):
    """Label model for categorizing orders and samples"""
    __tablename__ = "label"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(max_length=100, index=True)
    color: str = Field(max_length=7)  # Hex color code like #FF5733
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    
    # Override updated_at to ensure it has a default value
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class LabOrderLabel(BaseModel, table=True):
    """Junction table for order-label many-to-many relationship"""
    __tablename__ = "lab_order_labels"
    
    order_id: UUID = Field(foreign_key="lab_order.id", primary_key=True, ondelete="CASCADE")
    label_id: UUID = Field(foreign_key="label.id", primary_key=True, ondelete="CASCADE")

class SampleLabel(BaseModel, table=True):
    """Junction table for sample-label many-to-many relationship
    
    Note: These are ADDITIONAL labels specific to the sample.
    Labels from the order are inherited automatically.
    """
    __tablename__ = "sample_labels"
    
    sample_id: UUID = Field(foreign_key="sample.id", primary_key=True, ondelete="CASCADE")
    label_id: UUID = Field(foreign_key="label.id", primary_key=True, ondelete="CASCADE")
