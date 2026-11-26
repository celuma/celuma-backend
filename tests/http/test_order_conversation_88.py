"""Céluma 1.5 (#88): order conversation messages that were sent but never shown.

Reproduced on synthetic records against the unchanged code (see
HANDOFF-88-98-96.md):

1. An administrator created after registration (or edited into the role) has no
   `user_branch` rows — full-branch-access roles deliberately carry none — and
   the comment route checked the literal rows, so every send answered
   ``403 No access to this order's branch`` while the same user could read the
   order and its conversation.
2. The list returned the *oldest* page (`created_at ASC LIMIT 20`) and the web
   client never paginated, so from the 21st message on, a persisted message —
   including the one just sent — was absent after the reload that follows a
   send.
"""
from datetime import datetime, timedelta

from sqlmodel import select

from app.models.laboratory import OrderComment
from app.models.user import UserBranch

from .factories import auth_headers, create_branch, create_order, create_tenant, create_user


def _world(session, *, admin_with_branch_row=False):
    tenant = create_tenant(session, name="Lab Conversación")
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch, order_code="ORD-CONV-1")
    admin = create_user(session, tenant, email="admin.conv@example.test", roles=("admin",), full_name="Admin Sintético")
    if admin_with_branch_row:
        session.add(UserBranch(user_id=admin.id, branch_id=branch.id))
        session.commit()
    tech = create_user(session, tenant, email="tech.conv@example.test", roles=("lab_tech",), full_name="Técnico Sintético")
    session.add(UserBranch(user_id=tech.id, branch_id=branch.id))
    session.commit()
    return tenant, branch, order, admin, tech


def _url(order):
    return f"/api/v1/laboratory/orders/{order.id}/comments"


def test_full_branch_access_admin_without_branch_rows_can_send(client, session):
    _, _, order, admin, tech = _world(session)
    response = client.post(_url(order), json={"text": "Mensaje sintético", "mentions": []}, headers=auth_headers(admin))
    assert response.status_code == 200, response.text
    # A second authorized user sees it.
    listed = client.get(_url(order), headers=auth_headers(tech)).json()
    assert [item["text"] for item in listed["items"]] == ["Mensaje sintético"]


def test_non_member_of_branch_is_still_refused(client, session):
    tenant, _, _, _, tech = _world(session)
    other_branch = create_branch(session, tenant, code="NORTE")
    other_order = create_order(session, tenant, other_branch, order_code="ORD-CONV-2")
    response = client.post(_url(other_order), json={"text": "No debe guardarse", "mentions": []}, headers=auth_headers(tech))
    assert response.status_code == 403
    assert session.exec(select(OrderComment)).all() == []


def test_latest_page_contains_the_newest_message_after_twenty(client, session):
    tenant, branch, order, admin, tech = _world(session, admin_with_branch_row=True)
    base = datetime.utcnow() - timedelta(hours=2)
    for index in range(25):
        session.add(OrderComment(
            tenant_id=tenant.id, branch_id=branch.id, order_id=order.id, created_by=tech.id,
            text=f"Mensaje {index:02d}", comment_metadata={}, created_at=base + timedelta(minutes=index),
        ))
    session.commit()

    sent = client.post(_url(order), json={"text": "Mensaje recién enviado", "mentions": []}, headers=auth_headers(admin))
    assert sent.status_code == 200, sent.text

    page = client.get(_url(order), headers=auth_headers(tech)).json()
    texts = [item["text"] for item in page["items"]]
    # The latest 20, chronological inside the page (oldest first), ending with the one just sent.
    assert texts == [f"Mensaje {i:02d}" for i in range(6, 25)] + ["Mensaje recién enviado"]
    assert page["page_info"]["has_more"] is True

    older = client.get(_url(order), params={"before": page["page_info"]["next_before"]}, headers=auth_headers(tech)).json()
    older_texts = [item["text"] for item in older["items"]]
    assert older_texts == [f"Mensaje {i:02d}" for i in range(6)]
    assert older["page_info"]["has_more"] is False
    assert set(older_texts).isdisjoint(texts)
