"""Delivery materialization and lifecycle tests (Céluma 1.3, Phase 3,
Block D — D15 and D16).

Nothing in this file sends anything, and nothing it exercises can: there is
no SES client, no SMTP client and no HTTP call anywhere in
`app/services/notification_delivery.py`. The state machine is driven
directly, which is the point — it must be provably correct before Block E
introduces a provider, not because of one.

Two halves, matching the module under test:

  `TestMaterialization…`  a notification produces the right PENDING rows,
                          for the right recipients, and nothing else.
  `TestClaim` / `TestSent` / `TestFailed` / `TestStaleRecovery`
                          the lifecycle a future worker will drive.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.core.config import settings
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationDeliveryStatus,
    NotificationPreference,
    NotificationRecipient,
    NotificationResourceType,
    NotificationType,
)
from app.schemas.notification import NotificationCommand
from app.services.notification import NotificationService
from app.services.notification_delivery import (
    NotificationDeliveryTransitionError,
    STALE_CLAIM_ERROR_CODE,
    claim_pending_deliveries,
    compute_backoff_seconds,
    compute_next_attempt_at,
    mark_delivery_failed,
    mark_delivery_sent,
    normalize_recipient_address,
    release_stale_deliveries,
    sanitize_delivery_error_code,
    select_due_delivery_ids,
)

from tests.http.factories import create_branch, create_tenant, create_user

SUPPORTED = NotificationType.REPORT_PUBLISHED
UNSUPPORTED = NotificationType.SAMPLE_STATUS_CHANGED

TEMPLATE_FOR = {
    SUPPORTED: ("report_published_v1", {"order_number": "ORD-1", "actor_name": "Dra. M"}),
    UNSUPPORTED: (
        "sample_status_changed_v1",
        {"order_number": "ORD-1", "sample_code": "MTR-1", "new_state": "Lista"},
    ),
}


@pytest.fixture(name="world")
def world_fixture(session):
    tenant = create_tenant(session, name="Tenant A")
    create_branch(session, tenant)
    user = create_user(session, tenant, email="user@tenant-a.test")
    peer = create_user(session, tenant, email="peer@tenant-a.test")

    other_tenant = create_tenant(session, name="Tenant B")
    create_branch(session, other_tenant)
    stranger = create_user(session, other_tenant, email="stranger@tenant-b.test")

    return {
        "tenant": tenant,
        "user": user,
        "peer": peer,
        "other_tenant": other_tenant,
        "stranger": stranger,
    }


def notify(
    session,
    world,
    recipients,
    *,
    notification_type=SUPPORTED,
    marker=None,
    resource_id=None,
    tenant=None,
    strict=True,
):
    """The idempotency key is `{type}:{resource_type}:{resource_id}:{marker}`,
    so a duplicate test must hold **both** `resource_id` and `marker` fixed —
    repeating only the marker produces a legitimately different occurrence."""
    template_key, params = TEMPLATE_FOR[notification_type]
    return NotificationService.notify(
        session,
        NotificationCommand(
            tenant_id=(tenant or world["tenant"]).id,
            type=notification_type,
            resource_type=NotificationResourceType.REPORT,
            resource_id=resource_id or uuid.uuid4(),
            occurrence_marker=marker or f"m-{uuid.uuid4()}",
            template_key=template_key,
            template_params=params,
            recipient_user_ids=[u.id for u in recipients],
        ),
        strict=strict,
    )


def deliveries(session, notification_id=None):
    query = select(NotificationDelivery)
    if notification_id is not None:
        query = query.where(NotificationDelivery.notification_id == notification_id)
    return session.exec(query).all()


def set_preference(session, user, notification_type, *, email):
    session.add(
        NotificationPreference(
            tenant_id=user.tenant_id,
            user_id=user.id,
            notification_type=notification_type.value,
            email_enabled=email,
        )
    )
    session.commit()


def seed_delivery(session, world, **overrides):
    """A delivery row in an arbitrary state, for lifecycle tests that must
    start somewhere the service's own contract would not let them reach."""
    notification = Notification(
        tenant_id=world["tenant"].id,
        type=SUPPORTED.value,
        severity="INFO",
        title="T",
        resource_type="report",
        resource_id=uuid.uuid4(),
        idempotency_key=f"seed:{uuid.uuid4()}",
    )
    session.add(notification)
    session.commit()

    values = {
        "notification_id": notification.id,
        "tenant_id": world["tenant"].id,
        "recipient_user_id": world["user"].id,
        "recipient_address": "user@tenant-a.test",
        "channel": NotificationChannel.EMAIL.value,
        "status": NotificationDeliveryStatus.PENDING.value,
        "attempts": 0,
        "next_attempt_at": datetime.utcnow(),
    }
    values.update(overrides)
    delivery = NotificationDelivery(**values)
    session.add(delivery)
    session.commit()
    session.refresh(delivery)
    return delivery


