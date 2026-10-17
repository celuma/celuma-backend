"""Helpers for the order/sample management HTTP tests (Céluma 1.4, #9, #10, #11, #16)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlmodel import Session

from app.models.enums import EventType, OrderStatus, ReportStatus, ReviewStatus
from app.models.events import OrderEvent
from app.models.report import ReportVersion
from app.models.report_review import ReportReview
from app.models.requesting_physician import RequestingPhysician

from .factories import create_report
from .numbering_support import UNIFIED, Lab

LAB = "/api/v1/laboratory"


def register(client, lab: Lab, sample_types=("BX",), **extra) -> dict:
    """Register an order with samples through the real unified route; returns ids and codes."""
    res = client.post(UNIFIED, json=lab.unified(sample_types, **extra), headers=lab.headers)
    assert res.status_code == 200, res.text
    body = res.json()
    return {
        "order_id": body["order"]["id"], "order_code": body["order"]["order_code"],
        "sample_ids": [s["id"] for s in body["samples"]], "sample_codes": [s["sample_code"] for s in body["samples"]],
    }


def physician(session: Session, lab: Lab, code="MS1", first="Ana", last="Solicitante", *, tenant=None, branch=None,
              active=True) -> RequestingPhysician:
    p = RequestingPhysician(
        tenant_id=(tenant or lab.tenant).id, branch_id=(branch or lab.branch).id, physician_code=code,
        first_name=first, last_name=last, full_name=f"{first} {last}", is_active=active,
    )
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def set_status(session: Session, order_id: str, status: OrderStatus) -> None:
    session.execute(text('UPDATE "order" SET status = :s WHERE id = :i'), {"s": status.value, "i": order_id})
    session.commit()
    session.expire_all()


def add_event(session: Session, lab: Lab, order_id: str, event_type: EventType) -> None:
    session.add(OrderEvent(tenant_id=lab.tenant.id, branch_id=lab.branch.id, order_id=order_id,
                           event_type=event_type, description="", created_by=lab.user.id))
    session.commit()


def make_report(session: Session, lab: Lab, order_id: str, status=ReportStatus.DRAFT, **kw):
    from app.models.laboratory import Order

    order = session.get(Order, order_id)
    return create_report(session, lab.tenant, lab.branch, order, status=status, **kw)


def decide_review(session: Session, lab: Lab, order_id: str, status=ReviewStatus.APPROVED) -> None:
    session.add(ReportReview(tenant_id=lab.tenant.id, order_id=order_id, reviewer_user_id=lab.user.id,
                             status=status, decision_at=datetime.utcnow()))
    session.commit()


def sign_version(session: Session, lab: Lab, report_id) -> None:
    session.execute(text("UPDATE report_version SET signed_at = now(), signed_by = :u WHERE report_id = :r"),
                    {"u": str(lab.user.id), "r": str(report_id)})
    session.commit()


def count(session: Session, sql: str, **params) -> int:
    return session.execute(text(sql), params).scalar()


__all__ = ["LAB", "register", "physician", "set_status", "add_event", "make_report", "decide_review",
           "sign_version", "count", "ReportVersion"]
