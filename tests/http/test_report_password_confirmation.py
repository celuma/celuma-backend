"""Céluma 1.4 (#13): password-confirmed signing and reopening, credential
hygiene and the canonical lifecycle events.

Contract: celuma-engineering `specs/reports/lifecycle.md` ("Password
confirmation", "Lifecycle events") and ADR 0017.

What is proven here, on real Postgres with the real route stack:

* every signing route (`/sign-and-publish`, legacy `/sign`) and `/reopen`
  require the authenticated actor's OWN current password; missing, wrong,
  another user's and an administrator's password are refused;
* the confirmation is identity, not authority: role, capability, assignment,
  tenant and lifecycle refusals happen first, unchanged, and their callers'
  passwords are never examined (no security audit row appears for them);
* a refusal changes nothing — report, versions, signature, PDF, stored
  objects, accounting rows, reviews, timeline, clinical audit, notifications —
  except, for a wrong password, ONE credential-free security audit row;
* the per-actor throttle refuses even a correct password inside the window;
* the password never reaches any stored row, stored object, log record or
  validation response;
* each successful transition writes exactly one canonical audit row and one
  timeline row, also under retries and concurrent requests;
* a signed version's PDF can no longer be replaced or regenerated, whatever
  the report's status (the state an amendment will create).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from app.core.config import settings
from app.core.security import hash_password
from app.models.audit import AuditLog
from app.models.enums import EventType, ReportStatus, ReviewStatus
from app.models.events import OrderEvent
from app.models.report import Report, ReportVersion
from app.models.report_review import ReportReview
from app.schemas.report import ReportReopenRequest, ReportSignRequest
from app.services.password_confirmation import PASSWORD_CONFIRMATION_FAILED_ACTION

from .conftest import FakeS3Service, make_pdf_bytes, rendered_log_text
from .factories import (
    TEST_USER_PASSWORD,
    assign_reviewer,
    auth_headers,
    create_branch,
    create_order,
    create_report,
    create_tenant,
    create_user,
)
from .numbering_support import fresh_client, run_parallel  # noqa: F401  (fixture)

SIGN_AND_PUBLISH = "/api/v1/reports/{}/sign-and-publish"
SIGN = "/api/v1/reports/{}/sign"
REOPEN = "/api/v1/reports/{}/reopen"
APPROVE = "/api/v1/reports/{}/approve"
SIGNING_ROUTES = (SIGN_AND_PUBLISH, SIGN)

#: Synthetic, distinctive values: easy to search for in every sink.
WRONG_PASSWORD = "wrong-confirmation-7f3a9c"
OTHER_USER_PASSWORD = "other-user-confirmation-51d2"

CONFIRM = {"password": TEST_USER_PASSWORD}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def lab(session: Session):
    """An APPROVED, unsigned V2 report with a READY pre-signature PDF (so the
    legacy two-step `/sign` can run too), its assigned reviewer (decision
    recorded) and the other actors the contract distinguishes."""
    tenant = create_tenant(session)
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch)
    report, version = create_report(
        session, tenant, branch, order, status=ReportStatus.APPROVED, pdf_generation_status="READY"
    )
    reviewer = create_user(session, tenant, email="rev@pc.example", roles=("reviewer",))
    review = assign_reviewer(session, order, reviewer, report=report, status=ReviewStatus.APPROVED)
    review.decision_at = datetime.utcnow()
    session.add(review)
    session.commit()

    other = create_user(session, tenant, email="other-rev@pc.example", roles=("reviewer",))
    admin = create_user(session, tenant, email="admin@pc.example", roles=("admin",))
    for user, secret in ((other, OTHER_USER_PASSWORD), (admin, OTHER_USER_PASSWORD + "-admin")):
        user.hashed_password = hash_password(secret)
        session.add(user)
    session.commit()

    return {
        "tenant": tenant,
        "branch": branch,
        "order": order,
        "report": report,
        "version": version,
        "reviewer": reviewer,
        "other_reviewer": other,
        "admin": admin,
        "admin_password": OTHER_USER_PASSWORD + "-admin",
    }


_TABLES = (
    "report", "report_version", "report_review", "order_event", "storage_object",
    "tenant_usage", "notification", "notification_recipient", "notification_delivery",
    "order_comment", '"order"',
)


def _rows(session: Session, table: str) -> list[str]:
    return sorted(
        session.execute(text(f"SELECT row_to_json(t)::text FROM {table} t")).scalars().all()
    )


def _clinical_audit(session: Session) -> list[str]:
    return sorted(
        session.execute(
            text(
                "SELECT row_to_json(t)::text FROM audit_log t "
                "WHERE action <> :security"
            ).bindparams(security=PASSWORD_CONFIRMATION_FAILED_ACTION)
        ).scalars().all()
    )


class _Unchanged:
    """Everything a refused confirmation must leave alone: every row of every
    table the routes can touch, the clinical audit trail and every stored
    object's bytes."""

    def __init__(self, session: Session):
        self.session = session
        session.expire_all()
        self.tables = {t: _rows(session, t) for t in _TABLES}
        self.audit = _clinical_audit(session)
        self.objects = dict(FakeS3Service.store)

    def assert_intact(self):
        self.session.expire_all()
        for table, rows in self.tables.items():
            assert _rows(self.session, table) == rows, f"{table} changed on a refused request"
        assert _clinical_audit(self.session) == self.audit, "clinical audit changed"
        assert FakeS3Service.store == self.objects, "a stored object changed"


