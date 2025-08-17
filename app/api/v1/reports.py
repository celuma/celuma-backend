from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, Session
from app.core.db import get_session
from app.models.report import Report, ReportVersion
from app.models.laboratory import LabOrder
from app.models.tenant import Tenant, Branch

router = APIRouter(prefix="/reports")

@router.get("/")
def list_reports(session: Session = Depends(get_session)):
    """List all reports"""
    reports = session.exec(select(Report)).all()
    return [{
        "id": str(r.id),
        "status": r.status,
        "order_id": str(r.order_id),
        "tenant_id": str(r.tenant_id),
        "branch_id": str(r.branch_id)
    } for r in reports]

@router.post("/")
def create_report(
    tenant_id: str,
    branch_id: str,
    order_id: str,
    title: str = None,
    diagnosis_text: str = None,
    created_by: str = None,
    session: Session = Depends(get_session)
):
    """Create a new report"""
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
    
    # Check if report already exists for this order
    existing_report = session.exec(
        select(Report).where(Report.order_id == order_id)
    ).first()
    
    if existing_report:
        raise HTTPException(400, "Report already exists for this order")
    
    report = Report(
        tenant_id=tenant_id,
        branch_id=branch_id,
        order_id=order_id,
        title=title,
        diagnosis_text=diagnosis_text,
        created_by=created_by
    )
    
    session.add(report)
    session.commit()
    session.refresh(report)
    
    return {
        "id": str(report.id),
        "status": report.status,
        "order_id": str(report.order_id),
        "tenant_id": str(report.tenant_id),
        "branch_id": str(report.branch_id)
    }

@router.get("/{report_id}")
def get_report(report_id: str, session: Session = Depends(get_session)):
    """Get report details"""
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    
    return {
        "id": str(report.id),
        "status": report.status,
        "order_id": str(report.order_id),
        "tenant_id": str(report.tenant_id),
        "branch_id": str(report.branch_id),
        "title": report.title,
        "diagnosis_text": report.diagnosis_text,
        "published_at": report.published_at
    }

@router.get("/versions/")
def list_report_versions(session: Session = Depends(get_session)):
    """List all report versions"""
    versions = session.exec(select(ReportVersion)).all()
    return [{
        "id": str(v.id),
        "version_no": v.version_no,
        "report_id": str(v.report_id),
        "is_current": v.is_current
    } for v in versions]

@router.post("/versions/")
def create_report_version(
    report_id: str,
    version_no: int,
    pdf_storage_id: str,
    html_storage_id: str = None,
    changelog: str = None,
    authored_by: str = None,
    session: Session = Depends(get_session)
):
    """Create a new report version"""
    # Verify report exists
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    
    # Check if version number already exists for this report
    existing_version = session.exec(
        select(ReportVersion).where(
            ReportVersion.report_id == report_id,
            ReportVersion.version_no == version_no
        )
    ).first()
    
    if existing_version:
        raise HTTPException(400, "Version number already exists for this report")
    
    version = ReportVersion(
        report_id=report_id,
        version_no=version_no,
        pdf_storage_id=pdf_storage_id,
        html_storage_id=html_storage_id,
        changelog=changelog,
        authored_by=authored_by
    )
    
    session.add(version)
    session.commit()
    session.refresh(version)
    
    return {
        "id": str(version.id),
        "version_no": version.version_no,
        "report_id": str(version.report_id),
        "is_current": version.is_current
    }
