"""Delivery worker tests (Céluma 1.3, Phase 3, Block E — Stories E6, E8, E9,
E10).

This is where Block D's state machine and Block E's provider meet. Everything
runs against real PostgreSQL, through `NotificationService.notify()` so the
delivery rows are the ones production would create, and against
`FakeEmailProvider` so **no AWS credential, network call or clock is
involved**.

The batch function is driven synchronously — `process_delivery_batch(session,
provider)` — exactly as Block D's tests drive the lifecycle functions
directly. The asyncio loop around it is tested separately and briefly, because
what is worth asserting about a loop is that it starts once, stops cleanly and
does not swallow its own iteration failures.

Four properties are load-bearing here and each has its own class:

  `TestBatchProcessing`     a PENDING row ends up SENT, with the right envelope
  `TestFailureAndRetry`     failure -> backoff -> retry -> terminal
  `TestTransactionBoundary` no transaction is open across `provider.send()`
  `TestLogging`             no address, subject, body or raw exception is logged
"""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app.core.config import settings
from app.models.notification import (
    Notification,
    NotificationDelivery,
    NotificationDeliveryStatus,
    NotificationResourceType,
    NotificationType,
)
from app.schemas.notification import NotificationCommand
from app.services.email_provider import (
    ERROR_MESSAGE_REJECTED,
    ERROR_PROVIDER_UNAVAILABLE,
    ERROR_SES_THROTTLED,
    EmailProviderError,
    EmailProviderHealth,
)
from app.services.email_provider_fake import FakeEmailProvider
from app.services.notification import NotificationService
from app.services.notification_delivery import (
    STALE_CLAIM_ERROR_CODE,
    claim_pending_deliveries,
)
from app.services.notification_delivery_worker import (
    ERROR_NOTIFICATION_MISSING,
    ERROR_SENDER_NOT_CONFIGURED,
    ERROR_TEMPLATE_UNAVAILABLE,
    NotificationDeliveryWorker,
    log_provider_health,
    process_delivery_batch,
    start_worker,
    stop_worker,
)

from tests.http.factories import create_branch, create_tenant, create_user

SUPPORTED = NotificationType.REPORT_PUBLISHED
TEMPLATE_KEY = "report_published_v1"
TEMPLATE_PARAMS = {"order_number": "ORD-2026-00152", "actor_name": "Dra. Martínez"}

USER_ADDRESS = "user@tenant-a.test"
PEER_ADDRESS = "peer@tenant-a.test"


@pytest.fixture(name="world")
def world_fixture(session):
    tenant = create_tenant(session, name="Patología y Nefropatología")
    create_branch(session, tenant)
    user = create_user(session, tenant, email=USER_ADDRESS)
    peer = create_user(session, tenant, email=PEER_ADDRESS)
    return {"tenant": tenant, "user": user, "peer": peer}


@pytest.fixture(name="sender")
def sender_fixture(monkeypatch):
    """A configured sender for the duration of a test.

    `EMAIL_SENDER` has no default on purpose (Story E1), so without this the
    worker would correctly refuse every send — which is itself asserted, in
    `test_an_unconfigured_sender_fails_the_delivery`.
    """
    monkeypatch.setattr(settings, "email_sender", "notificaciones@celuma.test")
    monkeypatch.setattr(settings, "email_sender_name", "Céluma")
    return settings.email_sender


@pytest.fixture(name="provider")
def provider_fixture():
    return FakeEmailProvider()


def notify(session, world, recipients, *, params=None):
    """Create a real notification, which materializes real delivery rows."""
    notification_id = NotificationService.notify(
        session,
        NotificationCommand(
            tenant_id=world["tenant"].id,
            type=SUPPORTED,
            resource_type=NotificationResourceType.REPORT,
            resource_id=uuid.uuid4(),
            occurrence_marker=f"m-{uuid.uuid4()}",
            template_key=TEMPLATE_KEY,
            template_params=params or TEMPLATE_PARAMS,
            recipient_user_ids=[user.id for user in recipients],
        ),
        strict=True,
    )
    session.commit()
    return notification_id


