"""NotificationService tests (Céluma 1.3, Phase 3, Block B).

Block B wires no real clinical trigger, so every command here is synthetic.
That is the point: the service's guarantees — idempotency, tenant validation,
template safety, failure containment — must hold independently of which
transition eventually calls it, and Block F must be able to add real call
sites without re-proving any of them.

`occurrence_marker` is treated throughout as an opaque, caller-supplied
string. Nothing here encodes a per-event derivation rule (report_version.id,
a fresh request UUID, ...): choosing a marker that is stable across retries
of one occurrence and distinct across genuinely new ones is Block F's job at
each real trigger point, and baking a rule in now would bake in the wrong
one — a report can go DRAFT -> IN_REVIEW -> DRAFT -> IN_REVIEW on the same
ReportVersion row, which is two legitimate review requests, not one.
"""
import uuid
from datetime import datetime

import pytest
from sqlmodel import select

from app.models.notification import (
    Notification,
    NotificationRecipient,
    NotificationResourceType,
    NotificationSeverity,
    NotificationType,
)
from app.models.tenant import Tenant
from app.schemas.notification import NotificationCommand
from app.services.notification import (
    NotificationService,
    NotificationValidationError,
    build_idempotency_key,
    exclude_actor,
    normalize_recipient_ids,
    validate_recipient_tenants,
)
from app.services.notification_templates import (
    NOTIFICATION_TEMPLATES,
    NotificationTemplateError,
    get_template,
    render,
    validate_params,
)
from tests.http.factories import create_branch, create_tenant, create_user


#: Valid parameters for every registered template, so the safety tests below
#: can be driven across all six without hand-writing each case.
VALID_PARAMS = {
    NotificationType.REPORT_SUBMITTED: {
        "order_number": "ORD-2026-00152",
        "actor_name": "Dra. Martínez",
    },
    NotificationType.REPORT_PDF_READY: {"order_number": "ORD-2026-00152"},
    NotificationType.REPORT_PUBLISHED: {
        "order_number": "ORD-2026-00152",
        "actor_name": "Dra. Martínez",
    },
    NotificationType.REPORT_RETRACTED: {
        "order_number": "ORD-2026-00152",
        "actor_name": "Dra. Martínez",
    },
    NotificationType.ASSIGNMENT_ADDED: {
        "order_number": "ORD-2026-00152",
        "actor_name": "Dra. Martínez",
    },
    NotificationType.SAMPLE_STATUS_CHANGED: {
        "order_number": "ORD-2026-00152",
        "sample_code": "M-0031",
        # Pre-release remediation: `sample_status_changed_v2` takes the
        # already-translated label, not the raw `SampleState` enum value —
        # see app/services/sample_status_labels.py.
        "new_status_label": "Lista",
    },
    # Céluma 1.3, Phase 4, Block G. The two REACHED templates deliberately
    # declare no parameters at all — their copy states a fact that is as true
    # at 100% as at 250%, and a number there would read like an overage bill.
    NotificationType.STORAGE_USAGE_APPROACHING: {"usage_percent": 82},
    NotificationType.STORAGE_LIMIT_REACHED: {},
    NotificationType.USER_LIMIT_APPROACHING: {"usage_percent": 80},
    NotificationType.USER_LIMIT_REACHED: {},
}

#: The types whose template declares at least one required parameter. Two of
#: Block G's four declare none, which is a legitimate template shape — the
#: parametrized "missing params are rejected" case below is about templates
#: that *have* a requirement to violate, so it runs over this set rather than
#: over every type.
TYPES_WITH_REQUIRED_PARAMS = [
    notification_type
    for notification_type in NotificationType
    if VALID_PARAMS[notification_type]
]


