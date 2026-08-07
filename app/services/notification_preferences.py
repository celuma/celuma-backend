"""Effective notification-preference resolution (Céluma 1.3, Phase 3, Block D).

`notification_preference` stores **overrides only**. No row is ever seeded,
per user or per type, so an empty table is the correct steady state and
"absence means the policy default" is the load-bearing rule of this module.
Everything here is a **read**: nothing in this file inserts, updates or
deletes a preference row, and opening the Profile page therefore creates
nothing.

Two entry points, both built on the same `_apply_policy`:

  `resolve_effective_notification_preferences(...)`
      one type, many users — what delivery materialization needs.
  `resolve_all_effective_preferences(...)`
      one user, all six types in a stable order — what the GET endpoint
      returns.

Neither is N+1: each performs one query for the users in scope and one for
whatever override rows exist, then resolves in memory.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple
from uuid import UUID

from sqlmodel import Session, select

from app.models.notification import NotificationPreference, NotificationType
from app.models.user import AppUser
from app.services.notification_policies import (
    default_email_enabled,
    email_supported,
)

logger = logging.getLogger(__name__)


#: The order every API response and every test assertion uses. Taken from the
#: enum's declaration order (the order Block A's event inventory lists the six
#: MUST_HAVE_1_3 events in) rather than sorted alphabetically, so the Profile
#: screen reads as a workflow rather than a dictionary.
NOTIFICATION_TYPE_ORDER: Tuple[NotificationType, ...] = tuple(NotificationType)


@dataclass(frozen=True)
class EffectiveNotificationPreference:
    """What a given user's preference resolves to for one notification type.

    This is a resolved value, not a row: `is_explicit=False` means no row
    exists and these values came from the policy registry.
    """

    #: Always True in Céluma 1.3 — see `_resolve_in_app`.
    in_app_enabled: bool
    #: Policy-bounded: False whenever `email_supported` is False, regardless
    #: of what any stored row says.
    email_enabled: bool
    #: Whether the type may use email at all (the policy's answer).
    email_supported: bool
    #: Whether a `notification_preference` row backs this value.
    is_explicit: bool
    #: The row's `updated_at`, or None when this is an implicit default.
    updated_at: Optional[datetime] = None


def _resolve_in_app(
    notification_type: NotificationType,
    row: Optional[NotificationPreference],
    *,
    user_id: UUID,
) -> bool:
    """In-app delivery is enabled for every type in Céluma 1.3.

    A legacy or hand-edited row carrying `in_app_enabled = false` is
    **ignored** for eligibility and logged at warning level. Three
    alternatives were considered:

      - *Repair it on read.* Rejected: a GET must not write. It would also
        make an idempotent read produce a different database on every call,
        which is exactly the surprise the sparse-row model exists to avoid.
      - *Fail strictly.* Rejected: one malformed row would take out the
        user's whole preferences screen, and — far worse — would be reached
        from inside `NotificationService.notify()`, turning a bad config row
        into a notification-creation failure during a clinical transition.
      - *Ignore and warn* (selected). The notification is still delivered
        in-app, which is the safe direction: the failure mode of honouring
        the row is a user silently missing operational notifications they
        have no UI to re-enable, since the API refuses to write `false`.

    The warning names the user and type only — never content.
    """
    if row is not None and not row.in_app_enabled:
        logger.warning(
            "Ignoring an in-app opt-out that Céluma 1.3 does not support",
            extra={
                "event": "notification.preference.invalid_in_app_disabled",
                "notification_type": notification_type.value,
                "user_id": str(user_id),
                "error_code": "in_app_disable_not_supported",
            },
        )
    return True


def _apply_policy(
    notification_type: NotificationType,
    row: Optional[NotificationPreference],
    *,
    user_id: UUID,
) -> EffectiveNotificationPreference:
    """Resolve one (user, type) pair from its optional override row."""
    supported = email_supported(notification_type)
    if row is None:
        resolved_email = default_email_enabled(notification_type)
    else:
        # The policy is the outer bound: a stale row saying True for an
        # unsupported type resolves to False, and is left untouched.
        resolved_email = supported and row.email_enabled

    return EffectiveNotificationPreference(
        in_app_enabled=_resolve_in_app(notification_type, row, user_id=user_id),
        email_enabled=resolved_email,
        email_supported=supported,
        is_explicit=row is not None,
        updated_at=row.updated_at if row is not None else None,
    )


def _tenant_scoped_user_ids(
    session: Session, user_ids: Sequence[UUID], tenant_id: UUID
) -> List[UUID]:
    """The subset of `user_ids` that exists and belongs to `tenant_id`.

    Unknown and cross-tenant ids are dropped rather than raised on: this is a
    read helper reached from inside `notify()`, and the caller
    (`validate_recipient_tenants`) has already rejected a genuinely
    cross-tenant recipient before any write. Dropping here is defence in
    depth — a user this resolver cannot vouch for simply gets no resolved
    preference, and therefore no delivery row.

    Order follows `user_ids`, so results are deterministic.
    """
    if not user_ids:
        return []
    rows = session.exec(
        select(AppUser.id).where(
            AppUser.id.in_(list(user_ids)),
            AppUser.tenant_id == tenant_id,
        )
    ).all()
    valid = {row[0] if isinstance(row, (tuple, list)) else row for row in rows}
    seen: set[UUID] = set()
    ordered: List[UUID] = []
    for user_id in user_ids:
        if user_id in valid and user_id not in seen:
            seen.add(user_id)
            ordered.append(user_id)
    return ordered


def resolve_effective_notification_preferences(
    session: Session,
    *,
    tenant_id: UUID,
    user_ids: Sequence[UUID],
    notification_type: NotificationType,
) -> Dict[UUID, EffectiveNotificationPreference]:
    """One notification type, many users.

    Returns an entry for every id in `user_ids` that exists and belongs to
    `tenant_id`; unknown and cross-tenant ids are absent from the result.
    Inactive users **are** included — activity is a delivery-eligibility
    question, answered where the delivery is materialized, not a preference
    question.

    Two queries regardless of how many users are passed. Creates nothing.
    """
    valid_ids = _tenant_scoped_user_ids(session, user_ids, tenant_id)
    if not valid_ids:
        return {}

    rows = session.exec(
        select(NotificationPreference).where(
            NotificationPreference.tenant_id == tenant_id,
            NotificationPreference.user_id.in_(valid_ids),
            NotificationPreference.notification_type == notification_type,
        )
    ).all()
    by_user: Dict[UUID, NotificationPreference] = {row.user_id: row for row in rows}

    return {
        user_id: _apply_policy(
            notification_type, by_user.get(user_id), user_id=user_id
        )
        for user_id in valid_ids
    }


def resolve_all_effective_preferences(
    session: Session,
    *,
    tenant_id: UUID,
    user_id: UUID,
) -> List[Tuple[NotificationType, EffectiveNotificationPreference]]:
    """One user, all six notification types, in `NOTIFICATION_TYPE_ORDER`.

    Every type is always present, whether or not a row backs it — the API
    contract is that a client never has to reason about "missing means
    what?".

    One query. Creates nothing, so opening the preferences screen leaves the
    table exactly as it was.
    """
    rows = session.exec(
        select(NotificationPreference).where(
            NotificationPreference.tenant_id == tenant_id,
            NotificationPreference.user_id == user_id,
        )
    ).all()
    by_type: Dict[NotificationType, NotificationPreference] = {}
    for row in rows:
        # The column is a plain VARCHAR; normalize to the enum so a value the
        # CHECK constraint permits but this build does not know cannot key
        # the dict under a string and silently miss.
        try:
            by_type[NotificationType(row.notification_type)] = row
        except ValueError:  # pragma: no cover — the CHECK constraint prevents it
            logger.warning(
                "Ignoring a preference row with an unrecognised notification type",
                extra={
                    "event": "notification.preference.unknown_type",
                    "user_id": str(user_id),
                    "error_code": "unknown_notification_type",
                },
            )

    return [
        (
            notification_type,
            _apply_policy(
                notification_type, by_type.get(notification_type), user_id=user_id
            ),
        )
        for notification_type in NOTIFICATION_TYPE_ORDER
    ]
