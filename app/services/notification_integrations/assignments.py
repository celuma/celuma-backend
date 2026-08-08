"""Assignment notification integrations (Céluma 1.3, Phase 3, Block F).

Three call sites produce `ASSIGNMENT_ADDED`: order assignees, order reviewers
and sample assignees. All three share `_notify_assignment_added` because the
notification is identical from the recipient's point of view — *work was
handed to you on this order* — and the differences (which table synced, which
`EventType` was written) are upstream details that should not fork the
notification path.

The occurrence marker
---------------------
::

    f"{order_event.id}:{added_user_id}"

One persisted `OrderEvent` represents the whole `added` set, but the
notification is per-user: a PUT that adds three people is three
notifications, each of which must deduplicate independently. Keying on the
event id alone would let the first user's notification suppress the other
two; keying on a fresh per-request UUID would deduplicate nothing at all,
since a retry generates a new one (Block B service contract §5 names both
mistakes explicitly).

Why not a per-user "assignment id"? Because `Assignment` rows are soft-
unassigned and re-created, so an assign/unassign/re-assign cycle produces a
new row for the same person — and that cycle *is* a new occurrence worth
notifying. The event id already carries "which mutation", and pairing it with
the user carries "which recipient".
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence
from uuid import UUID

from sqlmodel import Session

from app.models.laboratory import Order, Sample
from app.models.notification import NotificationResourceType, NotificationType
from app.models.user import AppUser
from app.schemas.notification import NotificationCommand
from app.services.notification import NotificationService
from app.services.notification_integrations.recipients import (
    resolve_assignment_added_recipients,
)
from app.services.notification_templates import CURRENT_TEMPLATE_KEY

logger = logging.getLogger(__name__)


def _actor_name(actor: Optional[AppUser]) -> str:
    if actor is None:
        return "Un usuario"
    return (actor.full_name or "").strip() or (actor.username or "").strip() or "Un usuario"


def _notify_assignment_added(
    session: Session,
    *,
    tenant_id: UUID,
    order: Optional[Order],
    resource_type: NotificationResourceType,
    resource_id: UUID,
    added_user_ids: Sequence[UUID],
    actor: Optional[AppUser],
    order_event_id: UUID,
) -> int:
    """One notification per newly added user. Returns how many were created.

    Deliberately a loop rather than one shared notification with several
    recipients: each row's marker embeds its own recipient, which is what
    makes "adding Ana today and Bruno tomorrow" two independent occurrences
    rather than one that Ana already consumed.

    Every iteration goes through `NotificationService.notify()`, which
    contains its own failures — so one user's notification failing does not
    stop the rest, and none of them can break the assignment transaction.
    """
    if order is None or not added_user_ids:
        return 0

    recipients = resolve_assignment_added_recipients(
        session, tenant_id=tenant_id, added_user_ids=added_user_ids
    )
    created = 0
    for user_id in recipients:
        notification_id = NotificationService.notify(
            session,
            NotificationCommand(
                tenant_id=tenant_id,
                type=NotificationType.ASSIGNMENT_ADDED,
                resource_type=resource_type,
                resource_id=resource_id,
                occurrence_marker=f"{order_event_id}:{user_id}",
                template_key=CURRENT_TEMPLATE_KEY[NotificationType.ASSIGNMENT_ADDED],
                template_params={
                    "order_number": order.order_code,
                    "actor_name": _actor_name(actor),
                },
                recipient_user_ids=[user_id],
                created_by=(actor.id if actor else None),
            ),
        )
        if notification_id is not None:
            created += 1
    return created


def notify_order_assignments_added(
    session: Session,
    *,
    order: Order,
    added_user_ids: Sequence[UUID],
    actor: Optional[AppUser],
    order_event_id: UUID,
) -> int:
    """Staff were added as assignees on an order.

    The resource is the **order**, so the notification opens the order page —
    where `lab:read` and tenant ownership are re-checked exactly as they would
    be for a bookmark.
    """
    return _notify_assignment_added(
        session,
        tenant_id=order.tenant_id,
        order=order,
        resource_type=NotificationResourceType.ORDER,
        resource_id=order.id,
        added_user_ids=added_user_ids,
        actor=actor,
        order_event_id=order_event_id,
    )


def notify_order_reviewers_added(
    session: Session,
    *,
    order: Order,
    added_user_ids: Sequence[UUID],
    actor: Optional[AppUser],
    order_event_id: UUID,
) -> int:
    """Users were added as reviewers on an order.

    Reviewer addition is `ASSIGNMENT_ADDED`, not a distinct type: the six
    approved `NotificationType` values are frozen for Phase 3, and from the
    recipient's side "you were made a reviewer" is the same fact as "you were
    assigned" — work is now yours. Being *asked to review a submitted report*
    is the separate event, and it already has its own type
    (`REPORT_SUBMITTED`), fired when the report is actually submitted rather
    than when the reviewer list changes.
    """
    return _notify_assignment_added(
        session,
        tenant_id=order.tenant_id,
        order=order,
        resource_type=NotificationResourceType.ORDER,
        resource_id=order.id,
        added_user_ids=added_user_ids,
        actor=actor,
        order_event_id=order_event_id,
    )


def notify_sample_assignments_added(
    session: Session,
    *,
    sample: Sample,
    order: Optional[Order],
    added_user_ids: Sequence[UUID],
    actor: Optional[AppUser],
    order_event_id: UUID,
) -> int:
    """Staff were added as assignees on a sample.

    The resource is the **sample**, so the deep link opens the sample rather
    than its order — the assignment is to that specific specimen, and the
    order page would not show the recipient what they were actually given. The
    copy still names the order number, which is the identifier staff work
    from.
    """
    return _notify_assignment_added(
        session,
        tenant_id=sample.tenant_id,
        order=order,
        resource_type=NotificationResourceType.SAMPLE,
        resource_id=sample.id,
        added_user_ids=added_user_ids,
        actor=actor,
        order_event_id=order_event_id,
    )
