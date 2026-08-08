"""Notification delivery worker (Céluma 1.3, Phase 3, Block E, Story E6).

The loop that finally consumes what Block D produces. An in-process asyncio
poller, started from FastAPI's `lifespan` — not a second ECS service, not
`BackgroundTasks`, not Celery (Block A's delivery strategy §3, evaluated
against all four options and unchanged by anything since).

One iteration, in the order Block D's contract requires:

    release_stale_deliveries(session)                    # transaction 1
    claimed = claim_pending_deliveries(session)          # transaction 2, commits
    contexts = load render inputs                        # transaction 2b, ended
    for context in contexts:
        message_id = provider.send(...)                  # NO transaction open
        mark_delivery_sent / mark_delivery_failed        # transaction 3

Three small transactions per row with the network call outside all of them.
The reason is in Block A's delivery strategy §4 and the lifecycle contract §3:
a slow SES call inside the claim's transaction would hold row locks for the
duration of a network round trip, which is exactly what splitting the claim
out was for.

**The read that loads templates and tenant names is a transaction too.** It is
easy to satisfy "the claim committed" and still leave an open read transaction
across every `provider.send()` of the batch, because SQLAlchemy begins one
implicitly on the next query. `_load_send_contexts` therefore copies
everything it needs into frozen dataclasses and the caller ends the
transaction before the first send — asserted by
`test_no_transaction_is_open_during_send`.

Threading
---------
The database driver (`psycopg2`) and `boto3` are both synchronous, so an
iteration runs in a worker thread via `asyncio.to_thread` and the event loop —
which is also serving HTTP — is never blocked by a send. Each iteration opens
its own `Session`; nothing is shared with a request.

Privacy
-------
Content policy §7. No log line below carries a recipient address, a subject, a
body, a template parameter or a provider exception. What they carry is ids,
the attempt number, the provider name, a provider message id, a sanitized
error code and an elapsed time — asserted by tests that put an address and a
rendered subject into the failure path and check neither reaches a record.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence
from uuid import UUID

from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import engine
from app.models.notification import (
    Notification,
    NotificationDelivery,
    NotificationType,
)
from app.models.tenant import Tenant
from app.services.email_provider import (
    EmailMessage,
    EmailProvider,
    EmailProviderError,
    EmailProviderHealth,
)
from app.services.email_templates import EmailTemplateError, render_notification_email
from app.services.notification_delivery import (
    claim_pending_deliveries,
    mark_delivery_failed,
    mark_delivery_sent,
    release_stale_deliveries,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Failure codes owned by the worker
# ---------------------------------------------------------------------------
#
# These describe failures that happen *before* a provider is reached, so they
# are not provider error codes and do not belong in `email_provider.py`. All
# of them already satisfy `sanitize_delivery_error_code`, so they reach the
# column unchanged.

#: The delivery's parent notification row could not be loaded. Only reachable
#: through a foreign key violation the database should have prevented.
ERROR_NOTIFICATION_MISSING = "notification_missing"
#: The notification carries no `template_key`, or one no email template
#: matches. A Block F wiring bug, or a delivery row that outlived a template.
ERROR_TEMPLATE_UNAVAILABLE = "email_template_unavailable"
#: `EMAIL_SENDER` is unset. Checked per row rather than assumed, because a
#: worker can outlive a configuration reload.
ERROR_SENDER_NOT_CONFIGURED = "email_sender_not_configured"
#: Anything unanticipated between the claim and the provider call.
ERROR_WORKER_UNEXPECTED = "worker_unexpected_error"


@dataclass(frozen=True)
class DeliverySendContext:
    """Everything one send needs, copied out of the ORM.

    Frozen plain values, deliberately: the objects these came from are
    attached to a `Session` whose transaction is about to end, and any
    attribute read after that point would silently re-open one. Copying is
    what makes "no transaction is open during send" a property of the code
    rather than of a reviewer's attention.
    """

    delivery_id: UUID
    notification_id: UUID
    tenant_id: UUID
    recipient_address: str
    attempts: int
    tenant_name: Optional[str]
    notification_type: Optional[NotificationType]
    template_key: Optional[str]
    template_params: Optional[dict]
    #: The locale the notification's in-app copy was rendered in (Block F).
    #: Copied off the row rather than defaulted here, so the email renders in
    #: the same locale as the notification it accompanies even if the platform
    #: default changed between creation and delivery. `None` only when the
    #: notification itself could not be loaded, which is already a failure.
    locale: Optional[str] = None


@dataclass(frozen=True)
class BatchResult:
    """What one iteration did. Returned so a test can drive the loop body
    synchronously and assert on it, and so the batch log line has something to
    report."""

    released: int = 0
    claimed: int = 0
    sent: int = 0
    failed: int = 0
    elapsed_ms: int = 0


def _load_send_contexts(
    session: Session, deliveries: Sequence[NotificationDelivery]
) -> List[DeliverySendContext]:
    """Snapshot the render inputs for a claimed batch.

    Two queries for the whole batch, not two per row: every delivery of one
    notification shares a notification, and a laboratory has one tenant, so an
    N+1 here would be N+1 round trips to render one event's fan-out.

    A missing notification or tenant is **not** an exception. It comes back as
    a `None` field and is resolved into a failed delivery by the caller, which
    is the only place that knows the delivery id to record it against.
    """
    if not deliveries:
        return []

    notification_ids = {delivery.notification_id for delivery in deliveries}
    notifications: Dict[UUID, Notification] = {
        notification.id: notification
        for notification in session.exec(
            select(Notification).where(Notification.id.in_(list(notification_ids)))
        ).all()
    }

    tenant_ids = {delivery.tenant_id for delivery in deliveries}
    tenant_names: Dict[UUID, str] = {
        tenant.id: tenant.name
        for tenant in session.exec(
            select(Tenant).where(Tenant.id.in_(list(tenant_ids)))
        ).all()
    }

    contexts: List[DeliverySendContext] = []
    for delivery in deliveries:
        notification = notifications.get(delivery.notification_id)
        metadata = (notification.notification_metadata or {}) if notification else {}
        contexts.append(
            DeliverySendContext(
                delivery_id=delivery.id,
                notification_id=delivery.notification_id,
                tenant_id=delivery.tenant_id,
                recipient_address=delivery.recipient_address,
                attempts=delivery.attempts,
                tenant_name=tenant_names.get(delivery.tenant_id),
                notification_type=(
                    NotificationType(notification.type) if notification else None
                ),
                template_key=metadata.get("template_key"),
                template_params=metadata.get("template_params"),
                locale=(notification.locale if notification else None),
            )
        )
    return contexts


def _build_message(context: DeliverySendContext) -> EmailMessage:
    """Render `context` into a message, or raise `EmailTemplateError`.

    Separated from `_send_one` so a test can assert what a given delivery
    would produce without a provider, and so every raise site here is a
    template concern rather than a delivery-lifecycle one.
    """
    if context.notification_type is None:
        raise EmailTemplateError(
            ERROR_NOTIFICATION_MISSING, "The delivery's notification is missing"
        )
    if not context.template_key:
        raise EmailTemplateError(
            ERROR_TEMPLATE_UNAVAILABLE, "The notification carries no template key"
        )

    rendered = render_notification_email(
        tenant_name=context.tenant_name or "Céluma",
        notification_type=context.notification_type,
        template_key=context.template_key,
        template_params=context.template_params,
        locale=context.locale,
    )
    return EmailMessage(
        to_address=context.recipient_address,
        subject=rendered.subject,
        text_body=rendered.text_body,
        html_body=rendered.html_body,
        from_address=(settings.email_sender or "").strip(),
        from_name=settings.email_sender_name,
    )


def process_delivery_batch(
    session: Session,
    provider: EmailProvider,
    *,
    now: Optional[datetime] = None,
    limit: Optional[int] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> BatchResult:
    """One full iteration: recover, claim, send, resolve.

    Synchronous and directly callable — which is how every worker test in this
    block drives it, with no event loop and no sleeping, exactly as Block D's
    lifecycle tests drive the state machine.

    `should_stop` is consulted between rows so shutdown does not have to wait
    out a whole batch of 50 sends. A row already claimed but not reached is
    left `SENDING` and recovered by `release_stale_deliveries` on a later run —
    terminally, since Story E7, which is the conservative outcome for a
    message that may or may not have been sent.
    """
    started = time.monotonic()

    released = release_stale_deliveries(session, now=now)
    claimed = claim_pending_deliveries(session, now=now, limit=limit)
    if not claimed:
        return BatchResult(
            released=released,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    contexts = _load_send_contexts(session, claimed)

    sent = 0
    failed = 0
    for context in contexts:
        # End whatever transaction is currently open, every time, immediately
        # before the row that will contact the provider.
        #
        # Two separate things re-open one, and missing either would put a
        # database transaction across a network call — the exact thing Block
        # A's three-transaction split exists to prevent:
        #
        #   1. `_load_send_contexts` reads notifications and tenants, which
        #      begins a transaction after the claim committed;
        #   2. `mark_delivery_sent`/`mark_delivery_failed` commit and then
        #      call `session.refresh(...)`, and that refresh is a SELECT — so
        #      resolving row N leaves a transaction open across row N+1's
        #      send.
        #
        # The second is the subtle one, and it is why this is at the top of
        # the loop rather than once before it. A rollback rather than a commit
        # because everything pending here is a read.
        # `test_no_transaction_is_open_during_send` asserts the result for
        # every row of a multi-row batch, which is what catches (2).
        session.rollback()

        if should_stop is not None and should_stop():
            logger.info(
                "Delivery batch stopped early for shutdown",
                extra={
                    "event": "notification.delivery.batch_interrupted",
                    "remaining": len(contexts) - sent - failed,
                },
            )
            break
        if _send_one(session, provider, context, now=now):
            sent += 1
        else:
            failed += 1

    result = BatchResult(
        released=released,
        claimed=len(claimed),
        sent=sent,
        failed=failed,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    logger.info(
        "Notification delivery batch processed",
        extra={
            "event": "notification.delivery.batch",
            "provider": provider.name,
            "released_count": result.released,
            "claimed_count": result.claimed,
            "sent_count": result.sent,
            "failed_count": result.failed,
            "elapsed_ms": result.elapsed_ms,
        },
    )
    return result


def _send_one(
    session: Session,
    provider: EmailProvider,
    context: DeliverySendContext,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Render, send and resolve one delivery. True when it was accepted.

    Never raises. A delivery that cannot be resolved because the *resolution
    write itself* failed is the one case that escapes as a log line rather
    than a state change — and it is left `SENDING`, where stale recovery will
    find it.
    """
    log_base = {
        "delivery_id": str(context.delivery_id),
        "notification_id": str(context.notification_id),
        "tenant_id": str(context.tenant_id),
        "attempts": context.attempts,
        "provider": provider.name,
    }

    # Checked before the message is built, not after: `EmailMessage` validates
    # its own `from_address`, so an empty sender would surface as a generic
    # construction error and lose the one diagnosis that actually tells an
    # operator what to fix. Per row rather than once per batch, because a
    # long-lived worker can outlive a configuration change.
    if not (settings.email_sender or "").strip():
        return _resolve_failed(
            session, context, ERROR_SENDER_NOT_CONFIGURED, log_base, now=now
        )

    try:
        message = _build_message(context)
    except EmailTemplateError as exc:
        # A rendering failure is deterministic: the same stored parameters
        # will fail the same way on every retry. It is still recorded through
        # the ordinary failure path rather than forced terminal, because
        # doing otherwise would mean changing `mark_delivery_failed`'s
        # contract, which Block E is not allowed to do. Bounded by
        # `max_attempts` either way.
        return _resolve_failed(session, context, exc.code, log_base, now=now)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Unexpected error preparing a notification email",
            extra={
                **log_base,
                "event": "notification.delivery.prepare_failed",
                "error_code": type(exc).__name__,
            },
        )
        return _resolve_failed(session, context, ERROR_WORKER_UNEXPECTED, log_base, now=now)

    send_started = time.monotonic()
    try:
        result = provider.send(message)
    except EmailProviderError as exc:
        elapsed_ms = int((time.monotonic() - send_started) * 1000)
        logger.warning(
            "Notification email was not accepted by the provider",
            extra={
                **log_base,
                "event": "notification.delivery.send_failed",
                "error_code": exc.code,
                "retryable": exc.retryable,
                "elapsed_ms": elapsed_ms,
            },
        )
        return _resolve_failed(session, context, exc.code, log_base, now=now)
    except Exception as exc:  # noqa: BLE001
        # A provider that raised something other than `EmailProviderError`
        # violated its own contract. Contained here rather than allowed to
        # kill the loop, and recorded under a code that names the worker so
        # the provider bug is not misread as a delivery failure. `str(exc)` is
        # never logged — an unmapped vendor exception is exactly the kind that
        # quotes the envelope.
        elapsed_ms = int((time.monotonic() - send_started) * 1000)
        logger.error(
            "Email provider raised an unmapped exception",
            extra={
                **log_base,
                "event": "notification.delivery.provider_contract_violation",
                "error_code": type(exc).__name__,
                "elapsed_ms": elapsed_ms,
            },
        )
        return _resolve_failed(session, context, ERROR_WORKER_UNEXPECTED, log_base, now=now)

    elapsed_ms = int((time.monotonic() - send_started) * 1000)
    try:
        mark_delivery_sent(
            session,
            context.delivery_id,
            provider_message_id=result.provider_message_id,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001
        # The provider accepted it and Céluma could not write that down. The
        # row stays `SENDING` and stale recovery will terminate it — which is
        # why Story E7 made recovery terminal: retrying this specific row is
        # the one case guaranteed to double-send.
        logger.error(
            "A delivery was accepted by the provider but could not be recorded",
            extra={
                **log_base,
                "event": "notification.delivery.resolution_failed",
                "error_code": type(exc).__name__,
                "elapsed_ms": elapsed_ms,
            },
        )
        return False

    logger.info(
        "Notification email accepted by the provider",
        extra={
            **log_base,
            "event": "notification.delivery.send_succeeded",
            "provider_message_id": result.provider_message_id,
            "elapsed_ms": elapsed_ms,
        },
    )
    return True


def _resolve_failed(
    session: Session,
    context: DeliverySendContext,
    error_code: str,
    log_base: dict,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Record a failed attempt. Always returns False, so a caller can
    `return _resolve_failed(...)` and read as "this one did not send"."""
    try:
        mark_delivery_failed(session, context.delivery_id, error_code=error_code, now=now)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "A failed delivery could not be recorded",
            extra={
                **log_base,
                "event": "notification.delivery.resolution_failed",
                "error_code": type(exc).__name__,
            },
        )
    return False


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class NotificationDeliveryWorker:
    """The long-lived poller. One instance, owned by `lifespan`.

    Not a singleton by construction — a test builds its own — but there is a
    module-level owner (`start_worker`/`stop_worker`) that the application
    goes through, and starting a second one on the same instance raises rather
    than silently doubling the send rate.
    """

    def __init__(
        self,
        provider: EmailProvider,
        *,
        interval_seconds: Optional[int] = None,
        session_factory: Optional[Callable[[], Session]] = None,
        shutdown_grace_seconds: float = 30.0,
    ):
        self._provider = provider
        self._interval = interval_seconds or settings.delivery_poll_interval_seconds
        self._session_factory = session_factory or (lambda: Session(engine))
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._stop_event: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._iterations = 0

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def iterations(self) -> int:
        """How many loop iterations have completed. Only used by tests and by
        the shutdown log line."""
        return self._iterations

    async def start(self) -> None:
        """Begin polling. Raises if this instance is already running.

        Raising rather than ignoring: a second start is a wiring bug, and the
        symptom of ignoring it (two loops claiming from one queue) is a
        doubled provider bill and a race that `SKIP LOCKED` makes invisible.
        """
        if self.running:
            raise RuntimeError("The notification delivery worker is already running")
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(), name="notification-delivery-worker"
        )
        logger.info(
            "Notification delivery worker started",
            extra={
                "event": "notification.delivery.worker_started",
                "provider": self._provider.name,
                "interval_seconds": self._interval,
            },
        )

    async def stop(self) -> None:
        """Stop polling, waiting out the current iteration.

        The stop event is set first, so an iteration in progress finishes the
        row it is on and then breaks out of the batch (`should_stop`). Only if
        that does not complete within the grace period is the task cancelled —
        and a cancellation cannot interrupt the `to_thread` call anyway, so the
        log line says exactly that rather than implying the send was stopped.
        """
        if self._task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()

        try:
            await asyncio.wait_for(self._task, timeout=self._shutdown_grace_seconds)
        except asyncio.TimeoutError:
            logger.warning(
                "Notification delivery worker did not stop within the grace "
                "period; cancelling. An in-flight provider call is not "
                "interrupted and its delivery stays SENDING until stale "
                "recovery terminates it.",
                extra={
                    "event": "notification.delivery.worker_stop_timeout",
                    "grace_seconds": self._shutdown_grace_seconds,
                },
            )
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        except asyncio.CancelledError:
            pass

        logger.info(
            "Notification delivery worker stopped",
            extra={
                "event": "notification.delivery.worker_stopped",
                "iterations": self._iterations,
            },
        )
        self._task = None
        self._stop_event = None

    # -- the loop ----------------------------------------------------------

    async def _run(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                # `to_thread`, because everything inside is synchronous:
                # psycopg2 blocks, and so does boto3. Running the iteration on
                # the event loop would stall every HTTP request in the process
                # for the duration of an SES round trip.
                await asyncio.to_thread(self._run_once_blocking)
            except Exception:  # noqa: BLE001
                # The loop outlives any single iteration. An iteration that
                # failed has already left the database in a consistent state
                # (each lifecycle call is its own transaction) and every row it
                # claimed is recoverable through stale recovery.
                logger.exception(
                    "Notification delivery iteration failed",
                    extra={"event": "notification.delivery.iteration_failed"},
                )
            finally:
                self._iterations += 1

            # An interruptible sleep. `asyncio.sleep(interval)` would make
            # shutdown wait out a full poll interval; waiting on the stop
            # event returns the instant `stop()` sets it.
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval
                )
            except asyncio.TimeoutError:
                continue

    def _run_once_blocking(self) -> BatchResult:
        should_stop = (
            (lambda: self._stop_event.is_set()) if self._stop_event else None
        )
        with self._session_factory() as session:
            return process_delivery_batch(
                session, self._provider, should_stop=should_stop
            )


# ---------------------------------------------------------------------------
# Application ownership
# ---------------------------------------------------------------------------
#
# `app/main.py` calls exactly these two functions. The module-level `_worker`
# is what makes "exactly one polling owner" a property of the process rather
# than of the lifespan function being called once — the same structural rule
# Block C applied to the frontend's notification polling.

_worker: Optional[NotificationDeliveryWorker] = None


async def start_worker() -> Optional[NotificationDeliveryWorker]:
    """Start the delivery worker if configuration allows it.

    Returns the worker, or `None` with one log line saying why not. It never
    raises: an email misconfiguration must not stop the API from starting —
    architectural principle §4.3/§4.7, that a clinical operation must never
    depend on email, applied to the process's own boot.

    Three gates, in order:

    1. `EMAIL_ENABLED` is false — the default. Delivery is off; nothing else
       in Céluma is affected.
    2. Configuration is incomplete (`validate_email_configuration`). Refused,
       with the reasons logged.
    3. The provider's own health probe says it cannot send. **Logged, not
       refused** — an SES blip during a deploy should not leave the process
       permanently unable to deliver, and every claim is retried anyway. The
       distinction is deliberate: Céluma's own configuration is static for the
       process's lifetime, so a problem there will not fix itself; the
       provider's availability is not.
    """
    global _worker

    if _worker is not None and _worker.running:
        logger.warning(
            "A notification delivery worker is already running; not starting a second",
            extra={"event": "notification.delivery.worker_start_refused",
                   "reason": "already_running"},
        )
        return _worker

    if not settings.email_enabled:
        logger.info(
            "Notification email delivery is disabled (EMAIL_ENABLED is false); "
            "no delivery worker started",
            extra={
                "event": "notification.delivery.worker_disabled",
                "reason": "email_disabled",
            },
        )
        return None

    problems = settings.validate_email_configuration()
    if problems:
        logger.error(
            "Notification email delivery is enabled but not configured; "
            "no delivery worker started",
            extra={
                "event": "notification.delivery.worker_start_refused",
                "reason": "invalid_configuration",
                # Fixed sentences naming variables, never their values.
                "problems": problems,
            },
        )
        return None

    from app.services.email_provider_factory import build_email_provider

    try:
        provider = build_email_provider()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "The configured email provider could not be constructed; "
            "no delivery worker started",
            extra={
                "event": "notification.delivery.worker_start_refused",
                "reason": "provider_unavailable",
                "error_code": type(exc).__name__,
            },
        )
        return None

    log_provider_health(provider)

    _worker = NotificationDeliveryWorker(provider)
    await _worker.start()
    return _worker


async def stop_worker() -> None:
    """Stop the worker started by `start_worker`, if any."""
    global _worker
    if _worker is None:
        return
    await _worker.stop()
    _worker = None


def log_provider_health(provider: EmailProvider) -> Optional[EmailProviderHealth]:
    """Probe the provider and log the result (Story E9).

    Runs once at startup, before the first poll, so a wrong region, a missing
    IAM grant or an unverified identity is visible in the boot log rather than
    inferable from five failed deliveries an hour later. Sends nothing.

    Deliberately **not** exposed over HTTP: `/health` is public and
    unauthenticated, and Block D's API-surface tests assert the notification
    surface is closed. The reasoning is in
    `phase-3-block-e-architecture-decision.md`.
    """
    try:
        health = provider.health()
    except Exception as exc:  # noqa: BLE001
        # `health()` is contractually not allowed to raise; if one does, that
        # is a provider bug and not a reason to refuse to start.
        logger.error(
            "The email provider health probe raised",
            extra={
                "event": "notification.delivery.provider_health",
                "provider": provider.name,
                "healthy": False,
                "error_code": type(exc).__name__,
            },
        )
        return None

    record = {
        "event": "notification.delivery.provider_health",
        "provider": health.provider,
        "healthy": health.healthy,
        "configured": health.configured,
        "credentials_present": health.credentials_present,
        "reachable": health.reachable,
        "error_code": health.error_code,
        "detail": health.detail,
        **{f"provider_{key}": value for key, value in (health.context or {}).items()},
    }
    if health.healthy:
        logger.info("Email provider is healthy", extra=record)
    else:
        logger.error(
            "Email provider is not healthy; deliveries will be attempted and "
            "will fail until this is resolved",
            extra=record,
        )
    return health
