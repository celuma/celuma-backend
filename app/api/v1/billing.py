from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, Session
from app.core.db import get_session
from app.models.billing import Invoice, Payment
from app.models.laboratory import LabOrder
from app.models.tenant import Tenant, Branch
from app.schemas.billing import InvoiceCreate, InvoiceResponse, InvoiceDetailResponse, PaymentCreate, PaymentResponse

router = APIRouter(prefix="/billing")

@router.get("/invoices/")
def list_invoices(session: Session = Depends(get_session)):
    """List all invoices"""
    invoices = session.exec(select(Invoice)).all()
    return [{
        "id": str(i.id),
        "invoice_number": i.invoice_number,
        "amount_total": float(i.amount_total),
        "currency": i.currency,
        "status": i.status,
        "order_id": str(i.order_id),
        "tenant_id": str(i.tenant_id),
        "branch_id": str(i.branch_id)
    } for i in invoices]

@router.post("/invoices/", response_model=InvoiceResponse)
def create_invoice(invoice_data: InvoiceCreate, session: Session = Depends(get_session)):
    """Create a new invoice"""
    # Verify tenant, branch, and order exist
    tenant = session.get(Tenant, invoice_data.tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    branch = session.get(Branch, invoice_data.branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    
    order = session.get(LabOrder, invoice_data.order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    
    # Check if invoice_number is unique for this branch
    existing_invoice = session.exec(
        select(Invoice).where(
            Invoice.invoice_number == invoice_data.invoice_number,
            Invoice.branch_id == invoice_data.branch_id
        )
    ).first()
    
    if existing_invoice:
        raise HTTPException(400, "Invoice number already exists for this branch")
    
    invoice = Invoice(
        tenant_id=invoice_data.tenant_id,
        branch_id=invoice_data.branch_id,
        order_id=invoice_data.order_id,
        invoice_number=invoice_data.invoice_number,
        amount_total=invoice_data.amount_total,
        currency=invoice_data.currency,
        issued_at=invoice_data.issued_at if invoice_data.issued_at is not None else None
    )
    
    session.add(invoice)
    session.commit()
    session.refresh(invoice)
    
    return InvoiceResponse(
        id=str(invoice.id),
        invoice_number=invoice.invoice_number,
        amount_total=float(invoice.amount_total),
        currency=invoice.currency,
        status=invoice.status,
        order_id=str(invoice.order_id),
        tenant_id=str(invoice.tenant_id),
        branch_id=str(invoice.branch_id)
    )

@router.get("/invoices/{invoice_id}", response_model=InvoiceDetailResponse)
def get_invoice(invoice_id: str, session: Session = Depends(get_session)):
    """Get invoice details"""
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    
    return InvoiceDetailResponse(
        id=str(invoice.id),
        invoice_number=invoice.invoice_number,
        amount_total=float(invoice.amount_total),
        currency=invoice.currency,
        status=invoice.status,
        order_id=str(invoice.order_id),
        tenant_id=str(invoice.tenant_id),
        branch_id=str(invoice.branch_id),
        issued_at=invoice.issued_at
    )

@router.get("/payments/")
def list_payments(session: Session = Depends(get_session)):
    """List all payments"""
    payments = session.exec(select(Payment)).all()
    return [{
        "id": str(p.id),
        "amount_paid": float(p.amount_paid),
        "method": p.method,
        "invoice_id": str(p.invoice_id),
        "tenant_id": str(p.tenant_id),
        "branch_id": str(p.branch_id)
    } for p in payments]

@router.post("/payments/", response_model=PaymentResponse)
def create_payment(payment_data: PaymentCreate, session: Session = Depends(get_session)):
    """Create a new payment"""
    # Verify tenant, branch, and invoice exist
    tenant = session.get(Tenant, payment_data.tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    branch = session.get(Branch, payment_data.branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    
    invoice = session.get(Invoice, payment_data.invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    
    payment = Payment(
        tenant_id=payment_data.tenant_id,
        branch_id=payment_data.branch_id,
        invoice_id=payment_data.invoice_id,
        amount_paid=payment_data.amount_paid,
        method=payment_data.method,
        paid_at=payment_data.paid_at if payment_data.paid_at is not None else None
    )
    
    session.add(payment)
    session.commit()
    session.refresh(payment)
    
    return PaymentResponse(
        id=str(payment.id),
        amount_paid=float(payment.amount_paid),
        method=payment.method,
        invoice_id=str(payment.invoice_id),
        tenant_id=str(payment.tenant_id),
        branch_id=str(payment.branch_id)
    )
