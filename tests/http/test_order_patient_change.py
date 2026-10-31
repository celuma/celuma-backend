"""Changing the patient of an order registered on the wrong one (basic order edit), over real HTTP + PostgreSQL."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.models.enums import OrderStatus

from .factories import create_patient
from .management_support import LAB, count, physician, register, set_status
from .numbering_support import Lab, freeze


@pytest.fixture
def lab(session, monkeypatch):
    freeze(monkeypatch)
    return Lab(session, "Alfa", sample_types=("BX",))


@pytest.fixture
def other_patient(session, lab):
    return create_patient(session, lab.tenant, lab.branch, patient_code="PAT-2", first_name="Otra", last_name="Persona")


def patch(client, lab, order_id, body, headers=None):
    return client.patch(f"{LAB}/orders/{order_id}", json=body, headers=headers or lab.headers)


def detail(client, lab, order_id):
    return client.get(f"{LAB}/orders/{order_id}/full", headers=lab.headers).json()


class TestPatientChange:
    def test_moves_the_order_samples_and_history_to_the_new_patient(self, client, session, lab, other_patient):
        reg = register(client, lab)
        oid = reg["order_id"]
        res = patch(client, lab, oid, {"patient_id": str(other_patient.id)})
        assert res.status_code == 200, res.text
        full = detail(client, lab, oid)
        assert full["patient"]["id"] == str(other_patient.id)
        assert full["order"]["order_code"] == reg["order_code"]
        assert [s["sample_code"] for s in full["samples"]] == reg["sample_codes"]
        new_list = client.get(f"{LAB}/patients/{other_patient.id}/orders", headers=lab.headers).json()["orders"]
        old_list = client.get(f"{LAB}/patients/{lab.patient.id}/orders", headers=lab.headers).json()["orders"]
        assert [o["id"] for o in new_list] == [oid] and old_list == []

    def test_audited_with_old_and_new_patient(self, client, session, lab, other_patient):
        reg = register(client, lab)
        patch(client, lab, reg["order_id"], {"patient_id": str(other_patient.id)})
        audit = session.execute(text("SELECT old_values, new_values FROM audit_log WHERE action = 'ORDER.UPDATED'")).one()
        assert audit.old_values == {"patient_id": str(lab.patient.id)}
        assert audit.new_values == {"patient_id": str(other_patient.id)}

    def test_same_patient_writes_nothing(self, client, session, lab):
        reg = register(client, lab)
        assert patch(client, lab, reg["order_id"], {"patient_id": str(lab.patient.id)}).status_code == 200
        assert count(session, "SELECT count(*) FROM audit_log WHERE action = 'ORDER.UPDATED'") == 0

    def test_foreign_unknown_and_malformed_patients_are_refused(self, client, session, lab):
        reg = register(client, lab)
        foreign = Lab(session, "Beta").patient
        assert patch(client, lab, reg["order_id"], {"patient_id": str(foreign.id)}).status_code == 404
        assert patch(client, lab, reg["order_id"], {"patient_id": "00000000-0000-0000-0000-000000000009"}).status_code == 404
        assert patch(client, lab, reg["order_id"], {"patient_id": "no-es-uuid"}).status_code == 422
        assert detail(client, lab, reg["order_id"])["patient"]["id"] == str(lab.patient.id)

    def test_the_patient_can_be_removed_only_while_a_physician_remains(self, client, session, lab):
        reg = register(client, lab)
        oid = reg["order_id"]
        assert patch(client, lab, oid, {"patient_id": None}).status_code == 400
        doc = physician(session, lab)
        res = patch(client, lab, oid, {"patient_id": None, "requesting_physician_ids": [str(doc.id)]})
        assert res.status_code == 200, res.text
        assert detail(client, lab, oid)["patient"] is None

    def test_refused_from_review_onward(self, client, session, lab, other_patient):
        reg = register(client, lab)
        set_status(session, reg["order_id"], OrderStatus.REVIEW)
        assert patch(client, lab, reg["order_id"], {"patient_id": str(other_patient.id)}).status_code == 409

    def test_needs_update_order_permission(self, client, session, lab, other_patient):
        reg = register(client, lab)
        res = patch(client, lab, reg["order_id"], {"patient_id": str(other_patient.id)}, headers=lab.headers_for("reviewer"))
        assert res.status_code == 403