def deliveries(session, notification_id=None):
    query = select(NotificationDelivery)
    if notification_id is not None:
        query = query.where(NotificationDelivery.notification_id == notification_id)
    return session.exec(query).all()


def only_delivery(session, notification_id=None) -> NotificationDelivery:
    session.expire_all()
    rows = deliveries(session, notification_id)
    assert len(rows) == 1
    return rows[0]


# ---------------------------------------------------------------------------
# E6 — the batch
# ---------------------------------------------------------------------------


class TestBatchProcessing:
    def test_a_pending_delivery_is_sent_and_marked_sent(
        self, session, world, provider, sender
    ):
        """The whole point of Block E, in one test: Block D leaves a PENDING
        row and nothing consumes it; now something does."""
        notification_id = notify(session, world, [world["user"]])
        assert only_delivery(session, notification_id).status == (
            NotificationDeliveryStatus.PENDING
        )

        result = process_delivery_batch(session, provider)

        assert (result.claimed, result.sent, result.failed) == (1, 1, 0)
        row = only_delivery(session, notification_id)
        assert row.status == NotificationDeliveryStatus.SENT
        assert row.attempts == 1
        assert row.next_attempt_at is None
        assert row.error_code is None

    def test_the_provider_message_id_is_stored(
        self, session, world, provider, sender
    ):
        """Block E's only support-correlation handle (dependencies §11)."""
        notification_id = notify(session, world, [world["user"]])
        process_delivery_batch(session, provider)

        assert only_delivery(session, notification_id).provider_message_id == (
            "fake-message-1"
        )

    def test_the_message_goes_to_the_snapshotted_address(
        self, session, world, provider, sender
    ):
        """Not the user's *current* email — the address the delivery row
        recorded when the event happened (materialization contract §3)."""
        notify(session, world, [world["user"]])
        world["user"].email = "changed@tenant-a.test"
        session.add(world["user"])
        session.commit()

        process_delivery_batch(session, provider)

        assert [record.to_address for record in provider.sent] == [USER_ADDRESS]

    def test_the_envelope_is_rendered_from_the_email_registry(
        self, session, world, provider, sender
    ):
        notify(session, world, [world["user"]])
        process_delivery_batch(session, provider)

        record = provider.sent[0]
        assert record.subject == (
            "Patología y Nefropatología — Reporte publicado (Orden ORD-2026-00152)"
        )
        assert record.from_address == "notificaciones@celuma.test"
        assert record.from_name == "Céluma"

    def test_the_email_does_not_reuse_the_in_app_body(
        self, session, world, provider, sender
    ):
        """The in-app body names the actor ("El reporte fue publicado y
        firmado por Dra. Martínez"). The email registry does not declare that
        parameter, so it cannot arrive — which is the mechanism, not a
        coincidence of the current copy."""
        notification_id = notify(session, world, [world["user"]])
        process_delivery_batch(session, provider)

        notification = session.get(Notification, notification_id)
        assert "Martínez" in (notification.body or "")
        assert "Martínez" not in provider.sent[0].text_body
        assert "Martínez" not in provider.sent[0].subject

    def test_two_recipients_each_get_their_own_message(
        self, session, world, provider, sender
    ):
        notify(session, world, [world["user"], world["peer"]])

        result = process_delivery_batch(session, provider)

        assert result.sent == 2
        assert sorted(record.to_address for record in provider.sent) == sorted(
            [USER_ADDRESS, PEER_ADDRESS]
        )

    def test_an_empty_queue_sends_nothing(self, session, provider, sender):
        result = process_delivery_batch(session, provider)

        assert (result.claimed, result.sent, result.failed) == (0, 0, 0)
        assert provider.sent_count == 0

    def test_a_sent_delivery_is_not_sent_again(
        self, session, world, provider, sender
    ):
        """`SENT` is terminal and the claim excludes it. This is the property
        that makes a poller safe to run every ten seconds forever."""
        notify(session, world, [world["user"]])
        process_delivery_batch(session, provider)

        for _ in range(3):
            process_delivery_batch(session, provider)

        assert provider.sent_count == 1

    def test_the_batch_respects_the_claim_batch_size(
        self, session, world, provider, sender, monkeypatch
    ):
        monkeypatch.setattr(settings, "notification_delivery_claim_batch_size", 2)
        for _ in range(4):
            notify(session, world, [world["user"]])

        result = process_delivery_batch(session, provider)

        assert result.claimed == 2
        assert provider.sent_count == 2

    def test_stale_rows_are_released_before_claiming(
        self, session, world, provider, sender
    ):
        """`release_stale_deliveries` runs at the top of every iteration."""
        notification_id = notify(session, world, [world["user"]])
        claimed = claim_pending_deliveries(session)
        stale = claimed[0]
        stale.last_attempt_at = datetime.utcnow() - timedelta(
            seconds=settings.notification_delivery_stale_sending_seconds + 60
        )
        session.add(stale)
        session.commit()

        result = process_delivery_batch(session, provider)

        assert result.released == 1
        row = only_delivery(session, notification_id)
        assert row.error_code == STALE_CLAIM_ERROR_CODE
        # Story E7: terminal, so the same iteration does not then claim it.
        assert row.next_attempt_at is None
        assert result.claimed == 0


