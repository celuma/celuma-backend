"""Notification model and constraint tests (Céluma 1.3, Phase 3, Block B).

These run against the real migrated PostgreSQL schema (the `session` fixture
in conftest.py builds it with `alembic upgrade head`), not SQLModel metadata,
because what is under test *is* the database constraints — a unique index or
a CHECK that only exists in the model definition guarantees nothing.

Pure field-default assertions that need no database live in
tests/test_models.py alongside the other model unit tests.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import DataError, IntegrityError
from sqlmodel import select

from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationDeliveryStatus,
    NotificationPreference,
    NotificationRecipient,
    NotificationRecipientStatus,
    NotificationSeverity,
    NotificationType,
)
from tests.http.factories import (
    create_branch,
    create_notification,
    create_recipient,
    create_tenant,
    create_user,
)


@pytest.fixture(name="world")
def world_fixture(session):
    tenant = create_tenant(session, name="Tenant A")
    create_branch(session, tenant)
    user = create_user(session, tenant, email="a@tenant-a.test")
    other_user = create_user(session, tenant, email="b@tenant-a.test")

    other_tenant = create_tenant(session, name="Tenant B")
    create_branch(session, other_tenant)
    foreign_user = create_user(session, other_tenant, email="c@tenant-b.test")

    return {
        "tenant": tenant,
        "user": user,
        "other_user": other_user,
        "other_tenant": other_tenant,
        "foreign_user": foreign_user,
    }


class TestNotification:
    def test_persists_required_fields(self, session, world):
        notification = create_notification(
            session,
            world["tenant"],
            notification_type=NotificationType.REPORT_PUBLISHED.value,
            created_by=world["user"].id,
        )
        stored = session.get(Notification, notification.id)

        assert stored.tenant_id == world["tenant"].id
        assert stored.type == NotificationType.REPORT_PUBLISHED
        assert stored.title
        assert stored.resource_type == "report"
        assert stored.resource_id is not None
        assert stored.idempotency_key
        assert stored.created_at is not None
        assert stored.created_by == world["user"].id

    def test_severity_defaults_to_info(self, session, world):
        notification = Notification(
            tenant_id=world["tenant"].id,
            type=NotificationType.REPORT_SUBMITTED,
            title="Reporte listo para revisión — Orden ORD-1",
            resource_type="report",
            resource_id=uuid.uuid4(),
            idempotency_key=f"sev:{uuid.uuid4()}",
        )
        session.add(notification)
        session.commit()
        session.refresh(notification)

        assert notification.severity == NotificationSeverity.INFO

    def test_body_is_optional(self, session, world):
        notification = create_notification(session, world["tenant"], body=None)
        assert session.get(Notification, notification.id).body is None

    def test_created_by_is_optional_for_system_events(self, session, world):
        """REPORT_PDF_READY has no acting user — generation is system work."""
        notification = create_notification(
            session,
            world["tenant"],
            notification_type=NotificationType.REPORT_PDF_READY.value,
            created_by=None,
        )
        assert session.get(Notification, notification.id).created_by is None

    def test_title_longer_than_255_is_rejected(self, session, world):
        with pytest.raises((DataError, IntegrityError)):
            create_notification(session, world["tenant"], title="x" * 256)
        session.rollback()

    def test_body_longer_than_1000_is_rejected(self, session, world):
        with pytest.raises((DataError, IntegrityError)):
            create_notification(session, world["tenant"], body="x" * 1001)
        session.rollback()

    def test_unknown_type_is_rejected_by_the_check_constraint(self, session, world):
        with pytest.raises(IntegrityError):
            create_notification(session, world["tenant"], notification_type="NOT_A_TYPE")
        session.rollback()

    def test_idempotency_key_is_unique_within_a_tenant(self, session, world):
        create_notification(session, world["tenant"], idempotency_key="dup-key")

        with pytest.raises(IntegrityError):
            create_notification(session, world["tenant"], idempotency_key="dup-key")
        session.rollback()

    def test_same_idempotency_key_is_allowed_in_a_different_tenant(
        self, session, world
    ):
        """The constraint is scoped by tenant on purpose: two tenants running
        the same workflow legitimately produce the same key for their own
        separate occurrences."""
        create_notification(session, world["tenant"], idempotency_key="shared-key")
        create_notification(session, world["other_tenant"], idempotency_key="shared-key")

        rows = session.exec(
            select(Notification).where(Notification.idempotency_key == "shared-key")
        ).all()
        assert len(rows) == 2
        assert {r.tenant_id for r in rows} == {
            world["tenant"].id,
            world["other_tenant"].id,
        }

    def test_no_generic_update_path_is_exposed_by_the_api(self):
        """Immutability is an application contract, not a DB trigger. The
        enforceable half is that no route can update a notification's frozen
        content — assert that, rather than asserting a rule the database does
        not carry."""
        from app.api.v1.notifications import router

        # No PUT/PATCH/DELETE anywhere, and the only two write routes act on
        # a NotificationRecipient's read state — never on the shared row.
        write_paths = set()
        for route in router.routes:
            assert set(route.methods) <= {"GET", "POST"}, route.path
            if "POST" in route.methods:
                write_paths.add(route.path)

        assert write_paths == {
            "/notifications/read-all",
            "/notifications/{recipient_id}/read",
        }


class TestNotificationRecipient:
    def test_status_defaults_to_unread_with_no_read_timestamp(self, session, world):
        notification = create_notification(session, world["tenant"])
        recipient = create_recipient(session, notification, world["user"])

        assert recipient.status == NotificationRecipientStatus.UNREAD
        assert recipient.read_at is None

    def test_tenant_and_user_are_both_populated(self, session, world):
        notification = create_notification(session, world["tenant"])
        recipient = create_recipient(session, notification, world["user"])

        assert recipient.tenant_id == world["tenant"].id
        assert recipient.user_id == world["user"].id

    def test_created_at_is_copied_from_the_parent_notification(self, session, world):
        """The denormalized column only lets the inbox query skip the join if
        the two values never diverge."""
        notification = create_notification(session, world["tenant"])
        recipient = create_recipient(session, notification, world["user"])

        assert recipient.created_at == notification.created_at

    def test_a_user_cannot_be_a_recipient_of_the_same_notification_twice(
        self, session, world
    ):
        notification = create_notification(session, world["tenant"])
        create_recipient(session, notification, world["user"])

        with pytest.raises(IntegrityError):
            create_recipient(session, notification, world["user"])
        session.rollback()

    def test_the_same_user_may_receive_different_notifications(self, session, world):
        first = create_notification(session, world["tenant"])
        second = create_notification(session, world["tenant"])
        create_recipient(session, first, world["user"])
        create_recipient(session, second, world["user"])

        rows = session.exec(
            select(NotificationRecipient).where(
                NotificationRecipient.user_id == world["user"].id
            )
        ).all()
        assert len(rows) == 2

    def test_read_status_requires_a_read_timestamp(self, session, world):
        """A row that claims to be read must say when — enforced by
        ck_notification_recipient_read_requires_timestamp."""
        notification = create_notification(session, world["tenant"])

        with pytest.raises(IntegrityError):
            create_recipient(
                session, notification, world["user"], status="READ", read_at=None
            )
        session.rollback()

    def test_read_at_is_settable_together_with_read_status(self, session, world):
        notification = create_notification(session, world["tenant"])
        read_at = datetime.utcnow()
        recipient = create_recipient(
            session, notification, world["user"], status="READ", read_at=read_at
        )

        assert recipient.status == NotificationRecipientStatus.READ
        assert recipient.read_at is not None

    def test_there_is_no_delivered_at_column(self, session, world):
        """Block A proposed `delivered_at`; Block B deliberately did not add
        it. For an in-app notification "delivered" is "the row exists", which
        created_at already records — a second column carrying the same fact is
        state that can only drift. See the Block B architecture decision."""
        assert not hasattr(NotificationRecipient, "delivered_at")

        from sqlalchemy import inspect as sa_inspect

        columns = {c.name for c in sa_inspect(NotificationRecipient).columns}
        assert "delivered_at" not in columns
        assert {"created_at", "status", "read_at"} <= columns


class TestNotificationDelivery:
    def _delivery(self, session, world, **overrides):
        notification = overrides.pop(
            "notification", create_notification(session, world["tenant"])
        )
        values = {
            "notification_id": notification.id,
            "tenant_id": world["tenant"].id,
            "recipient_user_id": world["user"].id,
            "recipient_address": "destinatario@example.test",
            "channel": NotificationChannel.EMAIL.value,
        }
        values.update(overrides)
        delivery = NotificationDelivery(**values)
        session.add(delivery)
        session.commit()
        session.refresh(delivery)
        return delivery

    def test_status_defaults_to_pending_and_attempts_to_zero(self, session, world):
        delivery = self._delivery(session, world)

        assert delivery.status == NotificationDeliveryStatus.PENDING
        assert delivery.attempts == 0
        assert delivery.last_attempt_at is None
        assert delivery.next_attempt_at is None

    def test_email_delivery_requires_a_recipient_address(self, session, world):
        """NOT NULL rather than a conditional CHECK: EMAIL is the only channel
        in Phase 3, so the simplest model that matches the real scope is the
        right one — and it is what makes the uniqueness guarantee below real,
        since NULLs compare distinct in PostgreSQL."""
        with pytest.raises(IntegrityError):
            self._delivery(session, world, recipient_address=None)
        session.rollback()

    def test_delivery_is_unique_per_notification_channel_and_recipient(
        self, session, world
    ):
        """Céluma 1.3 Phase 3, Block D (v1_5_0) moved this guarantee from the
        address to the recipient: one delivery per (event, channel, user).
        The duplicate defence is still the database's, it just keys on the
        right thing."""
        notification = create_notification(session, world["tenant"])
        self._delivery(session, world, notification=notification)

        with pytest.raises(IntegrityError):
            self._delivery(session, world, notification=notification)
        session.rollback()

    def test_two_users_sharing_a_mailbox_each_get_their_own_delivery(
        self, session, world
    ):
        """The concrete case v1_5_0 exists for. Under v1_4_0's
        UNIQUE(notification_id, channel, recipient_address) the second user's
        row was rejected, so whoever shared a mailbox with a colleague simply
        never got email and nothing recorded that they hadn't."""
        notification = create_notification(session, world["tenant"])
        self._delivery(
            session,
            world,
            notification=notification,
            recipient_user_id=world["user"].id,
            recipient_address="compartido@example.test",
        )
        self._delivery(
            session,
            world,
            notification=notification,
            recipient_user_id=world["other_user"].id,
            recipient_address="compartido@example.test",
        )

        rows = session.exec(
            select(NotificationDelivery).where(
                NotificationDelivery.notification_id == notification.id
            )
        ).all()
        assert len(rows) == 2
        assert {row.recipient_user_id for row in rows} == {
            world["user"].id,
            world["other_user"].id,
        }

    def test_the_same_address_may_receive_different_notifications(self, session, world):
        first = create_notification(session, world["tenant"])
        second = create_notification(session, world["tenant"])
        self._delivery(session, world, notification=first)
        self._delivery(session, world, notification=second)

        rows = session.exec(
            select(NotificationDelivery).where(
                NotificationDelivery.recipient_address == "destinatario@example.test"
            )
        ).all()
        assert len(rows) == 2

    def test_unknown_channel_is_rejected(self, session, world):
        """PUSH is not a declared channel in Phase 3 — an unused value in a
        shipped constraint invites rows nothing knows how to process."""
        with pytest.raises(IntegrityError):
            self._delivery(session, world, channel="PUSH")
        session.rollback()

    def test_negative_attempts_are_rejected(self, session, world):
        with pytest.raises(IntegrityError):
            self._delivery(session, world, attempts=-1)
        session.rollback()

    def test_poller_index_exists(self, session):
        from sqlalchemy import inspect as sa_inspect

        indexes = sa_inspect(session.get_bind()).get_indexes("notification_delivery")
        poller = [i for i in indexes if i["name"] == "ix_notification_delivery_poller"]
        assert poller, "the Block E poller's claim query has no supporting index"
        assert poller[0]["column_names"] == ["status", "next_attempt_at"]

    def test_a_notification_now_materializes_a_pending_delivery_row(
        self, session, world
    ):
        """Céluma 1.3 Phase 3, Block D inverts this assertion.

        Block B asserted this table stayed empty after `notify()`, because
        delivery-row creation is preference-aware and was sequenced into
        Block D. Block D wires it, so the same call now leaves exactly one
        `EMAIL`/`PENDING` row per eligible recipient — and *nothing* sends
        it. The eligibility matrix (preferences, unsupported types, missing
        or invalid addresses, inactive users, duplicates) is covered in
        tests/http/test_notification_delivery.py; this one only pins the
        wiring, so a future refactor that quietly stops materializing fails
        here as well as there.
        """
        from app.schemas.notification import NotificationCommand
        from app.services.notification import NotificationService
        from app.models.notification import NotificationResourceType

        NotificationService.notify(
            session,
            NotificationCommand(
                tenant_id=world["tenant"].id,
                type=NotificationType.REPORT_PUBLISHED,
                resource_type=NotificationResourceType.REPORT,
                resource_id=uuid.uuid4(),
                occurrence_marker="delivery-check",
                template_key="report_published_v1",
                template_params={"order_number": "ORD-1", "actor_name": "Dra. M"},
                recipient_user_ids=[world["user"].id],
            ),
            strict=True,
        )
        session.commit()

        rows = session.exec(select(NotificationDelivery)).all()
        assert len(rows) == 1
        assert rows[0].recipient_user_id == world["user"].id
        assert rows[0].status == NotificationDeliveryStatus.PENDING
        assert rows[0].channel == NotificationChannel.EMAIL
        # Nothing has been attempted: no worker exists to attempt it.
        assert rows[0].attempts == 0
        assert rows[0].last_attempt_at is None
        assert rows[0].provider_message_id is None


