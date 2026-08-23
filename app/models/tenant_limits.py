"""TenantLimits — configured commercial ceilings for a tenant (Céluma 1.3,
Phase 4, Block B).

A dedicated domain, not columns on `Tenant` (see
docs/celuma-1.3/phase-4-block-a/tenant-plan-limits-model-proposal.md §3):
keeps `Tenant` as pure identity and gives commercial configuration a home
that is trivially extensible to a future named-plan catalog (Phase 5) by
adding a `plan_id` FK here, without ever touching `Tenant` again.

Nullable-means-unlimited
--------------------------
`storage_limit_bytes` and `user_limit` are both nullable, and `NULL` means
"unlimited / not configured" — not "zero allowed". This matches the
codebase's own existing convention for "absent means legacy/unset" fields
(`report_version.schema_version`, `pdf_*`, `letterhead_version_id`; see
docs/celuma-1.3/phase-3-closure/phase-3-alembic-v1-3-contract.md §7).

When a limit IS configured, it must be strictly positive — zero or negative
values are rejected at the database level. Phase 4 does not enforce limits
at all (measurement only), so there is no product need for "0 = no storage
allowed" semantics; that would have to be a deliberate future decision, not
an accidental one this schema falls into.

Absence of a `TenantLimits` row is unambiguous, unlike `TenantUsage`: it
simply means "no limits configured for this tenant" (both unlimited). There
is no "uninitialized vs. initialized-at-unlimited" distinction to make,
because there is nothing to initialize — an absent row and an explicit
all-NULL row mean the same thing.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import BigInteger, Column, DateTime, Integer
from sqlmodel import Field

from .base import BaseModel


class TenantLimits(BaseModel, table=True):
    """A tenant's configured storage/user ceilings. No enforcement in Block
    B — this table only makes the ceilings visible for a future threshold
    calculation (Block G) to compare against `TenantUsage`/live user counts.
    """
    __tablename__ = "tenant_limits"

    tenant_id: UUID = Field(foreign_key="tenant.id", primary_key=True)
    storage_limit_bytes: Optional[int] = Field(
        default=None,
        sa_column=Column("storage_limit_bytes", BigInteger, nullable=True),
    )
    user_limit: Optional[int] = Field(
        default=None,
        sa_column=Column("user_limit", Integer, nullable=True),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("updated_at", DateTime, nullable=False),
    )
