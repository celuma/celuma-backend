from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, Session
from app.core.db import get_session
from app.models.laboratory import LabOrder, Sample
from app.models.tenant import Tenant, Branch
from app.models.patient import Patient
from app.models.user import AppUser

router = APIRouter(prefix="/laboratory")

@router.get("/orders/")
def list_orders(session: Session = Depends(get_session)):
    """List all laboratory orders"""
    orders = session.exec(select(LabOrder)).all()
    return [{
        "id": str(o.id),
        "order_code": o.order_code,
        "status": o.status,
        "patient_id": str(o.patient_id),
        "tenant_id": str(o.tenant_id),
        "branch_id": str(o.branch_id)
    } for o in orders]

@router.post("/orders/")
def create_order(
    tenant_id: str,
    branch_id: str,
    patient_id: str,
    order_code: str,
    requested_by: str = None,
    notes: str = None,
    created_by: str = None,
    session: Session = Depends(get_session)
):
    """Create a new laboratory order"""
    # Verify tenant, branch, and patient exist
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    branch = session.get(Branch, branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    
    patient = session.get(Patient, patient_id)
    if not patient:
        raise HTTPException(404, "Patient not found")
    
    # Check if order_code is unique for this branch
    existing_order = session.exec(
        select(LabOrder).where(
            LabOrder.order_code == order_code,
            LabOrder.branch_id == branch_id
        )
    ).first()
    
    if existing_order:
        raise HTTPException(400, "Order code already exists for this branch")
    
    order = LabOrder(
        tenant_id=tenant_id,
        branch_id=branch_id,
        patient_id=patient_id,
        order_code=order_code,
        requested_by=requested_by,
        notes=notes,
        created_by=created_by
    )
    
    session.add(order)
    session.commit()
    session.refresh(order)
    
    return {
        "id": str(order.id),
        "order_code": order.order_code,
        "status": order.status,
        "patient_id": str(order.patient_id),
        "tenant_id": str(order.tenant_id),
        "branch_id": str(order.branch_id)
    }

@router.get("/orders/{order_id}")
def get_order(order_id: str, session: Session = Depends(get_session)):
    """Get order details"""
    order = session.get(LabOrder, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    
    return {
        "id": str(order.id),
        "order_code": order.order_code,
        "status": order.status,
        "patient_id": str(order.patient_id),
        "tenant_id": str(order.tenant_id),
        "branch_id": str(order.branch_id),
        "requested_by": order.requested_by,
        "notes": order.notes,
        "billed_lock": order.billed_lock
    }

@router.get("/samples/")
def list_samples(session: Session = Depends(get_session)):
    """List all samples"""
    samples = session.exec(select(Sample)).all()
    return [{
        "id": str(s.id),
        "sample_code": s.sample_code,
        "type": s.type,
        "state": s.state,
        "order_id": str(s.order_id),
        "tenant_id": str(s.tenant_id),
        "branch_id": str(s.branch_id)
    } for s in samples]

@router.post("/samples/")
def create_sample(
    tenant_id: str,
    branch_id: str,
    order_id: str,
    sample_code: str,
    type: str,
    notes: str = None,
    session: Session = Depends(get_session)
):
    """Create a new sample"""
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
    
    # Check if sample_code is unique for this order
    existing_sample = session.exec(
        select(Sample).where(
            Sample.sample_code == sample_code,
            Sample.order_id == order_id
        )
    ).first()
    
    if existing_sample:
        raise HTTPException(400, "Sample code already exists for this order")
    
    sample = Sample(
        tenant_id=tenant_id,
        branch_id=branch_id,
        order_id=order_id,
        sample_code=sample_code,
        type=type,
        notes=notes
    )
    
    session.add(sample)
    session.commit()
    session.refresh(sample)
    
    return {
        "id": str(sample.id),
        "sample_code": sample.sample_code,
        "type": sample.type,
        "state": sample.state,
        "order_id": str(sample.order_id),
        "tenant_id": str(sample.tenant_id),
        "branch_id": str(sample.branch_id)
    }
