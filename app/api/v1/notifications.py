"""Notification inbox API (Céluma 1.3, Phase 3, Block B).

Four endpoints, all self-scoped:

    GET  /api/v1/notifications
    GET  /api/v1/notifications/unread-count
    POST /api/v1/notifications/{recipient_id}/read
    POST /api/v1/notifications/read-all

Authentication only — no `require_permission(...)` anywhere in this module.
That is deliberate, not an oversight (Block A API proposal §3): every query
is filtered by the authenticated user's own id *and* tenant, so a user can
only ever see or act on rows addressed to them. An RBAC gate on top would
grant nothing and deny nothing, while implying a tenant-wide notification
surface that does not exist. Cross-user and cross-tenant path access returns
404, never 403, matching the codebase-wide convention of not confirming that
a resource exists to someone who should not see it.

No endpoint accepts `tenant_id` or `user_id` from the client, and there is no
endpoint that creates a notification: notifications originate from domain
events through `NotificationService`, never from an API call.
"""
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, update
from sqlmodel import Session, select

from app.api.v1.auth import AuthContext, current_user
from app.core.db import get_session
from app.models.notification import (
    Notification,
    NotificationRecipient,
    NotificationRecipientStatus,
    NotificationType,
)
from app.models.user import AppUser
from app.schemas.notification import (
    NotificationListItem,
    NotificationListResponse,
    NotificationReadAllResponse,
    NotificationRecipientReadResponse,
    NotificationUnreadCountResponse,
)
from app.services.cursor_pagination import decode_cursor, encode_cursor
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications")

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
#
# The shared `HTTPBearer()` in app/api/v1/auth.py runs with auto_error=True,
# which answers a request carrying no Authorization header with 403 "Not
# authenticated" rather than 401. Block B requires 401 for unauthenticated
# requests, so this router resolves the bearer credential itself
# (auto_error=False) and raises 401 when it is absent, then delegates to the
# shared `current_user` for everything else — token blacklisting, decoding,
# user lookup and the active-user check are unchanged.
#
# Scoped to this router on purpose: changing the shared scheme's behaviour
# would alter the response code of all seventeen existing routers, which is
# well outside this block.

_bearer = HTTPBearer(auto_error=False)


def notification_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    session: Session = Depends(get_session),
) -> AppUser:
    if credentials is None:
        raise HTTPException(401, "Not authenticated")
    return current_user(request, credentials, session)


def notification_ctx(user: AppUser = Depends(notification_user)) -> AuthContext:
    """The tenant/user scope every query in this module filters by.

    Derived from `notification_user` rather than the shared `get_auth_ctx` so
    it inherits the 401 behaviour above instead of re-triggering the shared
    scheme's 403.
    """
    return AuthContext(user_id=str(user.id), tenant_id=str(user.tenant_id))


# ---------------------------------------------------------------------------
# B6 — list
# ---------------------------------------------------------------------------

