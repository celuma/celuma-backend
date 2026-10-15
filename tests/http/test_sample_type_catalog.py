"""Sample-type catalog (Céluma 1.4, issue #17): administration, the required
type on every creation route, legacy "Sin tipo" samples, and the list/detail
representation.

Contract: `celuma-engineering/specs/laboratory/sample-types.md`. Synthetic data
only.
"""
from __future__ import annotations

import pathlib
import re
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.audit import AuditLog
from app.models.laboratory import Order, Sample
from app.models.report import ReportTemplate
from app.models.sample_type import SampleType
from app.models.study_type import StudyType

from .factories import (
    auth_headers,
    create_branch,
    create_order,
    create_sample,
    create_sample_type,
    create_tenant,
    create_user,
)

BASE = "/api/v1/sample-types/"
SAMPLES = "/api/v1/laboratory/samples/"
UNIFIED = "/api/v1/laboratory/orders/unified"


class Lab:
    def __init__(self, session: Session, name="Alfa", *, roles=("superuser",)):
        self.session = session
        self.tenant = create_tenant(session, name=name)
        self.branch = create_branch(session, self.tenant)
        self.user = create_user(session, self.tenant, email=f"u@{name.lower()}.invalid", roles=roles)
        self.headers = auth_headers(self.user)
        self.study_type = StudyType(tenant_id=self.tenant.id, code="HIST", name="Histopatología")
        session.add(self.study_type)
        session.commit()
        session.refresh(self.study_type)

    def user_with(self, role: str) -> dict:
        user = create_user(self.session, self.tenant, email=f"{role}@{self.tenant.id}.invalid", roles=(role,))
        return auth_headers(user)

    def order(self, code="ORD-1") -> Order:
        return create_order(self.session, self.tenant, self.branch, order_code=code)

    def sample_payload(self, order: Order, sample_type_id, code="S-1", **extra) -> dict:
        return {
            "tenant_id": str(self.tenant.id), "branch_id": str(self.branch.id), "order_id": str(order.id),
            "sample_code": code, "sample_type_id": str(sample_type_id), **extra,
        }

    def unified_payload(self, samples: list[dict], **extra) -> dict:
        return {
            "tenant_id": str(self.tenant.id), "branch_id": str(self.branch.id),
            "study_type_id": str(self.study_type.id), "requested_by": "Dr. Sintético",
            "samples": samples, **extra,
        }


@pytest.fixture
def lab(session):
    return Lab(session, "Alfa")


@pytest.fixture
def other(session):
    return Lab(session, "Beta")


def create(client, lab, **body):
    payload = {"code": "BIOPSIA", "name": "Biopsia", **body}
    return client.post(BASE, json=payload, headers=lab.headers)


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------


