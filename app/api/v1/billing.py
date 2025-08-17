from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, Session
from app.core.db import get_session
from app.models.billing import Invoice, Payment
from app.models.laboratory import LabOrder
from app.models.tenant import Tenant, Branch

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

@router.post("/invoices/")
def create_invoice(
    tenant_id: str,
    branch_id: str,
    order_id: str,
    invoice_number: str,
    amount_total: float,
    currency: str = "MXN",
    session: Session = Depends(get_session)
):
    """Create a new invoice"""
    # Verify tenant, branch, and order exist
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    branch = session.get(Branch, branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    
    order = session.get(LabOrder, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    
    # Check if invoice_number is unique for this branch
    existing_invoice = session.exec(
        select(Invoice).where(
            Invoice.invoice_number == invoice_number,
            Invoice.branch_id == branch_id
        )
    ).first()
    
    if existing_invoice:
        raise HTTPException(400, "Invoice number already exists for this branch")
    
    invoice = Invoice(
        tenant_id=tenant_id,
        branch_id=branch_id,
        order_id=order_id,
        invoice_number=invoice_number,
        amount_total=amount_total,
        currency=currency
    )
    
    session.add(invoice)
    session.commit()
    session.refresh(invoice)
    
    return {
        "id": str(invoice.id),
        "invoice_number": invoice.invoice_number,
        "amount_total": float(invoice.amount_total),
        "currency": invoice.currency,
        "status": invoice.status,
        "order_id": str(invoice.order_id),
        "tenant_id": str(invoice.tenant_id),
        "branch_id": str(invoice.branch_id)
    }

@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str, session: Session = Depends(get_session)):
    """Get invoice details"""
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    
    return {
        "id": str(invoice.id),
        "invoice_number": invoice.invoice_number,
        "amount_total": float(invoice.amount_total),
        "currency": invoice.currency,
        "status": invoice.status,
        "order_id": str(invoice.order_id),
        "tenant_id": str(invoice.tenant_id),
        "branch_id": str(invoice.branch_id),
        "issued_at": invoice.issued_at
    }

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

@router.post("/payments/")
def create_payment(
    tenant_id: str,
    branch_id: str,
    invoice_id: str,
    amount_paid: float,
    method: str = None,
    session: Session = Depends(get_session)
):
    """Create a new payment"""
    # Verify tenant, branch, and invoice exist
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    branch = session.get(Branch, branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    
    payment = Payment(
        tenant_id=tenant_id,
        branch_id=branch_id,
        invoice_id=invoice_id,
        amount_paid=amount_paid,
        method=method
    )
    
    session.add(payment)
    session.commit()
    session.refresh(payment)
    
    return {
        "id": str(payment.id),
        "amount_paid": float(payment.amount_paid),
        "method": payment.method,
        "invoice_id": str(payment.invoice_id),
        "tenant_id": str(payment.tenant_id),
        "branch_id": str(payment.branch_id)
    }