@router.get("", response_model=NotificationListResponse)
@router.get("/", response_model=NotificationListResponse, include_in_schema=False)
def list_notifications(
    cursor: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    unread_only: bool = False,
    type: Optional[List[NotificationType]] = Query(default=None),
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(notification_ctx),
    user: AppUser = Depends(notification_user),
):
    """The authenticated user's own inbox, newest first.

    Ordering is fixed `created_at DESC` — a notification feed has no other
    sensible order, so no `sort` parameter exists. The secondary sort on
    `NotificationRecipient.id` is what makes the cursor stable: several
    notifications created in the same transaction share a timestamp to the
    microsecond, and without a deterministic tiebreak a page boundary landing
    inside that group would skip or repeat rows.

    Severity filtering is deliberately not exposed while INFO is the only
    severity in active use.
    """
    if limit < 1 or limit > MAX_LIMIT:
        raise HTTPException(400, f"Limit must be between 1 and {MAX_LIMIT}")
    if since is not None and until is not None and since > until:
        raise HTTPException(400, "'since' must not be after 'until'")

    query = (
        select(NotificationRecipient, Notification)
        .join(Notification, Notification.id == NotificationRecipient.notification_id)
        .where(
            NotificationRecipient.tenant_id == ctx.tenant_id,
            NotificationRecipient.user_id == user.id,
        )
    )

    if unread_only:
        query = query.where(
            NotificationRecipient.status == NotificationRecipientStatus.UNREAD
        )
    if type:
        query = query.where(Notification.type.in_([t.value for t in type]))
    # Filtered on the recipient's denormalized copy, which is byte-identical
    # to the parent's and is the indexed column.
    if since is not None:
        query = query.where(NotificationRecipient.created_at >= since)
    if until is not None:
        query = query.where(NotificationRecipient.created_at <= until)

    if cursor:
        try:
            cursor_time, cursor_id = decode_cursor(cursor)
            cursor_uuid = UUID(cursor_id)
        except (ValueError, AttributeError):
            raise HTTPException(400, "Invalid cursor format")
        query = query.where(
            (NotificationRecipient.created_at < cursor_time)
            | (
                (NotificationRecipient.created_at == cursor_time)
                & (NotificationRecipient.id < cursor_uuid)
            )
        )

    query = query.order_by(
        NotificationRecipient.created_at.desc(), NotificationRecipient.id.desc()
    )

    rows = session.exec(query.limit(limit + 1)).all()
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    items = [
        NotificationListItem(
            recipient_id=str(recipient.id),
            notification_id=str(notification.id),
            type=notification.type,
            severity=notification.severity,
            title=notification.title,
            body=notification.body,
            resource_type=notification.resource_type,
            resource_id=str(notification.resource_id),
            status=recipient.status,
            created_at=recipient.created_at,
            read_at=recipient.read_at,
        )
        # `notification_metadata` is intentionally absent: it is an internal
        # audit field (content policy §6). The deep-link data the frontend
        # needs is already promoted to resource_type/resource_id above.
        for recipient, notification in rows
    ]

    next_cursor = None
    if has_more and rows:
        last_recipient = rows[-1][0]
        next_cursor = encode_cursor(last_recipient.created_at, str(last_recipient.id))

    return NotificationListResponse(items=items, next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# B7 — unread count
# ---------------------------------------------------------------------------

@router.get("/unread-count", response_model=NotificationUnreadCountResponse)
def get_unread_count(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(notification_ctx),
    user: AppUser = Depends(notification_user),
):
    """The polling endpoint, kept maximally cheap.

    A single COUNT(*) served by ix_notification_recipient_inbox_status
    (tenant_id, user_id, status). No join: the shared `Notification` row
    carries nothing this count needs.
    """
    count = session.exec(
        select(func.count())
        .select_from(NotificationRecipient)
        .where(
            NotificationRecipient.tenant_id == ctx.tenant_id,
            NotificationRecipient.user_id == user.id,
            NotificationRecipient.status == NotificationRecipientStatus.UNREAD,
        )
    ).one()
    return NotificationUnreadCountResponse(unread_count=_scalar(count))


# ---------------------------------------------------------------------------
# B9 — mark all as read
# ---------------------------------------------------------------------------
#
# Declared before the `/{recipient_id}/read` route below purely for reading
# order; the two cannot collide, since this path has one segment and that one
# has two.

@router.post("/read-all", response_model=NotificationReadAllResponse)
def mark_all_as_read(
    type: Optional[List[NotificationType]] = Query(default=None),
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(notification_ctx),
    user: AppUser = Depends(notification_user),
):
    """Mark the caller's UNREAD rows read, optionally narrowed by the same
    filters as the list endpoint so "mark all as read" matches whatever view
    the user is looking at.

    `unread_only` is not a parameter: the operation targets unread rows by
    definition. One UPDATE statement, one server-generated `read_at` shared
    by every affected row, so a later "everything I read at 14:32" query
    groups them correctly.
    """
    if since is not None and until is not None and since > until:
        raise HTTPException(400, "'since' must not be after 'until'")

    read_at = datetime.utcnow()
    statement = (
        update(NotificationRecipient)
        .where(
            NotificationRecipient.tenant_id == UUID(ctx.tenant_id),
            NotificationRecipient.user_id == user.id,
            NotificationRecipient.status == NotificationRecipientStatus.UNREAD.value,
        )
        .values(status=NotificationRecipientStatus.READ.value, read_at=read_at)
    )

    if type or since is not None or until is not None:
        scoped = select(NotificationRecipient.id).where(
            NotificationRecipient.tenant_id == ctx.tenant_id,
            NotificationRecipient.user_id == user.id,
            NotificationRecipient.status == NotificationRecipientStatus.UNREAD,
        )
        if type:
            scoped = scoped.join(
                Notification, Notification.id == NotificationRecipient.notification_id
            ).where(Notification.type.in_([t.value for t in type]))
        if since is not None:
            scoped = scoped.where(NotificationRecipient.created_at >= since)
        if until is not None:
            scoped = scoped.where(NotificationRecipient.created_at <= until)
        target_ids = [row for row in session.exec(scoped).all()]
        if not target_ids:
            return NotificationReadAllResponse(updated_count=0)
        statement = statement.where(
            NotificationRecipient.id.in_([_scalar(i) for i in target_ids])
        )

    result = session.exec(statement)
    updated = result.rowcount or 0
    session.commit()

    logger.info(
        "Notifications marked read in bulk",
        extra={
            "event": "notification.read_all",
            "tenant_id": ctx.tenant_id,
            "updated_count": updated,
        },
    )
    return NotificationReadAllResponse(updated_count=updated)


# ---------------------------------------------------------------------------
# B8 — mark one as read
# ---------------------------------------------------------------------------

@router.post("/{recipient_id}/read", response_model=NotificationRecipientReadResponse)
def mark_as_read(
    recipient_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(notification_ctx),
    user: AppUser = Depends(notification_user),
):
    """Mark one of the caller's own inbox rows as read.

    The path id is a `NotificationRecipient.id`, never a `Notification.id`:
    read state is per-user, and the shared notification row is immutable.

    Idempotent — a second call returns 200 with the unchanged state rather
    than an error, since two browser tabs marking the same row read is
    ordinary, not exceptional. There is no READ -> UNREAD transition in Phase
    3, so idempotent-read is the only repeat-call behaviour to define.
    """
    try:
        recipient_uuid = UUID(recipient_id)
    except (ValueError, AttributeError):
        # A malformed id is indistinguishable from a nonexistent one as far
        # as the caller is concerned, and answering 422 here would confirm
        # the id space's shape.
        raise HTTPException(404, "Notification not found")

    recipient = session.exec(
        select(NotificationRecipient).where(
            NotificationRecipient.id == recipient_uuid,
            NotificationRecipient.user_id == user.id,
            NotificationRecipient.tenant_id == ctx.tenant_id,
        )
    ).first()

    # 404 covers all three cases — missing, another user's, another tenant's
    # — so the response cannot be used to discover that a row exists
    # somewhere the caller cannot see it.
    if recipient is None:
        raise HTTPException(404, "Notification not found")

    if recipient.status == NotificationRecipientStatus.UNREAD:
        recipient.status = NotificationRecipientStatus.READ
        recipient.read_at = datetime.utcnow()
        session.add(recipient)
        session.commit()
        session.refresh(recipient)
        logger.info(
            "Notification marked read",
            extra={
                "event": "notification.read",
                "notification_id": str(recipient.notification_id),
                "tenant_id": ctx.tenant_id,
            },
        )

    return NotificationRecipientReadResponse(
        recipient_id=str(recipient.id),
        status=recipient.status,
        read_at=recipient.read_at,
    )


def _scalar(value):
    """`session.exec()` yields Row objects for some Core statements and bare
    scalars for others; normalize both."""
    if isinstance(value, (tuple, list)) or hasattr(value, "_fields"):
        return value[0]
    return value
