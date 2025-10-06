from fastapi import APIRouter, Depends
from sqlmodel import select, Session, func
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext
from app.models.laboratory import LabOrder, Sample
from app.models.patient import Patient
from app.models.report import Report
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

router = APIRouter(prefix="/dashboard")

class DashboardStats(BaseModel):
    total_patients: int
    total_orders: int
    total_samples: int
    total_reports: int
    pending_orders: int
    draft_reports: int
    published_reports: int

class RecentActivityItem(BaseModel):
    id: str
    title: str
    description: str
    timestamp: datetime
    type: str  # "order", "report", "sample"
    status: Optional[str] = None

class DashboardResponse(BaseModel):
    stats: DashboardStats
    recent_activity: List[RecentActivityItem]

@router.get("/", response_model=DashboardResponse)
def get_dashboard_data(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Get dashboard statistics and recent activity for the current tenant"""
    
    # Get basic counts
    total_patients = session.exec(
        select(func.count(Patient.id)).where(Patient.tenant_id == ctx.tenant_id)
    ).one()
    
    total_orders = session.exec(
        select(func.count(LabOrder.id)).where(LabOrder.tenant_id == ctx.tenant_id)
    ).one()
    
    total_samples = session.exec(
        select(func.count(Sample.id)).where(Sample.tenant_id == ctx.tenant_id)
    ).one()
    
    total_reports = session.exec(
        select(func.count(Report.id)).where(Report.tenant_id == ctx.tenant_id)
    ).one()
    
    # Get status-specific counts
    pending_orders = session.exec(
        select(func.count(LabOrder.id)).where(
            LabOrder.tenant_id == ctx.tenant_id,
            LabOrder.status.in_(["RECEIVED", "PROCESSING"])
        )
    ).one()
    
    draft_reports = session.exec(
        select(func.count(Report.id)).where(
            Report.tenant_id == ctx.tenant_id,
            Report.status == "DRAFT"
        )
    ).one()
    
    published_reports = session.exec(
        select(func.count(Report.id)).where(
            Report.tenant_id == ctx.tenant_id,
            Report.status == "PUBLISHED"
        )
    ).one()
    
    # Build stats object
    stats = DashboardStats(
        total_patients=total_patients,
        total_orders=total_orders,
        total_samples=total_samples,
        total_reports=total_reports,
        pending_orders=pending_orders,
        draft_reports=draft_reports,
        published_reports=published_reports,
    )
    
    # Get recent activity
    recent_activity = []
    
    # Recent orders (last 3)
    recent_orders = session.exec(
        select(LabOrder).where(LabOrder.tenant_id == ctx.tenant_id)
        .order_by(LabOrder.created_at.desc())
        .limit(3)
    ).all()
    
    for order in recent_orders:
        patient = session.get(Patient, order.patient_id)
        patient_name = f"{patient.first_name} {patient.last_name}" if patient else "Paciente desconocido"
        
        recent_activity.append(RecentActivityItem(
            id=str(order.id),
            title=f"Orden {order.order_code}",
            description=f"Paciente: {patient_name}{f' • Solicitado por: {order.requested_by}' if order.requested_by else ''}",
            timestamp=order.created_at,
            type="order",
            status=order.status,
        ))
    
    # Recent reports (last 2)
    recent_reports = session.exec(
        select(Report).where(Report.tenant_id == ctx.tenant_id)
        .order_by(Report.created_at.desc())
        .limit(2)
    ).all()
    
    for report in recent_reports:
        order = session.get(LabOrder, report.order_id)
        if order:
            patient = session.get(Patient, order.patient_id)
            patient_name = f"{patient.first_name} {patient.last_name}" if patient else "Paciente desconocido"
            
            recent_activity.append(RecentActivityItem(
                id=str(report.id),
                title=report.title,
                description=f"Orden: {order.order_code} • Paciente: {patient_name}",
                timestamp=report.created_at,
                type="report",
                status=report.status,
            ))
    
    # Recent samples (last 2)
    recent_samples = session.exec(
        select(Sample).where(Sample.tenant_id == ctx.tenant_id)
        .order_by(Sample.received_at.desc().nullslast(), Sample.collected_at.desc().nullslast())
        .limit(2)
    ).all()
    
    for sample in recent_samples:
        order = session.get(LabOrder, sample.order_id)
        if order:
            patient = session.get(Patient, order.patient_id)
            patient_name = f"{patient.first_name} {patient.last_name}" if patient else "Paciente desconocido"
            
            # Use received_at if available, otherwise collected_at, otherwise order created_at
            sample_timestamp = sample.received_at or sample.collected_at or order.created_at
            
            recent_activity.append(RecentActivityItem(
                id=str(sample.id),
                title=f"Muestra {sample.sample_code}",
                description=f"Orden: {order.order_code} • Paciente: {patient_name}",
                timestamp=sample_timestamp,
                type="sample",
                status=sample.state,
            ))
    
    # Sort by timestamp and limit to 8 items
    recent_activity.sort(key=lambda x: x.timestamp, reverse=True)
    recent_activity = recent_activity[:8]
    
    return DashboardResponse(
        stats=stats,
        recent_activity=recent_activity,
    )
