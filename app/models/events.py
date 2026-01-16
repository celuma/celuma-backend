from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import JSON
from .base import BaseModel, TimestampMixin, TenantMixin, BranchMixin
from .enums import EventType

class CaseEvent(BaseModel, TimestampMixin, TenantMixin, BranchMixin, table=True):
    """Case event model for timeline tracking"""
    __tablename__ = "case_event"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    branch_id: UUID = Field(foreign_key="branch.id")
    order_id: UUID = Field(foreign_key="lab_order.id")
    event_type: EventType
    description: str = Field(max_length=500)
    event_metadata: Optional[dict] = Field(default=None, sa_type=JSON)  # Additional event data
    created_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    
    # No relationships for now - will add as needed

