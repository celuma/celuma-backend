from pydantic import BaseModel, field_validator, EmailStr
from typing import Optional
from datetime import date

class PatientCreate(BaseModel):
    """Schema for creating a patient"""
    tenant_id: str
    branch_id: str
    patient_code: Optional[str] = None
    first_name: str
    last_name: str
    dob: Optional[date] = None
    sex: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None

    @field_validator("patient_code")
    @classmethod
    def validate_patient_code(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Patient code cannot be empty")
        if len(value) > 100:
            raise ValueError("Patient code cannot exceed 100 characters")
        return value.upper()


class PatientUpdate(BaseModel):
    """Schema for updating a patient"""
    branch_id: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    dob: Optional[date] = None
    sex: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None

class PatientResponse(BaseModel):
    """Schema for patient response"""
    id: str
    patient_code: str
    first_name: str
    last_name: str
    tenant_id: str
    branch_id: str

class PatientDetailResponse(BaseModel):
    """Schema for detailed patient response"""
    id: str
    patient_code: str
    first_name: str
    last_name: str
    dob: Optional[date] = None
    sex: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    tenant_id: str
    branch_id: str


class PatientFullResponse(BaseModel):
    """Schema for full patient profile used across endpoints."""
    id: str
    tenant_id: str
    branch_id: str
    patient_code: str
    first_name: str
    last_name: str
    dob: Optional[date] = None
    sex: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
