"""Send-time access checks for notification email (Céluma 1.5, #98).

A delivery row is materialized when the event commits; the worker sends it seconds to
hours later. For a conversation notice the recipient's right to read the order can
change in between (deactivated account, role change that drops `lab:read`, order
removed). This module answers, at send time and from fresh rows, whether the
recipient may still receive the email. A refusal is recorded as a TERMINAL failure
(`recipient_access_revoked`): retrying it later would not make it correct.

Only `ORDER_COMMENT_ADDED` is checked here. The other types keep their 1.3/1.4
contract (an address snapshotted at materialization); widening the check to them is
a separate decision, recorded in `specs/notifications/email.md`.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlmodel import Session

from app.models.laboratory import Order
from app.models.notification import Notification, NotificationResourceType, NotificationType
from app.models.user import AppUser
from app.services.notification_integrations.recipients import (
    ORDER_CONVERSATION_READ_PERMISSION,
    users_with_permission,
)

ERROR_RECIPIENT_ACCESS_REVOKED = "recipient_access_revoked"

#: Types whose email is re-authorized at send time.
ACCESS_CHECKED_TYPES = frozenset({NotificationType.ORDER_COMMENT_ADDED})


def delivery_access_error(
    session: Session,
    *,
    notification: Optional[Notification],
    tenant_id: UUID,
    recipient_user_id: Optional[UUID],
) -> Optional[str]:
    """`None` when the email may be sent; otherwise the terminal error code."""
    if notification is None:
        return None
    if NotificationType(notification.type) not in ACCESS_CHECKED_TYPES:
        return None
    if recipient_user_id is None:
        return ERROR_RECIPIENT_ACCESS_REVOKED
    user = session.get(AppUser, recipient_user_id)
    if user is None or not user.is_active or user.tenant_id != tenant_id:
        return ERROR_RECIPIENT_ACCESS_REVOKED
    if not users_with_permission(session, [user.id], ORDER_CONVERSATION_READ_PERMISSION):
        return ERROR_RECIPIENT_ACCESS_REVOKED
    if notification.resource_type == NotificationResourceType.ORDER.value:
        order = session.get(Order, notification.resource_id)
        if order is None or order.tenant_id != tenant_id or order.removed_at is not None:
            return ERROR_RECIPIENT_ACCESS_REVOKED
    return None