@pytest.fixture(name="world")
def world_fixture(session):
    tenant = create_tenant(session, name="Tenant A")
    create_branch(session, tenant)
    reviewer = create_user(session, tenant, email="reviewer@tenant-a.test")
    author = create_user(session, tenant, email="author@tenant-a.test")

    other_tenant = create_tenant(session, name="Tenant B")
    create_branch(session, other_tenant)
    foreign_user = create_user(session, other_tenant, email="user@tenant-b.test")

    return {
        "tenant": tenant,
        "reviewer": reviewer,
        "author": author,
        "other_tenant": other_tenant,
        "foreign_user": foreign_user,
    }


def command(world, **overrides) -> NotificationCommand:
    notification_type = overrides.pop("type", NotificationType.REPORT_SUBMITTED)
    values = {
        "tenant_id": world["tenant"].id,
        "type": notification_type,
        "resource_type": NotificationResourceType.REPORT,
        "resource_id": uuid.uuid4(),
        "occurrence_marker": "order-event-1",
        "template_key": NOTIFICATION_TEMPLATES[notification_type].key,
        "template_params": dict(VALID_PARAMS[notification_type]),
        "recipient_user_ids": [world["reviewer"].id],
    }
    values.update(overrides)
    return NotificationCommand(**values)


class TestCreation:
    def test_creates_one_notification_with_one_recipient(self, session, world):
        notification_id = NotificationService.notify(
            session, command(world), strict=True
        )
        session.commit()

        assert notification_id is not None
        assert len(session.exec(select(Notification)).all()) == 1
        recipients = session.exec(select(NotificationRecipient)).all()
        assert [r.user_id for r in recipients] == [world["reviewer"].id]

    def test_renders_spanish_title_and_body_from_the_registry(self, session, world):
        notification_id = NotificationService.notify(
            session, command(world), strict=True
        )
        session.commit()
        notification = session.get(Notification, notification_id)

        assert notification.title == "Reporte listo para revisión — Orden ORD-2026-00152"
        assert notification.body == "El reporte fue enviado a revisión por Dra. Martínez."

    def test_persists_template_key_and_safe_params(self, session, world):
        """Content policy §8's hybrid option: the frozen text is the record of
        what was shown, the structured params are what produced it."""
        notification_id = NotificationService.notify(
            session, command(world), strict=True
        )
        session.commit()
        metadata = session.get(Notification, notification_id).notification_metadata

        assert metadata["template_key"] == "report_submitted_v1"
        assert metadata["template_params"] == {
            "order_number": "ORD-2026-00152",
            "actor_name": "Dra. Martínez",
        }

    def test_extra_metadata_cannot_overwrite_template_provenance(self, session, world):
        notification_id = NotificationService.notify(
            session,
            command(
                world,
                extra_metadata={"order_id": "abc", "template_key": "spoofed"},
            ),
            strict=True,
        )
        session.commit()
        metadata = session.get(Notification, notification_id).notification_metadata

        assert metadata["template_key"] == "report_submitted_v1"
        assert metadata["order_id"] == "abc"

    def test_severity_defaults_to_info(self, session, world):
        notification_id = NotificationService.notify(
            session, command(world), strict=True
        )
        session.commit()

        # Compared by value, not identity: the column is a plain VARCHAR, so
        # a row loaded from the database carries the string. `str`-backed
        # enums make that comparison correct either way — the same shape
        # ReportTemplateVersionStatus already has.
        assert session.get(Notification, notification_id).severity == (
            NotificationSeverity.INFO
        )

    def test_recipient_created_at_matches_the_notification(self, session, world):
        notification_id = NotificationService.notify(
            session, command(world, recipient_user_ids=[world["reviewer"].id, world["author"].id]), strict=True
        )
        session.commit()

        notification = session.get(Notification, notification_id)
        recipients = session.exec(select(NotificationRecipient)).all()
        assert {r.created_at for r in recipients} == {notification.created_at}

    def test_deduplicates_repeated_recipient_ids(self, session, world):
        NotificationService.notify(
            session,
            command(
                world,
                recipient_user_ids=[
                    world["reviewer"].id,
                    world["reviewer"].id,
                    world["author"].id,
                ],
            ),
            strict=True,
        )
        session.commit()

        recipients = session.exec(select(NotificationRecipient)).all()
        assert len(recipients) == 2
        assert {r.user_id for r in recipients} == {
            world["reviewer"].id,
            world["author"].id,
        }

    def test_excludes_the_actor_by_default(self, session, world):
        """Recipient-matrix cross-cutting rule 1 — the actor already saw the
        result in the response that triggered this."""
        NotificationService.notify(
            session,
            command(
                world,
                recipient_user_ids=[world["reviewer"].id, world["author"].id],
                created_by=world["author"].id,
            ),
            strict=True,
        )
        session.commit()

        recipients = session.exec(select(NotificationRecipient)).all()
        assert [r.user_id for r in recipients] == [world["reviewer"].id]

    def test_actor_can_be_kept_when_explicitly_requested(self, session, world):
        NotificationService.notify(
            session,
            command(
                world,
                recipient_user_ids=[world["reviewer"].id, world["author"].id],
                created_by=world["author"].id,
                exclude_actor=False,
            ),
            strict=True,
        )
        session.commit()

        recipients = session.exec(select(NotificationRecipient)).all()
        assert len(recipients) == 2

    def test_zero_recipients_creates_the_notification_and_warns(
        self, session, world, caplog
    ):
        """Recipient-matrix rule 6: the notification row is the audit record
        that the event happened. An empty recipient set is invisible in every
        inbox but must never fail the caller's domain transaction."""
        with caplog.at_level("WARNING"):
            notification_id = NotificationService.notify(
                session, command(world, recipient_user_ids=[]), strict=True
            )
        session.commit()

        assert notification_id is not None
        assert session.exec(select(NotificationRecipient)).all() == []
        assert any(
            record.__dict__.get("event") == "notification.create.no_recipients"
            for record in caplog.records
        )

    def test_actor_exclusion_emptying_the_set_is_not_an_error(self, session, world):
        notification_id = NotificationService.notify(
            session,
            command(
                world,
                recipient_user_ids=[world["author"].id],
                created_by=world["author"].id,
            ),
            strict=True,
        )
        session.commit()

        assert notification_id is not None
        assert session.exec(select(NotificationRecipient)).all() == []

    def test_rejects_a_recipient_from_another_tenant(self, session, world):
        with pytest.raises(NotificationValidationError) as exc:
            NotificationService.notify(
                session,
                command(
                    world,
                    recipient_user_ids=[world["reviewer"].id, world["foreign_user"].id],
                ),
                strict=True,
            )
        session.rollback()

        assert exc.value.code == "cross_tenant_recipient"
        assert session.exec(select(Notification)).all() == []

    def test_cross_tenant_rejection_does_not_reveal_the_other_tenant(self, session, world):
        with pytest.raises(NotificationValidationError) as exc:
            NotificationService.notify(
                session,
                command(world, recipient_user_ids=[world["foreign_user"].id]),
                strict=True,
            )
        session.rollback()

        message = str(exc.value)
        assert str(world["other_tenant"].id) not in message
        assert str(world["foreign_user"].id) not in message

    def test_rejects_a_recipient_that_does_not_exist(self, session, world):
        with pytest.raises(NotificationValidationError) as exc:
            NotificationService.notify(
                session, command(world, recipient_user_ids=[uuid.uuid4()]), strict=True
            )
        session.rollback()

        assert exc.value.code == "unknown_recipient"

    def test_non_strict_mode_returns_none_instead_of_raising(self, session, world):
        """The production default. A Block F wiring mistake must not abort a
        clinical transition."""
        result = NotificationService.notify(
            session, command(world, recipient_user_ids=[world["foreign_user"].id])
        )

        assert result is None
        assert session.exec(select(Notification)).all() == []

    def test_does_not_commit_the_callers_transaction(self, session, world):
        """The caller owns the transaction boundary, so the notification lands
        in the same atomic commit as the domain transition."""
        NotificationService.notify(session, command(world), strict=True)

        assert session.in_transaction()
        session.rollback()
        assert session.exec(select(Notification)).all() == []


