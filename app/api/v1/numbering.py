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


def _response(settings: numbering.NumberingSettings, permitted: bool, activated_at=None) -> NumberingConfigResponse:
    return NumberingConfigResponse(
        entity_kind=settings.entity_kind, active=settings.active, manual_policy=settings.manual_policy,
        format=settings.format, sequence_min_width=settings.sequence_min_width, start_value=settings.start_value,
        scope=settings.scope, timezone=settings.timezone, is_default=settings.is_default,
        manual_entry=numbering.manual_entry_for(settings, permitted),
        activated_at=activated_at.isoformat() if activated_at else None,
    )


def _activated_at(session: Session, tenant_id, kind: str):
    row = session.exec(
        select(NumberingConfig).where(NumberingConfig.tenant_id == tenant_id, NumberingConfig.entity_kind == kind)
    ).first()
    return row.activated_at if row else None


def _problems(fmt: str, width, start, scope: str, tz: str = numbering.DEFAULT_TIMEZONE) -> list:
    problems = []
    if scope not in numbering.SCOPES:
        problems.append("scope must be one of: " + ", ".join(numbering.SCOPES))
        scope = numbering.SCOPE_TENANT
    return problems + numbering.validate_format(fmt, scope) + numbering.validate_min_width(width) + numbering.validate_start_value(start) + numbering.validate_timezone(tz)


@router.get("/config", response_model=NumberingConfigsResponse)
def get_numbering_config(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Effective configuration of both entity kinds (inactive defaults when unset), with how the
    caller should be offered the code input."""
    _require(user, "lab:read", session)
    permitted = numbering.can_enter_manual(session, user)
    return NumberingConfigsResponse(
        order=_response(numbering.get_settings(session, ctx.tenant_id, numbering.ORDER), permitted,
                        _activated_at(session, ctx.tenant_id, numbering.ORDER)),
        sample=_response(numbering.get_settings(session, ctx.tenant_id, numbering.SAMPLE), permitted,
                         _activated_at(session, ctx.tenant_id, numbering.SAMPLE)),
    )


@router.put("/config/{entity_kind}", response_model=NumberingConfigResponse)
def update_numbering_config(
    entity_kind: str,
    body: NumberingConfigUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Configure one entity kind (including its deliberate activation).

    Effective for allocations that START after this commits. Never rewinds a
    counter, never touches an issued code or the ledger: a raised start value
    moves counters forward, a lowered one changes nothing already reached;
    switching scope or format opens counters under their own keys and every
    rendered code still goes through the occupied-code skip.
    """
    _require(user, "admin:manage_catalog", session)
    kind = _kind(entity_kind)
    problems = _problems(body.format, body.sequence_min_width, body.start_value, body.scope, body.timezone)
    if body.manual_policy not in numbering.MANUAL_POLICIES:
        problems.append("manual_policy must be one of: " + ", ".join(numbering.MANUAL_POLICIES))
    if problems:
        raise HTTPException(422, {"code": "numbering_invalid_config", "message": "; ".join(problems)})

    before = numbering.get_settings(session, ctx.tenant_id, kind)
    row = session.exec(
        select(NumberingConfig).where(NumberingConfig.tenant_id == ctx.tenant_id, NumberingConfig.entity_kind == kind)
    ).first()
    if row is None:
        row = NumberingConfig(tenant_id=UUID(ctx.tenant_id), entity_kind=kind, format=body.format,
                              sequence_min_width=body.sequence_min_width)
    now = datetime.utcnow()
    if body.active and not row.activated_at:
        row.activated_at = now
    row.active, row.manual_policy, row.format = body.active, body.manual_policy, body.format
    row.sequence_min_width, row.start_value, row.scope = body.sequence_min_width, body.start_value, body.scope
    row.timezone = body.timezone
    row.updated_at, row.updated_by = now, user.id
    session.add(row)

    def snap(x):
        return {"active": x.active, "manual_policy": x.manual_policy, "format": x.format,
                "sequence_min_width": x.sequence_min_width, "start_value": x.start_value, "scope": x.scope,
                "timezone": x.timezone}

    record_audit(
        session, tenant_id=ctx.tenant_id, user_id=user.id, action="NUMBERING.CONFIG_UPDATED",
        entity_type="numbering_config", entity_id=ctx.tenant_id,
        description=f"Numbering configuration updated ({kind})",
        old_values={**snap(before), "is_default": before.is_default},
        new_values=snap(body),
        metadata={"entity_kind": kind, "activated": bool(body.active and not before.active)},
    )
    session.commit()
    return _response(numbering.get_settings(session, ctx.tenant_id, kind), numbering.can_enter_manual(session, user),
                     row.activated_at)


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
    pick = lambda value, default: default if value is None else value  # noqa: E731
    candidate = numbering.NumberingSettings(
        kind, pick(body.format, stored.format), pick(body.sequence_min_width, stored.sequence_min_width),
        stored.manual_policy, pick(body.start_value, stored.start_value), pick(body.scope, stored.scope),
        timezone=pick(body.timezone, stored.timezone), active=pick(body.active, stored.active), is_default=False,
    )
    branch = None
    if body.branch_id:
        try:
            branch = session.get(Branch, UUID(body.branch_id))
        except ValueError:
            branch = None
        if branch is None or str(branch.tenant_id) != ctx.tenant_id:
            raise HTTPException(404, "Branch not found")
    period = numbering.period_in_zone(candidate.zone_name(branch.timezone if branch else None))

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

    problems = _problems(candidate.format, candidate.sequence_min_width, candidate.start_value, candidate.scope, candidate.timezone)
    examples, estimate = [], None
    branch_code = branch.code if branch else "BRANCH"
    if not problems:
        try:
            if candidate.scheme == numbering.SCHEME_MONTHLY:
                examples = [NumberingExample(**e) for e in numbering.boundary_examples(candidate, base_code, period, branch_code)]
            else:  # compatibility scheme: `<code>-<n>`
                examples = [NumberingExample(sequence=n, code=f"{base_code}-{n}") for n in numbering.PREVIEW_SEQUENCES]
            if scope is not None:
                est = numbering.estimate_next(
                    session, tenant_id=ctx.tenant_id, kind=kind, scope_id=scope.id, base_code=base_code,
                    period=period, settings=candidate, branch_id=branch.id if branch else None,
                    branch_code=branch_code,
                )
                estimate = NumberingEstimate(sequence=est.sequence_value, code=est.code, skipped_occupied=est.skipped)
        except HTTPException as exc:  # e.g. rendered code too long, unsafe branch code
            problems.append(exc.detail["message"] if isinstance(exc.detail, dict) else str(exc.detail))
            examples, estimate = [], None
    return NumberingPreviewResponse(
        entity_kind=kind, valid=not problems, errors=problems, format=candidate.format,
        sequence_min_width=candidate.sequence_min_width, scheme=candidate.scheme,
        timezone=candidate.zone_name(branch.timezone if branch else None),
        period_year=period.year, period_month=period.month, base_code=base_code,
        examples=examples, estimate=estimate, notice=PREVIEW_NOTICE,
    )
