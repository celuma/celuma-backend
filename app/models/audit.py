from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import JSON
from .base import BaseModel, TimestampMixin, TenantMixin

class AuditLog(BaseModel, TimestampMixin, TenantMixin, table=True):
    """Audit log model for tracking system changes"""
    __tablename__ = "audit_log"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    branch_id: Optional[UUID] = Field(foreign_key="branch.id", default=None)  # Optional if applicable
    actor_user_id: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    action: str = Field(max_length=255)  # ORDER.UPDATE_STATUS, etc.
    entity_type: str = Field(max_length=100)  # lab_order, report, sample, etc.
    entity_id: UUID
    old_values: Optional[dict] = Field(default=None, sa_type=JSON)  # JSONB
    new_values: Optional[dict] = Field(default=None, sa_type=JSON)  # JSONB
    ip_address: Optional[str] = Field(max_length=45, default=None)  # INET compatible
    
    # No relationships for now - will add back as we fix the models
