from datetime import datetime
from typing import Optional
from pydantic import BaseModel, field_validator


class RequestingPhysicianCreate(BaseModel):
    """Schema for creating a requesting physician."""
    tenant_id: Optional[str] = None
    branch_id: str
    physician_code: Optional[str] = None
    first_name: str
    last_name: str
    specialty: Optional[str] = None
    professional_license: Optional[str] = None
    institution: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    is_active: Optional[bool] = True

    @field_validator("physician_code")
    @classmethod
    def validate_physician_code(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Physician code cannot be empty")
        if len(value) > 100:
            raise ValueError("Physician code cannot exceed 100 characters")
        return value.upper()

    @field_validator("first_name", "last_name")
    @classmethod
    def validate_required_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name fields cannot be empty")
        if len(value) > 255:
            raise ValueError("Name fields cannot exceed 255 characters")
        return value


class RequestingPhysicianUpdate(BaseModel):
    """Schema for updating a requesting physician."""
    branch_id: Optional[str] = None
    physician_code: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    specialty: Optional[str] = None
    professional_license: Optional[str] = None
    institution: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("physician_code")
    @classmethod
    def validate_physician_code(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Physician code cannot be empty")
        if len(value) > 100:
            raise ValueError("Physician code cannot exceed 100 characters")
        return value.upper()

    @field_validator("first_name", "last_name")
    @classmethod
    def validate_required_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Name fields cannot be empty")
        if len(value) > 255:
            raise ValueError("Name fields cannot exceed 255 characters")
        return value


class RequestingPhysicianResponse(BaseModel):
    """Schema for requesting physician response."""
    id: str
    tenant_id: str
    branch_id: str
    physician_code: str
    first_name: str
    last_name: str
    full_name: str
    specialty: Optional[str] = None
    professional_license: Optional[str] = None
    institution: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    is_active: bool
    created_at: datetime


class RequestingPhysicianDetailResponse(RequestingPhysicianResponse):
    """Schema for detailed requesting physician response."""


class RequestingPhysicianRef(BaseModel):
    """Minimal requesting physician reference."""
    id: str
    full_name: str
    physician_code: str
    specialty: Optional[str] = None
    institution: Optional[str] = None
    email: Optional[str] = None
