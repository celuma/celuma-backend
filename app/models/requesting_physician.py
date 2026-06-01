from typing import Optional
from uuid import UUID, uuid4
from sqlmodel import Field
from .base import BaseModel, TimestampMixin, TenantMixin, BranchMixin


class RequestingPhysician(BaseModel, TimestampMixin, TenantMixin, BranchMixin, table=True):
    """Requesting physician catalog for external laboratory requesters."""
    __tablename__ = "requesting_physician"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    branch_id: UUID = Field(foreign_key="branch.id", index=True)
    physician_code: str = Field(max_length=100, index=True)
    first_name: str = Field(max_length=255)
    last_name: str = Field(max_length=255)
    full_name: Optional[str] = Field(max_length=255, default=None)
    specialty: Optional[str] = Field(max_length=255, default=None)
    professional_license: Optional[str] = Field(max_length=100, default=None)
    institution: Optional[str] = Field(max_length=255, default=None)
    phone: Optional[str] = Field(max_length=20, default=None)
    email: Optional[str] = Field(max_length=255, default=None, index=True)
    address: Optional[str] = Field(max_length=500, default=None)
    is_active: bool = Field(default=True, index=True)