class TestAdministration:
    def test_create_list_get_update_deactivate(self, client, lab):
        created = create(client, lab, code=" biopsia ", description="Tejido")
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["code"] == "BIOPSIA" and body["is_active"] is True
        assert body["tenant_id"] == str(lab.tenant.id)

        assert [r["id"] for r in client.get(BASE, headers=lab.headers).json()["sample_types"]] == [body["id"]]
        assert client.get(f"{BASE}{body['id']}", headers=lab.headers).json()["name"] == "Biopsia"

        updated = client.put(f"{BASE}{body['id']}", json={"name": "Biopsia de piel"}, headers=lab.headers)
        assert updated.json()["name"] == "Biopsia de piel" and updated.json()["code"] == "BIOPSIA"

        assert client.delete(f"{BASE}{body['id']}", headers=lab.headers).status_code == 200
        assert client.get(BASE, headers=lab.headers).json()["sample_types"] == []
        everything = client.get(BASE, params={"active_only": "false"}, headers=lab.headers).json()["sample_types"]
        assert [r["is_active"] for r in everything] == [False]
        reactivated = client.put(f"{BASE}{body['id']}", json={"is_active": True}, headers=lab.headers)
        assert reactivated.json()["is_active"] is True

    def test_validation_matches_study_types(self, client, lab):
        assert create(client, lab, code="  ").status_code == 422
        assert create(client, lab, code="X" * 51).status_code == 422
        assert create(client, lab, name="").status_code == 422
        assert create(client, lab, name="N" * 256).status_code == 422
        assert create(client, lab, description="d" * 2001).status_code == 422

    def test_codes_are_unique_per_tenant_and_case_insensitive(self, client, lab, other):
        assert create(client, lab).status_code == 200
        assert create(client, lab, code="biopsia", name="Otra").status_code == 400
        # Another laboratory may use the same code.
        assert create(client, other).status_code == 200
        # Renaming into a taken code is refused, keeping one's own code is not.
        second = create(client, lab, code="SANGRE", name="Sangre").json()
        assert client.put(f"{BASE}{second['id']}", json={"code": "biopsia"}, headers=lab.headers).status_code == 400
        assert client.put(f"{BASE}{second['id']}", json={"code": "sangre"}, headers=lab.headers).status_code == 200

    def test_code_namespace_is_independent_from_study_types(self, client, lab):
        # `HIST` already exists as a STUDY type of this lab.
        assert create(client, lab, code="HIST", name="Histología").status_code == 200

    def test_database_enforces_the_unique_code(self, session, lab):
        create_sample_type(session, lab.tenant, code="DUP")
        with pytest.raises(IntegrityError):
            session.add(SampleType(tenant_id=lab.tenant.id, code="DUP", name="Dup"))
            session.commit()
        session.rollback()

    def test_default_template_reference_is_validated_against_the_tenant(self, client, session, lab, other):
        mine = ReportTemplate(tenant_id=lab.tenant.id, name="Mi plantilla")
        theirs = ReportTemplate(tenant_id=other.tenant.id, name="Ajena")
        session.add_all([mine, theirs])
        session.commit()
        ok = create(client, lab, default_report_template_id=str(mine.id))
        assert ok.status_code == 200 and ok.json()["default_template"]["name"] == "Mi plantilla"
        assert create(client, lab, code="X", default_report_template_id=str(theirs.id)).status_code == 403
        assert create(client, lab, code="Y", default_report_template_id=str(uuid.uuid4())).status_code == 404

    def test_a_sample_type_template_never_drives_report_defaults(self, client, session, lab):
        """Study-driven report defaults are untouched: the mirrored column has no effect."""
        study_tpl = ReportTemplate(tenant_id=lab.tenant.id, name="De estudio")
        sample_tpl = ReportTemplate(tenant_id=lab.tenant.id, name="De muestra")
        session.add_all([study_tpl, sample_tpl])
        session.commit()
        lab.study_type.default_report_template_id = study_tpl.id
        session.add(lab.study_type)
        session.commit()
        st = create(client, lab, default_report_template_id=str(sample_tpl.id)).json()
        order = lab.order()
        assert client.post(SAMPLES, json=lab.sample_payload(order, st["id"]), headers=lab.headers).status_code == 200
        defaults = client.get(f"/api/v1/study-types/{lab.study_type.id}/report-defaults", headers=lab.headers).json()
        assert defaults["template_id"] == str(study_tpl.id)


class TestPermissionsAndIsolation:
    def test_reading_needs_lab_read_and_changes_need_manage_catalog(self, client, session, lab):
        st = create(client, lab).json()
        viewer = lab.user_with("viewer")
        assistant = lab.user_with("assistant")
        for headers in (viewer, assistant):
            assert client.get(BASE, headers=headers).status_code == 200
            assert client.get(f"{BASE}{st['id']}", headers=headers).status_code == 200
            assert create(client, type("L", (), {"headers": headers}), code="Z").status_code == 403
            assert client.put(f"{BASE}{st['id']}", json={"name": "x"}, headers=headers).status_code == 403
            assert client.delete(f"{BASE}{st['id']}", headers=headers).status_code == 403
            assert client.get(f"{BASE}export", headers=headers).status_code == 403
            assert client.post(f"{BASE}import/preview", json={"document": {}}, headers=headers).status_code == 403
            assert client.post(f"{BASE}copy-from-study-types/preview",
                               json={"study_type_ids": [str(lab.study_type.id)]}, headers=headers).status_code == 403
        assert client.get(f"{BASE}export", headers=lab.headers).status_code == 200

    def test_unauthenticated_requests_are_refused(self, client):
        assert client.get(BASE).status_code in (401, 403)
        assert client.post(BASE, json={"code": "A", "name": "A"}).status_code in (401, 403)

    def test_another_tenants_types_are_invisible(self, client, lab, other):
        theirs = create(client, other, code="SECRETO", name="Secreto").json()
        assert client.get(BASE, params={"active_only": "false"}, headers=lab.headers).json()["sample_types"] == []
        assert client.get(f"{BASE}{theirs['id']}", headers=lab.headers).status_code == 404
        assert client.put(f"{BASE}{theirs['id']}", json={"name": "x"}, headers=lab.headers).status_code == 404
        assert client.delete(f"{BASE}{theirs['id']}", headers=lab.headers).status_code == 404

    def test_hard_delete_only_when_unreferenced(self, client, session, lab):
        used = create(client, lab, code="USADO").json()
        free = create(client, lab, code="LIBRE").json()
        client.post(SAMPLES, json=lab.sample_payload(lab.order(), used["id"]), headers=lab.headers)
        assert client.delete(f"{BASE}{used['id']}", params={"hard_delete": "true"}, headers=lab.headers).status_code == 409
        assert client.delete(f"{BASE}{free['id']}", params={"hard_delete": "true"}, headers=lab.headers).status_code == 200
        assert session.get(SampleType, uuid.UUID(used["id"])) is not None
        assert session.get(SampleType, uuid.UUID(free["id"])) is None

    def test_changes_are_audited(self, client, session, lab):
        st = create(client, lab).json()
        client.put(f"{BASE}{st['id']}", json={"name": "Nueva"}, headers=lab.headers)
        client.delete(f"{BASE}{st['id']}", headers=lab.headers)
        actions = [a.action for a in session.exec(
            select(AuditLog).where(AuditLog.tenant_id == lab.tenant.id).order_by(AuditLog.created_at)).all()]
        assert actions == ["SAMPLE_TYPE.CREATED", "SAMPLE_TYPE.UPDATED", "SAMPLE_TYPE.DEACTIVATED"]
        updated = session.exec(select(AuditLog).where(AuditLog.action == "SAMPLE_TYPE.UPDATED")).one()
        assert updated.old_values["name"] == "Biopsia" and updated.new_values["name"] == "Nueva"
        assert updated.actor_user_id == lab.user.id


