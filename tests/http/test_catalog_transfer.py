"""Catalog JSON export, import into sample types and direct copy from study
types (Céluma 1.4, issue #17).

Contract: `celuma-engineering/specs/laboratory/catalog-transfer.md`. Synthetic
data only; nothing here resembles a customer's catalog.
"""
from __future__ import annotations

import copy
import threading
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker
from sqlmodel import Session, select

from app.models.audit import AuditLog
from app.models.report import ReportTemplate
from app.models.sample_type import SampleType
from app.models.study_type import StudyType
from app.services import catalog_transfer

from .factories import auth_headers, create_branch, create_tenant, create_user

ST = "/api/v1/study-types/"
SMP = "/api/v1/sample-types/"
IMPORT = f"{SMP}import/"
COPY = f"{SMP}copy-from-study-types/"


class Lab:
    def __init__(self, session: Session, name="Alfa", *, role="superuser"):
        self.session = session
        self.tenant = create_tenant(session, name=name)
        self.branch = create_branch(session, self.tenant)
        self.user = create_user(session, self.tenant, email=f"u@{name.lower()}.invalid", roles=(role,))
        self.headers = auth_headers(self.user)

    def study(self, code, name, *, description=None, active=True, template=None) -> StudyType:
        row = StudyType(tenant_id=self.tenant.id, code=code, name=name, description=description,
                        is_active=active, default_report_template_id=template.id if template else None)
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def sample_type(self, code, name, *, active=True) -> SampleType:
        row = SampleType(tenant_id=self.tenant.id, code=code, name=name, is_active=active)
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def template(self, name="Plantilla") -> ReportTemplate:
        row = ReportTemplate(tenant_id=self.tenant.id, name=name)
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def sample_types(self) -> list[SampleType]:
        self.session.expire_all()
        return self.session.exec(
            select(SampleType).where(SampleType.tenant_id == self.tenant.id).order_by(SampleType.code)).all()


@pytest.fixture
def lab(session):
    return Lab(session, "Alfa")


@pytest.fixture
def other(session):
    return Lab(session, "Beta")


def doc(*items, kind="study_types", **extra):
    return {"schema": "celuma.catalog-export", "version": 1, "catalog_kind": kind,
            "items": list(items), **extra}


def item(code, name, **extra):
    return {"code": code, "name": name, **extra}


def preview(client, lab, document):
    return client.post(f"{IMPORT}preview", json={"document": document}, headers=lab.headers)


