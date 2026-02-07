from pydantic import BaseModel, field_validator
from typing import Optional, List
from datetime import datetime


class PriceCatalogCreate(BaseModel):
    """Schema for creating a price catalog entry"""
    study_type_id: str
    unit_price: float
    currency: Optional[str] = "MXN"
    is_active: Optional[bool] = True
    effective_from: Optional[datetime] = None
    effective_to: Optional[datetime] = None
    
    @field_validator('unit_price')
    @classmethod
    def validate_unit_price(cls, v: float) -> float:
        if v < 0:
            raise ValueError('Unit price cannot be negative')
        return v


class PriceCatalogUpdate(BaseModel):
    """Schema for updating a price catalog entry"""
    study_type_id: Optional[str] = None
    unit_price: Optional[float] = None
    currency: Optional[str] = None
    is_active: Optional[bool] = None
    effective_from: Optional[datetime] = None
    effective_to: Optional[datetime] = None
    
    @field_validator('unit_price')
    @classmethod
    def validate_unit_price(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v < 0:
            raise ValueError('Unit price cannot be negative')
        return v


class StudyTypeRef(BaseModel):
    """Minimal study type reference"""
    id: str
    code: str
    name: str


class PriceCatalogResponse(BaseModel):
    """Schema for price catalog response"""
    id: str
    tenant_id: str
    study_type_id: str
    unit_price: float
    currency: str
    is_active: bool
    effective_from: Optional[datetime] = None
    effective_to: Optional[datetime] = None
    created_at: datetime
    study_type: Optional[StudyTypeRef] = None


class PriceCatalogListResponse(BaseModel):
    """Response schema for price catalog list"""
    prices: List[PriceCatalogResponse]
