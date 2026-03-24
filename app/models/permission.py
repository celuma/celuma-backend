from typing import Optional, List, TYPE_CHECKING
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field, Relationship
from .base import BaseModel

if TYPE_CHECKING:
    from .role_permission import RolePermission


class Permission(BaseModel, table=True):
    """Atomic permission unit — unique by code, grouped by domain."""
    __tablename__ = "permission"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    code: str = Field(max_length=100, unique=True, index=True)
    domain: str = Field(max_length=50, index=True)
    display_name: str = Field(max_length=255)
    description: Optional[str] = Field(max_length=500, default=None)

    role_permissions: List["RolePermission"] = Relationship(back_populates="permission")
