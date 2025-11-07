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
    amount_total: float
    currency: str = "MXN"
    issued_at: Optional[datetime] = None

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
    paid_at: Optional[datetime] = None

class PaymentResponse(BaseModel):
    """Schema for payment response"""
    id: str
    amount_paid: float
    method: Optional[str] = None
    invoice_id: str
    tenant_id: str
    branch_id: str

# Service Catalog Schemas
class ServiceCatalogCreate(BaseModel):
    """Schema for creating a service"""
    service_name: str
    service_code: str
    description: Optional[str] = None
    price: float
    currency: str = "MXN"
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None

class ServiceCatalogUpdate(BaseModel):
    """Schema for updating a service"""
    service_name: Optional[str] = None
    service_code: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = None
    is_active: Optional[bool] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None

class ServiceCatalogResponse(BaseModel):
    """Schema for service catalog response"""
    id: str
    tenant_id: str
    service_name: str
    service_code: str
    description: Optional[str] = None
    price: float
    currency: str
    is_active: bool
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

# Invoice Item Schemas
class InvoiceItemCreate(BaseModel):
    """Schema for creating an invoice item"""
    service_id: Optional[str] = None
    description: str
    quantity: int = 1
    unit_price: float

class InvoiceItemResponse(BaseModel):
    """Schema for invoice item response"""
    id: str
    invoice_id: str
    service_id: Optional[str] = None
    description: str
    quantity: int
    unit_price: float
    subtotal: float

# Enhanced Invoice Schemas with Items
class InvoiceWithItemsResponse(BaseModel):
    """Schema for invoice with items"""
    id: str
    invoice_number: str
    amount_total: float
    currency: str
    status: str
    order_id: str
    tenant_id: str
    branch_id: str
    issued_at: Optional[datetime] = None
    items: List[InvoiceItemResponse]
    payments: List[PaymentResponse]
    balance: float
