"""Order conversation notification integration (Céluma 1.5, #98).

One event, `ORDER_COMMENT_ADDED`, raised by `POST /laboratory/orders/{id}/comments`
after the comment, its mentions, attachments and `COMMENT_ADDED` timeline event are
in the session and before the route commits — so a failed save produces no
notification, and an idempotent replay (which writes nothing) produces none either.

Two notifications per message at most, both with occurrence markers derived from the
persisted comment id:

- `{comment_id}:mention` — users the message mentions (`order_comment_mention_v1`);
- `{comment_id}` — the other participants (`order_comment_added_v1`).

Neither carries the message text, an image, a patient or sample reference: only the
order number and the author's name in-app, and nothing at all in email
(`email_templates.py`). Recipients: `resolve_order_comment_recipients`.
"""
from __future__ import annotations

from typing import List, Optional, Sequence
from uuid import UUID

from sqlmodel import Session

from app.models.laboratory import Order, OrderComment
from app.models.notification import NotificationResourceType, NotificationType
from app.models.user import AppUser
from app.schemas.notification import NotificationCommand
from app.services.notification import NotificationService
from app.services.notification_integrations.assignments import _actor_name
from app.services.notification_integrations.recipients import resolve_order_comment_recipients
from app.services.notification_templates import (
    CURRENT_TEMPLATE_KEY,
    ORDER_COMMENT_MENTION_TEMPLATE_KEY,
)


def notify_order_comment_added(
    session: Session,
    *,
    order: Order,
    comment: OrderComment,
    actor: AppUser,
    mentioned_user_ids: Sequence[UUID] = (),
) -> List[Optional[UUID]]:
    """Notify the order's participants about a new conversation message."""
    mentioned, others = resolve_order_comment_recipients(
        session,
        tenant_id=order.tenant_id,
        order_id=order.id,
        mentioned_user_ids=mentioned_user_ids,
    )
    params = {"order_number": order.order_code, "actor_name": _actor_name(actor)}
    created: List[Optional[UUID]] = []
    for recipients, template_key, marker in (
        (mentioned, ORDER_COMMENT_MENTION_TEMPLATE_KEY, f"{comment.id}:mention"),
        (others, CURRENT_TEMPLATE_KEY[NotificationType.ORDER_COMMENT_ADDED], str(comment.id)),
    ):
        recipients = [user_id for user_id in recipients if user_id != actor.id]
        if not recipients:
            continue
        created.append(
            NotificationService.notify(
                session,
                NotificationCommand(
                    tenant_id=order.tenant_id,
                    type=NotificationType.ORDER_COMMENT_ADDED,
                    resource_type=NotificationResourceType.ORDER,
                    resource_id=order.id,
                    occurrence_marker=marker,
                    template_key=template_key,
                    template_params=params,
                    recipient_user_ids=recipients,
                    created_by=actor.id,
                ),
            )
        )
    return created
