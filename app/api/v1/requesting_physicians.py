from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, and_, func, select
from app.core.db import get_session
from app.api.v1.auth import AuthContext, current_user, get_auth_ctx
from app.core.rbac import has_permission
from app.models.assignment import Assignment
from app.models.laboratory import Label, Order, OrderLabel, Sample
from app.models.requesting_physician import RequestingPhysician
from app.models.patient import Patient
from app.models.tenant import Branch, Tenant
from app.models.user import AppUser
from app.schemas.laboratory import (
    BranchRef,
    LabelResponse,
    OrderListItem,
    OrdersListResponse,
    PatientRef,
    RequestingPhysicianRef,
    UserRef,
)
from app.schemas.requesting_physician import (
    RequestingPhysicianCreate,
    RequestingPhysicianDetailResponse,
    RequestingPhysicianResponse,
    RequestingPhysicianUpdate,
)
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/requesting-physicians")


def _require(user_id, code: str, session: Session) -> None:
    if not has_permission(user_id, code, session):
        raise HTTPException(403, f"Permission required: {code}")


def _full_name(physician: RequestingPhysician) -> str:
    return physician.full_name or f"{physician.first_name} {physician.last_name}".strip()


def _to_response(physician: RequestingPhysician) -> RequestingPhysicianResponse:
    return RequestingPhysicianResponse(
        id=str(physician.id),
        tenant_id=str(physician.tenant_id),
        branch_id=str(physician.branch_id),
        physician_code=physician.physician_code,
        first_name=physician.first_name,
        last_name=physician.last_name,
        full_name=_full_name(physician),
        specialty=physician.specialty,
        professional_license=physician.professional_license,
        institution=physician.institution,
        phone=physician.phone,
        email=physician.email,
        address=physician.address,
        is_active=physician.is_active,
        created_at=physician.created_at,
    )


def _to_ref(physician: RequestingPhysician) -> RequestingPhysicianRef:
    return RequestingPhysicianRef(
        id=str(physician.id),
        full_name=_full_name(physician),
        physician_code=physician.physician_code,
        specialty=physician.specialty,
        institution=physician.institution,
        email=physician.email,
    )


