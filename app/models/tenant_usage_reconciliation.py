"""TenantUsageReconciliation — append-only reconciliation-run history
(Céluma 1.3, Phase 4, Block B).

One row per reconciliation run, never updated after it reaches a terminal
status (mirrors `AuditLog`/`OrderEvent`'s own append-only history
convention — see docs/celuma-1.3/phase-3-block-b/notification-core-domain-
model.md §6 for the equivalent no-hard-delete precedent in the notification
domain). Block B creates this table and its lifecycle contract only; no
service in this block ever writes a row. The reconciliation engine itself
(S3 listing, comparison, repair) is Block D's.

Lifecycle
----------
    RUNNING     -- started_at set, completed_at NULL
    SUCCEEDED   -- completed_at set, error_code normally NULL
    FAILED      -- completed_at set, error_code may be populated

A row is inserted as RUNNING and later updated in place to its terminal
status exactly once (Block D's contract, not enforced here structurally,
but the CHECK constraints below make the invalid combinations
unrepresentable: RUNNING must have a NULL completed_at, and a terminal
status must have a non-NULL completed_at).

`error_code` is a sanitized, stable code — never a raw exception message,
S3 key, AWS credential, or patient-identifying detail. Same convention as
`notification_delivery.error_code` (content policy already established in
Phase 3, Block B).

`expected_storage_bytes` / `actual_storage_bytes` / `difference_bytes`
-------------------------------------------------------------------------
`expected_storage_bytes` is what `TenantUsage.billable_storage_bytes` said
at run start. `actual_storage_bytes` is the billable total independently
recomputed by the reconciliation run (NOT "every physical byte in S3" —
Block A proved those two numbers differ by design, e.g. retained-after-
delete signature PNGs). `difference_bytes` is defined as
`actual_storage_bytes - expected_storage_bytes`: positive means the
counter under-counted, negative means it over-counted, zero means the two
agree. This is a fixed convention, not a per-run choice.

Counters (`objects_checked`, `orphans_found`, `missing_objects_found`,
`metadata_mismatches_found`) are NULL while a run is RUNNING and populated
once it reaches SUCCEEDED. `repaired` records whether this run corrected
`TenantUsage`.

One RUNNING row per tenant (Céluma 1.3, Phase 4, Block D)
----------------------------------------------------------
`ix_tenant_usage_reconciliation_one_running` — a partial unique index on
`(tenant_id) WHERE status = 'RUNNING'` — makes two concurrent runs for the
same tenant unrepresentable. A second attempt fails on the constraint and
is surfaced as `ConcurrentReconciliationError`, not as a duplicate history
row. A run abandoned by a process that died mid-flight is recovered by
`recover_stale_runs()` (FAILED, `error_code = "stale_run_recovered"`),
which is what stops that index from blocking a tenant forever.
"""
from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, Column, DateTime, String
from sqlmodel import Field

from .base import BaseModel


class TenantUsageReconciliationStatus(str, Enum):
    """Persisted as VARCHAR + CHECK, not a native Postgres ENUM — same
    convention every other lifecycle status in this codebase follows (see
    the notification domain's enums), so a future status change is a
    constraint edit, not `ALTER TYPE`.
    """
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class TenantUsageReconciliation(BaseModel, table=True):
    """One row per reconciliation run. Append-only operational history —
    never deleted, and updated at most once (RUNNING -> a terminal status).
    """
    __tablename__ = "tenant_usage_reconciliation"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    status: TenantUsageReconciliationStatus = Field(
        default=TenantUsageReconciliationStatus.RUNNING,
        sa_column=Column(
            "status", String(20), nullable=False, server_default="RUNNING"
        ),
    )
    started_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("started_at", DateTime, nullable=False),
    )
    completed_at: Optional[datetime] = Field(default=None)

    expected_storage_bytes: Optional[int] = Field(
        default=None,
        sa_column=Column("expected_storage_bytes", BigInteger, nullable=True),
    )
    actual_storage_bytes: Optional[int] = Field(
        default=None,
        sa_column=Column("actual_storage_bytes", BigInteger, nullable=True),
    )
    difference_bytes: Optional[int] = Field(
        default=None,
        sa_column=Column("difference_bytes", BigInteger, nullable=True),
    )

    objects_checked: Optional[int] = Field(
        default=None,
        sa_column=Column("objects_checked", BigInteger, nullable=True),
    )
    orphans_found: Optional[int] = Field(
        default=None,
        sa_column=Column("orphans_found", BigInteger, nullable=True),
    )
    missing_objects_found: Optional[int] = Field(
        default=None,
        sa_column=Column("missing_objects_found", BigInteger, nullable=True),
    )
    # Céluma 1.3 Phase 4, Block D: a *distinct* integrity class from
    # `missing_objects_found`, never an overload of it. A metadata mismatch
    # (the S3 object's size or ETag disagrees with the StorageObject row)
    # means the bytes are still there and the row describing them is stale;
    # a missing object may mean clinical data loss. Collapsing the two would
    # make the second indistinguishable from the first in every report.
    # Block D detects and reports both, and repairs neither — see
    # s3-integrity-reconciliation-contract.md.
    metadata_mismatches_found: Optional[int] = Field(
        default=None,
        sa_column=Column("metadata_mismatches_found", BigInteger, nullable=True),
    )

    repaired: Optional[bool] = Field(
        default=None,
        sa_column=Column("repaired", Boolean, nullable=True),
    )
    # Sanitized code only — never a raw exception/provider message (same
    # policy as notification_delivery.error_code).
    error_code: Optional[str] = Field(max_length=255, default=None)
