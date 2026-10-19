"""Céluma 1.4 (#13), independent review P1s: every report writer follows one
lock protocol — order lock, then version row lock, then fresh lifecycle /
amendment / signature / publish-claim checks, then writes — so a request that
loses a race is refused instead of writing over the state that replaced it.

Each race is driven in BOTH legal orders with real concurrent HTTP requests on
real Postgres. The first request is held at a gate INSIDE its critical section;
the second is started and proven to be waiting on a row lock
(`pg_stat_activity`) — or, while the first holds only the publish claim of the
lock-free rendering phase, proven to finish while the first is still held —
and only then is the first released. Gates always open in a `finally` and every
wait has a timeout, so a regression fails instead of hanging.

The independent reproducers paused a request after its checks and then ran the
opponent synchronously on the test thread. With correct locking that opponent
waits for the paused request, which can never resume: a test-induced deadlock.
These barriers keep the reproducers' invariants and avoid that wait.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import text
from sqlmodel import select

import app.api.v1.reports as routes
from app.models.enums import EventType, ReportStatus, ReviewStatus
from app.models.events import OrderEvent
from app.models.laboratory import Order
from app.models.report import Report, ReportVersion
from app.models.report_amendment import ReportAmendment
from app.models.report_review import ReportReview
from app.models.storage import StorageObject
from app.services import report_amendments
from app.services.report_pdf_generation import ReportPdfGenerationService
from app.services.report_publishing import PUBLISH_IN_PROGRESS_CODE

from .conftest import FakeS3Service, make_pdf_bytes
from .factories import auth_headers
from .numbering_support import new_client
from .test_report_amendments import (  # noqa: F401 - `lab` and `parallel` are fixtures
    _approve,
    _audit,
    _cancel,
    _drive_to_approved,
    _events,
    _fingerprint,
    _open,
    _save,
    _sign,
    _submit,
    _url,
    _version,
    CONFIRM,
    lab,
    parallel,
)

OFFICIAL_BYTES = make_pdf_bytes(1)  # what the `lab` fixture's renderer stub returns
MANUAL_BYTES = make_pdf_bytes(1) + b"\n% manual upload racing a signature\n"
UPLOAD_ROUTES = ["/versions/3/pdf", "/pdf"]


# ---------------------------------------------------------------------------
# Barriers
# ---------------------------------------------------------------------------


class Gate:
    """Holds one request inside its critical section until released."""

    def __init__(self):
        self.reached = Event()
        self.released = Event()

    def hold(self):
        self.reached.set()
        if not self.released.wait(30):
            raise AssertionError("gate was never released")


def _hold_after(monkeypatch, owner, name, gate, when):
    """Hold the request right after `owner.name(...)` returns, when `when(**kwargs)`."""
    original = getattr(owner, name)

    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        if when(**kwargs):
            gate.hold()
        return result

    monkeypatch.setattr(owner, name, wrapper)


def _hold_password_check(monkeypatch, gate, action):
    """Every password-confirmed route checks the password inside its locked
    section, after its lifecycle checks and before its first write."""
    _hold_after(monkeypatch, routes, "require_password_confirmation", gate,
                lambda **kw: kw.get("attempted_action") == action)


def _hold_manual_write(monkeypatch, gate):
    """Hold a manual upload just before its object write (inside its locks)."""
    original = FakeS3Service.upload_bytes

    def upload_bytes(self, data, key, content_type=None, acl=None):
        if data == MANUAL_BYTES:
            gate.hold()
        return original(self, data, key, content_type=content_type, acl=acl)

    monkeypatch.setattr(FakeS3Service, "upload_bytes", upload_bytes)


def _hold_render(monkeypatch, gate):
    """Hold the official render — signing holds only its publish claim here."""
    original = ReportPdfGenerationService._render_pdf  # the `stub_pdf_render` controller

    def render(self, report, version):
        gate.hold()
        return original(report, version)

    monkeypatch.setattr(ReportPdfGenerationService, "_render_pdf", render)


def _wait_until_blocked_on_a_lock(engine, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            waiting = conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )).scalar()
        if waiting:
            return
        time.sleep(0.02)
    raise AssertionError("the second request never waited on a row lock")


def _loaded(lab):
    """Load every fixture object on the test thread: worker threads must never
    lazy-load through the shared test session."""
    for key in ("tenant", "branch", "order", "report", "author", "reviewer", "admin", "review"):
        assert lab[key].id is not None
    return lab


def _race(engine, lab, first, gate, second):
    """`first` reaches `gate` inside its locks; `second` must then wait on a row
    lock; `first` is released. Returns (first response, second response)."""
    _loaded(lab)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            held = pool.submit(first, new_client())
            assert gate.reached.wait(15), "the first request never reached its gate"
            other = pool.submit(second, new_client())
            try:
                _wait_until_blocked_on_a_lock(engine)
            except AssertionError:
                if other.done():
                    raise AssertionError(f"the second request finished first: {other.result().text}") from None
                raise
            assert not other.done(), "the second request did not wait for the first"
        finally:
            gate.released.set()
        return held.result(timeout=60), other.result(timeout=60)


# ---------------------------------------------------------------------------
# Evidence helpers
# ---------------------------------------------------------------------------


def _row_counts(engine):
    with engine.connect() as conn:
        tables = conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
        )).scalars().all()
        return {t: conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar() for t in tables}


def _new_rows(engine, before):
    after = _row_counts(engine)
    return {t: after[t] - before.get(t, 0) for t in after if after[t] != before.get(t, 0)}


#: What a successful cancellation inserts (it notifies nobody): one audit row,
#: one timeline row. A stale writer that lost to it must add nothing.
CANCELLATION_ROWS = {"audit_log": 1, "order_event": 1}


def _versions(session, lab):
    session.expire_all()
    return session.exec(
        select(ReportVersion).where(ReportVersion.report_id == lab["report"].id)
    ).all()


def _current(session, lab):
    return [v.version_no for v in _versions(session, lab) if v.is_current]


def _report(session, lab):
    session.expire_all()
    return session.get(Report, lab["report"].id)


def _amendment(session, amendment_id):
    session.expire_all()
    return session.get(ReportAmendment, uuid.UUID(amendment_id))


def _review_status(session, lab):
    session.expire_all()
    return session.get(ReportReview, lab["review"].id).status


def _order_status(session, lab):
    session.expire_all()
    return session.get(Order, lab["order"].id).status


def _version_created_events(session, lab, version_no):
    session.expire_all()
    return [
        e for e in session.exec(select(OrderEvent).where(
            OrderEvent.order_id == lab["order"].id,
            OrderEvent.event_type == EventType.REPORT_VERSION_CREATED,
        )).all()
        if (e.event_metadata or {}).get("version_no") == version_no
    ]


def _upload(route):
    def post(client, lab):
        return client.post(
            _url(lab["report"], route),
            files={"file": ("manual.pdf", MANUAL_BYTES, "application/pdf")},
            headers=auth_headers(lab["author"]),
        )
    return post


def _assert_official_signed_v3(session, lab, client):
    """v3 is signed, READY, and points at the official render — hash, storage
    row and stored bytes agree — which is also what delivery serves."""
    v3 = _version(session, lab["report"], 3)
    assert v3.signed_at is not None and v3.signed_by == lab["reviewer"].id
    assert v3.pdf_generation_status == "READY"
    official = session.get(StorageObject, v3.pdf_storage_id)
    stored = FakeS3Service.store[official.object_key]
    assert "/official/" in official.object_key
    assert stored == OFFICIAL_BYTES and stored != MANUAL_BYTES
    assert hashlib.sha256(stored).hexdigest() == v3.pdf_sha256 == official.sha256_hex
    delivered = client.get(_url(lab["report"], "/pdf"), headers=auth_headers(lab["author"]))
    assert delivered.status_code == 200, delivered.text
    assert delivered.json()["pdf_key"] == official.object_key
    return v3


def _assert_republished_once(session, lab, amendment_id, v3):
    amendment = _amendment(session, amendment_id)
    assert amendment.state == "PUBLISHED" and amendment.published_version_id == v3.id
    assert _report(session, lab).status == ReportStatus.PUBLISHED
    events = _events(session, lab["order"].id, "REPORT_REPUBLISHED")
    assert len(events) == 1
    assert events[0].event_metadata["report_version_id"] == str(v3.id)
    assert events[0].event_metadata["amendment_id"] == amendment_id
    audit = [a for a in _audit(session, lab["report"].id, "REPORT.SIGN_AND_PUBLISH")
             if (a.new_values or {}).get("amendment_id") == amendment_id]
    assert len(audit) == 1


# ---------------------------------------------------------------------------
# P1 #1 — saving vs cancelling an amendment
# ---------------------------------------------------------------------------


class TestSaveVersusCancellation:
    def test_a_save_holding_the_lock_lands_first_then_cancellation_restores_one_current_version(
        self, parallel, engine, session, lab, monkeypatch,
    ):
        order_status_before = _order_status(session, lab)
        opened = _open(new_client(), lab)
        assert opened.status_code == 200, opened.text
        amendment_id = opened.json()["amendment"]["id"]

        gate = Gate()
        _hold_after(monkeypatch, report_amendments, "carry_forward_identification", gate,
                    lambda **kw: bool(kw.get("in_amendment")))
        saved, cancelled = _race(engine, lab, lambda c: _save(c, lab), gate,
                                 lambda c: _cancel(c, lab, amendment_id))

        assert saved.status_code == 200, saved.text
        assert cancelled.status_code == 200, cancelled.text
        assert sorted(v.version_no for v in _versions(session, lab)) == [1, 2, 3]
        assert _current(session, lab) == [1]
        assert _report(session, lab).status == ReportStatus.PUBLISHED
        assert _amendment(session, amendment_id).state == "CANCELLED"
        assert str(_version(session, lab["report"], 3).amendment_id) == amendment_id
        assert _review_status(session, lab) == ReviewStatus.APPROVED  # restored
        assert _order_status(session, lab) == order_status_before
        # The timeline tells what happened: the save inside the amendment, then
        # exactly one cancellation.
        created = _version_created_events(session, lab, 3)
        assert len(created) == 1 and created[0].event_metadata["amendment_id"] == amendment_id
        assert len(_events(session, lab["order"].id, "REPORT_AMENDMENT_CANCELLED")) == 1
        assert len(_audit(session, lab["report"].id, "REPORT.AMENDMENT_CANCEL")) == 1

    def test_a_cancellation_holding_the_lock_wins_and_the_stale_save_is_refused_without_side_effects(
        self, parallel, engine, session, lab, monkeypatch,
    ):
        opened = _open(new_client(), lab)
        assert opened.status_code == 200, opened.text
        amendment_id = opened.json()["amendment"]["id"]
        rows_before, store_before = _row_counts(engine), dict(FakeS3Service.store)

        gate = Gate()
        _hold_password_check(monkeypatch, gate, routes.AUDIT_AMENDMENT_CANCEL)
        cancelled, saved = _race(engine, lab, lambda c: _cancel(c, lab, amendment_id), gate,
                                 lambda c: _save(c, lab))

        assert cancelled.status_code == 200, cancelled.text
        assert saved.status_code == 409, saved.text
        assert sorted(v.version_no for v in _versions(session, lab)) == [1, 2]
        assert _current(session, lab) == [1]
        assert _report(session, lab).status == ReportStatus.PUBLISHED
        assert _amendment(session, amendment_id).state == "CANCELLED"
        assert _review_status(session, lab) == ReviewStatus.APPROVED
        # No version, storage object, object write, accounting row or event
        # from the refused save: only the cancellation's own rows.
        assert _new_rows(engine, rows_before) == CANCELLATION_ROWS
        assert FakeS3Service.store == store_before
        assert _version_created_events(session, lab, 3) == []


# ---------------------------------------------------------------------------
# P1 #2 — manual PDF uploads vs signing (both upload routes)
# ---------------------------------------------------------------------------


class TestUploadVersusSigning:
    @pytest.mark.parametrize("route", UPLOAD_ROUTES)
    def test_an_upload_holding_the_lock_lands_first_then_signing_publishes_the_official_pdf(
        self, parallel, engine, session, lab, monkeypatch, route,
    ):
        client = new_client()
        amendment_id = _open(client, lab).json()["amendment"]["id"]
        _drive_to_approved(client, lab)

        gate = Gate()
        _hold_manual_write(monkeypatch, gate)
        uploaded, signed = _race(engine, lab, lambda c: _upload(route)(c, lab), gate,
                                 lambda c: _sign(c, lab))

        assert uploaded.status_code == 200, uploaded.text
        assert signed.status_code == 200, signed.text
        v3 = _assert_official_signed_v3(session, lab, client)
        # The upload really landed first, and is now an unreferenced object.
        manual = session.get(StorageObject, uuid.UUID(uploaded.json()["pdf_storage_id"]))
        assert FakeS3Service.store[manual.object_key] == MANUAL_BYTES
        assert manual.id != v3.pdf_storage_id
        assert not session.exec(select(ReportVersion).where(ReportVersion.pdf_storage_id == manual.id)).all()
        _assert_republished_once(session, lab, amendment_id, v3)

    @pytest.mark.parametrize("window", ["while_claiming", "while_rendering"])
    @pytest.mark.parametrize("route", UPLOAD_ROUTES)
    def test_signing_wins_and_the_stale_upload_is_refused_without_side_effects(
        self, parallel, engine, session, lab, monkeypatch, route, window,
    ):
        client = new_client()
        amendment_id = _open(client, lab).json()["amendment"]["id"]
        _drive_to_approved(client, lab)
        objects_before = {o.id for o in session.exec(select(StorageObject)).all()}

        claim_gate, render_gate = Gate(), Gate()
        _hold_password_check(monkeypatch, claim_gate, "REPORT.SIGN_AND_PUBLISH")
        _hold_render(monkeypatch, render_gate)
        _loaded(lab)
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                signing = pool.submit(_sign, new_client(), lab)
                assert claim_gate.reached.wait(15)
                if window == "while_claiming":
                    # Signing holds the order and version locks: the upload waits...
                    uploading = pool.submit(_upload(route), new_client(), lab)
                    _wait_until_blocked_on_a_lock(engine)
                    assert not uploading.done()
                claim_gate.released.set()
                # ...and gets them only once the claim is committed. Signing is now
                # rendering, holding nothing but its claim.
                assert render_gate.reached.wait(15)
                if window == "while_rendering":
                    uploading = pool.submit(_upload(route), new_client(), lab)
                uploaded = uploading.result(timeout=15)
                assert not signing.done()
            finally:
                claim_gate.released.set()
                render_gate.released.set()
            signed = signing.result(timeout=60)

        assert uploaded.status_code == 409, uploaded.text
        assert uploaded.json()["detail"]["code"] == PUBLISH_IN_PROGRESS_CODE
        assert signed.status_code == 200, signed.text
        v3 = _assert_official_signed_v3(session, lab, client)
        # The refused upload wrote nothing: the only new object is the official PDF.
        session.expire_all()
        new_objects = [o for o in session.exec(select(StorageObject)).all() if o.id not in objects_before]
        assert [o.id for o in new_objects] == [v3.pdf_storage_id]
        assert MANUAL_BYTES not in FakeS3Service.store.values()
        _assert_republished_once(session, lab, amendment_id, v3)


# ---------------------------------------------------------------------------
# Signing vs cancelling (the independent control, both orders)
# ---------------------------------------------------------------------------


class TestSigningVersusCancellation:
    def test_a_cancellation_holding_the_lock_wins_and_the_stale_signature_is_refused(
        self, parallel, engine, session, lab, monkeypatch,
    ):
        client = new_client()
        amendment_id = _open(client, lab).json()["amendment"]["id"]
        _drive_to_approved(client, lab)
        rows_before, store_before = _row_counts(engine), dict(FakeS3Service.store)

        gate = Gate()
        _hold_password_check(monkeypatch, gate, routes.AUDIT_AMENDMENT_CANCEL)
        cancelled, signed = _race(engine, lab, lambda c: _cancel(c, lab, amendment_id), gate,
                                  lambda c: _sign(c, lab))

        assert cancelled.status_code == 200, cancelled.text
        assert signed.status_code == 400, signed.text  # no longer APPROVED
        v3 = _version(session, lab["report"], 3)
        assert v3.signed_at is None and v3.publish_started_at is None
        assert _current(session, lab) == [1]
        assert _amendment(session, amendment_id).state == "CANCELLED"
        assert _new_rows(engine, rows_before) == CANCELLATION_ROWS
        assert FakeS3Service.store == store_before
        assert _events(session, lab["order"].id, "REPORT_REPUBLISHED") == []

    def test_a_signature_holding_the_lock_wins_and_the_cancellation_is_refused(
        self, parallel, engine, session, lab, monkeypatch,
    ):
        client = new_client()
        amendment_id = _open(client, lab).json()["amendment"]["id"]
        _drive_to_approved(client, lab)

        claim_gate, render_gate = Gate(), Gate()
        _hold_password_check(monkeypatch, claim_gate, "REPORT.SIGN_AND_PUBLISH")
        _hold_render(monkeypatch, render_gate)
        _loaded(lab)
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                signing = pool.submit(_sign, new_client(), lab)
                assert claim_gate.reached.wait(15)
                cancelling = pool.submit(_cancel, new_client(), lab, amendment_id)
                _wait_until_blocked_on_a_lock(engine)
                assert not cancelling.done()
                claim_gate.released.set()
                assert render_gate.reached.wait(15)
                cancelled = cancelling.result(timeout=15)
            finally:
                claim_gate.released.set()
                render_gate.released.set()
            signed = signing.result(timeout=60)

        assert cancelled.status_code == 409, cancelled.text
        assert cancelled.json()["detail"]["code"] == report_amendments.PUBLISH_IN_PROGRESS_CODE
        assert signed.status_code == 200, signed.text
        v3 = _assert_official_signed_v3(session, lab, client)
        _assert_republished_once(session, lab, amendment_id, v3)
        assert _events(session, lab["order"].id, "REPORT_AMENDMENT_CANCELLED") == []
        assert _audit(session, lab["report"].id, "REPORT.AMENDMENT_CANCEL") == []


# ---------------------------------------------------------------------------
# Sibling writers against the same lifecycle
# ---------------------------------------------------------------------------


def _presentation(client, lab):
    return client.patch(_url(lab["report"], "/presentation"), json={"show_signature_section": False},
                        headers=auth_headers(lab["reviewer"]))


def _request_changes(client, lab):
    return client.post(_url(lab["report"], "/request-changes"), json={"comment": "Revisar la descripción"},
                       headers=auth_headers(lab["reviewer"]))


def _worklist_decision(client, lab):
    return client.post(f"/api/v1/report-reviews/{lab['review'].id}/decision", json={"decision": "approved"},
                       headers=auth_headers(lab["reviewer"]))


#: writer -> (amendment state to reach first, status of the stale writer).
#: The approval routes re-run the full reviewer contract on the fresh state,
#: where the cancellation has restored the reviewer's decision: no PENDING
#: review is left to decide, so they refuse with that contract's 403.
SIBLINGS = {
    "presentation": (_presentation, "draft", 409),
    "approve": (_approve, "in_review", 403),
    "request_changes": (_request_changes, "in_review", 403),
    "worklist_decision": (_worklist_decision, "in_review", 409),
}


class TestSiblingWriters:
    @pytest.mark.parametrize("writer", sorted(SIBLINGS))
    def test_a_writer_that_lost_to_a_cancellation_is_refused_without_side_effects(
        self, parallel, engine, session, lab, monkeypatch, writer,
    ):
        write, stage, refused = SIBLINGS[writer]
        client = new_client()
        amendment_id = _open(client, lab).json()["amendment"]["id"]
        if stage == "in_review":
            assert _save(client, lab).status_code == 200
            assert _submit(client, lab).status_code == 200
        rows_before, store_before = _row_counts(engine), dict(FakeS3Service.store)

        gate = Gate()
        _hold_password_check(monkeypatch, gate, routes.AUDIT_AMENDMENT_CANCEL)
        cancelled, stale = _race(engine, lab, lambda c: _cancel(c, lab, amendment_id), gate,
                                 lambda c: write(c, lab))

        assert cancelled.status_code == 200, cancelled.text
        assert stale.status_code == refused, stale.text
        assert _report(session, lab).status == ReportStatus.PUBLISHED
        assert _current(session, lab) == [1]
        assert _review_status(session, lab) == ReviewStatus.APPROVED  # the restored decision stands
        assert _new_rows(engine, rows_before) == CANCELLATION_ROWS
        assert FakeS3Service.store == store_before

    def test_writers_that_would_change_what_is_being_signed_are_refused_during_the_signature(
        self, parallel, engine, session, lab, monkeypatch,
    ):
        """Signing renders holding only its publish claim; reopening, a plain PDF
        generation and a review decision are refused for its whole duration."""
        client = new_client()
        amendment_id = _open(client, lab).json()["amendment"]["id"]
        _drive_to_approved(client, lab)

        render_gate = Gate()
        _hold_render(monkeypatch, render_gate)
        _loaded(lab)
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                signing = pool.submit(_sign, new_client(), lab)
                assert render_gate.reached.wait(15)
                other = new_client()
                reopened = other.post(_url(lab["report"], "/reopen"), json={"changelog": "Corregir", **CONFIRM},
                                      headers=auth_headers(lab["reviewer"]))
                generated = other.post(_url(lab["report"], "/versions/3/generate-pdf"),
                                       headers=auth_headers(lab["author"]))
                decided = _worklist_decision(other, lab)
                assert not signing.done()
            finally:
                render_gate.released.set()
            signed = signing.result(timeout=60)

        for refused in (reopened, generated, decided):
            assert refused.status_code == 409, refused.text
            assert refused.json()["detail"]["code"] == PUBLISH_IN_PROGRESS_CODE
        assert signed.status_code == 200, signed.text
        v3 = _assert_official_signed_v3(session, lab, client)
        _assert_republished_once(session, lab, amendment_id, v3)
        assert _events(session, lab["order"].id, "REPORT_REOPENED") == []
        assert _audit(session, lab["report"].id, "REPORT.REOPEN") == []

    def test_a_retraction_that_lost_to_an_amendment_opening_is_refused(
        self, parallel, engine, session, lab, monkeypatch,
    ):
        gate = Gate()
        _hold_password_check(monkeypatch, gate, routes.AUDIT_REOPEN_FOR_AMENDMENT)
        opened, retracted = _race(
            engine, lab, lambda c: _open(c, lab), gate,
            lambda c: c.post(_url(lab["report"], "/retract"), json={"changelog": "Retiro"},
                             headers=auth_headers(lab["author"])),
        )

        assert opened.status_code == 200, opened.text
        assert retracted.status_code == 400, retracted.text
        assert _report(session, lab).status == ReportStatus.DRAFT
        assert _amendment(session, opened.json()["amendment"]["id"]).state == "OPEN"
        assert _audit(session, lab["report"].id, "REPORT.RETRACT") == []