def _security_rows(session: Session) -> list[AuditLog]:
    session.expire_all()
    return list(
        session.exec(
            select(AuditLog).where(AuditLog.action == PASSWORD_CONFIRMATION_FAILED_ACTION)
        ).all()
    )


def _events(session: Session, order_id, event_type: EventType) -> list[OrderEvent]:
    session.expire_all()
    return list(
        session.exec(
            select(OrderEvent).where(
                OrderEvent.order_id == order_id, OrderEvent.event_type == event_type
            )
        ).all()
    )


def _audit(session: Session, report_id, action: str) -> list[AuditLog]:
    session.expire_all()
    return list(
        session.exec(
            select(AuditLog).where(AuditLog.entity_id == report_id, AuditLog.action == action)
        ).all()
    )


def _code(resp) -> str | None:
    detail = resp.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


def _post(client, route, report, actor, body):
    return client.post(route.format(report.id), json=body, headers=auth_headers(actor))


# ---------------------------------------------------------------------------
# Signing: both routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", SIGNING_ROUTES)
class TestSigningRequiresTheActorsPassword:
    def test_correct_password_publishes_with_one_canonical_event(
        self, client, session, lab, stub_pdf_render, route
    ):
        resp = _post(client, route, lab["report"], lab["reviewer"], {**CONFIRM, "changelog": "Firma"})
        assert resp.status_code == 200, resp.text

        session.expire_all()
        report = session.get(Report, lab["report"].id)
        version = session.get(ReportVersion, lab["version"].id)
        assert report.status == ReportStatus.PUBLISHED
        assert version.signed_by == lab["reviewer"].id and version.signed_at is not None

        events = _events(session, lab["order"].id, EventType.REPORT_PUBLISHED)
        assert len(events) == 1
        meta = events[0].event_metadata
        assert meta["action"] == "REPORT_PUBLISHED"
        assert meta["publication_kind"] == "INITIAL"
        assert meta["report_id"] == str(report.id)
        assert meta["report_version_id"] == str(version.id)
        assert meta["version_no"] == version.version_no
        assert meta["pdf_sha256"] == version.pdf_sha256
        assert meta["signed_at"] == version.signed_at.isoformat()
        assert meta["signer_id"] == str(lab["reviewer"].id)
        assert meta["from_status"] == "APPROVED" and meta["to_status"] == "PUBLISHED"
        assert meta["confirmation"] == "password"
        assert meta["changelog"] == "Firma"
        assert events[0].description  # Spanish fallback for consumers without the action
        # Publication is no longer recorded as a second approval.
        assert all(
            not (e.event_metadata or {}).get("published")
            for e in _events(session, lab["order"].id, EventType.REPORT_APPROVED)
        )

        action = "REPORT.SIGN_AND_PUBLISH" if route == SIGN_AND_PUBLISH else "REPORT.SIGN"
        audit = _audit(session, report.id, action)
        assert len(audit) == 1
        values = audit[0].new_values
        assert values["password_confirmed"] is True
        assert values["publication_kind"] == "INITIAL"
        assert values["report_version_id"] == str(version.id)
        assert values["pdf_sha256"] == version.pdf_sha256
        assert _security_rows(session) == []

    @pytest.mark.parametrize("body", [{}, {"password": ""}, {"password": None}])
    def test_missing_password_is_refused_without_side_effects(
        self, client, session, lab, stub_pdf_render, route, body
    ):
        before = _Unchanged(session)
        resp = _post(client, route, lab["report"], lab["reviewer"], body)
        assert resp.status_code == 403, resp.text
        assert _code(resp) == "password_confirmation_required"
        before.assert_intact()
        assert stub_pdf_render.call_count == 0
        assert _security_rows(session) == []  # no guess was made

    def test_wrong_password_is_refused_and_only_audited(
        self, client, session, lab, stub_pdf_render, route
    ):
        before = _Unchanged(session)
        resp = _post(client, route, lab["report"], lab["reviewer"], {"password": WRONG_PASSWORD})
        assert resp.status_code == 403, resp.text
        assert _code(resp) == "password_confirmation_invalid"
        before.assert_intact()
        assert stub_pdf_render.call_count == 0

        rows = _security_rows(session)
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_user_id == lab["reviewer"].id
        assert row.entity_id == lab["report"].id and row.entity_type == "report"
        assert row.tenant_id == lab["tenant"].id
        assert row.new_values == {
            "attempted_action": "REPORT.SIGN_AND_PUBLISH" if route == SIGN_AND_PUBLISH else "REPORT.SIGN",
            "failure": "invalid_password",
        }
        assert row.event_type is None and row.order_id is None  # never a timeline event

    @pytest.mark.parametrize("whose", ["other_reviewer", "admin"])
    def test_someone_elses_password_does_not_confirm(
        self, client, session, lab, stub_pdf_render, route, whose
    ):
        secret = OTHER_USER_PASSWORD if whose == "other_reviewer" else lab["admin_password"]
        before = _Unchanged(session)
        resp = _post(client, route, lab["report"], lab["reviewer"], {"password": secret})
        assert resp.status_code == 403
        assert _code(resp) == "password_confirmation_invalid"
        before.assert_intact()
        assert len(_security_rows(session)) == 1

    def test_retry_after_success_is_refused_and_writes_nothing(
        self, client, session, lab, stub_pdf_render, route
    ):
        assert _post(client, route, lab["report"], lab["reviewer"], CONFIRM).status_code == 200
        before = _Unchanged(session)
        again = _post(client, route, lab["report"], lab["reviewer"], CONFIRM)
        assert again.status_code == 400, again.text
        before.assert_intact()
        assert len(_events(session, lab["order"].id, EventType.REPORT_PUBLISHED)) == 1

    def test_failed_generation_records_no_publication(
        self, client, session, lab, stub_pdf_render, route
    ):
        from app.services.report_pdf_generation import ReportPdfGenerationError

        stub_pdf_render.fail(ReportPdfGenerationError("RENDER_FAILED", "synthetic failure"))
        resp = _post(client, route, lab["report"], lab["reviewer"], CONFIRM)
        assert resp.status_code == 422, resp.text
        session.expire_all()
        report = session.get(Report, lab["report"].id)
        version = session.get(ReportVersion, lab["version"].id)
        assert report.status == ReportStatus.APPROVED and report.published_at is None
        assert version.signed_at is None and version.publish_started_at is None
        assert _events(session, lab["order"].id, EventType.REPORT_PUBLISHED) == []
        assert _audit(session, report.id, "REPORT.SIGN") == []
        assert _audit(session, report.id, "REPORT.SIGN_AND_PUBLISH") == []