def confirm(client, lab, document, selected, plan, **extra):
    return client.post(f"{IMPORT}confirm", headers=lab.headers, json={
        "document": document, "selected_indexes": selected, "plan_digest": plan["plan_digest"], **extra})


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class TestExport:
    def test_study_type_export_envelope(self, client, session, lab):
        tpl = lab.template("Plantilla Uno")
        a = lab.study("HIST", "Histopatología", description="Tejido", template=tpl)
        lab.study("CITO", "Citología", active=False)
        response = client.get(f"{ST}export", headers=lab.headers)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.headers["content-disposition"].startswith(
            'attachment; filename="celuma-study-types-export-')
        body = response.json()
        assert (body["schema"], body["version"], body["catalog_kind"]) == ("celuma.catalog-export", 1, "study_types")
        assert body["item_count"] == 2 and body["include_inactive"] is True
        # deterministic: by code
        assert [i["code"] for i in body["items"]] == ["CITO", "HIST"]
        hist = body["items"][1]
        assert hist == {"code": "HIST", "name": "Histopatología", "description": "Tejido", "is_active": True,
                        "provenance": {"source_id": str(a.id), "default_report_template_id": str(tpl.id),
                                       "default_report_template_name": "Plantilla Uno"}}
        assert body["items"][0]["is_active"] is False
        # the tenant is not part of the document, and accents are real UTF-8
        assert str(lab.tenant.id) not in response.text and "Alfa" not in response.text
        assert "Histopatología".encode("utf-8") in response.content

    def test_inactive_entries_can_be_excluded_and_empty_catalogs_are_valid(self, client, lab):
        empty = client.get(f"{SMP}export", headers=lab.headers).json()
        assert empty["items"] == [] and empty["item_count"] == 0 and empty["catalog_kind"] == "sample_types"
        lab.sample_type("ON", "Activo")
        lab.sample_type("OFF", "Inactivo", active=False)
        only_active = client.get(f"{SMP}export", params={"include_inactive": "false"}, headers=lab.headers).json()
        assert [i["code"] for i in only_active["items"]] == ["ON"] and only_active["include_inactive"] is False
        assert client.get(f"{SMP}export", headers=lab.headers).json()["item_count"] == 2

    def test_the_two_exports_are_distinguishable(self, client, lab):
        study = client.get(f"{ST}export", headers=lab.headers)
        sample = client.get(f"{SMP}export", headers=lab.headers)
        assert "study-types" in study.headers["content-disposition"]
        assert "sample-types" in sample.headers["content-disposition"]
        assert study.json()["catalog_kind"] != sample.json()["catalog_kind"]

    def test_export_covers_the_full_dataset_and_only_this_tenant(self, client, session, lab, other):
        for n in range(130):
            session.add(StudyType(tenant_id=lab.tenant.id, code=f"T{n:03d}", name=f"Tipo {n}"))
        other.study("AJENO", "Ajeno")
        session.commit()
        body = client.get(f"{ST}export", headers=lab.headers).json()
        assert body["item_count"] == 130 == len(body["items"])
        assert "AJENO" not in {i["code"] for i in body["items"]}

    def test_export_is_read_only_and_audited(self, client, session, lab):
        lab.study("HIST", "Histopatología")
        before = [(r.id, r.code, r.name) for r in session.exec(select(StudyType)).all()]
        client.get(f"{ST}export", headers=lab.headers)
        client.get(f"{SMP}export", headers=lab.headers)
        session.expire_all()
        assert before == [(r.id, r.code, r.name) for r in session.exec(select(StudyType)).all()]
        audits = session.exec(select(AuditLog).where(AuditLog.action == "CATALOG.EXPORTED")).all()
        assert sorted(a.event_metadata["catalog_kind"] for a in audits) == ["sample_types", "study_types"]
        assert all(a.actor_user_id == lab.user.id and a.tenant_id == lab.tenant.id for a in audits)

    def test_a_sample_type_export_is_importable_as_is(self, client, lab, other):
        lab.sample_type("SAN", "Sangre")
        exported = client.get(f"{SMP}export", headers=lab.headers).json()
        body = preview(client, other, exported)
        assert body.status_code == 200 and body.json()["source_kind"] == "sample_types"
        assert [i["status"] for i in body.json()["items"]] == ["ready"]


# ---------------------------------------------------------------------------
# Import: validation and preview
# ---------------------------------------------------------------------------


