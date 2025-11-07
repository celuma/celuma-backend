from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

class UserCreateByAdmin(BaseModel):
    """Schema for admin creating a user"""
    email: str
    username: Optional[str] = None
    full_name: str
    role: str
    password: str

class UserUpdateByAdmin(BaseModel):
    """Schema for admin updating a user"""
    email: Optional[str] = None
    username: Optional[str] = None
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None

class UserDetailResponse(BaseModel):
    """Schema for user detail response"""
    id: str
    tenant_id: str
    email: str
    username: Optional[str] = None
    full_name: str
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

class UsersListResponse(BaseModel):
    """Schema for users list response"""
    users: List[UserDetailResponse]
