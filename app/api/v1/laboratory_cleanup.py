"""Privileged laboratory-content cleanup endpoints (Céluma 1.4, issue #39; ADR 0021).

Every route re-checks the authority in the backend (`laboratory_cleanup_authority.require_operator`):
the path tenant must be the caller's own tenant and the caller an ACTIVE holder of the protected system
Superadministrator (`superuser`) role. Hiding the UI entry point is a convenience, never authorization.

This router is separate from ordinary order/sample/report deletion on purpose: no existing
lifecycle endpoint gained a parameter, and signed-report immutability is untouched.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.api.v1.auth import current_user
from app.core.db import get_session
from app.models.laboratory_cleanup import LaboratoryCleanupOperation
from app.models.user import AppUser
from app.services import laboratory_cleanup as service
from app.services.laboratory_cleanup_authority import CleanupOperator, is_authorized, require_operator

router = APIRouter(prefix="/maintenance/tenants/{tenant_id}/laboratory-cleanup")

REPLAY_HEADER = "Idempotent-Replayed"


#: Owner decision (2026-10-28): the cleanup ALWAYS removes the order-linked invoices, items and payments. The old
#: `include_finance` option no longer exists; a caller that still sends it (either value) is told so, never
#: silently given a broader — or different — deletion than the one it believes it approved.
OBSOLETE_OPTION_CODE = "include_finance_not_supported"


def reject_obsolete_options(body: BaseModel) -> None:
    if getattr(body, "include_finance", None) is not None:
        raise HTTPException(422, {
            "code": OBSOLETE_OPTION_CODE,
            "message": "La limpieza siempre incluye las facturas, conceptos y pagos de las órdenes eliminadas; "
                       "la opción include_finance ya no existe. Genera una nueva vista previa.",
        })


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_finance: Optional[bool] = Field(default=None, description="Obsolete: any value is rejected.")


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_token: str = Field(min_length=10, max_length=4096)
    include_finance: Optional[bool] = Field(default=None, description="Obsolete: any value is rejected.")
    reason: str = Field(max_length=5000)
    confirm_name: str = Field(max_length=500)
    # Never logged, stored or echoed (see password_confirmation.py).
    password: Optional[str] = Field(default=None, max_length=2048, repr=False)


def operator_dependency(
    tenant_id: str,
    user: AppUser = Depends(current_user),
    session: Session = Depends(get_session),
) -> CleanupOperator:
    return require_operator(session, user, tenant_id)


@router.get("/authorization")
def authorization(
    tenant_id: str,
    user: AppUser = Depends(current_user),
    session: Session = Depends(get_session),
):
    """UI convenience only: is the caller authorized for THIS tenant? Another tenant's id is a 404."""
    if str(user.tenant_id) != str(tenant_id):
        raise HTTPException(404, "Laboratory not found")
    return {"authorized": is_authorized(session, user, tenant_id)}


@router.post("/preview")
def preview(
    body: PreviewIn,
    operator: CleanupOperator = Depends(operator_dependency),
    session: Session = Depends(get_session),
):
    reject_obsolete_options(body)
    return service.preview(session, operator)


@router.post("/operations", status_code=202)
def start_operation(
    body: StartIn,
    background: BackgroundTasks,
    response: Response,
    operator: CleanupOperator = Depends(operator_dependency),
    session: Session = Depends(get_session),
):
    reject_obsolete_options(body)
    result = service.start(
        session, operator,
        preview_token=body.preview_token, reason=body.reason,
        confirm_name=body.confirm_name, password=body.password,
    )
    op = result.operation
    if result.replayed:
        response.headers[REPLAY_HEADER] = "true"
    else:
        background.add_task(service.run_operation, session.get_bind(), op.id)
    session.refresh(op)
    return service.view(session, op)


@router.get("/operations")
def list_operations(
    operator: CleanupOperator = Depends(operator_dependency),
    session: Session = Depends(get_session),
) -> List[dict]:
    rows = session.exec(
        select(LaboratoryCleanupOperation)
        .where(LaboratoryCleanupOperation.tenant_id == operator.tenant.id)
        .order_by(LaboratoryCleanupOperation.created_at.desc())
        .limit(50)
    ).all()
    return [service.view(session, op) for op in rows]


@router.get("/operations/{operation_id}")
def read_operation(
    operation_id: str,
    operator: CleanupOperator = Depends(operator_dependency),
    session: Session = Depends(get_session),
):
    return service.view(session, service.get_operation(session, operator.tenant.id, operation_id))


@router.post("/operations/{operation_id}/retry", status_code=202)
def retry_operation(
    operation_id: str,
    background: BackgroundTasks,
    operator: CleanupOperator = Depends(operator_dependency),
    session: Session = Depends(get_session),
):
    op = service.get_operation(session, operator.tenant.id, operation_id)
    if not service.can_retry(op):
        code = "operation_in_progress" if op.status in ("QUEUED", "RUNNING") else "retry_not_allowed"
        raise HTTPException(409, {
            "code": code,
            "message": "La limpieza sigue en ejecución." if code == "operation_in_progress"
            else "Esta limpieza ya no admite reintentos.",
        })
    background.add_task(service.run_operation, session.get_bind(), op.id)
    return service.view(session, op)


@router.post("/operations/{operation_id}/cancel")
def cancel_operation(
    operation_id: str,
    operator: CleanupOperator = Depends(operator_dependency),
    session: Session = Depends(get_session),
):
    op = service.get_operation(session, operator.tenant.id, operation_id)
    op = service.request_cancel(session, operator, op)
    return service.view(session, op)