def _validate_branch(session: Session, branch_id: str, tenant_id: str) -> Branch:
    branch = session.get(Branch, branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    if str(branch.tenant_id) != tenant_id:
        raise HTTPException(403, "Branch does not belong to your tenant")
    return branch


def _generate_physician_code(session: Session, tenant_id: str) -> str:
    count = session.exec(
        select(func.count(RequestingPhysician.id)).where(
            RequestingPhysician.tenant_id == tenant_id,
        )
    ).first() or 0
    next_sequence = int(count) + 1

    while True:
        code = f"MS-{next_sequence:04d}"
        existing = session.exec(
            select(RequestingPhysician).where(
                RequestingPhysician.tenant_id == tenant_id,
                RequestingPhysician.physician_code == code,
            )
        ).first()
        if not existing:
            return code
        next_sequence += 1


@router.get("/", response_model=list[RequestingPhysicianResponse])
def list_requesting_physicians(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
    active_only: bool = True,
):
    """List requesting physicians for the current tenant (requires lab:read)."""
    _require(user.id, "lab:read", session)
    query = select(RequestingPhysician).where(RequestingPhysician.tenant_id == ctx.tenant_id)
    if active_only:
        query = query.where(RequestingPhysician.is_active == True)  # noqa: E712
    physicians = session.exec(query).all()
    return [_to_response(physician) for physician in physicians]


@router.get("/{physician_id}", response_model=RequestingPhysicianDetailResponse)
def get_requesting_physician(
    physician_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Get requesting physician details (requires lab:read)."""
    _require(user.id, "lab:read", session)
    physician = session.get(RequestingPhysician, physician_id)
    if not physician or str(physician.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Requesting physician not found")
    return RequestingPhysicianDetailResponse(**_to_response(physician).model_dump())


@router.get("/{physician_id}/orders", response_model=OrdersListResponse)
def list_requesting_physician_orders(
    physician_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """List all orders for a requesting physician (requires lab:read)."""
    _require(user.id, "lab:read", session)
    physician = session.get(RequestingPhysician, physician_id)
    if not physician or str(physician.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Requesting physician not found")

    orders = session.exec(
        select(Order).where(
            Order.requesting_physician_id == physician.id,
            Order.tenant_id == ctx.tenant_id,
        )
    ).all()
    results: list[OrderListItem] = []

    for order in orders:
        branch = session.get(Branch, order.branch_id)
        patient = session.get(Patient, order.patient_id) if order.patient_id else None
        sample_count = len(session.exec(select(Sample).where(Sample.order_id == order.id)).all())
        has_report = order.report_id is not None
        has_invoice = order.invoice_id is not None

        label_ids = session.exec(select(OrderLabel.label_id).where(OrderLabel.order_id == order.id)).all()
        labels = []
        if label_ids:
            label_objs = session.exec(select(Label).where(Label.id.in_(label_ids))).all()
            labels = [
                LabelResponse(
                    id=str(label.id),
                    name=label.name,
                    color=label.color,
                    tenant_id=str(label.tenant_id),
                    created_at=label.created_at,
                )
                for label in label_objs
            ]

        assignee_ids = session.exec(
            select(Assignment.assignee_user_id).where(
                and_(
                    Assignment.item_type == "lab_order",
                    Assignment.item_id == order.id,
                    Assignment.unassigned_at.is_(None),
                )
            )
        ).all()
        assignees = []
        if assignee_ids:
            users = session.exec(select(AppUser).where(AppUser.id.in_(assignee_ids))).all()
            assignees = [UserRef(id=str(item.id), name=item.full_name, email=item.email, avatar_url=item.avatar_url) for item in users]

        results.append(
            OrderListItem(
                id=str(order.id),
                order_code=order.order_code,
                status=order.status,
                tenant_id=str(order.tenant_id),
                branch=BranchRef(
                    id=str(order.branch_id),
                    name=branch.name if branch else "",
                    code=branch.code if branch else None,
                ),
                patient=PatientRef(
                    id=str(patient.id),
                    full_name=f"{patient.first_name} {patient.last_name}",
                    patient_code=patient.patient_code,
                ) if patient else None,
                requesting_physician=_to_ref(physician),
                requested_by=order.requested_by,
                notes=order.notes,
                created_at=str(getattr(order, "created_at", "")) if getattr(order, "created_at", None) else None,
                report_id=str(order.report_id) if order.report_id else None,
                invoice_id=str(order.invoice_id) if order.invoice_id else None,
                study_type_id=str(order.study_type_id) if order.study_type_id else None,
                sample_count=sample_count,
                has_report=has_report,
                has_invoice=has_invoice,
                labels=labels if labels else None,
                assignees=assignees if assignees else None,
            )
        )

    return OrdersListResponse(orders=results)


@router.post("/", response_model=RequestingPhysicianResponse)
def create_requesting_physician(
    physician_data: RequestingPhysicianCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a requesting physician (requires lab:create_order)."""
    _require(user.id, "lab:create_order", session)

    tenant = session.get(Tenant, ctx.tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    _validate_branch(session, physician_data.branch_id, ctx.tenant_id)

    physician_code = physician_data.physician_code or _generate_physician_code(session, ctx.tenant_id)
    existing = session.exec(
        select(RequestingPhysician).where(
            RequestingPhysician.tenant_id == ctx.tenant_id,
            RequestingPhysician.physician_code == physician_code,
        )
    ).first()
    if existing:
        raise HTTPException(400, "Requesting physician code already exists for this tenant")

    physician = RequestingPhysician(
        tenant_id=ctx.tenant_id,
        branch_id=physician_data.branch_id,
        physician_code=physician_code,
        first_name=physician_data.first_name,
        last_name=physician_data.last_name,
        full_name=f"{physician_data.first_name} {physician_data.last_name}".strip(),
        specialty=physician_data.specialty,
        professional_license=physician_data.professional_license,
        institution=physician_data.institution,
        phone=physician_data.phone,
        email=physician_data.email,
        address=physician_data.address,
        is_active=physician_data.is_active if physician_data.is_active is not None else True,
    )
    session.add(physician)
    session.commit()
    session.refresh(physician)

    logger.info(
        f"Requesting physician '{physician.physician_code}' created",
        extra={"event": "requesting_physician.created", "physician_id": str(physician.id), "user_id": str(user.id)},
    )

    return _to_response(physician)


@router.put("/{physician_id}", response_model=RequestingPhysicianResponse)
def update_requesting_physician(
    physician_id: str,
    physician_data: RequestingPhysicianUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Update a requesting physician (requires lab:create_order)."""
    _require(user.id, "lab:create_order", session)
    physician = session.get(RequestingPhysician, physician_id)
    if not physician or str(physician.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Requesting physician not found")

    if physician_data.branch_id is not None:
        _validate_branch(session, physician_data.branch_id, ctx.tenant_id)
        physician.branch_id = physician_data.branch_id

    if physician_data.physician_code is not None:
        existing = session.exec(
            select(RequestingPhysician).where(
                RequestingPhysician.tenant_id == ctx.tenant_id,
                RequestingPhysician.physician_code == physician_data.physician_code,
                RequestingPhysician.id != physician.id,
            )
        ).first()
        if existing:
            raise HTTPException(400, "Requesting physician code already exists for this tenant")
        physician.physician_code = physician_data.physician_code

    if physician_data.first_name is not None:
        physician.first_name = physician_data.first_name
    if physician_data.last_name is not None:
        physician.last_name = physician_data.last_name
    if physician_data.first_name is not None or physician_data.last_name is not None:
        physician.full_name = f"{physician.first_name} {physician.last_name}".strip()
    if physician_data.specialty is not None:
        physician.specialty = physician_data.specialty
    if physician_data.professional_license is not None:
        physician.professional_license = physician_data.professional_license
    if physician_data.institution is not None:
        physician.institution = physician_data.institution
    if physician_data.phone is not None:
        physician.phone = physician_data.phone
    if physician_data.email is not None:
        physician.email = physician_data.email
    if physician_data.address is not None:
        physician.address = physician_data.address
    if physician_data.is_active is not None:
        physician.is_active = physician_data.is_active

    session.add(physician)
    session.commit()
    session.refresh(physician)

    logger.info(
        f"Requesting physician '{physician.physician_code}' updated",
        extra={"event": "requesting_physician.updated", "physician_id": str(physician.id), "user_id": str(user.id)},
    )

    return _to_response(physician)


@router.delete("/{physician_id}")
def delete_requesting_physician(
    physician_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
    hard_delete: bool = False,
):
    """Delete a requesting physician (requires lab:create_order)."""
    _require(user.id, "lab:create_order", session)
    physician = session.get(RequestingPhysician, physician_id)
    if not physician or str(physician.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Requesting physician not found")

    if hard_delete:
        session.delete(physician)
        session.commit()
        logger.info(
            f"Requesting physician '{physician.physician_code}' permanently deleted",
            extra={"event": "requesting_physician.hard_deleted", "physician_id": physician_id, "user_id": str(user.id)},
        )
        return {"message": "Requesting physician permanently deleted", "id": physician_id}

    physician.is_active = False
    session.add(physician)
    session.commit()
    logger.info(
        f"Requesting physician '{physician.physician_code}' deactivated",
        extra={"event": "requesting_physician.soft_deleted", "physician_id": str(physician.id), "user_id": str(user.id)},
    )
    return {"message": "Requesting physician deactivated", "id": str(physician.id)}
