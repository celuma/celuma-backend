from pydantic import BaseModel, field_validator
from typing import Optional, List
from datetime import datetime


class ReportSectionCreate(BaseModel):
    """Schema for creating a report section"""
    section: str
    description: Optional[str] = None
    predefined_text: Optional[str] = None
    
    @field_validator('section')
    @classmethod
    def validate_section(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError('Section name cannot be empty')
        if len(v) > 255:
            raise ValueError('Section name cannot exceed 255 characters')
        return v
    
    @field_validator('description')
    @classmethod
    def validate_description(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v = v.strip()
            if not v:
                return None
        return v
    
    @field_validator('predefined_text')
    @classmethod
    def validate_predefined_text(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v = v.strip()
            if not v:
                return None
        return v


class ReportSectionUpdate(BaseModel):
    """Schema for updating a report section"""
    section: Optional[str] = None
    description: Optional[str] = None
    predefined_text: Optional[str] = None
    
    @field_validator('section')
    @classmethod
    def validate_section(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError('Section name cannot be empty')
        if len(v) > 255:
            raise ValueError('Section name cannot exceed 255 characters')
        return v
    
    @field_validator('description')
    @classmethod
    def validate_description(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v = v.strip()
            if not v:
                return None
        return v
    
    @field_validator('predefined_text')
    @classmethod
    def validate_predefined_text(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v = v.strip()
            if not v:
                return None
        return v


class ReportSectionResponse(BaseModel):
    """Schema for report section response"""
    id: str
    tenant_id: str
    section: str
    description: Optional[str] = None
    predefined_text: Optional[str] = None
    created_at: datetime
    created_by: Optional[str] = None


class ReportSectionDetailResponse(BaseModel):
    """Schema for detailed report section response"""
    id: str
    tenant_id: str
    section: str
    description: Optional[str] = None
    predefined_text: Optional[str] = None
    created_at: datetime
    created_by: Optional[str] = None


class ReportSectionsListResponse(BaseModel):
    """Response schema for report sections list"""
    report_sections: List[ReportSectionResponse]
