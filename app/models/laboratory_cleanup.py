"""Laboratory-content cleanup state (Céluma 1.4, issue #39).

Contract: `celuma-engineering/specs/laboratory/laboratory-content-cleanup.md`;
decision record: ADR 0021. Three tables, all created EMPTY by `v1_4_0` §7:

- `laboratory_cleanup_operation`: one row per confirmed cleanup — the durable
  state machine (status, phase, lease), the approved preview, the options and the
  safe counts. It references only the tenant: it is independent evidence that
  outlives the business graph it removed.
- `laboratory_cleanup_object`: the object-store deletion outbox. Written in the
  SAME transaction that deletes the database graph, so a rollback never leaves
  objects removed and a crash after the commit never forgets which objects are
  still to be deleted. It copies bucket/key because the `storage_object` row is
  gone by then.
- `laboratory_cleanup_retired_code`: every order/sample code the cleanup removed,
  consulted by numbering availability so even a manual/legacy code the ledger
  never saw is never issued again.

No foreign key points at an order, sample, report or storage object.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column, Index, UniqueConstraint, text
from sqlmodel import Field

from .base import BaseModel

#: Operation statuses. QUEUED/RUNNING/FAILED are "open": at most one per tenant.
STATUS_QUEUED = "QUEUED"
STATUS_RUNNING = "RUNNING"
STATUS_FAILED = "FAILED"
STATUS_COMPLETED = "COMPLETED"
STATUS_CANCELLED = "CANCELLED"
OPEN_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_FAILED)
TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_CANCELLED)

PHASE_QUIESCE = "QUIESCE"
PHASE_DATABASE = "DATABASE"
PHASE_OBJECTS = "OBJECTS"
PHASE_DONE = "DONE"

OBJECT_PENDING = "PENDING"
OBJECT_DELETED = "DELETED"
OBJECT_ABSENT = "ABSENT"  # already gone when we tried: counts as done
OBJECT_FAILED = "FAILED"
OBJECT_KEPT = "KEPT"  # a late reference appeared: the object stays, the outbox row is closed
OBJECT_DONE_STATUSES = (OBJECT_DELETED, OBJECT_ABSENT, OBJECT_KEPT)


class LaboratoryCleanupOperation(BaseModel, table=True):
    __tablename__ = "laboratory_cleanup_operation"
    __table_args__ = (
        UniqueConstraint("confirmation_jti", name="uq_laboratory_cleanup_confirmation_jti"),
        # At most one open operation per tenant; also what rejects a duplicate concurrent start.
        Index(
            "uq_laboratory_cleanup_open_per_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED','RUNNING','FAILED')"),
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    actor_user_id: UUID
    status: str = Field(default=STATUS_QUEUED, max_length=16)
    phase: str = Field(default=PHASE_QUIESCE, max_length=16)
    reason: str
    laboratory_name: str = Field(max_length=255)
    scope_revision: str = Field(max_length=64)
    confirmation_jti: str = Field(max_length=64)
    confirmation_fingerprint: str = Field(max_length=64)
    #: The preview's counts the operator approved (safe numbers only).
    approved_counts: dict = Field(sa_column=Column(JSON, nullable=False))
    #: What the database phase actually removed/retained; NULL until it commits.
    executed_counts: Optional[dict] = Field(default=None, sa_column=Column(JSON, nullable=True))
    error_code: Optional[str] = Field(default=None, max_length=64)
    error_message: Optional[str] = Field(default=None, max_length=500)
    attempts: int = Field(default=0)
    lease_owner: Optional[str] = Field(default=None, max_length=64)
    lease_expires_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = Field(default=None)
    database_cleaned_at: Optional[datetime] = Field(default=None)
    completed_at: Optional[datetime] = Field(default=None)
    cancelled_at: Optional[datetime] = Field(default=None)


class LaboratoryCleanupObject(BaseModel, table=True):
    __tablename__ = "laboratory_cleanup_object"
    __table_args__ = (
        UniqueConstraint("operation_id", "bucket", "object_key", name="uq_laboratory_cleanup_object_key"),
        Index("ix_laboratory_cleanup_object_tenant_status", "tenant_id", "status"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    operation_id: UUID = Field(foreign_key="laboratory_cleanup_operation.id", index=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    #: Copied before the `storage_object` row is deleted; deliberately not a foreign key.
    storage_object_id: UUID
    provider: str = Field(max_length=50)
    bucket: str = Field(max_length=255)
    object_key: str = Field(max_length=1000)
    size_bytes: Optional[int] = Field(default=None)
    category: str = Field(max_length=32)
    status: str = Field(default=OBJECT_PENDING, max_length=10)
    attempts: int = Field(default=0)
    error_code: Optional[str] = Field(default=None, max_length=64)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    deleted_at: Optional[datetime] = Field(default=None)


class LaboratoryCleanupRetiredCode(BaseModel, table=True):
    __tablename__ = "laboratory_cleanup_retired_code"
    __table_args__ = (
        UniqueConstraint("tenant_id", "entity_kind", "code", name="uq_laboratory_cleanup_retired_code"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    entity_kind: str = Field(max_length=16)  # 'order' | 'sample', as numbering_allocation.entity_kind
    code: str = Field(max_length=100)
    operation_id: UUID = Field(foreign_key="laboratory_cleanup_operation.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
