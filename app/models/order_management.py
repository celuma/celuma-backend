"""Order/sample management state (Céluma 1.4, issues #9, #10, #11, #16).

Contract: `celuma-engineering/specs/laboratory/order-sample-management.md`.
Both tables are created by `v1_4_0` §4.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field

from .base import BaseModel


class OrderRequestingPhysician(BaseModel, table=True):
    """One external requesting physician of an order (#16).

    A duplicate-free set per order; `position` only preserves the order the user listed them in
    (display order). It carries NO clinical meaning: there is no primary physician. Referencing a
    physician here grants that physician no access and sends nothing.
    """
    __tablename__ = "order_requesting_physician"
    __table_args__ = (UniqueConstraint("order_id", "position", name="uq_order_requesting_physician_position"),)

    order_id: UUID = Field(foreign_key="order.id", primary_key=True)
    requesting_physician_id: UUID = Field(foreign_key="requesting_physician.id", primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    position: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SampleCodeCorrection(BaseModel, table=True):
    """Audit trail AND retirement record of one sample-code correction (#10).

    `old_code` is retired for the tenant: availability checks treat it as occupied, so a corrected-away
    code can never be issued again (the numbering ledger already guarantees this for generated codes;
    this table extends the guarantee to legacy/manual codes the ledger never saw). No foreign keys to
    the sample: the record outlives it.
    """
    __tablename__ = "sample_code_correction"
    __table_args__ = (Index("ix_sample_code_correction_tenant_old_code", "tenant_id", "old_code"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    sample_id: UUID = Field(index=True)
    order_id: UUID
    old_code: str = Field(max_length=100)
    new_code: str = Field(max_length=100)
    reason: str
    corrected_by: Optional[UUID] = Field(default=None, foreign_key="app_user.id")
    corrected_at: datetime = Field(default_factory=datetime.utcnow)