class TestImportPreview:
    def test_ready_items_and_nothing_is_written(self, client, lab, other):
        lab.study("HIST", "Histopatología", description="d")
        exported = client.get(f"{ST}export", headers=lab.headers).json()
        body = preview(client, other, exported).json()
        assert body["origin"] == "file" and body["source_kind"] == "study_types"
        assert (body["total"], body["ready"], body["conflicts"], body["invalid"]) == (1, 1, 0, 0)
        first = body["items"][0]
        assert (first["code"], first["name"], first["description"], first["status"]) == (
            "HIST", "Histopatología", "d", "ready")
        assert other.sample_types() == [] and lab.sample_types() == []

    def test_normalisation_and_warnings(self, client, lab):
        body = preview(client, lab, doc(
            item(" bio ", "  Biopsia  ", description="  ", surprise=1),
        )).json()["items"][0]
        assert (body["code"], body["name"], body["description"]) == ("BIO", "Biopsia", None)
        assert [w["code"] for w in body["warnings"]] == ["unknown_field_ignored"]

    def test_field_validation(self, client, lab):
        long_desc = "d" * 2001
        cases = [
            item("", "x"), item("A", ""), item(None, "x"), item("A", None), item(5, "x"), item("A", ["x"]),
            item("X" * 51, "x"), item("A", "N" * 256), item("A", "x", description=long_desc),
            item("A", "x", description=7), item("A", "x", is_active="yes"), item("A", "x", provenance="p"),
            item("A\x00B", "x"), item("A", "bad\x07name"), item("A\nB", "x"), "not an object", 12, None,
        ]
        body = preview(client, lab, doc(*cases)).json()
        assert body["invalid"] == len(cases) and body["ready"] == 0
        assert all(i["status"] == "invalid" and i["issues"] for i in body["items"])

    def test_multiline_descriptions_are_allowed(self, client, lab):
        body = preview(client, lab, doc(item("A", "x", description="línea 1\nlínea 2\tcon tab"))).json()
        assert body["items"][0]["status"] == "ready"

    def test_collisions_with_the_destination_and_inside_the_source(self, client, lab):
        lab.sample_type("BIO", "Biopsia existente")
        lab.sample_type("OFF", "Apagado", active=False)
        body = preview(client, lab, doc(
            item("bio", "Otra biopsia"), item("OFF", "x"), item("NEW", "Nuevo"), item("new", "Nuevo repetido"),
            item("OK", "Biopsia existente"),
        )).json()
        assert [i["status"] for i in body["items"]] == [
            "code_exists", "code_exists", "ready", "duplicate_in_source", "ready"]
        assert body["conflicts"] == 3 and body["ready"] == 2
        assert body["items"][0]["existing_sample_type_id"] is not None
        assert "inactive" in body["items"][1]["issues"][0]["message"]
        # same NAME as an existing type is only a warning: names are not unique
        assert [w["code"] for w in body["items"][4]["warnings"]] == ["name_exists"]

    @pytest.mark.parametrize("document,status,code", [
        ({}, 422, "unsupported_schema"),
        (doc(), 200, None),
        ({**doc(), "schema": "other"}, 422, "unsupported_schema"),
        ({**doc(), "version": 2}, 422, "unsupported_version"),
        ({**doc(), "version": True}, 422, "unsupported_version"),
        ({**doc(), "version": "1"}, 422, "unsupported_version"),
        ({**doc(), "catalog_kind": "patients"}, 422, "unsupported_catalog_kind"),
        ({**doc(), "items": {}}, 422, "invalid_document"),
        ({**doc(), "items": "x"}, 422, "invalid_document"),
    ])
    def test_envelope_validation(self, client, lab, document, status, code):
        response = preview(client, lab, document)
        assert response.status_code == status, response.text
        if code:
            assert response.json()["detail"]["code"] == code

    def test_a_document_must_be_a_json_object(self, client, lab):
        for bad in ([], "x", 5, None):
            assert client.post(f"{IMPORT}preview", json={"document": bad}, headers=lab.headers).status_code == 422
        assert client.post(f"{IMPORT}preview", content=b"{not json", headers={
            **lab.headers, "content-type": "application/json"}).status_code == 422

    def test_resource_bounds(self, client, lab):
        many = doc(*[item(f"C{n}", f"Nombre {n}") for n in range(500)])
        assert preview(client, lab, many).json()["ready"] == 500
        too_many = doc(*[item(f"C{n}", f"Nombre {n}") for n in range(501)])
        refused = preview(client, lab, too_many)
        assert refused.status_code == 413 and refused.json()["detail"]["code"] == "too_many_items"
        big = doc(item("A", "x", provenance={"junk": "j" * (1024 * 1024 + 10)}))
        assert preview(client, lab, big).status_code == 413

    def test_template_references_resolve_only_inside_the_tenant(self, client, lab, other):
        mine, theirs = lab.template("Mía"), other.template("Ajena")
        body = preview(client, lab, doc(
            item("A", "x", provenance={"default_report_template_id": str(mine.id)}),
            item("B", "x", provenance={"default_report_template_id": str(theirs.id)}),
            item("C", "x", provenance={"default_report_template_id": str(uuid.uuid4())}),
            item("D", "x", provenance={"default_report_template_id": "garbage"}),
            item("E", "x", provenance={"source_id": "123"}),
        )).json()
        assert [i["template_reference"] for i in body["items"]] == ["mapped", "dropped", "dropped", "dropped", "none"]
        # foreign and unknown ids are indistinguishable
        assert body["items"][1]["warnings"] == body["items"][2]["warnings"]
        assert all(i["status"] == "ready" for i in body["items"])


# ---------------------------------------------------------------------------
# Import: confirmation
# ---------------------------------------------------------------------------


