from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, Session
from app.core.db import get_session
from app.models.report import Report, ReportVersion
from app.models.laboratory import LabOrder
from app.models.tenant import Tenant, Branch
from app.models.storage import StorageObject
from app.services.s3 import S3Service
from app.schemas.report import ReportCreate, ReportResponse, ReportDetailResponse, ReportVersionCreate, ReportVersionResponse
import json

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

@router.post("/", response_model=ReportResponse)
def create_report(report_data: ReportCreate, session: Session = Depends(get_session)):
    """Create a new report"""
    # Verify tenant, branch, and order exist
    tenant = session.get(Tenant, report_data.tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    branch = session.get(Branch, report_data.branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    
    order = session.get(LabOrder, report_data.order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    
    # Check if report already exists for this order
    existing_report = session.exec(
        select(Report).where(Report.order_id == report_data.order_id)
    ).first()
    
    if existing_report:
        raise HTTPException(400, "Report already exists for this order")
    
    report = Report(
        tenant_id=report_data.tenant_id,
        branch_id=report_data.branch_id,
        order_id=report_data.order_id,
        title=report_data.title,
        diagnosis_text=report_data.diagnosis_text,
        created_by=report_data.created_by,
        published_at=report_data.published_at
    )
    
    session.add(report)
    session.commit()
    session.refresh(report)
    
    # If a JSON report body is provided, upload to S3 and create initial version (v1)
    if report_data.report is not None:
        s3 = S3Service()
        # Build S3 key
        key = f"reports/{report.tenant_id}/{report.branch_id}/{report.id}/versions/1/report.json"
        data_bytes = json.dumps(report_data.report, ensure_ascii=False).encode("utf-8")
        info = s3.upload_bytes(data_bytes, key=key, content_type="application/json")

        storage = StorageObject(
            provider="aws",
            region=s3.region,
            bucket=info.bucket,
            object_key=info.key,
            version_id=info.version_id,
            etag=info.etag,
            content_type="application/json",
            size_bytes=info.size_bytes,
            created_by=report.created_by,
        )
        session.add(storage)
        session.flush()

        # Mark existing versions as not current (none expected on create)
        # Create version 1 as current
        version = ReportVersion(
            report_id=report.id,
            version_no=1,
            json_storage_id=storage.id,
            pdf_storage_id=None,
            html_storage_id=None,
            authored_by=report.created_by,
            is_current=True,
        )
        session.add(version)
        session.commit()
        session.refresh(version)

    return ReportResponse(
        id=str(report.id),
        status=report.status,
        order_id=str(report.order_id),
        tenant_id=str(report.tenant_id),
        branch_id=str(report.branch_id)
    )


@router.post("/{report_id}/new_version", response_model=ReportVersionResponse)
def create_report_new_version(report_id: str, report_data: ReportCreate, session: Session = Depends(get_session)):
    """Create a new report version for an existing report.

    - Increments version_no based on current version
    - Marks old current version as not current
    - Uploads provided JSON body to S3 and links it
    """
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")

    # Determine next version number
    current_version = session.exec(
        select(ReportVersion).where(ReportVersion.report_id == report.id, ReportVersion.is_current == True)
    ).first()
    next_version_no = (current_version.version_no + 1) if current_version else 1

    json_storage_id = None
    if report_data.report is not None:
        s3 = S3Service()
        key = f"reports/{report.tenant_id}/{report.branch_id}/{report.id}/versions/{next_version_no}/report.json"
        data_bytes = json.dumps(report_data.report, ensure_ascii=False).encode("utf-8")
        info = s3.upload_bytes(data_bytes, key=key, content_type="application/json")

        storage = StorageObject(
            provider="aws",
            region=s3.region,
            bucket=info.bucket,
            object_key=info.key,
            version_id=info.version_id,
            etag=info.etag,
            content_type="application/json",
            size_bytes=info.size_bytes,
            created_by=report_data.created_by,
        )
        session.add(storage)
        session.flush()
        json_storage_id = storage.id

    # Mark previous current version as not current
    if current_version:
        current_version.is_current = False
        session.add(current_version)

    # Create new version and set is_current
    new_version = ReportVersion(
        report_id=report.id,
        version_no=next_version_no,
        json_storage_id=json_storage_id,
        pdf_storage_id=None,
        html_storage_id=None,
        authored_by=report_data.created_by,
        is_current=True,
    )
    session.add(new_version)
    session.commit()
    session.refresh(new_version)

    return ReportVersionResponse(
        id=str(new_version.id),
        version_no=new_version.version_no,
        report_id=str(new_version.report_id),
        is_current=new_version.is_current,
    )

@router.get("/{report_id}", response_model=ReportDetailResponse)
def get_report(report_id: str, session: Session = Depends(get_session)):
    """Get report details"""
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    
    # Load current version JSON, if any
    current_version = session.exec(
        select(ReportVersion).where(ReportVersion.report_id == report.id, ReportVersion.is_current == True)
    ).first()

    report_json = None
    if current_version and current_version.json_storage_id:
        storage = session.get(StorageObject, current_version.json_storage_id)
        if storage:
            s3 = S3Service()
            try:
                text = s3.download_text(storage.object_key)
                report_json = json.loads(text)
            except Exception:
                report_json = None

    return ReportDetailResponse(
        id=str(report.id),
        version_no=(current_version.version_no if current_version else None),
        status=report.status,
        order_id=str(report.order_id),
        tenant_id=str(report.tenant_id),
        branch_id=str(report.branch_id),
        title=report.title,
        diagnosis_text=report.diagnosis_text,
        published_at=report.published_at,
        created_by=(str(report.created_by) if report.created_by else None),
        report=report_json,
    )

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

@router.post("/versions/", response_model=ReportVersionResponse)
def create_report_version(version_data: ReportVersionCreate, session: Session = Depends(get_session)):
    """Create a new report version"""
    # Verify report exists
    report = session.get(Report, version_data.report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    
    # Check if version number already exists for this report
    existing_version = session.exec(
        select(ReportVersion).where(
            ReportVersion.report_id == version_data.report_id,
            ReportVersion.version_no == version_data.version_no
        )
    ).first()
    
    if existing_version:
        raise HTTPException(400, "Version number already exists for this report")
    
    version = ReportVersion(
        report_id=version_data.report_id,
        version_no=version_data.version_no,
        pdf_storage_id=version_data.pdf_storage_id,
        html_storage_id=version_data.html_storage_id,
        changelog=version_data.changelog,
        authored_by=version_data.authored_by,
        authored_at=version_data.authored_at
    )
    
    session.add(version)
    session.commit()
    session.refresh(version)
    
    return ReportVersionResponse(
        id=str(version.id),
        version_no=version.version_no,
        report_id=str(version.report_id),
        is_current=version.is_current
    )