# ---------------------------------------------------------------------------
# A NEW sample must choose an active type of its own tenant, on every route
# ---------------------------------------------------------------------------


class TestRequiredTypeOnCreation:
    def test_single_sample_requires_a_type(self, client, session, lab, other):
        order = lab.order()
        active = create_sample_type(session, lab.tenant, code="ACT")
        inactive = create_sample_type(session, lab.tenant, code="OFF", is_active=False)
        foreign = create_sample_type(session, other.tenant, code="AJENO")
        base = lab.sample_payload(order, active.id)
        post = lambda body: client.post(SAMPLES, json=body, headers=lab.headers)  # noqa: E731

        missing = {k: v for k, v in base.items() if k != "sample_type_id"}
        assert post(missing).status_code == 422
        assert post({**base, "sample_type_id": None}).status_code == 422
        legacy = post({**missing, "type": "BIOPSIA"})
        assert legacy.status_code == 422 and "sample_type_id" in legacy.text
        assert post({**base, "type": "BIOPSIA"}).status_code == 422  # retired field is refused even with a valid id
        assert post({**base, "sample_type_id": str(uuid.uuid4())}).status_code == 404
        assert post({**base, "sample_type_id": "not-a-uuid"}).status_code == 404
        assert post({**base, "sample_type_id": str(foreign.id)}).status_code == 404
        refused = post({**base, "sample_type_id": str(inactive.id)})
        assert refused.status_code == 400 and "inactive" in refused.text
        session.expire_all()
        assert session.exec(select(Sample)).all() == []

        ok = post(base)
        assert ok.status_code == 200, ok.text
        assert ok.json()["sample_type"] == {"id": str(active.id), "code": "ACT", "name": "Act", "is_active": True}
        assert "type" not in ok.json()
        assert session.exec(select(Sample)).one().sample_type_id == active.id

    def test_unified_registration_requires_a_type_for_every_sample(self, client, session, lab, other):
        active = create_sample_type(session, lab.tenant, code="ACT")
        inactive = create_sample_type(session, lab.tenant, code="OFF", is_active=False)
        foreign = create_sample_type(session, other.tenant, code="AJENO")
        patient = client.post("/api/v1/patients/", json={
            "tenant_id": str(lab.tenant.id), "branch_id": str(lab.branch.id),
            "first_name": "Ana", "last_name": "Sintética", "sex": "F"}, headers=lab.headers).json()

        def post(samples):
            return client.post(UNIFIED, json=lab.unified_payload(samples, patient_id=patient["id"]),
                               headers=lab.headers)

        good = {"sample_code": "A-1", "sample_type_id": str(active.id)}
        for bad in (
            {"sample_code": "A-2"},
            {"sample_code": "A-2", "sample_type_id": None},
            {"sample_code": "A-2", "type": "BIOPSIA"},
            {"sample_code": "A-2", "sample_type_id": str(uuid.uuid4())},
            {"sample_code": "A-2", "sample_type_id": str(foreign.id)},
            {"sample_code": "A-2", "sample_type_id": str(inactive.id)},
        ):
            # One good sample next to a bad one: NOTHING is created.
            response = post([good, bad])
            assert response.status_code in (400, 404, 422), (bad, response.text)
        session.expire_all()
        assert session.exec(select(Order)).all() == [] and session.exec(select(Sample)).all() == []

        ok = post([good, {"sample_code": "A-3", "sample_type_id": str(active.id)}])
        assert ok.status_code == 200, ok.text
        assert [s["sample_type"]["code"] for s in ok.json()["samples"]] == ["ACT", "ACT"]
        assert {s.sample_type_id for s in session.exec(select(Sample)).all()} == {active.id}

    def test_created_event_records_the_type_name_and_id(self, client, session, lab):
        from app.models.events import OrderEvent
        st = create_sample_type(session, lab.tenant, code="ACT", name="Activa")
        client.post(SAMPLES, json=lab.sample_payload(lab.order(), st.id), headers=lab.headers)
        event = session.exec(select(OrderEvent).where(OrderEvent.event_type == "SAMPLE_CREATED")).one()
        assert event.event_metadata["sample_type"] == "Activa"
        assert event.event_metadata["sample_type_id"] == str(st.id)

    def test_no_other_code_path_constructs_samples(self):
        """Structural guard: `Sample(` is built only by the two routes that
        validate the type. A new creation route must call
        `require_active_sample_types` and be added here deliberately."""
        app_dir = pathlib.Path(__file__).resolve().parents[2] / "app"
        sites = {}
        for path in app_dir.rglob("*.py"):
            count = len(re.findall(r"(?<!class )(?<![A-Za-z_])Sample\(", path.read_text(encoding="utf-8")))
            if count:
                sites[str(path.relative_to(app_dir))] = count
        assert sites == {"api/v1/laboratory.py": 2}, sites
        source = (app_dir / "api/v1/laboratory.py").read_text(encoding="utf-8")
        assert source.count("require_active_sample_types(") == 2


