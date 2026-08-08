"""NotificationService (Céluma 1.3, Phase 3, Block B).

The single entry point through which a notification is ever created. Domain
transitions will call `NotificationService.notify()` from inside their own
transaction (Block F); Block B builds and tests the service without wiring a
single real trigger point.

Two properties drive the whole design.

1. Notification creation is **best effort relative to the caller's domain
   operation.** Publishing a report must not fail because a notification
   could not be persisted. `notify()` therefore contains its own failures,
   logs them, and returns `None` rather than propagating.

2. Containing a failure must not damage the caller's transaction. This is
   the part an ordinary try/except gets wrong: after an `IntegrityError`,
   PostgreSQL aborts the whole transaction, and every subsequent statement
   fails with "current transaction is aborted" until someone rolls back — so
   a caller that "caught" the duplicate would find its own clinical writes
   unusable. Two mechanisms prevent that here:

   - The duplicate path never raises in the first place. The notification
     insert uses ``INSERT ... ON CONFLICT DO NOTHING``, so a repeated
     occurrence is a zero-row insert, not an exception. Detecting an expected
     condition by exception is what makes the transaction-poisoning failure
     mode possible at all; removing the exception removes the failure mode,
     and it is still the database's unique index — not application logic —
     that decides who wins a race.
   - Everything this service writes runs inside ``session.begin_nested()``, a
     SAVEPOINT. An unexpected failure rolls back to the savepoint, discarding
     only the notification's own writes and leaving the surrounding
     transaction alive and committable.

   `notify()` never calls `session.rollback()`, never commits, and never
   opens its own session when given one: the caller owns the transaction
   boundary, so notification rows land in the same atomic commit as the
   domain transition that produced them.

Logging follows the content policy §7 allow-list: ids, type, counts and
sanitized codes. The rendered title/body and the template parameters are
never logged — they are already in the database, and log aggregation has
different retention and access controls than the primary store.

Céluma 1.3 Phase 3, Block D extends this with one more write: after the
notification and its recipient rows, eligible recipients get a
`NotificationDelivery(status=PENDING)` row. That work lives in
`app/services/notification_delivery.py` and runs inside a **second, nested**
savepoint, so a delivery-materialization failure cannot take the in-app
notification down with it — see `_materialize_deliveries` for the full
argument. **Nothing here sends anything**: an email delivery worker is Block
E's.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from app.models.notification import (
    Notification,
    NotificationRecipient,
    NotificationRecipientStatus,
)
from app.models.user import AppUser
from app.schemas.notification import NotificationCommand
from app.services.locale import DEFAULT_LOCALE, resolve_locale
from app.services.notification_delivery import materialize_email_deliveries
from app.services.notification_templates import (
    NotificationTemplateError,
    get_template,
    render,
)

logger = logging.getLogger(__name__)


class NotificationValidationError(ValueError):
    """A command violates the service contract — an unknown template, an
    unsafe parameter, a recipient outside the tenant.

    These are caller bugs, not runtime conditions: they are deterministic and
    fixable in code. `notify()` still contains them by default (a Block F
    wiring mistake must not break a clinical transition), but `strict=True`
    re-raises so tests and development surface them immediately instead of
    silently producing no notification.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def build_idempotency_key(command: NotificationCommand) -> str:
    """`{type}:{resource_type}:{resource_id}:{occurrence_marker}`.

    Deterministic in the command alone, so the same occurrence retried
    recomputes the same key without reading anything back.
    """
    key = (
        f"{command.type.value}:{command.resource_type.value}:"
        f"{command.resource_id}:{command.occurrence_marker}"
    )
    if len(key) > 255:
        raise NotificationValidationError(
            "idempotency_key_too_long",
            "Computed idempotency key exceeds the 255-character column limit",
        )
    return key


def normalize_recipient_ids(user_ids: Iterable[UUID]) -> List[UUID]:
    """Deduplicate while preserving first-seen order.

    Order is preserved only to keep logs and test assertions stable; nothing
    downstream depends on it.
    """
    seen: set[UUID] = set()
    ordered: List[UUID] = []
    for user_id in user_ids:
        if user_id not in seen:
            seen.add(user_id)
            ordered.append(user_id)
    return ordered


