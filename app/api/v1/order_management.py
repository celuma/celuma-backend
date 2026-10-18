"""Basic order edits, sample-code correction and erroneous-capture removal
(Céluma 1.4, issues #9, #10, #11, #16).

Contract: `celuma-engineering/specs/laboratory/order-sample-management.md`; decision record ADR 0015.

Every write: authenticate -> capability -> tenant -> LOCK the order and check the shared lifecycle
guard (`app/services/order_lifecycle.py`) -> write -> audit -> commit, in one transaction. A hidden
control is never authorization: each rule is enforced here.
"""
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.api.v1.auth import AuthContext, current_user, get_auth_ctx
from app.api.v1.laboratory import _order_detail_response
from app.core.db import get_session
from app.core.rbac import has_permission
from app.models.enums import EventType
from app.models.events import OrderEvent
from app.models.laboratory import Order, Sample
from app.models.report import Report
from app.models.user import AppUser
from app.schemas.laboratory import OrderDetailResponse
from app.schemas.order_management import (
    ActionState,
    BlockerOut,
    OrderManagementState,
    OrderUpdate,
    RemovalRequest,
    RemovalResponse,
    SampleCodeCorrectionRequest,
    SampleCodeCorrectionResponse,
    SampleManagementState,
)
from app.services import capture_removal, order_lifecycle, order_management, sample_code_correction
from app.services.numbering import MANUAL_CODE_PERMISSION
from app.services.sample_types import record_audit

router = APIRouter(prefix="/laboratory")

REMOVE_PERMISSION = "lab:remove_capture"


def _require(user_id, code: str, session: Session) -> None:
    if not has_permission(user_id, code, session):
        raise HTTPException(403, f"Permission required: {code}")


def _blockers(blockers) -> List[BlockerOut]:
    return [BlockerOut(**b.as_dict()) for b in blockers]


def _own_order(session: Session, order_id: str, tenant_id: str) -> Order:
    order = session.get(Order, order_id)
    if order is None or str(order.tenant_id) != tenant_id:
        raise HTTPException(404, "Order not found")
    return order


def _own_sample(session: Session, sample_id: str, tenant_id: str) -> Sample:
    sample = session.get(Sample, sample_id)
    if sample is None or str(sample.tenant_id) != tenant_id:
        raise HTTPException(404, "Sample not found")
    return sample


# --------------------------------------------------------------------------- read models