# ---------------------------------------------------------------------------
# Legacy samples and the list / detail representation
# ---------------------------------------------------------------------------


class TestRepresentation:
    def test_legacy_sample_is_untyped_everywhere(self, client, session, lab):
        order = lab.order()
        legacy = create_sample(session, lab.tenant, lab.branch, order, sample_code="OLD-1")
        listed = client.get(SAMPLES, headers=lab.headers).json()["samples"]
        assert listed[0]["sample_type"] is None and "type" not in listed[0]
        detail = client.get(f"{SAMPLES}{legacy.id}", headers=lab.headers).json()
        assert detail["sample_type"] is None
        full = client.get(f"/api/v1/laboratory/orders/{order.id}/full", headers=lab.headers).json()
        assert full["samples"][0]["sample_type"] is None

    def test_deactivated_and_renamed_types_stay_readable(self, client, session, lab):
        order = lab.order()
        st = create_sample_type(session, lab.tenant, code="TEJ", name="Tejido")
        sample = create_sample(session, lab.tenant, lab.branch, order, sample_type=st)
        client.delete(f"{BASE}{st.id}", headers=lab.headers)
        client.put(f"{BASE}{st.id}", json={"name": "Tejido fresco"}, headers=lab.headers)
        detail = client.get(f"{SAMPLES}{sample.id}", headers=lab.headers).json()["sample_type"]
        assert detail == {"id": str(st.id), "code": "TEJ", "name": "Tejido fresco", "is_active": False}
        assert client.get(SAMPLES, headers=lab.headers).json()["samples"][0]["sample_type"]["is_active"] is False
        # but it cannot be chosen for a new sample
        refused = client.post(SAMPLES, json=lab.sample_payload(order, st.id, code="NEW"), headers=lab.headers)
        assert refused.status_code == 400

    def test_state_and_notes_routes_keep_the_type(self, client, session, lab):
        st = create_sample_type(session, lab.tenant)
        sample = create_sample(session, lab.tenant, lab.branch, lab.order(), sample_type=st)
        moved = client.patch(f"{SAMPLES}{sample.id}/state", json={"state": "PROCESSING"}, headers=lab.headers)
        assert moved.status_code == 200 and moved.json()["sample_type"]["id"] == str(st.id)
        noted = client.patch(f"{SAMPLES}{sample.id}/notes", json={"notes": "n"}, headers=lab.headers)
        assert noted.json()["sample_type"]["id"] == str(st.id)


