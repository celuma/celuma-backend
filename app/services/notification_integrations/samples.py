"""Sample-transition notification integrations (Céluma 1.3, Phase 3, Block F).

One event, `SAMPLE_STATUS_CHANGED`, from two call sites: the explicit
`PATCH /samples/{id}/state`, and the automatic `RECEIVED -> PROCESSING`
transition on a sample's first image upload. Both write a persisted
`OrderEvent`, both are genuine state changes, and treating only one of them as
notifiable would mean the same visible transition sometimes notifies and
sometimes does not depending on how it was triggered.

In-app only
-----------
`notification_policies.py` gives this type `email_supported = False`, and that
is absolute — no user preference can re-enable it, and
`materialize_email_deliveries` checks the policy before anything else, so no
`NotificationDelivery` row can exist for it. Nothing in this module knows or
cares: the policy is enforced one layer down, which is exactly why it cannot
be forgotten here.

Volume
------
This is the highest-frequency event in the approved set, and the recipient set
is deliberately the narrower of the two plausible readings — see
`resolve_sample_status_changed_recipients` and the storm review in
phase-3-block-f-implementation-summary.md §Risks.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from sqlmodel import Session, select

from app.models.laboratory import Order, Sample
from app.models.notification import NotificationResourceType, NotificationType
from app.models.report import Report
from app.schemas.notification import NotificationCommand
from app.models.user import AppUser
from app.services.notification import NotificationService
from app.models.enums import SampleState
from app.services.notification_integrations.recipients import (
    resolve_sample_status_changed_recipients,
)
from app.services.notification_templates import CURRENT_TEMPLATE_KEY
from app.services.sample_status_labels import sample_status_label

logger = logging.getLogger(__name__)


def notify_sample_status_changed(
    session: Session,
    *,
    sample: Sample,
    order: Optional[Order],
    old_state: Optional[str],
    new_state: str,
    actor: Optional[AppUser],
    occurrence_marker: str,
) -> Optional[UUID]:
    """A sample changed state.

    Recipients: the order's assignees, plus the report author when a report
    exists, minus the actor performing the change.

    **A no-op transition creates nothing.** `PATCH /state` writes an
    `OrderEvent` even when the requested state equals the current one, so
    without this guard a client re-sending the same state would produce a
    fresh timeline row *and* a fresh notification with a fresh marker — a
    duplicate the idempotency key could not catch, because from the key's
    point of view it is a different occurrence. The check belongs here rather
    than in the endpoint: it is a question about whether the *notification* is
    warranted, and the timeline's own semantics are not Block F's to change.

    A genuine `RECEIVED -> PROCESSING -> RECEIVED -> PROCESSING` sequence
    produces four notifications, correctly: each transition really happened,
    each has its own `OrderEvent`, and each is something an assignee may need
    to know.
    """
    if order is None:
        return None
    if old_state is not None and old_state == new_state:
        return None

    report = session.exec(
        select(Report).where(Report.order_id == order.id)
    ).first()

    recipients = resolve_sample_status_changed_recipients(
        session,
        tenant_id=sample.tenant_id,
        order_id=order.id,
        report=report,
    )
    return NotificationService.notify(
        session,
        NotificationCommand(
            tenant_id=sample.tenant_id,
            type=NotificationType.SAMPLE_STATUS_CHANGED,
            resource_type=NotificationResourceType.SAMPLE,
            resource_id=sample.id,
            occurrence_marker=occurrence_marker,
            template_key=CURRENT_TEMPLATE_KEY[NotificationType.SAMPLE_STATUS_CHANGED],
            template_params={
                "order_number": order.order_code,
                "sample_code": sample.sample_code,
                # Pre-release remediation: the raw `SampleState` enum value
                # (e.g. "PROCESSING") must never reach a rendered Spanish
                # notification — translated once, centrally, in
                # sample_status_labels.py. The enum itself is still used for
                # the no-op guard above and for `OrderEvent.event_metadata`.
                "new_status_label": sample_status_label(SampleState(new_state)),
            },
            recipient_user_ids=recipients,
            created_by=(actor.id if actor else None),
        ),
    )
