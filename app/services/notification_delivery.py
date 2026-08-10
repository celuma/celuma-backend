"""NotificationDelivery materialization and lifecycle (Céluma 1.3, Phase 3,
Block D).

One module owns the `notification_delivery` table, in two clearly separated
halves:

  **Materialization** — turning an eligible recipient of a freshly created
  notification into a `PENDING` row. Reached from
  `NotificationService.notify()`, inside the caller's transaction.

  **Lifecycle** — the `PENDING -> SENDING -> SENT/FAILED` state machine, its
  claim primitive, its retry/backoff arithmetic, and stale-claim recovery.
  Reached by nothing yet: Block E's worker is the first caller.

Kept out of `app/services/notification.py` deliberately. That module's job is
"a domain event happened, record it"; this one's is "an external channel owes
somebody a message". Folding the worker's state machine into the notification
service would make the module that a clinical transition calls synchronously
also the module that a background worker mutates concurrently.

**Block D sends nothing.** There is no SES client, no SMTP client, no HTTP
call and no email template anywhere in this file. At the end of this block an
eligible recipient's notification produces a persisted
`NotificationDelivery(status=PENDING)` and nothing consumes it.

Privacy
-------
No log line in this module contains a recipient's email address, a
notification's title or body, or a raw provider exception. `error_code` is
always passed through `sanitize_delivery_error_code`, and log records carry
ids, counts and codes only (content policy §7).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence
from uuid import UUID

from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from app.core.config import settings
from app.models.notification import (
    NotificationChannel,
    NotificationDelivery,
    NotificationDeliveryStatus,
    NotificationType,
)
from app.models.user import AppUser
from app.services.notification_policies import email_supported
from app.services.notification_preferences import (
    resolve_effective_notification_preferences,
)

logger = logging.getLogger(__name__)


class NotificationDeliveryError(RuntimeError):
    """A delivery operation was asked for something it cannot do.

    Carries a stable `code` so a caller can branch, and so the failure can be
    logged without echoing an operand.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class NotificationDeliveryTransitionError(NotificationDeliveryError):
    """A status change the state machine forbids."""


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
#
# The complete set of legal transitions. `SENT` is terminal and appears as no
# key: a delivery that a provider accepted is a historical fact, and the
# uniqueness guarantee that stops a second send depends on never walking back
# out of it.
#
# `FAILED -> PENDING` is modeled but performed by **nothing** in Block D. A
# retryable `FAILED` row already carries a due `next_attempt_at` and is
# claimed straight to `SENDING`, so demoting it to `PENDING` first would be a
# write that changes no behaviour. It is listed because it is legal — an
# operator or a future manual-retry affordance may use it — and stating that
# explicitly is better than leaving a reader to infer it from the absence of
# a function.

ALLOWED_DELIVERY_TRANSITIONS: Dict[
    NotificationDeliveryStatus, frozenset[NotificationDeliveryStatus]
] = {
    NotificationDeliveryStatus.PENDING: frozenset({NotificationDeliveryStatus.SENDING}),
    NotificationDeliveryStatus.SENDING: frozenset(
        {NotificationDeliveryStatus.SENT, NotificationDeliveryStatus.FAILED}
    ),
    NotificationDeliveryStatus.FAILED: frozenset(
        {NotificationDeliveryStatus.SENDING, NotificationDeliveryStatus.PENDING}
    ),
    NotificationDeliveryStatus.SENT: frozenset(),
}


def _assert_transition(
    delivery: NotificationDelivery, target: NotificationDeliveryStatus
) -> None:
    current = NotificationDeliveryStatus(delivery.status)
    if target not in ALLOWED_DELIVERY_TRANSITIONS[current]:
        raise NotificationDeliveryTransitionError(
            "illegal_delivery_transition",
            f"Delivery cannot move from {current.value} to {target.value}",
        )


# ---------------------------------------------------------------------------
# Retry arithmetic
# ---------------------------------------------------------------------------