class TestNotificationPreference:
    def _preference(self, session, world, **overrides):
        values = {
            "tenant_id": world["tenant"].id,
            "user_id": world["user"].id,
            "notification_type": NotificationType.REPORT_PUBLISHED.value,
        }
        values.update(overrides)
        preference = NotificationPreference(**values)
        session.add(preference)
        session.commit()
        session.refresh(preference)
        return preference

    def test_both_channels_default_to_enabled(self, session, world):
        preference = self._preference(session, world)

        assert preference.in_app_enabled is True
        assert preference.email_enabled is True
        assert preference.updated_at is not None

    def test_one_override_per_user_and_type(self, session, world):
        self._preference(session, world)

        with pytest.raises(IntegrityError):
            self._preference(session, world)
        session.rollback()

    def test_a_user_may_override_several_types(self, session, world):
        self._preference(session, world)
        self._preference(
            session, world, notification_type=NotificationType.REPORT_RETRACTED.value
        )

        rows = session.exec(
            select(NotificationPreference).where(
                NotificationPreference.user_id == world["user"].id
            )
        ).all()
        assert len(rows) == 2

    def test_unknown_notification_type_is_rejected(self, session, world):
        with pytest.raises(IntegrityError):
            self._preference(session, world, notification_type="NOT_A_TYPE")
        session.rollback()

    def test_no_preference_row_is_seeded_automatically(self, session, world):
        """Absence of a row means "use the default". Creating a user, a
        tenant or a notification must never materialize one."""
        create_notification(session, world["tenant"])
        create_user(session, world["tenant"], email="fresh@tenant-a.test")

        assert session.exec(select(NotificationPreference)).all() == []

    def test_the_preference_endpoints_exist_and_nothing_else_does(self):
        """Céluma 1.3 Phase 3, Block D inverts this assertion too.

        Block B asserted the preference *table* existed while its API did
        not. Block D adds exactly two endpoints and no more — in particular
        no delivery endpoint, no worker endpoint and no email endpoint, all
        of which stay internal or belong to Block E.
        """
        from app.main import app

        paths = {route.path for route in app.routes}
        preference_paths = {p for p in paths if "notification-preference" in p}
        assert preference_paths == {
            "/api/v1/notification-preferences",
            "/api/v1/notification-preferences/",
        }

        methods = {
            method
            for route in app.routes
            if getattr(route, "path", None) == "/api/v1/notification-preferences"
            for method in route.methods
            if method in {"GET", "PUT", "POST", "PATCH", "DELETE"}
        }
        assert methods == {"GET", "PUT"}

        # The delivery lifecycle is an internal service. Exposing it would
        # let a client drive a state machine a worker owns.
        assert not any("delivery" in path for path in paths)
        assert not any("notification-worker" in path for path in paths)
        assert not any(path.endswith("/resend") for path in paths)
