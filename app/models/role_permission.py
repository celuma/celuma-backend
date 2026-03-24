from typing import TYPE_CHECKING
from uuid import UUID
from sqlmodel import Field, Relationship
from .base import BaseModel

if TYPE_CHECKING:
    from .role import Role
    from .permission import Permission


class RolePermission(BaseModel, table=True):
    """Join table: maps permissions to roles."""
    __tablename__ = "role_permission"

    role_id: UUID = Field(foreign_key="role.id", primary_key=True, ondelete="CASCADE")
    permission_id: UUID = Field(foreign_key="permission.id", primary_key=True, ondelete="CASCADE")

    role: "Role" = Relationship(back_populates="role_permissions")
    permission: "Permission" = Relationship(back_populates="role_permissions")
