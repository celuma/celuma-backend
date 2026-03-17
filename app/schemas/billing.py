from pydantic import BaseModel
from typing import Optional, List
from decimal import Decimal
from datetime import datetime

class InvoiceCreate(BaseModel):
    """Schema for creating an invoice"""
    tenant_id: str
    branch_id: str
    order_id: str
    invoice_number: str
    subtotal: float
    discount_total: float = 0.0
    tax_total: float = 0.0
    total: float
    currency: str = "MXN"
    issued_at: Optional[datetime] = None

class InvoiceResponse(BaseModel):
    """Schema for invoice response"""
    id: str
    invoice_number: str
    subtotal: float
    discount_total: float
    tax_total: float
    total: float
    amount_paid: float
    currency: str
    status: str
    order_id: str
    tenant_id: str
    branch_id: str
    paid_at: Optional[datetime] = None

class InvoiceDetailResponse(BaseModel):
    """Schema for detailed invoice response"""
    id: str
    invoice_number: str
    subtotal: float
    discount_total: float
    tax_total: float
    total: float
    amount_paid: float
    currency: str
    status: str
    order_id: str
    tenant_id: str
    branch_id: str
    issued_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    paid_at: Optional[datetime] = None

class PaymentCreate(BaseModel):
    """Schema for creating a payment"""
    tenant_id: str
    invoice_id: str
    amount: float
    currency: str = "MXN"
    method: Optional[str] = None
    reference: Optional[str] = None
    received_at: Optional[datetime] = None
    created_by: Optional[str] = None

class PaymentResponse(BaseModel):
    """Schema for payment response"""
    id: str
    amount: float
    currency: str
    method: Optional[str] = None
    reference: Optional[str] = None
    invoice_id: str
    tenant_id: str
    received_at: datetime
    created_by: Optional[str] = None

# Invoice Item Schemas
class InvoiceItemCreate(BaseModel):
    """Schema for creating an invoice item"""
    study_type_id: Optional[str] = None
    description: str
    quantity: int = 1
    unit_price: float

class InvoiceItemUpdate(BaseModel):
    """Schema for updating an invoice item"""
    description: Optional[str] = None
    quantity: Optional[int] = None
    unit_price: Optional[float] = None

class InvoiceItemResponse(BaseModel):
    """Schema for invoice item response"""
    id: str
    invoice_id: str
    study_type_id: Optional[str] = None
    description: str
    quantity: int
    unit_price: float
    subtotal: float

# Enhanced Invoice Schemas with Items
class InvoiceWithItemsResponse(BaseModel):
    """Schema for invoice with items"""
    id: str
    invoice_number: str
    subtotal: float
    discount_total: float
    tax_total: float
    total: float
    amount_paid: float
    currency: str
    status: str
    order_id: str
    tenant_id: str
    branch_id: str
    issued_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    paid_at: Optional[datetime] = None
    items: List[InvoiceItemResponse]
    payments: List[PaymentResponse]
    balance: float
