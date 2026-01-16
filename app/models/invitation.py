from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field, Relationship
from .base import BaseModel, TimestampMixin, TenantMixin
from .enums import UserRole

class UserInvitation(BaseModel, TimestampMixin, TenantMixin, table=True):
    """User invitation model for onboarding"""
    __tablename__ = "user_invitation"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    email: str = Field(max_length=255, index=True)
    full_name: str = Field(max_length=255)
    role: UserRole
    token: str = Field(max_length=255, unique=True, index=True)
    expires_at: datetime
    accepted_at: Optional[datetime] = Field(default=None)
    is_used: bool = Field(default=False)
    invited_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    
    # No relationships for now

class PasswordResetToken(BaseModel, TimestampMixin, table=True):
    """Password reset token model"""
    __tablename__ = "password_reset_token"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="app_user.id")
    token: str = Field(max_length=255, unique=True, index=True)
    expires_at: datetime
    is_used: bool = Field(default=False)
    used_at: Optional[datetime] = Field(default=None)
    ip_address: Optional[str] = Field(max_length=45, default=None)
    
    # No relationships for now

