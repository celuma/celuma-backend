from pydantic import BaseModel, EmailStr
from typing import Optional, List

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
    tenant_id: Optional[str] = None

class UserResponse(BaseModel):
    """Schema for user response"""
    id: str
    email: str
    username: Optional[str] = None
    full_name: str
    role: str
    branch_ids: List[str] = []

class LoginResponse(BaseModel):
    """Schema for login response"""
    access_token: str
    token_type: str = "bearer"
    tenant_id: str

class TenantOption(BaseModel):
    """Schema representing a tenant choice for login"""
    tenant_id: str
    tenant_name: str

class LoginTenantSelectionResponse(BaseModel):
    """Schema for login response when multiple tenants are available"""
    need_tenant_selection: bool = True
    options: List[TenantOption]

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
    branch_ids: List[str] = []
    avatar_url: Optional[str] = None

class UserProfileUpdate(BaseModel):
    """Schema for updating user profile and password"""
    full_name: Optional[str] = None
    username: Optional[str] = None
    email: Optional[EmailStr] = None
    current_password: Optional[str] = None
    new_password: Optional[str] = None


class RegistrationTenant(BaseModel):
    """Payload section for creating the tenant during unified registration"""
    name: str
    legal_name: Optional[str] = None
    tax_id: Optional[str] = None


class RegistrationBranch(BaseModel):
    """Payload section for creating the default branch during unified registration"""
    code: str
    name: str
    timezone: str = "America/Mexico_City"
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: str = "MX"


class RegistrationAdminUser(BaseModel):
    """Payload section for creating the initial admin user during unified registration"""
    email: EmailStr
    username: Optional[str] = None
    password: str
    full_name: str
    # Role will always be admin for initial registration


class RegistrationRequest(BaseModel):
    """Unified registration request to create tenant, branch and admin user in one shot"""
    tenant: RegistrationTenant
    branch: RegistrationBranch
    admin_user: RegistrationAdminUser


class RegistrationResponse(BaseModel):
    """Unified registration response with created entities identifiers"""
    tenant_id: str
    branch_id: str
    user_id: str