def compute_backoff_seconds(attempts: int) -> int:
    """Deterministic exponential backoff: `min(base * 2^(attempts-1), max)`.

    `attempts` is the number already made, so the first retry (after one
    attempt) waits exactly `base`. Values below 1 are clamped to `base`
    rather than producing a fractional delay.

    **No jitter.** Jitter exists to spread a herd, and Céluma has one
    in-process poller claiming a bounded batch — there is no herd to spread.
    Determinism is worth more here: a test can assert the exact schedule, and
    an operator reading `next_attempt_at` can predict when a row runs.
    Block E may add provider-aware jitter once there is a provider whose rate
    limits justify it.
    """
    base = settings.notification_delivery_base_backoff_seconds
    ceiling = settings.notification_delivery_max_backoff_seconds
    if attempts < 1:
        return min(base, ceiling)
    return min(base * (2 ** (attempts - 1)), ceiling)


def compute_next_attempt_at(attempts: int, now: datetime) -> Optional[datetime]:
    """When a row that has made `attempts` attempts becomes due again.

    `None` means terminal: the maximum has been reached and nothing will
    claim this row automatically again. There is no dead-letter table —
    a `FAILED` row with `next_attempt_at IS NULL` *is* the dead letter, which
    is also what makes a future SQS migration a no-op at the schema level.
    """
    if attempts >= settings.notification_delivery_max_attempts:
        return None
    return now + timedelta(seconds=compute_backoff_seconds(attempts))


#: Everything outside this set is stripped from an error code before it is
#: persisted or logged. Codes are machine-readable identifiers, not prose.
_ERROR_CODE_ALLOWED = re.compile(r"[^a-z0-9_.:-]+")
_ERROR_CODE_MAX_LENGTH = 64

#: Anything address-shaped is refused outright rather than normalized — see
#: `sanitize_delivery_error_code`.
_ADDRESS_SHAPED = re.compile(r"[^\s@]+@[^\s@]+")

#: What an unusable or address-bearing input becomes.
GENERIC_DELIVERY_ERROR_CODE = "delivery_failed"


def sanitize_delivery_error_code(value: Optional[str]) -> str:
    """Reduce anything a caller offers to a safe, stable code.

    This is the single choke point protecting `error_code` — a column any
    future ops view could surface — from a raw provider exception. A real SES
    error message can quote the envelope it choked on, which means the
    recipient address, and content policy §7 forbids that reaching a column
    or a log line.

    Two rules, in order:

    1. **An address-shaped input is refused, not normalized.** Collapsing
       ``554 rejected for user@lab.test`` to ``554_rejected_for_user_lab.test``
       removes the `@` and satisfies a naive "no address" check while leaving
       the address plainly reconstructible. Detecting the shape and returning
       the generic code instead is the only version of this that actually
       holds. The cost is losing the provider's wording in exactly the case
       where it would have been most informative — accepted, because the
       provider's wording is not worth a PHI-adjacent leak, and Block E is
       expected to pass a code rather than a message anyway.
    2. Otherwise: lower-case, collapse everything outside `[a-z0-9_.:-]` to
       `_`, and trim to 64 characters. Truncation is deliberately silent — a
       code long enough to be truncated is prose.
    """
    if not value:
        return GENERIC_DELIVERY_ERROR_CODE
    text = str(value).strip()
    if _ADDRESS_SHAPED.search(text):
        return GENERIC_DELIVERY_ERROR_CODE
    collapsed = _ERROR_CODE_ALLOWED.sub("_", text.lower()).strip("_")
    if not collapsed:
        return GENERIC_DELIVERY_ERROR_CODE
    return collapsed[:_ERROR_CODE_MAX_LENGTH]


#: Set on a row reclaimed from an abandoned `SENDING` claim.
STALE_CLAIM_ERROR_CODE = "worker_stale_claim"


# ---------------------------------------------------------------------------
# D5 — materialization
# ---------------------------------------------------------------------------

#: Conservative address screen. Deliberately a plain regex rather than
#: `pydantic.EmailStr`: the point here is to decide whether an address is
#: worth persisting as delivery intent, not to normalize it. `email_validator`
#: (which EmailStr uses) rewrites the value it validates — IDNA-encoding the
#: domain, for example — and a rewritten address would silently make the
#: stored snapshot differ from the address on the user's account, defeating
#: the reason the snapshot exists.
_ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
_ADDRESS_MAX_LENGTH = 320  # the column's width


def normalize_recipient_address(email: Optional[str]) -> Optional[str]:
    """The address to snapshot, or `None` when it is not deliverable.

    Trimmed and lower-cased — the case-folding matters because
    `UNIQUE (notification_id, channel, recipient_address)` is a byte
    comparison, so `A@x.test` and `a@x.test` would otherwise be two rows.
    """
    if not email:
        return None
    candidate = email.strip().lower()
    if not candidate or len(candidate) > _ADDRESS_MAX_LENGTH:
        return None
    if not _ADDRESS_RE.match(candidate):
        return None
    return candidate


