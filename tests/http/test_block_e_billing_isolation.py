"""Billing tenant-isolation boundary (Céluma 1.3, Phase 5, Block E — E-001/E-002).

Every other tenant-owned route in `billing.py` anchors the resource it loads:
`get_invoice`, `get_invoice_with_items`, `add_invoice_item`,
`update_invoice_item`, `get_order_balance` and `get_order_invoice` all compare
`invoice.tenant_id` / `order.tenant_id` against `ctx.tenant_id` before doing
anything. The two POST routes that take their foreign keys from the **request
body** rather than the path do not:

  * `POST /billing/invoices/` validates `invoice_data.tenant_id` against the
    caller (403), then dereferences `branch_id` and `order_id` with a bare
    `session.get(...)` and only checks existence.
  * `POST /billing/payments/` validates `payment_data.tenant_id` against the
    caller (403), then dereferences `invoice_id` the same way.

The tenant check on the *declared* `tenant_id` reads like an isolation guard,
which is why this survived Block C and Block D — but the declared tenant is
the caller's own, and it says nothing about who owns the referenced order or
invoice. That is the C-001 / D-001 shape once more: the guard that exists is
not the guard that matters.

The consequences are writes, not just reads. Both handlers call
`update_order_payment_lock(...)`, which sets `Order.billed_lock` — and
`billed_lock` is the gate `portal.py` uses to decide whether a patient or
requesting physician may download a report. A tenant-A billing user can
therefore flip a tenant-B order's delivery gate, and via `create_payment` can
mark a tenant-B invoice paid (`update_invoice_status` writes `amount_paid` and
`paid_at`) and release a tenant-B clinical report to third parties before that
laboratory has been paid. `create_payment` additionally writes an
`OrderEvent` row into tenant B's order timeline.

403, not 404, for the foreign reference. The C-001 / D-001 convention — hide
existence behind a 404 — governs *path-addressed* resources. These are
*body-supplied* references, and for those this codebase already has a settled
answer: 403. `patients.py::create_patient` refuses a foreign `branch_id` with
`403 "Branch does not belong to the current tenant"`, and its update route
repeats it.

The phrasing below follows `billing.py`'s own six existing guards
(`"… does not belong to your tenant"`) rather than `patients.py`'s wording, so
that every tenant-mismatch 403 in this module reads identically. The status
code is the contract; the sentence is the module's house style.

The trade-off is deliberate: a 403 does confirm that the referenced id exists
somewhere, where a 404 would not. That oracle is weak — these are unguessable
UUIDs, and the caller must already possess the id to ask — and it is worth
less than keeping one consistent contract across every body-supplied
reference in the API.
"""
from __future__ import annotations

from sqlmodel import Session, select

from app.models.billing import Invoice, Payment
from app.models.laboratory import Order

from .factories import (
    auth_headers,
    create_branch,
    create_order,
    create_tenant,
    create_user,
)

BILLING_ROLES = ("billing",)


def _billing_tenant(session: Session, *, name: str, branch_code: str,
                    order_code: str, email: str):
    """A tenant with a billing user, one branch and one unbilled order."""
    tenant = create_tenant(session, name=name)
    branch = create_branch(session, tenant, code=branch_code)
    user = create_user(session, tenant, email=email, roles=BILLING_ROLES)
    order = create_order(session, tenant, branch, order_code=order_code)
    return {"tenant": tenant, "branch": branch, "user": user, "order": order}


def _seed_invoice(session: Session, env, *, number: str, total: float = 500.0) -> Invoice:
    """An unpaid invoice owned by `env`'s tenant, with its order locked."""
    invoice = Invoice(
        tenant_id=env["tenant"].id,
        branch_id=env["branch"].id,
        order_id=env["order"].id,
        invoice_number=number,
        subtotal=total,
        total=total,
        amount_total=total,
        amount_paid=0.0,
    )
    session.add(invoice)
    order = session.get(Order, env["order"].id)
    order.billed_lock = True
    session.add(order)
    session.commit()
    session.refresh(invoice)
    return invoice


def _two_tenants(session: Session):
    a = _billing_tenant(
        session, name="Lab A", branch_code="A-MAIN",
        order_code="A-ORD-1", email="billing.a@example.com",
    )
    b = _billing_tenant(
        session, name="Lab B", branch_code="B-MAIN",
        order_code="B-ORD-1", email="billing.b@example.com",
    )
    return a, b


def _invoice_payload(*, tenant_id, branch_id, order_id, number="F-001"):
    return {
        "tenant_id": str(tenant_id),
        "branch_id": str(branch_id),
        "order_id": str(order_id),
        "invoice_number": number,
        "subtotal": 500.0,
        "total": 500.0,
        "currency": "MXN",
    }


# ---------------------------------------------------------------------------
# E-001 — POST /billing/invoices/
# ---------------------------------------------------------------------------

def test_create_invoice_for_own_order_succeeds(client, session):
    """Positive control: the same call, entirely within one tenant, works."""
    a, _ = _two_tenants(session)

    response = client.post(
        "/api/v1/billing/invoices/",
        json=_invoice_payload(
            tenant_id=a["tenant"].id,
            branch_id=a["branch"].id,
            order_id=a["order"].id,
        ),
        headers=auth_headers(a["user"]),
    )

    assert response.status_code == 200, response.text
    assert response.json()["tenant_id"] == str(a["tenant"].id)


def test_create_invoice_against_foreign_order_is_denied(client, session):
    """A declares its own tenant_id and branch, but points at B's order."""
    a, b = _two_tenants(session)

    response = client.post(
        "/api/v1/billing/invoices/",
        json=_invoice_payload(
            tenant_id=a["tenant"].id,
            branch_id=a["branch"].id,
            order_id=b["order"].id,
        ),
        headers=auth_headers(a["user"]),
    )

    assert response.status_code == 403, response.text
    assert "Order does not belong to your tenant" in response.json()["detail"]