class TestImportConfirm:
    def test_creates_independent_records_in_the_authenticated_tenant(self, client, session, lab, other):
        tpl = lab.template()
        src = lab.study("HIST", "Histopatología", description="d", template=tpl)
        lab.study("OFF", "Apagado", active=False)
        exported = client.get(f"{ST}export", headers=lab.headers).json()
        # A hostile document: tenant ids and source ids are only data.
        exported["tenant_id"] = str(lab.tenant.id)
        for entry in exported["items"]:
            entry["tenant_id"] = str(lab.tenant.id)
            entry["id"] = str(src.id)

        plan = preview(client, other, exported).json()
        result = confirm(client, other, exported, [0, 1], plan)
        assert result.status_code == 200, result.text
        created = result.json()["created"]
        assert {c["code"] for c in created} == {"HIST", "OFF"}
        rows = other.sample_types()
        assert {r.tenant_id for r in rows} == {other.tenant.id}
        assert src.id not in {r.id for r in rows}
        assert {r.code: r.is_active for r in rows} == {"HIST": True, "OFF": False}
        # the template of the SOURCE laboratory is not the destination's: dropped
        assert all(r.default_report_template_id is None for r in rows)
        assert lab.sample_types() == []
        # the source catalog is untouched
        assert {s.code for s in session.exec(select(StudyType).where(StudyType.tenant_id == lab.tenant.id)).all()} == {"HIST", "OFF"}

    def test_audit_trail(self, client, session, lab):
        document = doc(item("A", "Uno"), item("B", "Dos"))
        plan = preview(client, lab, document).json()
        result = confirm(client, lab, document, [0, 1], plan).json()
        audits = session.exec(select(AuditLog).where(AuditLog.tenant_id == lab.tenant.id)).all()
        by_action = {}
        for a in audits:
            by_action.setdefault(a.action, []).append(a)
        assert len(by_action["SAMPLE_TYPE.IMPORTED"]) == 2 and len(by_action["SAMPLE_TYPE.TRANSFER_COMPLETED"]) == 1
        summary = by_action["SAMPLE_TYPE.TRANSFER_COMPLETED"][0]
        assert str(summary.entity_id) == result["operation_id"]
        assert summary.event_metadata["created"] == 2 and summary.event_metadata["origin"] == "file"
        assert len(summary.event_metadata["source_document_sha256"]) == 64
        assert all(a.actor_user_id == lab.user.id for a in audits)
        one = by_action["SAMPLE_TYPE.IMPORTED"][0]
        assert one.new_values["code"] in {"A", "B"} and one.event_metadata["operation_id"] == result["operation_id"]

    def test_mapped_template_is_kept(self, client, lab):
        tpl = lab.template()
        document = doc(item("A", "x", provenance={"default_report_template_id": str(tpl.id)}))
        plan = preview(client, lab, document).json()
        created = confirm(client, lab, document, [0], plan).json()["created"][0]
        assert created["default_report_template_id"] == str(tpl.id)

    def test_selection_is_explicit_and_validated(self, client, lab):
        document = doc(item("A", "Uno"), item("", "Roto"), item("C", "Tres"))
        plan = preview(client, lab, document).json()
        assert confirm(client, lab, document, [], plan).status_code == 422
        assert confirm(client, lab, document, [9], plan).status_code == 422
        invalid = confirm(client, lab, document, [0, 1], plan)
        assert invalid.status_code == 422 and invalid.json()["detail"]["code"] == "invalid_selection"
        assert lab.sample_types() == []
        only = confirm(client, lab, document, [2], plan)
        assert only.status_code == 200
        assert [r.code for r in lab.sample_types()] == ["C"]  # unselected "A" is NOT created

    def test_abort_policy_is_all_or_nothing(self, client, lab):
        existing = lab.sample_type("B", "Original")
        document = doc(item("A", "Uno"), item("B", "Pisaría el original"), item("C", "Tres"))
        plan = preview(client, lab, document).json()
        refused = confirm(client, lab, document, [0, 1, 2], plan)
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "conflicts"
        assert refused.json()["detail"]["indexes"] == [1]
        assert [r.code for r in lab.sample_types()] == ["B"]
        assert lab.sample_types()[0].name == "Original" and lab.sample_types()[0].id == existing.id

    def test_skip_policy_creates_the_rest_and_never_overwrites(self, client, lab):
        lab.sample_type("B", "Original")
        document = doc(item("A", "Uno"), item("B", "Pisaría el original"), item("a", "Repetido"))
        plan = preview(client, lab, document).json()
        result = confirm(client, lab, document, [0, 1, 2], plan, on_conflict="skip").json()
        assert [c["code"] for c in result["created"]] == ["A"]
        assert {(s["index"], s["reason"]) for s in result["skipped"]} == {(1, "code_exists"), (2, "duplicate_in_source")}
        names = {r.code: r.name for r in lab.sample_types()}
        assert names == {"A": "Uno", "B": "Original"}

    def test_retry_after_success_creates_nothing_more(self, client, lab):
        document = doc(item("A", "Uno"))
        plan = preview(client, lab, document).json()
        assert confirm(client, lab, document, [0], plan).status_code == 200
        again = confirm(client, lab, document, [0], plan)
        assert again.status_code == 409 and again.json()["detail"]["code"] == "preview_stale"
        assert again.json()["detail"]["preview"]["items"][0]["status"] == "code_exists"
        assert len(lab.sample_types()) == 1
        # a fresh preview reports the conflict; skip is then a harmless no-op
        fresh = preview(client, lab, document).json()
        noop = confirm(client, lab, document, [0], fresh, on_conflict="skip")
        assert noop.status_code == 200 and noop.json()["created"] == [] and len(lab.sample_types()) == 1

    def test_a_changed_plan_requires_a_new_preview(self, client, lab):
        document = doc(item("A", "Uno"), item("B", "Dos"))
        plan = preview(client, lab, document).json()
        client.post(SMP, json={"code": "B", "name": "Alguien llegó antes"}, headers=lab.headers)
        stale = confirm(client, lab, document, [0], plan)
        assert stale.status_code == 409 and stale.json()["detail"]["code"] == "preview_stale"
        assert [r.code for r in lab.sample_types()] == ["B"]
        # editing the document after the preview is stale too
        edited = copy.deepcopy(document)
        edited["items"][0]["name"] = "Cambiado"
        plan2 = preview(client, lab, document).json()
        assert confirm(client, lab, edited, [0], plan2).status_code == 409

    def test_failure_midway_rolls_everything_back(self, client, session, lab, monkeypatch):
        document = doc(item("A", "Uno"), item("B", "Dos"), item("C", "Tres"))
        plan = preview(client, lab, document).json()
        calls = {"n": 0}
        real = catalog_transfer.record_audit

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated failure")
            return real(*args, **kwargs)

        monkeypatch.setattr(catalog_transfer, "record_audit", flaky)
        with pytest.raises(RuntimeError):
            confirm(client, lab, document, [0, 1, 2], plan)
        monkeypatch.undo()
        assert lab.sample_types() == []
        assert session.exec(select(AuditLog).where(AuditLog.action.like("SAMPLE_TYPE.%"))).all() == []
        # and the same request succeeds once the fault is gone
        assert confirm(client, lab, document, [0, 1, 2], plan).status_code == 200
        assert len(lab.sample_types()) == 3

    def test_a_concurrent_writer_between_validation_and_insert_is_a_clean_409(self, client, session, lab, monkeypatch):
        """The unique constraint is the last guard: another transaction commits
        the code after this plan was validated."""
        engine = session.get_bind()
        document = doc(item("A", "Uno"), item("B", "Dos"))
        plan = preview(client, lab, document).json()
        real = catalog_transfer.record_audit
        fired = {"done": False}

        def interloper(*args, **kwargs):
            if not fired["done"]:
                fired["done"] = True
                with Session(engine) as other_session:
                    other_session.add(SampleType(tenant_id=lab.tenant.id, code="B", name="Concurrente"))
                    other_session.commit()
            return real(*args, **kwargs)

        monkeypatch.setattr(catalog_transfer, "record_audit", interloper)
        response = confirm(client, lab, document, [0, 1], plan)
        monkeypatch.undo()
        assert response.status_code == 409 and response.json()["detail"]["code"] == "concurrent_conflict"
        assert [(r.code, r.name) for r in lab.sample_types()] == [("B", "Concurrente")]

    def test_two_simultaneous_confirmations_create_each_code_once(self, session, lab):
        engine = session.get_bind()
        document = doc(item("A", "Uno"), item("B", "Dos"))
        make = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
        with make() as s0:
            digest = catalog_transfer.plan_import(s0, lab.tenant.id, document).digest()
        barrier = threading.Barrier(2)
        outcomes: list = []

        def run():
            with make() as s:
                barrier.wait()
                try:
                    result = catalog_transfer.confirm(
                        s, tenant_id=lab.tenant.id, user_id=lab.user.id,
                        build=lambda: catalog_transfer.plan_import(s, lab.tenant.id, document),
                        selected=[0, 1], on_conflict="abort", plan_digest=digest, source_label={})
                    outcomes.append(("ok", len(result.created)))
                except HTTPException as exc:
                    outcomes.append(("refused", exc.status_code, exc.detail["code"]))

        threads = [threading.Thread(target=run) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sorted(o[0] for o in outcomes) == ["ok", "refused"], outcomes
        assert [r.code for r in lab.sample_types()] == ["A", "B"]

    def test_confirm_needs_the_catalog_permission_and_the_tenant_is_never_a_parameter(self, client, session, lab):
        viewer = create_user(session, lab.tenant, email="v@alfa.invalid", roles=("viewer",))
        document = doc(item("A", "Uno"))
        plan = preview(client, lab, document).json()
        denied = client.post(f"{IMPORT}confirm", headers=auth_headers(viewer), json={
            "document": document, "selected_indexes": [0], "plan_digest": plan["plan_digest"]})
        assert denied.status_code == 403 and lab.sample_types() == []


# ---------------------------------------------------------------------------
# Direct copy from study types
# ---------------------------------------------------------------------------


class TestCopyFromStudyTypes:
    def copy_preview(self, client, lab, ids):
        return client.post(f"{COPY}preview", json={"study_type_ids": [str(i) for i in ids]}, headers=lab.headers)

    def copy_confirm(self, client, lab, ids, plan, **extra):
        return client.post(f"{COPY}confirm", headers=lab.headers, json={
            "study_type_ids": [str(i) for i in ids], "plan_digest": plan["plan_digest"], **extra})

    def test_copies_only_the_selected_study_types_and_leaves_sources_intact(self, client, session, lab):
        tpl = lab.template()
        hist = lab.study("HIST", "Histopatología", description="Tejido", template=tpl)
        cito = lab.study("CITO", "Citología", active=False)
        lab.study("IHQ", "Inmuno")  # NOT selected
        snapshot_before = [(s.id, s.code, s.name, s.description, s.is_active, s.default_report_template_id)
                           for s in session.exec(select(StudyType).order_by(StudyType.code)).all()]

        plan = self.copy_preview(client, lab, [hist.id, cito.id]).json()
        assert plan["origin"] == "copy" and plan["source_kind"] == "study_types"
        assert [i["status"] for i in plan["items"]] == ["ready", "ready"]
        assert plan["items"][0]["template_reference"] == "mapped"
        assert [w["code"] for w in plan["items"][1]["warnings"]] == ["source_inactive"]
        assert lab.sample_types() == []  # preview writes nothing

        result = self.copy_confirm(client, lab, [hist.id, cito.id], plan)
        assert result.status_code == 200, result.text
        rows = {r.code: r for r in lab.sample_types()}
        assert set(rows) == {"CITO", "HIST"}  # IHQ was not selected
        assert rows["HIST"].id != hist.id and rows["HIST"].tenant_id == lab.tenant.id
        assert (rows["HIST"].name, rows["HIST"].description) == ("Histopatología", "Tejido")
        assert rows["HIST"].default_report_template_id == tpl.id
        assert rows["CITO"].is_active is False
        session.expire_all()
        assert snapshot_before == [(s.id, s.code, s.name, s.description, s.is_active, s.default_report_template_id)
                                   for s in session.exec(select(StudyType).order_by(StudyType.code)).all()]

    def test_orders_and_study_driven_behaviour_are_unchanged(self, client, session, lab):
        from app.models.laboratory import Order
        hist = lab.study("HIST", "Histopatología")
        order = Order(tenant_id=lab.tenant.id, branch_id=lab.branch.id, order_code="HIST-1", study_type_id=hist.id)
        session.add(order)
        session.commit()
        plan = self.copy_preview(client, lab, [hist.id]).json()
        assert self.copy_confirm(client, lab, [hist.id], plan).status_code == 200
        session.expire_all()
        assert session.get(Order, order.id).study_type_id == hist.id
        assert session.get(StudyType, hist.id).is_active is True

    def test_copy_audit_records_the_origin_and_sources(self, client, session, lab):
        hist = lab.study("HIST", "Histopatología")
        plan = self.copy_preview(client, lab, [hist.id]).json()
        self.copy_confirm(client, lab, [hist.id], plan)
        record = session.exec(select(AuditLog).where(AuditLog.action == "SAMPLE_TYPE.COPIED_FROM_STUDY_TYPE")).one()
        assert record.event_metadata["origin"] == "copy"
        assert record.event_metadata["source_id"] == str(hist.id)
        assert record.event_metadata["source_study_type_ids"] == [str(hist.id)]
        summary = session.exec(select(AuditLog).where(AuditLog.action == "SAMPLE_TYPE.TRANSFER_COMPLETED")).one()
        assert summary.event_metadata["origin"] == "copy"

    def test_conflicts_use_the_same_policies_as_import(self, client, lab):
        a, b = lab.study("A", "Uno"), lab.study("B", "Dos")
        lab.sample_type("B", "Ya existe")
        plan = self.copy_preview(client, lab, [a.id, b.id]).json()
        assert [i["status"] for i in plan["items"]] == ["ready", "code_exists"]
        refused = self.copy_confirm(client, lab, [a.id, b.id], plan)
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "conflicts"
        assert [r.code for r in lab.sample_types()] == ["B"]
        done = self.copy_confirm(client, lab, [a.id, b.id], plan, on_conflict="skip").json()
        assert [c["code"] for c in done["created"]] == ["A"]
        assert [s["reason"] for s in done["skipped"]] == ["code_exists"]
        assert {r.code: r.name for r in lab.sample_types()} == {"A": "Uno", "B": "Ya existe"}

    def test_repeating_the_copy_is_safe(self, client, lab):
        a = lab.study("A", "Uno")
        plan = self.copy_preview(client, lab, [a.id]).json()
        assert self.copy_confirm(client, lab, [a.id], plan).status_code == 200
        again = self.copy_confirm(client, lab, [a.id], plan)
        assert again.status_code == 409 and again.json()["detail"]["code"] == "preview_stale"
        assert len(lab.sample_types()) == 1
        fresh = self.copy_preview(client, lab, [a.id]).json()
        assert fresh["items"][0]["status"] == "code_exists"

    def test_selection_rules(self, client, lab, other):
        theirs = other.study("AJENO", "Ajeno")
        mine = lab.study("MIO", "Mío")
        assert self.copy_preview(client, lab, []).status_code == 422
        assert self.copy_preview(client, lab, [uuid.uuid4()]).status_code == 404
        assert self.copy_preview(client, lab, [theirs.id]).status_code == 404  # same answer as unknown
        assert self.copy_preview(client, lab, [mine.id, theirs.id]).status_code == 404
        assert client.post(f"{COPY}preview", json={"study_type_ids": ["nope"]}, headers=lab.headers).status_code == 422
        assert client.post(f"{COPY}preview", json={}, headers=lab.headers).status_code == 422
        assert self.copy_preview(client, lab, [mine.id, mine.id]).json()["total"] == 1  # duplicates collapse
        assert other.sample_types() == [] and lab.sample_types() == []

    def test_same_validation_as_import(self, client, session, lab):
        long = lab.study("LARGO", "Con descripción enorme", description="d" * 2500)
        plan = self.copy_preview(client, lab, [long.id]).json()
        assert plan["items"][0]["status"] == "invalid"
        assert self.copy_confirm(client, lab, [long.id], plan).status_code == 422
        # a plan that is identical to an import of the same data
        ok = lab.study("OK", "Normal", description="x")
        copied = self.copy_preview(client, lab, [ok.id]).json()["items"][0]
        imported = preview(client, lab, doc(item("OK", "Normal", description="x", provenance={"source_id": str(ok.id)}))).json()["items"][0]
        # (the code now exists neither way)
        assert {k: copied[k] for k in ("code", "name", "description", "status", "is_active")} == {
            k: imported[k] for k in ("code", "name", "description", "status", "is_active")}

    def test_copy_needs_the_catalog_permission(self, client, session, lab):
        a = lab.study("A", "Uno")
        viewer = create_user(session, lab.tenant, email="v@alfa.invalid", roles=("viewer",))
        denied = client.post(f"{COPY}confirm", headers=auth_headers(viewer),
                             json={"study_type_ids": [str(a.id)], "plan_digest": "x"})
        assert denied.status_code == 403 and lab.sample_types() == []

    def test_copy_failure_rolls_back_every_record(self, client, lab, monkeypatch):
        ids = [lab.study(c, c).id for c in ("A", "B", "C")]
        plan = self.copy_preview(client, lab, ids).json()
        real, calls = catalog_transfer.record_audit, {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("simulated failure")
            return real(*args, **kwargs)

        monkeypatch.setattr(catalog_transfer, "record_audit", flaky)
        with pytest.raises(RuntimeError):
            self.copy_confirm(client, lab, ids, plan)
        monkeypatch.undo()
        assert lab.sample_types() == []
