from pydantic import BaseModel
from typing import Optional
from decimal import Decimal
from datetime import datetime

class InvoiceCreate(BaseModel):
    """Schema for creating an invoice"""
    tenant_id: str
    branch_id: str
    order_id: str
    invoice_number: str
    amount_total: float
    currency: str = "MXN"

class InvoiceResponse(BaseModel):
    """Schema for invoice response"""
    id: str
    invoice_number: str
    amount_total: float
    currency: str
    status: str
    order_id: str
    tenant_id: str
    branch_id: str

class InvoiceDetailResponse(BaseModel):
    """Schema for detailed invoice response"""
    id: str
    invoice_number: str
    amount_total: float
    currency: str
    status: str
    order_id: str
    tenant_id: str
    branch_id: str
    issued_at: Optional[datetime] = None

class PaymentCreate(BaseModel):
    """Schema for creating a payment"""
    tenant_id: str
    branch_id: str
    invoice_id: str
    amount_paid: float
    method: Optional[str] = None

class PaymentResponse(BaseModel):
    """Schema for payment response"""
    id: str
    amount_paid: float
    method: Optional[str] = None
    invoice_id: str
    tenant_id: str
    branch_id: str