@pytest.mark.parametrize("route", SIGNING_ROUTES)
class TestConfirmationGrantsNoAuthority:
    """A correct password never widens the reviewer contract, and callers
    refused by it are refused BEFORE their password is examined: a wrong
    password from them produces the authorization answer and no security row."""

    @pytest.mark.parametrize("password", [TEST_USER_PASSWORD, WRONG_PASSWORD])
    @pytest.mark.parametrize("actor", ["admin", "superuser", "pathologist", "unassigned_reviewer"])
    def test_non_signers_are_refused_by_the_reviewer_contract(
        self, client, session, lab, stub_pdf_render, route, actor, password
    ):
        roles = {
            "admin": ("admin",),
            "superuser": ("superuser",),
            "pathologist": ("pathologist",),
            "unassigned_reviewer": ("reviewer",),
        }[actor]
        user = create_user(session, lab["tenant"], email=f"{actor}-{uuid.uuid4().hex[:6]}@pc.example", roles=roles)
        before = _Unchanged(session)
        resp = _post(client, route, lab["report"], user, {"password": password})
        assert resp.status_code == 403, resp.text
        assert _code(resp) is None  # the reviewer-contract refusal, not a confirmation code
        before.assert_intact()
        assert _security_rows(session) == []

    def test_foreign_tenant_reviewer_is_refused_before_confirmation(
        self, client, session, lab, stub_pdf_render, route
    ):
        other_tenant = create_tenant(session, name="Other Lab")
        intruder = create_user(session, other_tenant, email="intruder@other.example", roles=("reviewer",))
        before = _Unchanged(session)
        resp = _post(client, route, lab["report"], intruder, {"password": WRONG_PASSWORD})
        assert resp.status_code == 403
        before.assert_intact()
        assert _security_rows(session) == []

    def test_wrong_state_is_reported_before_confirmation(
        self, client, session, lab, stub_pdf_render, route
    ):
        report = session.get(Report, lab["report"].id)
        report.status = ReportStatus.IN_REVIEW
        session.add(report)
        session.commit()
        resp = _post(client, route, lab["report"], lab["reviewer"], {"password": WRONG_PASSWORD})
        assert resp.status_code == 400
        assert _security_rows(session) == []


