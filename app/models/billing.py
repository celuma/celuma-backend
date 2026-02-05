from datetime import datetime
from typing import Optional, List
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Numeric, UniqueConstraint
from .base import BaseModel, TimestampMixin, TenantMixin, BranchMixin
from .enums import PaymentStatus


class InvoiceItem(BaseModel, TimestampMixin, TenantMixin, table=True):
    """Invoice item for detailed billing"""
    __tablename__ = "invoice_item"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    invoice_id: UUID = Field(foreign_key="invoice.id")
    study_type_id: Optional[UUID] = Field(foreign_key="study_type.id", default=None)
    description: str = Field(max_length=500)
    quantity: int = Field(default=1)
    unit_price: float = Field(sa_type=Numeric(12, 2))
    subtotal: float = Field(sa_type=Numeric(12, 2))
    
    # Basic relationships only
    invoice: "Invoice" = Relationship(back_populates="items")


class Invoice(BaseModel, TimestampMixin, TenantMixin, BranchMixin, table=True):
    """Invoice model for laboratory billing"""
    __tablename__ = "invoice"
    __table_args__ = (UniqueConstraint("order_id", name="uq_invoice_order_id"),)
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    branch_id: UUID = Field(foreign_key="branch.id")
    order_id: UUID = Field(foreign_key="order.id")
    invoice_number: str = Field(max_length=100)  # Unique per branch
    subtotal: float = Field(sa_type=Numeric(12, 2), default=0)
    discount_total: float = Field(sa_type=Numeric(12, 2), default=0)
    tax_total: float = Field(sa_type=Numeric(12, 2), default=0)
    total: float = Field(sa_type=Numeric(12, 2))  # Total to pay
    amount_total: float = Field(sa_type=Numeric(12, 2))  # Kept for backwards compatibility
    amount_paid: float = Field(sa_type=Numeric(12, 2), default=0)  # Cache of sum(payments)
    currency: str = Field(default="MXN", max_length=3)
    status: PaymentStatus = Field(default=PaymentStatus.PENDING)
    issued_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = Field(default=None)
    paid_at: Optional[datetime] = Field(default=None)
    
    # Basic relationships only
    payments: List["Payment"] = Relationship(back_populates="invoice")
    items: List["InvoiceItem"] = Relationship(back_populates="invoice")


class Payment(BaseModel, TimestampMixin, TenantMixin, table=True):
    """Payment model for invoice payments"""
    __tablename__ = "payment"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    invoice_id: UUID = Field(foreign_key="invoice.id")
    amount: float = Field(sa_type=Numeric(12, 2))
    currency: str = Field(default="MXN", max_length=3)
    method: Optional[str] = Field(max_length=100, default=None)  # cash, card, transfer, other
    reference: Optional[str] = Field(max_length=255, default=None)
    received_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)
    
    # Basic relationships only
    invoice: Invoice = Relationship(back_populates="payments")
