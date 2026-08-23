"""Céluma 1.3, Phase 4, Block G — threshold idempotency under real
concurrency.

The invariant:

    one semantic threshold transition  ->  one notification event

Two simultaneous uploads can push a tenant from 79% to 82% together. Both
evaluate. Both must not notify. Nothing about that is provable with mocked
arithmetic or a patched lock, so every test here runs **real PostgreSQL,
several threads, several connections** — the same shape Block C used to prove
`adjust_storage`'s atomicity (`test_usage_service.py::
TestConcurrentAdjustments`), because that is the only shape that can fail for
the right reason.

Serialization is entirely the database's:

    INSERT ... ON CONFLICT DO NOTHING   -- the unique index decides who
                                        -- creates the row
    SELECT ... FOR UPDATE               -- and who waits for the winner

There is no process-local lock anywhere in `usage_thresholds.py`. A
`threading.Lock` would pass these tests on one process and fail silently the
day Céluma runs two API tasks, which is precisely the failure this module
exists to rule out.
"""
from __future__ import annotations

import threading

import pytest
from sqlmodel import Session, select

from app.models.notification import Notification, NotificationType
from app.models.tenant_limits import TenantLimits
from app.models.tenant_usage_threshold_state import (
    TenantUsageThresholdState,
    UsageResource,
    UsageThresholdState,
)
from app.services.usage import UsageService
from app.services.usage_thresholds import (
    UsageThresholdService,
    record_storage_delta_with_thresholds,
)

from .factories import create_branch, create_tenant, create_user

LIMIT = 10_000


def _setup(engine, *, used: int, limit: int | None = LIMIT, admins: int = 2):
    """A tenant with a counter, a limit and some `admin:manage_tenant`
    holders, committed and visible to every connection."""
    with Session(engine) as session:
        tenant = create_tenant(session, name="Concurrent Lab")
        create_branch(session, tenant)
        for index in range(admins):
            create_user(
                session, tenant, email=f"admin{index}@lab.test", roles=("admin",)
            )
        UsageService.initialize_usage(
            session, tenant.id, billable_storage_bytes=used, source="test"
        )
        session.add(TenantLimits(tenant_id=tenant.id, storage_limit_bytes=limit))
        session.commit()
        return tenant.id


def _run_concurrently(target, count: int):
    """Start `count` threads at once and collect anything they raise.

    A barrier rather than a bare `start()` loop: without it the first thread
    routinely finishes before the last one begins, and the test would pass
    against an implementation with no locking at all.
    """
    errors: list[BaseException] = []
    barrier = threading.Barrier(count)

    def wrapped(index: int):
        try:
            barrier.wait(timeout=10)
            target(index)
        except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        # Generous: a thread blocked forever on a row lock is a real failure
        # and must surface as one rather than hanging the suite.
        thread.join(timeout=60)
        assert not thread.is_alive(), "a thread is still blocked on a lock"
    return errors


def _notifications(engine, tenant_id, notification_type=None):
    with Session(engine) as session:
        statement = select(Notification).where(Notification.tenant_id == tenant_id)
        if notification_type is not None:
            statement = statement.where(Notification.type == notification_type.value)
        return list(session.exec(statement).all())


def _state(engine, tenant_id, resource=UsageResource.STORAGE):
    with Session(engine) as session:
        return session.exec(
            select(TenantUsageThresholdState).where(
                TenantUsageThresholdState.tenant_id == tenant_id,
                TenantUsageThresholdState.resource == resource.value,
            )
        ).first()


def _usage(engine, tenant_id):
    with Session(engine) as session:
        return UsageService.get_usage(session, tenant_id).billable_storage_bytes