def test_create_invoice_against_foreign_order_writes_nothing(client, session):
    """The denial must leave tenant B's order and invoice ledger untouched.

    `update_order_payment_lock` runs after the unanchored `session.get`, so a
    successful cross-tenant create would flip `billed_lock` on B's order —
    the flag `portal.py` reads to gate third-party report delivery.
    """
    a, b = _two_tenants(session)
    b_order_before = session.get(Order, b["order"].id).billed_lock

    client.post(
        "/api/v1/billing/invoices/",
        json=_invoice_payload(
            tenant_id=a["tenant"].id,
            branch_id=a["branch"].id,
            order_id=b["order"].id,
        ),
        headers=auth_headers(a["user"]),
    )

    session.expire_all()
    assert session.get(Order, b["order"].id).billed_lock == b_order_before
    assert session.exec(
        select(Invoice).where(Invoice.order_id == b["order"].id)
    ).all() == []


def test_create_invoice_against_foreign_branch_is_denied(client, session):
    """The branch reference is unanchored on the same route."""
    a, b = _two_tenants(session)

    response = client.post(
        "/api/v1/billing/invoices/",
        json=_invoice_payload(
            tenant_id=a["tenant"].id,
            branch_id=b["branch"].id,
            order_id=a["order"].id,
        ),
        headers=auth_headers(a["user"]),
    )

    assert response.status_code == 403, response.text
    assert "Branch does not belong to your tenant" in response.json()["detail"]


def test_create_invoice_declaring_foreign_tenant_still_403(client, session):
    """The pre-existing guard is preserved: an explicit foreign `tenant_id`
    in the body is a refused tenant context (403), not a hidden resource."""
    a, b = _two_tenants(session)

    response = client.post(
        "/api/v1/billing/invoices/",
        json=_invoice_payload(
            tenant_id=b["tenant"].id,
            branch_id=b["branch"].id,
            order_id=b["order"].id,
        ),
        headers=auth_headers(a["user"]),
    )

    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# E-002 — POST /billing/payments/
# ---------------------------------------------------------------------------

def test_register_payment_on_own_invoice_succeeds(client, session):
    """Positive control, and it proves the lock genuinely clears on payment —
    so the negative cases below are testing a real state change."""
    a, _ = _two_tenants(session)
    invoice = _seed_invoice(session, a, number="A-F-001")

    response = client.post(
        "/api/v1/billing/payments/",
        json={
            "tenant_id": str(a["tenant"].id),
            "invoice_id": str(invoice.id),
            "amount": 500.0,
            "currency": "MXN",
        },
        headers=auth_headers(a["user"]),
    )

    assert response.status_code == 200, response.text
    session.expire_all()
    assert session.get(Order, a["order"].id).billed_lock is False


def test_register_payment_on_foreign_invoice_is_denied(client, session):
    """A declares its own tenant_id but pays B's invoice."""
    a, b = _two_tenants(session)
    b_invoice = _seed_invoice(session, b, number="B-F-001")

    response = client.post(
        "/api/v1/billing/payments/",
        json={
            "tenant_id": str(a["tenant"].id),
            "invoice_id": str(b_invoice.id),
            "amount": 500.0,
            "currency": "MXN",
        },
        headers=auth_headers(a["user"]),
    )

    assert response.status_code == 403, response.text
    assert "Invoice does not belong to your tenant" in response.json()["detail"]


def test_register_payment_on_foreign_invoice_does_not_release_report(client, session):
    """The severe half of E-002.

    An accepted cross-tenant payment would mark B's invoice paid and clear
    `billed_lock` on B's order — releasing B's report to the patient and the
    requesting physician without B ever being paid.
    """
    a, b = _two_tenants(session)
    b_invoice = _seed_invoice(session, b, number="B-F-002")

    client.post(
        "/api/v1/billing/payments/",
        json={
            "tenant_id": str(a["tenant"].id),
            "invoice_id": str(b_invoice.id),
            "amount": 500.0,
            "currency": "MXN",
        },
        headers=auth_headers(a["user"]),
    )

    session.expire_all()
    assert session.get(Order, b["order"].id).billed_lock is True
    assert float(session.get(Invoice, b_invoice.id).amount_paid) == 0.0
    assert session.get(Invoice, b_invoice.id).paid_at is None


def test_register_payment_on_foreign_invoice_writes_no_payment_row(client, session):
    """No Payment row may be created in either tenant."""
    a, b = _two_tenants(session)
    b_invoice = _seed_invoice(session, b, number="B-F-003")

    client.post(
        "/api/v1/billing/payments/",
        json={
            "tenant_id": str(a["tenant"].id),
            "invoice_id": str(b_invoice.id),
            "amount": 500.0,
            "currency": "MXN",
        },
        headers=auth_headers(a["user"]),
    )

    session.expire_all()
    assert session.exec(
        select(Payment).where(Payment.invoice_id == b_invoice.id)
    ).all() == []


def test_register_payment_without_permission_is_denied(client, session):
    """RBAC still precedes tenancy: no billing permission, no write."""
    a, _ = _two_tenants(session)
    invoice = _seed_invoice(session, a, number="A-F-009")
    viewer = create_user(
        session, a["tenant"], email="viewer.a@example.com", roles=("viewer",)
    )

    response = client.post(
        "/api/v1/billing/payments/",
        json={
            "tenant_id": str(a["tenant"].id),
            "invoice_id": str(invoice.id),
            "amount": 500.0,
            "currency": "MXN",
        },
        headers=auth_headers(viewer),
    )

    assert response.status_code == 403, response.text
