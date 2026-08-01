from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlmodel import select, Session, and_
from sqlalchemy import cast, String
from sqlalchemy.orm.attributes import flag_modified
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext, current_user
from app.models.report import Report, ReportVersion, ReportTemplate
from app.models.report_template_version import ReportTemplateVersion, ReportTemplateVersionStatus
from app.models.report_letterhead import ReportLetterhead
from app.models.report_letterhead_version import (
    ReportLetterheadVersion,
    ReportLetterheadVersionStatus,
)
from app.models.laboratory import Order
from app.models.tenant import Tenant, Branch
from app.models.patient import Patient
from app.models.storage import StorageObject
from app.models.user import AppUser
from app.models.audit import AuditLog
from app.models.enums import ReportStatus, AssignmentItemType, ReviewStatus
from app.core.rbac import has_permission, has_any_role, ROLE_REVIEWER
from app.models.assignment import Assignment
from app.models.report_review import ReportReview
from app.services.s3 import S3Service
from app.services.managed_tenant_image_service import (
    ManagedTenantImageService,
    InvalidImageError,
    ImageRegistrationError,
)
from app.services.report_pdf_generation import (
    ReportPdfGenerationService,
    ReportPdfGenerationError,
    ReportPdfAlreadyInProgressError,
    ReportPdfImmutableError,
    load_locked_version,
)
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
    SignatureMetadata,
    ReportResolvedResources,
)
from app.schemas.report_template_version import (
    ReportTemplateVersionCreate,
    ReportTemplateVersionResponse,
    ReportTemplateVersionDetailResponse,
    ReportTemplateVersionsListResponse,
    ReportRenderingSnapshotV2,
    ReportPresentationSnapshotV2,
    ReportTemplateLogoUploadResponse,
)
from app.schemas.laboratory import ReportFullDetailResponse
from pydantic import ValidationError as PydanticValidationError
import json
import re
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports")

# Céluma 1.3 Fase 2, Bloque B: identifies which backend logic produced a V2
# report's rendering snapshot (there is no VersionedReportRendererV2 yet —
# this only audits the *persistence* path, see
# phase-2-block-b-architecture-decision.md).
SNAPSHOT_BUILDER_VERSION = "block-b/1.0.0"

# Statuses in which a report's content/template/branding must never change
# again through the normal creation/versioning/PDF-upload flow (Historia B9).
# RETRACTED is included deliberately: retraction ends the normal editing
# lifecycle and this block does not implement a formal amendment flow — see
# block-c-dependencies.md.
_IMMUTABLE_REPORT_STATUSES = (ReportStatus.PUBLISHED, ReportStatus.RETRACTED)


def _require(user_id, code: str, session: Session) -> None:
    """Raise 403 if user lacks the specified permission."""
    if not has_permission(user_id, code, session):
        raise HTTPException(403, f"Permission required: {code}")


_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


def official_pdf_filename(order_code: str, version_no: int) -> str:
    """Céluma 1.3 Fase 2, Bloque E, Historia E10: the download filename for
    an official report PDF. Deliberately never derived from a patient name —
    only the order's human-readable code, which is already shown in the lab
    UI and is not more sensitive than the case itself."""
    safe_code = _UNSAFE_FILENAME_CHARS.sub("-", order_code or "reporte").strip("-") or "reporte"
    return f"reporte-{safe_code}-v{version_no}.pdf"


def official_pdf_presigned_url(
    s3: S3Service, object_key: str, order_code: str, version_no: int
) -> str:
    filename = official_pdf_filename(order_code, version_no)
    return s3.generate_presigned_url(
        object_key,
        response_content_disposition=f'attachment; filename="{filename}"',
    )


# Deferred imports to avoid circular module-load (laboratory <-> reports)
def update_order_status_for_report(order_id: str, session: Session) -> None:
    """Wrapper to update order status after report changes"""
    from app.api.v1.laboratory import update_order_status
    update_order_status(order_id, session)


def _build_order_full_detail(order_id: str, session: Session, ctx: AuthContext):
    """Deferred wrapper around laboratory.build_order_full_detail."""
    from app.api.v1.laboratory import build_order_full_detail
    return build_order_full_detail(order_id, session, ctx)


@router.get("/", response_model=ReportsListResponse)
def list_reports(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """List all reports (requires reports:read)."""
    _require(user.id, "reports:read", session)
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
        
        # Get reviewers
        reviews = session.exec(
            select(ReportReview).where(ReportReview.order_id == order.id if order else None)
        ).all()
        reviewers = []
        if reviews:
            from app.schemas.report import ReviewerWithStatus
            for review in reviews:
                reviewer = session.get(AppUser, review.reviewer_user_id)
                if reviewer:
                    reviewers.append(ReviewerWithStatus(
                        id=str(reviewer.id),
                        name=reviewer.full_name,
                        email=reviewer.email,
                        avatar_url=reviewer.avatar_url,
                        status=review.status.lower(),
                        review_id=str(review.id)
                    ))
        
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
                published_at=r.published_at,
                created_at=str(getattr(r, "created_at", "")) if getattr(r, "created_at", None) else None,
                created_by=str(r.created_by) if r.created_by else None,
                signed_by=signed_by,
                signed_at=signed_at,
                version_no=version_no,
                has_pdf=has_pdf,
                reviewers=reviewers if reviewers else None,
            )
        )
    
    return ReportsListResponse(reports=results)