class TestConcurrentCrossing:
    def test_two_writes_crossing_80_percent_together_notify_once(self, engine):
        """The scenario from the contract, verbatim: 79% + two concurrent
        mutations = 82%, and exactly one APPROACHING notification.

        Both writers increment the counter and both evaluate, in separate
        transactions on separate connections. Whichever commits first records
        the transition; the second blocks on the state row's `FOR UPDATE`,
        wakes to find APPROACHING already recorded, and has no upward move to
        make.
        """
        tenant_id = _setup(engine, used=7_900)  # 79%

        def upload(_index: int):
            with Session(engine) as session:
                record_storage_delta_with_thresholds(
                    session, tenant_id, 150, source="sample_image_upload"
                )
                session.commit()

        assert _run_concurrently(upload, 2) == []

        # Both counter mutations applied — the threshold work must not have
        # cost either writer its update.
        assert _usage(engine, tenant_id) == 8_200

        created = _notifications(engine, tenant_id)
        assert len(created) == 1, [n.type for n in created]
        assert created[0].type == NotificationType.STORAGE_USAGE_APPROACHING.value

        row = _state(engine, tenant_id)
        assert row.state == UsageThresholdState.APPROACHING
        assert row.transition_count == 1

    def test_ten_concurrent_first_evaluations_create_one_state_row(self, engine):
        """The upsert-then-lock race, at width.

        Ten evaluators arrive with no state row in existence. Without the
        `INSERT ... ON CONFLICT DO NOTHING` there is nothing for `FOR UPDATE`
        to lock, so all ten would find "never evaluated", all ten would derive
        APPROACHING, and all ten would notify. The unique constraint is what
        makes exactly one of them the winner.
        """
        tenant_id = _setup(engine, used=8_500)  # 85%

        def evaluate(_index: int):
            with Session(engine) as session:
                UsageThresholdService.evaluate_storage(
                    session, tenant_id, source="test"
                )
                session.commit()

        assert _run_concurrently(evaluate, 10) == []

        with Session(engine) as session:
            rows = list(
                session.exec(
                    select(TenantUsageThresholdState).where(
                        TenantUsageThresholdState.tenant_id == tenant_id
                    )
                ).all()
            )
        assert len(rows) == 1
        assert len(_notifications(engine, tenant_id)) == 1

    def test_ten_concurrent_evaluations_at_reached_transition_once(self, engine):
        tenant_id = _setup(engine, used=12_000)  # 120%

        def evaluate(_index: int):
            with Session(engine) as session:
                UsageThresholdService.evaluate_storage(
                    session, tenant_id, source="test"
                )
                session.commit()

        assert _run_concurrently(evaluate, 10) == []

        created = _notifications(engine, tenant_id)
        assert len(created) == 1
        assert created[0].type == NotificationType.STORAGE_LIMIT_REACHED.value
        assert _state(engine, tenant_id).transition_count == 1

    def test_concurrent_user_and_storage_crossings_do_not_block_each_other(
        self, engine
    ):
        """Different resources are different rows, so they must not serialize
        against one another — and each still transitions exactly once."""
        tenant_id = _setup(engine, used=12_000)
        with Session(engine) as session:
            limits = session.get(TenantLimits, tenant_id)
            limits.user_limit = 1
            session.add(limits)
            session.commit()

        def evaluate(index: int):
            with Session(engine) as session:
                if index % 2 == 0:
                    UsageThresholdService.evaluate_storage(
                        session, tenant_id, source="test"
                    )
                else:
                    UsageThresholdService.evaluate_users(
                        session, tenant_id, source="test"
                    )
                session.commit()

        assert _run_concurrently(evaluate, 8) == []

        assert {n.type for n in _notifications(engine, tenant_id)} == {
            NotificationType.STORAGE_LIMIT_REACHED.value,
            NotificationType.USER_LIMIT_REACHED.value,
        }
        assert len(_notifications(engine, tenant_id)) == 2

    def test_a_burst_of_writes_across_two_bands_notifies_once_per_band(
        self, engine
    ):
        """Twelve concurrent uploads take the tenant from 75% to 105%. The
        counter must be exact, and the recipients must be told at most once
        about each band they actually entered — never twelve times, and never
        zero."""
        tenant_id = _setup(engine, used=7_500)

        def upload(_index: int):
            with Session(engine) as session:
                record_storage_delta_with_thresholds(
                    session, tenant_id, 250, source="sample_image_upload"
                )
                session.commit()

        assert _run_concurrently(upload, 12) == []

        assert _usage(engine, tenant_id) == 10_500
        types = [n.type for n in _notifications(engine, tenant_id)]
        assert len(types) == len(set(types)), f"duplicate notification: {types}"
        assert set(types) <= {
            NotificationType.STORAGE_USAGE_APPROACHING.value,
            NotificationType.STORAGE_LIMIT_REACHED.value,
        }
        # The tenant ends over its limit, so it must have been told that much.
        assert NotificationType.STORAGE_LIMIT_REACHED.value in types
        assert _state(engine, tenant_id).state == UsageThresholdState.REACHED

    def test_the_lock_is_in_the_database_not_the_process(self, engine):
        """A direct proof that the serialization point is the row.

        One session takes the state row's lock and holds it. A second session
        attempts an evaluation and must **block** — if it completed, the
        service would be relying on something process-local, which buys
        nothing the moment Céluma runs more than one worker.
        """
        tenant_id = _setup(engine, used=8_500)
        with Session(engine) as session:
            UsageThresholdService.evaluate_storage(session, tenant_id, source="test")
            session.commit()

        finished = threading.Event()
        holder_may_release = threading.Event()

        def hold_the_lock():
            with Session(engine) as session:
                session.exec(
                    select(TenantUsageThresholdState)
                    .where(TenantUsageThresholdState.tenant_id == tenant_id)
                    .with_for_update()
                ).first()
                holder_may_release.wait(timeout=10)
                session.commit()

        def try_to_evaluate():
            with Session(engine) as session:
                UsageThresholdService.evaluate_storage(
                    session, tenant_id, source="test"
                )
                session.commit()
            finished.set()

        holder = threading.Thread(target=hold_the_lock)
        holder.start()
        # Give the holder time to acquire before the contender starts.
        threading.Event().wait(0.5)

        contender = threading.Thread(target=try_to_evaluate)
        contender.start()

        assert not finished.wait(timeout=2.0), (
            "the second evaluation completed while the state row was locked — "
            "the lock is not doing anything"
        )

        holder_may_release.set()
        holder.join(timeout=15)
        contender.join(timeout=15)
        assert finished.is_set()
        # And still exactly one notification, from the first evaluation.
        assert len(_notifications(engine, tenant_id)) == 1


class TestConcurrentSafetyOfTheWriteFlow:
    def test_a_failing_evaluation_never_costs_a_concurrent_writer_its_delta(
        self, engine, monkeypatch
    ):
        """Containment, under concurrency. Ten writers upload while every
        threshold evaluation fails; all ten counter mutations must still
        commit, because a threshold failure unwinds only its own savepoint."""
        tenant_id = _setup(engine, used=7_900)

        def boom(*args, **kwargs):
            raise RuntimeError("simulated threshold failure")

        monkeypatch.setattr(
            "app.services.usage_thresholds.UsageThresholdService._evaluate", boom
        )

        def upload(_index: int):
            with Session(engine) as session:
                record_storage_delta_with_thresholds(
                    session, tenant_id, 100, source="sample_image_upload"
                )
                session.commit()

        assert _run_concurrently(upload, 10) == []

        assert _usage(engine, tenant_id) == 8_900
        assert _notifications(engine, tenant_id) == []

        # And the state is recoverable: one clean evaluation afterwards
        # produces the notification that was contained.
        monkeypatch.undo()
        with Session(engine) as session:
            UsageThresholdService.evaluate_storage(session, tenant_id, source="test")
            session.commit()
        assert len(_notifications(engine, tenant_id)) == 1