# ---------------------------------------------------------------------------
# Reopening
# ---------------------------------------------------------------------------


class TestReopenRequiresTheActorsPassword:
    @pytest.mark.parametrize("actor", ["reviewer", "admin", "superuser"])
    def test_authorized_reopeners_with_their_own_password(self, client, session, lab, actor):
        if actor == "reviewer":
            user, secret = lab["reviewer"], TEST_USER_PASSWORD
        elif actor == "admin":
            user, secret = lab["admin"], lab["admin_password"]
        else:
            user = create_user(session, lab["tenant"], email="su@pc.example", roles=("superuser",))
            secret = TEST_USER_PASSWORD
        resp = _post(client, REOPEN, lab["report"], user, {"password": secret, "changelog": "Membrete incorrecto"})
        assert resp.status_code == 200, resp.text

        session.expire_all()
        assert session.get(Report, lab["report"].id).status == ReportStatus.DRAFT
        events = [
            e for e in _events(session, lab["order"].id, EventType.STATUS_CHANGED)
            if (e.event_metadata or {}).get("action") == "REPORT_REOPENED"
        ]
        assert len(events) == 1
        meta = events[0].event_metadata
        assert meta["reopen_kind"] == "UNSIGNED"
        assert meta["reason"] == "Membrete incorrecto"
        assert meta["report_version_id"] == str(lab["version"].id)
        assert meta["reopened_by"] == str(user.id)
        assert meta["from_status"] == "APPROVED" and meta["to_status"] == "DRAFT"
        assert meta["confirmation"] == "password"
        assert meta["reviews_reset_to_pending"] == 1

        audit = _audit(session, lab["report"].id, "REPORT.REOPEN")
        assert len(audit) == 1
        assert audit[0].new_values["password_confirmed"] is True
        assert audit[0].new_values["reopen_kind"] == "UNSIGNED"
        assert audit[0].new_values["report_version_id"] == str(lab["version"].id)

    @pytest.mark.parametrize(
        "body,code",
        [
            ({}, "password_confirmation_required"),
            ({"password": WRONG_PASSWORD}, "password_confirmation_invalid"),
            ({"password": OTHER_USER_PASSWORD}, "password_confirmation_invalid"),
        ],
    )
    def test_refused_confirmation_changes_nothing(self, client, session, lab, body, code):
        before = _Unchanged(session)
        resp = _post(client, REOPEN, lab["report"], lab["reviewer"], body)
        assert resp.status_code == 403, resp.text
        assert _code(resp) == code
        before.assert_intact()
        expected = 0 if code == "password_confirmation_required" else 1
        rows = _security_rows(session)
        assert len(rows) == expected
        if rows:
            assert rows[0].new_values["attempted_action"] == "REPORT.REOPEN"

    def test_admin_password_cannot_reopen_for_the_reviewer(self, client, session, lab):
        resp = _post(client, REOPEN, lab["report"], lab["reviewer"], {"password": lab["admin_password"]})
        assert resp.status_code == 403 and _code(resp) == "password_confirmation_invalid"

    @pytest.mark.parametrize("password", [TEST_USER_PASSWORD, WRONG_PASSWORD])
    def test_pathologist_is_refused_before_confirmation(self, client, session, lab, password):
        patho = create_user(session, lab["tenant"], email="patho@pc.example", roles=("pathologist",))
        before = _Unchanged(session)
        resp = _post(client, REOPEN, lab["report"], patho, {"password": password})
        assert resp.status_code == 403 and _code(resp) is None
        before.assert_intact()
        assert _security_rows(session) == []

    def test_signed_report_is_refused_before_confirmation(self, client, session, lab):
        version = session.get(ReportVersion, lab["version"].id)
        version.signed_by = lab["reviewer"].id
        version.signed_at = datetime.utcnow()
        session.add(version)
        session.commit()
        resp = _post(client, REOPEN, lab["report"], lab["admin"], {"password": WRONG_PASSWORD})
        assert resp.status_code == 409
        assert _security_rows(session) == []

    def test_admin_who_reopened_still_cannot_approve_or_sign(self, client, session, lab, stub_pdf_render):
        admin, secret = lab["admin"], lab["admin_password"]
        assert _post(client, REOPEN, lab["report"], admin, {"password": secret}).status_code == 200

        report = session.get(Report, lab["report"].id)
        report.status = ReportStatus.IN_REVIEW
        session.add(report)
        session.commit()
        assert _post(client, APPROVE, lab["report"], admin, {}).status_code == 403

        report.status = ReportStatus.APPROVED
        session.add(report)
        session.commit()
        for route in SIGNING_ROUTES:
            resp = _post(client, route, lab["report"], admin, {"password": secret})
            assert resp.status_code == 403, f"{route}: {resp.text}"
        session.expire_all()
        assert session.get(Report, lab["report"].id).status == ReportStatus.APPROVED

    def test_repeated_reopen_writes_one_event(self, client, session, lab):
        assert _post(client, REOPEN, lab["report"], lab["reviewer"], CONFIRM).status_code == 200
        second = _post(client, REOPEN, lab["report"], lab["reviewer"], CONFIRM)
        assert second.status_code == 400
        assert len(_audit(session, lab["report"].id, "REPORT.REOPEN")) == 1


