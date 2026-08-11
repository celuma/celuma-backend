"""TenantUsage — fast-path billable storage counter (Céluma 1.3, Phase 4,
Block B).

One row per tenant, created only once usage tracking is initialized for that
tenant (Block C). This block ships the schema empty: no row exists for any
tenant until Block C's initialization/backfill story runs.

Row-absence contract
---------------------
Absence of a `TenantUsage` row for a tenant does NOT mean "0 bytes used". It
means "usage tracking not initialized for this tenant". `UsageService.
get_usage()` returns `None` in that case — never a zero-valued row, real or
synthetic. Once Block C initializes a tenant, a row exists with an explicit
`billable_storage_bytes = 0` (or whatever the backfill computes), and that
row's mere existence is what "initialized" means; no separate status column
is needed to distinguish "uninitialized" from "initialized at zero".

Field naming: `billable_storage_bytes`, not `storage_used_bytes`
------------------------------------------------------------------
Céluma 1.3, Phase 4, Block A proved the codebase's commercial storage metric
and the tenant's true physical S3 footprint are two different numbers (see
docs/celuma-1.3/phase-4-block-a/billable-storage-contract-proposal.md and
storage-drift-risk-analysis.md — e.g. sample-image deletes never remove the
S3 object, by design). `billable_storage_bytes` says on its face that this
column tracks Céluma's commercial accounting of a tenant's storage, not
"every byte physically sitting in the tenant's S3 prefix". A generic name
like `storage_used_bytes` would need a docstring to carry that distinction;
this name carries it without one. See
docs/celuma-1.3/phase-4-block-b/tenant-usage-contract.md for the full
naming review.

`last_updated` is the timestamp of the last mutation to
`billable_storage_bytes` itself — not the last reconciliation run. The two
are different events, tracked on different tables
(`TenantUsageReconciliation` owns reconciliation history).
"""
from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Column, DateTime
from sqlmodel import Field

from .base import BaseModel


class TenantUsage(BaseModel, table=True):
    """The dashboard's fast O(1) read path for a tenant's current billable
    storage usage. Maintained incrementally by Block C; read-only in Block B.
    """
    __tablename__ = "tenant_usage"

    tenant_id: UUID = Field(foreign_key="tenant.id", primary_key=True)
    billable_storage_bytes: int = Field(
        default=0,
        sa_column=Column(
            "billable_storage_bytes",
            BigInteger,
            nullable=False,
            server_default="0",
        ),
    )
    last_updated: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("last_updated", DateTime, nullable=False),
    )
