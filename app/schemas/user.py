from pydantic import BaseModel, field_validator, model_validator
from typing import Optional, List
from datetime import datetime
import re

class UserCreateByAdmin(BaseModel):
    """Schema for admin creating a user"""
    email: str
    username: Optional[str] = None
    first_name: str
    last_name: str
    # full_name is derived from first_name + last_name; kept for internal use after validation
    full_name: Optional[str] = None
    role: str
    password: str
    branch_ids: Optional[List[str]] = []

    @model_validator(mode="after")
    def build_full_name(self) -> "UserCreateByAdmin":
        self.full_name = f"{self.first_name} {self.last_name}".strip()
        return self

    @field_validator('username')
    @classmethod
    def validate_username(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v != "":
            # Only allow alphanumeric characters and underscores
            if not re.match(r'^[a-zA-Z0-9_]+$', v):
                raise ValueError('Username must contain only alphanumeric characters and underscores')
            if len(v) < 3:
                raise ValueError('Username must be at least 3 characters long')
            if len(v) > 30:
                raise ValueError('Username must be at most 30 characters long')
        return v

class UserUpdateByAdmin(BaseModel):
    """Schema for admin updating a user"""
    email: Optional[str] = None
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    # full_name still accepted for backward compatibility; first/last take precedence.
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None
    branch_ids: Optional[List[str]] = None

    @field_validator('username')
    @classmethod
    def validate_username(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v != "":
            # Only allow alphanumeric characters and underscores
            if not re.match(r'^[a-zA-Z0-9_]+$', v):
                raise ValueError('Username must contain only alphanumeric characters and underscores')
            if len(v) < 3:
                raise ValueError('Username must be at least 3 characters long')
            if len(v) > 30:
                raise ValueError('Username must be at most 30 characters long')
        return v

class UserDetailResponse(BaseModel):
    """Schema for user detail response"""
    id: str
    tenant_id: str
    email: str
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: str
    roles: List[str] = []
    is_active: bool
    created_at: datetime
    branch_ids: List[str] = []
    avatar_url: Optional[str] = None

class UsersListResponse(BaseModel):
    """Schema for users list response"""
    users: List[UserDetailResponse]


class SignatureResponse(BaseModel):
    """Response for current user's digital signature."""
    url: str
    has_signature: bool


class ReviewerItem(BaseModel):
    """User entry tailored for the reviewer selector / management screens."""
    id: str
    full_name: str
    email: str
    has_signature: bool
    avatar_url: Optional[str] = None


class ReviewersListResponse(BaseModel):
    """Response schema for the list of users with the reviewer role."""
    reviewers: List[ReviewerItem]