class TestListsFiltersAndSearch:
    @pytest.fixture
    def data(self, session, lab):
        order = lab.order()
        types = {c: create_sample_type(session, lab.tenant, code=c, name=n)
                 for c, n in (("BIO", "Biopsia"), ("SAN", "Sangre"), ("OLD", "Tipo retirado"), ("UNU", "Sin uso"))}
        types["OLD"].is_active = False
        session.add(types["OLD"])
        session.commit()
        n = 0
        for code, count in (("BIO", 22), ("SAN", 6), ("OLD", 3), (None, 9)):
            for _ in range(count):
                n += 1
                create_sample(session, lab.tenant, lab.branch, order, sample_code=f"S{n:03d}",
                              sample_type=types.get(code))
        return types

    def codes(self, client, lab, **params):
        body = client.get(SAMPLES, params=params, headers=lab.headers)
        assert body.status_code == 200, body.text
        return body.json()

    def test_filter_by_type_counts_the_whole_dataset_before_paging(self, client, lab, data):
        body = self.codes(client, lab, page=1, page_size=5, sample_type_id=str(data["BIO"].id))
        assert body["total"] == 22 and len(body["samples"]) == 5
        last = self.codes(client, lab, page=5, page_size=5, sample_type_id=str(data["BIO"].id))
        assert len(last["samples"]) == 2

    def test_untyped_filter_and_combinations(self, client, lab, data):
        assert self.codes(client, lab, page=1, sample_type_id="none")["total"] == 9
        mixed = self.codes(client, lab, page=1, sample_type_id=["none", str(data["SAN"].id)])
        assert mixed["total"] == 15
        both = self.codes(client, lab, page=1, sample_type_id=f"none,{data['OLD'].id}")
        assert both["total"] == 12
        assert self.codes(client, lab, page=1)["total"] == 40
        assert len(self.codes(client, lab)["samples"]) == 40  # legacy unpaged mode still works

    def test_inactive_referenced_type_remains_filterable(self, client, lab, data):
        assert self.codes(client, lab, page=1, sample_type_id=str(data["OLD"].id))["total"] == 3

    def test_invalid_and_foreign_filter_values(self, client, lab, other, data):
        assert client.get(SAMPLES, params={"page": 1, "sample_type_id": "x"}, headers=lab.headers).status_code == 422
        retired = client.get(SAMPLES, params={"page": 1, "type": "BIOPSIA"}, headers=lab.headers)
        assert retired.status_code == 422 and "sample_type_id" in retired.text
        foreign = uuid.uuid4()
        assert self.codes(client, lab, page=1, sample_type_id=str(foreign))["total"] == 0

    def test_filter_options_cover_every_sample_not_a_page(self, client, lab, data):
        options = client.get(f"{SAMPLES}filter-options", headers=lab.headers).json()
        assert options["has_untyped_samples"] is True
        assert "types" not in options
        by_value = {o["value"]: o["text"] for o in options["sample_types"]}
        assert by_value == {
            str(data["BIO"].id): "Biopsia", str(data["SAN"].id): "Sangre",
            str(data["OLD"].id): "Tipo retirado (inactivo)",
        }  # "Sin uso" has no samples; legacy has no catalog row

    def test_filter_options_without_legacy_samples(self, client, session, lab):
        st = create_sample_type(session, lab.tenant)
        create_sample(session, lab.tenant, lab.branch, lab.order(), sample_type=st)
        options = client.get(f"{SAMPLES}filter-options", headers=lab.headers).json()
        assert options["has_untyped_samples"] is False

    def test_tenant_isolation_of_options_and_filters(self, client, session, lab, other, data):
        theirs = create_sample_type(session, other.tenant, code="BIO", name="Biopsia ajena")
        create_sample(session, other.tenant, other.branch, other.order(), sample_type=theirs)
        options = client.get(f"{SAMPLES}filter-options", headers=lab.headers).json()
        assert str(theirs.id) not in {o["value"] for o in options["sample_types"]}
        assert self.codes(client, lab, page=1, sample_type_id=str(theirs.id))["total"] == 0

    def test_search_matches_type_name_and_code(self, client, lab, data):
        assert self.codes(client, lab, page=1, q="Sangre")["total"] == 6
        assert self.codes(client, lab, page=1, q="biosia")["total"] == 22  # typo tolerance on the name
        assert self.codes(client, lab, page=1, q="retirado")["total"] == 3
        assert self.codes(client, lab, page=1, q="SAN")["total"] >= 6
        # legacy rows are found by their other fields, not by a type
        assert self.codes(client, lab, page=1, q="S001")["total"] == 1
