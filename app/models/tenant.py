from datetime import datetime
from typing import Optional, List
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field, Relationship
from .base import BaseModel, TimestampMixin

class Tenant(BaseModel, TimestampMixin, table=True):
    """Tenant model for multi-tenancy"""
    __tablename__ = "tenant"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(max_length=255)
    legal_name: Optional[str] = Field(max_length=500, default=None)
    tax_id: Optional[str] = Field(max_length=50, default=None)
    logo_url: Optional[str] = Field(max_length=500, default=None)
    is_active: bool = Field(default=True)
    
    # Basic relationships only - will add more as we fix the models
    branches: List["Branch"] = Relationship(back_populates="tenant")
    users: List["AppUser"] = Relationship(back_populates="tenant")

class Branch(BaseModel, TimestampMixin, table=True):
    """Branch model for tenant locations"""
    __tablename__ = "branch"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    code: str = Field(max_length=50)  # Internal code unique per tenant
    name: str = Field(max_length=255)
    timezone: str = Field(default="America/Mexico_City", max_length=100)
    address_line1: Optional[str] = Field(max_length=255, default=None)
    address_line2: Optional[str] = Field(max_length=255, default=None)
    city: Optional[str] = Field(max_length=100, default=None)
    state: Optional[str] = Field(max_length=100, default=None)
    postal_code: Optional[str] = Field(max_length=20, default=None)
    country: str = Field(default="MX", max_length=2)
    is_active: bool = Field(default=True)
    
    # Basic relationships only
    tenant: Tenant = Relationship(back_populates="branches")
    users: List["UserBranch"] = Relationship(back_populates="branch")