def exclude_actor(
    user_ids: Sequence[UUID], actor_id: Optional[UUID]
) -> List[UUID]:
    """Drop the acting user from a recipient set.

    Per the recipient matrix's cross-cutting rule 1, no MUST_HAVE_1_3 event
    notifies the user who performed the action — they already saw the result
    in the response.
    """
    if actor_id is None:
        return list(user_ids)
    return [user_id for user_id in user_ids if user_id != actor_id]


def validate_recipient_tenants(
    session: Session, user_ids: Sequence[UUID], tenant_id: UUID
) -> List[UUID]:
    """Assert every recipient exists and belongs to `tenant_id`.

    Recipients are resolved from already-tenant-scoped source data, so a
    cross-tenant recipient should be structurally impossible; this check
    exists to catch a resolver bug before it writes a row that would leak one
    tenant's event into another tenant's inbox. The error message names
    counts, never which user belonged to which tenant, so the failure itself
    cannot be used to probe a cross-tenant relationship.
    """
    if not user_ids:
        return []

    rows = session.exec(
        select(AppUser.id, AppUser.tenant_id).where(AppUser.id.in_(list(user_ids)))
    ).all()
    tenant_by_user: Dict[UUID, UUID] = {row[0]: row[1] for row in rows}

    missing = [user_id for user_id in user_ids if user_id not in tenant_by_user]
    if missing:
        raise NotificationValidationError(
            "unknown_recipient",
            f"{len(missing)} recipient user id(s) do not exist",
        )

    foreign = [
        user_id for user_id in user_ids if tenant_by_user[user_id] != tenant_id
    ]
    if foreign:
        raise NotificationValidationError(
            "cross_tenant_recipient",
            f"{len(foreign)} recipient user id(s) belong to a different tenant",
        )
    return list(user_ids)


def create_recipient_rows(
    session: Session,
    *,
    notification_id: UUID,
    tenant_id: UUID,
    user_ids: Sequence[UUID],
    created_at: datetime,
) -> int:
    """Insert one inbox row per recipient, returning how many were created.

    `created_at` is the parent notification's exact timestamp: the column is
    denormalized precisely so the inbox query can sort without a join, which
    only works if the two values never diverge.

    ON CONFLICT DO NOTHING makes this safe to reach twice for the same
    notification. That should not happen — recipient creation is guarded by
    the notification insert's outcome — so it is a second line of defence,
    not the primary guarantee.
    """
    if not user_ids:
        return 0

    result = session.exec(
        pg_insert(NotificationRecipient)
        .values(
            [
                {
                    "notification_id": notification_id,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "status": NotificationRecipientStatus.UNREAD.value,
                    "created_at": created_at,
                    "read_at": None,
                }
                for user_id in user_ids
            ]
        )
        .on_conflict_do_nothing(
            constraint="uq_notification_recipient_notification_user"
        )
        .returning(NotificationRecipient.id)
    )
    return len(result.all())