# ---------------------------------------------------------------------------
# D15 — materialization
# ---------------------------------------------------------------------------

class TestEligibleRecipient:
    def test_creates_one_pending_email_delivery(self, session, world):
        notification_id = notify(session, world, [world["user"]])
        session.commit()

        rows = deliveries(session, notification_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.channel == NotificationChannel.EMAIL
        assert row.status == NotificationDeliveryStatus.PENDING
        assert row.recipient_user_id == world["user"].id
        assert row.tenant_id == world["tenant"].id

    def test_the_recipient_row_is_created_too(self, session, world):
        notification_id = notify(session, world, [world["user"]])
        session.commit()

        recipients = session.exec(
            select(NotificationRecipient).where(
                NotificationRecipient.notification_id == notification_id
            )
        ).all()
        assert len(recipients) == 1

    def test_uses_the_normalized_account_email_as_the_snapshot(
        self, session, world
    ):
        world["user"].email = "  MiXeD.Case@Tenant-A.test "
        session.add(world["user"])
        session.commit()

        notification_id = notify(session, world, [world["user"]])
        session.commit()

        assert deliveries(session, notification_id)[0].recipient_address == (
            "mixed.case@tenant-a.test"
        )

    def test_the_address_snapshot_survives_a_later_email_change(
        self, session, world
    ):
        notification_id = notify(session, world, [world["user"]])
        session.commit()

        world["user"].email = "moved@elsewhere.test"
        session.add(world["user"])
        session.commit()
        session.expire_all()

        assert deliveries(session, notification_id)[0].recipient_address == (
            "user@tenant-a.test"
        )

    def test_a_fresh_row_carries_no_attempt_state(self, session, world):
        notification_id = notify(session, world, [world["user"]])
        session.commit()

        row = deliveries(session, notification_id)[0]
        assert row.attempts == 0
        assert row.last_attempt_at is None
        assert row.provider_message_id is None
        assert row.error_code is None

    def test_a_fresh_row_is_immediately_due(self, session, world):
        """`next_attempt_at` is set rather than left NULL so the claim
        predicate stays a plain `<= now`, which the (status, next_attempt_at)
        index serves directly."""
        notification_id = notify(session, world, [world["user"]])
        session.commit()

        row = deliveries(session, notification_id)[0]
        assert row.next_attempt_at is not None
        assert row.next_attempt_at == row.created_at


class TestIneligibleRecipient:
    def _assert_no_delivery_but_an_inbox_row(self, session, notification_id):
        assert deliveries(session, notification_id) == []
        recipients = session.exec(
            select(NotificationRecipient).where(
                NotificationRecipient.notification_id == notification_id
            )
        ).all()
        assert len(recipients) == 1

    def test_email_preference_disabled(self, session, world):
        set_preference(session, world["user"], SUPPORTED, email=False)

        notification_id = notify(session, world, [world["user"]])
        session.commit()

        self._assert_no_delivery_but_an_inbox_row(session, notification_id)

    def test_unsupported_event_type(self, session, world):
        notification_id = notify(
            session, world, [world["user"]], notification_type=UNSUPPORTED
        )
        session.commit()

        self._assert_no_delivery_but_an_inbox_row(session, notification_id)

    def test_a_stale_row_cannot_re_enable_an_unsupported_type(self, session, world):
        set_preference(session, world["user"], UNSUPPORTED, email=True)

        notification_id = notify(
            session, world, [world["user"]], notification_type=UNSUPPORTED
        )
        session.commit()

        assert deliveries(session, notification_id) == []

    def test_missing_email(self, session, world):
        world["user"].email = ""
        session.add(world["user"])
        session.commit()

        notification_id = notify(session, world, [world["user"]])
        session.commit()

        self._assert_no_delivery_but_an_inbox_row(session, notification_id)

    @pytest.mark.parametrize(
        "address", ["not-an-email", "no@domain", "two@@at.test", "spaces in@x.test"]
    )
    def test_invalid_email(self, session, world, address):
        world["user"].email = address
        session.add(world["user"])
        session.commit()

        notification_id = notify(session, world, [world["user"]])
        session.commit()

        self._assert_no_delivery_but_an_inbox_row(session, notification_id)

    def test_inactive_user(self, session, world):
        """They keep the inbox row — Block B's decision that a deactivated
        user still receives one stands, because it leaks nothing — but there
        is no point mailing an account that cannot be used."""
        world["user"].is_active = False
        session.add(world["user"])
        session.commit()

        notification_id = notify(session, world, [world["user"]])
        session.commit()

        self._assert_no_delivery_but_an_inbox_row(session, notification_id)

    def test_a_cross_tenant_recipient_is_rejected_before_any_write(
        self, session, world
    ):
        """The notification service refuses the command outright, so this
        never reaches materialization at all."""
        from app.services.notification import NotificationValidationError

        with pytest.raises(NotificationValidationError):
            notify(session, world, [world["stranger"]])
        session.rollback()

        assert deliveries(session) == []
        assert session.exec(select(Notification)).all() == []


class TestMultipleRecipients:
    def test_one_row_per_eligible_recipient(self, session, world):
        notification_id = notify(session, world, [world["user"], world["peer"]])
        session.commit()

        rows = deliveries(session, notification_id)
        assert len(rows) == 2
        assert {row.recipient_user_id for row in rows} == {
            world["user"].id,
            world["peer"].id,
        }

    def test_an_ineligible_recipient_does_not_block_the_others(
        self, session, world
    ):
        """Eligibility-based omission is per-recipient and is not a failure —
        unlike an internal error, which drops the whole batch."""
        set_preference(session, world["peer"], SUPPORTED, email=False)

        notification_id = notify(session, world, [world["user"], world["peer"]])
        session.commit()

        rows = deliveries(session, notification_id)
        assert len(rows) == 1
        assert rows[0].recipient_user_id == world["user"].id

        # The ineligible recipient still has their in-app notification.
        recipients = session.exec(
            select(NotificationRecipient).where(
                NotificationRecipient.notification_id == notification_id
            )
        ).all()
        assert len(recipients) == 2

    def test_a_repeated_recipient_id_produces_one_row(self, session, world):
        notification_id = notify(
            session, world, [world["user"], world["user"], world["user"]]
        )
        session.commit()

        assert len(deliveries(session, notification_id)) == 1

    def test_two_users_sharing_a_mailbox_each_get_a_row(self, session, world):
        """The case v1_5_0 exists for. Under v1_4_0's address-keyed unique
        constraint the second user's row was swallowed by ON CONFLICT DO
        NOTHING, so whoever shared an inbox with a colleague silently never
        received email."""
        world["peer"].email = world["user"].email
        session.add(world["peer"])
        session.commit()

        notification_id = notify(session, world, [world["user"], world["peer"]])
        session.commit()

        rows = deliveries(session, notification_id)
        assert len(rows) == 2
        assert {row.recipient_address for row in rows} == {"user@tenant-a.test"}
        assert {row.recipient_user_id for row in rows} == {
            world["user"].id,
            world["peer"].id,
        }


class TestDuplicateNotification:
    def test_a_duplicate_creates_no_second_delivery(self, session, world):
        occurrence = {"marker": "same-occurrence", "resource_id": uuid.uuid4()}
        first = notify(session, world, [world["user"]], **occurrence)
        session.commit()
        before = deliveries(session, first)[0]
        before_updated = before.updated_at

        second = notify(session, world, [world["user"]], **occurrence)
        session.commit()

        assert second == first
        rows = deliveries(session, first)
        assert len(rows) == 1
        assert rows[0].id == before.id
        assert rows[0].updated_at == before_updated
        assert rows[0].status == NotificationDeliveryStatus.PENDING

    def test_a_duplicate_does_not_revive_a_resolved_delivery(
        self, session, world
    ):
        """A second `notify()` must not reset a delivery the worker already
        finished — the duplicate path touches nothing."""
        occurrence = {"marker": "same-occurrence", "resource_id": uuid.uuid4()}
        notification_id = notify(session, world, [world["user"]], **occurrence)
        session.commit()
        delivery_id = deliveries(session, notification_id)[0].id

        claim_pending_deliveries(session)
        mark_delivery_sent(session, delivery_id, provider_message_id="msg-1")

        second = notify(session, world, [world["user"]], **occurrence)
        session.commit()
        assert second == notification_id
        session.expire_all()

        rows = deliveries(session, notification_id)
        assert len(rows) == 1
        assert rows[0].status == NotificationDeliveryStatus.SENT
        assert rows[0].provider_message_id == "msg-1"


class TestFailureContainment:
    """The load-bearing property of the transaction boundary: notification
    persistence outranks optional email delivery."""

    def test_a_materialization_failure_leaves_the_notification_intact(
        self, session, world, monkeypatch
    ):
        monkeypatch.setattr(
            "app.services.notification.materialize_email_deliveries",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("simulated delivery materialization failure")
            ),
        )

        notification_id = notify(session, world, [world["user"], world["peer"]])
        session.commit()

        assert notification_id is not None
        assert session.get(Notification, notification_id) is not None
        recipients = session.exec(
            select(NotificationRecipient).where(
                NotificationRecipient.notification_id == notification_id
            )
        ).all()
        assert len(recipients) == 2
        # No partial delivery batch remains.
        assert deliveries(session) == []

    def test_the_callers_transaction_stays_usable(
        self, session, world, monkeypatch
    ):
        """The caller is a clinical transition. A delivery problem must not
        cost it its own writes."""
        monkeypatch.setattr(
            "app.services.notification.materialize_email_deliveries",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        notify(session, world, [world["user"]])
        # An unrelated write after the contained failure, in the same
        # transaction, must still commit.
        create_user(session, world["tenant"], email="after@tenant-a.test")
        session.commit()

        from app.models.user import AppUser

        assert session.exec(
            select(AppUser).where(AppUser.email == "after@tenant-a.test")
        ).first() is not None

    def test_the_contained_failure_logs_no_address_or_content(
        self, session, world, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            "app.services.notification.materialize_email_deliveries",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("could not insert user@tenant-a.test / Reporte publicado")
            ),
        )

        with caplog.at_level("ERROR"):
            notify(session, world, [world["user"]])
        session.commit()

        records = [
            r
            for r in caplog.records
            if getattr(r, "event", None)
            == "notification.delivery.materialize_failed"
        ]
        assert records
        blob = "\n".join(r.getMessage() + str(r.__dict__) for r in records)
        assert "user@tenant-a.test" not in blob
        assert "Reporte publicado" not in blob
        assert records[0].error_code == "RuntimeError"

    def test_a_notification_failure_leaves_no_delivery_either(
        self, session, world, monkeypatch
    ):
        """Outcome A: the outer savepoint unwinds and nothing survives."""
        monkeypatch.setattr(
            "app.services.notification.create_recipient_rows",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        result = NotificationService.notify(
            session,
            NotificationCommand(
                tenant_id=world["tenant"].id,
                type=SUPPORTED,
                resource_type=NotificationResourceType.REPORT,
                resource_id=uuid.uuid4(),
                occurrence_marker="outcome-a",
                template_key="report_published_v1",
                template_params={"order_number": "ORD-1", "actor_name": "Dra. M"},
                recipient_user_ids=[world["user"].id],
            ),
        )
        session.commit()

        assert result is None
        assert session.exec(select(Notification)).all() == []
        assert deliveries(session) == []


# ---------------------------------------------------------------------------
# D16 — lifecycle
# ---------------------------------------------------------------------------

class TestBackoff:
    def test_the_schedule_is_deterministic_and_capped(self):
        base = settings.notification_delivery_base_backoff_seconds
        ceiling = settings.notification_delivery_max_backoff_seconds

        assert compute_backoff_seconds(1) == base
        assert compute_backoff_seconds(2) == base * 2
        assert compute_backoff_seconds(3) == base * 4
        assert compute_backoff_seconds(50) == ceiling
        # Below one attempt is clamped rather than fractional.
        assert compute_backoff_seconds(0) == min(base, ceiling)

    def test_there_is_no_jitter(self):
        assert len({compute_backoff_seconds(3) for _ in range(20)}) == 1

    def test_the_maximum_attempt_count_is_terminal(self):
        now = datetime(2026, 8, 6, 12, 0, 0)
        last = settings.notification_delivery_max_attempts - 1

        assert compute_next_attempt_at(last, now) is not None
        assert (
            compute_next_attempt_at(settings.notification_delivery_max_attempts, now)
            is None
        )


class TestErrorCodeSanitization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("SES_THROTTLED", "ses_throttled"),
            ("  Message rejected  ", "message_rejected"),
            (None, "delivery_failed"),
            ("", "delivery_failed"),
            ("!!!", "delivery_failed"),
        ],
    )
    def test_codes_are_reduced_to_identifiers(self, raw, expected):
        assert sanitize_delivery_error_code(raw) == expected

    def test_a_provider_message_carrying_an_address_is_refused_outright(self):
        """A real SES error can quote the envelope it choked on.

        Merely stripping the `@` is not enough — `user_tenant-a.test` is still
        plainly an address — so an address-shaped input is replaced entirely
        rather than normalized.
        """
        code = sanitize_delivery_error_code(
            "554 Message rejected: recipient user@tenant-a.test is suppressed"
        )
        assert code == "delivery_failed"
        assert "tenant-a" not in code
        assert "user" not in code

    @pytest.mark.parametrize(
        "raw",
        [
            "user@lab.test",
            "bounce for a.b+tag@sub.lab.test",
            "SMTP 550 <recipient@x.test> unknown",
        ],
    )
    def test_anything_address_shaped_becomes_the_generic_code(self, raw):
        assert sanitize_delivery_error_code(raw) == "delivery_failed"

    def test_long_prose_is_truncated(self):
        assert len(sanitize_delivery_error_code("x" * 500)) == 64


class TestAddressNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("  User@Example.TEST ", "user@example.test"),
            ("a.b+tag@sub.example.test", "a.b+tag@sub.example.test"),
        ],
    )
    def test_valid_addresses_are_lowercased_and_trimmed(self, raw, expected):
        assert normalize_recipient_address(raw) == expected

    @pytest.mark.parametrize(
        "raw", [None, "", "   ", "nodomain", "no@tld", "a@b@c.test", "x" * 400]
    )
    def test_unusable_addresses_resolve_to_none(self, raw):
        assert normalize_recipient_address(raw) is None


class TestClaim:
    def test_claims_a_due_pending_row(self, session, world):
        delivery = seed_delivery(session, world)

        claimed = claim_pending_deliveries(session)

        assert [c.id for c in claimed] == [delivery.id]
        session.expire_all()
        row = session.get(NotificationDelivery, delivery.id)
        assert row.status == NotificationDeliveryStatus.SENDING
        assert row.attempts == 1
        assert row.last_attempt_at is not None
        # Cleared while claimed: a SENDING row must never satisfy the due
        # predicate.
        assert row.next_attempt_at is None

    def test_does_not_claim_a_row_that_is_not_yet_due(self, session, world):
        seed_delivery(
            session, world, next_attempt_at=datetime.utcnow() + timedelta(hours=1)
        )

        assert claim_pending_deliveries(session) == []

    def test_does_not_claim_a_sent_row(self, session, world):
        seed_delivery(
            session,
            world,
            status=NotificationDeliveryStatus.SENT.value,
            next_attempt_at=None,
        )

        assert claim_pending_deliveries(session) == []

    def test_does_not_claim_a_sending_row(self, session, world):
        seed_delivery(
            session,
            world,
            status=NotificationDeliveryStatus.SENDING.value,
            next_attempt_at=None,
        )

        assert claim_pending_deliveries(session) == []

    def test_claims_a_due_retryable_failed_row(self, session, world):
        delivery = seed_delivery(
            session,
            world,
            status=NotificationDeliveryStatus.FAILED.value,
            attempts=1,
            next_attempt_at=datetime.utcnow() - timedelta(seconds=1),
        )

        claimed = claim_pending_deliveries(session)

        assert [c.id for c in claimed] == [delivery.id]
        assert claimed[0].attempts == 2

    def test_does_not_claim_a_terminal_failed_row(self, session, world):
        """`next_attempt_at IS NULL` on a FAILED row is the dead letter."""
        seed_delivery(
            session,
            world,
            status=NotificationDeliveryStatus.FAILED.value,
            attempts=settings.notification_delivery_max_attempts,
            next_attempt_at=None,
        )

        assert claim_pending_deliveries(session) == []

    def test_does_not_claim_a_row_at_the_attempt_ceiling(self, session, world):
        """Belt and braces: even a due `next_attempt_at` cannot resurrect a
        row that has exhausted its attempts."""
        seed_delivery(
            session,
            world,
            status=NotificationDeliveryStatus.FAILED.value,
            attempts=settings.notification_delivery_max_attempts,
            next_attempt_at=datetime.utcnow() - timedelta(seconds=1),
        )

        assert claim_pending_deliveries(session) == []

    def test_respects_the_batch_size(self, session, world):
        for _ in range(5):
            seed_delivery(session, world)

        assert len(claim_pending_deliveries(session, limit=2)) == 2

    def test_orders_oldest_due_first(self, session, world):
        base = datetime.utcnow() - timedelta(hours=3)
        oldest = seed_delivery(session, world, next_attempt_at=base)
        middle = seed_delivery(
            session, world, next_attempt_at=base + timedelta(minutes=10)
        )
        seed_delivery(session, world, next_attempt_at=base + timedelta(minutes=20))

        claimed_ids = select_due_delivery_ids(session, limit=2)
        session.rollback()

        assert claimed_ids == [oldest.id, middle.id]

    def test_is_tenant_agnostic(self, session, world):
        """A worker processes the queue, not a tenant."""
        seed_delivery(session, world)
        seed_delivery(session, world, tenant_id=world["other_tenant"].id)

        assert len(claim_pending_deliveries(session)) == 2

    def test_two_sessions_never_claim_the_same_row(self, session, world):
        """`FOR UPDATE SKIP LOCKED`, validated against real PostgreSQL rather
        than assumed. This is why the due-selection step is a separate
        function: the committing wrapper releases its locks immediately, so
        the disjointness it guarantees would not be observable through it."""
        first = seed_delivery(session, world)
        second = seed_delivery(session, world)

        engine = session.get_bind()
        with Session(engine) as session_a, Session(engine) as session_b:
            claimed_a = select_due_delivery_ids(session_a, limit=1)
            claimed_b = select_due_delivery_ids(session_b, limit=1)

            assert len(claimed_a) == 1
            assert len(claimed_b) == 1
            assert set(claimed_a).isdisjoint(claimed_b)
            assert set(claimed_a) | set(claimed_b) == {first.id, second.id}

            session_a.rollback()
            session_b.rollback()

    def test_an_empty_queue_claims_nothing(self, session, world):
        assert claim_pending_deliveries(session) == []


