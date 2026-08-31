"""Céluma 1.3 Phase 2, Block E: internal report-render data endpoint.

Serves the exact JSON envelope the frontend's `/internal/report-render/...`
route needs to reproduce a report with `ReportRendererResolver`, for a
headless Chromium instance driven by `ReportPdfGenerationService`.

Deliberately NOT mounted on `reports_router` (which app/main.py wraps in
`dependencies=[Depends(current_user)]` for every route) and NOT protected by
`current_user` — a headless browser rendering one report_version_id for a
few seconds has no normal user session. Authorization is a short-lived,
narrow-purpose render token (see app/core/security.py) instead.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlmodel import Session, select
import logging

from app.core.db import get_session
from app.core.security import verify_render_token
from app.models.report import Report, ReportVersion
from app.models.report_review import ReportReview
from app.models.user import AppUser
from app.schemas.report import ReportDetailResponse
from app.api.v1.reports import _build_report_detail_response

logger = logging.getLogger(__name__)


class SignerLookupItem(BaseModel):
    id: str
    name: str


class InternalRenderDataResponse(ReportDetailResponse):
    """Same envelope shape used everywhere else, plus the minimal reviewer
    id->name lookup the renderers need to display a real signer name in the
    signature block (see report_editor.tsx's `signerLookup` prop). Only id
    and name are exposed here — no email/avatar — since this endpoint is
    reachable with nothing but a short-lived render token."""

    signer_lookup: list[SignerLookupItem] = []

router = APIRouter(prefix="/reports/internal")
_scheme = HTTPBearer()


class RenderTokenContext:
    def __init__(self, report_version_id: str, tenant_id: str):
        self.report_version_id = report_version_id
        self.tenant_id = tenant_id


def require_render_token(
    credentials: HTTPAuthorizationCredentials = Depends(_scheme),
) -> RenderTokenContext:
    try:
        payload = verify_render_token(credentials.credentials)
    except ValueError as exc:
        raise HTTPException(401, str(exc))
    return RenderTokenContext(
        report_version_id=payload["report_version_id"], tenant_id=payload["tenant_id"]
    )


@router.get(
    "/render-data/{report_id}/{version_no}", response_model=InternalRenderDataResponse
)
def get_internal_render_data(
    report_id: str,
    version_no: int,
    session: Session = Depends(get_session),
    ctx: RenderTokenContext = Depends(require_render_token),
):
    """Return the render envelope for one specific report version.

    Defense in depth: never trusts the signed token's claims blindly for the
    response contents — reloads report/version fresh from the DB and
    cross-checks both the tenant and the exact version the token was minted
    for, so a token can only ever be used for the single (report_id,
    version_no) it was issued for.
    """
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
    if str(version.id) != ctx.report_version_id:
        # The token was minted for a different version than the one being
        # requested — never fall back to "closest match".
        raise HTTPException(403, "Render token does not match the requested version")

    reviews = session.exec(
        select(ReportReview).where(ReportReview.order_id == report.order_id)
    ).all()
    signer_lookup: list[SignerLookupItem] = []
    for review in reviews:
        reviewer = session.get(AppUser, review.reviewer_user_id)
        if reviewer:
            signer_lookup.append(SignerLookupItem(id=str(reviewer.id), name=reviewer.full_name))

    logger.info(
        "Internal render data served",
        extra={
            "event": "report_pdf.render_data_served",
            "tenant_id": ctx.tenant_id,
            "report_id": report_id,
            "report_version_id": str(version.id),
        },
    )
    detail = _build_report_detail_response(report, version, session)

    # H-0c Blocker B — the actual root cause of "the autograph is missing from
    # the official PDF".
    #
    # `sign-and-publish` runs: claim -> embed signature -> GENERATE PDF ->
    # finalize (which is what sets `signed_by`/`signed_at`). The official PDF
    # is therefore rendered in the window BEFORE the version is marked signed,
    # so this endpoint served `signed_at = None`. `SignatureBlock` derives
    # `isSigned` from `signed_at` and refuses to draw the autograph without it
    # — so the official PDF came out with no signature and no "Firmado
    # digitalmente el ..." caption, every time, deterministically. Reopening
    # the same report afterwards showed the autograph, because by then
    # finalize had run: exactly the reported local-vs-official asymmetry.
    #
    # The publish claim IS the signature intent: it records who is signing and
    # when, it is held across generation, and `finalize_publish` now stores
    # `publish_started_at` as `signed_at`. Reporting it here therefore does not
    # invent a signed state — it reports the one that is already committed to,
    # with the same identity and the same timestamp that will be persisted.
    #
    # Deliberately scoped to THIS endpoint: the public report API must keep
    # reporting a mid-publication report as unsigned until it truly is.
    signed_by = detail.signed_by
    signed_at = detail.signed_at
    if signed_at is None and version.publish_started_at and version.publish_started_by:
        signed_by = str(version.publish_started_by)
        signed_at = version.publish_started_at

    payload = detail.model_dump()
    payload["signed_by"] = signed_by
    payload["signed_at"] = signed_at
    return InternalRenderDataResponse(**payload, signer_lookup=signer_lookup)
