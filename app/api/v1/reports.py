from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlmodel import select, Session
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext
from app.models.report import Report, ReportVersion
from app.models.laboratory import LabOrder
from app.models.tenant import Tenant, Branch
from app.models.patient import Patient
from app.models.storage import StorageObject
from app.services.s3 import S3Service
from app.schemas.report import (
    ReportCreate, 
    ReportResponse, 
    ReportDetailResponse, 
    ReportVersionCreate, 
    ReportVersionResponse,
    ReportsListResponse,
    ReportListItem,
    BranchRef,
    OrderRef,
    PatientRef
)
import json

router = APIRouter(prefix="/reports")

@router.get("/", response_model=ReportsListResponse)
def list_reports(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """List all reports with enriched branch, order, and patient info"""
    reports = session.exec(select(Report).where(Report.tenant_id == ctx.tenant_id)).all()
    results: list[ReportListItem] = []
    
    for r in reports:
        # Resolve related entities
        branch = session.get(Branch, r.branch_id)
        order = session.get(LabOrder, r.order_id)
        patient = session.get(Patient, order.patient_id) if order else None
        
        # Get current version info
        current_version = session.exec(
            select(ReportVersion).where(
                ReportVersion.report_id == r.id, 
                ReportVersion.is_current == True
            )
        ).first()
        
        version_no = current_version.version_no if current_version else None
        has_pdf = bool(current_version and current_version.pdf_storage_id)
        
        results.append(
            ReportListItem(
                id=str(r.id),
                status=r.status,
                tenant_id=str(r.tenant_id),
                branch=BranchRef(
                    id=str(r.branch_id),
                    name=branch.name if branch else "",
                    code=branch.code if branch else None
                ),
                order=OrderRef(
                    id=str(r.order_id),
                    order_code=order.order_code if order else "",
                    status=order.status if order else "",
                    requested_by=order.requested_by if order else None,
                    patient=PatientRef(
                        id=str(patient.id) if patient else "",
                        full_name=f"{patient.first_name} {patient.last_name}" if patient else "",
                        patient_code=patient.patient_code if patient else "",
                    ) if patient else None
                ),
                title=r.title,
                diagnosis_text=r.diagnosis_text,
                published_at=r.published_at,
                created_at=str(getattr(r, "created_at", "")) if getattr(r, "created_at", None) else None,
                created_by=str(r.created_by) if r.created_by else None,
                version_no=version_no,
                has_pdf=has_pdf
            )
        )
    
    return ReportsListResponse(reports=results)

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
def get_report(
    report_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Get report details"""
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    if str(report.tenant_id) != ctx.tenant_id:
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

@router.get("/{report_id}/versions")
def list_report_versions(
    report_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """List all versions for a report"""
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Report not found")
    versions = session.exec(select(ReportVersion).where(ReportVersion.report_id == report.id)).all()
    return [{
        "id": str(v.id),
        "version_no": v.version_no,
        "report_id": str(v.report_id),
        "is_current": v.is_current
    } for v in versions]

@router.get("/{report_id}/pdf")
def get_pdf_of_latest_version(
    report_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Return a presigned URL to download the PDF for the newest report version."""
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Report not found")

    latest_version = session.exec(
        select(ReportVersion)
        .where(ReportVersion.report_id == report.id)
        .order_by(ReportVersion.version_no.desc())
    ).first()
    if not latest_version:
        raise HTTPException(404, "No versions found for this report")

    if not latest_version.pdf_storage_id:
        raise HTTPException(404, "PDF not found for the latest version")

    storage = session.get(StorageObject, latest_version.pdf_storage_id)
    if not storage:
        raise HTTPException(404, "Storage object not found")

    s3 = S3Service()
    url = s3.generate_presigned_url(storage.object_key)
    return {
        "version_id": str(latest_version.id),
        "version_no": latest_version.version_no,
        "report_id": str(latest_version.report_id),
        "pdf_storage_id": str(storage.id),
        "pdf_key": storage.object_key,
        "pdf_url": url,
    }

@router.get("/{report_id}/{version_no}", response_model=ReportDetailResponse)
def get_report_version(
    report_id: str,
    version_no: int,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Get specific report version details (same shape as current detail)."""
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Report not found")

    version = session.exec(
        select(ReportVersion).where(
            ReportVersion.report_id == report.id,
            ReportVersion.version_no == version_no,
        )
    ).first()
    if not version:
        raise HTTPException(404, "Report version not found")

    report_json = None
    if version.json_storage_id:
        storage = session.get(StorageObject, version.json_storage_id)
        if storage:
            s3 = S3Service()
            try:
                text = s3.download_text(storage.object_key)
                report_json = json.loads(text)
            except Exception:
                report_json = None

    return ReportDetailResponse(
        id=str(report.id),
        version_no=version.version_no,
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


@router.post("/{report_id}/versions/{version_no}/pdf")
def upload_pdf_to_specific_version(
    report_id: str,
    version_no: int,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    """Upload a PDF to a specific report version.

    - Validates report and version exist
    - Uploads PDF to S3 under a deterministic key
    - Creates a StorageObject and links it to ReportVersion.pdf_storage_id
    """
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")

    version = session.exec(
        select(ReportVersion).where(
            ReportVersion.report_id == report.id,
            ReportVersion.version_no == version_no,
        )
    ).first()
    if not version:
        raise HTTPException(404, "Report version not found")

    # Basic content-type validation
    content_type = (file.content_type or "").lower()
    if "pdf" not in content_type:
        raise HTTPException(400, "Uploaded file must be a PDF")

    file_bytes = file.file.read()
    if not file_bytes:
        raise HTTPException(400, "Uploaded file is empty")

    s3 = S3Service()
    key = (
        f"reports/{report.tenant_id}/{report.branch_id}/{report.id}/"
        f"versions/{version.version_no}/report.pdf"
    )
    info = s3.upload_bytes(file_bytes, key=key, content_type="application/pdf")

    storage = StorageObject(
        provider="aws",
        region=s3.region,
        bucket=info.bucket,
        object_key=info.key,
        version_id=info.version_id,
        etag=info.etag,
        content_type="application/pdf",
        size_bytes=info.size_bytes,
        created_by=report.created_by,
    )
    session.add(storage)
    session.flush()

    version.pdf_storage_id = storage.id
    session.add(version)
    session.commit()
    session.refresh(version)

    return {
        "version_id": str(version.id),
        "version_no": version.version_no,
        "report_id": str(version.report_id),
        "pdf_storage_id": str(storage.id),
        "pdf_key": info.key,
        "pdf_url": s3.object_public_url(info.key),
    }


@router.post("/{report_id}/pdf")
def upload_pdf_to_latest_version(
    report_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    """Upload a PDF to the newest version of a report.

    - Selects the version with the highest version_no
    - If the report has no versions, returns 404
    - Uploads PDF, creates StorageObject, and updates pdf_storage_id
    """
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")

    latest_version = session.exec(
        select(ReportVersion)
        .where(ReportVersion.report_id == report.id)
        .order_by(ReportVersion.version_no.desc())
    ).first()
    if not latest_version:
        raise HTTPException(404, "No versions found for this report")

    content_type = (file.content_type or "").lower()
    if "pdf" not in content_type:
        raise HTTPException(400, "Uploaded file must be a PDF")

    file_bytes = file.file.read()
    if not file_bytes:
        raise HTTPException(400, "Uploaded file is empty")

    s3 = S3Service()
    key = (
        f"reports/{report.tenant_id}/{report.branch_id}/{report.id}/"
        f"versions/{latest_version.version_no}/report.pdf"
    )
    info = s3.upload_bytes(file_bytes, key=key, content_type="application/pdf")

    storage = StorageObject(
        provider="aws",
        region=s3.region,
        bucket=info.bucket,
        object_key=info.key,
        version_id=info.version_id,
        etag=info.etag,
        content_type="application/pdf",
        size_bytes=info.size_bytes,
        created_by=report.created_by,
    )
    session.add(storage)
    session.flush()

    latest_version.pdf_storage_id = storage.id
    session.add(latest_version)
    session.commit()
    session.refresh(latest_version)

    return {
        "version_id": str(latest_version.id),
        "version_no": latest_version.version_no,
        "report_id": str(latest_version.report_id),
        "pdf_storage_id": str(storage.id),
        "pdf_key": info.key,
        "pdf_url": s3.object_public_url(info.key),
    }


@router.get("/{report_id}/versions/{version_no}/pdf")
def get_pdf_of_specific_version(report_id: str, version_no: int, session: Session = Depends(get_session)):
    """Return a presigned URL to download the PDF for a specific report version."""
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")

    version = session.exec(
        select(ReportVersion).where(
            ReportVersion.report_id == report.id,
            ReportVersion.version_no == version_no,
        )
    ).first()
    if not version:
        raise HTTPException(404, "Report version not found")

    if not version.pdf_storage_id:
        raise HTTPException(404, "PDF not found for this version")

    storage = session.get(StorageObject, version.pdf_storage_id)
    if not storage:
        raise HTTPException(404, "Storage object not found")

    s3 = S3Service()
    url = s3.generate_presigned_url(storage.object_key)
    return {
        "version_id": str(version.id),
        "version_no": version.version_no,
        "report_id": str(version.report_id),
        "pdf_storage_id": str(storage.id),
        "pdf_key": storage.object_key,
        "pdf_url": url,
    }


@router.get("/{report_id}/pdf")
def get_pdf_of_latest_version(report_id: str, session: Session = Depends(get_session)):
    """Return a presigned URL to download the PDF for the newest report version."""
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")

    latest_version = session.exec(
        select(ReportVersion)
        .where(ReportVersion.report_id == report.id)
        .order_by(ReportVersion.version_no.desc())
    ).first()
    if not latest_version:
        raise HTTPException(404, "No versions found for this report")

    if not latest_version.pdf_storage_id:
        raise HTTPException(404, "PDF not found for the latest version")

    storage = session.get(StorageObject, latest_version.pdf_storage_id)
    if not storage:
        raise HTTPException(404, "Storage object not found")

    s3 = S3Service()
    url = s3.generate_presigned_url(storage.object_key)
    return {
        "version_id": str(latest_version.id),
        "version_no": latest_version.version_no,
        "report_id": str(latest_version.report_id),
        "pdf_storage_id": str(storage.id),
        "pdf_key": storage.object_key,
        "pdf_url": url,
    }