class TestSent:
    def test_sending_becomes_sent(self, session, world):
        delivery = seed_delivery(session, world)
        claim_pending_deliveries(session)

        result = mark_delivery_sent(
            session, delivery.id, provider_message_id="ses-123"
        )

        assert result.status == NotificationDeliveryStatus.SENT
        assert result.provider_message_id == "ses-123"
        assert result.next_attempt_at is None
        assert result.error_code is None

    def test_pending_cannot_become_sent(self, session, world):
        delivery = seed_delivery(session, world)

        with pytest.raises(NotificationDeliveryTransitionError):
            mark_delivery_sent(session, delivery.id)

    def test_failed_cannot_become_sent_without_a_claim(self, session, world):
        delivery = seed_delivery(
            session,
            world,
            status=NotificationDeliveryStatus.FAILED.value,
            attempts=1,
        )

        with pytest.raises(NotificationDeliveryTransitionError):
            mark_delivery_sent(session, delivery.id)

    def test_sent_is_terminal(self, session, world):
        delivery = seed_delivery(session, world)
        claim_pending_deliveries(session)
        mark_delivery_sent(session, delivery.id)

        with pytest.raises(NotificationDeliveryTransitionError):
            mark_delivery_sent(session, delivery.id)
        with pytest.raises(NotificationDeliveryTransitionError):
            mark_delivery_failed(session, delivery.id, error_code="late")


