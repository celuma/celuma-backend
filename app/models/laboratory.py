from datetime import datetime
from typing import Optional, List
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field, Relationship
from .base import BaseModel, TimestampMixin, TenantMixin, BranchMixin
from .enums import OrderStatus, SampleType

class LabOrder(BaseModel, TimestampMixin, TenantMixin, BranchMixin, table=True):
    """Laboratory order model"""
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
    
    # Basic relationships only
    samples: List["Sample"] = Relationship(back_populates="order")

class Sample(BaseModel, TenantMixin, BranchMixin, table=True):
    """Sample model for laboratory samples"""
    __tablename__ = "sample"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    branch_id: UUID = Field(foreign_key="branch.id")
    order_id: UUID = Field(foreign_key="lab_order.id")
    sample_code: str = Field(max_length=100)  # Unique per order or branch
    type: SampleType
    state: OrderStatus = Field(default=OrderStatus.RECEIVED)
    collected_at: Optional[datetime] = Field(default=None)
    received_at: Optional[datetime] = Field(default=None)
    notes: Optional[str] = Field(default=None)
    
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
