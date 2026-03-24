from typing import Optional, List, TYPE_CHECKING
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field, Relationship
from .base import BaseModel, TimestampMixin

if TYPE_CHECKING:
    from .role_permission import RolePermission
    from .user_role import UserRoleLink


class Role(BaseModel, TimestampMixin, table=True):
    """Named collection of permissions. System roles are seeded; custom roles are tenant-scoped."""
    __tablename__ = "role"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    code: str = Field(max_length=50, unique=True, index=True)
    name: str = Field(max_length=255)
    description: Optional[str] = Field(max_length=500, default=None)
    is_system: bool = Field(default=False)
    is_protected: bool = Field(default=False)  # Cannot be deleted or modified by tenant
    # NULL means available to all tenants (system role); set for tenant-custom roles
    tenant_id: Optional[UUID] = Field(default=None, foreign_key="tenant.id", index=True)

    role_permissions: List["RolePermission"] = Relationship(back_populates="role")
    user_roles: List["UserRoleLink"] = Relationship(back_populates="role")