class TestFailed:
    def test_sending_becomes_failed_with_a_backoff(self, session, world):
        delivery = seed_delivery(session, world)
        claim_pending_deliveries(session)
        now = datetime(2026, 8, 6, 12, 0, 0)

        result = mark_delivery_failed(
            session, delivery.id, error_code="SES_THROTTLED", now=now
        )

        assert result.status == NotificationDeliveryStatus.FAILED
        assert result.error_code == "ses_throttled"
        assert result.next_attempt_at == now + timedelta(
            seconds=settings.notification_delivery_base_backoff_seconds
        )

    def test_the_backoff_grows_with_each_attempt(self, session, world):
        delivery = seed_delivery(session, world, attempts=2)
        claim_pending_deliveries(session)  # attempts -> 3
        now = datetime(2026, 8, 6, 12, 0, 0)

        result = mark_delivery_failed(session, delivery.id, error_code="x", now=now)

        assert result.next_attempt_at == now + timedelta(
            seconds=compute_backoff_seconds(3)
        )

    def test_the_last_permitted_attempt_is_terminal(self, session, world):
        delivery = seed_delivery(
            session,
            world,
            attempts=settings.notification_delivery_max_attempts - 1,
        )
        claim_pending_deliveries(session)  # attempts -> max

        result = mark_delivery_failed(session, delivery.id, error_code="x")

        assert result.status == NotificationDeliveryStatus.FAILED
        assert result.next_attempt_at is None
        # And it is not claimable again.
        assert claim_pending_deliveries(session) == []

    def test_no_raw_provider_exception_is_persisted(self, session, world):
        delivery = seed_delivery(session, world)
        claim_pending_deliveries(session)

        result = mark_delivery_failed(
            session,
            delivery.id,
            error_code="554 Message rejected: user@tenant-a.test is suppressed",
        )

        # Refused wholesale, not merely stripped of its "@".
        assert result.error_code == "delivery_failed"
        assert "tenant-a" not in result.error_code

    def test_no_address_reaches_the_log(self, session, world, caplog):
        delivery = seed_delivery(session, world)
        claim_pending_deliveries(session)

        with caplog.at_level("WARNING"):
            mark_delivery_failed(
                session, delivery.id, error_code="rejected user@tenant-a.test"
            )

        records = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "notification.delivery.failed"
        ]
        assert records
        blob = "\n".join(r.getMessage() + str(r.__dict__) for r in records)
        assert "user@tenant-a.test" not in blob

    def test_pending_cannot_become_failed(self, session, world):
        delivery = seed_delivery(session, world)

        with pytest.raises(NotificationDeliveryTransitionError):
            mark_delivery_failed(session, delivery.id, error_code="x")

    def test_a_failed_row_becomes_claimable_again_when_it_is_due(
        self, session, world
    ):
        delivery = seed_delivery(session, world)
        claim_pending_deliveries(session)
        mark_delivery_failed(session, delivery.id, error_code="x")

        assert claim_pending_deliveries(session) == []

        # Make it due.
        session.expire_all()
        row = session.get(NotificationDelivery, delivery.id)
        row.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
        session.add(row)
        session.commit()

        claimed = claim_pending_deliveries(session)
        assert [c.id for c in claimed] == [delivery.id]
        assert claimed[0].attempts == 2


