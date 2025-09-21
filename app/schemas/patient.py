from pydantic import BaseModel
from typing import Optional
from datetime import date

class PatientCreate(BaseModel):
    """Schema for creating a patient"""
    tenant_id: str
    branch_id: str
    patient_code: str
    first_name: str
    last_name: str
    dob: Optional[date] = None
    sex: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None

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
