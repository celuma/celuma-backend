"""Removal versus billing writers, real PostgreSQL threads (Céluma 1.4 close-out, #11).

One lock protocol: every writer that can change an order's invoice or payments (payment, invoice, invoice
items) takes the order lifecycle lock FIRST, then re-checks tenant, removed state and invoice state; removal
takes the same lock and reads payment evidence under it. Both arrival orders are exercised deterministically by
pausing one handler INSIDE its locked section.
"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import text

from app.api.v1 import billing
from app.services import capture_removal

from .management_support import LAB, count, register
from .numbering_support import Lab, freeze, fresh_client, new_client  # noqa: F401

API = "/api/v1"


@pytest.fixture
def lab(session, monkeypatch):
    freeze(monkeypatch)
    return Lab(session, "Alfa", sample_types=("BX",))


def invoice_of(session, order_id):
    return str(session.execute(text("SELECT id FROM invoice WHERE order_id = :o"), {"o": order_id}).scalar_one())


def pay(client, lab, invoice_id, amount=10):
    return client.post(f"{API}/billing/payments/", headers=lab.headers, json={
        "tenant_id": str(lab.tenant.id), "invoice_id": invoice_id, "amount": amount})


def remove(client, lab, order_id, reason="orden capturada por error"):
    return client.post(f"{LAB}/orders/{order_id}/removal", json={"reason": reason}, headers=lab.headers)


class Pause:
    """Pauses the wrapped function once, inside the caller's locked section, until released."""

    def __init__(self, monkeypatch, module, name):
        self.reached, self.release = threading.Event(), threading.Event()
        original = getattr(module, name)

        def wrapper(*args, **kwargs):
            result = original(*args, **kwargs)
            if not self.reached.is_set():
                self.reached.set()
                assert self.release.wait(20), "test never released the paused handler"
            return result

        monkeypatch.setattr(module, name, wrapper)


def run(client_job, results, key):
    def target():
        try:
            results[key] = client_job(new_client())
        except Exception as exc:  # surfaced by the assertions
            results[key] = exc
    t = threading.Thread(target=target)
    t.start()
    return t


def test_a_payment_that_is_mid_flight_blocks_the_removal_which_is_then_refused(fresh_client, session, lab, monkeypatch):
    reg = register(fresh_client, lab)
    invoice = invoice_of(session, reg["order_id"])
    pause = Pause(monkeypatch, billing, "update_invoice_status")  # payment flushed, not yet committed
    results: dict = {}
    pay_thread = run(lambda c: pay(c, lab, invoice), results, "pay")
    assert pause.reached.wait(10)
    removal_thread = run(lambda c: remove(c, lab, reg["order_id"]), results, "remove")
    removal_thread.join(1.5)
    assert removal_thread.is_alive(), "removal must wait for the payment's order lock"
    pause.release.set()
    pay_thread.join(15)
    removal_thread.join(15)
    assert results["pay"].status_code == 200, results["pay"].text
    assert results["remove"].status_code == 409
    assert "invoice_has_payments" in {b["code"] for b in results["remove"].json()["detail"]["blockers"]}
    # nothing is hidden: the paid invoice is still visible and the order is not removed
    assert count(session, 'SELECT count(*) FROM "order" WHERE id = :i AND removed_at IS NULL', i=reg["order_id"]) == 1
    assert len(fresh_client.get(f"{API}/billing/invoices/", headers=lab.headers).json()) == 1


def test_a_removal_that_is_mid_flight_blocks_the_payment_which_is_then_refused(fresh_client, session, lab, monkeypatch):
    reg = register(fresh_client, lab)
    invoice = invoice_of(session, reg["order_id"])
    pause = Pause(monkeypatch, capture_removal, "invoice_blockers")  # inside the removal's locked section
    results: dict = {}
    removal_thread = run(lambda c: remove(c, lab, reg["order_id"]), results, "remove")
    assert pause.reached.wait(10)
    pay_thread = run(lambda c: pay(c, lab, invoice), results, "pay")
    pay_thread.join(1.5)
    assert pay_thread.is_alive(), "the payment must wait for the removal's order lock"
    pause.release.set()
    removal_thread.join(15)
    pay_thread.join(15)
    assert results["remove"].status_code == 200, results["remove"].text
    assert results["pay"].status_code == 404  # the invoice of a removed capture is gone for billing writers
    assert count(session, "SELECT count(*) FROM payment") == 0
    assert count(session, "SELECT status FROM invoice WHERE order_id = :o", o=reg["order_id"]) is not None
    assert session.execute(text("SELECT status::text FROM invoice WHERE order_id = :o"), {"o": reg["order_id"]}).scalar_one() == "PENDING"


@pytest.mark.parametrize("writer", ["item_add", "item_patch", "invoice_create", "payment"])
def test_after_a_removal_no_billing_writer_commits_a_side_effect(fresh_client, session, lab, writer):
    reg = register(fresh_client, lab)
    invoice = invoice_of(session, reg["order_id"])
    item = session.execute(text("SELECT id FROM invoice_item WHERE invoice_id = :i LIMIT 1"), {"i": invoice}).scalar()
    assert remove(fresh_client, lab, reg["order_id"]).status_code == 200
    before = {t: count(session, f"SELECT count(*) FROM {t}") for t in ("payment", "invoice", "invoice_item", "order_event")}
    if writer == "payment":
        res = pay(fresh_client, lab, invoice)
    elif writer == "item_add":
        res = fresh_client.post(f"{API}/billing/invoices/{invoice}/items", headers=lab.headers,
                                json={"description": "extra", "quantity": 1, "unit_price": 5})
    elif writer == "item_patch":
        res = fresh_client.patch(f"{API}/billing/invoices/{invoice}/items/{item or invoice}", headers=lab.headers,
                                 json={"unit_price": 99})
    else:
        res = fresh_client.post(f"{API}/billing/invoices/", headers=lab.headers, json={
            "tenant_id": str(lab.tenant.id), "branch_id": str(lab.branch.id), "order_id": reg["order_id"],
            "invoice_number": "INV-NEW", "subtotal": 1, "total": 1})
    assert res.status_code == 404, res.text
    after = {t: count(session, f"SELECT count(*) FROM {t}") for t in before}
    assert after == before


def test_two_simultaneous_removals_and_a_lost_response_retry_are_safe(fresh_client, session, lab):
    from .numbering_support import run_parallel
    reg = register(fresh_client, lab)
    results = run_parallel([lambda c: remove(c, lab, reg["order_id"]) for _ in range(3)])
    assert all(not isinstance(r, Exception) and r.status_code == 200 for r in results), results
    assert sorted(r.json()["already_removed"] for r in results) == [False, True, True]
    assert count(session, "SELECT count(*) FROM audit_log WHERE action = 'ORDER.REMOVED'") == 1
    again = remove(fresh_client, lab, reg["order_id"], reason="otro")
    assert again.status_code == 200 and again.json()["already_removed"] is True


def test_a_payment_that_won_keeps_the_invoice_visible_after_every_later_removal_attempt(fresh_client, session, lab):
    reg = register(fresh_client, lab)
    invoice = invoice_of(session, reg["order_id"])
    assert pay(fresh_client, lab, invoice).status_code == 200
    for _ in range(2):
        assert remove(fresh_client, lab, reg["order_id"]).status_code == 409
    assert len(fresh_client.get(f"{API}/billing/invoices/", headers=lab.headers).json()) == 1