class TestIdempotency:
    def test_same_command_twice_returns_the_same_id(self, session, world):
        cmd = command(world)
        first = NotificationService.notify(session, cmd, strict=True)
        session.commit()
        second = NotificationService.notify(session, cmd, strict=True)
        session.commit()

        assert first == second
        assert len(session.exec(select(Notification)).all()) == 1

    def test_duplicate_does_not_duplicate_recipient_rows(self, session, world):
        cmd = command(world, recipient_user_ids=[world["reviewer"].id, world["author"].id])
        NotificationService.notify(session, cmd, strict=True)
        session.commit()
        NotificationService.notify(session, cmd, strict=True)
        session.commit()

        assert len(session.exec(select(NotificationRecipient)).all()) == 2

    def test_duplicate_does_not_overwrite_frozen_content(self, session, world):
        cmd = command(world)
        notification_id = NotificationService.notify(session, cmd, strict=True)
        session.commit()
        original = session.get(Notification, notification_id)
        original_title, original_created = original.title, original.created_at

        # A second call for the same occurrence carrying different copy must
        # not rewrite what the recipient already saw.
        NotificationService.notify(
            session,
            command(
                world,
                resource_id=cmd.resource_id,
                occurrence_marker=cmd.occurrence_marker,
                template_params={
                    "order_number": "ORD-DIFFERENT",
                    "actor_name": "Otra Persona",
                },
            ),
            strict=True,
        )
        session.commit()
        session.refresh(original)

        assert original.title == original_title
        assert original.created_at == original_created

    def test_duplicate_does_not_re_resolve_recipients(self, session, world):
        cmd = command(world, recipient_user_ids=[world["reviewer"].id])
        NotificationService.notify(session, cmd, strict=True)
        session.commit()

        # Same occurrence, different recipient list — recipients were already
        # resolved once, and the duplicate path must not touch them.
        NotificationService.notify(
            session,
            command(
                world,
                resource_id=cmd.resource_id,
                occurrence_marker=cmd.occurrence_marker,
                recipient_user_ids=[world["author"].id],
            ),
            strict=True,
        )
        session.commit()

        recipients = session.exec(select(NotificationRecipient)).all()
        assert [r.user_id for r in recipients] == [world["reviewer"].id]

    def test_a_different_occurrence_marker_creates_a_new_notification(
        self, session, world
    ):
        """The same resource legitimately produces several notifications: a
        report can go DRAFT -> IN_REVIEW -> DRAFT -> IN_REVIEW, and each
        review request is a real, separate event."""
        resource_id = uuid.uuid4()
        first = NotificationService.notify(
            session,
            command(world, resource_id=resource_id, occurrence_marker="order-event-1"),
            strict=True,
        )
        session.commit()
        second = NotificationService.notify(
            session,
            command(world, resource_id=resource_id, occurrence_marker="order-event-2"),
            strict=True,
        )
        session.commit()

        assert first != second
        assert len(session.exec(select(Notification)).all()) == 2

    def test_the_same_marker_in_another_tenant_creates_a_separate_notification(
        self, session, world
    ):
        other_user = create_user(
            session, world["other_tenant"], email="reviewer@tenant-b.test"
        )
        resource_id = uuid.uuid4()

        first = NotificationService.notify(
            session, command(world, resource_id=resource_id), strict=True
        )
        session.commit()
        second = NotificationService.notify(
            session,
            command(
                world,
                tenant_id=world["other_tenant"].id,
                resource_id=resource_id,
                recipient_user_ids=[other_user.id],
            ),
            strict=True,
        )
        session.commit()

        assert first != second
        notifications = session.exec(select(Notification)).all()
        assert len(notifications) == 2
        assert len({n.idempotency_key for n in notifications}) == 1

    def test_a_different_resource_id_creates_a_new_notification(self, session, world):
        NotificationService.notify(session, command(world), strict=True)
        session.commit()
        NotificationService.notify(session, command(world), strict=True)
        session.commit()

        assert len(session.exec(select(Notification)).all()) == 2

    def test_the_database_constraint_is_what_resolves_a_competing_insert(
        self, session, world, engine
    ):
        """Two sessions, each committing the same occurrence — the second is
        resolved by the unique index, not by an application-level check, and
        the loser gets the winner's id back rather than an error.

        This is idempotency-strategy §6 example 5 with the SELECT-then-INSERT
        race window actually present: the second session computes its key
        before the first has committed.
        """
        from sqlmodel import Session

        cmd = command(world)
        with Session(engine) as first_session, Session(engine) as second_session:
            # Both sessions reach the insert before either commits.
            first_id = NotificationService.notify(first_session, cmd, strict=True)
            first_session.commit()

            second_id = NotificationService.notify(second_session, cmd, strict=True)
            second_session.commit()

        assert first_id == second_id
        assert len(session.exec(select(Notification)).all()) == 1

    def test_the_caller_session_stays_usable_after_the_duplicate_path(
        self, session, world
    ):
        """The failure mode this design exists to avoid: an IntegrityError
        would abort the caller's whole PostgreSQL transaction, making every
        later statement fail until someone rolls back. ON CONFLICT DO NOTHING
        means the duplicate is never an exception at all."""
        cmd = command(world)
        NotificationService.notify(session, cmd, strict=True)
        session.commit()

        NotificationService.notify(session, cmd, strict=True)

        # Same transaction, unrelated write — must still work.
        tenant = Tenant(name="Written after the duplicate path")
        session.add(tenant)
        session.commit()

        assert session.get(Tenant, tenant.id) is not None