class TestStaleRecovery:
    """Céluma 1.3 Phase 3, Block E, Story E7 rewrote what recovery *does*.

    Block D moved a stale `SENDING` row to `FAILED` with a backed-off
    `next_attempt_at`, so the ordinary claim picked it up again. That was
    correct while no provider existed — nothing could have been accepted, so no
    retry could duplicate anything. With a real `provider.send()` between the
    claim and the resolution the window is genuinely ambiguous, and a retry can
    send a physician a second copy of "report published".

    Block E made recovery **terminal**, restoring Block A's idempotency
    strategy §5. The three tests below that asserted the retry are inverted
    rather than deleted, each naming the supersession — the same treatment
    Block D gave the two Block B assertions it superseded.
    """

    def _stale(self, session, world, **overrides):
        age = settings.notification_delivery_stale_sending_seconds
        values = {
            "status": NotificationDeliveryStatus.SENDING.value,
            "attempts": 1,
            "last_attempt_at": datetime.utcnow() - timedelta(seconds=age + 60),
            "next_attempt_at": None,
        }
        values.update(overrides)
        return seed_delivery(session, world, **values)

    def test_a_stale_claim_becomes_a_terminal_failure(self, session, world):
        """Supersedes Block D's `test_a_stale_claim_becomes_a_retryable_failure`
        (Story E7). `next_attempt_at IS NULL` is the terminal marker the claim
        predicate excludes — the row is the dead letter."""
        delivery = self._stale(session, world)

        assert release_stale_deliveries(session) == 1

        session.expire_all()
        row = session.get(NotificationDelivery, delivery.id)
        assert row.status == NotificationDeliveryStatus.FAILED
        assert row.error_code == STALE_CLAIM_ERROR_CODE
        assert row.next_attempt_at is None

    def test_a_stale_claim_is_terminal_even_with_attempts_remaining(
        self, session, world
    ):
        """The point of Story E7, stated as its own case: terminality here is
        *not* the attempt ceiling doing its job. A row with four of five
        attempts left still stops, because what makes it unsafe is the
        ambiguity of the window, not exhaustion."""
        delivery = self._stale(session, world, attempts=1)
        assert settings.notification_delivery_max_attempts > 2

        release_stale_deliveries(session)

        session.expire_all()
        row = session.get(NotificationDelivery, delivery.id)
        assert row.attempts == 1
        assert row.next_attempt_at is None

    def test_a_fresh_claim_is_untouched(self, session, world):
        delivery = self._stale(session, world, last_attempt_at=datetime.utcnow())

        assert release_stale_deliveries(session) == 0

        session.expire_all()
        assert (
            session.get(NotificationDelivery, delivery.id).status
            == NotificationDeliveryStatus.SENDING
        )

    def test_recovery_is_idempotent(self, session, world):
        self._stale(session, world)

        assert release_stale_deliveries(session) == 1
        assert release_stale_deliveries(session) == 0
        assert release_stale_deliveries(session) == 0

    def test_an_exhausted_row_is_recovered_as_terminal(self, session, world):
        delivery = self._stale(
            session, world, attempts=settings.notification_delivery_max_attempts
        )

        release_stale_deliveries(session)

        session.expire_all()
        row = session.get(NotificationDelivery, delivery.id)
        assert row.status == NotificationDeliveryStatus.FAILED
        assert row.next_attempt_at is None

    def test_a_recovered_row_is_never_claimed_again(self, session, world):
        """Inverts Block D's `test_a_recovered_row_is_claimable_once_due`
        (Story E7). This is the assertion that actually prevents the double
        send: the provider may already have accepted this message, so nothing
        automatic may attempt it a second time."""
        self._stale(session, world)
        release_stale_deliveries(session)

        assert claim_pending_deliveries(session) == []

    def test_an_operator_can_still_requeue_a_recovered_row_by_hand(
        self, session, world
    ):
        """Terminal means "nothing claims it automatically", not "the row is
        frozen". Writing a due `next_attempt_at` — direct database access,
        which is the only delivery affordance Céluma 1.3 has — brings it back,
        so the decision to risk a duplicate stays available to a human who has
        checked whether the first one arrived."""
        delivery = self._stale(session, world)
        release_stale_deliveries(session)

        session.expire_all()
        row = session.get(NotificationDelivery, delivery.id)
        row.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
        session.add(row)
        session.commit()

        claimed = claim_pending_deliveries(session)
        assert [c.id for c in claimed] == [delivery.id]

    def test_recovery_is_tenant_agnostic(self, session, world):
        self._stale(session, world)
        self._stale(session, world, tenant_id=world["other_tenant"].id)

        assert release_stale_deliveries(session) == 2

    def test_recovery_touches_no_other_status(self, session, world):
        self._stale(session, world)
        pending = seed_delivery(session, world)
        sent = seed_delivery(
            session,
            world,
            status=NotificationDeliveryStatus.SENT.value,
            next_attempt_at=None,
        )

        release_stale_deliveries(session)

        session.expire_all()
        assert (
            session.get(NotificationDelivery, pending.id).status
            == NotificationDeliveryStatus.PENDING
        )
        assert (
            session.get(NotificationDelivery, sent.id).status
            == NotificationDeliveryStatus.SENT
        )


