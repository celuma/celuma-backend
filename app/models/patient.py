from datetime import date, datetime
from typing import Optional, List
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field, Relationship
from .base import BaseModel, TimestampMixin, TenantMixin, BranchMixin

class Patient(BaseModel, TimestampMixin, TenantMixin, BranchMixin, table=True):
    """Patient model for laboratory patients"""
    __tablename__ = "patient"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    branch_id: UUID = Field(foreign_key="branch.id")
    patient_code: str = Field(max_length=100)  # Visible in UI, unique per tenant
    first_name: str = Field(max_length=255)
    last_name: str = Field(max_length=255)
    dob: Optional[date] = Field(default=None)
    sex: Optional[str] = Field(max_length=10, default=None)
    phone: Optional[str] = Field(max_length=20, default=None)
    email: Optional[str] = Field(max_length=255, default=None)
    
    # No relationships for now - will add back as we fix the models
