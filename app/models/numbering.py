"""Monthly numbering state (Céluma 1.4, issue #8).

Contract: `celuma-engineering/specs/laboratory/monthly-numbering.md`;
decision record: ADR 0013. Three tables, all created EMPTY by `v1_4_0` §3:

- `numbering_config`: per tenant and entity kind; **no row means defaults**.
- `numbering_counter`: the monotonic monthly counter of one scope.
- `numbering_allocation`: the permanent ledger of every generated code. It has
  NO foreign key to orders, samples or catalog rows, on purpose: deleting any
  of them must never release a number.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field

from .base import BaseModel


class NumberingConfig(BaseModel, table=True):
    __tablename__ = "numbering_config"

    tenant_id: UUID = Field(foreign_key="tenant.id", primary_key=True)
    entity_kind: str = Field(max_length=16, primary_key=True)
    mode: str = Field(default="automatic", max_length=24)
    format: str = Field(max_length=60)
    sequence_min_width: int = Field(default=2)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    updated_by: Optional[UUID] = Field(default=None, foreign_key="app_user.id")


class NumberingCounter(BaseModel, table=True):
    __tablename__ = "numbering_counter"

    tenant_id: UUID = Field(foreign_key="tenant.id", primary_key=True)
    entity_kind: str = Field(max_length=16, primary_key=True)
    #: `study_type.id` (orders) or `sample_type.id` (samples). Deliberately no
    #: foreign key: the counter must outlive the catalog row.
    scope_id: UUID = Field(primary_key=True)
    period_year: int = Field(primary_key=True)
    period_month: int = Field(primary_key=True)
    last_value: int = Field(default=0)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class NumberingAllocation(BaseModel, table=True):
    __tablename__ = "numbering_allocation"
    __table_args__ = (
        UniqueConstraint("tenant_id", "entity_kind", "code", name="uq_numbering_allocation_code"),
        UniqueConstraint(
            "tenant_id", "entity_kind", "scope_id", "period_year", "period_month", "sequence_value",
            name="uq_numbering_allocation_value",
        ),
        Index("ix_numbering_allocation_entity", "entity_id"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    entity_kind: str = Field(max_length=16)
    scope_id: UUID
    period_year: int
    period_month: int
    sequence_value: int
    code: str = Field(max_length=100)
    format: str = Field(max_length=60)
    sequence_min_width: int
    #: The order/sample that carries the code (no FK; null only transiently).
    entity_id: Optional[UUID] = Field(default=None)
    created_by: Optional[UUID] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
