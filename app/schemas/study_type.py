from pydantic import BaseModel, field_validator
from typing import Optional, List
from datetime import datetime


class StudyTypeCreate(BaseModel):
    """Schema for creating a study type"""
    code: str
    name: str
    description: Optional[str] = None
    is_active: Optional[bool] = True
    default_report_template_id: Optional[str] = None
    
    @field_validator('code')
    @classmethod
    def validate_code(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError('Code cannot be empty')
        if len(v) > 50:
            raise ValueError('Code cannot exceed 50 characters')
        return v.upper()
    
    @field_validator('name')
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError('Name cannot be empty')
        if len(v) > 255:
            raise ValueError('Name cannot exceed 255 characters')
        return v


class StudyTypeUpdate(BaseModel):
    """Schema for updating a study type"""
    code: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    default_report_template_id: Optional[str] = None
    
    @field_validator('code')
    @classmethod
    def validate_code(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError('Code cannot be empty')
        if len(v) > 50:
            raise ValueError('Code cannot exceed 50 characters')
        return v.upper()
    
    @field_validator('name')
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError('Name cannot be empty')
        if len(v) > 255:
            raise ValueError('Name cannot exceed 255 characters')
        return v


class TemplateRef(BaseModel):
    """Minimal template reference"""
    id: str
    name: str


class StudyTypeResponse(BaseModel):
    """Schema for study type response"""
    id: str
    tenant_id: str
    code: str
    name: str
    description: Optional[str] = None
    is_active: bool
    created_at: datetime
    default_report_template_id: Optional[str] = None
    default_template: Optional[TemplateRef] = None


class StudyTypeDetailResponse(BaseModel):
    """Schema for detailed study type response"""
    id: str
    tenant_id: str
    code: str
    name: str
    description: Optional[str] = None
    is_active: bool
    created_at: datetime
    default_report_template_id: Optional[str] = None
    default_template: Optional[TemplateRef] = None


class StudyTypesListResponse(BaseModel):
    """Response schema for study types list"""
    study_types: List[StudyTypeResponse]


class StudyTypeRef(BaseModel):
    """Minimal study type reference for use in order responses"""
    id: str
    code: str
    name: str
