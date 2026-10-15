from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, func, select
from app.core.db import get_session
from app.api.v1.auth import AuthContext, current_user, get_auth_ctx
from app.core.rbac import has_permission
from app.models.laboratory import Order
from app.models.requesting_physician import RequestingPhysician
from app.models.tenant import Branch, Tenant
from app.models.user import AppUser
from app.services.laboratory_lists import build_order_items, order_list_statement
from app.schemas.laboratory import (
    OrdersListResponse,
    RequestingPhysicianRef,
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
        code = f"MS-{next_sequence}"
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
        order_list_statement(ctx.tenant_id, extra_where=(Order.requesting_physician_id == physician.id,))
    ).all()
    return OrdersListResponse(orders=build_order_items(session, orders), total=len(orders))


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
