from pydantic import BaseModel, EmailStr
from typing import Optional

class UserRegister(BaseModel):
    """Schema for user registration"""
    email: EmailStr
    username: Optional[str] = None
    password: str
    full_name: str
    role: str
    tenant_id: str

class UserLogin(BaseModel):
    """Schema for user login - can use either username or email"""
    username_or_email: str
    password: str
    tenant_id: str

class UserResponse(BaseModel):
    """Schema for user response"""
    id: str
    email: str
    username: Optional[str] = None
    full_name: str
    role: str

class LoginResponse(BaseModel):
    """Schema for login response"""
    access_token: str
    token_type: str = "bearer"

class LogoutResponse(BaseModel):
    """Schema for logout response"""
    message: str
    token_revoked: bool

class UserProfile(BaseModel):
    """Schema for user profile"""
    id: str
    email: str
    username: Optional[str] = None
    full_name: str
    role: str
    tenant_id: str
