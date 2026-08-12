"""Usage reconciliation worker (Céluma 1.3, Phase 4, Block D).

A second in-process asyncio poller, started from FastAPI's `lifespan`
alongside `NotificationDeliveryWorker` and deliberately separate from it:
reconciliation and email delivery share a process, not a loop. Block A's
reconciliation architecture proposal §3 chose this shape (Option A) over an
EventBridge/Fargate task or a Lambda, because it needs no new
infrastructure, no new IAM grant, and has a working precedent in this
codebase to copy rather than a design to invent.

One iteration:

    recover_stale_runs(session)                  # transaction 1
    for tenant in active tenants:                # one run per tenant,
        service.reconcile_tenant(session, ...)   # each independently
                                                 # transactional

Sequential, one tenant at a time. Reconciliation is not latency-sensitive
(the interval is hours, not seconds) and bounded concurrency would buy
nothing but contention against the same S3 endpoint and connection pool.

A failure for one tenant never stops the others: every tenant is logged,
attempted and contained on its own. That is why the loop catches around
each tenant rather than around the batch.

Threading: `psycopg2` and `boto3` are synchronous, so an iteration runs in
a worker thread via `asyncio.to_thread` — the event loop is also serving
HTTP and must never block on an S3 sweep.

Default off. `USAGE_RECONCILIATION_ENABLED` is false unless an operator
sets it, so shipping this code starts no scheduled work in any existing
environment (and keeps the worker out of the test suite, which runs the
real lifespan through `TestClient`).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, List, Optional
from uuid import UUID

from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import engine
from app.models.tenant import Tenant
from app.services.usage_reconciliation import (
    ConcurrentReconciliationError,
    UsageReconciliationService,
    recover_stale_runs,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconciliationCycleResult:
    """What one iteration did. Returned so a test can drive the loop body
    synchronously, exactly as the notification worker's `BatchResult` is."""

    recovered: int = 0
    tenants: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    elapsed_ms: int = 0


def _active_tenant_ids(session: Session) -> List[UUID]:
    """Active tenants only. A deactivated tenant's storage is frozen by
    definition — reconciling it every cycle would spend S3 calls to
    re-confirm a number nothing can change."""
    return list(
        session.exec(
            select(Tenant.id).where(Tenant.is_active == True)  # noqa: E712
        ).all()
    )


