"""Report-transition notification integrations (Céluma 1.3, Phase 3, Block F).

Four events, four functions, one call site each. Each one is invoked *after*
its domain transition and audit/`OrderEvent` row are in the session and
*before* the caller commits, so the notification lands in the same atomic
commit as the transition (Story F9).

Occurrence markers
------------------
Three of the four derive from the persisted `OrderEvent` the endpoint already
writes. `OrderEvent.id` is a client-side `uuid4` default, so it exists the
moment the row is constructed — before flush — and it is a real business
occurrence rather than a request artefact. A retry of the same HTTP request
creates a *different* `OrderEvent`, but a retry only reaches this code after
the state guard (`report.status != DRAFT` -> 400) has already rejected it, so
the second notification is prevented by the domain, not by the marker. What
the marker guarantees is that reprocessing *one* occurrence — a caller
invoking the integration twice, a future replay — deduplicates.

`REPORT_PDF_READY` is the exception and is documented on its function.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from sqlmodel import Session

from app.models.laboratory import Order
from app.models.notification import NotificationResourceType, NotificationType
from app.models.report import Report
from app.models.user import AppUser
from app.schemas.notification import NotificationCommand
from app.services.notification import NotificationService
from app.services.notification_integrations.recipients import (
    resolve_report_pdf_ready_recipients,
    resolve_report_published_recipients,
    resolve_report_retracted_recipients,
    resolve_report_submitted_recipients,
)
from app.services.notification_templates import CURRENT_TEMPLATE_KEY

logger = logging.getLogger(__name__)


def _actor_name(actor: Optional[AppUser]) -> str:
    """A display name for `actor_name`, always non-empty.

    `full_name` is NOT NULL but can be blank in practice, and the template
    registry rejects an empty parameter — which would turn a cosmetic gap into
    a rejected notification. Falling back through username to a generic noun
    keeps the notification, which is the thing that matters.
    """
    if actor is None:
        return "Un usuario"
    return (actor.full_name or "").strip() or (actor.username or "").strip() or "Un usuario"


def notify_report_submitted(
    session: Session,
    *,
    report: Report,
    order: Optional[Order],
    actor: Optional[AppUser],
    occurrence_marker: str,
) -> Optional[UUID]:
    """A report moved DRAFT -> IN_REVIEW and needs review.

    Recipients: the order's reviewers, minus the submitting author.

    The marker is the `REPORT_SUBMITTED` `OrderEvent`'s id, **not**
    `report_version.id` — Block B's service contract §5 spells out why: a
    report can cycle `DRAFT -> IN_REVIEW -> DRAFT -> IN_REVIEW` on one
    `ReportVersion` row because `request_changes` creates no new version, and
    each pass is a legitimate, separately notifiable review request. Keying on
    the version would silently drop every cycle after the first.
    """
    if order is None:
        return None

    recipients = resolve_report_submitted_recipients(
        session, tenant_id=report.tenant_id, order_id=report.order_id
    )
    return NotificationService.notify(
        session,
        NotificationCommand(
            tenant_id=report.tenant_id,
            type=NotificationType.REPORT_SUBMITTED,
            resource_type=NotificationResourceType.REPORT,
            resource_id=report.id,
            occurrence_marker=occurrence_marker,
            template_key=CURRENT_TEMPLATE_KEY[NotificationType.REPORT_SUBMITTED],
            template_params={
                "order_number": order.order_code,
                "actor_name": _actor_name(actor),
            },
            recipient_user_ids=recipients,
            created_by=(actor.id if actor else None),
        ),
    )


def notify_report_pdf_ready(
    session: Session,
    *,
    report: Report,
    order: Optional[Order],
    version_id: UUID,
    actor: Optional[AppUser],
) -> Optional[UUID]:
    """The official PDF for a report version reached READY and can be signed.

    Recipients: the order's reviewers who hold `reports:sign`.

    **The marker is the `ReportVersion` id**, and this is the one event with no
    `OrderEvent` behind it — `ReportPdfGenerationService` writes no timeline
    row and commits internally. One `READY` occurrence per report version is
    the correct granularity, provable from the state machine:

    - a version that fails and is retried reaches READY once, and the earlier
      attempts were never "ready to sign";
    - `sign_and_publish` regenerates with `force=True`, which is generation
      *during* signing, not a new invitation to sign — and that path does not
      call this function at all;
    - editing the report creates a **new** `ReportVersion`, whose own READY is
      a genuinely new occurrence with a different marker.

    The endpoint additionally declines to call this when the version was
    already READY on entry, so an idempotent `generate-pdf` retry costs no work
    at all. The marker is the second line of defence, not the first.
    """
    if order is None:
        return None

    recipients = resolve_report_pdf_ready_recipients(
        session, tenant_id=report.tenant_id, order_id=report.order_id
    )
    return NotificationService.notify(
        session,
        NotificationCommand(
            tenant_id=report.tenant_id,
            type=NotificationType.REPORT_PDF_READY,
            resource_type=NotificationResourceType.REPORT,
            resource_id=report.id,
            occurrence_marker=str(version_id),
            template_key=CURRENT_TEMPLATE_KEY[NotificationType.REPORT_PDF_READY],
            template_params={"order_number": order.order_code},
            recipient_user_ids=recipients,
            created_by=(actor.id if actor else None),
        ),
    )


def notify_report_published(
    session: Session,
    *,
    report: Report,
    order: Optional[Order],
    actor: Optional[AppUser],
    occurrence_marker: str,
) -> Optional[UUID]:
    """A report was signed and published (APPROVED -> PUBLISHED).

    Recipients: the order's assignees plus the report author, minus the signer.

    The marker is the signing `OrderEvent`'s id. Publication is guarded to
    `status == APPROVED` and further serialized by the `publish_started_at`
    claim, so one `ReportVersion` publishes at most once per retract/republish
    cycle — but the event id is used rather than the version id anyway, so
    that a future republish after a retraction is a distinct occurrence
    instead of being silently suppressed as a duplicate of the first
    publication.
    """
    if order is None:
        return None

    recipients = resolve_report_published_recipients(
        session, tenant_id=report.tenant_id, order_id=report.order_id, report=report
    )
    return NotificationService.notify(
        session,
        NotificationCommand(
            tenant_id=report.tenant_id,
            type=NotificationType.REPORT_PUBLISHED,
            resource_type=NotificationResourceType.REPORT,
            resource_id=report.id,
            occurrence_marker=occurrence_marker,
            template_key=CURRENT_TEMPLATE_KEY[NotificationType.REPORT_PUBLISHED],
            template_params={
                "order_number": order.order_code,
                "actor_name": _actor_name(actor),
            },
            recipient_user_ids=recipients,
            created_by=(actor.id if actor else None),
        ),
    )


def notify_report_retracted(
    session: Session,
    *,
    report: Report,
    order: Optional[Order],
    actor: Optional[AppUser],
    occurrence_marker: str,
) -> Optional[UUID]:
    """A published report was withdrawn (PUBLISHED -> RETRACTED).

    Recipients: the same staff set as publication, minus the retracting actor.

    **The retraction reason is not passed and cannot be.** `data.changelog` is
    user-authored free text; the template declares only `order_number` and
    `actor_name`, so there is no parameter it could arrive through, and
    `validate_params` rejects an undeclared one (`unknown_param`). The reason
    still reaches the `OrderEvent` timeline, where normal RBAC governs who
    reads it — which is the point: the notification says *what happened*, the
    resource says *why*.
    """
    if order is None:
        return None

    recipients = resolve_report_retracted_recipients(
        session, tenant_id=report.tenant_id, order_id=report.order_id, report=report
    )
    return NotificationService.notify(
        session,
        NotificationCommand(
            tenant_id=report.tenant_id,
            type=NotificationType.REPORT_RETRACTED,
            resource_type=NotificationResourceType.REPORT,
            resource_id=report.id,
            occurrence_marker=occurrence_marker,
            template_key=CURRENT_TEMPLATE_KEY[NotificationType.REPORT_RETRACTED],
            template_params={
                "order_number": order.order_code,
                "actor_name": _actor_name(actor),
            },
            recipient_user_ids=recipients,
            created_by=(actor.id if actor else None),
        ),
    )
