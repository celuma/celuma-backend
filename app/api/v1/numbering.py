"""Monthly numbering configuration and non-consuming preview (issue #8).

Contract: `celuma-engineering/specs/laboratory/monthly-numbering.md`.
Reading the configuration and previewing needs `lab:read` (registration forms
use them); changing the configuration needs `admin:manage_catalog`, the
permission that already governs the base codes. A preview never locks,
increments or writes anything.
"""
import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.api.v1.auth import AuthContext, current_user, get_auth_ctx
from app.core.db import get_session
from app.core.rbac import has_permission
from app.models.numbering import NumberingConfig
from app.models.sample_type import SampleType
from app.models.study_type import StudyType
from app.models.tenant import Branch
from app.models.user import AppUser
from app.schemas.numbering import (
    NumberingConfigResponse,
    NumberingConfigsResponse,
    NumberingConfigUpdate,
    NumberingEstimate,
    NumberingExample,
    NumberingPreviewRequest,
    NumberingPreviewResponse,
)
from app.services import numbering
from app.services.sample_types import record_audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/numbering")

PREVIEW_NOTICE = (
    "Vista previa orientativa: no reserva ni consume ningún número. "
    "El código real se asigna al registrar y puede diferir."
)


def _require(user: AppUser, code: str, session: Session) -> None:
    if not has_permission(user.id, code, session):
        raise HTTPException(403, f"Permission required: {code}")


def _kind(value: str) -> str:
    if value not in numbering.ENTITY_KINDS:
        raise HTTPException(422, "entity_kind must be 'order' or 'sample'")
    return value


def _response(settings: numbering.NumberingSettings) -> NumberingConfigResponse:
    return NumberingConfigResponse(
        entity_kind=settings.entity_kind, format=settings.format,
        sequence_min_width=settings.sequence_min_width, mode=settings.mode, is_default=settings.is_default,
    )


def _problems(fmt: str, width) -> list:
    return numbering.validate_format(fmt) + numbering.validate_min_width(width)


@router.get("/config", response_model=NumberingConfigsResponse)
def get_numbering_config(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Effective configuration of both entity kinds (defaults when unset)."""
    _require(user, "lab:read", session)
    return NumberingConfigsResponse(
        order=_response(numbering.get_settings(session, ctx.tenant_id, numbering.ORDER)),
        sample=_response(numbering.get_settings(session, ctx.tenant_id, numbering.SAMPLE)),
    )


@router.put("/config/{entity_kind}", response_model=NumberingConfigResponse)
def update_numbering_config(
    entity_kind: str,
    body: NumberingConfigUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Set the format, minimum width and mode of one entity kind. Applies to
    allocations that start after this commits; nothing issued changes."""
    _require(user, "admin:manage_catalog", session)
    kind = _kind(entity_kind)
    problems = _problems(body.format, body.sequence_min_width)
    if body.mode not in numbering.MODES:
        problems.append("mode must be one of: " + ", ".join(numbering.MODES))
    if problems:
        raise HTTPException(422, {"code": "numbering_invalid_config", "message": "; ".join(problems)})

    before = numbering.get_settings(session, ctx.tenant_id, kind)
    row = session.exec(
        select(NumberingConfig).where(NumberingConfig.tenant_id == ctx.tenant_id, NumberingConfig.entity_kind == kind)
    ).first()
    if row is None:
        row = NumberingConfig(tenant_id=UUID(ctx.tenant_id), entity_kind=kind, format=body.format,
                              sequence_min_width=body.sequence_min_width, mode=body.mode)
    row.format, row.sequence_min_width, row.mode = body.format, body.sequence_min_width, body.mode
    row.updated_at, row.updated_by = datetime.utcnow(), user.id
    session.add(row)
    record_audit(
        session, tenant_id=ctx.tenant_id, user_id=user.id, action="NUMBERING.CONFIG_UPDATED",
        entity_type="numbering_config", entity_id=ctx.tenant_id,
        description=f"Numbering configuration updated ({kind})",
        old_values={"format": before.format, "sequence_min_width": before.sequence_min_width,
                    "mode": before.mode, "is_default": before.is_default},
        new_values={"format": body.format, "sequence_min_width": body.sequence_min_width, "mode": body.mode},
        metadata={"entity_kind": kind},
    )
    session.commit()
    return _response(numbering.get_settings(session, ctx.tenant_id, kind))


@router.post("/preview", response_model=NumberingPreviewResponse)
def preview_numbering(
    body: NumberingPreviewRequest,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Validate a candidate configuration and show examples plus an estimated
    next code. Reads only: no lock, no counter increment, no ledger row."""
    _require(user, "lab:read", session)
    kind = _kind(body.entity_kind)
    stored = numbering.get_settings(session, ctx.tenant_id, kind)
    fmt = body.format if body.format is not None else stored.format
    width = body.sequence_min_width if body.sequence_min_width is not None else stored.sequence_min_width
    branch = session.get(Branch, UUID(body.branch_id)) if body.branch_id else None
    if body.branch_id and (branch is None or str(branch.tenant_id) != ctx.tenant_id):
        raise HTTPException(404, "Branch not found")
    period = numbering.period_for(branch)

    base_code = "BASE"
    scope = None
    if body.scope_id:
        model = StudyType if kind == numbering.ORDER else SampleType
        try:
            scope = session.get(model, UUID(body.scope_id))
        except ValueError:
            scope = None
        if scope is None or str(scope.tenant_id) != ctx.tenant_id:
            raise HTTPException(404, "Type not found")
        base_code = scope.code

    problems = _problems(fmt, width)
    examples, estimate = [], None
    if not problems:
        candidate = numbering.NumberingSettings(kind, fmt, width, stored.mode, False)
        try:
            examples = [NumberingExample(**e) for e in numbering.boundary_examples(candidate, base_code, period)]
            if scope is not None:
                est = numbering.estimate_next(
                    session, tenant_id=ctx.tenant_id, kind=kind, scope_id=scope.id,
                    base_code=base_code, period=period, settings=candidate,
                )
                estimate = NumberingEstimate(sequence=est.sequence_value, code=est.code, skipped_occupied=est.skipped)
        except HTTPException as exc:  # e.g. rendered code too long for the column
            problems.append(exc.detail["message"] if isinstance(exc.detail, dict) else str(exc.detail))
            examples, estimate = [], None
    return NumberingPreviewResponse(
        entity_kind=kind, valid=not problems, errors=problems, format=fmt, sequence_min_width=width,
        period_year=period.year, period_month=period.month, base_code=base_code,
        examples=examples, estimate=estimate, notice=PREVIEW_NOTICE,
    )
