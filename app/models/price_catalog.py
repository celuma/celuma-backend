from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Numeric
from .base import BaseModel, TimestampMixin, TenantMixin


class PriceCatalog(BaseModel, TimestampMixin, TenantMixin, table=True):
    """Price catalog for study types"""
    __tablename__ = "price_catalog"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    study_type_id: UUID = Field(foreign_key="study_type.id", index=True)
    unit_price: float = Field(sa_type=Numeric(12, 2))
    currency: str = Field(default="MXN", max_length=3)
    is_active: bool = Field(default=True)
    effective_from: Optional[datetime] = Field(default=None)
    effective_to: Optional[datetime] = Field(default=None)