class TestFailureContainment:
    """The load-bearing guarantee: a notification failure must not damage the
    caller's transaction.

    Céluma's architectural principle §4.3 — "publishing a report must never
    fail because notification delivery did" — is only true if the caller's
    other writes survive. A naive try/except around a failing INSERT does not
    achieve this on PostgreSQL: the transaction is already aborted by the
    time the exception surfaces.
    """

    def test_an_internal_failure_is_contained_and_the_caller_can_still_commit(
        self, session, world, monkeypatch, caplog
    ):
        # 1. The caller starts a transaction and writes unrelated domain data.
        unrelated = Tenant(name="Caller's unrelated write")
        session.add(unrelated)
        session.flush()

        # 2. Notification persistence fails partway through — after the
        #    notification row is inserted, while recipients are being written,
        #    which is the worst case: real rows already exist in the savepoint.
        def boom(*args, **kwargs):
            raise RuntimeError("simulated notification persistence failure")

        monkeypatch.setattr(
            "app.services.notification.create_recipient_rows", boom
        )

        with caplog.at_level("ERROR"):
            result = NotificationService.notify(session, command(world))

        # 3. The failure was contained, not propagated.
        assert result is None
        assert any(
            record.__dict__.get("event") == "notification.create.failed"
            for record in caplog.records
        )

        # 4. The caller's transaction is intact and still commits.
        session.commit()

        # 5. The unrelated write survived.
        assert session.get(Tenant, unrelated.id) is not None

        # 6. No half-written notification was left behind by the savepoint.
        assert session.exec(select(Notification)).all() == []
        assert session.exec(select(NotificationRecipient)).all() == []

    def test_the_contained_failure_does_not_log_notification_content(
        self, session, world, monkeypatch, caplog
    ):
        """Content policy §7: rendered text and template params must never
        reach a log line — log aggregation has different retention and access
        controls than the primary database."""

        def boom(*args, **kwargs):
            raise RuntimeError("Reporte listo para revisión — Orden ORD-2026-00152")

        monkeypatch.setattr("app.services.notification.create_recipient_rows", boom)

        with caplog.at_level("ERROR"):
            NotificationService.notify(session, command(world))
        session.rollback()

        for record in caplog.records:
            rendered = record.getMessage() + str(record.__dict__)
            assert "Reporte listo para revisión" not in rendered
            assert "Dra. Martínez" not in rendered
            assert "ORD-2026-00152" not in rendered

    def test_the_service_never_rolls_back_the_whole_caller_transaction(
        self, session, world, monkeypatch
    ):
        rollbacks = []
        original = type(session).rollback

        def tracking_rollback(self, *args, **kwargs):
            rollbacks.append(True)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(type(session), "rollback", tracking_rollback)

        def boom(*args, **kwargs):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr("app.services.notification.create_recipient_rows", boom)
        NotificationService.notify(session, command(world))

        assert rollbacks == [], "notify() must unwind its savepoint, not the transaction"


