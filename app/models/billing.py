from datetime import datetime
from typing import Optional, List
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Numeric
from .base import BaseModel, TimestampMixin, TenantMixin, BranchMixin
from .enums import PaymentStatus

class ServiceCatalog(BaseModel, TimestampMixin, TenantMixin, table=True):
    """Service catalog for pricing"""
    __tablename__ = "service_catalog"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    service_name: str = Field(max_length=255)
    service_code: str = Field(max_length=50)
    description: Optional[str] = Field(default=None)
    price: float = Field(sa_type=Numeric(12, 2))
    currency: str = Field(default="MXN", max_length=3)
    is_active: bool = Field(default=True)
    valid_from: Optional[datetime] = Field(default=None)
    valid_until: Optional[datetime] = Field(default=None)

class InvoiceItem(BaseModel, TimestampMixin, TenantMixin, table=True):
    """Invoice item for detailed billing"""
    __tablename__ = "invoice_item"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    invoice_id: UUID = Field(foreign_key="invoice.id")
    service_id: Optional[UUID] = Field(foreign_key="service_catalog.id", default=None)
    description: str = Field(max_length=500)
    quantity: int = Field(default=1)
    unit_price: float = Field(sa_type=Numeric(12, 2))
    subtotal: float = Field(sa_type=Numeric(12, 2))
    
    # Basic relationships only
    invoice: "Invoice" = Relationship(back_populates="items")

class Invoice(BaseModel, TimestampMixin, TenantMixin, BranchMixin, table=True):
    """Invoice model for laboratory billing"""
    __tablename__ = "invoice"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    branch_id: UUID = Field(foreign_key="branch.id")
    order_id: UUID = Field(foreign_key="order.id")
    invoice_number: str = Field(max_length=100)  # Unique per branch
    amount_total: float = Field(sa_type=Numeric(12, 2))
    currency: str = Field(default="MXN", max_length=3)
    status: PaymentStatus = Field(default=PaymentStatus.PENDING)
    issued_at: datetime = Field(default_factory=datetime.utcnow)
    
    # Basic relationships only
    payments: List["Payment"] = Relationship(back_populates="invoice")
    items: List["InvoiceItem"] = Relationship(back_populates="invoice")

class Payment(BaseModel, TimestampMixin, TenantMixin, BranchMixin, table=True):
    """Payment model for invoice payments"""
    __tablename__ = "payment"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    branch_id: UUID = Field(foreign_key="branch.id")
    invoice_id: UUID = Field(foreign_key="invoice.id")
    amount_paid: float = Field(sa_type=Numeric(12, 2))
    method: Optional[str] = Field(max_length=100, default=None)  # transfer, card, etc.
    paid_at: datetime = Field(default_factory=datetime.utcnow)
    
    # Basic relationships only
    invoice: Invoice = Relationship(back_populates="payments")
