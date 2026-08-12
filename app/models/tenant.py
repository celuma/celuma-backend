from datetime import datetime
from typing import Optional, List
from uuid import UUID, uuid4
from sqlalchemy import Column, ForeignKey
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import SQLModel, Field, Relationship
from .base import BaseModel, TimestampMixin

class Tenant(BaseModel, TimestampMixin, table=True):
    """Tenant model for multi-tenancy"""
    __tablename__ = "tenant"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(max_length=255)
    legal_name: Optional[str] = Field(max_length=500, default=None)
    tax_id: Optional[str] = Field(max_length=50, default=None)
    # Presentation/backward-compatibility value only, since Céluma 1.3
    # Phase 4, Block D. It is what API responses and rendered reports show;
    # it is NOT how Céluma decides which StorageObject is the current logo
    # (see `logo_storage_id`). Kept because existing clients read it — see
    # docs/celuma-1.3/phase-4-block-d/tenant-logo-db-scope-contract.md.
    logo_url: Optional[str] = Field(max_length=500, default=None)
    # Céluma 1.3 Phase 4, Block D: the canonical current-logo relationship.
    # Every runtime answer to "which StorageObject is this tenant's logo?"
    # — billing, usage accounting, reconciliation, replacement — comes from
    # this FK. Nothing parses `logo_url` any more, so a MEDIA_PUBLIC_BASE_URL
    # change can no longer detach a tenant from its own logo (the schema gap
    # Block C recorded as debt in block-d-dependencies.md §6).
    #
    # NULL means "Céluma does not know which object is current": either the
    # tenant has no logo, or it has a legacy `logo_url` the Block D backfill
    # could not resolve unambiguously — reported by reconciliation as
    # `legacy_logo_reference_unresolved`, never guessed at.
    #
    # `use_alter` because tenant and storage_object reference each other
    # (storage_object.tenant_id -> tenant.id): the FK is emitted separately
    # so the cycle never has to be ordered. No cascade — deleting a
    # StorageObject must not silently rewrite tenant identity.
    logo_storage_id: Optional[UUID] = Field(
        default=None,
        sa_column=Column(
            "logo_storage_id",
            PGUUID(as_uuid=True),
            ForeignKey(
                "storage_object.id",
                name="fk_tenant_logo_storage_id_storage_object",
                use_alter=True,
            ),
            nullable=True,
        ),
    )
    is_active: bool = Field(default=True)
    # Céluma 1.3 Phase 2, Block A: gates creation of new V2 reports only.
    # Does not affect rendering of existing reports (schema_version-based,
    # see report-schema-versioning.md) and is not read anywhere in this block.
    reports_v2_enabled: bool = Field(default=False)

    # Basic relationships only - will add more as we fix the models
    branches: List["Branch"] = Relationship(back_populates="tenant")
    users: List["AppUser"] = Relationship(back_populates="tenant")

class Branch(BaseModel, TimestampMixin, table=True):
    """Branch model for tenant locations"""
    __tablename__ = "branch"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    code: str = Field(max_length=50)  # Internal code unique per tenant
    name: str = Field(max_length=255)
    timezone: str = Field(default="America/Mexico_City", max_length=100)
    address_line1: Optional[str] = Field(max_length=255, default=None)
    address_line2: Optional[str] = Field(max_length=255, default=None)
    city: Optional[str] = Field(max_length=100, default=None)
    state: Optional[str] = Field(max_length=100, default=None)
    postal_code: Optional[str] = Field(max_length=20, default=None)
    country: str = Field(default="MX", max_length=2)
    is_active: bool = Field(default=True)
    
    # Basic relationships only
    tenant: Tenant = Relationship(back_populates="branches")
    users: List["UserBranch"] = Relationship(back_populates="branch")