class TestFailureAndRetry:
    def test_a_provider_failure_marks_the_delivery_failed_with_a_backoff(
        self, session, world, provider, sender
    ):
        provider.fail_addresses[USER_ADDRESS] = ERROR_SES_THROTTLED
        notification_id = notify(session, world, [world["user"]])

        result = process_delivery_batch(session, provider)

        assert (result.sent, result.failed) == (0, 1)
        row = only_delivery(session, notification_id)
        assert row.status == NotificationDeliveryStatus.FAILED
        assert row.error_code == ERROR_SES_THROTTLED
        assert row.next_attempt_at is not None

    def test_the_provider_code_reaches_the_column_unchanged(
        self, session, world, provider, sender
    ):
        """The reason the adapter maps to codes rather than passing a message:
        `sanitize_delivery_error_code` would reduce a real SES message to
        `delivery_failed` because it quotes an address."""
        provider.fail_addresses[USER_ADDRESS] = ERROR_MESSAGE_REJECTED
        notification_id = notify(session, world, [world["user"]])

        process_delivery_batch(session, provider)

        assert only_delivery(session, notification_id).error_code == "message_rejected"

    def test_a_failed_delivery_is_retried_when_it_becomes_due_and_can_succeed(
        self, session, world, provider, sender
    ):
        """Transient outage, then recovery — driven by the fake's script, with
        no sleeping and no clock manipulation beyond making the row due."""
        provider.fail_next_n = 1
        notification_id = notify(session, world, [world["user"]])

        process_delivery_batch(session, provider)
        row = only_delivery(session, notification_id)
        assert row.status == NotificationDeliveryStatus.FAILED

        row.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
        session.add(row)
        session.commit()

        process_delivery_batch(session, provider)

        row = only_delivery(session, notification_id)
        assert row.status == NotificationDeliveryStatus.SENT
        assert row.attempts == 2
        assert row.error_code is None

    def test_repeated_failure_ends_terminal_at_the_attempt_ceiling(
        self, session, world, provider, sender
    ):
        """Retry cannot amplify without bound. After `max_attempts` the row
        keeps `FAILED` with a null `next_attempt_at` and *is* the dead
        letter."""
        provider.fail_addresses[USER_ADDRESS] = ERROR_PROVIDER_UNAVAILABLE
        notification_id = notify(session, world, [world["user"]])

        for _ in range(settings.notification_delivery_max_attempts):
            process_delivery_batch(session, provider)
            row = only_delivery(session, notification_id)
            if row.next_attempt_at is not None:
                row.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
                session.add(row)
                session.commit()

        row = only_delivery(session, notification_id)
        assert row.status == NotificationDeliveryStatus.FAILED
        assert row.attempts == settings.notification_delivery_max_attempts
        assert row.next_attempt_at is None

        assert process_delivery_batch(session, provider).claimed == 0

    def test_one_failing_recipient_does_not_stop_the_others(
        self, session, world, provider, sender
    ):
        provider.fail_addresses[USER_ADDRESS] = ERROR_MESSAGE_REJECTED
        notification_id = notify(session, world, [world["user"], world["peer"]])

        result = process_delivery_batch(session, provider)

        assert (result.sent, result.failed) == (1, 1)
        assert [record.to_address for record in provider.sent] == [PEER_ADDRESS]
        statuses = {
            row.recipient_address: row.status
            for row in deliveries(session, notification_id)
        }
        assert statuses[USER_ADDRESS] == NotificationDeliveryStatus.FAILED
        assert statuses[PEER_ADDRESS] == NotificationDeliveryStatus.SENT

    def test_a_provider_that_violates_its_contract_does_not_kill_the_loop(
        self, session, world, provider, sender
    ):
        """A provider raising something other than `EmailProviderError` is a
        bug in that provider. It is contained under a code that names the
        worker, so the provider bug is not misread as a delivery failure."""

        class BrokenProvider(FakeEmailProvider):
            def send(self, message):
                raise RuntimeError(f"leaky {message.to_address}")

        notification_id = notify(session, world, [world["user"]])

        result = process_delivery_batch(session, BrokenProvider())

        assert result.failed == 1
        assert only_delivery(session, notification_id).error_code == (
            "worker_unexpected_error"
        )

    def test_an_unconfigured_sender_fails_the_delivery_rather_than_sending(
        self, session, world, provider, monkeypatch
    ):
        """`EMAIL_SENDER` has no default (Story E1). Checked per row, because
        a long-lived worker can outlive a configuration change."""
        monkeypatch.setattr(settings, "email_sender", None)
        notification_id = notify(session, world, [world["user"]])

        result = process_delivery_batch(session, provider)

        assert result.failed == 1
        assert provider.sent_count == 0
        assert only_delivery(session, notification_id).error_code == (
            ERROR_SENDER_NOT_CONFIGURED
        )


