"""Céluma 1.4 — reviewer AVAILABILITY for the review workflow.

A `ReportReview` row is an *assignment*; it says who was asked, not that they can
still act. The clinical contract (`report_authorization`) needs a user who is in
the tenant, active, holds the `reviewer` role AND `reports:approve`. A row whose
user lost any of that (an old signer later made an administrator, a deactivated
account, a revoked role) can never approve, reject or sign.

Two rules live here so that no route re-implements them:

  * entering review needs at least ONE assigned reviewer who is eligible NOW
    (`require_eligible_reviewer`), otherwise the report would sit IN_REVIEW with
    nobody able to act;
  * while a report is IN_REVIEW or APPROVED (waiting for the signature) the
    assignment can be repaired or replaced but never emptied of eligible
    reviewers (`require_review_continuity`).

Nothing here grants authority: administrators are not reviewers, and an
explicit assignment is never replaced by the tenant default (that safety net
only acts on an order with NO review row at all — `report_default_reviewer`).
"""
from __future__ import annotations

from typing import Iterable, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.rbac import ROLE_REVIEWER, get_user_roles, has_permission
from app.models.enums import ReportStatus
from app.models.report import Report, ReportVersion
from app.models.report_review import ReportReview
from app.models.user import AppUser
from app.services.report_publishing import PUBLISH_IN_PROGRESS_CODE, publish_in_progress

REVIEWERS_UNAVAILABLE_CODE = "reviewers_unavailable"
REVIEW_CONTINUITY_CODE = "review_requires_eligible_reviewer"

#: Report states in which somebody must still be able to act as reviewer.
REVIEW_CONTINUITY_STATUSES = (ReportStatus.IN_REVIEW, ReportStatus.APPROVED)


def reviewer_ineligibility_reason(session: Session, user: Optional[AppUser], tenant_id) -> Optional[str]:
    """Why this user cannot be (or stay) a reviewer, in Spanish; `None` when eligible."""
    if user is None or str(user.tenant_id) != str(tenant_id):
        return "no pertenece a este laboratorio"
    if not user.is_active:
        return "está inactivo"
    if ROLE_REVIEWER not in get_user_roles(user.id, session):
        return "no tiene el rol de revisor"
    if not has_permission(user.id, "reports:approve", session):
        return "no tiene el permiso para aprobar reportes"
    return None


def eligible_reviewer_ids(session: Session, user_ids: Iterable, tenant_id) -> set:
    ids = set(user_ids)
    if not ids:
        return set()
    users = {u.id: u for u in session.exec(select(AppUser).where(AppUser.id.in_(ids))).all()}
    return {uid for uid in ids if reviewer_ineligibility_reason(session, users.get(uid), tenant_id) is None}


def require_eligible_reviewer(session: Session, reviews: list[ReportReview], tenant_id) -> None:
    """409 unless at least one assigned reviewer is eligible right now."""
    if eligible_reviewer_ids(session, [r.reviewer_user_id for r in reviews], tenant_id):
        return
    raise HTTPException(
        409,
        {
            "code": REVIEWERS_UNAVAILABLE_CODE,
            "message": (
                "No se puede enviar a revisión: ningún revisor asignado a esta orden puede "
                "revisarla (ya no tiene el rol de revisor, está inactivo o pertenece a otro "
                "laboratorio). Pide a quien administra las asignaciones que asigne a la orden "
                "a un revisor activo del laboratorio y vuelve a enviar. Un administrador no "
                "puede aprobar ni firmar como revisor."
            ),
        },
    )


def require_review_continuity(session: Session, report: Optional[Report], tenant_id, remaining_user_ids: Iterable) -> None:
    """409 when the new reviewer set would leave an IN_REVIEW/APPROVED report with nobody eligible."""
    if report is None or report.status not in REVIEW_CONTINUITY_STATUSES:
        return
    if eligible_reviewer_ids(session, remaining_user_ids, tenant_id):
        return
    raise HTTPException(
        409,
        {
            "code": REVIEW_CONTINUITY_CODE,
            "message": (
                "El reporte está en revisión y esta orden no puede quedarse sin un revisor "
                "activo. Para cambiar la asignación, incluye al menos a un revisor activo "
                "del laboratorio (reemplázalo en lugar de quitarlo)."
            ),
        },
    )


def refuse_while_signing(session: Session, report: Optional[Report]) -> None:
    """409 while a signature of the report's current version is in progress.

    The order lock is released by the signature's claim commit before the slow, lock-free
    rendering, so it alone does not span that window: the fresh publish claim does. Same
    protocol as every other writer of what is being signed — the caller already holds the
    order lock; the current version row is locked and re-read here (order before version),
    so either the claim is visible and the change waits for nothing and is refused, or the
    signature has not claimed yet and, once it does, it re-authorizes against the new
    assignment. Without this, the signer could be replaced mid-render and still publish."""
    if report is None:
        return
    current = session.exec(
        select(ReportVersion)
        .where(ReportVersion.report_id == report.id, ReportVersion.is_current == True)  # noqa: E712
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if publish_in_progress(current):
        raise HTTPException(
            409,
            {
                "code": PUBLISH_IN_PROGRESS_CODE,
                "message": (
                    "El reporte se está firmando en este momento: no se pueden cambiar los "
                    "revisores hasta que termine. Espera y actualiza el reporte."
                ),
            },
        )