# ---------------------------------------------------------------------------
# Concurrency: exactly one transition and one event
# ---------------------------------------------------------------------------


class TestConcurrentTransitions:
    def test_concurrent_reopens_produce_one_transition(self, fresh_client, session, lab):
        headers = auth_headers(lab["reviewer"])
        url = REOPEN.format(lab["report"].id)
        results = run_parallel([lambda c: c.post(url, json=CONFIRM, headers=headers) for _ in range(4)])
        codes = sorted(getattr(r, "status_code", r) for r in results)
        assert codes == [200, 400, 400, 400], [getattr(r, "text", r) for r in results]
        assert len(_audit(session, lab["report"].id, "REPORT.REOPEN")) == 1
        reopen_events = [
            e for e in _events(session, lab["order"].id, EventType.STATUS_CHANGED)
            if (e.event_metadata or {}).get("action") == "REPORT_REOPENED"
        ]
        assert len(reopen_events) == 1

    @pytest.mark.parametrize("route", SIGNING_ROUTES)
    def test_concurrent_signatures_publish_once(self, fresh_client, session, lab, stub_pdf_render, route):
        headers = auth_headers(lab["reviewer"])
        url = route.format(lab["report"].id)
        results = run_parallel([lambda c: c.post(url, json=CONFIRM, headers=headers) for _ in range(3)])
        codes = [getattr(r, "status_code", r) for r in results]
        assert codes.count(200) == 1, [getattr(r, "text", r) for r in results]
        assert all(c in (200, 400, 409) for c in codes), codes
        assert len(_events(session, lab["order"].id, EventType.REPORT_PUBLISHED)) == 1
        session.expire_all()
        assert session.get(Report, lab["report"].id).status == ReportStatus.PUBLISHED


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------


