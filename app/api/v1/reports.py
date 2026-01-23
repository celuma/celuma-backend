from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlmodel import select, Session, and_
from sqlalchemy import cast, String
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext, current_user
from app.models.report import Report, ReportVersion, ReportTemplate
from app.models.laboratory import Order
from app.models.tenant import Tenant, Branch
from app.models.patient import Patient
from app.models.storage import StorageObject
from app.models.user import AppUser
from app.models.audit import AuditLog
from app.models.enums import ReportStatus, UserRole, AssignmentItemType, ReviewStatus
from app.models.assignment import Assignment
from app.models.report_review import ReportReview
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
    PatientRef,
    ReportStatusUpdate,
    ReportSignRequest,
    ReportReviewComment,
    ReportActionResponse,
    ReportTemplateCreate,
    ReportTemplateUpdate,
    ReportTemplateResponse,
    ReportTemplateDetailResponse,
    ReportTemplatesListResponse,
)
import json
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports")

# Import update_order_status to sync order status with report status
# We do this here to avoid circular imports
def update_order_status_for_report(order_id: str, session: Session) -> None:
    """Wrapper to update order status after report changes"""
    from app.api.v1.laboratory import update_order_status
    update_order_status(order_id, session)


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
        order = session.get(Order, r.order_id)
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
        signed_by = str(current_version.signed_by) if current_version and current_version.signed_by else None
        signed_at = current_version.signed_at if current_version else None
        
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
                signed_by=signed_by,
                signed_at=signed_at,
                version_no=version_no,
                has_pdf=has_pdf
            )
        )
    
    return ReportsListResponse(reports=results)