def _compensate_failed_v2_report_creation(report_id, session: Session) -> None:
    """Best-effort compensation when a V2 report's S3/version write fails
    after its `Report` row was already committed (Historia B8).

    This is NOT a distributed transaction: there is a real (narrow) window
    where a process crash between the two commits below could still leave an
    orphaned `Report`. That residual risk is documented in
    phase-2-block-b-architecture-decision.md rather than solved with a
    saga/outbox mechanism, which would be out of scope for this block.
    """
    report = session.get(Report, report_id)
    if not report:
        return
    order_id = report.order_id

    order = session.get(Order, order_id)
    if order and order.report_id == report.id:
        order.report_id = None
        session.add(order)

    from app.models.report_review import ReportReview

    reviews = session.exec(
        select(ReportReview).where(ReportReview.report_id == report_id)
    ).all()
    for review in reviews:
        review.report_id = None
        session.add(review)

    from app.models.events import OrderEvent
    from app.models.enums import EventType

    events = session.exec(
        select(OrderEvent).where(
            OrderEvent.order_id == order_id,
            OrderEvent.event_type == EventType.REPORT_CREATED,
        )
    ).all()
    for event in events:
        metadata = event.event_metadata or {}
        if metadata.get("report_id") == str(report_id):
            session.delete(event)

    session.delete(report)
    session.flush()

    if order:
        update_order_status_for_report(str(order_id), session)

    session.commit()


