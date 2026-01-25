from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import JSON
from .base import BaseModel, TimestampMixin, TenantMixin
from .enums import EventType

class AuditLog(BaseModel, TimestampMixin, TenantMixin, table=True):
    """
    Audit log model for tracking system changes.
    
    Now includes order_event compatible fields for better integration with timeline tracking.
    This table is more strict than order_event, containing additional audit-specific fields
    like old_values, new_values, and ip_address for compliance.
    """
    __tablename__ = "audit_log"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    branch_id: Optional[UUID] = Field(foreign_key="branch.id", default=None)  # Optional if applicable
    
    # Original audit fields
    actor_user_id: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    action: str = Field(max_length=255)  # ORDER.UPDATE_STATUS, etc.
    entity_type: str = Field(max_length=100)  # lab_order, report, sample, etc.
    entity_id: UUID
    old_values: Optional[dict] = Field(default=None, sa_type=JSON)  # JSONB
    new_values: Optional[dict] = Field(default=None, sa_type=JSON)  # JSONB
    ip_address: Optional[str] = Field(max_length=45, default=None)  # INET compatible
    
    # OrderEvent compatible fields (for timeline integration)
    order_id: Optional[UUID] = Field(foreign_key="order.id", index=True, default=None)
    sample_id: Optional[UUID] = Field(foreign_key="sample.id", index=True, default=None)
    event_type: Optional[EventType] = Field(index=True, default=None)  # EventType enum
    description: Optional[str] = Field(max_length=500, default=None)
    event_metadata: Optional[dict] = Field(default=None, sa_type=JSON)
    created_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    
    # No relationships for now - will add back as we fix the models
