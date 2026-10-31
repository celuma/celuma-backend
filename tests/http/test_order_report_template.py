"""The order's report template (Céluma 1.4.1 hotfix), over real HTTP + PostgreSQL.

Contract: `celuma-engineering/specs/laboratory/order-report-template.md`; decision record ADR 0022.

Precedence for a NEW report: the order's explicit choice > the sample-type default when the order's samples
agree on one > the study-type default (optional) > nothing (no laboratory-wide clinical-template fallback).
Samples that disagree are AMBIGUOUS and need an explicit choice. The choice is persisted by unified creation
and by the order edit, consumed by `GET /laboratory/orders/{id}/report-defaults` and enforced by
`create_report`, which still requires `template_id` + `template_hash`. Existing reports keep their snapshot.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.models.enums import EventType, OrderStatus
from app.models.laboratory import Order
from app.models.report import Report, ReportTemplate, ReportVersion
from app.models.sample_type import SampleType
from app.models.storage import StorageObject
from app.schemas.laboratory import OrderUnifiedCreate
from app.services import idempotency
from app.services.report_template_hash import hash_clinical_template_block

from .conftest import FakeS3Service
from .factories import auth_headers, create_default_letterhead, create_letterhead, create_letterhead_version, create_user
from .management_support import LAB, add_event, count, make_report, register, set_status
from .numbering_support import ORDERS, SAMPLES, UNIFIED, Lab, freeze, ledger_rows

TEMPLATES = "/api/v1/reports/templates/"
SAMPLE_TYPES = "/api/v1/sample-types/"
REPORTS = "/api/v1/reports/"


def structure(section: str) -> dict:
    """A minimal valid clinical structure whose only section key names the template (so snapshots differ)."""
    return {
        "base": {"diagnosis": {"label": "Diagnóstico", "type": "text", "is_visible": True}},
        "sections": {section: {"label": section.title(), "type": "richtext", "is_visible": True, "content": ""}},
        "base_order": ["diagnosis"],
        "section_order": [section],
    }


class TemplateLab(Lab):
    """A `Lab` (superuser, study type, sample types) plus clinical templates and a default letterhead."""

    def __init__(self, session, name="Alfa", **kw):
        super().__init__(session, name, **kw)
        self.letterhead, self.letterhead_version = create_default_letterhead(session, self.tenant)
        self.templates: dict = {}

    def template(self, key: str, *, active=True) -> ReportTemplate:
        row = ReportTemplate(tenant_id=self.tenant.id, name=f"Plantilla {key}", template_json=structure(key),
                             is_active=active)
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        self.templates[key] = row
        return row

    def link_sample_type(self, code: str, template) -> SampleType:
        row = self.sample_types[code]
        row.default_report_template_id = template.id if template else None
        self.session.add(row)
        self.session.commit()
        return row

    def link_study_type(self, template, study_type=None) -> None:
        row = study_type or self.study_type
        row.default_report_template_id = template.id if template else None
        self.session.add(row)
        self.session.commit()


@pytest.fixture
def lab(session, monkeypatch):
    freeze(monkeypatch)
    return TemplateLab(session, "Alfa", sample_types=("BX", "TJ", "CT"))


@pytest.fixture
def other(session):
    return TemplateLab(session, "Beta")


def detail(client, lab, order_id) -> dict:
    res = client.get(f"{LAB}/orders/{order_id}", headers=lab.headers)
    assert res.status_code == 200, res.text
    return res.json()


def selection(client, lab, order_id) -> dict:
    return detail(client, lab, order_id)["report_template"]


def defaults(client, lab, order_id, headers=None):
    return client.get(f"{LAB}/orders/{order_id}/report-defaults", headers=headers or lab.headers)


def patch(client, lab, order_id, body, headers=None):
    return client.patch(f"{LAB}/orders/{order_id}", json=body, headers=headers or lab.headers)


def create_report_for(client, lab, order_id, template, *, template_hash=..., headers=None, **extra):
    """The editor's create call: `template_id` + the `template_hash` read with the structure."""
    lab.session.expire_all()
    current = lab.session.get(ReportTemplate, template.id)
    payload = {
        "tenant_id": str(lab.tenant.id), "branch_id": str(lab.branch.id), "order_id": str(order_id),
        "title": "Reporte", "template": current.template_json,
        "report": {"base": {}, "sections": {}},
        "template_id": str(template.id),
        "template_hash": hash_clinical_template_block(current.template_json) if template_hash is ... else template_hash,
        **extra,
    }
    return client.post(REPORTS, json=payload, headers=headers or lab.headers)


