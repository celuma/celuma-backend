from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field
from pydantic import ConfigDict

class BaseModel(SQLModel):
    """Base model with common configuration"""
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        json_encoders={
            datetime: lambda v: v.isoformat(),
            UUID: lambda v: str(v)
        }
    )

class TimestampMixin(SQLModel):
    """Mixin for created_at timestamp"""
    created_at: datetime = Field(default_factory=datetime.utcnow)

class TenantMixin(SQLModel):
    """Mixin for tenant_id field"""
    tenant_id: UUID = Field(foreign_key="tenant.id")

class BranchMixin(SQLModel):
    """Mixin for branch_id field"""
    branch_id: UUID = Field(foreign_key="branch.id")
