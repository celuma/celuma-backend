from typing import TYPE_CHECKING
from uuid import UUID, uuid4
from datetime import datetime
from sqlmodel import Field, Relationship, UniqueConstraint
from .base import BaseModel, TimestampMixin

if TYPE_CHECKING:
    from .user import AppUser
    from .role import Role


class UserRoleLink(BaseModel, TimestampMixin, table=True):
    """Join table: assigns roles to users. A user may hold multiple roles."""
    __tablename__ = "user_role"
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_user_role"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="app_user.id", index=True)
    role_id: UUID = Field(foreign_key="role.id", index=True)

    user: "AppUser" = Relationship(back_populates="user_roles")
    role: "Role" = Relationship(back_populates="user_roles")
