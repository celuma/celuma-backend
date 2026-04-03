from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, Session, func
from typing import List
from datetime import datetime
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext, current_user
from app.models.billing import Invoice, Payment, InvoiceItem
from app.models.laboratory import Order
from app.models.tenant import Tenant, Branch
from app.models.user import AppUser
from app.models.enums import PaymentStatus
from app.core.rbac import has_permission
from app.schemas.billing import (
    InvoiceCreate, InvoiceResponse, InvoiceDetailResponse, 
    PaymentCreate, PaymentResponse,
    InvoiceItemCreate, InvoiceItemUpdate, InvoiceItemResponse, InvoiceWithItemsResponse
)
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing")


def create_invoice_for_order(session: Session, order: Order) -> Invoice:
    """Create invoice automatically for an order with default price from price_catalog
    
    Args:
        session: Database session
        order: Order object (must be flushed/committed with id)
    
    Returns:
        Created Invoice object
    """
    # Check if invoice already exists
    if order.invoice_id:
        existing_invoice = session.get(Invoice, order.invoice_id)
        if existing_invoice:
            logger.info(f"Invoice already exists for order {order.id}")
            return existing_invoice
    
    # Check if invoice exists by order_id (in case invoice_id wasn't set on order)
    existing_invoice = session.exec(
        select(Invoice).where(Invoice.order_id == order.id)
    ).first()
    
    if existing_invoice:
        logger.info(f"Invoice already exists for order {order.id}")
        order.invoice_id = existing_invoice.id
        session.add(order)
        return existing_invoice
    
    # Get price from price_catalog if study_type is set
    unit_price = 0.0
    study_type_name = "Servicio"
    
    if order.study_type_id:
        from app.models.study_type import StudyType
        from app.models.price_catalog import PriceCatalog
        
        study_type = session.get(StudyType, order.study_type_id)
        if study_type:
            study_type_name = study_type.name
            
            # Get active price for this study type
            price_entry = session.exec(
                select(PriceCatalog).where(
                    PriceCatalog.study_type_id == order.study_type_id,
                    PriceCatalog.tenant_id == order.tenant_id,
                    PriceCatalog.is_active == True
                ).order_by(PriceCatalog.created_at.desc())
            ).first()
            
            if price_entry:
                unit_price = float(price_entry.unit_price)
    
    # Generate invoice_number (simple: branch_id + order_code or sequential)
    invoice_number = f"INV-{order.order_code}"
    
    # Create Invoice
    invoice = Invoice(
        tenant_id=order.tenant_id,
        branch_id=order.branch_id,
        order_id=order.id,
        invoice_number=invoice_number,
        subtotal=unit_price,
        discount_total=0.0,
        tax_total=0.0,
        total=unit_price,
        amount_total=unit_price,
        amount_paid=0.0,
        currency="MXN",
        status=PaymentStatus.PENDING,
        issued_at=datetime.utcnow(),
    )
    
    session.add(invoice)
    session.flush()  # Get invoice.id
    
    # Create InvoiceItem (one line)
    item = InvoiceItem(
        tenant_id=order.tenant_id,
        invoice_id=invoice.id,
        study_type_id=order.study_type_id,
        description=study_type_name,
        quantity=1,
        unit_price=unit_price,
        subtotal=unit_price,
    )
    
    session.add(item)
    
    # Update order.invoice_id
    order.invoice_id = invoice.id
    session.add(order)
    
    # Create event
    from app.models.events import OrderEvent
    from app.models.enums import EventType
    
    event = OrderEvent(
        tenant_id=order.tenant_id,
        branch_id=order.branch_id,
        order_id=order.id,
        event_type=EventType.INVOICE_CREATED,
        description="Factura creada",
        event_metadata={
            "invoice_id": str(invoice.id),
            "invoice_number": invoice_number,
            "total": unit_price,
        },
        created_by=order.created_by,
    )
    session.add(event)
    
    logger.info(
        f"Invoice created for order {order.order_code}",
        extra={
            "event": "invoice.auto_created",
            "order_id": str(order.id),
            "invoice_id": str(invoice.id),
            "total": unit_price,
        },
    )

    # Set billed_lock so the order cannot be released before payment
    update_order_payment_lock(session, str(order.id))
    from app.api.v1.laboratory import update_order_status
    update_order_status(str(order.id), session)

    return invoice


