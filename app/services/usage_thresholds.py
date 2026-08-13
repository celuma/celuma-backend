"""UsageThresholdService — usage-limit awareness (Céluma 1.3, Phase 4,
Block G).

The one component that decides whether a tenant has *crossed* a usage
threshold, remembers that it did, and turns the crossing into exactly one
notification. Every trigger — a clinical storage write, a user lifecycle
change, a limit change, a reconciliation repair — goes through here; there is
no second implementation anywhere, and in particular reconciliation does not
have its own.

    usage-changing operation
            |
            v
    UsageThresholdService.evaluate(...)
            |
            v
    durable state  (tenant_usage_threshold_state, row-locked)
            |
            v
    upward transition?  -> NotificationService.notify(...)
            |
            v
    existing Phase 3 delivery infrastructure

Awareness, not enforcement
--------------------------
Phase 4 measures. **Nothing in this module rejects, disables, throttles or
degrades anything.** A tenant at 250% of its storage limit uploads samples,
generates PDFs, publishes reports, creates users and logs in exactly as one
at 5% does. The copy the notifications carry is written to match (see
`notification_templates.py`); if enforcement is ever wanted it is a separate
product decision with its own block and its own gate.

Why the state has to be durable
--------------------------------
"The tenant is above 80%" is true on *every* request once it becomes true.
The naive shape —

    if percent >= 80: notify()

— sends one notification per sample upload, forever, to every admin. What is
notifiable is not the condition but the **transition into** it, and a
transition is only observable against a remembered previous state. That
memory is `tenant_usage_threshold_state`, one row per `(tenant, resource)`,
and it is the primary idempotency mechanism of this block. The notification
table's own `idempotency_key` is a second line of defence layered on top of
it, not a substitute for it.

The frontend is not, and cannot be, the trigger
------------------------------------------------
Block F's `USAGE_WARNING_PERCENT` / `USAGE_OVER_LIMIT_PERCENT` are
presentation values that decide what colour a bar is drawn in. They are not
read here, not imported, and not mirrored from — the direction of authority
runs the other way. A dashboard-driven trigger would fire only while someone
had the page open, would re-fire on every 7-second poll tick, would have
nowhere durable to record "already sent", and could not answer the
tenant-level RBAC question of who the recipients are. See
`phase-4-block-f/block-g-dependencies.md` §3.

Transaction and failure model
------------------------------
Every public method here runs its whole body inside `session.begin_nested()`
— a SAVEPOINT — and never commits, never rolls back the caller's
transaction, and never raises. Two properties follow, and both are load-
bearing:

1. **The transition and its notification are atomic.** They are written
   inside one savepoint, in the caller's transaction, so they commit together
   with the business mutation that caused them or not at all. The invariant
   the idempotency contract states —

       transition recorded  <=>  notification creation durably recorded

   — holds by construction rather than by sequencing. This is why
   `NotificationService.notify(..., strict=True)` is used: the default
   contains its own failures and returns `None`, which would leave a
   recorded transition with no notification behind it and permanently
   swallow that crossing.

2. **A threshold failure cannot break a clinical workflow.** If anything in
   here fails, the savepoint unwinds — discarding the state write and the
   notification, and clearing PostgreSQL's aborted-transaction state — and
   the caller's transaction is left alive and committable. The sample upload
   commits, the counter is correct, the threshold state is simply unchanged,
   and the *next* evaluation (or the next reconciliation run) re-derives it
   from live usage and limits. Nothing is lost that is not recomputable.

Concurrency
-----------
Two requests can push a tenant across 80% at the same time. Both must not
notify. Serialization is the database's: the state row is created (if
absent) with `INSERT ... ON CONFLICT DO NOTHING` against
`uq_tenant_usage_threshold_state_tenant_resource`, then taken with
`SELECT ... FOR UPDATE`. The second evaluator blocks until the first commits,
then reads the state the first wrote and finds no transition to make. No
process-local lock is involved, so this holds across workers and across
processes. Proved against real PostgreSQL in
`tests/http/test_usage_threshold_concurrency.py`.

Logging
-------
Structured, ids/counts/states only. No recipient email address, no user name,
no object key, no bucket, no patient/report/sample reference, and no raw
exception message — a database error can echo the row it choked on, so
failures are logged as `exception_type` (the class name) plus a stable code,
following the sanitization rules Block E established for reconciliation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Mapping, Optional, Tuple
from uuid import UUID, uuid4

from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from app.models.notification import (
    NotificationResourceType,
    NotificationSeverity,
    NotificationType,
)
from app.models.tenant_limits import TenantLimits
from app.models.tenant_usage import TenantUsage
from app.models.tenant_usage_threshold_state import (
    USAGE_THRESHOLD_STATE_RANK,
    TenantUsageThresholdState,
    UsageResource,
    UsageThresholdState,
)
from app.schemas.notification import NotificationCommand
from app.services.notification import NotificationService
from app.services.notification_integrations.recipients import (
    resolve_usage_threshold_recipients,
)
from app.services.notification_templates import CURRENT_TEMPLATE_KEY
from app.services.usage import UsageService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Policy — the single backend source of truth for the threshold numbers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UsageThresholdPolicy:
    """The two percentages one resource is measured against.

    A typed object rather than four loose module constants so that a resource
    is *given* its policy at the one place the mapping lives, and no service
    can accidentally compare storage usage against the user percentages. Both
    values are whole-number percents; see `derive_state` for why they are
    never turned into floats.
    """

    approaching_percent: int
    reached_percent: int

    def __post_init__(self) -> None:
        if not 0 < self.approaching_percent <= self.reached_percent:
            raise ValueError(
                "approaching_percent must be positive and at most reached_percent"
            )


#: Céluma 1.3's initial policy: warn at 80%, report the limit reached at 100%,
#: for both resources.
#:
#: **90% is deliberately not a state.** Block F's dashboard has a third visual
#: band, and the temptation is to mirror it here. It is resisted: a third
#: notifying state would mean a third notification for the same tenant on the
#: way up, for a boundary that carries no new information — "you are near the
#: limit" was already said at 80%, and "you are at the limit" is said at 100%.
#: The frontend's presentation bands and this policy are independent by
#: design, and are allowed to differ.
STORAGE_THRESHOLD_POLICY = UsageThresholdPolicy(
    approaching_percent=80, reached_percent=100
)
USER_THRESHOLD_POLICY = UsageThresholdPolicy(
    approaching_percent=80, reached_percent=100
)

#: The only place a resource is paired with its percentages. Nothing else in
#: the backend may hardcode 80 or 100 for a usage threshold — a second copy is
#: how the two silently diverge.
USAGE_THRESHOLD_POLICIES: Dict[UsageResource, UsageThresholdPolicy] = {
    UsageResource.STORAGE: STORAGE_THRESHOLD_POLICY,
    UsageResource.USERS: USER_THRESHOLD_POLICY,
}

#: `(resource, state) -> NotificationType`. Only the two notifying states
#: appear: `NORMAL` and `UNMONITORED` are real states with no notification, and
#: modelling them here as `None` would invite a caller to send one.
NOTIFICATION_TYPE_BY_TRANSITION: Dict[
    Tuple[UsageResource, UsageThresholdState], NotificationType
] = {
    (
        UsageResource.STORAGE,
        UsageThresholdState.APPROACHING,
    ): NotificationType.STORAGE_USAGE_APPROACHING,
    (
        UsageResource.STORAGE,
        UsageThresholdState.REACHED,
    ): NotificationType.STORAGE_LIMIT_REACHED,
    (
        UsageResource.USERS,
        UsageThresholdState.APPROACHING,
    ): NotificationType.USER_LIMIT_APPROACHING,
    (
        UsageResource.USERS,
        UsageThresholdState.REACHED,
    ): NotificationType.USER_LIMIT_REACHED,
}

#: Both threshold notifications are `WARNING`, not `INFO` and not
#: `ACTION_REQUIRED`. `INFO` would put "the laboratory is over its configured
#: limit" in the same visual register as "a sample changed state";
#: `ACTION_REQUIRED` would promise an action that does not exist — nothing is
#: blocked, so there is nothing the recipient must do to restore service.
USAGE_THRESHOLD_SEVERITY = NotificationSeverity.WARNING


# ---------------------------------------------------------------------------
# Why a resource may not be evaluable
# ---------------------------------------------------------------------------

#: No limit is configured for this resource — `TenantLimits` is absent, or the
#: column is NULL. There is no threshold to cross, so no state can be
#: meaningful and no notification is possible. This is a *decision* someone
#: made (or never made), not missing information, which is why it resets any
#: previously remembered state to `UNMONITORED`.
UNEVALUABLE_UNLIMITED = "unlimited"

#: Storage usage has never been initialized — no `TenantUsage` row. This is
#: **absent information**, not a zero: Block B's contract is explicit that a
#: missing row does not mean 0 bytes. Any remembered state is therefore left
#: exactly as it was rather than being overwritten from a number nobody has.
#: (There is no user equivalent: `active_internal_users` is computed live, so
#: it is always known.)
UNEVALUABLE_USAGE_UNINITIALIZED = "usage_uninitialized"


@dataclass(frozen=True)
class UsageThresholdEvaluation:
    """What one evaluation did. Returned for logging, tests and callers that
    want to assert on the outcome; no production caller branches on it."""

    resource: UsageResource
    previous_state: UsageThresholdState
    new_state: UsageThresholdState
    used_value: Optional[int]
    limit_value: Optional[int]
    notification_type: Optional[NotificationType] = None
    notification_id: Optional[UUID] = None
    recipient_count: int = 0
    #: True when the evaluation itself failed and was contained. The state
    #: row is then unchanged and a later evaluation will retry.
    failed: bool = False
    #: Set when the resource could not be evaluated — one of the two
    #: `UNEVALUABLE_*` codes above.
    unevaluable_reason: Optional[str] = None

    @property
    def notified(self) -> bool:
        return self.notification_id is not None


# ---------------------------------------------------------------------------
# State derivation — raw integers, never a rounded percentage
# ---------------------------------------------------------------------------

def derive_state(
    used: Optional[int], limit: Optional[int], policy: UsageThresholdPolicy
) -> UsageThresholdState:
    """Where `used`/`limit` sits, from exact integer arithmetic.

    **No floats, and no rounded percentage.** `GET /api/v1/tenant/usage`
    rounds `usage_percent` to two decimals for display, and a tenant at
    79.9996% renders as `80.0`. Deciding state from that number would fire an
    APPROACHING notification for a tenant that is not at 80%. The comparison
    is therefore

        used * 100 >= limit * percent

    which is exact for every value a `BIGINT` can hold, has no
    division-by-zero case (a configured limit is `> 0` by CHECK constraint),
    and no rounding mode to get wrong. `reached` is checked before
    `approaching` so a tenant that is over the limit is never mislabelled.

    `None` for either operand yields `UNMONITORED` — the caller decides what
    to do with that, because the two reasons are not interchangeable.
    """
    if used is None or limit is None or limit <= 0:
        return UsageThresholdState.UNMONITORED
    if used * 100 >= limit * policy.reached_percent:
        return UsageThresholdState.REACHED
    if used * 100 >= limit * policy.approaching_percent:
        return UsageThresholdState.APPROACHING
    return UsageThresholdState.NORMAL


def display_percent(used: int, limit: int) -> int:
    """The whole-number percentage the notification copy shows.

    Floored, not rounded: the body says "aproximadamente el N%", and flooring
    guarantees the sentence never claims more usage than there is. Display
    only — `derive_state` never sees this value.
    """
    return (used * 100) // limit


def is_upward(
    previous: UsageThresholdState, new: UsageThresholdState
) -> bool:
    """Whether `previous -> new` is a crossing worth telling someone about.

    Upward by the rank in `tenant_usage_threshold_state.py`, where
    `UNMONITORED` sits *below* `NORMAL`. That single ordering choice makes
    three cases fall out of one rule instead of needing their own branches:

      - first evaluation ever (a row that is still `UNMONITORED`) at 85%
        is `UNMONITORED -> APPROACHING`, upward, one notification;
      - a limit configured for the first time, or restored under existing
        usage, is the same shape and behaves identically;
      - every downward move — including into `UNMONITORED` when a limit is
        removed — is not upward, so it silently re-arms instead of notifying.
    """
    return USAGE_THRESHOLD_STATE_RANK[new] > USAGE_THRESHOLD_STATE_RANK[previous]


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def _storage_inputs(
    session: Session, tenant_id: UUID
) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """`(used_bytes, limit_bytes, unevaluable_reason)` for `STORAGE`.

    Read as two scalar SELECTs rather than `session.get()`, deliberately: the
    counter is mutated by a Core `UPDATE` on the same transaction
    (`UsageService.adjust_storage`), and an ORM identity-map hit could hand
    back the pre-update value. A scalar read always goes to the database and
    therefore always sees this transaction's own writes.

    **The counter is the source, never a recomputation.** Nothing here calls
    `StorageBillingService.compute_billable_storage_bytes()`: that is a scan
    over the tenant's storage objects, and running it on every sample upload
    to answer a threshold question would make the fast path slow to no
    purpose. Repairing the counter is reconciliation's job, and a repair
    triggers its own evaluation.
    """
    used = session.exec(
        select(TenantUsage.billable_storage_bytes).where(
            TenantUsage.tenant_id == tenant_id
        )
    ).first()
    used = _scalar(used)

    limit = _scalar(
        session.exec(
            select(TenantLimits.storage_limit_bytes).where(
                TenantLimits.tenant_id == tenant_id
            )
        ).first()
    )

    if used is None:
        return None, limit, UNEVALUABLE_USAGE_UNINITIALIZED
    if limit is None:
        return used, None, UNEVALUABLE_UNLIMITED
    return used, limit, None


def _user_inputs(
    session: Session, tenant_id: UUID
) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """`(active_internal_users, user_limit, unevaluable_reason)` for `USERS`.

    `active_internal_users` is the licensed-seat numerator and the only one —
    `registered_users` counts inactive accounts and `active_physician_portal_
    users` counts portal-only physicians, and neither consumes a seat (Block
    B's `tenant-user-metrics-contract.md`, restated in Block E's
    `usage-response-semantics.md` §3). It is computed live, so there is no
    "uninitialized" case for this resource: the number is always known, and
    the only reason users can be unevaluable is an absent limit.
    """
    limit = _scalar(
        session.exec(
            select(TenantLimits.user_limit).where(
                TenantLimits.tenant_id == tenant_id
            )
        ).first()
    )
    if limit is None:
        return None, None, UNEVALUABLE_UNLIMITED

    metrics = UsageService.get_user_metrics(session, tenant_id)
    return metrics.active_internal_users, limit, None


_INPUT_READERS = {
    UsageResource.STORAGE: _storage_inputs,
    UsageResource.USERS: _user_inputs,
}


def _scalar(row):
    """`session.exec()` yields Row objects for column selects and bare values
    elsewhere; normalize both."""
    if row is None:
        return None
    if isinstance(row, (tuple, list)) or hasattr(row, "_fields"):
        return row[0]
    return row


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------

class UsageThresholdService:
    """Threshold evaluation for one tenant/resource pair.

    Owns the current value, the limit, the comparison, the state machine, the
    durable state and the decision to notify. It owns none of the things it
    depends on: usage numbers come from `UsageService`, recipients from the
    Phase 3 resolver module, and the notification itself from
    `NotificationService`. Nothing here re-implements any of them.
    """

    @staticmethod
    def evaluate(
        session: Session,
        tenant_id: UUID,
        resource: UsageResource,
        *,
        source: str,
        actor_id: Optional[UUID] = None,
        now: Optional[datetime] = None,
    ) -> UsageThresholdEvaluation:
        """Evaluate one resource. Never raises, never commits.

        `source` is a short, stable label for what triggered this
        (`sample_image_upload`, `user_created`, `usage_reconciliation`, …).
        It reaches the structured logs only — it is not persisted and not
        rendered.

        `actor_id` becomes `Notification.created_by`. It does **not** remove
        that user from the recipient set: usage thresholds set
        `exclude_actor=False`, unlike every Phase 3 event. See
        `resolve_usage_threshold_recipients`.
        """
        now = now or datetime.utcnow()
        try:
            with session.begin_nested():
                return UsageThresholdService._evaluate(
                    session,
                    tenant_id,
                    resource,
                    source=source,
                    actor_id=actor_id,
                    now=now,
                )
        except Exception as exc:  # noqa: BLE001 — deliberate containment
            # The savepoint has unwound: the state row is untouched, no
            # notification exists, PostgreSQL's aborted-transaction state is
            # cleared, and the caller's transaction is still committable. A
            # later evaluation — the next storage write, the next user
            # change, the next reconciliation run — re-derives the same state
            # from live data and retries. `str(exc)` is never logged: a
            # database error quotes the row it choked on.
            logger.error(
                "Usage threshold evaluation failed and was contained",
                extra={
                    "event": "usage_threshold.evaluation_failed",
                    "tenant_id": str(tenant_id),
                    "resource": resource.value,
                    "source": source,
                    "error_code": "threshold_evaluation_failed",
                    "exception_type": type(exc).__name__,
                },
            )
            return UsageThresholdEvaluation(
                resource=resource,
                previous_state=UsageThresholdState.UNMONITORED,
                new_state=UsageThresholdState.UNMONITORED,
                used_value=None,
                limit_value=None,
                failed=True,
            )

    @staticmethod
    def evaluate_storage(
        session: Session,
        tenant_id: UUID,
        *,
        source: str,
        actor_id: Optional[UUID] = None,
        now: Optional[datetime] = None,
    ) -> UsageThresholdEvaluation:
        """`evaluate(..., UsageResource.STORAGE)`."""
        return UsageThresholdService.evaluate(
            session,
            tenant_id,
            UsageResource.STORAGE,
            source=source,
            actor_id=actor_id,
            now=now,
        )

    @staticmethod
    def evaluate_users(
        session: Session,
        tenant_id: UUID,
        *,
        source: str,
        actor_id: Optional[UUID] = None,
        now: Optional[datetime] = None,
    ) -> UsageThresholdEvaluation:
        """`evaluate(..., UsageResource.USERS)`."""
        return UsageThresholdService.evaluate(
            session,
            tenant_id,
            UsageResource.USERS,
            source=source,
            actor_id=actor_id,
            now=now,
        )

    @staticmethod
    def evaluate_tenant(
        session: Session,
        tenant_id: UUID,
        *,
        source: str,
        actor_id: Optional[UUID] = None,
        now: Optional[datetime] = None,
    ) -> Dict[UsageResource, UsageThresholdEvaluation]:
        """Evaluate **both** resources.

        This is the limit-change hook (master spec §31): changing or removing
        `TenantLimits.storage_limit_bytes` / `user_limit` can cross — or
        un-cross — a threshold without any usage moving at all. A tenant using
        80 GB against a 100 GB limit is at 80%; drop the limit to 70 GB and it
        is at 114%, having uploaded nothing. Any code that makes a limit
        durable must call this afterwards, in the same transaction, or the
        remembered state silently describes a limit that no longer exists.

        Céluma 1.3 has **no production write path for `TenantLimits`** —
        confirmed by grep across `app/`, and asserted by
        `tests/http/test_usage_threshold_triggers.py::TestLimitMutation
        Inventory::test_no_production_module_writes_tenant_limits`, which is
        what stops that statement from quietly going stale. The hook exists,
        is fully tested against every limit-change scenario, and is waiting
        for the endpoint that will need it.
        """
        return {
            resource: UsageThresholdService.evaluate(
                session,
                tenant_id,
                resource,
                source=source,
                actor_id=actor_id,
                now=now,
            )
            for resource in UsageResource
        }

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _evaluate(
        session: Session,
        tenant_id: UUID,
        resource: UsageResource,
        *,
        source: str,
        actor_id: Optional[UUID],
        now: datetime,
    ) -> UsageThresholdEvaluation:
        used, limit, unevaluable = _INPUT_READERS[resource](session, tenant_id)
        if unevaluable is not None:
            return UsageThresholdService._handle_unevaluable(
                session,
                tenant_id,
                resource,
                reason=unevaluable,
                source=source,
                now=now,
            )

        row = UsageThresholdService._lock_state_row(session, tenant_id, resource, now)
        previous_state = UsageThresholdState(row.state)
        policy = USAGE_THRESHOLD_POLICIES[resource]
        new_state = derive_state(used, limit, policy)

        notification_type: Optional[NotificationType] = None
        notification_id: Optional[UUID] = None
        recipient_count = 0
        transition_count = row.transition_count

        if is_upward(previous_state, new_state):
            notification_type = NOTIFICATION_TYPE_BY_TRANSITION.get(
                (resource, new_state)
            )

        if notification_type is not None:
            transition_count += 1
            recipients = resolve_usage_threshold_recipients(
                session, tenant_id=tenant_id
            )
            recipient_count = len(recipients)
            # strict=True on purpose. The default swallows its own failures
            # and returns None, which would let the state write below record
            # a transition whose notification never existed — permanently
            # losing that crossing, because the state machine would then
            # believe the recipient had already been told. Raising instead
            # unwinds `evaluate`'s savepoint, so neither is written and the
            # next evaluation retries the pair.
            notification_id = NotificationService.notify(
                session,
                NotificationCommand(
                    tenant_id=tenant_id,
                    type=notification_type,
                    severity=USAGE_THRESHOLD_SEVERITY,
                    resource_type=NotificationResourceType.TENANT,
                    resource_id=tenant_id,
                    occurrence_marker=(
                        f"{resource.value}:{new_state.value}:{transition_count}"
                    ),
                    template_key=CURRENT_TEMPLATE_KEY[notification_type],
                    template_params=_template_params(
                        new_state, used=used, limit=limit
                    ),
                    recipient_user_ids=recipients,
                    created_by=actor_id,
                    # The one Phase 3 rule this event does not follow — see
                    # `resolve_usage_threshold_recipients` for why.
                    exclude_actor=False,
                ),
                strict=True,
            )

        state_changed = new_state != previous_state
        session.exec(
            sa_update(TenantUsageThresholdState)
            .where(TenantUsageThresholdState.id == row.id)
            .values(
                state=new_state.value,
                last_value=used,
                last_limit=limit,
                transition_count=transition_count,
                last_transition_at=(
                    now if state_changed else row.last_transition_at
                ),
                updated_at=now,
            )
        )

        _log_evaluation(
            tenant_id=tenant_id,
            resource=resource,
            previous_state=previous_state,
            new_state=new_state,
            used=used,
            limit=limit,
            source=source,
            notification_type=notification_type,
            notification_id=notification_id,
            recipient_count=recipient_count,
        )

        return UsageThresholdEvaluation(
            resource=resource,
            previous_state=previous_state,
            new_state=new_state,
            used_value=used,
            limit_value=limit,
            notification_type=notification_type,
            notification_id=notification_id,
            recipient_count=recipient_count,
        )

    @staticmethod
    def _handle_unevaluable(
        session: Session,
        tenant_id: UUID,
        resource: UsageResource,
        *,
        reason: str,
        source: str,
        now: datetime,
    ) -> UsageThresholdEvaluation:
        """A resource with no answer. Never notifies; the two reasons differ
        in what they do to remembered state.

        **Unlimited** resets any remembered state to `UNMONITORED` and clears
        the numbers it was derived from. Removing a limit is a deliberate act,
        and leaving a stale `REACHED` behind would mean a tenant that is now
        unlimited still reads as over a limit, and — worse — that restoring a
        limit later could not produce a notification, because `REACHED ->
        REACHED` is not an upward move. Resetting re-arms it.

        **Uninitialized storage** changes nothing at all. A missing
        `TenantUsage` row is absent information, not zero (Block B's contract,
        and Block E's `null` table); overwriting a meaningful `APPROACHING`
        with `UNMONITORED` because a counter row is temporarily missing would
        both destroy history and re-arm a crossing that never un-crossed, so
        the tenant would be notified a second time when the row came back.
        Nothing is written, no row is created, and the state resumes exactly
        where it was once usage is initialized again.

        No row is created in either case: an absent row already means "never
        evaluated", which is what both of these are.
        """
        previous_state = UsageThresholdState.UNMONITORED
        if reason == UNEVALUABLE_UNLIMITED:
            # One statement, no lock needed: it is idempotent, it touches
            # nothing when the row is already UNMONITORED (so
            # `last_transition_at` is not churned on every evaluation of an
            # unlimited tenant), and it cannot race — two concurrent runs of
            # it produce the same row.
            existing = session.exec(
                select(TenantUsageThresholdState.state).where(
                    TenantUsageThresholdState.tenant_id == tenant_id,
                    TenantUsageThresholdState.resource == resource.value,
                )
            ).first()
            existing_state = _scalar(existing)
            if existing_state is not None:
                previous_state = UsageThresholdState(existing_state)
            if (
                existing_state is not None
                and existing_state != UsageThresholdState.UNMONITORED.value
            ):
                session.exec(
                    sa_update(TenantUsageThresholdState)
                    .where(
                        TenantUsageThresholdState.tenant_id == tenant_id,
                        TenantUsageThresholdState.resource == resource.value,
                    )
                    .values(
                        state=UsageThresholdState.UNMONITORED.value,
                        last_value=None,
                        last_limit=None,
                        last_transition_at=now,
                        updated_at=now,
                    )
                )

        logger.info(
            "Usage threshold not evaluable",
            extra={
                "event": "usage_threshold.unmonitored",
                "tenant_id": str(tenant_id),
                "resource": resource.value,
                "source": source,
                "reason": reason,
                "previous_state": previous_state.value,
            },
        )
        return UsageThresholdEvaluation(
            resource=resource,
            previous_state=previous_state,
            new_state=(
                UsageThresholdState.UNMONITORED
                if reason == UNEVALUABLE_UNLIMITED
                else previous_state
            ),
            used_value=None,
            limit_value=None,
            unevaluable_reason=reason,
        )

    @staticmethod
    def _lock_state_row(
        session: Session,
        tenant_id: UUID,
        resource: UsageResource,
        now: datetime,
    ) -> TenantUsageThresholdState:
        """The tenant/resource state row, created if absent, locked either way.

        Upsert-then-lock, and the order matters. `SELECT ... FOR UPDATE`
        cannot lock a row that does not exist, so two concurrent *first*
        evaluations would both find nothing, both derive `APPROACHING`, and
        both notify. Inserting first makes the unique index the serialization
        point: `ON CONFLICT DO NOTHING` blocks on a conflicting uncommitted
        insert until that transaction resolves, so exactly one row exists by
        the time either evaluator reaches its `FOR UPDATE`, and the loser then
        waits on the lock and reads whatever the winner committed.

        The row is inserted `UNMONITORED` with no values — the same state as
        "not evaluable" — because that is exactly what "never evaluated" is,
        and it is what makes the first real evaluation an ordinary upward
        transition rather than a special case.
        """
        session.exec(
            pg_insert(TenantUsageThresholdState)
            .values(
                id=uuid4(),
                tenant_id=tenant_id,
                resource=resource.value,
                state=UsageThresholdState.UNMONITORED.value,
                last_value=None,
                last_limit=None,
                transition_count=0,
                last_transition_at=None,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                constraint="uq_tenant_usage_threshold_state_tenant_resource"
            )
        )
        row = session.exec(
            select(TenantUsageThresholdState)
            .where(
                TenantUsageThresholdState.tenant_id == tenant_id,
                TenantUsageThresholdState.resource == resource.value,
            )
            .with_for_update()
        ).first()
        if row is None:
            # Unreachable: the insert above either created the row or found a
            # committed one. Raising rather than continuing means the
            # savepoint unwinds and the caller is unaffected, instead of a
            # `None` propagating into an attribute access.
            raise RuntimeError(
                "Threshold state row is not visible immediately after upsert"
            )
        return row


def _template_params(
    state: UsageThresholdState, *, used: int, limit: int
) -> Mapping[str, object]:
    """The notification's template parameters — a percentage, or nothing.

    The two REACHED templates declare no parameters at all, so passing one
    would be rejected by the registry as `unknown_param`. The two APPROACHING
    templates take the floored integer percentage their copy interpolates.
    Neither carries a byte count, a user count, a limit or an id: the
    dashboard at `/config/usage` is where the actual numbers live, behind the
    permission that gates them.
    """
    if state is UsageThresholdState.APPROACHING:
        return {"usage_percent": display_percent(used, limit)}
    return {}


def _log_evaluation(
    *,
    tenant_id: UUID,
    resource: UsageResource,
    previous_state: UsageThresholdState,
    new_state: UsageThresholdState,
    used: int,
    limit: int,
    source: str,
    notification_type: Optional[NotificationType],
    notification_id: Optional[UUID],
    recipient_count: int,
) -> None:
    """Two events, not one: every evaluation is `usage_threshold.evaluated`
    at DEBUG (there is one per storage write, so INFO would drown the log),
    and a real transition additionally emits `usage_threshold.transition` and
    `usage_threshold.notification_created` at INFO.

    Fields are ids, states, integers and a stable source label. No recipient
    email address, no user name, no object key, no bucket, no patient or
    report reference — the allow-list Block E's sanitization rules establish.
    """
    base = {
        "tenant_id": str(tenant_id),
        "resource": resource.value,
        "previous_state": previous_state.value,
        "new_state": new_state.value,
        "used_value": used,
        "limit_value": limit,
        "source": source,
    }
    logger.debug(
        "Usage threshold evaluated",
        extra={"event": "usage_threshold.evaluated", **base},
    )
    if new_state == previous_state:
        return

    logger.info(
        "Usage threshold state changed",
        extra={
            "event": "usage_threshold.transition",
            **base,
            "notified": notification_id is not None,
        },
    )
    if notification_id is not None:
        logger.info(
            "Usage threshold notification created",
            extra={
                "event": "usage_threshold.notification_created",
                **base,
                "notification_type": (
                    notification_type.value if notification_type else None
                ),
                "notification_id": str(notification_id),
                "recipient_count": recipient_count,
            },
        )


# ---------------------------------------------------------------------------
# The storage write-flow hook
# ---------------------------------------------------------------------------

def record_storage_delta_with_thresholds(
    session: Session,
    tenant_id: UUID,
    delta_bytes: int,
    *,
    source: str,
    resource_type: Optional[str] = None,
    actor_id: Optional[UUID] = None,
) -> None:
    """`UsageService.record_storage_delta`, then a storage threshold
    evaluation. **The one call every production storage write flow makes.**

    Why a wrapper rather than a call inside `UsageService.record_storage_delta`
    itself: Block C's accounting contract (§8) states, as a design commitment,
    that `app/services/usage.py` stays a pure counter mutation — "Block G can
    add threshold evaluation as a caller of these methods, not a change to
    them". Honouring that keeps the atomic-`UPDATE` primitive free of a state
    machine, a notification service and a recipient resolver, and avoids the
    import cycle that reaching back into those from `usage.py` would create.
    The cost is that a *new* storage flow could call the bare primitive and
    silently skip its threshold evaluation — which is why
    `tests/http/test_usage_threshold_triggers.py::TestStorageTriggerCoverage`
    asserts that no production module outside `usage.py` calls
    `record_storage_delta` directly.

    Transaction boundary: **the caller's**. Both the counter mutation and the
    threshold work are flushed into the transaction that is about to commit
    the `StorageObject` change, so all three land in one atomic commit — a
    rollback takes the notification with it, and there is no window in which a
    committed notification describes a storage change that never happened.
    Nothing here commits.

    Neither half can fail the caller. `record_storage_delta` contains a
    missing `TenantUsage` row (Block C), and `evaluate_storage` contains
    everything of its own inside a savepoint. A zero delta still evaluates:
    the counter did not move, but the *limit* may have, and an evaluation that
    finds no transition costs one indexed read and one small update.
    """
    UsageService.record_storage_delta(
        session, tenant_id, delta_bytes, source=source, resource_type=resource_type
    )
    UsageThresholdService.evaluate_storage(
        session, tenant_id, source=source, actor_id=actor_id
    )
