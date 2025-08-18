from pydantic import BaseModel
from typing import Optional

class TenantCreate(BaseModel):
    """Schema for creating a tenant"""
    name: str
    legal_name: Optional[str] = None
    tax_id: Optional[str] = None

class TenantResponse(BaseModel):
    """Schema for tenant response"""
    id: str
    name: str
    legal_name: Optional[str] = None

class TenantDetailResponse(BaseModel):
    """Schema for detailed tenant response"""
    id: str
    name: str
    legal_name: Optional[str] = None
    tax_id: Optional[str] = None

class BranchCreate(BaseModel):
    """Schema for creating a branch"""
    tenant_id: str
    code: str
    name: str
    timezone: str = "America/Mexico_City"
    address_line1: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: str = "MX"

class BranchResponse(BaseModel):
    """Schema for branch response"""
    id: str
    name: str
    code: str
    tenant_id: str

class BranchDetailResponse(BaseModel):
    """Schema for detailed branch response"""
    id: str
    name: str
    code: str
    city: Optional[str] = None
    state: Optional[str] = None
    country: str
    tenant_id: str