def calculate_invoice_balance(session: Session, invoice_id: str) -> float:
    """Calculate remaining balance for an invoice"""
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        return 0.0
    
    # Sum all payments for this invoice (using new 'amount' field)
    total_paid = session.exec(
        select(func.sum(Payment.amount))
        .where(Payment.invoice_id == invoice_id)
    ).first() or 0.0
    
    balance = float(invoice.total) - float(total_paid)
    return max(balance, 0.0)  # Never negative


def update_invoice_status(session: Session, invoice_id: str) -> None:
    """Update invoice status based on payment balance and cache amount_paid/paid_at"""
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        return
    
    # Calculate total paid
    total_paid = session.exec(
        select(func.sum(Payment.amount))
        .where(Payment.invoice_id == invoice_id)
    ).first() or 0.0
    
    balance = float(invoice.total) - float(total_paid)
    
    # Update amount_paid cache
    invoice.amount_paid = total_paid
    invoice.updated_at = datetime.utcnow()
    
    # Update status
    if balance <= 0.0:
        invoice.status = PaymentStatus.PAID
        if not invoice.paid_at:
            invoice.paid_at = datetime.utcnow()
    elif balance < float(invoice.total):
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
    order = session.get(Order, order_id)
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
    user: AppUser = Depends(current_user),
):
    """List all invoices (requires billing:read)."""
    if not has_permission(user.id, "billing:read", session):
        raise HTTPException(403, "Permission required: billing:read")
    invoices = session.exec(select(Invoice).where(Invoice.tenant_id == ctx.tenant_id)).all()
    return [{
        "id": str(i.id),
        "invoice_number": i.invoice_number,
        "subtotal": float(i.subtotal),
        "discount_total": float(i.discount_total),
        "tax_total": float(i.tax_total),
        "total": float(i.total),
        "amount_paid": float(i.amount_paid),
        "currency": i.currency,
        "status": i.status,
        "order_id": str(i.order_id),
        "tenant_id": str(i.tenant_id),
        "branch_id": str(i.branch_id),
        "paid_at": i.paid_at,
    } for i in invoices]

