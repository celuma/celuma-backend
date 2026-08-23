from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import select, Session
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext, current_user
from app.core.rbac import has_permission
from app.models.laboratory import Order
from app.models.patient import Patient
from app.models.report import Report, ReportVersion
from app.models.storage import StorageObject
from app.models.user import AppUser
from app.models.enums import ReportStatus
from app.services.s3 import S3Service
from app.api.v1.reports import official_pdf_presigned_url
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import hashlib
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/portal")


# Physician Portal Endpoints

class PhysicianOrderSummary(BaseModel):
    """Summary of order for physician portal"""
    id: str
    order_code: str
    patient_name: str
    patient_code: str
    status: str
    has_report: bool
    report_status: Optional[str] = None
    requested_by: Optional[str] = None


@router.get("/physician/orders", response_model=List[PhysicianOrderSummary])
def list_physician_orders(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """List orders for the authenticated physician (requires portal:physician_access)."""
    if not has_permission(user.id, "portal:physician_access", session):
        raise HTTPException(403, "Permission required: portal:physician_access")
    # Get orders where user's email matches requested_by
    orders = session.exec(
        select(Order).where(
            Order.tenant_id == ctx.tenant_id,
            Order.requested_by == user.email
        )
    ).all()
    
    results = []
    for order in orders:
        patient = session.get(Patient, order.patient_id)
        patient_name = f"{patient.first_name} {patient.last_name}" if patient else "Unknown"
        patient_code = patient.patient_code if patient else ""
        
        # Check if report exists
        report = session.get(Report, order.report_id) if order.report_id else None
        
        results.append(
            PhysicianOrderSummary(
                id=str(order.id),
                order_code=order.order_code,
                patient_name=patient_name,
                patient_code=patient_code,
                status=order.status,
                has_report=bool(report),
                report_status=report.status if report else None,
                requested_by=order.requested_by,
            )
        )
    
    return results


@router.get("/physician/orders/{order_id}/report")
def get_physician_report(
    order_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Get report for a specific order (requires portal:physician_access)."""
    if not has_permission(user.id, "portal:physician_access", session):
        raise HTTPException(403, "Permission required: portal:physician_access")
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(404, "Order not found")

    # Céluma 1.3 Phase 5, Block D (D-001): anchor the path-addressed order to
    # the caller's own tenant before any other check. `requested_by` is a free
    # text email column with no cross-tenant uniqueness, so the ownership test
    # below is not a tenant boundary on its own: a physician whose address is
    # recorded as the requesting physician in two tenants would otherwise read
    # the other tenant's published report and its presigned PDF URL.
    #
    # 404 rather than 403, matching `list_physician_orders`' tenant filter and
    # the convention every other path-addressed route in this codebase uses
    # (and that C-001 restored in `laboratory.py`): a foreign resource hides
    # its existence rather than confirming it.
    if str(order.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Order not found")

    # Verify this physician requested this order
    if order.requested_by != user.email:
        raise HTTPException(403, "You are not authorized to view this report")
    
    # Check if order is locked
    if order.billed_lock:
        raise HTTPException(403, "Report access blocked due to pending payment")
    
    # Get report
    report = session.get(Report, order.report_id) if order.report_id else None
    
    if not report:
        raise HTTPException(404, "Report not found for this order")
    
    # Only show published reports
    if report.status != ReportStatus.PUBLISHED:
        raise HTTPException(403, "Report is not yet available")
    
    # Get latest version with PDF
    latest_version = session.exec(
        select(ReportVersion)
        .where(ReportVersion.report_id == report.id)
        .order_by(ReportVersion.version_no.desc())
    ).first()
    
    if not latest_version or not latest_version.pdf_storage_id:
        raise HTTPException(404, "PDF not available for this report")
    
    storage = session.get(StorageObject, latest_version.pdf_storage_id)
    if not storage:
        raise HTTPException(404, "Storage object not found")
    
    # Generate presigned URL (short expiration). Céluma 1.3 Phase 2, Block E:
    # was previously calling generate_presigned_url(..., expiration=600) —
    # `expiration` is not a real parameter of that method (it's `expires_in`),
    # so this call would have raised a TypeError at runtime the first time a
    # physician actually tried to download a PDF. Fixed here as part of
    # connecting the portal to the persisted-PDF download flow (E11).
    s3 = S3Service()
    url = official_pdf_presigned_url(s3, storage.object_key, order.order_code, latest_version.version_no)

    return {
        "report_id": str(report.id),
        "order_code": order.order_code,
        "status": report.status,
        "title": report.title,
        "published_at": report.published_at,
        "pdf_url": url,
    }


# Patient Portal Endpoints

def generate_patient_access_code(order_code: str, patient_code: str) -> str:
    """Generate unique access code for patient"""
    combined = f"{order_code}:{patient_code}"
    return hashlib.sha256(combined.encode()).hexdigest()[:16].upper()


@router.get("/patient/report")
def get_patient_report(
    code: str = Query(..., description="Patient access code"),
    session: Session = Depends(get_session),
):
    """Get report by patient access code (public endpoint, no auth required)"""
    # Try to find matching order by attempting to match code
    # This is a simplified version - in production you'd want a dedicated access_code field
    
    orders = session.exec(select(Order)).all()
    
    matched_order = None
    for order in orders:
        patient = session.get(Patient, order.patient_id)
        if patient:
            expected_code = generate_patient_access_code(order.order_code, patient.patient_code)
            if expected_code == code.upper():
                matched_order = order
                break
    
    if not matched_order:
        raise HTTPException(404, "Report not found with this access code")
    
    # Check if order is locked
    if matched_order.billed_lock:
        raise HTTPException(403, "Report access blocked due to pending payment")
    
    # Get report
    report = session.get(Report, matched_order.report_id) if matched_order.report_id else None
    
    if not report:
        raise HTTPException(404, "Report not available for this case")
    
    # Only show published reports
    if report.status != ReportStatus.PUBLISHED:
        raise HTTPException(403, "Report is not yet available")
    
    # Get latest version with PDF
    latest_version = session.exec(
        select(ReportVersion)
        .where(ReportVersion.report_id == report.id)
        .order_by(ReportVersion.version_no.desc())
    ).first()
    
    if not latest_version or not latest_version.pdf_storage_id:
        raise HTTPException(404, "PDF not available for this report")
    
    storage = session.get(StorageObject, latest_version.pdf_storage_id)
    if not storage:
        raise HTTPException(404, "Storage object not found")
    
    # Generate presigned URL (short expiration). Same `expiration` ->
    # `expires_in` fix as get_physician_report above.
    s3 = S3Service()
    url = official_pdf_presigned_url(
        s3, storage.object_key, matched_order.order_code, latest_version.version_no
    )

    patient = session.get(Patient, matched_order.patient_id)
    patient_name = f"{patient.first_name} {patient.last_name}" if patient else "Unknown"
    
    return {
        "report_id": str(report.id),
        "order_code": matched_order.order_code,
        "patient_name": patient_name,
        "status": report.status,
        "title": report.title,
        "published_at": report.published_at,
        "pdf_url": url,
    }