@router.post("/", response_model=ReportResponse)
def create_report(
    report_data: ReportCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new report (requires reports:create).

    Céluma 1.3 Fase 2, Bloque B: when the tenant has
    `reports_v2_enabled=true` AND the caller explicitly selects a
    `template_version_id`, the report is created as schema_version=2 with a
    backend-built, backend-validated rendering snapshot embedded in its JSON
    body. In every other case (flag off, or no template_version_id sent) the
    legacy flow below is unchanged byte-for-byte. See
    phase-2-block-b-architecture-decision.md for the full rationale.
    """
    _require(user.id, "reports:create", session)
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

    # ------------------------------------------------------------------
    # Céluma 1.3 Fase 2, Bloque B, Historia B6 — resolve V2 vs legacy BEFORE
    # writing anything to the database, so an invalid V2 request never
    # creates a partial Report row.
    # ------------------------------------------------------------------
    template_version: ReportTemplateVersion | None = None
    validated_snapshot: ReportRenderingSnapshotV2 | None = None
    resolved_letterhead_version: ReportLetterheadVersion | None = None
    if report_data.template_version_id is not None:
        if not tenant.reports_v2_enabled:
            raise HTTPException(403, "V2 report creation is not enabled for this tenant")

        template_version = session.get(ReportTemplateVersion, report_data.template_version_id)
        if not template_version or str(template_version.tenant_id) != ctx.tenant_id:
            raise HTTPException(404, "Template version not found")
        if template_version.status == ReportTemplateVersionStatus.ARCHIVED:
            raise HTTPException(
                409, "Cannot create a report from an archived template version"
            )
        if report_data.report is None:
            raise HTTPException(
                400, "V2 reports require report content to build the rendering snapshot"
            )
        try:
            validated_snapshot = ReportRenderingSnapshotV2.model_validate(
                template_version.configuration
            )
        except PydanticValidationError as exc:
            logger.error(
                "Stored template version configuration failed re-validation",
                extra={
                    "event": "report.create_v2_invalid_template_version_configuration",
                    "template_version_id": str(template_version.id),
                    "error": str(exc),
                },
            )
            raise HTTPException(500, "Template version configuration is invalid") from exc

        # ------------------------------------------------------------------
        # Post-Fase-2 remediation, R7: resolve a membrete (letterhead) to
        # override the template version's embedded `presentation`.
        # Resolution order: explicit letterhead_version_id -> the owning
        # template's preferred_letterhead_version_id -> the tenant's default
        # letterhead's ACTIVE version. If none resolves, silently keep the
        # template version's own presentation (never blocks V2 creation) —
        # this is what keeps tenants that have not adopted the letterhead
        # domain yet byte-for-byte unchanged.
        # ------------------------------------------------------------------
        if report_data.letterhead_version_id is not None:
            resolved_letterhead_version = session.get(
                ReportLetterheadVersion, report_data.letterhead_version_id
            )
            if (
                not resolved_letterhead_version
                or str(resolved_letterhead_version.tenant_id) != ctx.tenant_id
            ):
                raise HTTPException(404, "Letterhead version not found")
            if resolved_letterhead_version.status == ReportLetterheadVersionStatus.ARCHIVED:
                raise HTTPException(
                    409, "Cannot create a report from an archived letterhead version"
                )
        else:
            owning_template = session.get(ReportTemplate, template_version.report_template_id)
            preferred_id = (
                owning_template.preferred_letterhead_version_id if owning_template else None
            )
            if preferred_id is not None:
                candidate = session.get(ReportLetterheadVersion, preferred_id)
                if candidate is not None and candidate.status != ReportLetterheadVersionStatus.ARCHIVED:
                    resolved_letterhead_version = candidate
            if resolved_letterhead_version is None:
                default_letterhead = session.exec(
                    select(ReportLetterhead).where(
                        ReportLetterhead.tenant_id == ctx.tenant_id,
                        ReportLetterhead.is_default == True,
                    )
                ).first()
                if default_letterhead is not None:
                    resolved_letterhead_version = session.exec(
                        select(ReportLetterheadVersion).where(
                            ReportLetterheadVersion.report_letterhead_id == default_letterhead.id,
                            ReportLetterheadVersion.status == ReportLetterheadVersionStatus.ACTIVE,
                        )
                    ).first()

        if resolved_letterhead_version is not None:
            try:
                resolved_presentation = ReportPresentationSnapshotV2.model_validate(
                    resolved_letterhead_version.configuration
                )
            except PydanticValidationError as exc:
                logger.error(
                    "Stored letterhead version configuration failed re-validation",
                    extra={
                        "event": "report.create_v2_invalid_letterhead_version_configuration",
                        "letterhead_version_id": str(resolved_letterhead_version.id),
                        "error": str(exc),
                    },
                )
                raise HTTPException(500, "Letterhead version configuration is invalid") from exc
            validated_snapshot = ReportRenderingSnapshotV2(
                schema_version=2,
                template=validated_snapshot.template,
                presentation=resolved_presentation,
            )

    report = Report(
        tenant_id=report_data.tenant_id,
        branch_id=report_data.branch_id,
        order_id=report_data.order_id,
        title=report_data.title,
        template=report_data.template,
        created_by=report_data.created_by,
        published_at=report_data.published_at
    )
    
    session.add(report)
    session.flush()
    
    # Update order with report_id (1-to-1 relationship)
    order.report_id = report.id
    session.add(order)
    
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
        body = dict(report_data.report)
        is_v2 = template_version is not None
        if is_v2:
            # Backend-authoritative: the client's `report_data.report` never
            # carries its own snapshot — only the validated, server-resolved
            # one is embedded (Historia B6, "el frontend puede seleccionar
            # template_version_id, pero no debe poder suministrar
            # arbitrariamente el snapshot final").
            body["schema_version"] = 2
            body["rendering_snapshot"] = validated_snapshot.model_dump(mode="json")

        try:
            s3 = S3Service()
            # Build S3 key
            key = f"reports/{report.tenant_id}/{report.branch_id}/{report.id}/versions/1/report.json"
            data_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
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
                schema_version=(2 if is_v2 else None),
                template_version_id=(template_version.id if is_v2 else None),
                generated_by_renderer_version=(
                    f"backend-snapshot-builder/{SNAPSHOT_BUILDER_VERSION}" if is_v2 else None
                ),
                letterhead_version_id=(
                    resolved_letterhead_version.id
                    if is_v2 and resolved_letterhead_version
                    else None
                ),
            )
            session.add(version)
            session.commit()
            session.refresh(version)
        except Exception as exc:
            session.rollback()
            if is_v2:
                # Historia B8: a V2 report is all-or-nothing. Compensate the
                # already-committed Report row rather than leave it orphaned
                # with no content and no snapshot anywhere.
                _compensate_failed_v2_report_creation(report.id, session)
                logger.error(
                    "V2 report creation failed after the Report row was committed; compensated",
                    extra={
                        "event": "report.create_v2_failed_compensated",
                        "report_id": str(report.id),
                        "error": str(exc),
                    },
                )
                raise HTTPException(
                    500,
                    "Failed to create the V2 report: the rendering snapshot could "
                    "not be persisted. No report was created.",
                )
            # Legacy path: this failure mode (Report row committed, content
            # upload then fails) pre-dates this block and is not changed
            # here — see phase-2-block-b-architecture-decision.md, B8, for
            # why fixing it is out of scope for Bloque B.
            logger.error(
                "Report content upload failed after the Report row was already committed",
                extra={
                    "event": "report.create_legacy_content_upload_failed",
                    "report_id": str(report.id),
                    "error": str(exc),
                },
            )
            raise

    return ReportResponse(
        id=str(report.id),
        status=report.status,
        order_id=str(report.order_id),
        tenant_id=str(report.tenant_id),
        branch_id=str(report.branch_id)
    )


def _carry_forward_v2_metadata(
    current_version: ReportVersion | None,
    report_body: dict | None,
    session: Session,
) -> tuple[dict | None, int | None, str | None, str | None, str | None]:
    """Céluma 1.3 Fase 2, Bloque C, Historia C9.

    `create_report_new_version` only ever changes clinical content — it must
    never let a content-only save silently degrade a V2 report to legacy.
    Before this fix, neither `ReportVersion.schema_version`/
    `template_version_id` nor the JSON body's `rendering_snapshot` were
    carried forward onto a new content version, so saving an edit through
    the editor (whose `buildEnvelope()` rebuilds `report` from the template
    definition) would silently strip the snapshot and every later read would
    resolve the report as legacy. See versioned-renderer-v2-contract.md,
    "Continuidad del snapshot entre versiones de contenido", and
    phase-2-block-c-architecture-decision.md.

    Always re-attaches the FROZEN snapshot already stored on the current
    version — this never re-resolves or re-validates against a live
    ReportTemplateVersion (that would violate "no reconsultar la plantilla
    administrativa"), and never trusts a `rendering_snapshot` the client may
    have sent, only ever the one already persisted for this report.

    Post-Fase-2 remediation: also carries forward `letterhead_version_id`,
    the administrative twin of `template_version_id` — the membrete
    selector is only ever shown before a report's first save (D10), so a
    content-only save on an existing report must never change which
    letterhead produced its frozen `presentation` block.
    """
    if current_version is None or current_version.schema_version != 2:
        return report_body, None, None, None, None

    carried_metadata = (
        current_version.schema_version,
        str(current_version.template_version_id) if current_version.template_version_id else None,
        current_version.generated_by_renderer_version,
        str(current_version.letterhead_version_id) if current_version.letterhead_version_id else None,
    )

    if report_body is None:
        return None, *carried_metadata

    frozen_snapshot = None
    if current_version.json_storage_id:
        storage = session.get(StorageObject, current_version.json_storage_id)
        if storage:
            s3 = S3Service()
            try:
                existing = json.loads(s3.download_text(storage.object_key))
                frozen_snapshot = existing.get("rendering_snapshot")
            except Exception:
                logger.warning(
                    "Could not re-download the current V2 rendering_snapshot while "
                    "creating a new content version; the new version's JSON body will "
                    "be uploaded without it",
                    extra={
                        "event": "report.new_version_v2_snapshot_carry_forward_failed",
                        "report_version_id": str(current_version.id),
                    },
                )
                frozen_snapshot = None

    if frozen_snapshot is None:
        return report_body, *carried_metadata

    carried_body = dict(report_body)
    carried_body["schema_version"] = 2
    carried_body["rendering_snapshot"] = frozen_snapshot
    return carried_body, *carried_metadata


@router.post("/{report_id}/new_version", response_model=ReportVersionResponse)
def create_report_new_version(
    report_id: str,
    report_data: ReportCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new report version for an existing report (requires reports:edit).

    - Increments version_no based on current version
    - Marks old current version as not current
    - Uploads provided JSON body to S3 and links it
    """
    _require(user.id, "reports:edit", session)

    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")

    # Verify report belongs to the authenticated user's tenant
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report does not belong to your tenant")

    # Céluma 1.3 Fase 2, Bloque B, Historia B9: a published (or retracted)
    # report's content/template/branding is frozen. This moves the
    # protection from the frontend (which already disables the relevant
    # buttons) into the API itself — see phase-2-block-b-architecture-decision.md.
    if report.status in _IMMUTABLE_REPORT_STATUSES:
        raise HTTPException(
            409, f"Cannot create a new version for a report in {report.status} status"
        )

    # Determine next version number
    current_version = session.exec(
        select(ReportVersion).where(ReportVersion.report_id == report.id, ReportVersion.is_current == True)
    ).first()
    next_version_no = (current_version.version_no + 1) if current_version else 1

    (
        carried_report_body,
        carried_schema_version,
        carried_template_version_id,
        carried_renderer_version,
        carried_letterhead_version_id,
    ) = _carry_forward_v2_metadata(current_version, report_data.report, session)

    json_storage_id = None
    if carried_report_body is not None:
        s3 = S3Service()
        key = f"reports/{report.tenant_id}/{report.branch_id}/{report.id}/versions/{next_version_no}/report.json"
        data_bytes = json.dumps(carried_report_body, ensure_ascii=False).encode("utf-8")
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
        schema_version=carried_schema_version,
        template_version_id=carried_template_version_id,
        generated_by_renderer_version=carried_renderer_version,
        letterhead_version_id=carried_letterhead_version_id,
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
        schema_version=new_version.schema_version,
        template_version_id=(
            str(new_version.template_version_id) if new_version.template_version_id else None
        ),
        letterhead_version_id=(
            str(new_version.letterhead_version_id) if new_version.letterhead_version_id else None
        ),
        generated_by_renderer_version=new_version.generated_by_renderer_version,
    )

@router.get("/worklist", response_model=ReportsListResponse)
def get_pathologist_worklist(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
    branch_id: str = None,
):
    """Get worklist of reports in review (requires reports:read)."""
    _require(user.id, "reports:read", session)
    
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
    user: AppUser = Depends(current_user),
    active_only: bool = True,
):
    """List all report templates (requires reports:read)."""
    _require(user.id, "reports:read", session)
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
                preferred_letterhead_version_id=(
                    str(t.preferred_letterhead_version_id)
                    if t.preferred_letterhead_version_id
                    else None
                ),
            )
            for t in templates
        ]
    )


