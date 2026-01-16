from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, Session, func
from typing import List
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext, current_user
from app.models.billing import Invoice, Payment, ServiceCatalog, InvoiceItem
from app.models.laboratory import LabOrder
from app.models.tenant import Tenant, Branch
from app.models.user import AppUser
from app.models.enums import PaymentStatus, UserRole
from app.schemas.billing import (
    InvoiceCreate, InvoiceResponse, InvoiceDetailResponse, 
    PaymentCreate, PaymentResponse,
    ServiceCatalogCreate, ServiceCatalogUpdate, ServiceCatalogResponse,
    InvoiceItemCreate, InvoiceItemResponse, InvoiceWithItemsResponse
)
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing")


def calculate_invoice_balance(session: Session, invoice_id: str) -> float:
    """Calculate remaining balance for an invoice"""
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        return 0.0
    
    # Sum all payments for this invoice
    total_paid = session.exec(
        select(func.sum(Payment.amount_paid))
        .where(Payment.invoice_id == invoice_id)
    ).first() or 0.0
    
    balance = float(invoice.amount_total) - float(total_paid)
    return max(balance, 0.0)  # Never negative


def update_invoice_status(session: Session, invoice_id: str) -> None:
    """Update invoice status based on payment balance"""
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        return
    
    balance = calculate_invoice_balance(session, invoice_id)
    
    if balance == 0.0:
        invoice.status = PaymentStatus.PAID
    elif balance < float(invoice.amount_total):
        invoice.status = PaymentStatus.PARTIAL
    else:
        invoice.status = PaymentStatus.PENDING
    
    session.add(invoice)


def update_order_payment_lock(session: Session, order_id: str) -> None:
    """Update order payment lock based on invoice balance"""
    # Get all invoices for this order
    invoices = session.exec(
        select(Invoice).where(Invoice.order_id == order_id)
    ).all()
    
    if not invoices:
        return
    
    # Calculate total balance across all invoices
    total_balance = sum(calculate_invoice_balance(session, str(inv.id)) for inv in invoices)
    
    # Update order lock
    order = session.get(LabOrder, order_id)
    if order:
        order.billed_lock = total_balance > 0
        session.add(order)
        
        logger.info(
            f"Order {order_id} payment lock updated",
            extra={
                "event": "order.payment_lock_updated",
                "order_id": order_id,
                "locked": order.billed_lock,
                "total_balance": total_balance,
            },
        )

