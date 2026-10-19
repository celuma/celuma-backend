from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlmodel import select, Session, and_
from sqlalchemy import cast, String
from sqlalchemy.orm.attributes import flag_modified
from app.core.config import settings
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext, current_user
from app.services.report_v2_policy import require_v2_authoring
from app.services.password_confirmation import require_password_confirmation
from app.services.report_lifecycle_events import (
    ACTION_REPORT_REOPENED,
    AUDIT_AMENDMENT_CANCEL,
    AUDIT_REOPEN_FOR_AMENDMENT,
    REOPEN_KIND_UNSIGNED,
    amendment_context,
    build_amendment_cancel_event,
    build_amendment_reopen_event,
    build_publication_event,
    publication_correlation,
)
from app.services import report_amendments
from app.models.report_amendment import (
    AMENDMENT_STATE_CANCELLED,
    AMENDMENT_STATE_PUBLISHED,
    ReportAmendment,
)
from app.models.report import Report, ReportVersion, ReportTemplate
from app.models.report_template_version import ReportTemplateVersion, ReportTemplateVersionStatus
from app.models.report_letterhead import ReportLetterhead
from app.models.report_letterhead_version import (
    ReportLetterheadVersion,
    ReportLetterheadVersionStatus,
)
from app.models.laboratory import Order
from app.models.tenant import Tenant, Branch
from app.models.storage import StorageObject
from app.models.user import AppUser
from app.models.audit import AuditLog
from app.models.enums import ReportStatus, AssignmentItemType, ReviewStatus
from app.core.rbac import has_permission
from app.models.assignment import Assignment
from app.models.report_review import ReportReview
from app.services.s3 import S3Service
from app.services import order_lifecycle
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
from app.models.study_type import StudyType
from app.services.report_filename import build_report_pdf_filename
from app.services.letterhead_resolution import (
    LetterheadArchivedError,
    LetterheadConfigurationError,
    LetterheadNotFoundError,
    resolve_effective_letterhead_version,
)
# Céluma 1.3 Phase 3, Block F: the domain -> notification integration layer.
# These are the ONLY notification symbols this module imports —
# `NotificationService`, the template registry and the delivery machinery are
# all behind them, which is what keeps report code free of notification
# internals and notification code free of report internals.
from app.services.notification_integrations import (
    notify_report_pdf_ready,
    notify_report_published,
    notify_report_republished,
    notify_report_retracted,
    notify_report_submitted,
)
from app.services.report_template_autoversion import snapshot_and_activate_template_version
from app.services.report_template_hash import hash_clinical_template_block
from app.services.list_pagination import (
    MAX_PAGE_SIZE,
    enum_filter,
    page_request,
    run_list,
    search_text,
    sort_field,
    uuid_filter,
)
from app.services.report_lists import (
    REPORT_SORTS,
    build_report_items,
    report_filter_options,
    report_list_statement,
)
from app.services.usage import UsageService
from app.services.report_authorization import (
    AmendmentLegacyOriginalError,
    ReportAlreadySignedError,
    ReportAuthorizationError,
    ReportStateError,
    authorize_amendment_cancellation,
    authorize_amendment_opening,
    authorize_approval,
    authorize_presentation_change,
    authorize_reopen,
    authorize_request_changes,
    authorize_signing,
    is_content_editable,
    is_signed_version,
)
from app.services.report_presentation import (
    SIGNATURE_METADATA_KEY,
    author_requested_letterhead_change,
    signature_defaults_from_template,
    enforce_author_presentation_boundary,
    PresentationContentMissingError,
    PresentationLetterheadError,
    PresentationLetterheadNotFoundError,
    apply_presentation_change,
)
from app.services.usage_thresholds import record_storage_delta_with_thresholds
from app.services.report_publishing import (
    embed_signature_metadata_if_required,
    claim_publish,
    clear_publish_claim,
    finalize_publish,
    ReportPublishError,
    ReportPublishAlreadyInProgressError,
    ReportPublishConflictError,
)
from app.services.report_default_reviewer import ensure_default_reviewer_assignment
from app.services.report_metadata import (
    apply_authoritative_report_metadata,
    embed_delivery_date_at_signing,
)
from app.schemas.report import (
    ReportCreate, 
    ReportPresentationUpdate,
    ReportPresentationResponse,
    ReportResponse, 
    ReportDetailResponse, 
    ReportVersionCreate, 
    ReportVersionResponse,
    ReportsListResponse,
    ReportFilterOptionsResponse,
    ReportStatusUpdate,
    ReportSignRequest,
    ReportReopenRequest,
    ReportAmendmentOpenRequest,
    ReportAmendmentCancelRequest,
    ReportAmendmentInfo,
    ReportAmendmentActionResponse,
    ReportEmissionInfo,
    ReportEmissionsResponse,
    ReportReviewComment,
    ReportActionResponse,
    ReportSignAndPublishResponse,
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
import copy
import json
import re
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports")

# Céluma 1.3 Phase 2, Block B: identifies which backend logic produced a V2
# report's rendering snapshot (there is no VersionedReportRendererV2 yet —
# this only audits the *persistence* path, see
# phase-2-block-b-architecture-decision.md).
SNAPSHOT_BUILDER_VERSION = "block-b/1.0.0"

# Statuses in which a report's content/template/branding must never change
# again through the normal creation/versioning/PDF-upload flow (Story B9).
# RETRACTED is included deliberately: retraction ends the normal editing
# lifecycle and this block does not implement a formal amendment flow — see
# block-c-dependencies.md.
_IMMUTABLE_REPORT_STATUSES = (ReportStatus.PUBLISHED, ReportStatus.RETRACTED)


def _require(user_id, code: str, session: Session) -> None:
    """Raise 403 if user lacks the specified permission."""
    if not has_permission(user_id, code, session):
        raise HTTPException(403, f"Permission required: {code}")


def authorize_report_read_access(
    session: Session, ctx: AuthContext, user: AppUser, report_id: str
) -> Report:
    """Fifth post-Phase-2 remediation — SINGLE read-access contract for a
    report by internal laboratory staff.

    Before this remediation every read-only route repeated its own checks
    by hand, and that is where the divergence that produced the real
    reported `403` slipped in: `GET /{id}/full` returned 200 while
    `GET /{id}/versions/{n}/pdf` returned
    `{"detail":"Report access blocked due to pending payment"}` (57 bytes,
    byte-for-byte the body captured in Safari) for the SAME user, the
    SAME report, and the SAME permission. See
    official-pdf-download-root-cause.md and
    report-read-authorization-contract.md.

    The policy, now in one place:

      * `reports:read`            -> 403 if missing (even if the id is known).
      * missing report            -> 404.
      * report from another tenant -> 404, NEVER 403: a 403 would confirm
                                     that id exists in another laboratory.

    `Order.billed_lock` deliberately does NOT participate here. It is the
    delivery gate for third parties (patient and requesting physician) and
    continues to apply, unchanged, in `app/api/v1/portal.py`. Applying it
    to internal staff protected nothing — `/full` already returns the full
    clinical content and the "Imprimir copia local" button already prints
    from there — and it did break the real flow: the pathologist signed
    their own report and could not download the PDF they had just generated.
    """
    _require(user.id, "reports:read", session)
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Report not found")
    return report


def official_pdf_filename(order_code: str, study_type: str | None = None) -> str:
    """Céluma 1.3 Phase 2, Block E, Story E10: the download filename for an
    official report PDF. Deliberately never derived from a patient name.

    H-0c: now the canonical contract shared with the local copy —
    `<ORDER_CODE>-<StudyTypePascalCase>.pdf`. The version was removed on
    purpose: the official filename names the canonical artifact, and
    provenance comes from the report id, version, object key, sha256 and
    audit history, never from a human-visible name. See
    app/services/report_filename.py."""
    return build_report_pdf_filename(order_code, study_type, local_copy=False)


def _order_study_type_name(order, session: Session) -> str | None:
    """The study type's display name for the filename. Returns None when the
    order has none, which the filename builder handles deterministically."""
    if order is None or getattr(order, "study_type_id", None) is None:
        return None
    study_type = session.get(StudyType, order.study_type_id)
    return study_type.name if study_type else None


def official_pdf_presigned_url(
    s3: S3Service, object_key: str, order_code: str, study_type: str | None = None
) -> str:
    """The presigned URL carries the friendly name via Content-Disposition.
    The stored object key is untouched — historical artifacts are never
    renamed to match a naming change (H-0c §11)."""
    filename = official_pdf_filename(order_code, study_type)
    return s3.generate_presigned_url(
        object_key,
        response_content_disposition=f'attachment; filename="{filename}"',
    )


# Deferred imports to avoid circular module-load (laboratory <-> reports)
def update_order_status_for_report(order_id: str, session: Session) -> None:
    """Wrapper to update order status after report changes"""
    from app.api.v1.laboratory import update_order_status
    update_order_status(order_id, session)


# ---------------------------------------------------------------------------
# Céluma 1.4 (#13): amendment helpers shared by the lifecycle routes
# ---------------------------------------------------------------------------

def _version_body(session: Session, version: ReportVersion | None) -> dict | None:
    """The stored JSON body of a version, or None when it cannot be read."""
    if version is None or not version.json_storage_id:
        return None
    storage = session.get(StorageObject, version.json_storage_id)
    if storage is None:
        return None
    try:
        return json.loads(S3Service().download_text(storage.object_key))
    except Exception:
        logger.warning(
            "Could not read a report version body",
            extra={"event": "report.version_body_unreadable", "report_version_id": str(version.id)},
        )
        return None


def _user_name(session: Session, user_id) -> str | None:
    if not user_id:
        return None
    u = session.get(AppUser, user_id)
    return (u.full_name or u.username) if u else None


def _amendment_info(session: Session, amendment: ReportAmendment) -> ReportAmendmentInfo:
    source = session.get(ReportVersion, amendment.source_version_id)
    published = (
        session.get(ReportVersion, amendment.published_version_id)
        if amendment.published_version_id else None
    )
    return ReportAmendmentInfo(
        id=str(amendment.id),
        amendment_no=amendment.amendment_no,
        state=amendment.state,
        category=amendment.category,
        reason=amendment.reason,
        source_version_id=str(amendment.source_version_id),
        source_version_no=source.version_no if source else None,
        source_signed_at=source.signed_at if source else None,
        requested_by=str(amendment.requested_by),
        requested_by_name=_user_name(session, amendment.requested_by),
        requested_at=amendment.requested_at,
        published_version_id=str(amendment.published_version_id) if amendment.published_version_id else None,
        published_version_no=published.version_no if published else None,
        published_at=amendment.published_at,
        cancelled_by=str(amendment.cancelled_by) if amendment.cancelled_by else None,
        cancelled_by_name=_user_name(session, amendment.cancelled_by),
        cancelled_at=amendment.cancelled_at,
        cancel_reason=amendment.cancel_reason,
    )


def _emission_info(
    session: Session, version: ReportVersion, delivered: ReportVersion | None
) -> ReportEmissionInfo:
    amendment = report_amendments.version_amendment(session, version)
    return ReportEmissionInfo(
        report_version_id=str(version.id),
        version_no=version.version_no,
        signed_at=version.signed_at,
        signed_by=str(version.signed_by) if version.signed_by else None,
        signed_by_name=_user_name(session, version.signed_by),
        amendment_no=amendment.amendment_no if amendment else None,
        is_delivered=bool(delivered is not None and delivered.id == version.id),
        pdf_available=bool(version.pdf_storage_id and version.pdf_generation_status == "READY"),
        pdf_sha256=version.pdf_sha256,
    )


def _amendment_scope(session: Session, report: Report) -> tuple[ReportAmendment | None, ReportVersion | None]:
    """The open amendment and its source emission, or (None, None)."""
    amendment = report_amendments.open_amendment(session, report)
    if amendment is None:
        return None, None
    return amendment, session.get(ReportVersion, amendment.source_version_id)


def _require_change_note(session: Session, version: ReportVersion | None) -> None:
    """D2: an amended emission cannot go to review or be signed without the
    author's change note."""
    if not report_amendments.change_note_of(_version_body(session, version)):
        raise HTTPException(
            409,
            {
                "code": report_amendments.CHANGE_NOTE_REQUIRED_CODE,
                "message": (
                    "Escribe la nota de enmienda (qué cambió y por qué) antes de "
                    "enviar a revisión o firmar."
                ),
            },
        )


def _build_order_full_detail(order_id: str, session: Session, ctx: AuthContext):
    """Deferred wrapper around laboratory.build_order_full_detail."""
    from app.api.v1.laboratory import build_order_full_detail
    return build_order_full_detail(order_id, session, ctx)


@router.get("/", response_model=ReportsListResponse)
def list_reports(
    page: Optional[int] = Query(None, ge=1),
    page_size: Optional[int] = Query(None, ge=1, le=MAX_PAGE_SIZE),
    q: Optional[str] = None,
    sort: Optional[str] = None,
    order: Literal["asc", "desc"] = "asc",
    status: Optional[List[str]] = Query(None),
    patient_id: Optional[List[str]] = Query(None),
    reviewer_id: Optional[List[str]] = Query(None),
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """List reports (requires reports:read).

    Céluma 1.4: search, filters and ordering apply to the tenant's whole
    collection; `page`/`page_size` select one page and `total` counts every
    match. See `app/services/list_pagination.py`.
    """
    _require(user.id, "reports:read", session)
    stmt = report_list_statement(
        ctx.tenant_id,
        q=search_text(q),
        statuses=enum_filter(status, ReportStatus, "status"),
        patient_ids=uuid_filter(patient_id, "patient_id"),
        reviewer_ids=uuid_filter(reviewer_id, "reviewer_id"),
        sort=sort_field(sort, REPORT_SORTS),
        descending=order == "desc",
    )
    request = page_request(page, page_size)
    items, total = run_list(session, stmt, request, build_report_items)
    return ReportsListResponse(
        reports=items,
        total=total,
        page=request.page if request else None,
        page_size=request.page_size if request else None,
    )


@router.get("/filter-options", response_model=ReportFilterOptionsResponse)
def get_report_filter_options(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Column filter catalogs over every report of the tenant (requires reports:read)."""
    _require(user.id, "reports:read", session)
    return report_filter_options(session, ctx.tenant_id)

def _compensate_failed_v2_report_creation(report_id, session: Session) -> None:
    """Best-effort compensation when a V2 report's S3/version write fails
    after its `Report` row was already committed (Story B8).

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

    Céluma 1.4 (#22): Reports V2 is the only creation path. The caller must
    select a `template_id` (with its `template_hash`) or a
    `template_version_id`; the report is created as schema_version=2 with a
    backend-built, backend-validated rendering snapshot embedded in its JSON
    body. A request with neither selector is refused with 409
    `reports_v2_template_required`, and the legacy tenant column
    `reports_v2_enabled` is no longer read: a persisted `false` cannot
    restore the retired V1 flow. See
    specs/reports/reports-v2-only.md in celuma-engineering.
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

    # Céluma 1.4 (#11): same order lock as removal/edit; a removed order cannot gain a report.
    order_lifecycle.lock_and_refresh_order(session, order)

    # Check if report already exists for this order
    existing_report = session.exec(
        select(Report).where(Report.order_id == report_data.order_id)
    ).first()

    if existing_report:
        raise HTTPException(400, "Report already exists for this order")

    # ------------------------------------------------------------------
    # Céluma 1.3 Phase 2, Block B, Story B6 — resolve V2 vs legacy BEFORE
    # writing anything to the database, so an invalid V2 request never
    # creates a partial Report row.
    # ------------------------------------------------------------------
    # Céluma 1.3.1 Block C (CEL-131-05) — TWO selectors, one V2 path.
    #
    # `template_version_id` is the ORIGINAL selector: "build this report from
    # exactly this historical published version". `template_id` is the
    # CURRENT one: "build it from this clinical template as it stands now".
    # The second exists because requiring the first made an ACTIVE
    # `ReportTemplateVersion` a runtime prerequisite for authoring any V2
    # report, and the final 1.3 architecture does not need one: a V2 report
    # is reconstructed solely from the `rendering_snapshot` frozen into its
    # own JSON body (see `_carry_forward_v2_metadata`), the clinical
    # structure comes from `ReportTemplate.template_json`, and presentation
    # comes from `resolve_effective_letterhead_version`.
    #
    # Both converge on the same freeze-and-validate code below, so there is
    # ONE V2 creation path and no duplicated bootstrap. They differ in
    # exactly two things: where `snapshot.template` is read from, and whether
    # a `template_version_id` is recorded as provenance.
    #
    # `template_version_id` wins if both are sent: an explicit historical
    # selection is more specific than a template-level one.
    template_version: ReportTemplateVersion | None = None
    validated_snapshot: ReportRenderingSnapshotV2 | None = None
    resolved_letterhead_version: ReportLetterheadVersion | None = None
    owning_template: ReportTemplate | None = None
    clinical_structure: dict | None = None
    if report_data.template_version_id is None and report_data.template_id is None:
        # Céluma 1.4 (#22): Reports V2 is the only authoring path. A request
        # that names no template (old client, direct API call, or a tenant
        # whose legacy `reports_v2_enabled` column is false) is refused; it
        # never falls back to the retired V1 flow.
        raise HTTPException(
            409,
            {
                "code": "reports_v2_template_required",
                "message": (
                    "Los reportes nuevos requieren una plantilla de reporte. "
                    "Selecciona una plantilla activa (Configuración → "
                    "Plantillas de Reporte) y vuelve a intentarlo."
                ),
            },
        )


    if report_data.template_version_id is not None:
        template_version = session.get(
            ReportTemplateVersion, report_data.template_version_id
        )
        if not template_version or str(template_version.tenant_id) != ctx.tenant_id:
            raise HTTPException(404, "Template version not found")
        if template_version.status == ReportTemplateVersionStatus.ARCHIVED:
            raise HTTPException(
                409, "Cannot create a report from an archived template version"
            )
        try:
            stored_snapshot = ReportRenderingSnapshotV2.model_validate(
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
            raise HTTPException(
                500, "Template version configuration is invalid"
            ) from exc
        clinical_structure = stored_snapshot.template
        owning_template = session.get(
            ReportTemplate, template_version.report_template_id
        )
    else:
        # Block C: the live clinical template IS the creation-time source
        # of truth. No ACTIVE version is looked up, and none is created —
        # the structure is frozen into this report's own snapshot below,
        # which is what every later read and render uses.
        owning_template = session.get(ReportTemplate, report_data.template_id)
        if not owning_template or str(owning_template.tenant_id) != ctx.tenant_id:
            raise HTTPException(404, "Template not found")
        if not owning_template.is_active:
            raise HTTPException(
                409,
                "Esta plantilla de reporte está desactivada, así que no "
                "puede usarse para crear reportes nuevos.",
            )
        # --------------------------------------------------------------
        # Céluma 1.3.1 Block C, finding C-8 — optimistic concurrency.
        #
        # `template_json` is a MUTABLE column, and this is a second read of
        # it: the editor read it at bootstrap and the author wrote clinical
        # content against what they saw. If an administrator saved the
        # template in between, freezing what is here now would produce a
        # report whose content is keyed by the author's structure and whose
        # `rendering_snapshot.template` describes a different one — and the
        # V2 renderer resolves sections through the SNAPSHOT, so the
        # author's text would be silently absent from the report and from
        # the official PDF.
        #
        # The token is compared, never trusted as data: a mismatch refuses
        # the request rather than substituting either structure. The
        # author's work is still in their editor; a newer template is not a
        # reason to rewrite their report without telling them.
        #
        # Placed here deliberately — after the tenant-scoped 404 and the
        # lifecycle 409, before the letterhead resolution, the snapshot
        # build, and every durable side effect (the `Report` row, the
        # `ReportVersion`, the S3 object, storage accounting, the order
        # event, the status transition). A conflict therefore leaves
        # nothing behind. It is also not an information oracle: a caller
        # who cannot read this template already got 404 above, so a 409
        # only ever reaches someone authorized for this exact template.
        #
        # Schema validation guarantees the token is present for this
        # selector (`ReportCreate._require_template_hash_for_the_template_id_selector`),
        # so there is no "absent hash" branch to fall through here.
        # See docs/celuma-1.3.1/block-c/template-mutation-race.md.
        # --------------------------------------------------------------
        current_template_hash = hash_clinical_template_block(
            owning_template.template_json
        )
        if report_data.template_hash != current_template_hash:
            logger.info(
                "V2 report creation refused: the clinical template changed "
                "while the report was being authored",
                extra={
                    "event": "report.create_v2_stale_template",
                    "template_id": str(owning_template.id),
                    "tenant_id": ctx.tenant_id,
                },
            )
            raise HTTPException(
                409,
                "La plantilla de este reporte cambió mientras lo "
                "editabas. Vuelve a cargar el reporte para trabajar con la "
                "plantilla actualizada; de lo contrario quedaría guardado "
                "con una estructura distinta a la que usaste.",
            )
        clinical_structure = owning_template.template_json

    if report_data.report is None:
        raise HTTPException(
            400, "V2 reports require report content to build the rendering snapshot"
        )

    # ------------------------------------------------------------------
    # Post-Phase-2 remediation, R7; deterministic resolution + explicit
    # block in the third remediation. `resolve_effective_letterhead_version`
    # is the ONLY source of truth (shared with
    # `GET /study-types/{id}/report-defaults`): explicit -> template
    # preferred -> tenant default, with strict invariants
    # (same tenant, active letterhead, EXACTLY one ACTIVE version).
    #
    # Deliberate behavior change: if NO letterhead resolves, V2 creation
    # is blocked with 409 instead of silently keeping the `presentation`
    # embedded in the template version. That silence is exactly what
    # produced V2 reports with a letterhead the user never chose (and,
    # in the editor, the fallback to Legacy). See
    # deterministic-letterhead-resolution-contract.md.
    # ------------------------------------------------------------------
    try:
        resolved_letterhead = resolve_effective_letterhead_version(
            session,
            ctx.tenant_id,
            template=owning_template,
            letterhead_version_id=report_data.letterhead_version_id,
        )
    except LetterheadNotFoundError as exc:
        raise HTTPException(404, exc.message) from None
    except (LetterheadArchivedError, LetterheadConfigurationError) as exc:
        raise HTTPException(409, exc.message) from None

    if resolved_letterhead is None:
        raise HTTPException(
            409,
            "No hay ningún membrete predeterminado configurado para este "
            "laboratorio. Configura uno en Configuración → Membretes y "
            "márcalo como predeterminado antes de crear reportes.",
        )

    resolved_letterhead_version = resolved_letterhead.version
    # The definitive snapshot, built and validated server-side. A
    # structure that cannot produce a valid snapshot fails HERE, naming
    # the real missing invariant — it never degrades to legacy and never
    # substitutes some other template's version.
    try:
        validated_snapshot = ReportRenderingSnapshotV2(
            schema_version=2,
            template=clinical_structure,
            presentation=resolved_letterhead.presentation,
        )
    except PydanticValidationError as exc:
        logger.error(
            "V2 report creation failed: the resolved clinical structure is not a "
            "valid rendering snapshot",
            extra={
                "event": "report.create_v2_invalid_clinical_structure",
                "template_id": (
                    str(owning_template.id) if owning_template is not None else None
                ),
                "error": str(exc),
            },
        )
        raise HTTPException(
            409,
            "La estructura clínica de esta plantilla no es válida, así que "
            "no puede congelarse en un reporte. Revisa la plantilla en "
            "Plantillas de Reporte.",
        ) from None
    logger.info(
        "V2 report letterhead resolved",
        extra={
            "event": "report.create_v2_letterhead_resolved",
            "letterhead_id": resolved_letterhead.letterhead_id,
            "letterhead_version_id": resolved_letterhead.letterhead_version_id,
            "resolution_source": resolved_letterhead.source.value,
            # Block C: NULL whenever the report was built from the live
            # template. Recorded as-is; never inferred from whichever
            # version happens to be ACTIVE.
            "template_version_id": (
                str(template_version.id) if template_version is not None else None
            ),
        },
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
        # Céluma 1.4 (#13): an original is never an amended document; only the
        # amendment routes write the identification block.
        body.pop(report_amendments.AMENDMENT_KEY, None)
        # Céluma 1.3.1 Block C: the V2 marker is the server-built SNAPSHOT,
        # not the presence of a `ReportTemplateVersion`. A report built from
        # the live template is fully V2 and carries no version row.
        # Céluma 1.4 (#22): every creation reaches this point with a snapshot.
        assert validated_snapshot is not None
        # Backend-authoritative: the client's `report_data.report` never
        # carries its own snapshot — only the validated, server-resolved
        # one is embedded (Story B6, "el frontend puede seleccionar
        # template_version_id, pero no debe poder suministrar
        # arbitrariamente el snapshot final").
        body["schema_version"] = 2
        body["rendering_snapshot"] = validated_snapshot.model_dump(mode="json")

        # Céluma 1.3.1 Block A (corrected A5/A6): same backend-authoritative
        # principle as `rendering_snapshot` directly above, applied to the
        # signature settings. A new report's presentation comes from the
        # template's defaults, never from the creating author's request — the
        # author does not own these fields at any point in the lifecycle, and
        # creation is where they would otherwise be set for free.
        #
        # The defaults are read from the SERVER-resolved snapshot, not from
        # `report.template` (which is the client's copy).
        body[SIGNATURE_METADATA_KEY] = signature_defaults_from_template(
            body["rendering_snapshot"].get("template")
        )

        # Céluma 1.3.1 Block D: same backend-authoritative principle, applied
        # to the system metadata base fields (requesting physician, reception
        # date, delivery date). Applies to V2 and Legacy alike — these are
        # ordinary base fields, not a V2-only concept. The effective template
        # is resolved exactly as it is for the signature defaults directly
        # above, and decides which of the three this report declares: a field
        # it declares is guaranteed to exist (the client cannot suppress an
        # official field by omitting the key), and one it does not declare is
        # never injected. See report_metadata.py. Runs after the C-8
        # template-hash guard above and after the Report row itself, like
        # every other durable side effect in this function.
        body = apply_authoritative_report_metadata(
            session,
            body,
            order,
            template=body["rendering_snapshot"].get("template"),
        )

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
                tenant_id=report.tenant_id,
            )
            session.add(storage)
            session.flush()
            # Céluma 1.3 Phase 4, Block C: report JSON bodies are billable,
            # permanent once created (see storage-flow-accounting-matrix.md
            # "report JSON create"). Same transaction as the insert/commit
            # below.
            record_storage_delta_with_thresholds(
                session,
                report.tenant_id,
                storage.size_bytes or 0,
                source="report_json",
                resource_type="report_json",
                actor_id=user.id,
            )

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
                schema_version=2,
                # Block C: real provenance or nothing. NULL when no template
                # version was read — permitted since the consolidated `v1_3_1`
                # dropped `ck_report_version_v2_requires_template_version`.
                # Never back-filled from the currently ACTIVE version: that
                # would assert the report came from a snapshot it never saw.
                template_version_id=(template_version.id if template_version else None),
                generated_by_renderer_version=(
                    f"backend-snapshot-builder/{SNAPSHOT_BUILDER_VERSION}"
                ),
                letterhead_version_id=(
                    resolved_letterhead_version.id if resolved_letterhead_version else None
                ),
            )
            session.add(version)
            session.commit()
            session.refresh(version)
        except Exception as exc:
            session.rollback()
            # Story B8: a V2 report is all-or-nothing. Compensate the
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
    """Céluma 1.3 Phase 2, Block C, Story C9.

    `create_report_new_version` only ever changes clinical content — it must
    never let a content-only save silently degrade a V2 report to legacy.
    Before this fix, neither `ReportVersion.schema_version`/
    `template_version_id` nor the JSON body's `rendering_snapshot` were
    carried forward onto a new content version, so saving an edit through
    the editor (whose `buildEnvelope()` rebuilds `report` from the template
    definition) would silently strip the snapshot and every later read would
    resolve the report as legacy. See versioned-renderer-v2-contract.md,
    "Snapshot continuity across content versions", and
    phase-2-block-c-architecture-decision.md.

    Always re-attaches the FROZEN snapshot already stored on the current
    version — this never re-resolves or re-validates against a live
    ReportTemplateVersion (that would violate "do not re-query the
    administrative template"), and never trusts a `rendering_snapshot` the
    client may have sent, only ever the one already persisted for this report.

    Post-Phase-2 remediation: also carries forward `letterhead_version_id`,
    the administrative twin of `template_version_id` — the letterhead
    selector is only ever shown before a report's first save (D10), so a
    content-only save on an existing report must never change which
    letterhead produced its frozen `presentation` block.

    Fifth post-Phase-2 remediation: that "only before the first save"
    boundary was precisely the wrong one (Observation A). This function
    REMAINS pure carry-forward — it never changes letterhead on its own —
    and the explicit, validated change is applied on top in
    `_apply_draft_letterhead_change`. See
    draft-letterhead-change-contract.md.
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


# Statuses where the letterhead is already part of the version submitted
# for review and cannot be changed (fifth post-Phase-2 remediation,
# Observation A). The boundary is NOT "the report already has an id" — it
# is "the report has left DRAFT". See letterhead-freeze-at-review-contract.md.
_LETTERHEAD_EDITABLE_STATUSES = (ReportStatus.DRAFT,)


def _apply_draft_letterhead_change(
    session: Session,
    ctx: AuthContext,
    report: Report,
    carried_body: dict | None,
    carried_schema_version: int | None,
    carried_letterhead_version_id: str | None,
    requested_letterhead_version_id: str | None,
) -> tuple[dict | None, str | None, ReportLetterheadVersion | None]:
    """Fifth post-Phase-2 remediation — letterhead change on an already
    persisted DRAFT (Observation A).

    Replaces EXCLUSIVELY `rendering_snapshot.presentation` and
    `ReportVersion.letterhead_version_id`. Never touches
    `rendering_snapshot.template`, `template_version_id`, base fields,
    sections, clinical values, images, patient or order data, signature,
    history, or already-generated PDFs: the incoming `carried_body` is
    already the definitive clinical content and only one key is replaced
    in it.

    Presentation is ALWAYS obtained server-side from the letterhead
    version id (via `resolve_effective_letterhead_version`, the only
    source of truth since the third remediation). A `presentation` that
    arrives in the client body is never used — the client only chooses,
    it does not dictate the design.

    Returns `(body, letterhead_version_id, resolved_version|None)`; the
    third position is non-null only when a real change happened, so the
    caller can audit it.
    """
    # No explicit request, or it is exactly the letterhead the version
    # already has: this is a normal content save — pure carry-forward.
    if requested_letterhead_version_id is None:
        return carried_body, carried_letterhead_version_id, None
    if str(requested_letterhead_version_id) == str(carried_letterhead_version_id or ""):
        return carried_body, carried_letterhead_version_id, None

    # A legacy report (or V2 without a snapshot) has no `presentation`
    # block to replace; changing the letterhead there means nothing and is
    # ignored silently, exactly as before this remediation.
    if carried_schema_version != 2:
        return carried_body, carried_letterhead_version_id, None

    # --- La frontera de inmutabilidad -------------------------------------
    if report.status not in _LETTERHEAD_EDITABLE_STATUSES:
        raise HTTPException(
            409,
            "El membrete quedó fijado al enviar el reporte a revisión y ya no "
            f"puede cambiarse (estado actual: {report.status}).",
        )

    # --- Validation of the requested letterhead ---------------------------
    try:
        resolved = resolve_effective_letterhead_version(
            session,
            ctx.tenant_id,
            letterhead_version_id=requested_letterhead_version_id,
        )
    except LetterheadNotFoundError as exc:
        # Includes the cross-tenant case: the resolver does not distinguish
        # "does not exist" from "belongs to another laboratory", precisely
        # so foreign ids are never confirmed.
        raise HTTPException(404, exc.message) from None
    except (LetterheadArchivedError, LetterheadConfigurationError) as exc:
        raise HTTPException(409, exc.message) from None

    if resolved is None:  # pragma: no cover - explicit branch never returns None
        raise HTTPException(404, "Letterhead version not found")

    # `resolve_effective_letterhead_version` validates tenant, archived, and
    # VERSION configuration, but the explicit branch does not check that
    # the logical letterhead is still active. A deactivated letterhead
    # remains readable for historical reports that already use it, but
    # must not be selectable again.
    if not resolved.letterhead.is_active:
        raise HTTPException(
            409,
            f"El membrete «{resolved.letterhead.name}» está desactivado y no "
            "puede asignarse a un reporte.",
        )
    if resolved.version.status != ReportLetterheadVersionStatus.ACTIVE:
        raise HTTPException(
            409,
            "Solo puede asignarse la versión activa de un membrete "
            f"(la versión indicada está en estado {resolved.version.status}).",
        )

    # --- Surgical replacement of `presentation` ---------------------------
    if not isinstance(carried_body, dict):
        # A save without a JSON body cannot rewrite the snapshot; only the
        # administrative link is changed, which is all there is.
        return carried_body, str(resolved.version.id), resolved.version

    snapshot = carried_body.get("rendering_snapshot")
    if not isinstance(snapshot, dict):
        return carried_body, str(resolved.version.id), resolved.version

    new_body = dict(carried_body)
    new_snapshot = dict(snapshot)
    # `template` is preserved byte-for-byte: letterhead is presentation,
    # the clinical template is something else (separation from the first
    # remediation).
    new_snapshot["presentation"] = resolved.presentation.model_dump(mode="json")
    new_body["rendering_snapshot"] = new_snapshot

    logger.info(
        "DRAFT report letterhead changed",
        extra={
            "event": "report.draft_letterhead_changed",
            "report_id": str(report.id),
            "old_letterhead_version_id": carried_letterhead_version_id,
            "new_letterhead_version_id": str(resolved.version.id),
        },
    )
    return new_body, str(resolved.version.id), resolved.version


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

    # Céluma 1.3 Phase 2, Block B, Story B9 moved content freezing out of the
    # frontend and into the API. Céluma 1.3.1 Block B (finding B-3) narrows it
    # from "PUBLISHED and RETRACTED are frozen" to an allowlist of the states
    # in which authoring is actually valid — see CONTENT_EDITABLE_STATUSES.
    #
    # APPROVED used to be editable. Once Block B added an explicit way back
    # from it (`POST /{id}/reopen`), leaving it editable was a hole straight
    # through the lifecycle: a holder of `reports:edit` could rewrite the
    # clinical content of an approved report and have it signed without the
    # new content ever being reviewed, leaving the approval attesting to text
    # that no longer existed. Reopening is now the only way back, and it costs
    # a fresh review and a fresh approval.
    #
    # PUBLISHED and RETRACTED are unchanged: still refused, still 409.
    if not is_content_editable(report):
        detail = f"Cannot create a new version for a report in {report.status} status"
        if report.status == ReportStatus.APPROVED:
            detail = (
                "Este reporte ya está aprobado y no puede editarse. Para "
                "corregirlo, reábrelo primero: volverá a borrador y deberá "
                "pasar de nuevo por revisión y aprobación."
            )
        raise HTTPException(409, detail)

    # Determine next version number
    current_version = session.exec(
        select(ReportVersion).where(ReportVersion.report_id == report.id, ReportVersion.is_current == True)
    ).first()
    next_version_no = (current_version.version_no + 1) if current_version else 1

    # Céluma 1.4 (#22): no new content version may continue a legacy report.
    require_v2_authoring(report, current_version)

    (
        carried_report_body,
        carried_schema_version,
        carried_template_version_id,
        carried_renderer_version,
        carried_letterhead_version_id,
    ) = _carry_forward_v2_metadata(current_version, report_data.report, session)

    # Céluma 1.3.1 Block A (corrected A5/A6). The letterhead is reviewer-only
    # in EVERY state, so the content path no longer changes it.
    #
    # This supersedes the fifth post-Phase-2 remediation (Observation A),
    # which let a DRAFT save carry `letterhead_version_id` on the reasoning
    # that the change should travel with the content on screen. That reasoning
    # still holds for whoever owns the choice — it is the OWNER that changed:
    # the letterhead decides what the final clinical document looks like, and
    # that is the reviewer's call, not the author's. The reviewer's equivalent
    # is `PATCH /reports/{id}/presentation`, which keeps the atomicity
    # argument intact for them (one call, validated the same way).
    #
    # Rejected rather than ignored, unlike the signature fields below: a
    # letterhead is chosen explicitly and visibly, so silently keeping the old
    # one would leave the caller believing the change landed.
    if author_requested_letterhead_change(
        report_data.letterhead_version_id, current_version
    ):
        raise HTTPException(
            403,
            "El membrete solo puede cambiarlo el revisor asignado, mientras el "
            "reporte está en revisión.",
        )

    # The signature settings are reviewer-only too, and are carried forward
    # from what is already persisted (or the template's defaults for a first
    # body) regardless of what the request contains.
    carried_report_body = enforce_author_presentation_boundary(
        session,
        carried_report_body,
        current_version=current_version,
        template=report.template,
    )

    # Céluma 1.4 (#13): inside an open amendment the identification block is
    # carried forward from the previous version and only the author's change
    # note is taken from the request; outside one it is stripped (D2).
    amendment, amendment_source = _amendment_scope(session, report)
    carried_report_body = report_amendments.carry_forward_identification(
        _version_body(session, current_version) if amendment is not None else None,
        carried_report_body,
        in_amendment=amendment is not None,
    )

    # Céluma 1.3.1 Block D: same backend-authoritative overwrite as
    # create_report, applied on every later content save too — a report
    # reaching this point is DRAFT or IN_REVIEW (is_content_editable above),
    # always pre-signing, so delivery_date is correctly forced back to
    # "unavailable" here rather than trusting whatever the client carried
    # forward.
    #
    # The effective template is the report's OWN frozen snapshot when
    # `_carry_forward_v2_metadata` re-attached one (V2), else the Report row's
    # stored template (Legacy) — never the live `ReportTemplate`, which may
    # have been migrated or edited since this report was authored.
    metadata_order = session.get(Order, report.order_id)
    carried_snapshot = (carried_report_body or {}).get("rendering_snapshot")
    carried_report_body = apply_authoritative_report_metadata(
        session,
        carried_report_body,
        metadata_order,
        template=(
            carried_snapshot.get("template")
            if isinstance(carried_snapshot, dict)
            else report.template
        ),
    )

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
            tenant_id=report.tenant_id,
        )
        session.add(storage)
        session.flush()
        json_storage_id = storage.id
        # Céluma 1.3 Phase 4, Block C: see the equivalent comment in
        # create_report above — same billable/permanent rule for every
        # report JSON body, one per version.
        record_storage_delta_with_thresholds(
            session,
            report.tenant_id,
            storage.size_bytes or 0,
            source="report_json",
            resource_type="report_json",
            actor_id=user.id,
        )

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
        amendment_id=amendment.id if amendment is not None else None,
    )
    session.add(new_version)
    
    # Reset all review statuses to pending when new version is created.
    # R9 extracted this into `_reset_order_reviews_to_pending`, which is this
    # exact operation named; the reopen transition now shares it.
    reviewer_count = _reset_order_reviews_to_pending(session, report.order_id)
    
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
            **amendment_context(amendment, amendment_source),
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
    
    extra = [Report.status == ReportStatus.IN_REVIEW]
    if branch_id:
        extra.append(Report.branch_id == branch_id)
    reports = session.exec(report_list_statement(ctx.tenant_id, extra_where=extra)).all()
    return ReportsListResponse(
        reports=build_report_items(session, reports, include_reviewers=False),
        total=len(reports),
    )


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
                preferred_letterhead_id=(
                    str(t.preferred_letterhead_id) if t.preferred_letterhead_id else None
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
        # Céluma 1.3.1 Block C (C-8): computed from the very object serialized
        # on the line above, so the token and the structure it describes cannot
        # disagree. The editor echoes it back on create; `create_report` refuses
        # with 409 if the template has moved since.
        template_hash=hash_clinical_template_block(template.template_json),
        created_by=str(template.created_by) if template.created_by else None,
        is_active=template.is_active,
        created_at=template.created_at,
        preferred_letterhead_version_id=(
            str(template.preferred_letterhead_version_id)
            if template.preferred_letterhead_version_id
            else None
        ),
        preferred_letterhead_id=(
            str(template.preferred_letterhead_id) if template.preferred_letterhead_id else None
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

    # Second post-Phase-2 remediation (UX): create and activate the first
    # internal clinical revision at once, if a letterhead is already
    # resolvable.
    snapshot_and_activate_template_version(session, template, user.id)

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
    # Post-Phase-2 remediation: unlike the fields above, an explicit null is
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

    # Second post-Phase-2 remediation (UX): the field the app writes from
    # now on is the logical letterhead, not a concrete version. Same
    # `model_fields_set` pattern as the legacy field above: an explicit
    # `null` clears the preference (falls back to the tenant default).
    if "preferred_letterhead_id" in template_data.model_fields_set:
        new_letterhead_pref = template_data.preferred_letterhead_id
        if new_letterhead_pref is not None:
            pref_letterhead = session.get(ReportLetterhead, new_letterhead_pref)
            if not pref_letterhead or str(pref_letterhead.tenant_id) != ctx.tenant_id:
                raise HTTPException(
                    400,
                    "preferred_letterhead_id must reference a letterhead owned by this tenant",
                )
        template.preferred_letterhead_id = new_letterhead_pref

    session.add(template)
    session.commit()
    session.refresh(template)

    # Second post-Phase-2 remediation (UX): re-snapshot/activate the
    # internal clinical revision only if `template_json` changed (internal
    # hash-diff). A `preferred_letterhead_id` change alone does not trigger
    # a new revision: the letterhead is resolved fresh on every report
    # creation (resolve_effective_letterhead_version), never from the
    # `presentation` embedded in this version — see
    # report-letterhead-selection-ux.md.
    snapshot_and_activate_template_version(session, template, user.id)

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
        preferred_letterhead_id=(
            str(template.preferred_letterhead_id) if template.preferred_letterhead_id else None
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
        # Céluma 1.3 Phase 2, Block B: a template with published versions can
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
# Report Template Version Endpoints (append-only, immutable) — Block B
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
    """List all published versions of a template, newest first (requires reports:read).

    H-0c: reading versions is part of AUTHORING a report, not administering
    templates. See `get_template_version` for the full rationale."""
    _require(user.id, "reports:read", session)
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
    """Get a specific template version, including its full immutable
    configuration (requires reports:read).

    H-0c pre-cutover blocker. This read is step 3 of the report editor's V2
    bootstrap chain: `report-defaults` returns `active_template_version_id`
    and the editor immediately fetches that version's configuration to build
    the empty report. Gating it behind `reports:manage_templates` meant only
    administrators could open a new report — pathologists, the core
    report-authoring role, got a 403 that the editor surfaced as "Falta el
    membrete predeterminado del laboratorio".

    READ and WRITE are now separated: authoring a report requires reading the
    effective configuration (`reports:read`), while creating, activating and
    archiving versions remain `reports:manage_templates`. Tenant anchoring is
    unchanged — `_get_owned_template` / `_get_owned_template_version` still
    404 across tenants, so this widens the ROLE, never the TENANT."""
    _require(user.id, "reports:read", session)
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
        # Céluma 1.3 Phase 2, Block C, Story C1: a logo referenced by a
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
# Report Template Logo Upload — Block D, Story D2
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
    the letterhead-logo endpoint and the tenant-logo endpoint (post-Phase-2
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

    # Céluma 1.3 Phase 4, Block C: same rule as the letterhead-logo endpoint
    # — see the equivalent comment in report_letterheads.upload_letterhead_logo.
    record_storage_delta_with_thresholds(
        session,
        template.tenant_id,
        result.size_bytes,
        source="letterhead_asset",
        resource_type="template_logo",
        actor_id=user.id,
    )
    session.commit()

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
    """Céluma 1.3 Phase 2, Block C, Story C1.

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
    if not isinstance(presentation, dict):
        return None
    header = presentation.get("header")
    footer = presentation.get("footer")
    header_logo_storage_id = header.get("logo_storage_id") if isinstance(header, dict) else None
    footer_logo_storage_id = footer.get("logo_storage_id") if isinstance(footer, dict) else None
    if not header_logo_storage_id and not footer_logo_storage_id:
        return None

    s3 = S3Service()

    def _resolve_one(storage_id: str | None) -> str | None:
        if not storage_id:
            return None
        try:
            logo_object = session.get(StorageObject, storage_id)
        except (ValueError, TypeError):
            return None
        if not logo_object:
            return None
        # Defense in depth: the object was already validated as belonging
        # to this tenant when the ReportTemplateVersion/ReportLetterheadVersion
        # that produced this snapshot was published. Re-checking here means
        # a future bug in that check, or a historical row inserted before
        # this validation existed, can never leak a cross-tenant logo
        # through a report read.
        if str(logo_object.tenant_id) != str(report.tenant_id):
            return None
        return s3.object_public_url(logo_object.object_key)

    header_logo_url = _resolve_one(header_logo_storage_id)
    footer_logo_url = _resolve_one(footer_logo_storage_id)
    if header_logo_url is None and footer_logo_url is None:
        # Preserves the pre-existing contract: `resolved_resources` is None
        # whenever there's nothing usable to resolve — whether because
        # nothing was referenced, or because every reference that WAS
        # present failed validation (e.g. cross-tenant) — never an object
        # with every field null.
        return None
    return ReportResolvedResources(header_logo_url=header_logo_url, footer_logo_url=footer_logo_url)


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
        open_amendment=(
            _amendment_info(session, amendment)
            if (amendment := report_amendments.open_amendment(session, report)) is not None
            else None
        ),
        delivered_emission=(
            _emission_info(session, delivered, delivered)
            if (delivered := report_amendments.delivered_emission(session, report)) is not None
            else None
        ),
    )


@router.get("/{report_id}", response_model=ReportDetailResponse)
def get_report(
    report_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Get report details (requires reports:read)."""
    report = authorize_report_read_access(session, ctx, user, report_id)

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
    report = authorize_report_read_access(session, ctx, user, report_id)

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
    report = authorize_report_read_access(session, ctx, user, report_id)
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
    """Return a presigned URL to download the PDF for the newest report version (requires reports:read).

    Fifth post-Phase-2 remediation: shares `authorize_report_read_access`
    with `/full` — see report-read-authorization-contract.md. The
    `Order.billed_lock` check that lived here was removed: it is the
    third-party delivery gate and remains in force in `portal.py`, not in
    the internal laboratory flow.
    """
    report = authorize_report_read_access(session, ctx, user, report_id)
    order = session.get(Order, report.order_id)

    # Céluma 1.4 (#13, D3/D6): once a report has been signed, "latest" means
    # the delivered emission (latest signed version) — never an unsigned
    # amendment draft that happens to have a higher version number.
    latest_version = report_amendments.delivered_emission(session, report) or session.exec(
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
    url = official_pdf_presigned_url(
        s3, storage.object_key, order.order_code if order else "",
        _order_study_type_name(order, session),
    )
    return {
        "version_id": str(latest_version.id),
        "version_no": latest_version.version_no,
        "report_id": str(latest_version.report_id),
        "pdf_storage_id": str(storage.id),
        "pdf_key": storage.object_key,
        "pdf_url": url,
    }

@router.get("/{report_id}/emissions", response_model=ReportEmissionsResponse)
def list_report_emissions(
    report_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Céluma 1.4 (#13): authorized history — every signed emission (newest
    first, with which one is delivered) and every amendment. Same read policy
    as the report itself (`reports:read`, tenant)."""
    report = authorize_report_read_access(session, ctx, user, report_id)
    delivered = report_amendments.delivered_emission(session, report)
    signed = session.exec(
        select(ReportVersion)
        .where(ReportVersion.report_id == report.id, ReportVersion.signed_at.is_not(None))
        .order_by(ReportVersion.version_no.desc())
    ).all()
    amendments = session.exec(
        select(ReportAmendment)
        .where(ReportAmendment.report_id == report.id)
        .order_by(ReportAmendment.amendment_no.desc())
    ).all()
    return ReportEmissionsResponse(
        emissions=[_emission_info(session, v, delivered) for v in signed],
        amendments=[_amendment_info(session, a) for a in amendments],
    )


@router.get("/{report_id}/{version_no}", response_model=ReportDetailResponse)
def get_report_version(
    report_id: str,
    version_no: int,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Get specific report version details (requires reports:read)."""
    report = authorize_report_read_access(session, ctx, user, report_id)

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

    # Céluma 1.4 (#22): never generate a new official PDF for a legacy draft.
    require_v2_authoring(report, version)

    # Céluma 1.3 Phase 3, Block F: captured BEFORE the call, because
    # `generate()` short-circuits on an already-READY version and returns it
    # unchanged. Without this, an idempotent retry (a double-click, a client
    # re-post) would look identical to a first success and notify the signers
    # again. The occurrence marker would still deduplicate it, but "notify
    # nobody about work that did not happen" is the correct behaviour, not a
    # duplicate the database happens to absorb.
    was_already_ready = version.pdf_generation_status == "READY"

    service = ReportPdfGenerationService(session)
    try:
        version = service.generate(report, version, user.id)
    except (ReportPdfAlreadyInProgressError, ReportPdfImmutableError) as exc:
        raise HTTPException(409, exc.message) from None
    except ReportPdfGenerationError as exc:
        raise HTTPException(422, exc.message) from None

    # The generation service commits internally, so the READY transition is
    # already durable here — this notification is a separate, smaller
    # transaction over an unchanged domain state. That is the one event whose
    # notification cannot be atomic with its transition, and it is the safe
    # asymmetry: the PDF exists whether or not anyone is told about it.
    if not was_already_ready and version.pdf_generation_status == "READY":
        notify_report_pdf_ready(
            session,
            report=report,
            order=session.get(Order, report.order_id) if report.order_id else None,
            version_id=version.id,
            actor=user,
        )
        session.commit()

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

    # Céluma 1.3 Phase 2, Block B, Story B9: never silently replace the
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

    # Céluma 1.4 (#13): a signed version's PDF is an issued artifact, whatever
    # the report's status (an amended report is editable again).
    if is_signed_version(version):
        raise HTTPException(409, "Cannot replace the PDF of a signed report version")

    # Céluma 1.4 (#22): refuse before any file read, object write or accounting.
    require_v2_authoring(report, version)

    # Basic content-type validation
    content_type = (file.content_type or "").lower()
    if "pdf" not in content_type:
        raise HTTPException(400, "Uploaded file must be a PDF")

    file_bytes = file.file.read()
    if not file_bytes:
        raise HTTPException(400, "Uploaded file is empty")

    # Céluma 1.3 Phase 4, Block C: capture the previously-referenced PDF
    # BEFORE repointing — this endpoint's key is deterministic, so a second
    # upload for the same version physically overwrites the S3 bytes under
    # the *previous* StorageObject row (R1 in storage-drift-risk-
    # analysis.md). See storage-flow-accounting-matrix.md "legacy PDF
    # replacement" for the delta rule this implements.
    previous_pdf_storage = (
        session.get(StorageObject, version.pdf_storage_id) if version.pdf_storage_id else None
    )

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
        tenant_id=report.tenant_id,
    )
    session.add(storage)
    session.flush()

    # Legacy-PDF-replacing-legacy-PDF (same sha256_hex-less category, same
    # slot): apply only the delta so the stale row's bytes are not
    # double-counted. An official PDF being repointed away from (sha256_hex
    # IS NOT NULL) is a different, permanently-billable category — it is
    # never decremented here (see billable-storage-calculation-contract.md).
    if previous_pdf_storage is not None and previous_pdf_storage.sha256_hex is None:
        delta = (storage.size_bytes or 0) - (previous_pdf_storage.size_bytes or 0)
    else:
        delta = storage.size_bytes or 0
    record_storage_delta_with_thresholds(
        session,
        report.tenant_id,
        delta,
        source="legacy_pdf",
        resource_type="report_pdf",
        actor_id=user.id,
    )

    version.pdf_storage_id = storage.id
    # Céluma 1.3 Phase 2, Block E: this manual endpoint bypasses
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

    # Céluma 1.3 Phase 2, Block B, Story B9: never silently replace the
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

    # Céluma 1.4 (#13): see upload_pdf_to_specific_version.
    if is_signed_version(latest_version):
        raise HTTPException(409, "Cannot replace the PDF of a signed report version")

    # Céluma 1.4 (#22): refuse before any file read, object write or accounting.
    require_v2_authoring(report, latest_version)

    content_type = (file.content_type or "").lower()
    if "pdf" not in content_type:
        raise HTTPException(400, "Uploaded file must be a PDF")

    file_bytes = file.file.read()
    if not file_bytes:
        raise HTTPException(400, "Uploaded file is empty")

    # Céluma 1.3 Phase 4, Block C: see the equivalent comment in
    # upload_pdf_to_specific_version above — same overwrite-under-a-stale-
    # row risk, same delta rule.
    previous_pdf_storage = (
        session.get(StorageObject, latest_version.pdf_storage_id)
        if latest_version.pdf_storage_id
        else None
    )

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
        tenant_id=report.tenant_id,
    )
    session.add(storage)
    session.flush()

    if previous_pdf_storage is not None and previous_pdf_storage.sha256_hex is None:
        delta = (storage.size_bytes or 0) - (previous_pdf_storage.size_bytes or 0)
    else:
        delta = storage.size_bytes or 0
    record_storage_delta_with_thresholds(
        session,
        report.tenant_id,
        delta,
        source="legacy_pdf",
        resource_type="report_pdf",
        actor_id=user.id,
    )

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
    """Return a presigned URL to download the PDF for a specific report version (requires reports:read).

    Fifth post-Phase-2 remediation — THIS is the endpoint of the real
    reported `403`. It now literally shares the same authorization
    function as `GET /{id}/full`, so "I can open the report but cannot
    download its PDF" is no longer representable. Cross-tenant remains
    404 (third remediation, bug 4), and the `Order.billed_lock` check —
    the root cause — was removed from here: it remains in force in
    `portal.py`, which is where the laboratory actually holds a report
    until it is paid for. See official-pdf-download-root-cause.md.
    """
    report = authorize_report_read_access(session, ctx, user, report_id)
    order = session.get(Order, report.order_id)

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
    url = official_pdf_presigned_url(
        s3, storage.object_key, order.order_code if order else "",
        _order_study_type_name(order, session),
    )
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


def _validate_letterhead_before_review(session: Session, report: Report) -> None:
    """Fifth post-Phase-2 remediation (§3.5) — checks just before freezing
    the letterhead when submitting for review.

    Deliberately does NOT require `letterhead_version_id`. There are
    historical V2 versions from before the first remediation whose
    `presentation` was frozen directly from the template version, with no
    administrative link; that field was never backfilled (see
    `ReportDetailResponse.letterhead_version_id`). Requiring it here would
    leave those drafts impossible to submit for review, which is exactly
    the kind of regression §14 forbids. What IS required is what truly
    determines how the official document will print: that the frozen
    version has a valid `presentation` block, and that — if there is an
    administrative link — it still points at a letterhead of this tenant.

    Legacy reports (schema_version != 2) have no `presentation` and are
    skipped entirely: their branch does not change in this remediation.
    """
    current_version = session.exec(
        select(ReportVersion).where(
            ReportVersion.report_id == report.id,
            ReportVersion.is_current == True,  # noqa: E712
        )
    ).first()
    if current_version is None or current_version.schema_version != 2:
        return

    # 1. The frozen snapshot must carry a valid presentation.
    presentation = None
    if current_version.json_storage_id:
        storage = session.get(StorageObject, current_version.json_storage_id)
        if storage:
            try:
                body = json.loads(S3Service().download_text(storage.object_key))
                snapshot = body.get("rendering_snapshot")
                if isinstance(snapshot, dict):
                    presentation = snapshot.get("presentation")
            except Exception:
                logger.warning(
                    "Could not re-read the frozen rendering_snapshot while validating "
                    "a submit-for-review",
                    extra={
                        "event": "report.submit_snapshot_unreadable",
                        "report_version_id": str(current_version.id),
                    },
                )
                # A network/storage failure is not evidence that the report
                # is misconfigured: submission is not blocked for it (that
                # would have turned a transient incident into a business
                # error).
                return

    if presentation is None:
        raise HTTPException(
            409,
            "Este reporte no tiene un membrete aplicado, así que no puede "
            "enviarse a revisión. Vuelve al borrador y selecciona un membrete.",
        )
    try:
        ReportPresentationSnapshotV2.model_validate(presentation)
    except PydanticValidationError:
        raise HTTPException(
            409,
            "La configuración de membrete congelada en este reporte no es "
            "válida. Selecciona de nuevo el membrete en el borrador antes de "
            "enviarlo a revisión.",
        ) from None

    # 2. If there is an administrative link, it must still belong to this tenant.
    if current_version.letterhead_version_id:
        lh_version = session.get(
            ReportLetterheadVersion, current_version.letterhead_version_id
        )
        if lh_version is None or str(lh_version.tenant_id) != str(report.tenant_id):
            raise HTTPException(
                409,
                "El membrete aplicado a este reporte ya no está disponible en "
                "este laboratorio. Selecciona otro antes de enviarlo a revisión.",
            )


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

    # Céluma 1.4 (#9/#10/#11): entering review is what closes the window for direct order/sample
    # edits and erroneous-capture removal. Take the SAME order lock those operations take, then
    # re-read the report, so a concurrent edit/removal either completes first (and this sees its
    # data) or waits and then sees the review evidence.
    order_lifecycle.lock_report_order(session, report)
    
    if report.status != ReportStatus.DRAFT:
        raise HTTPException(400, f"Cannot submit report in {report.status} status")

    # Céluma 1.4 (#22): a legacy draft cannot enter the review path.
    submit_version = session.exec(
        select(ReportVersion).where(
            ReportVersion.report_id == report.id,
            ReportVersion.is_current == True,  # noqa: E712
        )
    ).first()
    require_v2_authoring(report, submit_version)

    # Céluma 1.4 (#13): an amended emission needs its change note (D2).
    amendment, amendment_source = _amendment_scope(session, report)
    if amendment is not None:
        _require_change_note(session, submit_version)

    # Fifth post-Phase-2 remediation (§3.5): submit-for-review is the
    # moment the letterhead is FROZEN, so it is also the last moment to
    # check that what is being frozen is coherent. From here on no route
    # allows changing it (see letterhead-freeze-at-review-contract.md).
    _validate_letterhead_before_review(session, report)

    # Get all reviewers for this order (regardless of current status)
    reviewers = session.exec(
        select(ReportReview).where(
            and_(
                ReportReview.tenant_id == report.tenant_id,
                ReportReview.order_id == report.order_id,
            )
        )
    ).all()

    if not reviewers:
        # Céluma 1.3.1 Block D (CEL-131-06), amended by the manual-validation
        # remediation (R8): the tenant's configured default reviewer is now
        # materialized at ORDER CREATION, so by the time a normal report
        # reaches submission the order already has this assignment and the
        # guard above short-circuits.
        #
        # This call is the SAFETY NET, not the primary path, and it is the
        # same helper — there is one implementation of "materialize the
        # default", not two. It still matters for: orders created before this
        # release; orders created while no default was configured; a default
        # configured after the order already existed; and any other legitimate
        # order that reaches submission with no reviewer.
        #
        # `ensure_default_reviewer_assignment` re-checks emptiness itself, so
        # it is idempotent and cannot duplicate the order-creation row. It
        # never fabricates authority either: a stale or unconfigured default
        # returns None and the pre-existing 400 below stands.
        fallback = ensure_default_reviewer_assignment(
            session,
            tenant_id=report.tenant_id,
            order_id=report.order_id,
            report_id=report.id,
        )
        if fallback is not None:
            reviewers = [fallback]

    if not reviewers or len(reviewers) == 0:
        raise HTTPException(400, "Cannot submit report for review without reviewers assigned")
    
    # Reset all reviews to PENDING when re-submitting (allows re-review after
    # changes). Deliberately NOT `_reset_order_reviews_to_pending`: that helper
    # re-queries the table, while this list may hold the safety-net row created
    # moments ago and not yet flushed. Same semantics, applied to the list this
    # route already resolved.
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
        new_values={
            "status": report.status,
            "changelog": data.changelog,
            **amendment_context(amendment, amendment_source),
        },
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
            **amendment_context(amendment, amendment_source),
        },
        created_by=user.id,
    )
    session.add(submit_event)

    # Update order status (DIAGNOSIS -> REVIEW)
    if report.order_id:
        update_order_status_for_report(str(report.order_id), session)

    # Céluma 1.3 Phase 3, Block F: notify the order's reviewers, inside this
    # transaction and after the transition + OrderEvent are in the session, so
    # the notification commits atomically with the submission. The service
    # contains its own failures and returns None rather than raising, so this
    # cannot stop a report being submitted — asserted by
    # tests/http/test_notification_integration_failures.py.
    notify_report_submitted(
        session,
        report=report,
        order=session.get(Order, report.order_id) if report.order_id else None,
        actor=user,
        occurrence_marker=str(submit_event.id),
    )

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

    Céluma 1.3.1 Block A / CEL-131-01. The user must satisfy the full
    clinical reviewer contract — reviewer role AND `reports:approve` AND an
    assignment for this report's order. See
    `app/services/report_authorization.py`.

    Before 1.3.1 the rule was `assigned reviewer OR reports:approve`, and
    because the seed granted `reports:approve` to `pathologist`, any
    pathologist (and any superuser) could approve any report in the tenant.

    Updates the user's review record and applies MVP rule: ≥1 approved = report approved.
    """
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")

    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report does not belong to your tenant")

    try:
        user_review = authorize_approval(session, report, user)
    except ReportStateError as exc:
        raise HTTPException(400, exc.message) from None
    except ReportAuthorizationError as exc:
        raise HTTPException(403, exc.message) from None

    user_review.status = ReviewStatus.APPROVED
    user_review.decision_at = datetime.utcnow()
    session.add(user_review)
    amendment, amendment_source = _amendment_scope(session, report)

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
                "review_id": str(user_review.id),
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
        new_values={
            "status": report.status,
            "changelog": data.changelog,
            **amendment_context(amendment, amendment_source),
        },
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
            **amendment_context(amendment, amendment_source),
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
            "review_id": str(user_review.id),
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
    
    Céluma 1.3.1 Block A. The rejection half of the review decision, and it
    carried the identical `assigned reviewer OR reports:approve` defect as
    approval. Both halves of one decision share one contract — otherwise
    "request changes" becomes the unguarded way to move a report out of
    IN_REVIEW. See `app/services/report_authorization.py`.

    Updates the user's review record to REJECTED with comment.
    """
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")

    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report does not belong to your tenant")

    try:
        user_review = authorize_request_changes(session, report, user)
    except ReportStateError as exc:
        raise HTTPException(400, exc.message) from None
    except ReportAuthorizationError as exc:
        raise HTTPException(403, exc.message) from None

    user_review.status = ReviewStatus.REJECTED
    user_review.decision_at = datetime.utcnow()
    session.add(user_review)
    amendment, amendment_source = _amendment_scope(session, report)

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
                "review_id": str(user_review.id),
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
        new_values={
            "status": report.status,
            "comment": data.comment,
            **amendment_context(amendment, amendment_source),
        },
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
            **amendment_context(amendment, amendment_source),
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


def _reset_order_reviews_to_pending(session: Session, order_id) -> int:
    """Return every reviewer of an order to PENDING and clear their decision.
    Returns how many rows were reset.

    This is the workflow's canonical "a new review cycle starts now" step. It
    already existed, inline and identical, in two places — `submit_report` and
    `create_report_new_version` — and this is that same operation, named, so a
    third caller reuses it instead of reimplementing a state transition.

    The semantics are deliberately narrow and are what make it safe to call on
    a reopen:

    * the ASSIGNMENT is untouched — no row is created and none is deleted, so
      who reviews this order does not change;
    * only the *current operational decision* is cleared. `REJECTED` is not
      used: the previous cycle was not rejected, it is simply no longer
      current;
    * the historical evidence of the previous decision lives in `audit_log`
      (`REPORT.APPROVE`) and in the order timeline (`REPORT_APPROVED`), which
      this never touches. `report_review` only ever held the LATEST decision
      per reviewer — that predates 1.3.1 and is why the durable record is kept
      elsewhere.

    Every row for the order is reset rather than just the one belonging to
    whoever acted, because a reopened report has to be reconsidered by every
    reviewer participating in its cycle.
    """
    reviews = session.exec(
        select(ReportReview).where(ReportReview.order_id == order_id)
    ).all()
    for review in reviews:
        review.status = ReviewStatus.PENDING
        review.decision_at = None
        session.add(review)
    return len(reviews)


@router.post("/{report_id}/reopen", response_model=ReportActionResponse)
def reopen_report(
    report_id: str,
    data: ReportReopenRequest,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Céluma 1.3.1 Block B / CEL-131-03 — reopen an approved report that has
    NOT been signed (APPROVED → DRAFT).

    The supported correction path when a mistake is found after approval but
    before the signature: a wrong letterhead, a signature setting that should
    not have been enabled, a clinical detail the reviewer wants rewritten. It
    replaces the only alternative laboratories had, which was to publish a
    document they knew was wrong and then retract it.

    Deliberately NOT an amendment mechanism. A signed report is refused here
    in every case — editing or amending one belongs to the Céluma 1.4
    amendment workflow, and the guard is signature evidence on the report's
    versions, not `report.status` alone (see `authorize_reopen`).

    Reopening confers no other authority. The report re-enters the ordinary
    lifecycle at DRAFT and must travel submit → IN_REVIEW → approve before it
    can be signed again; the reopener does not gain the right to approve or
    sign it, and an administrator never had it.

    Céluma 1.4 (#13): the actor confirms with their own current password,
    verified after every authorization/lifecycle guard and before anything
    is written. The order lock serializes concurrent reopens: the second one
    re-reads DRAFT and is refused, so exactly one transition and one event
    are recorded.
    """
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")

    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report does not belong to your tenant")

    order_lifecycle.lock_report_order(session, report)

    try:
        current_version = authorize_reopen(session, report, user)
    except ReportAlreadySignedError as exc:
        raise HTTPException(409, exc.message) from None
    except ReportStateError as exc:
        raise HTTPException(400, exc.message) from None
    except ReportAuthorizationError as exc:
        raise HTTPException(403, exc.message) from None

    require_password_confirmation(
        session,
        user=user,
        report=report,
        attempted_action="REPORT.REOPEN",
        password=data.password,
    )

    old_status = report.status
    report.status = ReportStatus.DRAFT
    session.add(report)

    # Nothing else is reset. Each related field was considered against the
    # current lifecycle code; the full disposition table is in
    # docs/celuma-1.3.1/block-b/lifecycle-contract.md §4. In short:
    #
    #   * `ReportReview` rows keep their ASSIGNMENT and lose their stale
    #     DECISION — see `_reset_order_reviews_to_pending` immediately below.
    #     Block B originally preserved the decision too, reasoning that
    #     `submit_report` resets it on the next submission anyway. Manual
    #     validation found the gap that leaves: between the reopen and that
    #     next submission the reviewer is still rendered with the green
    #     APPROVED check, which states that the report currently carries an
    #     approval it no longer has. The approval is reversed the moment the
    #     report is reopened, so the decision is cleared at the same moment.
    #   * PDF generation state on the version is left alone. It is the record
    #     of an artifact that really was produced, and `sign-and-publish`
    #     regenerates with `force=True`, so a stale PDF can never be the one
    #     published.
    #   * `published_at` is necessarily NULL here (a published report is
    #     refused above) and is never written by this route.
    #   * The presentation settings and the frozen letterhead stay on the
    #     current version. Reopening returns the report to DRAFT, where the
    #     assigned reviewer may adjust presentation directly (remediation R1
    #     added DRAFT to `PRESENTATION_EDITABLE_STATUSES`; Block B was written
    #     when that window was IN_REVIEW-only). The correction path is
    #     reopen → author edits / reviewer adjusts presentation → resubmit →
    #     approve.

    # Céluma 1.3.1 manual-validation remediation (R9): the reopened report is
    # no longer approved, so no reviewer may still be recorded as having
    # approved it. Reuses the workflow's canonical reset rather than a second
    # implementation, and runs BEFORE the audit record so the count it reports
    # is the one that was actually applied.
    reviews_reset = _reset_order_reviews_to_pending(session, report.order_id)

    _create_audit_log(
        session=session,
        tenant_id=ctx.tenant_id,
        branch_id=str(report.branch_id),
        actor_user_id=ctx.user_id,
        action="REPORT.REOPEN",
        entity_type="report",
        entity_id=report_id,
        old_values={"status": old_status},
        new_values={
            "status": report.status,
            "reopened_version_no": current_version.version_no,
            "changelog": data.changelog,
            # R9: how many reviewer decisions this reopen invalidated. The
            # decisions themselves stay in the trail as their own
            # REPORT.APPROVE rows — this only records that they stopped being
            # current, and when.
            "reviews_reset_to_pending": reviews_reset,
            # Céluma 1.4 (#13): canonical correlation, shared with the
            # timeline event below. Reopening for amendment is a different
            # kind (and a different action), never this one.
            "reopen_kind": REOPEN_KIND_UNSIGNED,
            "report_version_id": str(current_version.id),
            "password_confirmed": True,
            **amendment_context(*_amendment_scope(session, report)),
        },
    )

    # Céluma 1.3.1 manual-validation remediation (R2, CEL-131-03): the
    # user-facing timeline event.
    #
    # Block B recorded the reopen in `audit_log` only, and documented the
    # resulting gap: `OrderEvent.event_type` is a native Postgres enum
    # (`public.eventtype`) with no `REPORT_REOPENED` member, and adding one
    # means `ALTER TYPE … ADD VALUE`, which Postgres cannot reverse — there is
    # no DROP VALUE, so the label would outlive any downgrade. Manual
    # validation showed the gap is not acceptable to the workflow: every other
    # report transition appears in the order timeline and this one silently
    # did not.
    #
    # The release owner's decision is to reuse the enum's existing generic
    # member rather than change the enum. `STATUS_CHANGED` is declared in
    # `EventType` as "Legacy/generic" and is written by no other code path, so
    # it carries no historical rows whose rendering could change. What makes
    # the event unambiguous is the metadata, not the type: `action` names the
    # transition and `from_status`/`to_status` carry it, so the timeline (and
    # any future consumer) identifies a reopen structurally instead of by
    # parsing the description.
    #
    # Exactly one row, inside the same transaction as the status change and
    # the audit record, so a failed or unauthorized reopen — every one of
    # which raises before this point — writes neither.
    from app.models.events import OrderEvent
    from app.models.enums import EventType

    reopen_event = OrderEvent(
        tenant_id=report.tenant_id,
        branch_id=report.branch_id,
        order_id=report.order_id,
        event_type=EventType.STATUS_CHANGED,
        # Unlike the REPORT_* events, whose text the UI builds from the type,
        # a generic type cannot be rendered from the type alone — so this one
        # carries its own human-readable text as the last-resort fallback for
        # any consumer that does not know the `action` key.
        description="Reporte reabierto",
        event_metadata={
            "action": ACTION_REPORT_REOPENED,
            "reopen_kind": REOPEN_KIND_UNSIGNED,
            "report_id": str(report.id),
            "report_title": report.title,
            # `ReportStatus` is a `(str, Enum)`, whose `str()` renders as
            # "ReportStatus.DRAFT" on Python 3.12. The timeline stores the
            # bare value, matching what `audit_log` records for the same
            # transition.
            "from_status": getattr(old_status, "value", old_status),
            "to_status": getattr(report.status, "value", report.status),
            "report_version_id": str(current_version.id),
            "reopened_version_no": current_version.version_no,
            "reopened_by": str(user.id),
            "reopened_by_name": user.full_name or user.username,
            "reason": data.changelog or None,
            "reviews_reset_to_pending": reviews_reset,
            "confirmation": "password",
            **amendment_context(*_amendment_scope(session, report)),
        },
        created_by=user.id,
    )
    session.add(reopen_event)

    session.commit()
    session.refresh(report)

    logger.info(
        f"Report {report_id} reopened to DRAFT by user {ctx.user_id}",
        extra={
            "event": "report.reopen",
            "report_id": report_id,
            "user_id": ctx.user_id,
            "previous_status": old_status,
        },
    )

    return ReportActionResponse(
        id=str(report.id),
        status=report.status,
        message=(
            "Reporte reabierto. Vuelve a borrador y debe enviarse a revisión "
            "y aprobarse de nuevo antes de poder firmarse."
        ),
    )


# ---------------------------------------------------------------------------
# Céluma 1.4 (#13): amendments of signed reports
# ---------------------------------------------------------------------------

def _amendment_refusal(exc: ReportAuthorizationError) -> HTTPException:
    if isinstance(exc, AmendmentLegacyOriginalError):
        return HTTPException(
            409, {"code": report_amendments.LEGACY_ORIGINAL_CODE, "message": exc.message}
        )
    if isinstance(exc, ReportStateError):
        return HTTPException(400, exc.message)
    return HTTPException(403, exc.message)


@router.post("/{report_id}/amendments", response_model=ReportAmendmentActionResponse)
def open_report_amendment(
    report_id: str,
    data: ReportAmendmentOpenRequest,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Reopen a PUBLISHED report for amendment ("reopening after signature").

    Creates the amendment record and its first draft version — a complete copy
    of the delivered emission's content and frozen snapshot (D1), without the
    signer's image URL or delivery date, carrying the server-owned
    identification block (D2) — and returns the report to DRAFT (D6). The
    source version, its JSON, PDF and signature evidence are untouched, and it
    stays the delivered emission until the amendment republishes (D3).

    Actors: the assigned reviewer or admin/superuser (`reports:manage_templates`),
    with their own current password and a required reason; neither grants
    approval or signing. V2 originals only (D5). Exactly once: the order lock
    re-reads the state and a partial unique index allows one open amendment.
    """
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report does not belong to your tenant")

    order_lifecycle.lock_report_order(session, report)

    try:
        source = authorize_amendment_opening(session, report, user)
    except ReportAuthorizationError as exc:
        raise _amendment_refusal(exc) from None

    source_body = _version_body(session, source)
    if source_body is None:
        raise HTTPException(
            409,
            "No se pudo leer la emisión firmada de este reporte; no se abrió la enmienda.",
        )

    require_password_confirmation(
        session,
        user=user,
        report=report,
        attempted_action=AUDIT_REOPEN_FOR_AMENDMENT,
        password=data.password,
    )

    now = datetime.utcnow()
    reviews = session.exec(
        select(ReportReview).where(ReportReview.order_id == report.order_id)
    ).all()
    amendment = ReportAmendment(
        tenant_id=report.tenant_id,
        report_id=report.id,
        amendment_no=report_amendments.next_amendment_no(session, report),
        source_version_id=source.id,
        category=data.category,
        reason=data.reason,
        requested_by=user.id,
        requested_at=now,
        reviews_at_opening=[
            {
                "review_id": str(r.id),
                "status": getattr(r.status, "value", r.status),
                "decision_at": r.decision_at.isoformat() if r.decision_at else None,
            }
            for r in reviews
        ],
    )
    session.add(amendment)
    session.flush()

    # D1: a complete re-emission. The new signer's image and the delivery
    # date are set again at signing, never inherited from the original.
    body = copy.deepcopy(source_body)
    body.pop(report_amendments.AMENDMENT_KEY, None)
    signature_metadata = body.get(SIGNATURE_METADATA_KEY)
    if isinstance(signature_metadata, dict):
        signature_metadata.pop("signature_url", None)
    snapshot = body.get("rendering_snapshot")
    body = apply_authoritative_report_metadata(
        session,
        body,
        session.get(Order, report.order_id),
        template=snapshot.get("template") if isinstance(snapshot, dict) else report.template,
    )
    body[report_amendments.AMENDMENT_KEY] = report_amendments.identification_block(amendment, source)

    next_version_no = (
        max(
            session.exec(
                select(ReportVersion.version_no).where(ReportVersion.report_id == report.id)
            ).all()
        )
        + 1
    )
    s3 = S3Service()
    key = f"reports/{report.tenant_id}/{report.branch_id}/{report.id}/versions/{next_version_no}/report.json"
    info = s3.upload_bytes(
        json.dumps(body, ensure_ascii=False).encode("utf-8"), key=key, content_type="application/json"
    )
    storage = StorageObject(
        provider="aws",
        region=s3.region,
        bucket=info.bucket,
        object_key=info.key,
        version_id=info.version_id,
        etag=info.etag,
        content_type="application/json",
        size_bytes=info.size_bytes,
        created_by=user.id,
        tenant_id=report.tenant_id,
    )
    session.add(storage)
    session.flush()
    record_storage_delta_with_thresholds(
        session,
        report.tenant_id,
        storage.size_bytes or 0,
        source="report_json",
        resource_type="report_json",
        actor_id=user.id,
    )

    for version in session.exec(
        select(ReportVersion).where(
            ReportVersion.report_id == report.id, ReportVersion.is_current == True  # noqa: E712
        )
    ).all():
        version.is_current = False
        session.add(version)
    draft = ReportVersion(
        report_id=report.id,
        version_no=next_version_no,
        json_storage_id=storage.id,
        authored_by=user.id,
        authored_at=now,
        is_current=True,
        schema_version=2,
        template_version_id=source.template_version_id,
        generated_by_renderer_version=source.generated_by_renderer_version,
        letterhead_version_id=source.letterhead_version_id,
        changelog=f"Enmienda {amendment.amendment_no}",
        amendment_id=amendment.id,
    )
    session.add(draft)

    old_status = report.status
    report.status = ReportStatus.DRAFT
    session.add(report)
    # A new review cycle starts (R9 semantics); the decisions on the delivered
    # emission are kept on the amendment and restored if it is cancelled.
    reviews_reset = _reset_order_reviews_to_pending(session, report.order_id)
    session.flush()

    _create_audit_log(
        session=session,
        tenant_id=ctx.tenant_id,
        branch_id=str(report.branch_id),
        actor_user_id=ctx.user_id,
        action=AUDIT_REOPEN_FOR_AMENDMENT,
        entity_type="report",
        entity_id=report_id,
        old_values={"status": old_status, "delivered_version_id": str(source.id)},
        new_values={
            "status": report.status,
            **report_amendments.correlation(amendment, source),
            "report_version_id": str(draft.id),
            "version_no": draft.version_no,
            "category": amendment.category,
            "reason": amendment.reason,
            "reviews_reset_to_pending": reviews_reset,
            "password_confirmed": True,
        },
    )
    session.add(build_amendment_reopen_event(report, amendment, source, draft, user, old_status=old_status))

    # D6: the order leaves Cerrada/Liberada while the amendment is worked on.
    if report.order_id:
        update_order_status_for_report(str(report.order_id), session)

    session.commit()
    session.refresh(report)
    session.refresh(amendment)

    logger.info(
        "Report reopened for amendment",
        extra={
            "event": "report.amendment_opened",
            "report_id": report_id,
            "amendment_id": str(amendment.id),
            "user_id": ctx.user_id,
        },
    )
    return ReportAmendmentActionResponse(
        id=str(report.id),
        status=report.status,
        message=(
            f"Enmienda {amendment.amendment_no} abierta. La emisión firmada sigue "
            "vigente hasta que la enmienda se firme."
        ),
        amendment=_amendment_info(session, amendment),
        version_no=next_version_no,
    )


@router.post(
    "/{report_id}/amendments/{amendment_id}/cancel",
    response_model=ReportAmendmentActionResponse,
)
def cancel_report_amendment(
    report_id: str,
    amendment_id: str,
    data: ReportAmendmentCancelRequest,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Cancel the open amendment (D3): the report returns to PUBLISHED with
    its delivered emission current again, the review decisions it had are
    restored, and the order returns to its completed state. Nothing signed
    is rewritten; the amendment's draft versions stay as history.

    Same actors and password rule as opening it, plus a required reason.
    Refused while a signature of the amendment is in progress.
    """
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report does not belong to your tenant")

    order_lifecycle.lock_report_order(session, report)

    try:
        amendment = authorize_amendment_cancellation(session, report, user)
    except ReportAuthorizationError as exc:
        raise _amendment_refusal(exc) from None
    if str(amendment.id) != amendment_id:
        raise HTTPException(
            409,
            {
                "code": report_amendments.NO_OPEN_AMENDMENT_CODE,
                "message": "Esa enmienda ya no está abierta. Actualiza el reporte.",
            },
        )

    current = session.exec(
        select(ReportVersion).where(
            ReportVersion.report_id == report.id, ReportVersion.is_current == True  # noqa: E712
        ).with_for_update().execution_options(populate_existing=True)
    ).first()
    if current is not None and current.publish_started_at is not None:
        stale_after = timedelta(seconds=settings.report_publish_timeout_seconds * 3)
        if datetime.utcnow() - current.publish_started_at < stale_after:
            raise HTTPException(
                409,
                {
                    "code": report_amendments.PUBLISH_IN_PROGRESS_CODE,
                    "message": "La enmienda se está firmando; no puede cancelarse ahora.",
                },
            )

    require_password_confirmation(
        session,
        user=user,
        report=report,
        attempted_action=AUDIT_AMENDMENT_CANCEL,
        password=data.password,
    )

    source = session.get(ReportVersion, amendment.source_version_id)
    now = datetime.utcnow()
    amendment.state = AMENDMENT_STATE_CANCELLED
    amendment.cancelled_by = user.id
    amendment.cancelled_at = now
    amendment.cancel_reason = data.reason
    session.add(amendment)

    for version in session.exec(
        select(ReportVersion).where(
            ReportVersion.report_id == report.id, ReportVersion.is_current == True  # noqa: E712
        )
    ).all():
        version.is_current = False
        session.add(version)
    source.is_current = True
    session.add(source)

    restored = 0
    for entry in amendment.reviews_at_opening or []:
        review = session.get(ReportReview, entry.get("review_id"))
        if review is None or review.order_id != report.order_id:
            continue
        review.status = ReviewStatus(entry["status"])
        review.decision_at = (
            datetime.fromisoformat(entry["decision_at"]) if entry.get("decision_at") else None
        )
        session.add(review)
        restored += 1

    old_status = report.status
    report.status = ReportStatus.PUBLISHED
    session.add(report)
    session.flush()

    _create_audit_log(
        session=session,
        tenant_id=ctx.tenant_id,
        branch_id=str(report.branch_id),
        actor_user_id=ctx.user_id,
        action=AUDIT_AMENDMENT_CANCEL,
        entity_type="report",
        entity_id=report_id,
        old_values={"status": old_status, "amendment_state": "OPEN"},
        new_values={
            "status": report.status,
            "amendment_state": amendment.state,
            **report_amendments.correlation(amendment, source),
            "report_version_id": str(source.id),
            "version_no": source.version_no,
            "reason": amendment.cancel_reason,
            "reviews_restored": restored,
            "password_confirmed": True,
        },
    )
    session.add(build_amendment_cancel_event(report, amendment, source, user, old_status=old_status))

    if report.order_id:
        update_order_status_for_report(str(report.order_id), session)

    session.commit()
    session.refresh(report)
    session.refresh(amendment)

    logger.info(
        "Report amendment cancelled",
        extra={
            "event": "report.amendment_cancelled",
            "report_id": report_id,
            "amendment_id": str(amendment.id),
            "user_id": ctx.user_id,
        },
    )
    return ReportAmendmentActionResponse(
        id=str(report.id),
        status=report.status,
        message=(
            f"Enmienda {amendment.amendment_no} cancelada. La emisión firmada "
            f"(versión {source.version_no}) sigue siendo la vigente."
        ),
        amendment=_amendment_info(session, amendment),
        version_no=source.version_no,
    )


@router.patch("/{report_id}/presentation", response_model=ReportPresentationResponse)
def update_report_presentation(
    report_id: str,
    data: ReportPresentationUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Céluma 1.3.1 Block A / A4-A5 — the reviewer-only presentation route.

    Changes ONLY the signature-section toggle, the digital-signature toggle,
    and the selected letterhead, and only while the report is DRAFT or
    IN_REVIEW, and only for the assigned reviewer.

    Why this endpoint exists at all: these three settings change the final
    clinical document, so they are reviewer decisions — but they live inside
    the report JSON body, which before 1.3.1 was reachable only through
    `POST /{id}/new_version` and its `reports:edit` guard. The reviewer role
    deliberately does not hold `reports:edit` and must not be given it (that
    would let a reviewer rewrite clinical content), so the narrow route is
    the mechanism that keeps the capability and the content boundary
    separate. See `app/services/report_presentation.py`.

    Lifecycle (A5, amended by the manual-validation remediation R1):
      * DRAFT     — the assigned reviewer's window too. Block A excluded it;
                    real use showed presentation is reviewer-owned from the
                    moment the report exists, not only after submission.
      * IN_REVIEW — the reviewer's window. Unchanged.
      * APPROVED  — frozen. 409. The 1.3.1 route back is Block B's reopen,
                    never a silent mutation of an approved document.

    Admitting DRAFT changes nothing about approval or signing: those read
    their own lifecycle tuples in `report_authorization.py`, and neither
    accepts DRAFT. The author boundary is likewise untouched — a content-path
    save still cannot write these fields (`enforce_author_presentation_boundary`
    carries the persisted values forward), so what a reviewer configures in
    DRAFT survives the author's next save instead of being overwritten by it.
    """
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    if str(report.tenant_id) != ctx.tenant_id:
        # 404, not 403: never confirm another laboratory's report id.
        raise HTTPException(404, "Report not found")

    try:
        authorize_presentation_change(session, report, user)
    except ReportStateError as exc:
        raise HTTPException(409, exc.message) from None
    except ReportAuthorizationError as exc:
        raise HTTPException(403, exc.message) from None

    current_version = session.exec(
        select(ReportVersion).where(
            ReportVersion.report_id == report.id,
            ReportVersion.is_current == True,  # noqa: E712
        )
    ).first()
    if not current_version:
        raise HTTPException(404, "No current version found for this report")

    # Céluma 1.4 (#13): a signed version's JSON is an issued artifact.
    if is_signed_version(current_version):
        raise HTTPException(409, "Cannot change the presentation of a signed report version")

    # Céluma 1.4 (#22): a legacy report's stored JSON is never rewritten.
    require_v2_authoring(report, current_version)

    old_values = {
        "letterhead_version_id": (
            str(current_version.letterhead_version_id)
            if current_version.letterhead_version_id
            else None
        ),
    }

    try:
        effective = apply_presentation_change(
            session,
            report,
            current_version,
            user,
            show_signature_section=data.show_signature_section,
            require_digital_signature=data.require_digital_signature,
            letterhead_version_id=data.letterhead_version_id,
        )
    except PresentationLetterheadNotFoundError as exc:
        raise HTTPException(404, exc.message) from None
    except PresentationLetterheadError as exc:
        raise HTTPException(409, exc.message) from None
    except PresentationContentMissingError as exc:
        raise HTTPException(409, exc.message) from None

    _create_audit_log(
        session=session,
        tenant_id=ctx.tenant_id,
        branch_id=str(report.branch_id),
        actor_user_id=ctx.user_id,
        action="REPORT.PRESENTATION_UPDATE",
        entity_type="report",
        entity_id=report_id,
        old_values=old_values,
        new_values=effective,
    )

    session.commit()
    session.refresh(report)

    logger.info(
        f"Report {report_id} presentation updated by reviewer {ctx.user_id}",
        extra={
            "event": "report.presentation_update",
            "report_id": report_id,
            "user_id": ctx.user_id,
        },
    )

    return ReportPresentationResponse(
        id=str(report.id),
        status=report.status,
        **effective,
    )


def _require_amendment_signable(
    session: Session, report: Report, version: ReportVersion
) -> None:
    """Céluma 1.4 (#13): inside an open amendment, the version being signed
    must be the amendment's own draft and carry the author's change note."""
    amendment, _ = _amendment_scope(session, report)
    if amendment is None:
        return
    if version.amendment_id != amendment.id:
        raise HTTPException(409, "The current version does not belong to the open amendment.")
    _require_change_note(session, version)


def _record_publication(
    session: Session,
    ctx: AuthContext,
    user: AppUser,
    report: Report,
    version: ReportVersion,
    *,
    old_status,
    changelog: str | None,
    audit_action: str,
):
    """Céluma 1.4 (#13): the single audit row, timeline row, order update and
    notification of a successful publication, staged right after
    `finalize_publish` in its transaction. Inside an open amendment this is a
    REPUBLICATION: the amendment closes as PUBLISHED and its signed version
    becomes the delivered emission, superseding `source` (D1/D3); recipients
    are told in-app only (D4)."""
    amendment, source = _amendment_scope(session, report)
    if amendment is not None:
        amendment.state = AMENDMENT_STATE_PUBLISHED
        amendment.published_version_id = version.id
        amendment.published_at = report.published_at
        session.add(amendment)

    _create_audit_log(
        session=session,
        tenant_id=ctx.tenant_id,
        branch_id=str(report.branch_id),
        actor_user_id=ctx.user_id,
        action=audit_action,
        entity_type="report",
        entity_id=str(report.id),
        old_values={"status": old_status},
        new_values={
            "status": report.status,
            "signed_by": str(user.id),
            "changelog": changelog,
            # `signed_at` is the stored signature instant (the claim time, as
            # on the version and in the PDF), not `published_at`.
            **publication_correlation(version, amendment, source),
            "password_confirmed": True,
        },
    )

    sign_event = build_publication_event(
        report, version, user, old_status=old_status, changelog=changelog,
        amendment=amendment, source=source,
    )
    session.add(sign_event)

    # Order status follows the report (D6): back to Cerrada/Liberada.
    if report.order_id:
        update_order_status_for_report(str(report.order_id), session)

    order = session.get(Order, report.order_id) if report.order_id else None
    if amendment is not None:
        notify_report_republished(
            session, report=report, order=order, actor=user,
            occurrence_marker=str(sign_event.id),
        )
    else:
        notify_report_published(
            session, report=report, order=order, actor=user,
            occurrence_marker=str(sign_event.id),
        )
    return sign_event


@router.post("/{report_id}/sign", response_model=ReportActionResponse)
def sign_report(
    report_id: str,
    data: ReportSignRequest,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Sign and publish a report (APPROVED → PUBLISHED) — requires the full
    reviewer contract: `reports:sign` + 'reviewer' role + assignment.

    Céluma 1.3.1 Block A / A3 added the assignment half. The role and
    permission halves were already correct (which is why superuser, holding
    `reports:sign` but not the role, was already rejected), but any reviewer
    in the tenant could sign any approved report — including one they were
    never assigned to.

    Céluma 1.4 (#13): the signer confirms with their own current password,
    and this route now runs exactly the publication sequence of
    `sign-and-publish` — row-locked claim, regeneration, `finalize_publish`
    re-verification under the lock — so a retried or concurrent call can no
    longer publish (or record the publication) twice.
    """
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")

    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report does not belong to your tenant")

    try:
        authorize_signing(session, report, user)
    except ReportStateError as exc:
        raise HTTPException(400, exc.message) from None
    except ReportAuthorizationError as exc:
        raise HTTPException(403, exc.message) from None

    current_version = session.exec(
        select(ReportVersion).where(
            ReportVersion.report_id == report.id,
            ReportVersion.is_current == True,  # noqa: E712
        )
    ).first()
    if not current_version:
        raise HTTPException(404, "No current version found for this report")

    # Céluma 1.4 (#22): a legacy report cannot be signed anew.
    require_v2_authoring(report, current_version)

    # Céluma 1.3 Phase 2, Block E: a report cannot be published without a
    # validated, hashed, persisted official PDF for the version being
    # published — see pdf-publication-workflow.md. This two-step route keeps
    # that precondition (generation is the explicit first step); it then
    # regenerates from the signed state below (H-0c Blocker B).
    if current_version.pdf_generation_status != "READY":
        raise HTTPException(
            422,
            "Cannot sign: this report version has no generated PDF yet. "
            "Generate the official PDF before publishing.",
        )

    _require_amendment_signable(session, report, current_version)

    require_password_confirmation(
        session,
        user=user,
        report=report,
        attempted_action="REPORT.SIGN",
        password=data.password,
    )

    report, version = load_locked_version(session, report_id, current_version.version_no)
    if not report or not version:
        raise HTTPException(404, "Report or version not found")

    try:
        claim_publish(session, version, user.id)
    except ReportPublishAlreadyInProgressError as exc:
        raise HTTPException(409, exc.message) from None

    try:
        embed_signature_metadata_if_required(session, report_id, version, user)
        # Céluma 1.3.1 Block D: delivery_date exists for the first time at
        # this instant, whether or not a signature image is required.
        embed_delivery_date_at_signing(session, report_id, version, user)
        # H-0c Blocker B (§4): never publish a PDF generated BEFORE signing.
        # Regenerate from the just-signed state with `force=True`, inside the
        # claim, exactly as `sign-and-publish` does.
        pdf_service = ReportPdfGenerationService(session)
        version = pdf_service.generate(report, version, user.id, force=True)
    except (ReportPdfAlreadyInProgressError, ReportPdfImmutableError) as exc:
        clear_publish_claim(session, version)
        raise HTTPException(409, exc.message) from None
    except ReportPdfGenerationError as exc:
        clear_publish_claim(session, version)
        raise HTTPException(422, exc.message) from None
    except ReportPublishError as exc:
        clear_publish_claim(session, version)
        raise HTTPException(422, exc.message) from None
    except Exception:
        clear_publish_claim(session, version)
        raise

    report, version = load_locked_version(session, report_id, version.version_no)
    old_status = report.status
    try:
        finalize_publish(session, report, version, user.id, data.changelog)
    except ReportPublishConflictError as exc:
        clear_publish_claim(session, version)
        raise HTTPException(409, exc.message) from None

    # Céluma 1.3 Phase 3, Block F. This is the legacy two-step `/sign`
    # endpoint, kept for compatibility; `/sign-and-publish` below is what the
    # UI calls. Both reach PUBLISHED and both record and notify the same way,
    # because a report published through either path is equally published.
    _record_publication(
        session, ctx, user, report, version,
        old_status=old_status, changelog=data.changelog, audit_action="REPORT.SIGN",
    )

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


@router.post("/{report_id}/sign-and-publish", response_model=ReportSignAndPublishResponse)
def sign_and_publish_report(
    report_id: str,
    data: ReportSignRequest,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Second post-Phase-2 remediation (UX): "Sign and publish" as a
    single product action (requires reports:sign + reviewer role),
    replacing the two-button flow "Generate official PDF" -> "Sign and
    Publish" in the normal UI.

    Orchestrates, under a claim that serializes concurrent attempts
    (`publish_started_at`/`publish_started_by` on `ReportVersion`, same
    staleness pattern as PDF generation):
      1. Embeds the digital signature in the persisted JSON IF the report
         requires it (before generating the PDF, not after — see
         signed-pdf-publication-workflow.md on why the previous order was
         a conceptual bug: the "official" PDF never reflected the truly
         signed state).
      2. Generates (forcing regeneration: `force=True`) the official PDF
         already reflecting that signed state.
      3. If both steps succeed, sets signed_by/signed_at and
         report.status=PUBLISHED atomically. If anything fails, the claim
         is released, the report stays APPROVED, and the operation is
         retryable — never a signed-but-unpublished report, nor an
         official PDF without the signature.

    `POST .../generate-pdf` and `POST .../sign` (above) remain intact for
    compatibility/internal use — this endpoint is the only one the main
    UI invokes.
    """
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    if str(report.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report does not belong to your tenant")

    # Céluma 1.3.1 Block A / A3: role + `reports:sign` + assignment. The
    # assignment half is new; see `app/services/report_authorization.py`.
    try:
        authorize_signing(session, report, user)
    except ReportStateError as exc:
        raise HTTPException(400, exc.message) from None
    except ReportAuthorizationError as exc:
        raise HTTPException(403, exc.message) from None

    current_version = session.exec(
        select(ReportVersion).where(
            ReportVersion.report_id == report.id,
            ReportVersion.is_current == True,
        )
    ).first()
    if not current_version:
        raise HTTPException(404, "No current version found for this report")

    # Céluma 1.4 (#22): a legacy report cannot be signed anew.
    require_v2_authoring(report, current_version)

    _require_amendment_signable(session, report, current_version)

    # Céluma 1.4 (#13): every guard above has passed and nothing has been
    # written yet — the claim below is the first write.
    require_password_confirmation(
        session,
        user=user,
        report=report,
        attempted_action="REPORT.SIGN_AND_PUBLISH",
        password=data.password,
    )

    report, version = load_locked_version(session, report_id, current_version.version_no)
    if not report or not version:
        raise HTTPException(404, "Report or version not found")

    try:
        claim_publish(session, version, user.id)
    except ReportPublishAlreadyInProgressError as exc:
        raise HTTPException(409, exc.message) from None

    try:
        embed_signature_metadata_if_required(session, report_id, version, user)
        # Céluma 1.3.1 Block D: see the equivalent comment in sign_report
        # above — delivery_date must be embedded before PDF generation,
        # regardless of whether a digital signature image is required.
        embed_delivery_date_at_signing(session, report_id, version, user)
        pdf_service = ReportPdfGenerationService(session)
        version = pdf_service.generate(report, version, user.id, force=True)
    except (ReportPdfAlreadyInProgressError, ReportPdfImmutableError) as exc:
        clear_publish_claim(session, version)
        raise HTTPException(409, exc.message) from None
    except ReportPdfGenerationError as exc:
        clear_publish_claim(session, version)
        raise HTTPException(422, exc.message) from None
    except ReportPublishError as exc:
        clear_publish_claim(session, version)
        raise HTTPException(422, exc.message) from None
    except Exception:
        clear_publish_claim(session, version)
        raise

    report, version = load_locked_version(session, report_id, version.version_no)
    old_status = report.status
    try:
        finalize_publish(session, report, version, user.id, data.changelog)
    except ReportPublishConflictError as exc:
        clear_publish_claim(session, version)
        raise HTTPException(409, exc.message) from None

    # Céluma 1.3 Phase 3, Block F: the single-action sign-and-publish path.
    # No REPORT_PDF_READY notification is emitted for the forced regeneration
    # this endpoint performs — that generation is an internal step of
    # publishing, not an invitation to sign something that is already being
    # signed. See notify_report_pdf_ready's docstring.
    _record_publication(
        session, ctx, user, report, version,
        old_status=old_status, changelog=data.changelog, audit_action="REPORT.SIGN_AND_PUBLISH",
    )

    session.commit()
    session.refresh(report)
    session.refresh(version)

    logger.info(
        f"Report {report_id} signed and published (single-action) by pathologist {ctx.user_id}",
        extra={
            "event": "report.sign_and_publish",
            "report_id": report_id,
            "user_id": ctx.user_id,
        },
    )

    return ReportSignAndPublishResponse(
        id=str(report.id),
        status=report.status,
        message="Reporte firmado y publicado",
        pdf_generation_status=version.pdf_generation_status,
        pdf_sha256=version.pdf_sha256,
        pdf_size_bytes=version.pdf_size_bytes,
        pdf_page_count=version.pdf_page_count,
        pdf_generated_at=version.pdf_generated_at,
        # Fifth remediation: the EXACT version whose official PDF was just
        # generated, so the UI does not have to derive it from a refreshed
        # `/full`. See sign-and-publish-response-contract.md.
        report_version_id=str(version.id),
        version_no=version.version_no,
        official_pdf_available=bool(
            version.pdf_storage_id and version.pdf_generation_status == "READY"
        ),
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

    # Céluma 1.3 Phase 3, Block F. `data.changelog` — the retraction reason —
    # is deliberately NOT forwarded: it is user-authored free text, the
    # template declares no parameter it could occupy, and `validate_params`
    # would reject it as `unknown_param` if anyone tried. It remains on the
    # OrderEvent above, where normal RBAC governs who can read it.
    notify_report_retracted(
        session,
        report=report,
        order=session.get(Order, report.order_id) if report.order_id else None,
        actor=user,
        occurrence_marker=str(retract_event.id),
    )

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