@router.get("/templates/{template_id}", response_model=ReportTemplateDetailResponse)
def get_template(
    template_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Get a specific report template by ID (requires reports:read)."""
    _require(user.id, "reports:read", session)
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
        preferred_letterhead_version_id=(
            str(template.preferred_letterhead_version_id)
            if template.preferred_letterhead_version_id
            else None
        ),
    )


@router.post("/templates/", response_model=ReportTemplateResponse)
def create_template(
    template_data: ReportTemplateCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new report template (requires reports:manage_templates)."""
    _require(user.id, "reports:manage_templates", session)
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
    """Update an existing report template (requires reports:manage_templates)."""
    _require(user.id, "reports:manage_templates", session)
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
        flag_modified(template, "template_json")
    if template_data.is_active is not None:
        template.is_active = template_data.is_active
    # Post-Fase-2 remediation: unlike the fields above, an explicit null is
    # meaningful here ("no preference, use the tenant default") — so this
    # checks `model_fields_set` instead of `is not None`.
    if "preferred_letterhead_version_id" in template_data.model_fields_set:
        new_pref = template_data.preferred_letterhead_version_id
        if new_pref is not None:
            pref_version = session.get(ReportLetterheadVersion, new_pref)
            if (
                not pref_version
                or str(pref_version.tenant_id) != ctx.tenant_id
                or pref_version.status == ReportLetterheadVersionStatus.ARCHIVED
            ):
                raise HTTPException(
                    400,
                    "preferred_letterhead_version_id must reference a "
                    "non-archived letterhead version owned by this tenant",
                )
        template.preferred_letterhead_version_id = new_pref

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
        preferred_letterhead_version_id=(
            str(template.preferred_letterhead_version_id)
            if template.preferred_letterhead_version_id
            else None
        ),
    )