def materialize_email_deliveries(
    session: Session,
    *,
    notification_id: UUID,
    tenant_id: UUID,
    notification_type: NotificationType,
    recipient_user_ids: Sequence[UUID],
    created_at: datetime,
) -> int:
    """Create one `EMAIL`/`PENDING` row per eligible recipient. Returns the
    number created.

    Eligibility — every condition must hold:

      1. the notification type's policy allows email at all;
      2. the recipient exists and belongs to `tenant_id`;
      3. the recipient is active;
      4. the recipient's account email parses as an address;
      5. their effective preference has `email_enabled = true`.

    An ineligible recipient is **not** a failure. They keep their in-app
    notification and simply get no delivery row, which is why the count
    returned can legitimately be lower than `len(recipient_user_ids)` — or
    zero — without anything having gone wrong.

    The address is read from the user's own account row. A caller cannot pass
    one in: an address supplied by a caller is an address a bug can redirect,
    and the recipient set has already been tenant-validated, so the account
    is the only trustworthy source.

    `recipient_address` is a **snapshot**. If the user later changes their
    email, this row still records where the message was intended to go when
    the event happened — delivery history is an audit trail, not a view over
    current account state.

    Does not commit. The rows are flushed into the caller's transaction.
    """
    if not recipient_user_ids:
        return 0

    # The policy gate first, so an in-app-only type costs no query at all.
    if not email_supported(notification_type):
        return 0

    # One query for the user facts eligibility needs (existence, tenant,
    # activity, address) — not one per recipient.
    users = session.exec(
        select(AppUser).where(
            AppUser.id.in_(list(recipient_user_ids)),
            AppUser.tenant_id == tenant_id,
        )
    ).all()
    by_id: Dict[UUID, AppUser] = {user.id: user for user in users}

    preferences = resolve_effective_notification_preferences(
        session,
        tenant_id=tenant_id,
        user_ids=recipient_user_ids,
        notification_type=notification_type,
    )

    rows: List[dict] = []
    seen: set[UUID] = set()
    for user_id in recipient_user_ids:
        if user_id in seen:
            continue
        seen.add(user_id)

        user = by_id.get(user_id)
        if user is None or not user.is_active:
            continue

        preference = preferences.get(user_id)
        if preference is None or not preference.email_enabled:
            continue

        address = normalize_recipient_address(user.email)
        if address is None:
            continue

        rows.append(
            {
                "notification_id": notification_id,
                "tenant_id": tenant_id,
                "recipient_user_id": user_id,
                "recipient_address": address,
                "channel": NotificationChannel.EMAIL.value,
                "status": NotificationDeliveryStatus.PENDING.value,
                "attempts": 0,
                "last_attempt_at": None,
                # Due immediately. Storing `now` rather than NULL keeps the
                # claim predicate a plain `next_attempt_at <= now`, which the
                # ix_notification_delivery_poller index on
                # (status, next_attempt_at) serves directly — an
                # `IS NULL OR <=` predicate would not.
                "next_attempt_at": created_at,
                "provider_message_id": None,
                "error_code": None,
                "created_at": created_at,
                "updated_at": created_at,
            }
        )

    if not rows:
        return 0

    # ON CONFLICT DO NOTHING against the per-recipient partial unique index
    # `uq_notification_delivery_recipient_user`, created by the `v1_3_0`
    # release migration. Every row written here carries a non-null
    # recipient_user_id, so that is the index Postgres infers. The duplicate
    # defence stays in the database, exactly as it does for the notification
    # insert itself: a repeated materialization is a zero-row insert, not an
    # exception, so it cannot poison the caller's transaction.
    result = session.exec(
        pg_insert(NotificationDelivery)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=[
                NotificationDelivery.notification_id,
                NotificationDelivery.channel,
                NotificationDelivery.recipient_user_id,
            ],
            index_where=NotificationDelivery.recipient_user_id.is_not(None),
        )
        .returning(NotificationDelivery.id)
    )
    return len(result.all())


# ---------------------------------------------------------------------------
# D6 — claim
# ---------------------------------------------------------------------------

