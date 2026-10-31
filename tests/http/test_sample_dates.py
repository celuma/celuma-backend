"""Post-registration receipt/collection date correction, over real HTTP + PostgreSQL."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.models.enums import OrderStatus

from .management_support import LAB, count, register, set_status
from .numbering_support import Lab, configure, freeze


@pytest.fixture
def lab(session, monkeypatch):
    freeze(monkeypatch)
    return Lab(session, "Alfa", sample_types=("BX", "TJ"))


def patch(client, lab, sample_id, body, headers=None):
    return client.patch(f"{LAB}/samples/{sample_id}/dates", json=body, headers=headers or lab.headers)


def detail(client, lab, sample_id):
    return client.get(f"{LAB}/samples/{sample_id}", headers=lab.headers).json()


def received_day(client, lab, sample_id):
    return detail(client, lab, sample_id)["received_at"][:10]


class TestDates:
    def test_moves_within_the_numbering_month_without_touching_the_code(self, client, session, lab):
        reg = register(client, lab)
        sid = reg["sample_ids"][0]
        before = detail(client, lab, sid)
        month = before["received_at"][:7]
        ledger = count(session, "SELECT count(*) FROM numbering_allocation")
        res = patch(client, lab, sid, {"received_at": f"{month}-02", "collected_at": f"{month}-01", "reason": "fecha mal capturada"})
        assert res.status_code == 200, res.text
        after = detail(client, lab, sid)
        assert after["received_at"][:10] == f"{month}-02" and after["collected_at"][:10] == f"{month}-01"
        assert after["sample_code"] == before["sample_code"]
        assert count(session, "SELECT count(*) FROM numbering_allocation") == ledger
        assert count(session, "SELECT count(*) FROM numbering_allocation") == count(session, "SELECT count(*) FROM numbering_allocation")

    def test_audit_trail_has_actor_reason_old_and_new(self, client, session, lab):
        reg = register(client, lab)
        sid = reg["sample_ids"][0]
        month = detail(client, lab, sid)["received_at"][:7]
        patch(client, lab, sid, {"received_at": f"{month}-02", "reason": "se tecleó mal"})
        audit = session.execute(text(
            "SELECT old_values, new_values, event_metadata FROM audit_log WHERE action = 'SAMPLE.DATES_UPDATED'")).one()
        assert list(audit.new_values) == ["received_at"] and audit.new_values["received_at"].startswith(f"{month}-02")
        assert audit.event_metadata["reason"] == "se tecleó mal"
        event = session.execute(text(
            "SELECT event_metadata FROM order_event WHERE event_type = 'ORDER_UPDATED' AND sample_id = :s"), {"s": sid}).scalar_one()
        assert event["changed_fields"] == ["received_at"] and event["reason"] == "se tecleó mal"

    def test_legacy_numbering_moves_the_month_and_keeps_the_code(self, client, session, lab):
        reg = register(client, lab)  # numbering not activated: legacy codes do not encode a month
        sid = reg["sample_ids"][0]
        res = patch(client, lab, sid, {"received_at": "2026-09-20", "reason": "otro mes"})
        assert res.status_code == 200, res.text
        assert res.json()["old_code"] is None and res.json()["sample_code"] == reg["sample_codes"][0]
        assert received_day(client, lab, sid) == "2026-09-20"
        state = client.get(f"{LAB}/samples/{sid}/management", headers=lab.headers).json()
        assert state["code_follows_receipt_month"] is False

    def test_collection_date_can_be_cleared_and_receipt_cannot(self, client, lab):
        reg = register(client, lab)
        sid = reg["sample_ids"][0]
        month = detail(client, lab, sid)["received_at"][:7]
        patch(client, lab, sid, {"collected_at": f"{month}-01", "reason": "dato"})
        assert patch(client, lab, sid, {"collected_at": None, "reason": "se quita"}).status_code == 200
        assert detail(client, lab, sid)["collected_at"] is None
        res = patch(client, lab, sid, {"received_at": None, "reason": "no se puede"})
        assert res.status_code == 422 and res.json()["detail"]["code"] == "sample_receipt_required"

    def test_unchanged_is_a_safe_noop(self, client, session, lab):
        reg = register(client, lab)
        sid = reg["sample_ids"][0]
        day = received_day(client, lab, sid)
        res = patch(client, lab, sid, {"received_at": day, "reason": "igual"})
        assert res.status_code == 200 and res.json()["unchanged"] is True
        assert count(session, "SELECT count(*) FROM audit_log WHERE action = 'SAMPLE.DATES_UPDATED'") == 0

    @pytest.mark.parametrize("body", [{"reason": "sin campos"}, {"received_at": "no es fecha", "reason": "abc"}, {"received_at": 20260101, "reason": "abc"}])
    def test_bad_input_is_422(self, client, lab, body):
        reg = register(client, lab)
        assert patch(client, lab, reg["sample_ids"][0], body).status_code == 422

    @pytest.mark.parametrize("reason", ["", " ab ", "x" * 1001])
    def test_a_reason_is_required(self, client, lab, reason):
        reg = register(client, lab)
        sid = reg["sample_ids"][0]
        day = received_day(client, lab, sid)
        assert patch(client, lab, sid, {"collected_at": day, "reason": reason}).status_code == 422

    def test_permission_and_tenant_isolation(self, client, session, lab):
        reg = register(client, lab)
        sid = reg["sample_ids"][0]
        day = received_day(client, lab, sid)
        body = {"collected_at": day, "reason": "dato"}
        assert patch(client, lab, sid, body, headers=lab.headers_for("reviewer")).status_code == 403
        assert patch(client, lab, sid, body, headers=lab.headers_for("lab_tech")).status_code == 200
        other = Lab(session, "Beta")
        assert patch(client, lab, sid, body, headers=other.headers).status_code == 404

    def test_closed_lifecycle_window_blocks_and_the_state_explains_it(self, client, session, lab):
        reg = register(client, lab)
        sid = reg["sample_ids"][0]
        state = client.get(f"{LAB}/samples/{sid}/management", headers=lab.headers).json()
        assert state["dates"]["allowed"] is True and state["dates"]["permitted"] is True
        set_status(session, reg["order_id"], OrderStatus.REVIEW)
        res = patch(client, lab, sid, {"collected_at": received_day(client, lab, sid), "reason": "tarde"})
        assert res.status_code == 409
        state = client.get(f"{LAB}/samples/{sid}/management", headers=lab.headers).json()
        assert state["dates"]["allowed"] is False and state["dates"]["blockers"]


class TestMonthlyRegeneration:
    """Active monthly numbering (`{BASE}{SEQ}{MM}{YY}`): the receipt month is part of the code."""

    @pytest.fixture
    def monthly(self, session, lab):
        configure(session, lab)
        return lab

    def test_a_new_month_regenerates_the_code_and_retires_the_old_one(self, client, session, monthly):
        lab = monthly
        reg = register(client, lab, ("BX", "BX"))
        sid, other = reg["sample_ids"]
        old = reg["sample_codes"][0]
        assert old == "BX011026"
        state = client.get(f"{LAB}/samples/{sid}/management", headers=lab.headers).json()
        assert state["code_follows_receipt_month"] is True
        ledger = count(session, "SELECT count(*) FROM numbering_allocation")

        res = patch(client, lab, sid, {"received_at": "2026-09-28", "reason": "la muestra llegó en septiembre"})
        assert res.status_code == 200, res.text
        assert res.json()["old_code"] == old and res.json()["sample_code"] == "BX010926"
        after = detail(client, lab, sid)
        assert after["sample_code"] == "BX010926" and after["received_at"][:10] == "2026-09-28"
        # The other sample and the October counter are untouched (a gap stays; nothing is renumbered).
        assert detail(client, lab, other)["sample_code"] == reg["sample_codes"][1]
        assert count(session, "SELECT count(*) FROM numbering_allocation") == ledger + 1
        assert count(session, "SELECT last_value FROM numbering_counter WHERE entity_kind = 'sample' AND period_month = 10") == 2
        # The old code is retired like a correction, with the reason, and never comes back.
        row = session.execute(text("SELECT old_code, new_code, reason FROM sample_code_correction")).one()
        assert (row.old_code, row.new_code, row.reason) == (old, "BX010926", "la muestra llegó en septiembre")
        assert session.execute(text(
            "SELECT event_metadata FROM order_event WHERE event_type = 'SAMPLE_CODE_CORRECTED'")).scalar_one()["regenerated"] is True
        assert count(session, "SELECT count(*) FROM audit_log WHERE action = 'SAMPLE.CODE_REGENERATED'") == 1
        nxt = register(client, lab, ("BX",))
        assert nxt["sample_codes"] == ["BX031026"]

    def test_the_same_month_keeps_the_code(self, client, session, monthly):
        reg = register(client, monthly)
        res = patch(client, monthly, reg["sample_ids"][0], {"received_at": "2026-10-02", "reason": "otro día"})
        assert res.status_code == 200 and res.json()["old_code"] is None
        assert res.json()["sample_code"] == reg["sample_codes"][0]

    def test_a_corrected_code_does_not_follow_the_month(self, client, session, monthly):
        reg = register(client, monthly)
        sid = reg["sample_ids"][0]
        assert client.post(f"{LAB}/samples/{sid}/code-correction", json={"sample_code": "LAB.7-A", "reason": "rotulada"},
                           headers=monthly.headers).status_code == 200
        res = patch(client, monthly, sid, {"received_at": "2026-09-28", "reason": "otro mes"})
        assert res.status_code == 200 and res.json()["sample_code"] == "LAB.7-A" and res.json()["old_code"] is None

    def test_regeneration_respects_the_lifecycle_window(self, client, session, monthly):
        reg = register(client, monthly)
        set_status(session, reg["order_id"], OrderStatus.REVIEW)
        res = patch(client, monthly, reg["sample_ids"][0], {"received_at": "2026-09-28", "reason": "tarde"})
        assert res.status_code == 409
        assert detail(client, monthly, reg["sample_ids"][0])["sample_code"] == reg["sample_codes"][0]