class TestRenderFailures:
    """A delivery whose content cannot be produced still has to be resolved —
    leaving it `SENDING` would strand it until stale recovery terminates it."""

    def test_a_notification_without_a_template_key_fails_the_delivery(
        self, session, world, provider, sender
    ):
        notification_id = notify(session, world, [world["user"]])
        notification = session.get(Notification, notification_id)
        notification.notification_metadata = {"template_params": TEMPLATE_PARAMS}
        session.add(notification)
        session.commit()

        result = process_delivery_batch(session, provider)

        assert (result.sent, result.failed) == (0, 1)
        assert only_delivery(session, notification_id).error_code == (
            ERROR_TEMPLATE_UNAVAILABLE
        )
        assert provider.sent_count == 0

    def test_an_unknown_template_key_fails_the_delivery(
        self, session, world, provider, sender
    ):
        notification_id = notify(session, world, [world["user"]])
        notification = session.get(Notification, notification_id)
        notification.notification_metadata = {
            "template_key": "retired_template_v0",
            "template_params": TEMPLATE_PARAMS,
        }
        session.add(notification)
        session.commit()

        process_delivery_batch(session, provider)

        assert only_delivery(session, notification_id).error_code == (
            "email_template_not_found"
        )

    def test_unsafe_stored_parameters_fail_the_delivery(
        self, session, world, provider, sender
    ):
        """The render-time screen earning its keep: parameters written by an
        earlier version of the code, or edited by hand, are screened again."""
        notification_id = notify(session, world, [world["user"]])
        notification = session.get(Notification, notification_id)
        notification.notification_metadata = {
            "template_key": TEMPLATE_KEY,
            "template_params": {"order_number": "<script>alert(1)</script>"},
        }
        session.add(notification)
        session.commit()

        process_delivery_batch(session, provider)

        assert only_delivery(session, notification_id).error_code == (
            "email_param_unsafe_content"
        )
        assert provider.sent_count == 0

    def test_a_render_failure_is_bounded_by_the_attempt_ceiling(
        self, session, world, provider, sender
    ):
        """A rendering failure is deterministic — every retry fails the same
        way. It is still recorded through the ordinary failure path, because
        forcing it terminal would mean changing `mark_delivery_failed`'s
        contract, which Block E may not do. Bounded either way."""
        notification_id = notify(session, world, [world["user"]])
        notification = session.get(Notification, notification_id)
        notification.notification_metadata = {"template_key": None}
        session.add(notification)
        session.commit()

        for _ in range(settings.notification_delivery_max_attempts + 2):
            process_delivery_batch(session, provider)
            row = only_delivery(session, notification_id)
            if row.next_attempt_at is not None:
                row.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
                session.add(row)
                session.commit()

        row = only_delivery(session, notification_id)
        assert row.attempts == settings.notification_delivery_max_attempts
        assert row.next_attempt_at is None

    def test_the_error_codes_the_worker_produces_survive_the_sanitizer(self):
        from app.services.notification_delivery import sanitize_delivery_error_code

        for code in (
            ERROR_NOTIFICATION_MISSING,
            ERROR_SENDER_NOT_CONFIGURED,
            ERROR_TEMPLATE_UNAVAILABLE,
            "worker_unexpected_error",
        ):
            assert sanitize_delivery_error_code(code) == code


