"""Notification-preference API (Céluma 1.3, Phase 3, Block D).

    GET /api/v1/notification-preferences
    PUT /api/v1/notification-preferences

Its own router rather than two more handlers on the notifications router,
because that router carries `prefix="/notifications"` and these paths are not
under it. Everything else is deliberately identical to Block B's router: the
same self-scoping, the same authentication-only gate, and the same
`notification_user` dependency — imported rather than re-implemented — so
these endpoints answer **401** for a missing `Authorization` header like the
inbox does, instead of the platform-wide 403 the shared `HTTPBearer()`
produces.

Self-scoped, and structurally so
--------------------------------
No endpoint here accepts a `user_id` or a `tenant_id` in a path, a query
parameter or a body — `NotificationPreferenceUpdateItem` sets
`extra="forbid"`, so sending one is a 422 rather than a silently ignored
field. Scope comes from the token. This is why there is no cross-user 403 or
404 case to define: there is no way to *name* another user's preferences, so
reaching them is not a permission failure, it is unrepresentable.

No permission check either, for the same reason Block B's inbox has none: a
gate on top would grant nothing and deny nothing, while implying a
tenant-wide preference surface that does not exist. Managing tenant-wide
defaults — if it is ever built — is a different endpoint that would reuse
`admin:manage_tenant`, per Block A's permission decision.

What this API does not do
-------------------------
It sends no email, starts no worker, and creates no notification or delivery
row. **Preference changes affect future notifications only**: a delivery row
already materialized for a past notification is never created, deleted or
rewritten by anything in this module.
"""
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete as sa_delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session

from app.api.v1.auth import AuthContext
from app.api.v1.notifications import notification_ctx, notification_user
from app.core.db import get_session
from app.models.notification import NotificationPreference, NotificationType
from app.models.user import AppUser
from app.schemas.notification import (
    NotificationPreferenceItem,
    NotificationPreferenceListResponse,
    NotificationPreferenceUpdateRequest,
)
from app.services.notification_policies import default_email_enabled, email_supported
from app.services.notification_preferences import resolve_all_effective_preferences
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notification-preferences")


def _effective_response(
    session: Session, *, tenant_id, user_id
) -> NotificationPreferenceListResponse:
    """The full effective list — the single response shape both endpoints
    return, so a client's post-save state comes from the same resolution path
    as its initial load."""
    resolved = resolve_all_effective_preferences(
        session, tenant_id=tenant_id, user_id=user_id
    )
    return NotificationPreferenceListResponse(
        preferences=[
            NotificationPreferenceItem(
                notification_type=notification_type,
                in_app_enabled=preference.in_app_enabled,
                email_enabled=preference.email_enabled,
                email_supported=preference.email_supported,
                is_explicit=preference.is_explicit,
                updated_at=preference.updated_at,
            )
            for notification_type, preference in resolved
        ]
    )


# ---------------------------------------------------------------------------
# D3 — read
# ---------------------------------------------------------------------------

@router.get("", response_model=NotificationPreferenceListResponse)
@router.get("/", response_model=NotificationPreferenceListResponse, include_in_schema=False)
def get_notification_preferences(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(notification_ctx),
    user: AppUser = Depends(notification_user),
):
    """Every notification type's effective preference for the caller.

    All six are always returned, in a fixed order, whether or not a row
    backs them. **This creates nothing** — an empty
    `notification_preference` table is the correct steady state, and merely
    opening the Profile screen must leave it empty.
    """
    return _effective_response(session, tenant_id=ctx.tenant_id, user_id=user.id)


# ---------------------------------------------------------------------------
# D3 — write
# ---------------------------------------------------------------------------

@router.put("", response_model=NotificationPreferenceListResponse)
@router.put("/", response_model=NotificationPreferenceListResponse, include_in_schema=False)
def update_notification_preferences(
    payload: NotificationPreferenceUpdateRequest,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(notification_ctx),
    user: AppUser = Depends(notification_user),
):
    """Apply a partial batch of email-preference changes.

    Sparse rows, deliberately
    -------------------------
    A value equal to the type's effective policy default **removes** any
    override row instead of persisting one that says the same thing as the
    default. Two reasons this is the right half of the choice:

      - It keeps "absence means default" true in both directions. Under the
        alternative, a user who toggled a switch off and back on would leave
        behind a row asserting the current default forever — and if the
        product default ever changed, that row would silently pin them to
        the old behaviour they never actually chose.
      - It keeps the table genuinely sparse: rows exist only where a user
        disagrees with Céluma.

    For a type with `email_supported = false` the effective default is
    `false`, so `email_enabled: false` is a no-op that stores nothing, and
    `email_enabled: true` is rejected below. One uniform rule covers both.

    Atomicity
    ---------
    Every item is validated before any statement runs, and the whole batch
    lands in a single commit. One invalid item means **no** change is
    applied — a partially applied batch would leave the user's screen and
    the database disagreeing about what they just saved.
    """
    unsupported: List[str] = [
        item.notification_type.value
        for item in payload.preferences
        if item.email_enabled and not email_supported(item.notification_type)
    ]
    if unsupported:
        # 422, not 400: this is a semantic problem with a field's value, the
        # same class of error the schema's own validation produces, and the
        # client fix is the same — read `email_supported` from the GET and
        # do not offer the control.
        raise HTTPException(
            422,
            "Email delivery is not available for the requested notification "
            f"type(s): {sorted(unsupported)}",
        )

    updated_at = datetime.utcnow()
    to_delete: List[NotificationType] = []
    to_upsert: List[dict] = []

    for item in payload.preferences:
        if item.email_enabled == default_email_enabled(item.notification_type):
            to_delete.append(item.notification_type)
            continue
        to_upsert.append(
            {
                "tenant_id": user.tenant_id,
                "user_id": user.id,
                "notification_type": item.notification_type.value,
                # Never writable by a user in Céluma 1.3, and never written
                # as anything but True — the schema has no field for it, and
                # this is the only place a preference row is created.
                "in_app_enabled": True,
                "email_enabled": item.email_enabled,
                "updated_at": updated_at,
            }
        )

    if to_delete:
        session.exec(
            sa_delete(NotificationPreference).where(
                NotificationPreference.user_id == user.id,
                NotificationPreference.tenant_id == user.tenant_id,
                NotificationPreference.notification_type.in_(
                    [notification_type.value for notification_type in to_delete]
                ),
            )
        )

    if to_upsert:
        # ON CONFLICT DO UPDATE on the real unique constraint, so "create the
        # override" and "change the override" are one statement and cannot
        # race each other into a duplicate. `updated_at` is server-generated
        # here, never client-supplied.
        session.exec(
            pg_insert(NotificationPreference)
            .values(to_upsert)
            .on_conflict_do_update(
                constraint="uq_notification_preference_user_type",
                set_={
                    "tenant_id": pg_insert(NotificationPreference).excluded.tenant_id,
                    "in_app_enabled": True,
                    "email_enabled": pg_insert(
                        NotificationPreference
                    ).excluded.email_enabled,
                    "updated_at": updated_at,
                },
            )
        )

    session.commit()

    logger.info(
        "Notification preferences updated",
        extra={
            "event": "notification.preference.updated",
            "tenant_id": ctx.tenant_id,
            "user_id": str(user.id),
            "overrides_written": len(to_upsert),
            "overrides_removed": len(to_delete),
        },
    )

    return _effective_response(session, tenant_id=ctx.tenant_id, user_id=user.id)