@router.post("/invoices/", response_model=InvoiceResponse)
def create_invoice(
    invoice_data: InvoiceCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new invoice (requires billing:create_invoice)."""
    if not has_permission(user.id, "billing:create_invoice", session):
        raise HTTPException(403, "Permission required: billing:create_invoice")
    if str(invoice_data.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Cannot create invoice for a different tenant")
    # Verify tenant, branch, and order exist
    tenant = session.get(Tenant, invoice_data.tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    branch = session.get(Branch, invoice_data.branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    
    order = session.get(Order, invoice_data.order_id)
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
        subtotal=invoice_data.subtotal,
        discount_total=invoice_data.discount_total,
        tax_total=invoice_data.tax_total,
        total=invoice_data.total,
        amount_total=invoice_data.total,
        amount_paid=0.0,
        currency=invoice_data.currency,
        issued_at=invoice_data.issued_at if invoice_data.issued_at is not None else None
    )
    
    session.add(invoice)
    session.flush()  # Get invoice.id before updating locks

    # Recalculate payment lock and order status so a manually created invoice
    # also blocks the order from being released before payment.
    update_order_payment_lock(session, str(invoice_data.order_id))
    try:
        from app.api.v1.laboratory import update_order_status
        update_order_status(str(invoice_data.order_id), session)
    except Exception as e:
        logger.warning(
            f"Could not update order status after manual invoice creation: {e}",
            extra={"event": "order.status_update_skipped", "order_id": str(invoice_data.order_id)},
        )

    session.commit()
    session.refresh(invoice)
    
    return InvoiceResponse(
        id=str(invoice.id),
        invoice_number=invoice.invoice_number,
        subtotal=float(invoice.subtotal),
        discount_total=float(invoice.discount_total),
        tax_total=float(invoice.tax_total),
        total=float(invoice.total),
        amount_paid=float(invoice.amount_paid),
        currency=invoice.currency,
        status=invoice.status,
        order_id=str(invoice.order_id),
        tenant_id=str(invoice.tenant_id),
        branch_id=str(invoice.branch_id),
        paid_at=invoice.paid_at,
    )

@router.get("/invoices/{invoice_id}", response_model=InvoiceDetailResponse)
def get_invoice(
    invoice_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Get invoice details (requires billing:read)."""
    if not has_permission(user.id, "billing:read", session):
        raise HTTPException(403, "Permission required: billing:read")
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    if str(invoice.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Invoice not found")
    
    return InvoiceDetailResponse(
        id=str(invoice.id),
        invoice_number=invoice.invoice_number,
        subtotal=float(invoice.subtotal),
        discount_total=float(invoice.discount_total),
        tax_total=float(invoice.tax_total),
        total=float(invoice.total),
        amount_paid=float(invoice.amount_paid),
        currency=invoice.currency,
        status=invoice.status,
        order_id=str(invoice.order_id),
        tenant_id=str(invoice.tenant_id),
        branch_id=str(invoice.branch_id),
        issued_at=invoice.issued_at,
        updated_at=invoice.updated_at,
        paid_at=invoice.paid_at,
    )


@router.get("/invoices/{invoice_id}/full", response_model=InvoiceWithItemsResponse)
def get_invoice_with_items(
    invoice_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Get invoice details with items and payments (requires billing:read)."""
    if not has_permission(user.id, "billing:read", session):
        raise HTTPException(403, "Permission required: billing:read")
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
        subtotal=float(invoice.subtotal),
        discount_total=float(invoice.discount_total),
        tax_total=float(invoice.tax_total),
        total=float(invoice.total),
        amount_paid=float(invoice.amount_paid),
        currency=invoice.currency,
        status=invoice.status,
        order_id=str(invoice.order_id),
        tenant_id=str(invoice.tenant_id),
        branch_id=str(invoice.branch_id),
        issued_at=invoice.issued_at,
        updated_at=invoice.updated_at,
        paid_at=invoice.paid_at,
        items=[
            InvoiceItemResponse(
                id=str(item.id),
                invoice_id=str(item.invoice_id),
                study_type_id=str(item.study_type_id) if item.study_type_id else None,
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
                amount=float(p.amount),
                currency=p.currency,
                method=p.method,
                reference=p.reference,
                invoice_id=str(p.invoice_id),
                tenant_id=str(p.tenant_id),
                received_at=p.received_at,
                created_by=str(p.created_by) if p.created_by else None,
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
    """Add an item to an invoice (requires billing:edit_items)."""
    if not has_permission(user.id, "billing:edit_items", session):
        raise HTTPException(403, "Permission required: billing:edit_items")
    
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
        study_type_id=item_data.study_type_id if item_data.study_type_id else None,
        description=item_data.description,
        quantity=item_data.quantity,
        unit_price=item_data.unit_price,
        subtotal=subtotal,
    )
    
    session.add(item)
    session.flush()
    
    # Recalculate invoice totals
    all_items = session.exec(
        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)
    ).all()
    new_subtotal = sum(float(i.subtotal) for i in all_items)
    invoice.subtotal = new_subtotal
    invoice.total = new_subtotal + float(invoice.discount_total) + float(invoice.tax_total)
    invoice.amount_total = invoice.total
    invoice.updated_at = datetime.utcnow()
    session.add(invoice)
    
    # Update payment lock since invoice total changed
    update_order_payment_lock(session, str(invoice.order_id))
    
    session.commit()
    session.refresh(item)
    
    return InvoiceItemResponse(
        id=str(item.id),
        invoice_id=str(item.invoice_id),
        study_type_id=str(item.study_type_id) if item.study_type_id else None,
        description=item.description,
        quantity=item.quantity,
        unit_price=float(item.unit_price),
        subtotal=float(item.subtotal),
    )

@router.patch("/invoices/{invoice_id}/items/{item_id}", response_model=InvoiceItemResponse)
def update_invoice_item(
    invoice_id: str,
    item_id: str,
    item_data: InvoiceItemUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Update an invoice item (requires billing:edit_items)."""
    if not has_permission(user.id, "billing:edit_items", session):
        raise HTTPException(403, "Permission required: billing:edit_items")
    
    # Get invoice and verify tenant
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    
    if str(invoice.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Invoice does not belong to your tenant")
    
    # Get item and verify it belongs to the invoice
    item = session.get(InvoiceItem, item_id)
    if not item:
        raise HTTPException(404, "Invoice item not found")
    
    if str(item.invoice_id) != invoice_id:
        raise HTTPException(400, "Item does not belong to this invoice")
    
    # Update fields if provided
    if item_data.description is not None:
        item.description = item_data.description
    if item_data.quantity is not None:
        item.quantity = item_data.quantity
    if item_data.unit_price is not None:
        item.unit_price = item_data.unit_price
    
    # Recalculate item subtotal
    item.subtotal = item.quantity * item.unit_price
    session.add(item)
    session.flush()
    
    # Recalculate invoice totals
    all_items = session.exec(
        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)
    ).all()
    new_subtotal = sum(float(i.subtotal) for i in all_items)
    invoice.subtotal = new_subtotal
    invoice.total = new_subtotal + float(invoice.discount_total) + float(invoice.tax_total)
    invoice.amount_total = invoice.total
    invoice.updated_at = datetime.utcnow()
    session.add(invoice)
    
    # Update payment lock since invoice total changed
    update_order_payment_lock(session, str(invoice.order_id))
    
    session.commit()
    session.refresh(item)
    
    return InvoiceItemResponse(
        id=str(item.id),
        invoice_id=str(item.invoice_id),
        study_type_id=str(item.study_type_id) if item.study_type_id else None,
        description=item.description,
        quantity=item.quantity,
        unit_price=float(item.unit_price),
        subtotal=float(item.subtotal),
    )

@router.get("/payments/")
def list_payments(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """List all payments (requires billing:read)."""
    if not has_permission(user.id, "billing:read", session):
        raise HTTPException(403, "Permission required: billing:read")
    payments = session.exec(select(Payment).where(Payment.tenant_id == ctx.tenant_id)).all()
    return [{
        "id": str(p.id),
        "amount": float(p.amount),
        "currency": p.currency,
        "method": p.method,
        "reference": p.reference,
        "invoice_id": str(p.invoice_id),
        "tenant_id": str(p.tenant_id),
        "received_at": p.received_at,
        "created_by": str(p.created_by) if p.created_by else None,
    } for p in payments]

@router.post("/payments/", response_model=PaymentResponse)
def create_payment(
    payment_data: PaymentCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new payment and update invoice/order status (requires billing:register_payment)."""
    if not has_permission(user.id, "billing:register_payment", session):
        raise HTTPException(403, "Permission required: billing:register_payment")
    # Verify tenant and invoice exist
    tenant = session.get(Tenant, payment_data.tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    if str(tenant.id) != ctx.tenant_id:
        raise HTTPException(403, "Cannot create payment for different tenant")
    
    invoice = session.get(Invoice, payment_data.invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    
    payment = Payment(
        tenant_id=payment_data.tenant_id,
        invoice_id=payment_data.invoice_id,
        amount=payment_data.amount,
        currency=payment_data.currency,
        method=payment_data.method,
        reference=payment_data.reference,
        received_at=payment_data.received_at if payment_data.received_at else datetime.utcnow(),
        created_by=payment_data.created_by if payment_data.created_by else user.id,
    )
    
    session.add(payment)
    session.flush()
    
    # Update invoice status based on new balance (also updates amount_paid and paid_at)
    update_invoice_status(session, str(invoice.id))
    
    # Update order payment lock (billed_lock = False when balance is 0)
    update_order_payment_lock(session, str(invoice.order_id))
    # Recompute order status so it can move from CLOSED to RELEASED when payment is cleared
    from app.api.v1.laboratory import update_order_status
    update_order_status(str(invoice.order_id), session)
    
    # Create payment event
    from app.models.events import OrderEvent
    from app.models.enums import EventType
    
    event = OrderEvent(
        tenant_id=invoice.tenant_id,
        branch_id=invoice.branch_id,
        order_id=invoice.order_id,
        event_type=EventType.PAYMENT_RECEIVED,
        description="Pago recibido",
        event_metadata={
            "payment_id": str(payment.id),
            "amount": float(payment.amount),
            "method": payment.method,
            "invoice_number": invoice.invoice_number,
        },
        created_by=payment.created_by,
    )
    session.add(event)
    
    session.commit()
    session.refresh(payment)
    
    logger.info(
        f"Payment created for invoice {invoice.invoice_number}",
        extra={
            "event": "payment.created",
            "payment_id": str(payment.id),
            "invoice_id": str(invoice.id),
            "amount": float(payment.amount),
        },
    )
    
    return PaymentResponse(
        id=str(payment.id),
        amount=float(payment.amount),
        currency=payment.currency,
        method=payment.method,
        reference=payment.reference,
        invoice_id=str(payment.invoice_id),
        tenant_id=str(payment.tenant_id),
        received_at=payment.received_at,
        created_by=str(payment.created_by) if payment.created_by else None,
    )


@router.get("/orders/{order_id}/balance")
def get_order_balance(
    order_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Get payment balance for an order (requires billing:read)."""
    if not has_permission(user.id, "billing:read", session):
        raise HTTPException(403, "Permission required: billing:read")
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    
    if str(order.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Order does not belong to your tenant")
    
    # Get all invoices for this order
    invoices = session.exec(
        select(Invoice).where(Invoice.order_id == order_id)
    ).all()
    
    total_invoiced = sum(float(inv.total) for inv in invoices)
    total_paid = sum(float(inv.amount_paid) for inv in invoices)
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
                "total": float(inv.total),
                "status": inv.status,
                "balance": calculate_invoice_balance(session, str(inv.id)),
            }
            for inv in invoices
        ]
    }


@router.get("/orders/{order_id}/invoice", response_model=InvoiceWithItemsResponse)
def get_order_invoice(
    order_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Get invoice for an order (requires billing:read)."""
    if not has_permission(user.id, "billing:read", session):
        raise HTTPException(403, "Permission required: billing:read")
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    
    if str(order.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Order does not belong to your tenant")
    
    # Get invoice by order_id
    invoice = session.exec(
        select(Invoice).where(Invoice.order_id == order_id)
    ).first()
    
    if not invoice:
        raise HTTPException(404, "Invoice not found for this order")
    
    # Get items
    items = session.exec(
        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)
    ).all()
    
    # Get payments
    payments = session.exec(
        select(Payment).where(Payment.invoice_id == invoice.id)
    ).all()
    
    # Calculate balance
    balance = calculate_invoice_balance(session, str(invoice.id))
    
    return InvoiceWithItemsResponse(
        id=str(invoice.id),
        invoice_number=invoice.invoice_number,
        subtotal=float(invoice.subtotal),
        discount_total=float(invoice.discount_total),
        tax_total=float(invoice.tax_total),
        total=float(invoice.total),
        amount_paid=float(invoice.amount_paid),
        currency=invoice.currency,
        status=invoice.status,
        order_id=str(invoice.order_id),
        tenant_id=str(invoice.tenant_id),
        branch_id=str(invoice.branch_id),
        issued_at=invoice.issued_at,
        updated_at=invoice.updated_at,
        paid_at=invoice.paid_at,
        items=[
            InvoiceItemResponse(
                id=str(item.id),
                invoice_id=str(item.invoice_id),
                study_type_id=str(item.study_type_id) if item.study_type_id else None,
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
                amount=float(p.amount),
                currency=p.currency,
                method=p.method,
                reference=p.reference,
                invoice_id=str(p.invoice_id),
                tenant_id=str(p.tenant_id),
                received_at=p.received_at,
                created_by=str(p.created_by) if p.created_by else None,
            )
            for p in payments
        ],
        balance=balance,
    )