class NotificationService:
    """Creates notifications. Nothing else in the codebase may."""

    @staticmethod
    def notify(
        session: Session,
        command: NotificationCommand,
        *,
        strict: bool = False,
    ) -> Optional[UUID]:
        """Create (or detect the duplicate of) one notification.

        Returns the `Notification.id` — the newly created one, or the
        existing one when this occurrence was already recorded — or `None` if
        notification-specific work failed and was contained.

        Does not commit. The rows are flushed into the caller's transaction
        so they land in the same atomic commit as the domain transition that
        produced them.

        With `strict=True`, contract violations (`NotificationValidationError`,
        `NotificationTemplateError`) are re-raised instead of contained. Only
        tests and development tooling should pass it; production call sites
        rely on the default, which never raises into a clinical transaction.
        """
        try:
            return NotificationService._notify(session, command)
        except (NotificationValidationError, NotificationTemplateError) as exc:
            logger.error(
                "Notification command rejected",
                extra={
                    "event": "notification.create.rejected",
                    "notification_type": command.type.value,
                    "tenant_id": str(command.tenant_id),
                    "resource_type": command.resource_type.value,
                    "resource_id": str(command.resource_id),
                    "error_code": getattr(exc, "code", "validation_error"),
                },
            )
            if strict:
                raise
            return None
        except Exception as exc:  # noqa: BLE001 — deliberate containment
            # The savepoint inside `_notify` has already rolled back this
            # service's own writes; the caller's transaction is intact and
            # still committable. `str(exc)` is not logged: a database error
            # can echo back the row it choked on, which may include rendered
            # notification content.
            logger.error(
                "Notification creation failed and was contained",
                extra={
                    "event": "notification.create.failed",
                    "notification_type": command.type.value,
                    "tenant_id": str(command.tenant_id),
                    "resource_type": command.resource_type.value,
                    "resource_id": str(command.resource_id),
                    "error_code": type(exc).__name__,
                },
            )
            if strict:
                raise
            return None

    @staticmethod
    def _notify(session: Session, command: NotificationCommand) -> UUID:
        # Céluma 1.3, Phase 3, Block F: the rendering locale is resolved here,
        # not supplied. `NotificationCommand` has no locale field for the same
        # structural reason it has no `title` field — a call site that could
        # name a locale could name one with no registered copy, and the
        # resulting fallback would be silent. When a real locale source exists
        # (user preference, then tenant default, then this default), it is
        # resolved here and every call site is unaffected.
        locale = resolve_locale(DEFAULT_LOCALE)
        template = get_template(command.type, command.template_key, locale)
        title, body, safe_params = render(template, command.template_params)
        idempotency_key = build_idempotency_key(command)

        recipient_ids = normalize_recipient_ids(command.recipient_user_ids)
        if command.exclude_actor:
            recipient_ids = exclude_actor(recipient_ids, command.created_by)
        recipient_ids = validate_recipient_tenants(
            session, recipient_ids, command.tenant_id
        )

        metadata: Dict[str, object] = {
            "template_key": template.key,
            "template_params": safe_params,
        }
        if command.extra_metadata:
            # Reserved keys win: a caller must not be able to overwrite the
            # provenance of the rendered text.
            metadata = {**command.extra_metadata, **metadata}

        created_at = datetime.utcnow()

        # Every write below is inside a SAVEPOINT. An unexpected failure
        # unwinds to here and leaves the caller's transaction usable.
        with session.begin_nested():
            inserted = session.exec(
                pg_insert(Notification)
                .values(
                    tenant_id=command.tenant_id,
                    type=command.type.value,
                    severity=command.severity.value,
                    title=title,
                    body=body,
                    resource_type=command.resource_type.value,
                    resource_id=command.resource_id,
                    notification_metadata=metadata,
                    idempotency_key=idempotency_key,
                    locale=locale,
                    created_at=created_at,
                    created_by=command.created_by,
                )
                .on_conflict_do_nothing(
                    constraint="uq_notification_tenant_idempotency_key"
                )
                .returning(Notification.id)
            ).first()

            if inserted is None:
                # Already recorded — by an earlier call or by a concurrent
                # transaction that won the unique index. Return the existing
                # id and touch nothing: frozen content stays frozen,
                # recipients are not re-resolved, no delivery row is created.
                existing_id = session.exec(
                    select(Notification.id).where(
                        Notification.tenant_id == command.tenant_id,
                        Notification.idempotency_key == idempotency_key,
                    )
                ).first()
                if existing_id is None:
                    # Only reachable if a concurrent transaction holds the
                    # winning row uncommitted. Nothing safe to return.
                    raise NotificationValidationError(
                        "duplicate_unresolvable",
                        "Notification conflicted but the existing row is not visible",
                    )
                notification_id = _scalar(existing_id)
                logger.info(
                    "Duplicate notification suppressed",
                    extra={
                        "event": "notification.create.duplicate",
                        "notification_id": str(notification_id),
                        "notification_type": command.type.value,
                        "tenant_id": str(command.tenant_id),
                        "resource_type": command.resource_type.value,
                        "resource_id": str(command.resource_id),
                        "duplicate_detected": True,
                    },
                )
                return notification_id

            notification_id = _scalar(inserted)
            recipient_count = create_recipient_rows(
                session,
                notification_id=notification_id,
                tenant_id=command.tenant_id,
                user_ids=recipient_ids,
                created_at=created_at,
            )
            delivery_count = _materialize_deliveries(
                session,
                notification_id=notification_id,
                tenant_id=command.tenant_id,
                notification_type=command.type,
                recipient_user_ids=recipient_ids,
                created_at=created_at,
            )

        if recipient_count == 0:
            # Not an error: the notification row remains the audit record
            # that the event happened, it is simply invisible in every inbox
            # (recipient matrix rule 6). Warning level so a resolver that
            # silently resolves nobody is noticeable.
            logger.warning(
                "Notification created with zero recipients",
                extra={
                    "event": "notification.create.no_recipients",
                    "notification_id": str(notification_id),
                    "notification_type": command.type.value,
                    "tenant_id": str(command.tenant_id),
                    "resource_type": command.resource_type.value,
                    "resource_id": str(command.resource_id),
                    "recipient_count": 0,
                    "created_by": str(command.created_by) if command.created_by else None,
                },
            )
        else:
            logger.info(
                "Notification created",
                extra={
                    "event": "notification.create.success",
                    "notification_id": str(notification_id),
                    "notification_type": command.type.value,
                    "tenant_id": str(command.tenant_id),
                    "resource_type": command.resource_type.value,
                    "resource_id": str(command.resource_id),
                    "recipient_count": recipient_count,
                    "delivery_count": delivery_count,
                    "created_by": str(command.created_by) if command.created_by else None,
                    "duplicate_detected": False,
                },
            )
        return notification_id