class TestThrottle:
    def test_correct_password_is_refused_inside_the_window(self, client, session, lab, monkeypatch, stub_pdf_render):
        monkeypatch.setattr(settings, "password_confirmation_max_failures", 3)
        for _ in range(3):
            resp = _post(client, REOPEN, lab["report"], lab["reviewer"], {"password": WRONG_PASSWORD})
            assert _code(resp) == "password_confirmation_invalid"

        before = _Unchanged(session)
        for route in (REOPEN, SIGN_AND_PUBLISH):
            resp = _post(client, route, lab["report"], lab["reviewer"], CONFIRM)
            assert resp.status_code == 429, resp.text
            assert _code(resp) == "password_confirmation_throttled"
            assert 0 < resp.json()["detail"]["retry_after_seconds"] <= settings.password_confirmation_window_seconds + 1
        before.assert_intact()
        assert stub_pdf_render.call_count == 0
        # Throttled attempts are not counted as further failures.
        assert len(_security_rows(session)) == 3

    def test_the_throttle_is_per_actor(self, client, session, lab, monkeypatch):
        monkeypatch.setattr(settings, "password_confirmation_max_failures", 2)
        for _ in range(2):
            _post(client, REOPEN, lab["report"], lab["reviewer"], {"password": WRONG_PASSWORD})
        assert _post(client, REOPEN, lab["report"], lab["reviewer"], CONFIRM).status_code == 429
        resp = _post(client, REOPEN, lab["report"], lab["admin"], {"password": lab["admin_password"]})
        assert resp.status_code == 200, resp.text

    def test_failures_outside_the_window_do_not_count(self, client, session, lab, monkeypatch):
        monkeypatch.setattr(settings, "password_confirmation_max_failures", 2)
        old = datetime.utcnow() - timedelta(seconds=settings.password_confirmation_window_seconds + 60)
        for _ in range(2):
            session.add(AuditLog(
                tenant_id=lab["tenant"].id, branch_id=lab["branch"].id,
                actor_user_id=lab["reviewer"].id, action=PASSWORD_CONFIRMATION_FAILED_ACTION,
                entity_type="report", entity_id=lab["report"].id, created_at=old,
                new_values={"attempted_action": "REPORT.REOPEN", "failure": "invalid_password"},
            ))
        session.commit()
        assert _post(client, REOPEN, lab["report"], lab["reviewer"], CONFIRM).status_code == 200


# ---------------------------------------------------------------------------
# Credential hygiene
# ---------------------------------------------------------------------------