@router.post("/", response_model=ReportResponse)
def create_report(
    report_data: ReportCreate, 
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx)
):
    """Create a new report"""
    # Verify that the report's tenant_id matches the authenticated user's tenant_id
    if report_data.tenant_id != ctx.tenant_id:
        raise HTTPException(403, "Cannot create reports for a different tenant")
    
    # Verify tenant, branch, and order exist
    tenant = session.get(Tenant, report_data.tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    branch = session.get(Branch, report_data.branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    
    # Verify branch belongs to the tenant
    if str(branch.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Branch does not belong to your tenant")
    
    order = session.get(Order, report_data.order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    
    # Verify order belongs to the tenant
    if str(order.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Order does not belong to your tenant")
    
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
    session.flush()
    
    # Initialize report_id in existing report_review records for this order
    from app.models.report_review import ReportReview
    existing_reviews = session.exec(
        select(ReportReview).where(
            and_(
                ReportReview.order_id == report.order_id,
                ReportReview.report_id.is_(None),
            )
        )
    ).all()
    
    for review in existing_reviews:
        review.report_id = report.id
        session.add(review)
    
    # Create timeline event for report creation
    from app.models.events import OrderEvent
    from app.models.enums import EventType
    
    # Get creator info for event
    creator = session.get(AppUser, report.created_by)
    
    creation_event = OrderEvent(
        tenant_id=report.tenant_id,
        branch_id=report.branch_id,
        order_id=report.order_id,
        event_type=EventType.REPORT_CREATED,
        description="",  # Not used - message built in UI
        event_metadata={
            "report_id": str(report.id),
            "report_title": report.title,
            "created_by_name": creator.full_name or creator.username if creator else None,
        },
        created_by=report.created_by,
    )
    session.add(creation_event)
    
    # Update order status based on report creation (PROCESSING -> DIAGNOSIS)
    update_order_status_for_report(str(report.order_id), session)
    
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
def create_report_new_version(
    report_id: str, 
    report_data: ReportCreate, 
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx)
):
    """Create a new report version for an existing report.

    - Increments version_no based on current version
    - Marks old current version as not current
    - Uploads provided JSON body to S3 and links it
    """
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    
    # Verify report belongs to the authenticated user's tenant
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report does not belong to your tenant")

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
    
    # Reset all review statuses to pending when new version is created
    reviews = session.exec(
        select(ReportReview).where(ReportReview.order_id == report.order_id)
    ).all()
    
    reviewer_count = 0
    for review in reviews:
        review.status = ReviewStatus.PENDING
        review.decision_at = None
        session.add(review)
        reviewer_count += 1
    
    # Create timeline event for new version
    from app.models.events import OrderEvent
    from app.models.enums import EventType
    
    version_event = OrderEvent(
        tenant_id=report.tenant_id,
        branch_id=report.branch_id,
        order_id=report.order_id,
        event_type=EventType.REPORT_VERSION_CREATED,
        description="",  # Not used - message built in UI
        event_metadata={
            "report_id": str(report.id),
            "report_title": report.title,
            "version_no": next_version_no,
            "reviews_reset": reviewer_count,
        },
        created_by=report_data.created_by,
    )
    session.add(version_event)
    
    session.commit()
    session.refresh(new_version)

    return ReportVersionResponse(
        id=str(new_version.id),
        version_no=new_version.version_no,
        report_id=str(new_version.report_id),
        is_current=new_version.is_current,
    )

@router.get("/worklist", response_model=ReportsListResponse)
def get_pathologist_worklist(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
    branch_id: str = None,
):
    """Get worklist of reports in review for pathologist"""
    # This endpoint is primarily for pathologists, but we allow all users to see what's in review
    # in case they need to check status
    
    # Build query for reports in IN_REVIEW status
    query = select(Report).where(
        Report.tenant_id == ctx.tenant_id,
        Report.status == ReportStatus.IN_REVIEW
    )
    
    # Optional branch filter
    if branch_id:
        query = query.where(Report.branch_id == branch_id)
    
    reports = session.exec(query).all()
    results: list[ReportListItem] = []
    
    for r in reports:
        # Resolve related entities
        branch = session.get(Branch, r.branch_id)
        order = session.get(Order, r.order_id)
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
        signed_by = str(current_version.signed_by) if current_version and current_version.signed_by else None
        signed_at = current_version.signed_at if current_version else None
        
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
                signed_by=signed_by,
                signed_at=signed_at,
                version_no=version_no,
                has_pdf=has_pdf
            )
        )
    
    return ReportsListResponse(reports=results)


# ============================================================================
# Report Templates CRUD Endpoints
# ============================================================================

@router.get("/templates/", response_model=ReportTemplatesListResponse)
def list_templates(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    active_only: bool = True,
):
    """List all report templates for the tenant"""
    query = select(ReportTemplate).where(ReportTemplate.tenant_id == ctx.tenant_id)
    
    if active_only:
        query = query.where(ReportTemplate.is_active == True)
    
    templates = session.exec(query).all()
    
    return ReportTemplatesListResponse(
        templates=[
            ReportTemplateResponse(
                id=str(t.id),
                tenant_id=str(t.tenant_id),
                name=t.name,
                description=t.description,
                is_active=t.is_active,
                created_at=t.created_at,
            )
            for t in templates
        ]
    )


@router.get("/templates/{template_id}", response_model=ReportTemplateDetailResponse)
def get_template(
    template_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Get a specific report template by ID"""
    template = session.get(ReportTemplate, template_id)
    if not template:
        raise HTTPException(404, "Template not found")
    
    if str(template.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Template not found")
    
    return ReportTemplateDetailResponse(
        id=str(template.id),
        tenant_id=str(template.tenant_id),
        name=template.name,
        description=template.description,
        template_json=template.template_json,
        created_by=str(template.created_by) if template.created_by else None,
        is_active=template.is_active,
        created_at=template.created_at,
    )


@router.post("/templates/", response_model=ReportTemplateResponse)
def create_template(
    template_data: ReportTemplateCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new report template"""
    template = ReportTemplate(
        tenant_id=ctx.tenant_id,
        name=template_data.name,
        description=template_data.description,
        template_json=template_data.template_json,
        created_by=user.id,
        is_active=True,
    )
    
    session.add(template)
    session.commit()
    session.refresh(template)
    
    logger.info(
        f"Report template '{template.name}' created",
        extra={
            "event": "report_template.created",
            "template_id": str(template.id),
            "user_id": str(user.id),
        },
    )
    
    return ReportTemplateResponse(
        id=str(template.id),
        tenant_id=str(template.tenant_id),
        name=template.name,
        description=template.description,
        is_active=template.is_active,
        created_at=template.created_at,
    )


@router.put("/templates/{template_id}", response_model=ReportTemplateResponse)
def update_template(
    template_id: str,
    template_data: ReportTemplateUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Update an existing report template"""
    template = session.get(ReportTemplate, template_id)
    if not template:
        raise HTTPException(404, "Template not found")
    
    if str(template.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Template does not belong to your tenant")
    
    # Update fields if provided
    if template_data.name is not None:
        template.name = template_data.name
    if template_data.description is not None:
        template.description = template_data.description
    if template_data.template_json is not None:
        template.template_json = template_data.template_json
    if template_data.is_active is not None:
        template.is_active = template_data.is_active
    
    session.add(template)
    session.commit()
    session.refresh(template)
    
    logger.info(
        f"Report template '{template.name}' updated",
        extra={
            "event": "report_template.updated",
            "template_id": str(template.id),
            "user_id": str(user.id),
        },
    )
    
    return ReportTemplateResponse(
        id=str(template.id),
        tenant_id=str(template.tenant_id),
        name=template.name,
        description=template.description,
        is_active=template.is_active,
        created_at=template.created_at,
    )


@router.delete("/templates/{template_id}")
def delete_template(
    template_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
    hard_delete: bool = False,
):
    """Delete a report template (soft delete by default, hard delete optional)"""
    template = session.get(ReportTemplate, template_id)
    if not template:
        raise HTTPException(404, "Template not found")
    
    if str(template.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Template does not belong to your tenant")
    
    if hard_delete:
        # Permanently delete the template
        session.delete(template)
        session.commit()
        
        logger.info(
            f"Report template '{template.name}' permanently deleted",
            extra={
                "event": "report_template.hard_deleted",
                "template_id": template_id,
                "user_id": str(user.id),
            },
        )
        
        return {"message": "Template permanently deleted", "id": template_id}
    else:
        # Soft delete - just mark as inactive
        template.is_active = False
        session.add(template)
        session.commit()
        
        logger.info(
            f"Report template '{template.name}' soft deleted (deactivated)",
            extra={
                "event": "report_template.soft_deleted",
                "template_id": str(template.id),
                "user_id": str(user.id),
            },
        )
        
        return {"message": "Template deactivated", "id": str(template.id)}


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
        signed_by=(str(current_version.signed_by) if current_version and current_version.signed_by else None),
        signed_at=(current_version.signed_at if current_version else None),
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

    # Check if order is locked due to pending payment
    order = session.get(Order, report.order_id)
    if order and order.billed_lock:
        raise HTTPException(403, "Report access blocked due to pending payment")

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
        signed_by=(str(version.signed_by) if version.signed_by else None),
        signed_at=version.signed_at,
        report=report_json,
    )


@router.post("/{report_id}/versions/{version_no}/pdf")
def upload_pdf_to_specific_version(
    report_id: str,
    version_no: int,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx)
):
    """Upload a PDF to a specific report version.

    - Validates report and version exist
    - Uploads PDF to S3 under a deterministic key
    - Creates a StorageObject and links it to ReportVersion.pdf_storage_id
    """
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    
    # Verify report belongs to the authenticated user's tenant
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report does not belong to your tenant")

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
    ctx: AuthContext = Depends(get_auth_ctx)
):
    """Upload a PDF to the newest version of a report.

    - Selects the version with the highest version_no
    - If the report has no versions, returns 404
    - Uploads PDF, creates StorageObject, and updates pdf_storage_id
    """
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    
    # Verify report belongs to the authenticated user's tenant
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report does not belong to your tenant")

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
def get_pdf_of_specific_version(
    report_id: str, 
    version_no: int, 
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx)
):
    """Return a presigned URL to download the PDF for a specific report version."""
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    
    # Verify report belongs to the authenticated user's tenant
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report does not belong to your tenant")

    # Check if order is locked due to pending payment
    order = session.get(Order, report.order_id)
    if order and order.billed_lock:
        raise HTTPException(403, "Report access blocked due to pending payment")

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


# Helper function to create audit log
def _create_audit_log(
    session: Session,
    tenant_id: str,
    branch_id: str,
    actor_user_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    old_values: dict = None,
    new_values: dict = None,
):
    """Create an audit log entry"""
    audit = AuditLog(
        tenant_id=tenant_id,
        branch_id=branch_id,
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        old_values=old_values,
        new_values=new_values,
    )
    session.add(audit)


@router.post("/{report_id}/submit", response_model=ReportActionResponse)
def submit_report(
    report_id: str,
    data: ReportStatusUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Submit a report for review (DRAFT → IN_REVIEW)"""
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report does not belong to your tenant")
    
    if report.status != ReportStatus.DRAFT:
        raise HTTPException(400, f"Cannot submit report in {report.status} status")
    
    # Get all reviewers for this order (regardless of current status)
    reviewers = session.exec(
        select(ReportReview).where(
            and_(
                ReportReview.tenant_id == report.tenant_id,
                ReportReview.order_id == report.order_id,
            )
        )
    ).all()
    
    if not reviewers or len(reviewers) == 0:
        raise HTTPException(400, "Cannot submit report for review without reviewers assigned")
    
    # Reset all reviews to PENDING when re-submitting (allows re-review after changes)
    for reviewer in reviewers:
        reviewer.status = ReviewStatus.PENDING
        reviewer.decision_at = None
        session.add(reviewer)
    
    # Update status
    old_status = report.status
    report.status = ReportStatus.IN_REVIEW
    session.add(report)
    
    # Create audit log
    _create_audit_log(
        session=session,
        tenant_id=ctx.tenant_id,
        branch_id=str(report.branch_id),
        actor_user_id=ctx.user_id,
        action="REPORT.SUBMIT",
        entity_type="report",
        entity_id=report_id,
        old_values={"status": old_status},
        new_values={"status": report.status, "changelog": data.changelog},
    )
    
    # Create timeline event for report submission
    from app.models.events import OrderEvent
    from app.models.enums import EventType
    
    submit_event = OrderEvent(
        tenant_id=report.tenant_id,
        branch_id=report.branch_id,
        order_id=report.order_id,
        event_type=EventType.REPORT_SUBMITTED,
        description="",  # Not used - message built in UI
        event_metadata={
            "report_id": str(report.id),
            "report_title": report.title,
            "submitted_by": str(user.id),
            "submitted_by_name": user.full_name or user.username,
        },
        created_by=user.id,
    )
    session.add(submit_event)
    
    # Update order status (DIAGNOSIS -> REVIEW)
    if report.order_id:
        update_order_status_for_report(str(report.order_id), session)
    
    session.commit()
    session.refresh(report)
    
    logger.info(
        f"Report {report_id} submitted for review by user {ctx.user_id}",
        extra={
            "event": "report.submit",
            "report_id": report_id,
            "user_id": ctx.user_id,
        },
    )
    
    return ReportActionResponse(
        id=str(report.id),
        status=report.status,
        message="Report submitted for review"
    )


@router.post("/{report_id}/approve", response_model=ReportActionResponse)
def approve_report(
    report_id: str,
    data: ReportStatusUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """
    Approve a report (IN_REVIEW → APPROVED).
    
    The user must be either:
    - A pathologist (can approve any report)
    - An assigned reviewer for this report
    
    Updates the user's review record and applies MVP rule: ≥1 approved = report approved.
    """
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report does not belong to your tenant")
    
    if report.status != ReportStatus.IN_REVIEW:
        raise HTTPException(400, f"Cannot approve report in {report.status} status")
    
    # Check if user has a pending review for this report
    user_review = session.exec(
        select(ReportReview).where(
            and_(
                ReportReview.tenant_id == report.tenant_id,
                ReportReview.order_id == report.order_id,
                ReportReview.reviewer_user_id == user.id,
                ReportReview.status == ReviewStatus.PENDING,
            )
        )
    ).first()
    
    # If user has a review, update it; otherwise check if they're a pathologist or admin
    if user_review:
        user_review.status = ReviewStatus.APPROVED
        user_review.decision_at = datetime.utcnow()
        session.add(user_review)
    elif user.role not in [UserRole.PATHOLOGIST, UserRole.ADMIN]:
        raise HTTPException(403, "Only assigned reviewers, pathologists, or admins can approve reports")
    
    # Update report status (MVP rule: ≥1 approved = report approved)
    old_status = report.status
    report.status = ReportStatus.APPROVED
    session.add(report)
    
    # Create comment in conversation if there's a changelog
    if data.changelog and data.changelog.strip():
        from app.models.laboratory import OrderComment
        order_comment = OrderComment(
            tenant_id=report.tenant_id,
            branch_id=report.branch_id,
            order_id=report.order_id,
            created_by=user.id,
            text=data.changelog,
            comment_metadata={
                "source": "review_approval",
                "report_id": str(report.id),
                "review_id": str(user_review.id) if user_review else None,
            },
        )
        session.add(order_comment)
    
    # Create audit log
    _create_audit_log(
        session=session,
        tenant_id=ctx.tenant_id,
        branch_id=str(report.branch_id),
        actor_user_id=ctx.user_id,
        action="REPORT.APPROVE",
        entity_type="report",
        entity_id=report_id,
        old_values={"status": old_status},
        new_values={"status": report.status, "changelog": data.changelog},
    )
    
    # Create timeline event for report approval
    from app.models.events import OrderEvent
    from app.models.enums import EventType
    
    approve_event = OrderEvent(
        tenant_id=report.tenant_id,
        branch_id=report.branch_id,
        order_id=report.order_id,
        event_type=EventType.REPORT_APPROVED,
        description="",  # Not used - message built in UI
        event_metadata={
            "report_id": str(report.id),
            "report_title": report.title,
            "reviewer_id": str(user.id),
            "reviewer_name": user.full_name or user.username,
            "comment": data.changelog if data.changelog else None,
        },
        created_by=user.id,
    )
    session.add(approve_event)
    
    session.commit()
    session.refresh(report)
    
    logger.info(
        f"Report {report_id} approved by user {ctx.user_id}",
        extra={
            "event": "report.approve",
            "report_id": report_id,
            "user_id": ctx.user_id,
            "had_review": user_review is not None,
        },
    )
    
    return ReportActionResponse(
        id=str(report.id),
        status=report.status,
        message="Report approved"
    )


@router.post("/{report_id}/request-changes", response_model=ReportActionResponse)
def request_changes(
    report_id: str,
    data: ReportReviewComment,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """
    Request changes on a report (IN_REVIEW → DRAFT).
    
    The user must be either:
    - A pathologist (can request changes on any report)
    - An assigned reviewer for this report
    
    Updates the user's review record to REJECTED with comment.
    """
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report does not belong to your tenant")
    
    if report.status != ReportStatus.IN_REVIEW:
        raise HTTPException(400, f"Cannot request changes for report in {report.status} status")
    
    # Check if user has a pending review for this report
    user_review = session.exec(
        select(ReportReview).where(
            and_(
                ReportReview.tenant_id == report.tenant_id,
                ReportReview.order_id == report.order_id,
                ReportReview.reviewer_user_id == user.id,
                ReportReview.status == ReviewStatus.PENDING,
            )
        )
    ).first()
    
    # If user has a review, update it; otherwise check if they're a pathologist or admin
    if user_review:
        user_review.status = ReviewStatus.REJECTED
        user_review.decision_at = datetime.utcnow()
        session.add(user_review)
    elif user.role not in [UserRole.PATHOLOGIST, UserRole.ADMIN]:
        raise HTTPException(403, "Only assigned reviewers, pathologists, or admins can request changes")
    
    # Update status back to DRAFT
    old_status = report.status
    report.status = ReportStatus.DRAFT
    session.add(report)
    
    # Create comment in conversation
    if data.comment and data.comment.strip():
        from app.models.laboratory import OrderComment
        order_comment = OrderComment(
            tenant_id=report.tenant_id,
            branch_id=report.branch_id,
            order_id=report.order_id,
            created_by=user.id,
            text=data.comment,
            comment_metadata={
                "source": "review_rejection",
                "report_id": str(report.id),
                "review_id": str(user_review.id) if user_review else None,
            },
        )
        session.add(order_comment)
    
    # Create audit log
    _create_audit_log(
        session=session,
        tenant_id=ctx.tenant_id,
        branch_id=str(report.branch_id),
        actor_user_id=ctx.user_id,
        action="REPORT.REQUEST_CHANGES",
        entity_type="report",
        entity_id=report_id,
        old_values={"status": old_status},
        new_values={"status": report.status, "comment": data.comment},
    )
    
    # Create timeline event for changes requested
    from app.models.events import OrderEvent
    from app.models.enums import EventType
    
    changes_event = OrderEvent(
        tenant_id=report.tenant_id,
        branch_id=report.branch_id,
        order_id=report.order_id,
        event_type=EventType.REPORT_CHANGES_REQUESTED,
        description="",  # Not used - message built in UI
        event_metadata={
            "report_id": str(report.id),
            "report_title": report.title,
            "reviewer_id": str(user.id),
            "reviewer_name": user.full_name or user.username,
            "comment": data.comment if data.comment else None,
        },
        created_by=user.id,
    )
    session.add(changes_event)
    
    session.commit()
    session.refresh(report)
    
    logger.info(
        f"Changes requested for report {report_id} by pathologist {ctx.user_id}",
        extra={
            "event": "report.request_changes",
            "report_id": report_id,
            "user_id": ctx.user_id,
        },
    )
    
    return ReportActionResponse(
        id=str(report.id),
        status=report.status,
        message="Changes requested, report returned to draft"
    )


@router.post("/{report_id}/sign", response_model=ReportActionResponse)
def sign_report(
    report_id: str,
    data: ReportSignRequest,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Sign and publish a report (APPROVED → PUBLISHED) - Pathologist or Admin only"""
    # Check user role
    if user.role not in [UserRole.PATHOLOGIST, UserRole.ADMIN]:
        raise HTTPException(403, "Only pathologists or admins can sign reports")
    
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report does not belong to your tenant")
    
    if report.status != ReportStatus.APPROVED:
        raise HTTPException(400, f"Cannot sign report in {report.status} status. Report must be approved first.")
    
    # Get current version and sign it
    current_version = session.exec(
        select(ReportVersion).where(
            ReportVersion.report_id == report.id,
            ReportVersion.is_current == True
        )
    ).first()
    
    if not current_version:
        raise HTTPException(404, "No current version found for this report")
    
    # Update version with signature
    current_version.signed_by = user.id
    current_version.signed_at = datetime.utcnow()
    if data.changelog:
        current_version.changelog = data.changelog
    session.add(current_version)
    
    # Update report status and published_at
    old_status = report.status
    report.status = ReportStatus.PUBLISHED
    report.published_at = datetime.utcnow()
    session.add(report)
    
    # Create audit log
    _create_audit_log(
        session=session,
        tenant_id=ctx.tenant_id,
        branch_id=str(report.branch_id),
        actor_user_id=ctx.user_id,
        action="REPORT.SIGN",
        entity_type="report",
        entity_id=report_id,
        old_values={"status": old_status},
        new_values={
            "status": report.status,
            "signed_by": str(user.id),
            "signed_at": report.published_at.isoformat(),
            "changelog": data.changelog,
        },
    )
    
    # Create timeline event for report signature/publication
    from app.models.events import OrderEvent
    from app.models.enums import EventType
    
    sign_event = OrderEvent(
        tenant_id=report.tenant_id,
        branch_id=report.branch_id,
        order_id=report.order_id,
        event_type=EventType.REPORT_APPROVED,  # Using REPORT_APPROVED for signing
        description="",
        event_metadata={
            "report_id": str(report.id),
            "signer_id": str(user.id),
            "signer_name": user.full_name or user.username,
            "published": True,
            "changelog": data.changelog if data.changelog else None,
        },
        created_by=user.id,
    )
    session.add(sign_event)
    
    # Update order status based on report being published
    if report.order_id:
        update_order_status_for_report(str(report.order_id), session)
    
    session.commit()
    session.refresh(report)
    
    logger.info(
        f"Report {report_id} signed and published by pathologist {ctx.user_id}",
        extra={
            "event": "report.sign",
            "report_id": report_id,
            "user_id": ctx.user_id,
        },
    )
    
    return ReportActionResponse(
        id=str(report.id),
        status=report.status,
        message="Report signed and published"
    )


@router.post("/{report_id}/retract", response_model=ReportActionResponse)
def retract_report(
    report_id: str,
    data: ReportStatusUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Retract a published report (PUBLISHED → RETRACTED) - Pathologist or Admin only"""
    # Check user role
    if user.role not in [UserRole.PATHOLOGIST, UserRole.ADMIN]:
        raise HTTPException(403, "Only pathologists or admins can retract reports")
    
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report does not belong to your tenant")
    
    if report.status != ReportStatus.PUBLISHED:
        raise HTTPException(400, f"Cannot retract report in {report.status} status")
    
    # Update status
    old_status = report.status
    report.status = ReportStatus.RETRACTED
    session.add(report)
    
    # Create audit log
    _create_audit_log(
        session=session,
        tenant_id=ctx.tenant_id,
        branch_id=str(report.branch_id),
        actor_user_id=ctx.user_id,
        action="REPORT.RETRACT",
        entity_type="report",
        entity_id=report_id,
        old_values={"status": old_status},
        new_values={"status": report.status, "changelog": data.changelog},
    )
    
    # Create timeline event for report retraction
    from app.models.events import OrderEvent
    from app.models.enums import EventType
    
    retract_event = OrderEvent(
        tenant_id=report.tenant_id,
        branch_id=report.branch_id,
        order_id=report.order_id,
        event_type=EventType.REPORT_RETRACTED,
        description="",  # Not used - message built in UI
        event_metadata={
            "report_id": str(report.id),
            "report_title": report.title,
            "reason": data.changelog if data.changelog else "Sin razón especificada",
            "retracted_by": str(user.id),
            "retracted_by_name": user.full_name or user.username,
        },
        created_by=user.id,
    )
    session.add(retract_event)
    
    # Update order status based on report being retracted (CLOSED -> REVIEW)
    if report.order_id:
        update_order_status_for_report(str(report.order_id), session)
    
    session.commit()
    session.refresh(report)
    
    logger.info(
        f"Report {report_id} retracted by pathologist {ctx.user_id}",
        extra={
            "event": "report.retract",
            "report_id": report_id,
            "user_id": ctx.user_id,
        },
    )
    
    return ReportActionResponse(
        id=str(report.id),
        status=report.status,
        message="Report retracted"
    )