class TestTransactionBoundary:
    """Block A's delivery strategy §4: three small transactions per row, with
    the network call outside all of them."""

    def test_no_transaction_is_open_during_send(
        self, session, world, provider, sender
    ):
        """The subtle one. The claim commits, but loading the notification and
        the tenant to render the email opens a *new* transaction — and without
        an explicit end, it would stay open across every send in the batch,
        which is exactly what splitting the claim out was meant to prevent."""
        observed = []

        class ObservingProvider(FakeEmailProvider):
            def send(self, message):
                observed.append(session.in_transaction())
                return super().send(message)

        notify(session, world, [world["user"], world["peer"]])

        process_delivery_batch(session, ObservingProvider())

        assert observed == [False, False]

    def test_the_claim_is_durable_before_the_send(
        self, session, world, provider, sender
    ):
        """A row left `PENDING` during an in-flight send is a row another
        worker would send again (Block A idempotency §5). By the time the
        provider is called, the row is committed as `SENDING` with its attempt
        already counted."""
        seen = {}

        class ObservingProvider(FakeEmailProvider):
            def send(self, message):
                session.expire_all()
                row = session.exec(select(NotificationDelivery)).first()
                seen["status"] = row.status
                seen["attempts"] = row.attempts
                session.rollback()
                return super().send(message)

        notify(session, world, [world["user"]])
        process_delivery_batch(session, ObservingProvider())

        assert seen == {"status": NotificationDeliveryStatus.SENDING, "attempts": 1}


class TestShutdownBehaviour:
    def test_the_batch_stops_between_rows_when_asked_to(
        self, session, world, provider, sender
    ):
        """Shutdown does not have to wait out a batch of fifty sends. The row
        left claimed stays `SENDING` and is terminated by stale recovery —
        which is the conservative outcome for a message that may or may not
        have been sent."""
        notify(session, world, [world["user"], world["peer"]])

        result = process_delivery_batch(
            session, provider, should_stop=lambda: provider.sent_count >= 1
        )

        assert result.claimed == 2
        assert result.sent == 1
        statuses = {row.status for row in deliveries(session)}
        assert NotificationDeliveryStatus.SENDING in statuses


