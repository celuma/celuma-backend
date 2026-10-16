"""Monthly numbering state (Céluma 1.4, issue #8).

Contract: `celuma-engineering/specs/laboratory/monthly-numbering.md`;
decision record: ADR 0013. Three tables, all created EMPTY by `v1_4_0` §3:

- `numbering_config`: per tenant and entity kind; **no row means not activated**.
- `numbering_counter`: the monotonic monthly counter of one scope.
- `numbering_allocation`: the permanent ledger of every generated code. It has
  NO foreign key to orders, samples or catalog rows, on purpose: deleting any
  of them must never release a number.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column, Index, UniqueConstraint
from sqlmodel import Field

from .base import BaseModel


class NumberingConfig(BaseModel, table=True):
    __tablename__ = "numbering_config"

    tenant_id: UUID = Field(foreign_key="tenant.id", primary_key=True)
    entity_kind: str = Field(max_length=16, primary_key=True)
    #: Deliberate per-tenant, per-kind activation. No row / false = the compatibility behaviour.
    active: bool = Field(default=False)
    #: 'allowed' | 'forbidden' | 'required' (applies while active).
    manual_policy: str = Field(default="allowed", max_length=12)
    format: str = Field(max_length=60)
    sequence_min_width: int = Field(default=2)
    #: First value of a new counter; raising it also jumps existing counters forward, never back.
    start_value: int = Field(default=1)
    #: 'tenant' | 'branch' (branch needs the {BRANCH} token).
    scope: str = Field(default="tenant", max_length=8)
    #: IANA zone defining the month of tenant-wide counters (branch-scoped ones use the branch's zone).
    timezone: str = Field(default="America/Mexico_City", max_length=100)
    activated_at: Optional[datetime] = Field(default=None)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    updated_by: Optional[UUID] = Field(default=None, foreign_key="app_user.id")


class NumberingCounter(BaseModel, table=True):
    __tablename__ = "numbering_counter"

    tenant_id: UUID = Field(foreign_key="tenant.id", primary_key=True)
    entity_kind: str = Field(max_length=16, primary_key=True)
    #: `study_type.id` (orders) or `sample_type.id` (samples). Deliberately no
    #: foreign key: the counter must outlive the catalog row.
    scope_id: UUID = Field(primary_key=True)
    #: Branch of a branch-scoped counter; the nil UUID for tenant-wide (and compatibility) counters.
    branch_id: UUID = Field(primary_key=True)
    period_year: int = Field(primary_key=True)
    period_month: int = Field(primary_key=True)
    last_value: int = Field(default=0)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class NumberingAllocation(BaseModel, table=True):
    __tablename__ = "numbering_allocation"
    __table_args__ = (
        UniqueConstraint("tenant_id", "entity_kind", "code", name="uq_numbering_allocation_code"),
        UniqueConstraint(
            "tenant_id", "entity_kind", "scope_id", "branch_id", "period_year", "period_month", "sequence_value",
            name="uq_numbering_allocation_value",
        ),
        Index("ix_numbering_allocation_entity", "entity_id"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    entity_kind: str = Field(max_length=16)
    scope_id: UUID
    branch_id: UUID
    period_year: int
    period_month: int
    sequence_value: int
    code: str = Field(max_length=100)
    #: 'monthly' | 'legacy' (the `<code>-<n>` compatibility allocator of tenants not yet activated).
    scheme: str = Field(default="monthly", max_length=8)
    format: str = Field(max_length=60)
    sequence_min_width: int
    #: The order/sample that carries the code (no FK; null only transiently).
    entity_id: Optional[UUID] = Field(default=None)
    created_by: Optional[UUID] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class IdempotencyReceipt(BaseModel, table=True):
    """Durable receipt of one committed creation request (issue #8).

    Written in the SAME transaction as the order/sample/allocation it
    describes, so a receipt exists if and only if its work committed. Keyed by
    tenant + actor + operation + client key. Holds a SHA-256 fingerprint of the
    canonical request and the ids and codes it produced — no names, no tokens,
    never the raw request or the response body (a replay rebuilds the response
    from the records, so it shows their current state). No foreign
    keys to resources or users: a receipt outlives them.
    """
    __tablename__ = "idempotency_receipt"
    __table_args__ = (
        UniqueConstraint("tenant_id", "actor_user_id", "operation", "idempotency_key", name="uq_idempotency_receipt_key"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    actor_user_id: UUID
    operation: str = Field(max_length=32)
    idempotency_key: str = Field(max_length=128)
    request_fingerprint: str = Field(max_length=64)
    response_status: int = Field(default=200)
    #: Codes only (`{"order": {id, code}, "samples": [{id, code}]}`): enough to audit what a key produced.
    result: dict = Field(sa_column=Column(JSON, nullable=False))
    #: `[{"kind": "order"|"sample", "id": "..."}]`: what a replay verifies still exists.
    resources: list = Field(sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=datetime.utcnow)
