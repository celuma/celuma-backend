from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import JSON
from .base import BaseModel, TimestampMixin, TenantMixin, BranchMixin
from .enums import EventType

class OrderEvent(BaseModel, TimestampMixin, TenantMixin, BranchMixin, table=True):
    """Order event model for timeline tracking.
    
    Used for:
    - Order timeline: all events with this order_id
    - Sample timeline: filter by sample_id
    - Report timeline: filter by event_type REPORT_*
    """
    __tablename__ = "order_event"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    branch_id: UUID = Field(foreign_key="branch.id")
    order_id: UUID = Field(foreign_key="order.id")
    sample_id: Optional[UUID] = Field(foreign_key="sample.id", default=None)  # Optional: for sample-specific events
    event_type: EventType
    description: str = Field(max_length=500)
    event_metadata: Optional[dict] = Field(default=None, sa_type=JSON)  # Additional event data
    created_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    
    # No relationships for now - will add as needed