@router.get("/invoices/")
def list_invoices(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """List all invoices"""
    invoices = session.exec(select(Invoice).where(Invoice.tenant_id == ctx.tenant_id)).all()
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
def get_invoice(
    invoice_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Get invoice details"""
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    if str(invoice.tenant_id) != ctx.tenant_id:
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


@router.get("/invoices/{invoice_id}/full", response_model=InvoiceWithItemsResponse)
def get_invoice_with_items(
    invoice_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Get invoice details with items and payments"""
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    if str(invoice.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Invoice not found")
    
    # Get items
    items = session.exec(
        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)
    ).all()
    
    # Get payments
    payments = session.exec(
        select(Payment).where(Payment.invoice_id == invoice_id)
    ).all()
    
    # Calculate balance
    balance = calculate_invoice_balance(session, invoice_id)
    
    return InvoiceWithItemsResponse(
        id=str(invoice.id),
        invoice_number=invoice.invoice_number,
        amount_total=float(invoice.amount_total),
        currency=invoice.currency,
        status=invoice.status,
        order_id=str(invoice.order_id),
        tenant_id=str(invoice.tenant_id),
        branch_id=str(invoice.branch_id),
        issued_at=invoice.issued_at,
        items=[
            InvoiceItemResponse(
                id=str(item.id),
                invoice_id=str(item.invoice_id),
                service_id=str(item.service_id) if item.service_id else None,
                description=item.description,
                quantity=item.quantity,
                unit_price=float(item.unit_price),
                subtotal=float(item.subtotal),
            )
            for item in items
        ],
        payments=[
            PaymentResponse(
                id=str(p.id),
                amount_paid=float(p.amount_paid),
                method=p.method,
                invoice_id=str(p.invoice_id),
                tenant_id=str(p.tenant_id),
                branch_id=str(p.branch_id),
            )
            for p in payments
        ],
        balance=balance,
    )


@router.post("/invoices/{invoice_id}/items", response_model=InvoiceItemResponse)
def add_invoice_item(
    invoice_id: str,
    item_data: InvoiceItemCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Add an item to an invoice"""
    # Check user role
    if user.role not in [UserRole.ADMIN, UserRole.BILLING]:
        raise HTTPException(403, "Only administrators or billing staff can add invoice items")
    
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    
    if str(invoice.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Invoice does not belong to your tenant")
    
    # Calculate subtotal
    subtotal = item_data.quantity * item_data.unit_price
    
    item = InvoiceItem(
        tenant_id=invoice.tenant_id,
        invoice_id=invoice.id,
        service_id=item_data.service_id if item_data.service_id else None,
        description=item_data.description,
        quantity=item_data.quantity,
        unit_price=item_data.unit_price,
        subtotal=subtotal,
    )
    
    session.add(item)
    session.flush()
    
    # Recalculate invoice total
    all_items = session.exec(
        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)
    ).all()
    invoice.amount_total = sum(float(i.subtotal) for i in all_items)
    session.add(invoice)
    
    # Update payment lock since invoice total changed
    update_order_payment_lock(session, str(invoice.order_id))
    
    session.commit()
    session.refresh(item)
    
    return InvoiceItemResponse(
        id=str(item.id),
        invoice_id=str(item.invoice_id),
        service_id=str(item.service_id) if item.service_id else None,
        description=item.description,
        quantity=item.quantity,
        unit_price=float(item.unit_price),
        subtotal=float(item.subtotal),
    )

@router.get("/payments/")
def list_payments(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """List all payments"""
    payments = session.exec(select(Payment).where(Payment.tenant_id == ctx.tenant_id)).all()
    return [{
        "id": str(p.id),
        "amount_paid": float(p.amount_paid),
        "method": p.method,
        "invoice_id": str(p.invoice_id),
        "tenant_id": str(p.tenant_id),
        "branch_id": str(p.branch_id)
    } for p in payments]

@router.post("/payments/", response_model=PaymentResponse)
def create_payment(payment_data: PaymentCreate, session: Session = Depends(get_session), ctx: AuthContext = Depends(get_auth_ctx)):
    """Create a new payment and update invoice/order status"""
    # Verify tenant, branch, and invoice exist
    tenant = session.get(Tenant, payment_data.tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    if str(tenant.id) != ctx.tenant_id:
        raise HTTPException(403, "Cannot create payment for different tenant")
    
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
    session.flush()
    
    # Update invoice status based on new balance
    update_invoice_status(session, str(invoice.id))
    
    # Update order payment lock
    update_order_payment_lock(session, str(invoice.order_id))
    
    session.commit()
    session.refresh(payment)
    
    logger.info(
        f"Payment created for invoice {invoice.invoice_number}",
        extra={
            "event": "payment.created",
            "payment_id": str(payment.id),
            "invoice_id": str(invoice.id),
            "amount": float(payment.amount_paid),
        },
    )
    
    return PaymentResponse(
        id=str(payment.id),
        amount_paid=float(payment.amount_paid),
        method=payment.method,
        invoice_id=str(payment.invoice_id),
        tenant_id=str(payment.tenant_id),
        branch_id=str(payment.branch_id)
    )


@router.get("/orders/{order_id}/balance")
def get_order_balance(
    order_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Get payment balance for an order"""
    order = session.get(LabOrder, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    
    if str(order.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Order does not belong to your tenant")
    
    # Get all invoices for this order
    invoices = session.exec(
        select(Invoice).where(Invoice.order_id == order_id)
    ).all()
    
    total_invoiced = sum(float(inv.amount_total) for inv in invoices)
    total_paid = 0.0
    
    for invoice in invoices:
        payments = session.exec(
            select(Payment).where(Payment.invoice_id == invoice.id)
        ).all()
        total_paid += sum(float(p.amount_paid) for p in payments)
    
    balance = total_invoiced - total_paid
    
    return {
        "order_id": str(order.id),
        "total_invoiced": total_invoiced,
        "total_paid": total_paid,
        "balance": max(balance, 0.0),
        "is_locked": order.billed_lock,
        "invoices": [
            {
                "id": str(inv.id),
                "invoice_number": inv.invoice_number,
                "amount_total": float(inv.amount_total),
                "status": inv.status,
                "balance": calculate_invoice_balance(session, str(inv.id)),
            }
            for inv in invoices
        ]
    }


# Service Catalog Endpoints

@router.get("/catalog", response_model=List[ServiceCatalogResponse])
def list_services(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    active_only: bool = True,
):
    """List all services in catalog"""
    query = select(ServiceCatalog).where(ServiceCatalog.tenant_id == ctx.tenant_id)
    
    if active_only:
        query = query.where(ServiceCatalog.is_active == True)
    
    services = session.exec(query).all()
    
    return [
        ServiceCatalogResponse(
            id=str(s.id),
            tenant_id=str(s.tenant_id),
            service_name=s.service_name,
            service_code=s.service_code,
            description=s.description,
            price=float(s.price),
            currency=s.currency,
            is_active=s.is_active,
            valid_from=s.valid_from,
            valid_until=s.valid_until,
            created_at=s.created_at,
        )
        for s in services
    ]


@router.post("/catalog", response_model=ServiceCatalogResponse)
def create_service(
    service_data: ServiceCatalogCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new service (Admin only)"""
    # Check user role
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Only administrators can create services")
    
    # Check if service code already exists for this tenant
    existing = session.exec(
        select(ServiceCatalog).where(
            ServiceCatalog.tenant_id == ctx.tenant_id,
            ServiceCatalog.service_code == service_data.service_code
        )
    ).first()
    
    if existing:
        raise HTTPException(400, "Service code already exists")
    
    service = ServiceCatalog(
        tenant_id=ctx.tenant_id,
        service_name=service_data.service_name,
        service_code=service_data.service_code,
        description=service_data.description,
        price=service_data.price,
        currency=service_data.currency,
        valid_from=service_data.valid_from,
        valid_until=service_data.valid_until,
    )
    
    session.add(service)
    session.commit()
    session.refresh(service)
    
    logger.info(
        f"Service {service.service_code} created",
        extra={
            "event": "service.created",
            "service_id": str(service.id),
            "user_id": str(user.id),
        },
    )
    
    return ServiceCatalogResponse(
        id=str(service.id),
        tenant_id=str(service.tenant_id),
        service_name=service.service_name,
        service_code=service.service_code,
        description=service.description,
        price=float(service.price),
        currency=service.currency,
        is_active=service.is_active,
        valid_from=service.valid_from,
        valid_until=service.valid_until,
        created_at=service.created_at,
    )


@router.put("/catalog/{service_id}", response_model=ServiceCatalogResponse)
def update_service(
    service_id: str,
    service_data: ServiceCatalogUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Update a service (Admin only)"""
    # Check user role
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Only administrators can update services")
    
    service = session.get(ServiceCatalog, service_id)
    if not service:
        raise HTTPException(404, "Service not found")
    
    if str(service.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Service does not belong to your tenant")
    
    # Update fields
    if service_data.service_name is not None:
        service.service_name = service_data.service_name
    if service_data.service_code is not None:
        service.service_code = service_data.service_code
    if service_data.description is not None:
        service.description = service_data.description
    if service_data.price is not None:
        service.price = service_data.price
    if service_data.currency is not None:
        service.currency = service_data.currency
    if service_data.is_active is not None:
        service.is_active = service_data.is_active
    if service_data.valid_from is not None:
        service.valid_from = service_data.valid_from
    if service_data.valid_until is not None:
        service.valid_until = service_data.valid_until
    
    session.add(service)
    session.commit()
    session.refresh(service)
    
    logger.info(
        f"Service {service.service_code} updated",
        extra={
            "event": "service.updated",
            "service_id": str(service.id),
            "user_id": str(user.id),
        },
    )
    
    return ServiceCatalogResponse(
        id=str(service.id),
        tenant_id=str(service.tenant_id),
        service_name=service.service_name,
        service_code=service.service_code,
        description=service.description,
        price=float(service.price),
        currency=service.currency,
        is_active=service.is_active,
        valid_from=service.valid_from,
        valid_until=service.valid_until,
        created_at=service.created_at,
    )


@router.delete("/catalog/{service_id}")
def deactivate_service(
    service_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Deactivate a service (Admin only)"""
    # Check user role
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Only administrators can deactivate services")
    
    service = session.get(ServiceCatalog, service_id)
    if not service:
        raise HTTPException(404, "Service not found")
    
    if str(service.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Service does not belong to your tenant")
    
    service.is_active = False
    session.add(service)
    session.commit()
    
    logger.info(
        f"Service {service.service_code} deactivated",
        extra={
            "event": "service.deactivated",
            "service_id": str(service.id),
            "user_id": str(user.id),
        },
    )
    
    return {"message": "Service deactivated successfully"}