class TestTemplateSafety:
    @pytest.mark.parametrize("notification_type", list(NotificationType))
    def test_every_approved_type_has_a_registered_template(self, notification_type):
        template = NOTIFICATION_TEMPLATES[notification_type]
        assert template.notification_type is notification_type
        # Not hardcoded to "_v1": pre-release remediation shipped
        # `sample_status_changed_v2`, so this checks the general versioned-key
        # shape instead (see test_every_in_app_key_carries_an_explicit_version_suffix
        # in test_notification_localization.py, the canonical form of this check).
        suffix = template.key.rsplit("_", 1)[-1]
        assert suffix.startswith("v") and suffix[1:].isdigit(), template.key
        assert template.title

    @pytest.mark.parametrize("notification_type", list(NotificationType))
    def test_every_template_renders_from_its_valid_params(self, notification_type):
        template = NOTIFICATION_TEMPLATES[notification_type]
        title, body, safe = render(template, VALID_PARAMS[notification_type])

        assert title and len(title) <= 255
        assert body is None or len(body) <= 1000
        assert set(safe) == set(VALID_PARAMS[notification_type])

    @pytest.mark.parametrize("notification_type", list(NotificationType))
    def test_unknown_params_are_rejected(self, notification_type):
        template = NOTIFICATION_TEMPLATES[notification_type]
        params = {**VALID_PARAMS[notification_type], "diagnosis": "carcinoma"}

        with pytest.raises(NotificationTemplateError) as exc:
            validate_params(template, params)
        assert exc.value.code == "unknown_param"

    @pytest.mark.parametrize("notification_type", TYPES_WITH_REQUIRED_PARAMS)
    def test_missing_required_params_are_rejected(self, notification_type):
        template = NOTIFICATION_TEMPLATES[notification_type]

        with pytest.raises(NotificationTemplateError) as exc:
            validate_params(template, {})
        assert exc.value.code == "missing_param"

    @pytest.mark.parametrize("notification_type", list(NotificationType))
    def test_a_template_with_no_params_renders_from_nothing(self, notification_type):
        """The complement of the case above, asserted for every type so the
        two together cover the whole enum.

        A parameterless template must render cleanly from `{}` rather than
        raising — that is what makes "this event needs no data" expressible.
        `STORAGE_LIMIT_REACHED` and `USER_LIMIT_REACHED` are the two."""
        template = NOTIFICATION_TEMPLATES[notification_type]
        if template.required_param_names:
            pytest.skip("covered by test_missing_required_params_are_rejected")
        title, body, safe = render(template, {})
        assert title and safe == {}
        assert body is None or body

    @pytest.mark.parametrize("notification_type", TYPES_WITH_REQUIRED_PARAMS)
    def test_oversized_params_are_rejected(self, notification_type):
        template = NOTIFICATION_TEMPLATES[notification_type]
        params = dict(VALID_PARAMS[notification_type])
        first = sorted(params)[0]
        params[first] = "x" * 500

        with pytest.raises(NotificationTemplateError) as exc:
            validate_params(template, params)
        assert exc.value.code == "param_too_long"

    @pytest.mark.parametrize(
        "unsafe",
        [
            "<script>alert(1)</script>",
            "<b>ORD-1</b>",
            "https://celuma.example/reports/123",
            "javascript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "Bearer abc123",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g",
            "ORD-1\nDiagnóstico: carcinoma",
        ],
        ids=[
            "script-tag",
            "markup",
            "url",
            "javascript-scheme",
            "data-uri",
            "bearer-token",
            "jwt",
            "newline-injection",
        ],
    )
    def test_unsafe_parameter_values_are_rejected(self, unsafe):
        """Markup would be rendered, a URL turns a notification into a
        navigation vector, and token-shaped material must never be embedded
        (content policy §5 — a notification can never function as a
        credential)."""
        template = NOTIFICATION_TEMPLATES[NotificationType.REPORT_SUBMITTED]

        with pytest.raises(NotificationTemplateError) as exc:
            validate_params(
                template, {"order_number": unsafe, "actor_name": "Dra. Martínez"}
            )
        assert exc.value.code in {"unsafe_param_content", "param_too_long"}

    @pytest.mark.parametrize(
        "nested", [{"a": 1}, ["a"], ("a",), {"a"}], ids=["dict", "list", "tuple", "set"]
    )
    def test_nested_structures_are_rejected(self, nested):
        """Stringifying a structure would let its repr land in the rendered
        text; rejecting outright means it cannot."""
        template = NOTIFICATION_TEMPLATES[NotificationType.REPORT_SUBMITTED]

        with pytest.raises(NotificationTemplateError) as exc:
            validate_params(
                template, {"order_number": nested, "actor_name": "Dra. Martínez"}
            )
        assert exc.value.code == "unsafe_param_type"

    def test_an_unknown_template_key_is_rejected_by_the_schema(self, world):
        with pytest.raises(ValueError):
            command(world, template_key="not_a_real_template_v1")

    def test_a_template_key_from_the_wrong_type_is_rejected(self, session, world):
        """Registered but mismatched: a copy/paste that pairs the right key
        with the wrong type would otherwise produce a plausible-looking but
        wrong notification."""
        with pytest.raises(NotificationTemplateError) as exc:
            get_template(NotificationType.REPORT_SUBMITTED, "report_published_v1")
        assert exc.value.code == "template_key_mismatch"

    @pytest.mark.parametrize("notification_type", list(NotificationType))
    def test_clinical_vocabulary_never_appears_in_rendered_output(
        self, notification_type
    ):
        """The regression guard the implementation plan asks for: if a future
        template gains a parameter that interpolates clinical content, this
        fails."""
        template = NOTIFICATION_TEMPLATES[notification_type]
        title, body, _ = render(template, VALID_PARAMS[notification_type])
        rendered = f"{title} {body or ''}".lower()

        forbidden = (
            "diagnóstico",
            "diagnostico",
            "carcinoma",
            "malignidad",
            "biopsia de",
            "microscópic",
            "macroscópic",
            "hallazgo",
            "paciente",
        )
        for word in forbidden:
            assert word not in rendered, f"{template.key} rendered '{word}'"

    def test_user_authored_free_text_has_no_parameter_to_arrive_through(self):
        """Content policy §4's two named leak vectors — pathologist-renamed
        report titles and free-text fields (comments, retraction reasons) —
        are closed structurally: no template declares a parameter for them."""
        declared = {
            param.name
            for template in NOTIFICATION_TEMPLATES.values()
            for param in template.params
        }
        assert declared == {
            "order_number",
            "actor_name",
            "sample_code",
            "new_status_label",
            # Céluma 1.3, Phase 4, Block G. A backend-computed integer
            # percentage — the only parameter in the registry with no
            # user-editable field anywhere in its provenance, and therefore
            # not a leak vector for anything §4 names.
            "usage_percent",
        }

        retracted = NOTIFICATION_TEMPLATES[NotificationType.REPORT_RETRACTED]
        assert "reason" not in retracted.allowed_param_names

    def test_the_command_schema_has_no_title_or_body_field(self):
        """The structural guarantee: a production caller cannot bypass the
        registry by supplying pre-rendered content."""
        assert "title" not in NotificationCommand.model_fields
        assert "body" not in NotificationCommand.model_fields

    def test_the_command_schema_forbids_unknown_fields(self, world):
        with pytest.raises(ValueError):
            NotificationCommand(
                tenant_id=world["tenant"].id,
                type=NotificationType.REPORT_SUBMITTED,
                resource_type=NotificationResourceType.REPORT,
                resource_id=uuid.uuid4(),
                occurrence_marker="m",
                template_key="report_submitted_v1",
                title="Bypass attempt",
            )