@router.delete("/templates/{template_id}")
def delete_template(
    template_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
    hard_delete: bool = False,
):
    """Delete a report template (requires reports:manage_templates)."""
    _require(user.id, "reports:manage_templates", session)
    template = session.get(ReportTemplate, template_id)
    if not template:
        raise HTTPException(404, "Template not found")
    
    if str(template.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Template does not belong to your tenant")
    
    if hard_delete:
        # Céluma 1.3 Fase 2, Bloque B: a template with published versions can
        # never be hard-deleted — those versions may still be referenced by
        # report_version rows and must remain reconstructible. Soft-delete
        # (deactivate) instead. The FK itself already blocks this at the DB
        # level (no ON DELETE CASCADE); this check returns a clear 409
        # instead of surfacing a raw IntegrityError.
        has_versions = session.exec(
            select(ReportTemplateVersion.id).where(
                ReportTemplateVersion.report_template_id == template.id
            )
        ).first()
        if has_versions:
            raise HTTPException(
                409,
                "Cannot permanently delete a template with published versions. "
                "Archive its versions or deactivate the template instead.",
            )
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


# ============================================================================
# Report Template Version Endpoints (append-only, immutable) — Bloque B
#
# These publish/activate/archive immutable snapshots of a template's
# rendering configuration for administration and audit. They are NEVER
# consulted by a renderer to reconstruct an existing report — that source of
# truth is the snapshot embedded in the report's own JSON body at creation
# time (see phase-2-block-b-architecture-decision.md). There is deliberately
# no PUT/PATCH here: correcting a version means publishing a new one.
# ============================================================================

def _get_owned_template(template_id: str, ctx: AuthContext, session: Session) -> ReportTemplate:
    template = session.get(ReportTemplate, template_id)
    if not template:
        raise HTTPException(404, "Template not found")
    if str(template.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Template not found")
    return template


def _get_owned_template_version(
    template_id: str, version_id: str, ctx: AuthContext, session: Session
) -> ReportTemplateVersion:
    version = session.get(ReportTemplateVersion, version_id)
    if (
        not version
        or str(version.report_template_id) != template_id
        or str(version.tenant_id) != ctx.tenant_id
    ):
        raise HTTPException(404, "Template version not found")
    return version


def _template_version_response(v: ReportTemplateVersion) -> ReportTemplateVersionResponse:
    return ReportTemplateVersionResponse(
        id=str(v.id),
        tenant_id=str(v.tenant_id),
        report_template_id=str(v.report_template_id),
        version_number=v.version_number,
        schema_version=v.schema_version,
        status=v.status,
        created_by=str(v.created_by) if v.created_by else None,
        published_at=v.published_at,
        activated_at=v.activated_at,
        archived_at=v.archived_at,
    )


@router.get(
    "/templates/{template_id}/versions", response_model=ReportTemplateVersionsListResponse
)
def list_template_versions(
    template_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """List all published versions of a template, newest first (requires reports:manage_templates)."""
    _require(user.id, "reports:manage_templates", session)
    _get_owned_template(template_id, ctx, session)
    versions = session.exec(
        select(ReportTemplateVersion)
        .where(ReportTemplateVersion.report_template_id == template_id)
        .order_by(ReportTemplateVersion.version_number.desc())
    ).all()
    return ReportTemplateVersionsListResponse(
        versions=[_template_version_response(v) for v in versions]
    )


@router.get(
    "/templates/{template_id}/versions/{version_id}",
    response_model=ReportTemplateVersionDetailResponse,
)
def get_template_version(
    template_id: str,
    version_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Get a specific template version, including its full immutable configuration."""
    _require(user.id, "reports:manage_templates", session)
    _get_owned_template(template_id, ctx, session)
    version = _get_owned_template_version(template_id, version_id, ctx, session)
    return ReportTemplateVersionDetailResponse(
        **_template_version_response(version).model_dump(),
        configuration=version.configuration,
    )


@router.post(
    "/templates/{template_id}/versions", response_model=ReportTemplateVersionDetailResponse
)
def create_template_version(
    template_id: str,
    payload: ReportTemplateVersionCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Publish a new, immutable version of a template's rendering configuration.

    `payload.configuration` is validated against `ReportRenderingSnapshotV2`
    by FastAPI/Pydantic before this function runs. Append-only: this is the
    only way to add a version; there is no endpoint to edit one afterwards.
    """
    _require(user.id, "reports:manage_templates", session)
    template = _get_owned_template(template_id, ctx, session)

    logo_storage_id = payload.configuration.presentation.header.logo_storage_id
    if logo_storage_id is not None:
        logo_object = session.get(StorageObject, logo_storage_id)
        if not logo_object:
            raise HTTPException(400, "logo_storage_id does not reference an existing object")
        # Céluma 1.3 Fase 2, Bloque C, Historia C1: a logo referenced by a
        # published template version must be explicitly owned by the same
        # tenant that publishes it. `StorageObject.tenant_id` is nullable
        # (most objects predate this scoping and are tenant-scoped
        # indirectly through a parent entity instead), so an unscoped
        # object is rejected here too — it was never tagged as belonging to
        # this tenant, so it cannot be trusted as this tenant's logo. See
        # report-resource-resolution-contract.md.
        if str(logo_object.tenant_id) != str(template.tenant_id):
            raise HTTPException(
                400, "logo_storage_id does not reference an object owned by this tenant"
            )

    last_version = session.exec(
        select(ReportTemplateVersion)
        .where(ReportTemplateVersion.report_template_id == template.id)
        .order_by(ReportTemplateVersion.version_number.desc())
    ).first()
    next_version_number = (last_version.version_number + 1) if last_version else 1

    version = ReportTemplateVersion(
        tenant_id=template.tenant_id,
        report_template_id=template.id,
        version_number=next_version_number,
        schema_version=payload.configuration.schema_version,
        configuration=payload.configuration.model_dump(mode="json"),
        status=ReportTemplateVersionStatus.PUBLISHED,
        created_by=user.id,
    )
    session.add(version)
    session.commit()
    session.refresh(version)

    logger.info(
        f"Report template version {version.version_number} published for template {template_id}",
        extra={
            "event": "report_template_version.published",
            "template_id": template_id,
            "version_id": str(version.id),
            "user_id": str(user.id),
        },
    )

    return ReportTemplateVersionDetailResponse(
        **_template_version_response(version).model_dump(),
        configuration=version.configuration,
    )


@router.post(
    "/templates/{template_id}/versions/{version_id}/activate",
    response_model=ReportTemplateVersionResponse,
)
def activate_template_version(
    template_id: str,
    version_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Mark a version as the template's active/default version.

    At most one version is ACTIVE per template at a time (also enforced by a
    partial unique index at the database level). Calling this on an
    ARCHIVED version reactivates it — that is the explicit, intentional flow
    for reactivation; nothing else may reactivate an archived version as a
    side effect.
    """
    _require(user.id, "reports:manage_templates", session)
    _get_owned_template(template_id, ctx, session)
    version = _get_owned_template_version(template_id, version_id, ctx, session)

    previous_active = session.exec(
        select(ReportTemplateVersion).where(
            ReportTemplateVersion.report_template_id == template_id,
            ReportTemplateVersion.status == ReportTemplateVersionStatus.ACTIVE,
            ReportTemplateVersion.id != version.id,
        )
    ).first()
    if previous_active:
        previous_active.status = ReportTemplateVersionStatus.PUBLISHED
        session.add(previous_active)
        # Flush the demotion before promoting `version` below: both rows are
        # covered by the same partial unique index (at most one ACTIVE per
        # template), which Postgres checks per-statement, not per-transaction.
        # Without this explicit ordering, the flush order of the two UPDATEs
        # is otherwise unspecified and can violate the index transiently.
        session.flush()

    version.status = ReportTemplateVersionStatus.ACTIVE
    version.activated_at = datetime.utcnow()
    version.archived_at = None
    session.add(version)
    session.commit()
    session.refresh(version)

    logger.info(
        f"Report template version {version_id} activated for template {template_id}",
        extra={
            "event": "report_template_version.activated",
            "template_id": template_id,
            "version_id": version_id,
            "user_id": str(user.id),
        },
    )
    return _template_version_response(version)


@router.post(
    "/templates/{template_id}/versions/{version_id}/archive",
    response_model=ReportTemplateVersionResponse,
)
def archive_template_version(
    template_id: str,
    version_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Retire a version so it can no longer be selected for new reports.

    Existing reports that already reference this version are unaffected:
    their rendering snapshot was already embedded in their own JSON body at
    creation time and is never re-read from here.
    """
    _require(user.id, "reports:manage_templates", session)
    _get_owned_template(template_id, ctx, session)
    version = _get_owned_template_version(template_id, version_id, ctx, session)

    if version.status == ReportTemplateVersionStatus.ACTIVE:
        raise HTTPException(
            409,
            "Cannot archive the active version. Activate a replacement version first.",
        )
    if version.status == ReportTemplateVersionStatus.ARCHIVED:
        raise HTTPException(400, "Version is already archived")

    version.status = ReportTemplateVersionStatus.ARCHIVED
    version.archived_at = datetime.utcnow()
    session.add(version)
    session.commit()
    session.refresh(version)

    logger.info(
        f"Report template version {version_id} archived for template {template_id}",
        extra={
            "event": "report_template_version.archived",
            "template_id": template_id,
            "version_id": version_id,
            "user_id": str(user.id),
        },
    )
    return _template_version_response(version)


# ============================================================================
# Report Template Logo Upload — Bloque D, Historia D2
#
# Uploads a logo image to be referenced (by StorageObject id) as
# `presentation.header.logo_storage_id` when publishing a
# ReportTemplateVersion (see create_template_version above, which validates
# tenant ownership of the referenced object at publish time). A
# report-template logo must be resolvable by id, with tenant_id populated,
# per report-resource-resolution-contract.md. SVG is explicitly rejected: it
# can carry embedded scripts/markup, which nothing else in this contract
# permits. Validation/upload shared with the tenant-logo and letterhead-logo
# endpoints via `ManagedTenantImageService` — see
# managed-logo-upload-contract.md.
# ============================================================================

@router.post(
    "/templates/{template_id}/logo",
    response_model=ReportTemplateLogoUploadResponse,
)
def upload_template_logo(
    template_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Upload a logo image for a template version's `presentation.header`.

    Returns a `storage_object_id` to use as `logo_storage_id` when publishing
    a version. Does not touch any ReportTemplateVersion — publishing is a
    separate, explicit step (create_template_version).

    Validation/upload delegated to `ManagedTenantImageService`, shared with
    the letterhead-logo endpoint and the tenant-logo endpoint (post-Fase-2
    remediation R5/R9 — see managed-logo-upload-contract.md).
    """
    _require(user.id, "reports:manage_templates", session)
    template = _get_owned_template(template_id, ctx, session)

    file_bytes = file.file.read()
    try:
        result = ManagedTenantImageService().upload(
            file_bytes=file_bytes,
            declared_content_type=file.content_type or "",
            tenant_id=template.tenant_id,
            key_prefix=f"report-templates/{template_id}/logos",
            created_by=user.id,
            session=session,
        )
    except InvalidImageError as exc:
        raise HTTPException(400, exc.message) from None
    except ImageRegistrationError:
        raise HTTPException(500, "Failed to register uploaded logo") from None

    logger.info(
        f"Report template logo uploaded for template {template_id}",
        extra={
            "event": "report_template_logo.uploaded",
            "template_id": template_id,
            "storage_object_id": str(result.storage_object.id),
            "user_id": str(user.id),
        },
    )

    return ReportTemplateLogoUploadResponse(
        storage_object_id=str(result.storage_object.id),
        url=result.url,
        content_type=result.content_type,
        size_bytes=result.size_bytes,
    )


def _resolve_report_resources(
    report: Report,
    report_json: dict | None,
    session: Session,
) -> ReportResolvedResources | None:
    """Céluma 1.3 Fase 2, Bloque C, Historia C1.

    Resolves ephemeral, request-scoped resources referenced by a V2 report's
    `rendering_snapshot` (currently: `presentation.header.logo_storage_id`)
    into a downloadable URL. Never mutates `report_json` and never persists
    anything — recomputed on every read. See
    report-resource-resolution-contract.md for the full contract, including
    why an unresolved/cross-tenant/missing logo falls back to `None` instead
    of raising.
    """
    if not isinstance(report_json, dict):
        return None
    snapshot = report_json.get("rendering_snapshot")
    if not isinstance(snapshot, dict):
        return None
    presentation = snapshot.get("presentation")
    header = presentation.get("header") if isinstance(presentation, dict) else None
    logo_storage_id = header.get("logo_storage_id") if isinstance(header, dict) else None
    if not logo_storage_id:
        return None

    try:
        logo_object = session.get(StorageObject, logo_storage_id)
    except (ValueError, TypeError):
        logo_object = None
    if not logo_object:
        return None
    # Defense in depth: the object was already validated as belonging to
    # this tenant when the ReportTemplateVersion that produced this
    # snapshot was published (create_template_version). Re-checking here
    # means a future bug in that check, or a historical row inserted before
    # this validation existed, can never leak a cross-tenant logo through a
    # report read.
    if str(logo_object.tenant_id) != str(report.tenant_id):
        return None

    s3 = S3Service()
    return ReportResolvedResources(header_logo_url=s3.object_public_url(logo_object.object_key))


def _build_report_detail_response(
    report: Report,
    version: ReportVersion | None,
    session: Session,
) -> ReportDetailResponse:
    """Build a ReportDetailResponse, downloading the JSON payload from S3 when available."""
    report_json = None
    if version and version.json_storage_id:
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
        version_no=(version.version_no if version else None),
        status=report.status,
        order_id=str(report.order_id),
        tenant_id=str(report.tenant_id),
        branch_id=str(report.branch_id),
        title=report.title,
        template=report.template,
        published_at=report.published_at,
        created_by=(str(report.created_by) if report.created_by else None),
        signed_by=(str(version.signed_by) if version and version.signed_by else None),
        signed_at=(version.signed_at if version else None),
        report=report_json,
        schema_version=(version.schema_version if version else None),
        template_version_id=(
            str(version.template_version_id)
            if version and version.template_version_id
            else None
        ),
        letterhead_version_id=(
            str(version.letterhead_version_id)
            if version and version.letterhead_version_id
            else None
        ),
        generated_by_renderer_version=(
            version.generated_by_renderer_version if version else None
        ),
        resolved_resources=_resolve_report_resources(report, report_json, session),
        pdf_generation_status=(version.pdf_generation_status if version else None),
        pdf_generated_at=(version.pdf_generated_at if version else None),
        pdf_sha256=(version.pdf_sha256 if version else None),
        pdf_size_bytes=(version.pdf_size_bytes if version else None),
        pdf_page_count=(version.pdf_page_count if version else None),
        pdf_error_code=(version.pdf_error_code if version else None),
        pdf_error_message=(version.pdf_error_message if version else None),
    )


@router.get("/{report_id}", response_model=ReportDetailResponse)
def get_report(
    report_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Get report details (requires reports:read)."""
    _require(user.id, "reports:read", session)
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Report not found")

    current_version = session.exec(
        select(ReportVersion).where(ReportVersion.report_id == report.id, ReportVersion.is_current == True)
    ).first()

    return _build_report_detail_response(report, current_version, session)


@router.get("/{report_id}/full", response_model=ReportFullDetailResponse)
def get_report_full(
    report_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Return all data needed to render the report editor (requires reports:read).
    order (with assignees, reviewers, labels), patient, samples, and full report detail
    including the template snapshot and the current version JSON from S3.
    """
    _require(user.id, "reports:read", session)

    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Report not found")

    # Aggregate order / patient / samples using the shared builder
    base = _build_order_full_detail(str(report.order_id), session, ctx)

    # Build full report detail with S3 JSON
    current_version = session.exec(
        select(ReportVersion).where(ReportVersion.report_id == report.id, ReportVersion.is_current == True)
    ).first()
    report_detail = _build_report_detail_response(report, current_version, session)

    return ReportFullDetailResponse(
        order=base.order,
        patient=base.patient,
        samples=base.samples,
        report=report_detail,
    )

@router.get("/{report_id}/versions")
def list_report_versions(
    report_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """List all versions for a report (requires reports:read)."""
    _require(user.id, "reports:read", session)
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
    user: AppUser = Depends(current_user),
):
    """Return a presigned URL to download the PDF for the newest report version (requires reports:read)."""
    _require(user.id, "reports:read", session)
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
    url = official_pdf_presigned_url(s3, storage.object_key, order.order_code if order else "", latest_version.version_no)
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
    user: AppUser = Depends(current_user),
):
    """Get specific report version details (requires reports:read)."""
    _require(user.id, "reports:read", session)
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

    return _build_report_detail_response(report, version, session)


def _pdf_generation_status_response(version: ReportVersion) -> dict:
    return {
        "version_id": str(version.id),
        "version_no": version.version_no,
        "report_id": str(version.report_id),
        "pdf_generation_status": version.pdf_generation_status,
        "pdf_generated_at": version.pdf_generated_at,
        "pdf_sha256": version.pdf_sha256,
        "pdf_size_bytes": version.pdf_size_bytes,
        "pdf_page_count": version.pdf_page_count,
        "pdf_error_code": version.pdf_error_code,
        "pdf_error_message": version.pdf_error_message,
    }


@router.post("/{report_id}/versions/{version_no}/generate-pdf")
def generate_report_pdf(
    report_id: str,
    version_no: int,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Generate the official PDF for one report version (requires reports:edit).

    Idempotent once READY (returns the existing artifact's metadata without
    regenerating). Retryable while FAILED as long as the report is still
    editable. Rejected (409) once the report is PUBLISHED/RETRACTED, or if a
    generation attempt is already running for this version. See
    pdf-generation-contract.md.
    """
    _require(user.id, "reports:edit", session)

    report, version = load_locked_version(session, report_id, version_no)
    if not report or not version:
        raise HTTPException(404, "Report or version not found")
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Report or version not found")

    service = ReportPdfGenerationService(session)
    try:
        version = service.generate(report, version, user.id)
    except (ReportPdfAlreadyInProgressError, ReportPdfImmutableError) as exc:
        raise HTTPException(409, exc.message) from None
    except ReportPdfGenerationError as exc:
        raise HTTPException(422, exc.message) from None

    return _pdf_generation_status_response(version)


@router.post("/{report_id}/versions/{version_no}/pdf")
def upload_pdf_to_specific_version(
    report_id: str,
    version_no: int,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Upload a PDF to a specific report version (requires reports:edit).

    - Validates report and version exist
    - Uploads PDF to S3 under a deterministic key
    - Creates a StorageObject and links it to ReportVersion.pdf_storage_id
    """
    _require(user.id, "reports:edit", session)

    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")

    # Verify report belongs to the authenticated user's tenant
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report does not belong to your tenant")

    # Céluma 1.3 Fase 2, Bloque B, Historia B9: never silently replace the
    # PDF of a published/retracted report.
    if report.status in _IMMUTABLE_REPORT_STATUSES:
        raise HTTPException(
            409, f"Cannot replace the PDF for a report in {report.status} status"
        )

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
    # Céluma 1.3 Fase 2, Bloque E: this manual endpoint bypasses
    # ReportPdfGenerationService entirely (no render, no validation, no
    # hash). Any generation metadata a prior official generation left behind
    # must never keep claiming READY for bytes that were never validated —
    # reset it so the publish gate in sign_report() correctly requires a
    # fresh official generation.
    version.pdf_generation_status = None
    version.pdf_generation_started_at = None
    version.pdf_generated_at = None
    version.pdf_sha256 = None
    version.pdf_size_bytes = None
    version.pdf_page_count = None
    version.pdf_generator_version = None
    version.pdf_error_code = None
    version.pdf_error_message = None
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
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Upload a PDF to the newest version of a report (requires reports:edit).

    - Selects the version with the highest version_no
    - If the report has no versions, returns 404
    - Uploads PDF, creates StorageObject, and updates pdf_storage_id
    """
    _require(user.id, "reports:edit", session)

    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")

    # Verify report belongs to the authenticated user's tenant
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report does not belong to your tenant")

    # Céluma 1.3 Fase 2, Bloque B, Historia B9: never silently replace the
    # PDF of a published/retracted report.
    if report.status in _IMMUTABLE_REPORT_STATUSES:
        raise HTTPException(
            409, f"Cannot replace the PDF for a report in {report.status} status"
        )

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
    # See the equivalent reset in upload_pdf_to_specific_version above.
    latest_version.pdf_generation_status = None
    latest_version.pdf_generation_started_at = None
    latest_version.pdf_generated_at = None
    latest_version.pdf_sha256 = None
    latest_version.pdf_size_bytes = None
    latest_version.pdf_page_count = None
    latest_version.pdf_generator_version = None
    latest_version.pdf_error_code = None
    latest_version.pdf_error_message = None
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
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Return a presigned URL to download the PDF for a specific report version (requires reports:read)."""
    _require(user.id, "reports:read", session)
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")

    # Post-Fase-2 remediation (bug 4): tenant mismatch now 404s, matching
    # get_pdf_of_latest_version and every other report-lookup endpoint in
    # this file — a 403 here previously leaked "this report exists but
    # belongs to another tenant", inconsistent with the rest of the API.
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Report not found")

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
    url = official_pdf_presigned_url(s3, storage.object_key, order.order_code if order else "", version.version_no)
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
    """Submit a report for review — DRAFT → IN_REVIEW (requires reports:submit)."""
    _require(user.id, "reports:submit", session)
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
    elif not has_permission(user.id, "reports:approve", session):
        raise HTTPException(403, "Permission required: reports:approve")
    
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
    elif not has_permission(user.id, "reports:approve", session):
        raise HTTPException(403, "Permission required: reports:approve")
    
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
    """Sign and publish a report (APPROVED → PUBLISHED) — requires reports:sign + 'reviewer' role."""
    if not has_permission(user.id, "reports:sign", session):
        raise HTTPException(403, "Permission required: reports:sign")
    if not has_any_role(user.id, {ROLE_REVIEWER}, session):
        raise HTTPException(403, f"Only users with the '{ROLE_REVIEWER}' role can sign reports")

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

    # Céluma 1.3 Fase 2, Bloque E: a report cannot be published without a
    # validated, hashed, persisted official PDF for the version being
    # published — see pdf-publication-workflow.md. Generation is a separate,
    # explicit step (POST .../generate-pdf); signing never generates one
    # itself, to keep the (potentially slow, browser-driven) generation out
    # of this transaction.
    if current_version.pdf_generation_status != "READY":
        raise HTTPException(
            422,
            "Cannot sign: this report version has no generated PDF yet. "
            "Generate the official PDF before publishing.",
        )

    # If the persisted JSON requires a digital signature, embed the signer's
    # PNG (presigned URL) into the document under signatureMetadata before
    # finalising the signature.
    if current_version.json_storage_id is not None:
        json_storage = session.get(StorageObject, current_version.json_storage_id)
        if json_storage is not None:
            s3 = S3Service()
            try:
                raw_json = s3.download_text(json_storage.object_key)
                report_doc = json.loads(raw_json)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to load report JSON from S3 while signing",
                    extra={
                        "event": "report.sign_json_load_failed",
                        "report_id": report_id,
                        "object_key": json_storage.object_key,
                        "error": str(exc),
                    },
                )
                raise HTTPException(500, "Failed to load report content for signing")

            metadata_dict = report_doc.get("signatureMetadata") or {}
            try:
                signature_meta = SignatureMetadata.model_validate(metadata_dict)
            except Exception:
                # Tolerate legacy or malformed metadata: fall back to defaults.
                signature_meta = SignatureMetadata()

            if signature_meta.require_digital_signature:
                if user.signature_storage_id is None:
                    raise HTTPException(
                        422,
                        "Cannot sign: the report requires a digital signature image but the signer has no signature uploaded",
                    )
                sig_storage = session.get(StorageObject, user.signature_storage_id)
                if sig_storage is None:
                    raise HTTPException(
                        422,
                        "Cannot sign: signer's signature storage object is missing",
                    )
                # Use the public CDN URL (same pattern as avatars, sample images
                # and /users/me/signature). Presigned S3 URLs would fail in the
                # browser when the bucket is fronted by CloudFront with public
                # access blocked at the S3 level. The signature object key is
                # already unique per upload (timestamp-suffixed), so no cache
                # buster query string is needed.
                signature_url = s3.object_public_url(sig_storage.object_key)
                report_doc["signatureMetadata"] = {
                    **metadata_dict,
                    "show_signature_section": True,
                    "require_digital_signature": True,
                    "signature_url": signature_url,
                }

                updated_bytes = json.dumps(report_doc, ensure_ascii=False).encode("utf-8")
                info = s3.upload_bytes(
                    updated_bytes,
                    key=json_storage.object_key,
                    content_type="application/json",
                )
                json_storage.etag = info.etag
                json_storage.size_bytes = info.size_bytes
                json_storage.version_id = info.version_id
                session.add(json_storage)

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
    """Retract a published report (PUBLISHED → RETRACTED) — requires reports:retract."""
    if not has_permission(user.id, "reports:retract", session):
        raise HTTPException(403, "Permission required: reports:retract")
    
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