def frozen_template(session, report_id) -> dict:
    version = session.exec(select(ReportVersion).where(
        ReportVersion.report_id == uuid.UUID(str(report_id)), ReportVersion.is_current == True)).first()  # noqa: E712
    storage = session.get(StorageObject, version.json_storage_id)
    return json.loads(FakeS3Service.store[storage.object_key].decode("utf-8"))["rendering_snapshot"]["template"]


def reports_of(session, order_id) -> int:
    session.expire_all()
    return len(session.exec(select(Report).where(Report.order_id == uuid.UUID(str(order_id)))).all())


def code_of(res) -> str | None:
    detail_ = res.json().get("detail")
    return detail_.get("code") if isinstance(detail_, dict) else None


# ---------------------------------------------------------------------------------------- the catalog link


class TestSampleTypeTemplateLink:
    def create(self, client, lab, **body):
        return client.post(SAMPLE_TYPES, json={"code": "NEW", "name": "Nueva", **body}, headers=lab.headers)

    def test_link_is_created_changed_and_cleared_with_audit(self, client, session, lab):
        a, b = lab.template("a"), lab.template("b")
        created = self.create(client, lab, default_report_template_id=str(a.id))
        assert created.status_code == 200, created.text
        st_id = created.json()["id"]
        assert created.json()["default_template"] == {"id": str(a.id), "name": "Plantilla a"}

        changed = client.put(f"{SAMPLE_TYPES}{st_id}", json={"default_report_template_id": str(b.id)}, headers=lab.headers)
        assert changed.json()["default_report_template_id"] == str(b.id)
        # Omitted = unchanged.
        renamed = client.put(f"{SAMPLE_TYPES}{st_id}", json={"name": "Renombrada"}, headers=lab.headers)
        assert renamed.json()["default_report_template_id"] == str(b.id)
        # Explicit null clears (1.4.1; before, null was silently "unchanged" and the UI could not clear it).
        cleared = client.put(f"{SAMPLE_TYPES}{st_id}", json={"default_report_template_id": None}, headers=lab.headers)
        assert cleared.json()["default_report_template_id"] is None and cleared.json()["default_template"] is None
        # Blank clears too (study-type compatible spelling).
        client.put(f"{SAMPLE_TYPES}{st_id}", json={"default_report_template_id": str(a.id)}, headers=lab.headers)
        blank = client.put(f"{SAMPLE_TYPES}{st_id}", json={"default_report_template_id": ""}, headers=lab.headers)
        assert blank.json()["default_report_template_id"] is None

        audits = session.execute(text(
            "SELECT old_values->>'default_report_template_id', new_values->>'default_report_template_id' "
            "FROM audit_log WHERE action = 'SAMPLE_TYPE.UPDATED' AND entity_id = :e ORDER BY created_at"),
            {"e": st_id}).all()
        assert (str(b.id), None) in [tuple(r) for r in audits]  # the clear is audited with its old value

    def test_foreign_and_missing_templates_are_refused(self, client, lab, other):
        theirs = other.template("ajena")
        assert self.create(client, lab, default_report_template_id=str(theirs.id)).status_code == 403
        assert self.create(client, lab, code="Y", default_report_template_id=str(uuid.uuid4())).status_code == 404
        st = lab.sample_types["BX"]
        res = client.put(f"{SAMPLE_TYPES}{st.id}", json={"default_report_template_id": str(theirs.id)}, headers=lab.headers)
        assert res.status_code == 403

    def test_the_study_type_template_is_optional_and_clearable(self, client, lab):
        a = lab.template("a")
        url = f"/api/v1/study-types/{lab.study_type.id}"
        assert client.put(url, json={"default_report_template_id": str(a.id)}, headers=lab.headers).status_code == 200
        assert client.put(url, json={"name": "Renombrado"}, headers=lab.headers).json()["default_report_template_id"] == str(a.id)
        for clear in (None, ""):
            client.put(url, json={"default_report_template_id": str(a.id)}, headers=lab.headers)
            res = client.put(url, json={"default_report_template_id": clear}, headers=lab.headers)
            assert res.status_code == 200, res.text
            assert res.json()["default_report_template_id"] is None and res.json()["default_template"] is None

    def test_only_catalog_managers_change_the_link(self, client, lab):
        a = lab.template("a")
        st = lab.sample_types["BX"]
        for role in ("assistant", "pathologist", "viewer"):
            res = client.put(f"{SAMPLE_TYPES}{st.id}", json={"default_report_template_id": str(a.id)},
                             headers=lab.headers_for(role))
            assert res.status_code == 403, role


# ---------------------------------------------------------------------------------------- registration