# ---------------------------------------------------------------------------
# E6 — the loop
# ---------------------------------------------------------------------------


class TestWorkerLoop:
    def test_it_starts_polls_and_stops(self, session, world, provider, sender):
        """One real turn of the asyncio loop, with a short interval so the
        test does not sleep. Anything longer belongs to the batch tests."""
        notify(session, world, [world["user"]])

        worker = NotificationDeliveryWorker(
            provider,
            interval_seconds=1,
            session_factory=lambda: _NonClosingSession(session),
        )

        async def run():
            await worker.start()
            assert worker.running
            for _ in range(50):
                if provider.sent_count:
                    break
                await asyncio.sleep(0.02)
            await worker.stop()

        asyncio.run(run())

        assert provider.sent_count == 1
        assert worker.running is False
        assert worker.iterations >= 1

    def test_starting_twice_raises_rather_than_doubling_the_send_rate(
        self, provider
    ):
        """A second loop claiming from one queue is a doubled provider bill
        and a race `SKIP LOCKED` makes invisible."""
        worker = NotificationDeliveryWorker(provider, interval_seconds=60)

        async def run():
            await worker.start()
            try:
                with pytest.raises(RuntimeError):
                    await worker.start()
            finally:
                await worker.stop()

        asyncio.run(run())

    def test_stopping_a_worker_that_never_started_is_a_no_op(self, provider):
        asyncio.run(NotificationDeliveryWorker(provider).stop())

    def test_shutdown_does_not_wait_out_the_poll_interval(self, provider):
        """The sleep is an interruptible wait on the stop event, not
        `asyncio.sleep(interval)` — otherwise every deploy would hang for a
        full interval."""
        worker = NotificationDeliveryWorker(
            provider, interval_seconds=3600, session_factory=_raise_if_called
        )

        async def run():
            await worker.start()
            await asyncio.sleep(0.05)
            started = asyncio.get_event_loop().time()
            await worker.stop()
            return asyncio.get_event_loop().time() - started

        assert asyncio.run(run()) < 2.0

    def test_an_iteration_failure_does_not_stop_the_loop(self, provider):
        """Each lifecycle call is its own transaction, so an iteration that
        failed left the database consistent and every row it claimed is
        recoverable. The loop outlives it."""
        calls = {"count": 0}

        def exploding_session_factory():
            calls["count"] += 1
            raise RuntimeError("database is having a moment")

        worker = NotificationDeliveryWorker(
            provider, interval_seconds=1, session_factory=exploding_session_factory
        )

        async def run():
            await worker.start()
            await asyncio.sleep(0.05)
            running = worker.running
            await worker.stop()
            return running

        assert asyncio.run(run()) is True
        assert calls["count"] >= 1


