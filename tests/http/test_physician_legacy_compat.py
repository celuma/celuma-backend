"""Several long physician names versus the legacy `requested_by` varchar(255) (close-out, #16).

The authoritative collection is `order_requesting_physician`; `requested_by` is only a bounded legacy summary.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.services import order_management

from .management_support import LAB, count, physician, register
from .numbering_support import Lab, freeze


@pytest.fixture
def lab(session, monkeypatch):
    freeze(monkeypatch)
    return Lab(session, "Alfa", sample_types=("BX",))


@pytest.fixture
def long_docs(session, lab):
    # ten valid physicians; accents (multi-byte) and long names so the joined text is far over 255 characters
    return [physician(session, lab, f"L{i}", f"Ángel María José Ñandú {i}" + "á" * 20, "Fernández-Villaseñor " + "é" * 25)
            for i in range(10)]


def detail(client, lab, oid):
    return client.get(f"{LAB}/orders/{oid}", headers=lab.headers).json()


def test_registration_with_ten_long_names_keeps_every_name_and_fits_the_legacy_column(client, session, lab, long_docs):
    ids = [str(d.id) for d in long_docs]
    reg = register(client, lab, requesting_physician_ids=ids, requested_by=None)
    body = detail(client, lab, reg["order_id"])
    assert [p["id"] for p in body["requesting_physicians"]] == ids          # nothing lost, order kept
    assert body["requesting_physician_id"] is None                           # still no primary
    legacy = body["requested_by"]
    assert 0 < len(legacy) <= 255
    assert legacy.startswith(order_management.physician_name(long_docs[0]))  # whole names, never cut mid-name
    assert "más" in legacy                                                   # says that more are listed


def test_edit_to_ten_long_names_and_back_to_one(client, session, lab, long_docs):
    reg = register(client, lab, requesting_physician_ids=[str(long_docs[0].id)], requested_by=None)
    res = client.patch(f"{LAB}/orders/{reg['order_id']}", headers=lab.headers,
                       json={"requesting_physician_ids": [str(d.id) for d in long_docs]})
    assert res.status_code == 200, res.text
    assert len(res.json()["requesting_physicians"]) == 10 and len(res.json()["requested_by"]) <= 255
    back = client.patch(f"{LAB}/orders/{reg['order_id']}", headers=lab.headers,
                        json={"requesting_physician_ids": [str(long_docs[3].id)]})
    assert back.json()["requested_by"] == order_management.physician_name(long_docs[3])  # derived text followed


def test_report_metadata_still_has_the_full_names(client, session, lab, long_docs):
    from app.models.laboratory import Order
    from app.services import report_metadata
    reg = register(client, lab, requesting_physician_ids=[str(d.id) for d in long_docs], requested_by=None)
    full = report_metadata.resolve_requesting_physician_name(session, session.get(Order, reg["order_id"]))
    assert full == "; ".join(order_management.physician_name(d) for d in long_docs) and len(full) > 255


def test_an_operator_supplied_legacy_value_is_never_truncated_or_rewritten(client, session, lab, long_docs):
    portal_id = "medico.portal@laboratorio.invalid"
    reg = register(client, lab, requesting_physician_ids=[str(long_docs[0].id)], requested_by=portal_id)
    client.patch(f"{LAB}/orders/{reg['order_id']}", headers=lab.headers,
                 json={"requesting_physician_ids": [str(d.id) for d in long_docs]})
    assert detail(client, lab, reg["order_id"])["requested_by"] == portal_id   # portal match key untouched


def test_an_oversized_operator_value_is_a_validation_error_not_a_database_error(client, lab):
    res = client.post(f"{LAB}/orders/unified", json=lab.unified(requested_by="x" * 256), headers=lab.headers)
    assert res.status_code == 422


def test_removing_every_physician_on_a_patient_order_clears_only_the_derived_text(client, session, lab, long_docs):
    reg = register(client, lab, requesting_physician_ids=[str(long_docs[0].id)], requested_by=None)
    res = client.patch(f"{LAB}/orders/{reg['order_id']}", headers=lab.headers, json={"requesting_physician_ids": []})
    assert res.status_code == 200
    body = res.json()
    assert body["requesting_physicians"] == [] and body["requested_by"] is None and body["requesting_physician_id"] is None


def test_an_inactive_historical_physician_can_stay_on_the_order_while_other_fields_change(client, session, lab, long_docs):
    reg = register(client, lab, requesting_physician_ids=[str(long_docs[0].id), str(long_docs[1].id)], requested_by=None)
    session.execute(text("UPDATE requesting_physician SET is_active = false WHERE id = :i"), {"i": str(long_docs[0].id)})
    session.commit()
    keep = [str(long_docs[0].id), str(long_docs[1].id), str(long_docs[2].id)]
    res = client.patch(f"{LAB}/orders/{reg['order_id']}", headers=lab.headers, json={"requesting_physician_ids": keep})
    assert res.status_code == 200, res.text
    assert [p["id"] for p in res.json()["requesting_physicians"]] == keep
    # but an inactive physician cannot be newly added
    session.execute(text("UPDATE requesting_physician SET is_active = false WHERE id = :i"), {"i": str(long_docs[5].id)})
    session.commit()
    assert client.patch(f"{LAB}/orders/{reg['order_id']}", headers=lab.headers,
                        json={"requesting_physician_ids": keep + [str(long_docs[5].id)]}).status_code == 400


def test_structured_edits_never_grant_or_transfer_portal_access(client, session, lab, long_docs):
    from .factories import auth_headers, create_user
    portal = create_user(session, lab.tenant, email="ana@laboratorio.invalid", roles=("superuser",))
    reg = register(client, lab, requesting_physician_ids=[str(long_docs[0].id)], requested_by="otro@laboratorio.invalid")
    client.patch(f"{LAB}/orders/{reg['order_id']}", headers=lab.headers, json={"requesting_physician_ids": [str(long_docs[1].id)]})
    listed = client.get("/api/v1/portal/physician/orders", headers=auth_headers(portal)).json()
    assert listed == []
    assert count(session, "SELECT count(*) FROM notification") == 0


def test_derivation_unit():
    class P:  # minimal stand-in
        def __init__(self, n): self.full_name, self.first_name, self.last_name = n, "", ""
    assert order_management.derive_requested_by([]) is None
    assert order_management.derive_requested_by([P("A"), P("B")]) == "A; B"
    many = [P(("N" * 60) + str(i)) for i in range(6)]
    out = order_management.derive_requested_by(many)
    assert len(out) <= 255 and out.startswith("N" * 60 + "0") and out.endswith("más")
    assert len(order_management.derive_requested_by([P("Z" * 400)])) <= 255
