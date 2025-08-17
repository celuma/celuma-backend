from datetime import datetime
from typing import Optional, List
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field, Relationship
from .base import BaseModel, TimestampMixin, TenantMixin
from .enums import UserRole

class AppUser(BaseModel, TimestampMixin, TenantMixin, table=True):
    """Application user model with multi-tenant support"""
    __tablename__ = "app_user"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    email: str = Field(max_length=255, index=True)
    full_name: str = Field(max_length=255)
    role: UserRole
    hashed_password: str = Field(max_length=255)
    is_active: bool = Field(default=True)
    
    # Basic relationships only
    tenant: "Tenant" = Relationship(back_populates="users")
    branches: List["UserBranch"] = Relationship(back_populates="user")

class UserBranch(BaseModel, table=True):
    """User-branch association table for multi-branch access"""
    __tablename__ = "user_branch"
    
    user_id: UUID = Field(foreign_key="app_user.id", primary_key=True)
    branch_id: UUID = Field(foreign_key="branch.id", primary_key=True)
    
    # Relationships
    user: AppUser = Relationship(back_populates="branches")
    branch: "Branch" = Relationship(back_populates="users")