class TestApplicationOwnership:
    """`start_worker`/`stop_worker` are what `app/main.py` calls, and they are
    what make "exactly one polling owner" a property of the process."""

    def test_it_does_not_start_when_email_is_disabled(self, monkeypatch):
        """The default, and what keeps the worker out of the test suite:
        `TestClient` runs the lifespan, so a worker gated on anything other
        than configuration would start under pytest."""
        monkeypatch.setattr(settings, "email_enabled", False)

        assert asyncio.run(start_worker()) is None

    def test_it_does_not_start_when_the_configuration_is_incomplete(
        self, monkeypatch
    ):
        monkeypatch.setattr(settings, "email_enabled", True)
        monkeypatch.setattr(settings, "email_sender", None)

        assert asyncio.run(start_worker()) is None

    def test_it_never_raises_on_a_bad_configuration(self, monkeypatch):
        """An email misconfiguration must not stop the API from booting —
        architectural principle §4.3/§4.7, applied to the process's own
        start-up."""
        monkeypatch.setattr(settings, "email_enabled", True)
        monkeypatch.setattr(settings, "email_sender", "not an address")
        monkeypatch.setattr(settings, "email_ses_region", None)
        monkeypatch.setattr(settings, "aws_region", None)

        asyncio.run(start_worker())  # must not raise
        asyncio.run(stop_worker())

    def test_it_starts_and_stops_with_a_complete_configuration(self, monkeypatch):
        monkeypatch.setattr(settings, "email_enabled", True)
        monkeypatch.setattr(settings, "email_provider", "fake")
        monkeypatch.setattr(settings, "email_sender", "notificaciones@celuma.test")
        monkeypatch.setattr(settings, "delivery_poll_interval_seconds", 3600)

        async def run():
            worker = await start_worker()
            assert worker is not None and worker.running
            await stop_worker()
            return worker

        assert asyncio.run(run()).running is False

    def test_the_disabled_path_logs_why(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "email_enabled", False)

        with caplog.at_level(logging.INFO):
            asyncio.run(start_worker())

        events = [getattr(record, "event", None) for record in caplog.records]
        assert "notification.delivery.worker_disabled" in events


# ---------------------------------------------------------------------------
# E9 — health
# ---------------------------------------------------------------------------


class TestProviderHealthProbe:
    def test_a_healthy_provider_is_logged_at_info(self, provider, caplog):
        with caplog.at_level(logging.INFO):
            health = log_provider_health(provider)

        assert health.healthy
        record = _find(caplog, "notification.delivery.provider_health")
        assert record.levelno == logging.INFO
        assert record.healthy is True

    def test_an_unhealthy_provider_is_logged_at_error(self, caplog):
        """So a wrong region, a missing IAM grant or an unverified identity is
        visible in the boot log rather than inferable from five failed
        deliveries an hour later."""
        provider = FakeEmailProvider(
            health_result=EmailProviderHealth(
                provider="fake",
                configured=True,
                credentials_present=False,
                reachable=False,
                error_code="provider_access_denied",
                detail="no grant",
            )
        )

        with caplog.at_level(logging.INFO):
            log_provider_health(provider)

        record = _find(caplog, "notification.delivery.provider_health")
        assert record.levelno == logging.ERROR
        assert record.error_code == "provider_access_denied"

    def test_the_probe_sends_nothing(self, provider):
        log_provider_health(provider)
        assert provider.sent_count == 0

    def test_a_provider_whose_health_raises_does_not_break_startup(self, caplog):
        class ExplodingProvider(FakeEmailProvider):
            def health(self):
                raise RuntimeError("catastrophe")

        with caplog.at_level(logging.INFO):
            assert log_provider_health(ExplodingProvider()) is None


# ---------------------------------------------------------------------------
# E8 — logging
# ---------------------------------------------------------------------------