def select_due_delivery_ids(
    session: Session,
    *,
    now: Optional[datetime] = None,
    limit: Optional[int] = None,
    statuses: Optional[Sequence[NotificationDeliveryStatus]] = None,
) -> List[UUID]:
    """Lock and return the ids of up to `limit` due deliveries.

    `SELECT ... FOR UPDATE SKIP LOCKED`, which is what makes the claim safe
    for more than one worker. Céluma runs a single API task today, so this
    costs nothing that a plain read would not — but retrofitting it later,
    under production pressure, after a `desired_count` bump silently started
    double-sending, is a much worse trade than writing it correctly now.

    **Does not commit, and leaves the rows locked.** It is separated from
    `claim_pending_deliveries` for exactly that reason: a test can call it
    from two concurrent sessions and prove they receive disjoint sets, which
    is not observable through the committing wrapper.

    Due means: a claimable status, a non-null `next_attempt_at` at or before
    `now`, and attempts still under the maximum. A `NULL` `next_attempt_at`
    is the terminal marker and is excluded here rather than in the caller, so
    no code path can accidentally resurrect a row that has given up.
    """
    now = now or datetime.utcnow()
    limit = limit or settings.notification_delivery_claim_batch_size
    statuses = statuses or (
        NotificationDeliveryStatus.PENDING,
        NotificationDeliveryStatus.FAILED,
    )

    rows = session.exec(
        select(NotificationDelivery.id)
        .where(
            NotificationDelivery.status.in_([status.value for status in statuses]),
            NotificationDelivery.next_attempt_at.is_not(None),
            NotificationDelivery.next_attempt_at <= now,
            NotificationDelivery.attempts
            < settings.notification_delivery_max_attempts,
        )
        # Oldest-due first so a row that has been waiting longest is not
        # starved; the id tiebreak makes the order total, which is what keeps
        # `limit` deterministic when several rows share a timestamp (every
        # delivery of one notification does).
        .order_by(
            NotificationDelivery.next_attempt_at.asc(),
            NotificationDelivery.id.asc(),
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    return [row[0] if isinstance(row, (tuple, list)) else row for row in rows]


def claim_pending_deliveries(
    session: Session,
    *,
    now: Optional[datetime] = None,
    limit: Optional[int] = None,
) -> List[NotificationDelivery]:
    """Claim a batch of due deliveries, moving them to `SENDING`.

    **Commits.** The claim must be durable before any provider call begins,
    for the reason Block A's idempotency strategy §5 sets out: a row left
    `PENDING` while a send is in flight is a row another worker (or the same
    worker after a restart) would send again. Committing here is also what
    releases the row locks, so a long provider call never holds a database
    transaction open.

    `attempts` is incremented **here**, at the claim, not at the outcome.
    That is the conservative choice and it is deliberate: an attempt that
    crashes between the claim and its resolution has still consumed a real
    send opportunity, and a counter incremented only on a recorded outcome
    would let a crash loop retry forever without ever reaching the maximum.
    The cost is that a claim which fails before contacting the provider still
    burns an attempt — an under-delivered email, which is the safe direction.

    `next_attempt_at` is cleared while the row is `SENDING`, so a claimed row
    can never satisfy the due predicate. Recovery of an abandoned claim goes
    through `last_attempt_at` instead — see `release_stale_deliveries`.

    Tenant-agnostic by design: a worker processes the queue, not a tenant.
    Every row carries its own `tenant_id` for downstream use.
    """
    now = now or datetime.utcnow()
    delivery_ids = select_due_delivery_ids(session, now=now, limit=limit)
    if not delivery_ids:
        session.commit()
        return []

    session.exec(
        sa_update(NotificationDelivery)
        .where(NotificationDelivery.id.in_(delivery_ids))
        .values(
            status=NotificationDeliveryStatus.SENDING.value,
            attempts=NotificationDelivery.attempts + 1,
            last_attempt_at=now,
            next_attempt_at=None,
            updated_at=now,
        )
    )
    session.commit()

    claimed = session.exec(
        select(NotificationDelivery)
        .where(NotificationDelivery.id.in_(delivery_ids))
        .order_by(NotificationDelivery.id.asc())
    ).all()

    logger.info(
        "Notification deliveries claimed",
        extra={
            "event": "notification.delivery.claimed",
            "claimed_count": len(claimed),
        },
    )
    return list(claimed)


# ---------------------------------------------------------------------------
# D6 — resolution
# ---------------------------------------------------------------------------

def _load_for_update(session: Session, delivery_id: UUID) -> NotificationDelivery:
    delivery = session.exec(
        select(NotificationDelivery)
        .where(NotificationDelivery.id == delivery_id)
        .with_for_update()
    ).first()
    if delivery is None:
        raise NotificationDeliveryError(
            "delivery_not_found", "No delivery row with that id"
        )
    return delivery


def mark_delivery_sent(
    session: Session,
    delivery_id: UUID,
    *,
    provider_message_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> NotificationDelivery:
    """`SENDING -> SENT`. Commits.

    Only reachable from `SENDING`, which is the whole point of the claim
    state: it means a row can be marked sent only by the worker that took
    responsibility for sending it. `PENDING -> SENT` and `FAILED -> SENT`
    both raise, so no code path can record a send that was never claimed, and
    `SENT` is terminal so a second call raises rather than re-stamping.

    `provider_message_id` is passed by the internal caller only — there is no
    HTTP endpoint through which it could arrive — and is not part of any
    response shape in Céluma 1.3.
    """
    now = now or datetime.utcnow()
    delivery = _load_for_update(session, delivery_id)
    _assert_transition(delivery, NotificationDeliveryStatus.SENT)

    delivery.status = NotificationDeliveryStatus.SENT
    delivery.provider_message_id = provider_message_id
    # A successful send has no next attempt and no outstanding error. There
    # is no `sent_at` column: `updated_at` on a row whose terminal status is
    # SENT already records when it was sent, and a second column carrying the
    # same fact is a column that can drift (the same argument that kept
    # `delivered_at` off NotificationRecipient in Block B).
    delivery.next_attempt_at = None
    delivery.error_code = None
    delivery.updated_at = now
    session.add(delivery)
    session.commit()
    session.refresh(delivery)

    logger.info(
        "Notification delivery sent",
        extra={
            "event": "notification.delivery.sent",
            "delivery_id": str(delivery.id),
            "notification_id": str(delivery.notification_id),
            "tenant_id": str(delivery.tenant_id),
            "channel": delivery.channel,
            "attempts": delivery.attempts,
        },
    )
    return delivery


def mark_delivery_failed(
    session: Session,
    delivery_id: UUID,
    *,
    error_code: Optional[str] = None,
    now: Optional[datetime] = None,
) -> NotificationDelivery:
    """`SENDING -> FAILED`, scheduling a retry unless the row is exhausted.
    Commits.

    `error_code` is sanitized before it is stored *or* logged, so a raw
    provider exception cannot reach either. After
    `notification_delivery_max_attempts`, `next_attempt_at` is `None` and the
    row is terminal for automatic processing — it stays in the table as the
    permanent record that this delivery was attempted and given up on.
    """
    now = now or datetime.utcnow()
    delivery = _load_for_update(session, delivery_id)
    _assert_transition(delivery, NotificationDeliveryStatus.FAILED)

    safe_code = sanitize_delivery_error_code(error_code)
    delivery.status = NotificationDeliveryStatus.FAILED
    delivery.error_code = safe_code
    delivery.next_attempt_at = compute_next_attempt_at(delivery.attempts, now)
    delivery.updated_at = now
    session.add(delivery)
    session.commit()
    session.refresh(delivery)

    logger.warning(
        "Notification delivery failed",
        extra={
            "event": "notification.delivery.failed",
            "delivery_id": str(delivery.id),
            "notification_id": str(delivery.notification_id),
            "tenant_id": str(delivery.tenant_id),
            "channel": delivery.channel,
            "attempts": delivery.attempts,
            "error_code": safe_code,
            "terminal": delivery.next_attempt_at is None,
        },
    )
    return delivery


# ---------------------------------------------------------------------------
# D8 — stale-claim recovery
# ---------------------------------------------------------------------------

def release_stale_deliveries(
    session: Session,
    *,
    now: Optional[datetime] = None,
    limit: Optional[int] = None,
) -> int:
    """Recover rows abandoned in `SENDING`, returning how many were moved.

    A worker that dies between claiming a row and recording its outcome
    leaves that row `SENDING` forever: the claim predicate excludes it (its
    `next_attempt_at` is null) and no one will ever resolve it. This sweeps
    those up, using `last_attempt_at` — set at claim time — as the age.

    **Selected lifecycle: stale `SENDING` -> TERMINAL `FAILED`, with
    `next_attempt_at = NULL` and `error_code = "worker_stale_claim"`.**
    Nothing claims the row again automatically; the row is the dead letter,
    and the code distinguishes "we never learned the outcome" from "the
    provider rejected it" for whoever reads the table.

    Céluma 1.3 Phase 3, Block E, Story E7 — the retry was removed
    ------------------------------------------------------------
    Block D wrote a **backed-off** `next_attempt_at` here, so the ordinary
    claim picked the row up again. That was correct for Block D and is wrong
    now, and the reason is entirely about what sits between the claim and the
    resolution:

        Block D:  claim -> (nothing) -> resolve
        Block E:  claim -> ses.send() -> resolve

    In Block D no provider existed, so an abandoned `SENDING` row provably
    carried no delivered message and a retry could not duplicate anything —
    retrying was the more useful default for a lifecycle nothing drove. With a
    real send in that gap the window is genuinely ambiguous: a worker that
    died *after* the provider accepted the message, but *before*
    `mark_delivery_sent` committed, leaves a row that looks identical to one
    that died before the provider was ever contacted. Retrying it delivers a
    second copy of a message the provider already took.

    The trade, made explicitly:

    | | Cost |
    |---|---|
    | Retry (Block D) | A physician may receive two copies of "report published" about a clinical document |
    | Terminal (this) | A message may be silently under-delivered; the in-app notification is unaffected |

    Terminal, per Block A's idempotency strategy §5 — whose reasoning this
    restores rather than overrides — and on this codebase's own precedent that
    an ambiguous outcome earns explicit operational visibility rather than a
    silent automatic retry (the manual "Reintentar" for PDF generation). The
    in-app Notification Center is unaffected either way, which is what makes
    under-delivery the survivable direction.

    Full argument, including what would have to change to revisit it, in
    docs/celuma-1.3/phase-3-block-e/phase-3-block-e-architecture-decision.md.

    Safe to run repeatedly: a second pass finds nothing, because the first
    moved the rows out of `SENDING`. Fresh claims are untouched. Rows are
    locked with `SKIP LOCKED`, so a sweep never blocks a worker mid-claim,
    and the batch is bounded.

    Tenant-agnostic, like the claim. Sends nothing.
    """
    now = now or datetime.utcnow()
    limit = limit or settings.notification_delivery_claim_batch_size
    cutoff = now - timedelta(
        seconds=settings.notification_delivery_stale_sending_seconds
    )

    stale = session.exec(
        select(NotificationDelivery)
        .where(
            NotificationDelivery.status == NotificationDeliveryStatus.SENDING.value,
            NotificationDelivery.last_attempt_at.is_not(None),
            NotificationDelivery.last_attempt_at < cutoff,
        )
        .order_by(NotificationDelivery.last_attempt_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()

    if not stale:
        session.commit()
        return 0

    # Céluma 1.3 Phase 3, Block E, Story E7: `next_attempt_at = None`
    # unconditionally, where Block D wrote
    # `compute_next_attempt_at(delivery.attempts, now)`. A null next attempt
    # is the terminal marker the claim predicate excludes, so these rows are
    # never picked up again — see this function's docstring for why a real
    # provider makes that the correct side of the trade.
    #
    # This is also why the loop is no longer arithmetic: every row gets the
    # same value now, so it *could* be one UPDATE. It stays row-by-row because
    # the rows are already loaded and locked for the count, and one statement
    # would buy nothing on a batch bounded by `limit`.
    for delivery in stale:
        delivery.status = NotificationDeliveryStatus.FAILED
        delivery.error_code = STALE_CLAIM_ERROR_CODE
        delivery.next_attempt_at = None
        delivery.updated_at = now
        session.add(delivery)

    session.commit()

    logger.warning(
        "Recovered notification deliveries abandoned in SENDING",
        extra={
            "event": "notification.delivery.stale_released",
            "released_count": len(stale),
            "error_code": STALE_CLAIM_ERROR_CODE,
            # Always true since Story E7. Logged explicitly so an operator
            # reading the line does not have to know the block history to know
            # whether these rows will be retried.
            "terminal": True,
        },
    )
    return len(stale)
