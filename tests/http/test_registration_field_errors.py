"""Refused unified registrations name the fields and sample rows to correct (Céluma 1.5, #67).

The web form keeps every value after a refusal and marks what to fix. These
tests pin the server half: the status codes and messages of the existing checks
are unchanged, `detail` carries `{code, message, fields}`, nothing is written,
and the idempotency contract the form relies on to retry without retyping
(ADR 0014, Amendment 1):

- a refused request leaves no receipt, so the corrected payload may reuse the key;
- after a lost response (the server committed), a corrected payload with the
  same key is refused with 409 and the previous payload replays the original
  order, never a second one.

Synthetic data only.
"""
from __future__ import annotations

import uuid

from sqlmodel import select

import pytest

from app.models.laboratory import Order, Sample

from .factories import create_patient, create_sample_type
from .test_sample_type_catalog import UNIFIED, Lab


@pytest.fixture
def lab(session):
    lab = Lab(session, "Registro")
    lab.biopsia = create_sample_type(session, lab.tenant, code="BIO")
    lab.citologia = create_sample_type(session, lab.tenant, code="CIT")
    lab.patient = create_patient(session, lab.tenant, lab.branch)
    base = lab.unified_payload
    lab.unified_payload = lambda samples, **extra: base(samples, **{"patient_id": str(lab.patient.id), **extra})
    return lab


def row(sample_type, **extra) -> dict:
    return {"sample_type_id": str(sample_type.id if hasattr(sample_type, "id") else sample_type),
            "received_at": "2026-11-20", **extra}


def post(client, lab, payload, key=None, headers=None):
    hdrs = dict(headers or lab.headers)
    if key:
        hdrs["Idempotency-Key"] = key
    return client.post(UNIFIED, json=payload, headers=hdrs)


def nothing_created(session) -> bool:
    session.expire_all()
    return session.exec(select(Order)).all() == [] and session.exec(select(Sample)).all() == []


class TestFieldPointers:
    def test_an_inactive_type_names_every_row_that_uses_it(self, client, session, lab):
        off = create_sample_type(session, lab.tenant, code="OFF", is_active=False)
        res = post(client, lab, lab.unified_payload([row(lab.biopsia), row(off), row(lab.citologia), row(off)]))
        assert res.status_code == 400, res.text
        detail = res.json()["detail"]
        assert detail["code"] == "sample_type_inactive"
        assert detail["message"] == "Sample type 'OFF' is inactive and cannot be used for new samples"
        assert detail["fields"] == ["samples.1.sample_type_id", "samples.3.sample_type_id"]
        assert nothing_created(session)

    def test_missing_and_foreign_types_are_indistinguishable(self, client, session, lab):
        other = Lab(session, "Otro")
        foreign = create_sample_type(session, other.tenant, code="AJENO")
        bodies = []
        for bad in (foreign.id, uuid.uuid4()):
            res = post(client, lab, lab.unified_payload([row(lab.biopsia), row(bad)]))
            assert res.status_code == 404, res.text
            bodies.append(res.json())
        assert bodies[0] == bodies[1] == {"detail": {
            "code": "sample_type_not_found", "message": "Sample type not found", "fields": ["samples.1.sample_type_id"],
        }}
        assert nothing_created(session)

    def test_order_level_fields(self, client, session, lab):
        other = Lab(session, "Otro")
        cases = [
            ({"patient_id": str(uuid.uuid4())}, 404, "patient_invalid", ["patient_id"]),
            ({"patient_id": None}, 400, "patient_or_physician_required", ["patient_id", "requesting_physician_ids"]),
            ({"patient_id": None, "requesting_physician_ids": [str(uuid.uuid4())]}, 404,
             "requesting_physician_invalid", ["requesting_physician_ids"]),
            ({"study_type_id": str(other.study_type.id)}, 403, "study_type_not_in_tenant", ["study_type_id"]),
            ({"study_type_id": str(uuid.uuid4())}, 404, "study_type_not_found", ["study_type_id"]),
            ({"branch_id": str(other.branch.id)}, 403, "branch_not_in_tenant", ["branch_id"]),
            ({"report_template_id": str(uuid.uuid4())}, 404, "report_template_not_found", ["report_template_id"]),
        ]
        for extra, status, code, fields in cases:
            res = post(client, lab, lab.unified_payload([row(lab.biopsia)], **extra))
            assert res.status_code == status, (extra, res.text)
            detail = res.json()["detail"]
            assert (detail["code"], detail["fields"]) == (code, fields), (extra, detail)
            assert isinstance(detail["message"], str) and detail["message"]
        assert nothing_created(session)

    def test_a_typed_code_without_permission_names_the_typed_rows(self, client, session, lab):
        assistant = lab.user_with("assistant")
        payload = lab.unified_payload([row(lab.biopsia), row(lab.biopsia, sample_code="MAN-1"), row(lab.citologia)])
        res = post(client, lab, payload, headers=assistant)
        assert res.status_code == 403, res.text
        detail = res.json()["detail"]
        assert detail["code"] == "manual_code_permission_required"
        assert detail["fields"] == ["samples.1.sample_code"]
        assert "lab:enter_manual_code" in detail["message"]
        assert nothing_created(session)

    def test_request_validation_keeps_its_row_locations(self, client, session, lab):
        payload = lab.unified_payload([row(lab.biopsia), {"sample_type_id": str(lab.biopsia.id)}])
        res = post(client, lab, payload)
        assert res.status_code == 422, res.text
        assert ["body", "samples", 1, "received_at"] in [e["loc"] for e in res.json()["detail"]]
        assert nothing_created(session)