class TestTheCredentialIsNeverKept:
    def test_no_sink_contains_a_submitted_password(self, client, session, lab, stub_pdf_render, caplog):
        caplog.set_level(logging.DEBUG)
        responses = [
            _post(client, REOPEN, lab["report"], lab["reviewer"], {"password": WRONG_PASSWORD}),
            _post(client, SIGN_AND_PUBLISH, lab["report"], lab["reviewer"], {"password": WRONG_PASSWORD}),
            _post(client, SIGN_AND_PUBLISH, lab["report"], lab["reviewer"], {**CONFIRM, "changelog": "ok"}),
        ]
        assert [r.status_code for r in responses] == [403, 403, 200]

        session.expire_all()
        stored = "\n".join(
            "\n".join(_rows(session, table))
            for table in (*_TABLES, "audit_log")
        )
        objects = b"\n".join(FakeS3Service.store.values()).decode("utf-8", "replace")
        logs = rendered_log_text(caplog)
        bodies = "\n".join(r.text for r in responses)
        for secret in (WRONG_PASSWORD, TEST_USER_PASSWORD):
            for where, haystack in (("database", stored), ("object storage", objects), ("logs", logs), ("responses", bodies)):
                assert secret not in haystack, f"a submitted password reached the {where}"

    @pytest.mark.parametrize(
        "body",
        [
            {"password": {"value": WRONG_PASSWORD}},
            {"password": [WRONG_PASSWORD]},
            {"changelog": 7, "password": {"nested": WRONG_PASSWORD}},
        ],
    )
    def test_validation_errors_do_not_echo_the_password(self, client, session, lab, body):
        resp = _post(client, SIGN_AND_PUBLISH, lab["report"], lab["reviewer"], body)
        assert resp.status_code == 422
        assert WRONG_PASSWORD not in resp.text
        locs = [tuple(e["loc"]) for e in resp.json()["detail"]]
        assert any("password" in loc for loc in locs)  # the error itself is still reported

    def test_whole_body_errors_redact_credential_keys(self, client):
        resp = client.post("/api/v1/auth/login", json=[{"password": WRONG_PASSWORD}])
        assert resp.status_code == 422
        assert WRONG_PASSWORD not in resp.text

    def test_request_models_never_serialize_the_password(self):
        for model in (ReportSignRequest, ReportReopenRequest):
            body = model(changelog="x", password=WRONG_PASSWORD)
            assert body.password == WRONG_PASSWORD
            assert WRONG_PASSWORD not in repr(body)
            assert WRONG_PASSWORD not in json.dumps(body.model_dump())
            assert WRONG_PASSWORD not in body.model_dump_json()


# ---------------------------------------------------------------------------
# Signed versions are immutable by themselves
# ---------------------------------------------------------------------------


class TestSignedVersionArtifacts:
    """The state an amendment creates: the report is editable again (DRAFT)
    while an older version is signed. Report status no longer protects that
    version, so the version's own signature evidence must."""

    @pytest.fixture
    def amended_shape(self, session, lab):
        signed = session.get(ReportVersion, lab["version"].id)
        signed.signed_by = lab["reviewer"].id
        signed.signed_at = datetime.utcnow()
        signed.is_current = False
        session.add(signed)
        draft = ReportVersion(report_id=lab["report"].id, version_no=2, is_current=True, schema_version=2)
        session.add(draft)
        report = session.get(Report, lab["report"].id)
        report.status = ReportStatus.DRAFT
        session.add(report)
        session.commit()
        return signed

    def test_a_signed_version_pdf_is_not_regenerated(self, client, session, lab, stub_pdf_render, amended_shape):
        editor = create_user(session, lab["tenant"], email="editor@pc.example", roles=("pathologist",))
        before = _Unchanged(session)
        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/versions/1/generate-pdf", headers=auth_headers(editor)
        )
        assert resp.status_code == 409, resp.text
        assert stub_pdf_render.call_count == 0
        before.assert_intact()

    def test_a_signed_version_pdf_is_not_replaced_by_upload(self, client, session, lab, amended_shape):
        editor = create_user(session, lab["tenant"], email="editor@pc.example", roles=("pathologist",))
        before = _Unchanged(session)
        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/versions/1/pdf",
            files={"file": ("x.pdf", make_pdf_bytes(1), "application/pdf")},
            headers=auth_headers(editor),
        )
        assert resp.status_code == 409, resp.text
        before.assert_intact()

    def test_the_latest_upload_route_refuses_a_signed_latest_version(self, client, session, lab):
        version = session.get(ReportVersion, lab["version"].id)
        version.signed_by = lab["reviewer"].id
        version.signed_at = datetime.utcnow()
        session.add(version)
        report = session.get(Report, lab["report"].id)
        report.status = ReportStatus.DRAFT
        session.add(report)
        session.commit()
        editor = create_user(session, lab["tenant"], email="editor@pc.example", roles=("pathologist",))
        before = _Unchanged(session)
        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/pdf",
            files={"file": ("x.pdf", make_pdf_bytes(1), "application/pdf")},
            headers=auth_headers(editor),
        )
        assert resp.status_code == 409, resp.text
        before.assert_intact()