def run_reconciliation_cycle(
    session: Session,
    service: UsageReconciliationService,
    *,
    should_stop: Optional[Callable[[], bool]] = None,
) -> ReconciliationCycleResult:
    """One full cycle: recover abandoned runs, then reconcile each active
    tenant in its own set of transactions.

    Synchronous and directly callable — which is how the worker tests drive
    it, with no event loop and no sleeping.
    """
    started = time.monotonic()
    recovered = recover_stale_runs(session)

    tenant_ids = _active_tenant_ids(session)
    succeeded = 0
    failed = 0
    skipped = 0

    for tenant_id in tenant_ids:
        if should_stop is not None and should_stop():
            logger.info(
                "Reconciliation cycle stopped early for shutdown",
                extra={
                    "event": "usage_reconciliation.cycle_interrupted",
                    "remaining": len(tenant_ids) - succeeded - failed - skipped,
                },
            )
            break
        try:
            outcome = service.reconcile_tenant(
                session,
                tenant_id,
                repair=settings.usage_reconciliation_repair_enabled,
                verify_s3=settings.usage_reconciliation_s3_verify_enabled,
            )
        except ConcurrentReconciliationError:
            # Another process (or the manual endpoint) is already
            # reconciling this tenant. Not an error — the DB said so.
            skipped += 1
            continue
        except Exception:  # noqa: BLE001
            # One tenant's failure must not cost every later tenant its
            # cycle. `reconcile_tenant` already contains expected failures;
            # reaching here means something unanticipated, which is logged
            # without its message (it can quote object keys) and left
            # behind.
            failed += 1
            logger.exception(
                "Reconciliation failed for a tenant; continuing with the rest",
                extra={
                    "event": "usage_reconciliation.tenant_failed",
                    "tenant_id": str(tenant_id),
                },
            )
            session.rollback()
            continue

        if outcome.status == "SUCCEEDED":
            succeeded += 1
        else:
            failed += 1

    result = ReconciliationCycleResult(
        recovered=recovered,
        tenants=len(tenant_ids),
        succeeded=succeeded,
        failed=failed,
        skipped=skipped,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    logger.info(
        "Usage reconciliation cycle processed",
        extra={
            "event": "usage_reconciliation.cycle",
            "recovered_count": result.recovered,
            "tenant_count": result.tenants,
            "succeeded_count": result.succeeded,
            "failed_count": result.failed,
            "skipped_count": result.skipped,
            "elapsed_ms": result.elapsed_ms,
        },
    )
    return result


class UsageReconciliationWorker:
    """The long-lived poller. One instance, owned by `lifespan`.

    Same lifecycle shape as `NotificationDeliveryWorker` — start/stop, an
    interruptible sleep, `asyncio.to_thread` around the synchronous body,
    and a second `start()` on a running instance raising rather than
    silently doubling the S3 traffic.
    """

    def __init__(
        self,
        *,
        interval_seconds: Optional[int] = None,
        session_factory: Optional[Callable[[], Session]] = None,
        service: Optional[UsageReconciliationService] = None,
        shutdown_grace_seconds: float = 30.0,
    ):
        self._interval = (
            interval_seconds or settings.usage_reconciliation_interval_seconds
        )
        self._session_factory = session_factory or (lambda: Session(engine))
        self._service = service or UsageReconciliationService()
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._stop_event: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._iterations = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def iterations(self) -> int:
        return self._iterations

    async def start(self) -> None:
        if self.running:
            raise RuntimeError("The usage reconciliation worker is already running")
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="usage-reconciliation-worker")
        logger.info(
            "Usage reconciliation worker started",
            extra={
                "event": "usage_reconciliation.worker_started",
                "interval_seconds": self._interval,
                "repair_enabled": settings.usage_reconciliation_repair_enabled,
                "s3_verify_enabled": settings.usage_reconciliation_s3_verify_enabled,
            },
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=self._shutdown_grace_seconds)
        except asyncio.TimeoutError:
            logger.warning(
                "Usage reconciliation worker did not stop within the grace "
                "period; cancelling. A RUNNING row left behind is recovered "
                "by stale-run recovery on a later cycle.",
                extra={
                    "event": "usage_reconciliation.worker_stop_timeout",
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
            "Usage reconciliation worker stopped",
            extra={
                "event": "usage_reconciliation.worker_stopped",
                "iterations": self._iterations,
            },
        )
        self._task = None
        self._stop_event = None

    async def _run(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.to_thread(self._run_once_blocking)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Usage reconciliation iteration failed",
                    extra={"event": "usage_reconciliation.iteration_failed"},
                )
            finally:
                self._iterations += 1

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue

    def _run_once_blocking(self) -> ReconciliationCycleResult:
        should_stop = (
            (lambda: self._stop_event.is_set()) if self._stop_event else None
        )
        with self._session_factory() as session:
            return run_reconciliation_cycle(
                session, self._service, should_stop=should_stop
            )


# ---------------------------------------------------------------------------
# Application ownership
# ---------------------------------------------------------------------------
#
# `app/main.py` calls exactly these two functions, the same way it does for
# the notification worker. The module-level `_worker` is what makes "exactly
# one reconciliation poller" a property of the process.

_worker: Optional[UsageReconciliationWorker] = None


async def start_reconciliation_worker() -> Optional[UsageReconciliationWorker]:
    """Start the reconciliation worker if configuration allows it.

    Returns the worker, or `None` with one log line saying why not. Never
    raises: reconciliation is an operational safety net, and a safety net
    that can stop the API from booting is worse than no safety net.
    """
    global _worker

    if _worker is not None and _worker.running:
        logger.warning(
            "A usage reconciliation worker is already running; not starting a second",
            extra={
                "event": "usage_reconciliation.worker_start_refused",
                "reason": "already_running",
            },
        )
        return _worker

    if not settings.usage_reconciliation_enabled:
        logger.info(
            "Usage reconciliation is disabled (USAGE_RECONCILIATION_ENABLED is "
            "false); no reconciliation worker started",
            extra={
                "event": "usage_reconciliation.worker_disabled",
                "reason": "reconciliation_disabled",
            },
        )
        return None

    _worker = UsageReconciliationWorker()
    await _worker.start()
    return _worker


async def stop_reconciliation_worker() -> None:
    """Stop the worker started by `start_reconciliation_worker`, if any."""
    global _worker
    if _worker is None:
        return
    await _worker.stop()
    _worker = None