class TestRegistration:
    def test_a_study_type_without_template_no_longer_blocks_registration(self, client, lab):
        a = lab.template("a")
        lab.link_sample_type("BX", a)
        reg = register(client, lab, ("BX",))
        sel = selection(client, lab, reg["order_id"])
        assert sel["report_template_id"] is None
        assert (sel["status"], sel["source"], sel["template"]["id"]) == ("RESOLVED", "SAMPLE_TYPE", str(a.id))

    def test_registration_without_any_template_still_works(self, client, lab):
        reg = register(client, lab, ("BX",))
        sel = selection(client, lab, reg["order_id"])
        assert (sel["status"], sel["template"]) == ("NONE", None)

    def test_an_explicit_choice_persists_through_creation_and_reload(self, client, lab):
        a, b = lab.template("a"), lab.template("b")
        lab.link_sample_type("BX", a)
        reg = register(client, lab, ("BX",), report_template_id=str(b.id))
        body = detail(client, lab, reg["order_id"])
        assert body["report_template_id"] == str(b.id)
        assert (body["report_template"]["source"], body["report_template"]["template"]["id"]) == ("ORDER", str(b.id))
        # What clearing it would restore is still visible.
        assert body["report_template"]["default_template"]["id"] == str(a.id)
        full = client.get(f"{LAB}/orders/{reg['order_id']}/full", headers=lab.headers).json()
        assert full["order"]["report_template_id"] == str(b.id)

    def test_the_legacy_order_route_accepts_the_same_optional_field(self, client, lab):
        b = lab.template("b")
        res = client.post(ORDERS, json=lab.order_payload(report_template_id=str(b.id)), headers=lab.headers)
        assert res.status_code == 200, res.text
        assert detail(client, lab, res.json()["id"])["report_template_id"] == str(b.id)

    @pytest.mark.parametrize("which", ["foreign", "unknown", "malformed"])
    def test_an_unusable_reference_leaves_nothing_behind(self, client, session, lab, other, which):
        ref = {"foreign": lambda: str(other.template("x").id), "unknown": lambda: str(uuid.uuid4()),
               "malformed": lambda: "no-es-un-uuid"}[which]()
        orders_before, ledger_before = count(session, 'SELECT count(*) FROM "order"'), ledger_rows(session)
        res = client.post(UNIFIED, json=lab.unified(("BX",), report_template_id=ref), headers=lab.headers)
        assert res.status_code == 404 and code_of(res) == "report_template_not_found"
        assert count(session, 'SELECT count(*) FROM "order"') == orders_before
        assert ledger_rows(session) == ledger_before

    def test_an_inactive_template_cannot_be_chosen(self, client, session, lab):
        off = lab.template("off", active=False)
        res = client.post(UNIFIED, json=lab.unified(("BX",), report_template_id=str(off.id)), headers=lab.headers)
        assert res.status_code == 409 and code_of(res) == "report_template_inactive"
        assert count(session, 'SELECT count(*) FROM "order"') == 0

    def test_idempotent_replay_keeps_the_choice_and_a_changed_choice_is_a_conflict(self, client, session, lab):
        a, b = lab.template("a"), lab.template("b")
        payload = lab.unified(("BX",), report_template_id=str(a.id))
        key = {"Idempotency-Key": f"tpl-{uuid.uuid4().hex}"}
        first = client.post(UNIFIED, json=payload, headers={**lab.headers, **key})
        replay = client.post(UNIFIED, json=payload, headers={**lab.headers, **key})
        assert first.status_code == replay.status_code == 200
        assert replay.json()["order"]["id"] == first.json()["order"]["id"]
        changed = client.post(UNIFIED, json={**payload, "report_template_id": str(b.id)}, headers={**lab.headers, **key})
        assert changed.status_code == 409
        assert count(session, 'SELECT count(*) FROM "order"') == 1
        assert detail(client, lab, first.json()["order"]["id"])["report_template_id"] == str(a.id)

    def test_the_fingerprint_of_a_request_without_the_field_is_the_1_4_0_fingerprint(self, lab):
        """A retry across the deploy (receipt written by 1.4.0, which had no such field) still replays."""
        import hashlib

        payload = OrderUnifiedCreate(**lab.unified(("BX",)))
        legacy = payload.model_dump(mode="json")
        legacy.pop("report_template_id")
        canonical = json.dumps(legacy, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        assert idempotency.fingerprint(payload) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        explicit = OrderUnifiedCreate(**lab.unified(("BX",)), report_template_id=str(uuid.uuid4()))
        assert idempotency.fingerprint(explicit) != idempotency.fingerprint(payload)


# ---------------------------------------------------------------------------------------- precedence


class TestPrecedence:
    def test_nothing_configured_is_no_template(self, client, lab):
        reg = register(client, lab, ("BX",))
        body = defaults(client, lab, reg["order_id"]).json()
        assert (body["template_id"], body["template_status"], body["v2_blocked_reason"]) == (None, "NONE", "NO_TEMPLATE")

    def test_the_study_type_template_is_an_optional_default(self, client, lab):
        s = lab.template("estudio")
        lab.link_study_type(s)
        reg = register(client, lab, ("BX",))
        body = defaults(client, lab, reg["order_id"]).json()
        assert (body["template_id"], body["template_source"], body["v2_blocked_reason"]) == (str(s.id), "STUDY_TYPE", None)

    def test_the_sample_type_default_outranks_the_study_type(self, client, lab):
        s, m = lab.template("estudio"), lab.template("muestra")
        lab.link_study_type(s)
        lab.link_sample_type("BX", m)
        reg = register(client, lab, ("BX",))
        body = defaults(client, lab, reg["order_id"]).json()
        assert (body["template_id"], body["template_source"]) == (str(m.id), "SAMPLE_TYPE")
        # The pre-1.4.1 study-type bootstrap is unchanged (compatibility): it still names the study template.
        legacy = client.get(f"/api/v1/study-types/{lab.study_type.id}/report-defaults", headers=lab.headers).json()
        assert legacy["template_id"] == str(s.id)

    def test_samples_that_agree_resolve_without_ambiguity(self, client, lab):
        m = lab.template("muestra")
        lab.link_sample_type("BX", m)
        lab.link_sample_type("TJ", m)
        reg = register(client, lab, ("BX", "TJ", "BX"))
        body = defaults(client, lab, reg["order_id"]).json()
        assert (body["template_id"], body["template_status"]) == (str(m.id), "RESOLVED")

    def test_samples_that_disagree_need_an_explicit_choice(self, client, lab):
        s, z, a = lab.template("estudio"), lab.template("zeta"), lab.template("alfa")
        lab.link_study_type(s)
        lab.link_sample_type("BX", z)
        lab.link_sample_type("TJ", a)
        lab.link_sample_type("CT", z)
        # The sample order is deliberately the reverse of the candidates' display order.
        reg = register(client, lab, ("BX", "TJ", "CT"))
        body = defaults(client, lab, reg["order_id"]).json()
        assert body["template_id"] is None and body["template_status"] == "AMBIGUOUS"
        assert body["v2_blocked_reason"] == "TEMPLATE_AMBIGUOUS"
        # Display order by name, and the study default does not silently settle it.
        assert [c["id"] for c in body["template_candidates"]] == [str(a.id), str(z.id)]
        # An explicit choice settles it.
        assert patch(client, lab, reg["order_id"], {"report_template_id": str(z.id)}).status_code == 200
        body = defaults(client, lab, reg["order_id"]).json()
        assert (body["template_id"], body["template_source"], body["v2_blocked_reason"]) == (str(z.id), "ORDER", None)

    def test_an_inactive_default_is_ignored_like_an_unset_one(self, client, session, lab):
        s, m = lab.template("estudio"), lab.template("muestra")
        lab.link_study_type(s)
        lab.link_sample_type("BX", m)
        m.is_active = False
        session.add(m)
        session.commit()
        reg = register(client, lab, ("BX",))
        assert defaults(client, lab, reg["order_id"]).json()["template_source"] == "STUDY_TYPE"

    def test_a_deactivated_explicit_choice_blocks_instead_of_falling_back(self, client, session, lab):
        s, b = lab.template("estudio"), lab.template("b")
        lab.link_study_type(s)
        reg = register(client, lab, ("BX",), report_template_id=str(b.id))
        b.is_active = False
        session.add(b)
        session.commit()
        body = defaults(client, lab, reg["order_id"]).json()
        assert (body["template_id"], body["template_status"], body["v2_blocked_reason"]) == (
            None, "INACTIVE", "TEMPLATE_INACTIVE")
        assert body["template_name"] == "Plantilla b"

    def test_an_order_from_before_the_hotfix_resolves_as_1_4_0_did(self, client, session, lab):
        """Untyped legacy samples and no explicit choice: the study type's template, as before."""
        s = lab.template("estudio")
        lab.link_study_type(s)
        reg = register(client, lab, ("BX",))
        session.execute(text("UPDATE sample SET sample_type_id = NULL WHERE order_id = :o"), {"o": reg["order_id"]})
        session.commit()
        assert defaults(client, lab, reg["order_id"]).json()["template_id"] == str(s.id)

    def test_a_removed_sample_no_longer_suggests_its_template(self, client, lab):
        a, b = lab.template("a"), lab.template("b")
        lab.link_sample_type("BX", a)
        lab.link_sample_type("TJ", b)
        reg = register(client, lab, ("BX", "TJ"))
        assert defaults(client, lab, reg["order_id"]).json()["template_status"] == "AMBIGUOUS"
        res = client.post(f"{LAB}/samples/{reg['sample_ids'][1]}/removal", json={"reason": "Captura duplicada"},
                          headers=lab.headers)
        assert res.status_code == 200, res.text
        assert defaults(client, lab, reg["order_id"]).json()["template_id"] == str(a.id)

    def test_letterhead_precedence_is_unchanged_by_the_template_choice(self, client, session, lab):
        """The chosen template's preferred letterhead applies, exactly as `create_report` resolves it."""
        plain, branded = lab.template("plain"), lab.template("branded")
        preferred = create_letterhead(session, lab.tenant, name="Preferido")
        create_letterhead_version(session, lab.tenant, preferred, status="ACTIVE")
        branded.preferred_letterhead_id = preferred.id
        session.add(branded)
        session.commit()
        reg = register(client, lab, ("BX",), report_template_id=str(plain.id))
        assert defaults(client, lab, reg["order_id"]).json()["letterhead_resolution_source"] == "TENANT_DEFAULT"
        patch(client, lab, reg["order_id"], {"report_template_id": str(branded.id)})
        body = defaults(client, lab, reg["order_id"]).json()
        assert (body["letterhead_resolution_source"], body["letterhead_id"]) == ("TEMPLATE_PREFERRED", str(preferred.id))

    def test_report_defaults_is_read_only_tenant_scoped_and_permissioned(self, client, session, lab, other):
        m = lab.template("muestra")
        lab.link_sample_type("BX", m)
        reg = register(client, lab, ("BX",))
        tables = ("audit_log", "order_event", '"order"', "report", "idempotency_receipt")
        before = {t: count(session, f"SELECT count(*) FROM {t}") for t in tables}
        row_before = session.execute(text('SELECT * FROM "order" WHERE id = :o'), {"o": reg["order_id"]}).one()
        for role in ("pathologist", "assistant", "viewer"):
            assert defaults(client, lab, reg["order_id"], headers=lab.headers_for(role)).status_code == 200, role
        assert {t: count(session, f"SELECT count(*) FROM {t}") for t in tables} == before
        assert session.execute(text('SELECT * FROM "order" WHERE id = :o'), {"o": reg["order_id"]}).one() == row_before
        assert defaults(client, lab, reg["order_id"], headers=other.headers).status_code == 404
        nobody = create_user(session, lab.tenant, email=f"none-{uuid.uuid4().hex[:6]}@x.invalid", roles=())
        assert defaults(client, lab, reg["order_id"], headers=auth_headers(nobody)).status_code == 403


# ---------------------------------------------------------------------------------------- order edit


class TestOrderEdit:
    def events(self, session, order_id):
        session.expire_all()
        return session.execute(text(
            "SELECT event_metadata FROM order_event WHERE order_id = :o AND event_type = :t ORDER BY created_at"),
            {"o": order_id, "t": EventType.ORDER_UPDATED.value}).scalars().all()

    def test_set_change_and_reset_are_audited_in_the_same_transaction(self, client, session, lab):
        a, b = lab.template("a"), lab.template("b")
        lab.link_sample_type("BX", a)
        reg = register(client, lab, ("BX",))
        oid = reg["order_id"]

        res = patch(client, lab, oid, {"report_template_id": str(b.id)})
        assert res.status_code == 200, res.text
        assert (res.json()["report_template_id"], res.json()["report_template"]["source"]) == (str(b.id), "ORDER")
        reset = patch(client, lab, oid, {"report_template_id": None})
        assert reset.json()["report_template_id"] is None
        assert (reset.json()["report_template"]["source"], reset.json()["report_template"]["template"]["id"]) == (
            "SAMPLE_TYPE", str(a.id))

        events = self.events(session, oid)
        assert [e["changed_fields"] for e in events] == [["report_template_id"], ["report_template_id"]]
        assert events[0]["old"] == {"report_template_id": None} and events[0]["new"] == {"report_template_id": str(b.id)}
        assert events[0]["report_template_names"] == {"old": None, "new": "Plantilla b"}
        assert events[1]["report_template_names"] == {"old": "Plantilla b", "new": None}
        audits = session.execute(text(
            "SELECT old_values, new_values FROM audit_log WHERE action = 'ORDER.UPDATED' AND entity_id = :o "
            "ORDER BY created_at"), {"o": oid}).all()
        assert [(r[0], r[1]) for r in audits] == [
            ({"report_template_id": None}, {"report_template_id": str(b.id)}),
            ({"report_template_id": str(b.id)}, {"report_template_id": None}),
        ]

    def test_an_omitted_field_and_an_unchanged_value_write_nothing(self, client, session, lab):
        b = lab.template("b")
        reg = register(client, lab, ("BX",), report_template_id=str(b.id))
        oid = reg["order_id"]
        assert patch(client, lab, oid, {"notes": "Nota"}).json()["report_template_id"] == str(b.id)
        before = len(self.events(session, oid))
        assert patch(client, lab, oid, {"report_template_id": str(b.id).upper()}).status_code == 200
        assert len(self.events(session, oid)) == before

    def test_unusable_references_are_refused_without_change(self, client, lab, other):
        b = lab.template("b")
        off = lab.template("off", active=False)
        reg = register(client, lab, ("BX",), report_template_id=str(b.id))
        oid = reg["order_id"]
        theirs = other.template("ajena")
        for ref, status, code in ((str(theirs.id), 404, "report_template_not_found"),
                                  (str(uuid.uuid4()), 404, "report_template_not_found"),
                                  ("basura", 404, "report_template_not_found"),
                                  (str(off.id), 409, "report_template_inactive")):
            res = patch(client, lab, oid, {"report_template_id": ref})
            assert (res.status_code, code_of(res)) == (status, code), ref
        assert detail(client, lab, oid)["report_template_id"] == str(b.id)

    def test_a_foreign_order_is_not_found(self, client, lab, other):
        reg = register(client, lab, ("BX",))
        theirs = other.template("t")
        assert patch(client, lab, reg["order_id"], {"report_template_id": str(theirs.id)}, headers=other.headers).status_code == 404

    def test_roles_follow_the_order_edit_capability(self, client, lab):
        b = lab.template("b")
        reg = register(client, lab, ("BX",))
        assert patch(client, lab, reg["order_id"], {"report_template_id": str(b.id)},
                     headers=lab.headers_for("viewer")).status_code == 403
        assert patch(client, lab, reg["order_id"], {"report_template_id": str(b.id)},
                     headers=lab.headers_for("billing")).status_code == 403
        assert patch(client, lab, reg["order_id"], {"report_template_id": str(b.id)},
                     headers=lab.headers_for("assistant")).status_code == 200

    @pytest.mark.parametrize("closed", ["status", "review_history"])
    def test_the_capture_edit_window_applies(self, client, session, lab, closed):
        b = lab.template("b")
        reg = register(client, lab, ("BX",))
        if closed == "status":
            set_status(session, reg["order_id"], OrderStatus.REVIEW)
        else:
            add_event(session, lab, reg["order_id"], EventType.REPORT_SUBMITTED)
        res = patch(client, lab, reg["order_id"], {"report_template_id": str(b.id)})
        assert res.status_code == 409, res.text
        assert detail(client, lab, reg["order_id"])["report_template_id"] is None

    def test_a_report_freezes_the_choice_but_not_the_other_fields(self, client, session, lab):
        a, b = lab.template("a"), lab.template("b")
        reg = register(client, lab, ("BX",), report_template_id=str(a.id))
        oid = reg["order_id"]
        make_report(session, lab, oid)
        state = client.get(f"{LAB}/orders/{oid}/management", headers=lab.headers).json()
        assert state["edit"]["allowed"] is True
        assert state["report_template"]["allowed"] is False
        assert [x["code"] for x in state["report_template"]["blockers"]] == ["report_template_locked"]
        for body in ({"report_template_id": str(b.id)}, {"report_template_id": None}):
            res = patch(client, lab, oid, body)
            assert (res.status_code, code_of(res)) == (409, "report_template_locked")
        # Re-sending the stored value (the edit form does not, but a client may) and other fields still work.
        res = patch(client, lab, oid, {"report_template_id": str(a.id), "notes": "Sigue editable"})
        assert res.status_code == 200 and res.json()["notes"] == "Sigue editable"

    def test_management_state_offers_the_change_inside_the_window(self, client, lab):
        reg = register(client, lab, ("BX",))
        state = client.get(f"{LAB}/orders/{reg['order_id']}/management", headers=lab.headers).json()
        assert state["report_template"] == {"permitted": True, "allowed": True, "blockers": []}
        assert "report_template_id" in state["editable_fields"]
        viewer = client.get(f"{LAB}/orders/{reg['order_id']}/management", headers=lab.headers_for("viewer")).json()
        assert viewer["report_template"]["permitted"] is False

    def test_adding_samples_never_overwrites_an_explicit_choice(self, client, lab):
        a, b, c = lab.template("a"), lab.template("b"), lab.template("c")
        lab.link_sample_type("BX", a)
        lab.link_sample_type("TJ", c)
        reg = register(client, lab, ("BX",), report_template_id=str(b.id))
        res = client.post(SAMPLES, json=lab.sample_payload(reg["order_id"], "TJ"), headers=lab.headers)
        assert res.status_code == 200, res.text
        sel = selection(client, lab, reg["order_id"])
        assert (sel["source"], sel["template"]["id"], sel["default_status"]) == ("ORDER", str(b.id), "AMBIGUOUS")


# ---------------------------------------------------------------------------------------- new report authoring


class TestNewReportConsumesTheOrderChoice:
    def test_the_editor_bootstrap_and_creation_use_the_persisted_choice(self, client, session, lab):
        s, m, x = lab.template("estudio"), lab.template("muestra"), lab.template("elegida")
        lab.link_study_type(s)
        lab.link_sample_type("BX", m)
        reg = register(client, lab, ("BX",), report_template_id=str(x.id))
        boot = defaults(client, lab, reg["order_id"]).json()
        assert boot["template_id"] == str(x.id) and boot["v2_blocked_reason"] is None
        tpl = client.get(f"{TEMPLATES}{boot['template_id']}", headers=lab.headers).json()
        res = create_report_for(client, lab, reg["order_id"], x, template_hash=tpl["template_hash"])
        assert res.status_code == 200, res.text
        assert frozen_template(session, res.json()["id"]) == structure("elegida")

    @pytest.mark.parametrize("wrong", ["study", "sample"])
    def test_another_template_than_the_order_effective_one_is_refused(self, client, session, lab, wrong):
        s, m, x = lab.template("estudio"), lab.template("muestra"), lab.template("elegida")
        lab.link_study_type(s)
        lab.link_sample_type("BX", m)
        reg = register(client, lab, ("BX",), report_template_id=str(x.id))
        res = create_report_for(client, lab, reg["order_id"], {"study": s, "sample": m}[wrong])
        assert (res.status_code, code_of(res)) == (409, "order_report_template_changed")
        assert reports_of(session, reg["order_id"]) == 0

    def test_an_older_client_reading_the_study_type_cannot_bypass_the_sample_default(self, client, session, lab):
        s, m = lab.template("estudio"), lab.template("muestra")
        lab.link_study_type(s)
        lab.link_sample_type("BX", m)
        reg = register(client, lab, ("BX",))
        assert create_report_for(client, lab, reg["order_id"], s).status_code == 409
        res = create_report_for(client, lab, reg["order_id"], m)
        assert res.status_code == 200, res.text
        assert frozen_template(session, res.json()["id"]) == structure("muestra")

    def test_a_choice_changed_after_the_bootstrap_is_a_conflict_with_no_side_effects(self, client, session, lab):
        a, b = lab.template("a"), lab.template("b")
        reg = register(client, lab, ("BX",), report_template_id=str(a.id))
        oid = reg["order_id"]
        boot = defaults(client, lab, oid).json()
        tpl = client.get(f"{TEMPLATES}{boot['template_id']}", headers=lab.headers).json()
        patch(client, lab, oid, {"report_template_id": str(b.id)})  # another user, meanwhile
        events_before = count(session, "SELECT count(*) FROM order_event WHERE order_id = :o", o=oid)
        res = create_report_for(client, lab, oid, a, template_hash=tpl["template_hash"])
        assert (res.status_code, code_of(res)) == (409, "order_report_template_changed")
        assert reports_of(session, oid) == 0
        assert count(session, "SELECT count(*) FROM order_event WHERE order_id = :o", o=oid) == events_before
        assert count(session, "SELECT count(*) FROM storage_object") == 0

    def test_the_stale_hash_guard_still_applies_to_the_order_template(self, client, session, lab):
        a = lab.template("a")
        lab.link_sample_type("BX", a)
        reg = register(client, lab, ("BX",))
        tpl = client.get(f"{TEMPLATES}{a.id}", headers=lab.headers).json()
        changed = client.put(f"{TEMPLATES}{a.id}", json={"template_json": structure("otra")}, headers=lab.headers)
        assert changed.status_code == 200, changed.text
        res = create_report_for(client, lab, reg["order_id"], a, template_hash=tpl["template_hash"])
        assert res.status_code == 409
        assert reports_of(session, reg["order_id"]) == 0
        assert create_report_for(client, lab, reg["order_id"], a).status_code == 200  # reloaded editor

    def test_an_ambiguous_order_cannot_get_a_report_until_someone_chooses(self, client, session, lab):
        a, b = lab.template("a"), lab.template("b")
        lab.link_sample_type("BX", a)
        lab.link_sample_type("TJ", b)
        reg = register(client, lab, ("BX", "TJ"))
        res = create_report_for(client, lab, reg["order_id"], a)
        assert (res.status_code, code_of(res)) == (409, "order_report_template_ambiguous")
        patch(client, lab, reg["order_id"], {"report_template_id": str(b.id)})
        assert create_report_for(client, lab, reg["order_id"], b).status_code == 200

    def test_a_deactivated_explicit_choice_blocks_creation(self, client, session, lab):
        b = lab.template("b")
        reg = register(client, lab, ("BX",), report_template_id=str(b.id))
        session.execute(text("UPDATE report_template SET is_active = false WHERE id = :t"), {"t": str(b.id)})
        session.commit()
        assert create_report_for(client, lab, reg["order_id"], b).status_code == 409
        assert reports_of(session, reg["order_id"]) == 0

    def test_an_order_with_nothing_configured_keeps_the_pre_1_4_1_api(self, client, lab):
        free = lab.template("libre")
        reg = register(client, lab, ("BX",))
        assert create_report_for(client, lab, reg["order_id"], free).status_code == 200

    def test_the_historical_version_selector_is_checked_too(self, client, session, lab):
        from app.models.report_template_version import ReportTemplateVersion
        from .factories import valid_rendering_snapshot

        a, other_tpl = lab.template("a"), lab.template("otra")
        version = ReportTemplateVersion(
            tenant_id=lab.tenant.id, report_template_id=other_tpl.id, version_number=1,
            configuration=valid_rendering_snapshot(), status="ACTIVE", schema_version=2)
        session.add(version)
        session.commit()
        reg = register(client, lab, ("BX",), report_template_id=str(a.id))
        payload = {"tenant_id": str(lab.tenant.id), "branch_id": str(lab.branch.id), "order_id": reg["order_id"],
                   "report": {"base": {}, "sections": {}}, "template_version_id": str(version.id)}
        res = client.post(REPORTS, json=payload, headers=lab.headers)
        assert (res.status_code, code_of(res)) == (409, "order_report_template_changed")

    def test_roles_tenant_and_lifecycle_of_report_creation_are_unchanged(self, client, session, lab, other):
        a = lab.template("a")
        reg = register(client, lab, ("BX",), report_template_id=str(a.id))
        assert create_report_for(client, lab, reg["order_id"], a, headers=lab.headers_for("assistant")).status_code == 403
        assert create_report_for(client, lab, reg["order_id"], a, headers=other.headers).status_code == 403
        assert reports_of(session, reg["order_id"]) == 0
        assert create_report_for(client, lab, reg["order_id"], a, headers=lab.headers_for("pathologist")).status_code == 200


# ---------------------------------------------------------------------------------------- frozen artifacts


class TestExistingReportsAreNeverReResolved:
    def test_later_catalog_template_and_order_changes_do_not_touch_the_snapshot(self, client, session, lab):
        a, b = lab.template("a"), lab.template("b")
        lab.link_sample_type("BX", a)
        reg = register(client, lab, ("BX",))
        created = create_report_for(client, lab, reg["order_id"], a)
        assert created.status_code == 200, created.text
        report_id = created.json()["id"]
        frozen = frozen_template(session, report_id)

        lab.link_sample_type("BX", b)                                         # catalog default moves
        client.put(f"{TEMPLATES}{a.id}", json={"template_json": structure("cambiada")}, headers=lab.headers)
        locked = patch(client, lab, reg["order_id"], {"report_template_id": str(b.id)})
        assert code_of(locked) == "report_template_locked"

        saved = client.post(f"{REPORTS}{report_id}/new_version", json={
            "tenant_id": str(lab.tenant.id), "branch_id": str(lab.branch.id), "order_id": reg["order_id"],
            "report": {"base": {}, "sections": {"a": {"content": "Texto"}}}}, headers=lab.headers)
        assert saved.status_code == 200, saved.text
        assert frozen_template(session, report_id) == frozen == structure("a")


# ---------------------------------------------------------------------------------------- deletion


class TestTemplateDeletion:
    def test_a_referenced_template_is_a_clear_conflict_not_a_server_error(self, client, session, lab):
        chosen, linked, studied, free = lab.template("o"), lab.template("s"), lab.template("e"), lab.template("f")
        register(client, lab, ("BX",), report_template_id=str(chosen.id))
        lab.link_sample_type("TJ", linked)
        lab.link_study_type(studied)
        for tpl in (chosen, linked, studied):
            res = client.delete(f"{TEMPLATES}{tpl.id}?hard_delete=true", headers=lab.headers)
            assert res.status_code == 409, tpl.name
        assert client.delete(f"{TEMPLATES}{free.id}?hard_delete=true", headers=lab.headers).status_code == 200

    def test_a_removed_order_still_protects_its_choice(self, client, session, lab):
        chosen = lab.template("o")
        reg = register(client, lab, ("BX",), report_template_id=str(chosen.id))
        res = client.post(f"{LAB}/orders/{reg['order_id']}/removal", json={"reason": "Captura errónea"}, headers=lab.headers)
        assert res.status_code == 200, res.text
        assert client.delete(f"{TEMPLATES}{chosen.id}?hard_delete=true", headers=lab.headers).status_code == 409
        assert session.get(Order, uuid.UUID(reg["order_id"])) is None  # hidden, but its row is kept