class TestLogging:
    """Content policy §7. The allow-list is ids, the attempt, the provider,
    the provider message id, a sanitized code and an elapsed time."""

    def test_a_successful_send_logs_the_allowed_fields(
        self, session, world, provider, sender, caplog
    ):
        notification_id = notify(session, world, [world["user"]])

        with caplog.at_level(logging.INFO):
            process_delivery_batch(session, provider)

        record = _find(caplog, "notification.delivery.send_succeeded")
        assert record.notification_id == str(notification_id)
        assert record.provider == "fake"
        assert record.provider_message_id == "fake-message-1"
        assert record.attempts == 1
        assert isinstance(record.elapsed_ms, int)

    def test_a_failed_send_logs_the_sanitized_code_and_retryability(
        self, session, world, provider, sender, caplog
    ):
        provider.fail_addresses[USER_ADDRESS] = ERROR_SES_THROTTLED
        notify(session, world, [world["user"]])

        with caplog.at_level(logging.INFO):
            process_delivery_batch(session, provider)

        record = _find(caplog, "notification.delivery.send_failed")
        assert record.error_code == ERROR_SES_THROTTLED
        assert record.retryable is True

    def test_no_recipient_address_reaches_any_log_record(
        self, session, world, provider, sender, caplog
    ):
        """The assertion Block D established for its own log lines, extended
        across the send path — where an address is genuinely in scope and
        therefore genuinely at risk."""
        notify(session, world, [world["user"]])

        with caplog.at_level(logging.DEBUG):
            process_delivery_batch(session, provider)

        for record in caplog.records:
            rendered = record.getMessage() + str(record.__dict__)
            assert USER_ADDRESS not in rendered

    def test_no_subject_or_body_reaches_any_log_record(
        self, session, world, provider, sender, caplog
    ):
        """They are already in the database. Log aggregation has different
        retention and access controls than the primary store."""
        notify(session, world, [world["user"]])

        with caplog.at_level(logging.DEBUG):
            process_delivery_batch(session, provider)

        subject_fragment = "Reporte publicado (Orden"
        for record in caplog.records:
            rendered = record.getMessage() + str(record.__dict__)
            assert subject_fragment not in rendered
            assert "Inicia sesión en Céluma" not in rendered

    def test_no_template_parameter_reaches_any_log_record(
        self, session, world, provider, sender, caplog
    ):
        params = {"order_number": "ORD-SECRET-99", "actor_name": "Dra. Martínez"}
        notify(session, world, [world["user"]], params=params)

        with caplog.at_level(logging.DEBUG):
            process_delivery_batch(session, provider)

        for record in caplog.records:
            rendered = record.getMessage() + str(record.__dict__)
            assert "ORD-SECRET-99" not in rendered
            assert "Martínez" not in rendered

    def test_no_raw_provider_exception_reaches_any_log_record(
        self, session, world, provider, sender, caplog
    ):
        """A real SES rejection quotes the envelope. The worker logs the
        mapped code and nothing from the exception."""
        leaky = f"Email address is not verified: {USER_ADDRESS} — rejected"

        class LeakyProvider(FakeEmailProvider):
            def send(self, message):
                raise EmailProviderError(ERROR_MESSAGE_REJECTED)

        class VeryLeakyProvider(FakeEmailProvider):
            def send(self, message):
                raise RuntimeError(leaky)

        notify(session, world, [world["user"]])
        with caplog.at_level(logging.DEBUG):
            process_delivery_batch(session, LeakyProvider())

        notify(session, world, [world["peer"]])
        with caplog.at_level(logging.DEBUG):
            process_delivery_batch(session, VeryLeakyProvider())

        for record in caplog.records:
            rendered = record.getMessage() + str(record.__dict__)
            assert leaky not in rendered
            assert USER_ADDRESS not in rendered

    def test_the_batch_summary_is_logged(
        self, session, world, provider, sender, caplog
    ):
        notify(session, world, [world["user"]])

        with caplog.at_level(logging.INFO):
            process_delivery_batch(session, provider)

        record = _find(caplog, "notification.delivery.batch")
        assert record.claimed_count == 1
        assert record.sent_count == 1
        assert record.failed_count == 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _find(caplog, event):
    for record in caplog.records:
        if getattr(record, "event", None) == event:
            return record
    raise AssertionError(f"No log record with event={event!r}")


def _raise_if_called():  # pragma: no cover - the loop must not reach it
    raise AssertionError("The worker polled when it should have been sleeping")


class _NonClosingSession:
    """Hands the test's own session to the worker without closing it.

    The worker opens and closes a `Session` per iteration, which is right in
    production and wrong here: the test's session owns the ephemeral database
    and the rows under assertion. Delegating keeps the worker's real
    `with self._session_factory() as session` code path intact.
    """

    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *args):
        return False