class TestHelpers:
    """The generic helpers Block F reuses when it writes the real, event-
    specific recipient resolvers."""

    def test_build_idempotency_key_shape(self, world):
        cmd = command(world, occurrence_marker="marker-1")
        assert build_idempotency_key(cmd) == (
            f"REPORT_SUBMITTED:report:{cmd.resource_id}:marker-1"
        )

    def test_build_idempotency_key_rejects_an_oversized_key(self, world):
        """Defence in depth against a future longer resource type or marker
        cap silently producing a key the column truncates. `model_construct`
        skips validation because the schema's own 120-character
        `occurrence_marker` limit already makes this unreachable through
        ordinary construction — which is the point: the guard covers the case
        where that limit changes."""
        oversized = NotificationCommand.model_construct(
            tenant_id=world["tenant"].id,
            type=NotificationType.REPORT_SUBMITTED,
            resource_type=NotificationResourceType.REPORT,
            resource_id=uuid.uuid4(),
            occurrence_marker="x" * 300,
            template_key="report_submitted_v1",
        )

        with pytest.raises(NotificationValidationError) as exc:
            build_idempotency_key(oversized)
        assert exc.value.code == "idempotency_key_too_long"

    def test_normalize_recipient_ids_deduplicates_preserving_order(self):
        a, b = uuid.uuid4(), uuid.uuid4()
        assert normalize_recipient_ids([a, b, a, b, a]) == [a, b]

    def test_normalize_recipient_ids_on_an_empty_set(self):
        assert normalize_recipient_ids([]) == []

    def test_exclude_actor_removes_only_the_actor(self):
        a, b = uuid.uuid4(), uuid.uuid4()
        assert exclude_actor([a, b], a) == [b]

    def test_exclude_actor_with_no_actor_is_a_no_op(self):
        a, b = uuid.uuid4(), uuid.uuid4()
        assert exclude_actor([a, b], None) == [a, b]

    def test_validate_recipient_tenants_accepts_same_tenant_users(self, session, world):
        ids = [world["reviewer"].id, world["author"].id]
        assert validate_recipient_tenants(session, ids, world["tenant"].id) == ids

    def test_validate_recipient_tenants_on_an_empty_set(self, session, world):
        assert validate_recipient_tenants(session, [], world["tenant"].id) == []

    def test_supports_an_arbitrary_stable_occurrence_marker(self, session, world):
        """Block F derives markers from real transition identifiers — an
        OrderEvent id, or f"{order_event.id}:{added_user_id}" for assignment.
        The service must accept any stable string without knowing the rule."""
        resource_id = uuid.uuid4()
        event_id, user_id = uuid.uuid4(), uuid.uuid4()

        for marker in (str(event_id), f"{event_id}:{user_id}", "2026-08-05T12:00:00"):
            first = NotificationService.notify(
                session,
                command(world, resource_id=resource_id, occurrence_marker=marker),
                strict=True,
            )
            session.commit()
            repeat = NotificationService.notify(
                session,
                command(world, resource_id=resource_id, occurrence_marker=marker),
                strict=True,
            )
            session.commit()
            assert first == repeat

        assert len(session.exec(select(Notification)).all()) == 3