@router.get("/orders/{order_id}/management", response_model=OrderManagementState)
def get_order_management_state(
    order_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """What can be done with this order right now, and why not (requires lab:read). A read:
    the write endpoints re-check everything under the lock."""
    _require(user.id, "lab:read", session)
    order = _own_order(session, order_id, ctx.tenant_id)
    eligibility = order_lifecycle.evaluate(session, order)
    removal_blockers = list(eligibility.blockers) + capture_removal.invoice_blockers(session, order)
    samples = session.exec(select(Sample.id).where(Sample.order_id == order.id)).all()
    return OrderManagementState(
        order_id=str(order.id), order_code=order.order_code, status=getattr(order.status, "value", str(order.status)),
        edit=ActionState(permitted=has_permission(user.id, "lab:update_order", session),
                         allowed=eligibility.allowed, blockers=_blockers(eligibility.blockers)),
        removal=ActionState(permitted=has_permission(user.id, REMOVE_PERMISSION, session),
                            allowed=not removal_blockers, blockers=_blockers(removal_blockers)),
        sample_count=len(samples), has_report=bool(eligibility.report_ids),
        removal_consequences=_order_removal_consequences(len(samples), len(eligibility.report_ids)),
    )


def _order_removal_consequences(samples: int, reports: int) -> List[str]:
    lines = [
        "The order and its code are retained as an auditable record and hidden from lists, searches and portals.",
        "The code is never reused and no other order is renumbered.",
        f"{samples} sample(s) are removed with the order; their images stay in storage.",
        "Its invoice is kept in the database and hidden from billing; nothing is refunded or voided.",
    ]
    if reports:
        lines.append(f"{reports} draft report(s) with no review history are kept and hidden with the order.")
    return lines


@router.get("/samples/{sample_id}/management", response_model=SampleManagementState)
def get_sample_management_state(
    sample_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """What can be done with this sample right now, and why not (requires lab:read)."""
    _require(user.id, "lab:read", session)
    sample = _own_sample(session, sample_id, ctx.tenant_id)
    order = session.get(Order, sample.order_id)
    if order is None:
        raise HTTPException(404, "Sample not found")
    eligibility = order_lifecycle.evaluate(session, order)
    removal_blockers = list(eligibility.blockers) + capture_removal.sample_blockers(session, sample)
    return SampleManagementState(
        sample_id=str(sample.id), sample_code=sample.sample_code, order_id=str(order.id), order_code=order.order_code,
        correction=ActionState(
            permitted=has_permission(user.id, "lab:update_sample", session)
            and has_permission(user.id, MANUAL_CODE_PERMISSION, session),
            allowed=eligibility.allowed, blockers=_blockers(eligibility.blockers)),
        removal=ActionState(permitted=has_permission(user.id, REMOVE_PERMISSION, session),
                            allowed=not removal_blockers, blockers=_blockers(removal_blockers)),
        removal_consequences=[
            "The sample and its code are retained as an auditable record and hidden from lists and searches.",
            "The code is never reused. Images stay in storage.",
            "The order keeps its other samples; its status is recalculated.",
        ],
    )


# --------------------------------------------------------------------------- #9 / #16 order edit


@router.patch("/orders/{order_id}", response_model=OrderDetailResponse)
def update_order(
    order_id: str,
    data: OrderUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Controlled basic order edit (requires lab:update_order).

    Allowlist: `requesting_physician_ids`, `expediente`, `notes`. Allowed only from Reception through
    Diagnosis and while no related report has review/approval/signing/publication evidence; checked
    under the order lock, so a concurrent submission cannot bypass it. An unchanged request writes nothing.
    """
    _require(user.id, "lab:update_order", session)
    _own_order(session, order_id, ctx.tenant_id)
    order = order_lifecycle.lock_and_guard(session, order_id, ctx.tenant_id)

    changes_old: dict = {}
    changes_new: dict = {}
    fields = data.model_fields_set

    if "requesting_physician_ids" in fields:
        previous = order_management.order_physicians(session, order)
        physicians = order_management.load_physicians(
            session, order.tenant_id, data.requesting_physician_ids, keep_inactive=[p.id for p in previous])
        if not physicians and not order.patient_id:
            raise HTTPException(400, "An order without a patient needs at least one requesting physician")
        if [p.id for p in physicians] != [p.id for p in previous]:
            changes_old["requesting_physician_ids"] = [str(p.id) for p in previous]
            changes_new["requesting_physician_ids"] = [str(p.id) for p in physicians]
            order_management.set_order_physicians(session, order, physicians)
    if "expediente" in fields and data.expediente != order.expediente:
        changes_old["expediente"], changes_new["expediente"] = order.expediente, data.expediente
        order.expediente = data.expediente
    if "notes" in fields and data.notes != order.notes:
        changes_old["notes"], changes_new["notes"] = order.notes, data.notes
        order.notes = data.notes

    if changes_new:
        session.add(order)
        session.add(OrderEvent(
            tenant_id=order.tenant_id, branch_id=order.branch_id, order_id=order.id, sample_id=None,
            event_type=EventType.ORDER_UPDATED, description="",
            event_metadata={"order_id": str(order.id), "order_code": order.order_code,
                            "changed_fields": sorted(changes_new), "old": changes_old, "new": changes_new},
            created_by=user.id,
        ))
        record_audit(
            session, tenant_id=order.tenant_id, user_id=user.id, action="ORDER.UPDATED", entity_type="order",
            entity_id=order.id, description="Order basic fields updated",
            old_values=changes_old, new_values=changes_new, metadata={"order_code": order.order_code},
        )
    session.commit()
    session.refresh(order)
    return _order_detail_response(session, order, ctx.tenant_id)


# --------------------------------------------------------------------------- #10 sample code correction


@router.post("/samples/{sample_id}/code-correction", response_model=SampleCodeCorrectionResponse)
def correct_sample_code(
    sample_id: str,
    data: SampleCodeCorrectionRequest,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Correct ONLY a sample's code after registration (requires lab:update_sample AND the
    privileged identifier capability lab:enter_manual_code). Same lifecycle window and evidence
    guard as every direct edit; audited with the reason; the old code is retired for good."""
    _require(user.id, "lab:update_sample", session)
    _require(user.id, MANUAL_CODE_PERMISSION, session)
    result = sample_code_correction.correct_sample_code(
        session, sample_id=sample_id, tenant_id=ctx.tenant_id, user=user,
        new_code=data.sample_code, reason=data.reason,
    )
    return SampleCodeCorrectionResponse(
        sample_id=str(result.sample.id), old_code=result.old_code, sample_code=result.sample.sample_code,
        unchanged=result.unchanged,
    )


# --------------------------------------------------------------------------- #11 removal


def _removal_response(result: capture_removal.RemovalResult) -> RemovalResponse:
    return RemovalResponse(
        kind=result.kind, id=result.entity_id, code=result.code, order_id=result.order_id,
        order_code=result.order_code, already_removed=result.already_removed, removed_at=result.removed_at,
        reason=result.reason, samples_removed=result.samples_removed, reports_hidden=result.reports_hidden,
        invoice_retained=result.invoice_retained,
    )


@router.post("/orders/{order_id}/removal", response_model=RemovalResponse)
def remove_order(
    order_id: str,
    data: RemovalRequest,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Remove an erroneous order (tombstone; requires lab:remove_capture). See
    `app/services/capture_removal.py` for the exact semantics and blockers."""
    _require(user.id, REMOVE_PERMISSION, session)
    return _removal_response(capture_removal.remove_order(
        session, order_id=order_id, tenant_id=ctx.tenant_id, user=user, reason=data.reason))


@router.post("/samples/{sample_id}/removal", response_model=RemovalResponse)
def remove_sample(
    sample_id: str,
    data: RemovalRequest,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Remove an erroneous sample (tombstone; requires lab:remove_capture). Blocked for the order's
    last sample and on every lifecycle/evidence rule of the order."""
    _require(user.id, REMOVE_PERMISSION, session)
    return _removal_response(capture_removal.remove_sample(
        session, sample_id=sample_id, tenant_id=ctx.tenant_id, user=user, reason=data.reason))
