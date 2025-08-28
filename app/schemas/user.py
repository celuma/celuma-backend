from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime

class UserRead(BaseModel):
    id: int
    email: EmailStr

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"

class LogoutRequest(BaseModel):
    """Schema for logout request"""
    pass

class LogoutResponse(BaseModel):
    """Schema for logout response"""
    message: str
    token_revoked: bool

class UserProfile(BaseModel):
    """Schema for user profile information"""
    id: str
    email: str
    username: Optional[str] = None
    full_name: str
    role: str
    tenant_id: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