class TestRetryWithoutRetyping:
    def test_a_corrected_payload_may_reuse_the_key_of_a_refused_one(self, client, session, lab):
        off = create_sample_type(session, lab.tenant, code="OFF", is_active=False)
        key = f"form-{uuid.uuid4().hex}"
        refused = lab.unified_payload([row(lab.biopsia), row(off), row(lab.citologia)])
        assert post(client, lab, refused, key=key).status_code == 400
        corrected = lab.unified_payload([row(lab.biopsia), row(lab.biopsia), row(lab.citologia)])
        first = post(client, lab, corrected, key=key)
        assert first.status_code == 200, first.text
        assert "Idempotent-Replayed" not in first.headers
        again = post(client, lab, corrected, key=key)
        assert again.status_code == 200 and again.headers.get("Idempotent-Replayed") == "true"
        assert again.json()["order"] == first.json()["order"]
        assert [s["sample_code"] for s in again.json()["samples"]] == [s["sample_code"] for s in first.json()["samples"]]
        # The refused payload is now a different request under a used key.
        assert post(client, lab, refused, key=key).status_code == 409
        session.expire_all()
        assert len(session.exec(select(Order)).all()) == 1 and len(session.exec(select(Sample)).all()) == 3

    def test_after_a_lost_response_a_correction_is_refused_and_the_original_replays(self, client, session, lab):
        key = f"form-{uuid.uuid4().hex}"
        original = lab.unified_payload([row(lab.biopsia), row(lab.citologia), row(lab.biopsia, notes="Fragmento 3")])
        committed = post(client, lab, original, key=key)  # the browser never saw this response
        assert committed.status_code == 200, committed.text
        corrected = lab.unified_payload([row(lab.biopsia), row(lab.citologia), row(lab.citologia, notes="Fragmento 3")])
        refused = post(client, lab, corrected, key=key)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["code"] == "idempotency_key_reused"
        recovered = post(client, lab, original, key=key)
        assert recovered.status_code == 200 and recovered.headers.get("Idempotent-Replayed") == "true"
        assert recovered.json()["order"]["id"] == committed.json()["order"]["id"]
        session.expire_all()
        assert len(session.exec(select(Order)).all()) == 1 and len(session.exec(select(Sample)).all()) == 3