class TestTheLifecycleStillOwnsNoProvider:
    """Renamed from Block D's `TestNoProviderExists` (Story E6/E7).

    A provider exists now, so the class cannot keep asserting that nothing
    sends. What it asserts instead is the boundary that survived: **this
    module still contains no provider client**, and the poller lives somewhere
    else. `block-e-dependencies.md` §14 anticipated exactly this — "if
    `test_no_poller_is_started` starts failing, that is Block E landing; the
    test's assertion should move, not be deleted."
    """

    def test_the_delivery_module_contains_no_provider_client(self):
        """Unchanged from Block D, and it must stay that way.

        The lifecycle service owns the `notification_delivery` table and
        nothing else. Block E put the SES client in
        `app/services/email_provider_ses.py`, behind an interface, precisely so
        that this assertion keeps holding — a contributor who adds a send path
        to the state machine fails CI."""
        import inspect as py_inspect

        import app.services.notification_delivery as module

        source = py_inspect.getsource(module)
        for forbidden in ("boto3", "smtplib", "send_email", "requests.", "httpx"):
            assert forbidden not in source, forbidden

    def test_the_poller_is_started_from_lifespan(self):
        """Supersedes `test_no_poller_is_started`.

        Block A's delivery strategy §3 chose an in-process asyncio task in
        FastAPI's `lifespan` over the three alternatives. Asserting the shape
        structurally is what stops a later contributor "simplifying" it into a
        `BackgroundTasks` callback (lost on restart, no retry, no record) or
        adding a queue nobody decided to operate."""
        import inspect as py_inspect

        import app.main as module

        source = py_inspect.getsource(module)
        assert "start_worker" in source
        assert "stop_worker" in source
        assert "lifespan" in source
        for forbidden in ("BackgroundTasks", "celery", "Celery", "APScheduler"):
            assert forbidden not in source, forbidden

    def test_main_cannot_drive_the_queue_itself(self):
        """The claim primitive stays the worker's. `app/main.py` knows two
        functions — start and stop — and has no way to claim, resolve or
        release a delivery, so an HTTP handler added to that file could not
        reach into the queue even by accident."""
        import inspect as py_inspect

        import app.main as module

        source = py_inspect.getsource(module)
        for forbidden in (
            "claim_pending_deliveries",
            "mark_delivery_sent",
            "mark_delivery_failed",
            "release_stale_deliveries",
        ):
            assert forbidden not in source, forbidden