def _materialize_deliveries(
    session: Session,
    *,
    notification_id: UUID,
    tenant_id: UUID,
    notification_type,
    recipient_user_ids: Sequence[UUID],
    created_at: datetime,
) -> int:
    """Create the notification's `PENDING` delivery rows, containing any
    failure so the notification itself survives.

    **A second, nested SAVEPOINT.** The notification and its recipient rows
    are already written inside the outer savepoint when this runs, and the
    ordering of the two guarantees is not symmetric:

        notification persistence  >  optional email delivery

    Sharing one savepoint would invert it — a failure materializing an
    optional email would roll back the in-app notification, which is the
    durable operational channel and the one Block C's Notification Center
    reads. So the delivery batch gets its own savepoint: on failure Postgres
    rolls back to it, which also clears the aborted-transaction state, and
    the outer savepoint (with the notification and its recipients intact) is
    still alive and committable.

    The three outcomes this produces:

      A. The notification insert fails -> the *outer* savepoint unwinds, and
         `notify()` contains it. No notification, no recipients, no
         deliveries; the caller's own transaction is untouched.
      B. The notification and recipients succeed and materialization fails ->
         only this savepoint unwinds. The notification and every recipient
         row survive, **no partial delivery batch remains**, the caller
         continues, and one sanitized log line records it.
      C. Some recipients are simply not eligible for email -> not a failure
         at all. Eligible recipients get their rows, ineligible ones keep
         their in-app notification and get none.

    Delivery materialization is therefore all-or-nothing *as a batch*: a
    partially written set caused by an internal database error is far harder
    to reconcile later than an entirely absent one, because nothing
    downstream can tell "these three were the eligible ones" from "these
    three are the ones that happened to be inserted before the error".
    Eligibility-based omission is a different thing and is per-recipient.

    Never raises. Returns the number of rows created, or 0 on a contained
    failure.
    """
    try:
        with session.begin_nested():
            return materialize_email_deliveries(
                session,
                notification_id=notification_id,
                tenant_id=tenant_id,
                notification_type=notification_type,
                recipient_user_ids=recipient_user_ids,
                created_at=created_at,
            )
    except Exception as exc:  # noqa: BLE001 — deliberate containment
        # `str(exc)` is not logged, for the same reason it is not logged on
        # the notification containment path: a database error can echo back
        # the row it choked on, and a delivery row contains an email address.
        logger.error(
            "Delivery materialization failed and was contained",
            extra={
                "event": "notification.delivery.materialize_failed",
                "notification_id": str(notification_id),
                "notification_type": notification_type.value,
                "tenant_id": str(tenant_id),
                "recipient_count": len(recipient_user_ids),
                "error_code": type(exc).__name__,
            },
        )
        return 0


def _scalar(row) -> UUID:
    """`session.exec()` returns Row objects for Core statements and bare
    scalars for some SQLModel paths; normalize both to the UUID."""
    return row[0] if isinstance(row, (tuple, list)) or hasattr(row, "_fields") else row
