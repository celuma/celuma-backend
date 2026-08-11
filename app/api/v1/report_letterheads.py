"""Administration endpoints for the shared, tenant-owned letterhead
domain — post-Phase-2 remediation, R6.

Mirrors the existing report-template-version pattern (`reports.py`'s
`/templates/{id}/versions...` endpoints) exactly: an append-only, immutable
`ReportLetterheadVersion` published under a mutable `ReportLetterhead`
shell. See report-letterhead-domain-contract.md and
report-letterhead-version-contract.md for the full rationale.

Permission reuse: every mutating endpoint here requires
`reports:manage_templates` (not a new `reports:manage_letterheads`
permission) — same actors (lab admin, superuser) already manage report
configuration under that permission. See
remediation-architecture-decision.md §5 for the reasoning and the
documented reversal path if a future block needs to split it.
"""
import re
import unicodedata
from datetime import datetime
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse
from sqlmodel import Session, select
from pydantic import ValidationError as PydanticValidationError

from app.api.v1.auth import get_auth_ctx, AuthContext, current_user
from app.core.db import get_session
from app.core.rbac import has_permission
from app.models.report import ReportTemplate, ReportVersion
from app.models.report_letterhead import ReportLetterhead
from app.models.report_letterhead_version import (
    ReportLetterheadVersion,
    ReportLetterheadVersionStatus,
)
from app.models.storage import StorageObject
from app.models.user import AppUser
from app.schemas.report_letterhead import (
    ReportLetterheadCreate,
    ReportLetterheadUpdate,
    ReportLetterheadResponse,
    ReportLetterheadDetailResponse,
    ReportLetterheadsListResponse,
    ReportLetterheadVersionCreate,
    ReportLetterheadVersionResponse,
    ReportLetterheadVersionDetailResponse,
    ReportLetterheadVersionsListResponse,
    ReportLetterheadLogoUploadResponse,
    CelumaLetterheadEnvelope,
)
from app.services.managed_tenant_image_service import (
    ManagedTenantImageService,
    InvalidImageError,
    ImageRegistrationError,
)
from app.services.letterhead_portability import (
    export_letterhead_version,
    import_letterhead_version,
    CelumaPortabilityError,
)
from app.services.legacy_letterhead_adapter import build_legacy_letterhead_export
from app.services.letterhead_resources import resolve_letterhead_resources
from app.services.usage import UsageService
from app.services.letterhead_resolution import (
    LetterheadConfigurationError,
    sole_active_version,
)
import json
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/report-letterheads")


def _require(user_id, code: str, session: Session) -> None:
    if not has_permission(user_id, code, session):
        raise HTTPException(403, f"Permission required: {code}")


def _get_owned_letterhead(
    letterhead_id: str, ctx: AuthContext, session: Session
) -> ReportLetterhead:
    letterhead = session.get(ReportLetterhead, letterhead_id)
    if not letterhead or str(letterhead.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Letterhead not found")
    return letterhead


def _get_owned_letterhead_version(
    letterhead_id: str, version_id: str, ctx: AuthContext, session: Session
) -> ReportLetterheadVersion:
    version = session.get(ReportLetterheadVersion, version_id)
    if (
        not version
        or str(version.report_letterhead_id) != letterhead_id
        or str(version.tenant_id) != ctx.tenant_id
    ):
        raise HTTPException(404, "Letterhead version not found")
    return version


def _blocking_references(session: Session, letterhead: ReportLetterhead) -> list[str]:
    """Reasons why this letterhead cannot be physically deleted.

    Third post-Phase-2 remediation — safe-deletion policy (see
    letterhead-delete-deactivate-contract.md). ALL references are checked,
    not just the first, so the user can be told everything they would need
    to resolve first. Never cascades over reports or snapshots: if anything
    references it, the letterhead is kept.
    """
    reasons: list[str] = []

    if letterhead.is_default:
        reasons.append("es el membrete predeterminado del laboratorio")

    preferred_by = session.exec(
        select(ReportTemplate.name).where(
            ReportTemplate.preferred_letterhead_id == letterhead.id
        )
    ).all()
    version_preferred_by = session.exec(
        select(ReportTemplate.name)
        .join(
            ReportLetterheadVersion,
            ReportLetterheadVersion.id == ReportTemplate.preferred_letterhead_version_id,
        )
        .where(ReportLetterheadVersion.report_letterhead_id == letterhead.id)
    ).all()
    template_names = sorted({*preferred_by, *version_preferred_by})
    if template_names:
        listed = ", ".join(f"«{n}»" for n in template_names[:3])
        more = f" y {len(template_names) - 3} más" if len(template_names) > 3 else ""
        reasons.append(f"está configurado como membrete de {listed}{more}")

    used_by_reports = session.exec(
        select(ReportVersion.id)
        .join(
            ReportLetterheadVersion,
            ReportLetterheadVersion.id == ReportVersion.letterhead_version_id,
        )
        .where(ReportLetterheadVersion.report_letterhead_id == letterhead.id)
    ).all()
    if used_by_reports:
        reasons.append(f"lo usan {len(used_by_reports)} reporte(s) ya creados")

    return reasons


def _has_active_version(session: Session, letterhead: ReportLetterhead) -> bool:
    return (
        session.exec(
            select(ReportLetterheadVersion.id).where(
                ReportLetterheadVersion.report_letterhead_id == letterhead.id,
                ReportLetterheadVersion.status == ReportLetterheadVersionStatus.ACTIVE,
            )
        ).first()
        is not None
    )


def _letterhead_response(
    l: ReportLetterhead, session: Session | None = None
) -> ReportLetterheadResponse:
    references = _blocking_references(session, l) if session is not None else []
    return ReportLetterheadResponse(
        id=str(l.id),
        tenant_id=str(l.tenant_id),
        name=l.name,
        description=l.description,
        is_default=l.is_default,
        is_active=l.is_active,
        created_at=l.created_at,
        has_active_version=_has_active_version(session, l) if session is not None else False,
        can_hard_delete=(session is not None and not references),
        blocking_references=references,
    )


def _validate_logo_references(
    configuration, letterhead: ReportLetterhead, session: Session
) -> None:
    """Both `logo_storage_id` values must exist and belong to this tenant.

    Third remediation: previously only the header one was validated on
    `POST .../versions` — the footer one was persisted unchecked, so a
    missing or cross-tenant id was stored and later resolved to no URL,
    producing the symptom "Remove appears but the logo is not shown"
    (problem C).
    """
    for slot, label in (("header", "logo_storage_id"), ("footer", "footer.logo_storage_id")):
        storage_id = getattr(getattr(configuration, slot), "logo_storage_id", None)
        if storage_id is None:
            continue
        logo_object = session.get(StorageObject, storage_id)
        if not logo_object:
            raise HTTPException(400, f"{label} does not reference an existing object")
        if str(logo_object.tenant_id) != str(letterhead.tenant_id):
            raise HTTPException(
                400, f"{label} does not reference an object owned by this tenant"
            )


def _version_detail_response(
    version: ReportLetterheadVersion, session: Session
) -> ReportLetterheadVersionDetailResponse:
    """Version detail + ephemeral URLs for its logos. Sole constructor of
    this response, so no endpoint can forget `resolved_resources`
    (problems B and C were born exactly from that)."""
    resolved = resolve_letterhead_resources(
        version.configuration, str(version.tenant_id), session
    )
    return ReportLetterheadVersionDetailResponse(
        **_version_response(version).model_dump(),
        configuration=version.configuration,
        resolved_resources=resolved.model_dump() if resolved else None,
    )


def _version_response(v: ReportLetterheadVersion) -> ReportLetterheadVersionResponse:
    return ReportLetterheadVersionResponse(
        id=str(v.id),
        tenant_id=str(v.tenant_id),
        report_letterhead_id=str(v.report_letterhead_id),
        version_number=v.version_number,
        schema_version=v.schema_version,
        status=v.status,
        created_by=str(v.created_by) if v.created_by else None,
        published_at=v.published_at,
        activated_at=v.activated_at,
        archived_at=v.archived_at,
    )


# ============================================================================
# ReportLetterhead CRUD
# ============================================================================

@router.get("/", response_model=ReportLetterheadsListResponse)
def list_letterheads(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
    active_only: bool = True,
):
    """List all letterheads shared across this tenant's templates (requires reports:read)."""
    _require(user.id, "reports:read", session)
    query = select(ReportLetterhead).where(ReportLetterhead.tenant_id == ctx.tenant_id)
    if active_only:
        query = query.where(ReportLetterhead.is_active == True)
    letterheads = session.exec(query.order_by(ReportLetterhead.created_at.asc())).all()
    return ReportLetterheadsListResponse(
        letterheads=[_letterhead_response(l, session) for l in letterheads]
    )


@router.get("/{letterhead_id}", response_model=ReportLetterheadDetailResponse)
def get_letterhead(
    letterhead_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Get a specific letterhead by ID (requires reports:read)."""
    _require(user.id, "reports:read", session)
    letterhead = _get_owned_letterhead(letterhead_id, ctx, session)
    return ReportLetterheadDetailResponse(
        **_letterhead_response(letterhead, session).model_dump(),
        created_by=str(letterhead.created_by) if letterhead.created_by else None,
    )


@router.post("/", response_model=ReportLetterheadResponse)
def create_letterhead(
    data: ReportLetterheadCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new letterhead shell (requires reports:manage_templates)."""
    _require(user.id, "reports:manage_templates", session)
    letterhead = ReportLetterhead(
        tenant_id=ctx.tenant_id,
        name=data.name,
        description=data.description,
        created_by=user.id,
        is_active=True,
    )
    session.add(letterhead)
    session.commit()
    session.refresh(letterhead)

    logger.info(
        f"Letterhead '{letterhead.name}' created",
        extra={
            "event": "report_letterhead.created",
            "letterhead_id": str(letterhead.id),
            "user_id": str(user.id),
        },
    )
    return _letterhead_response(letterhead)


@router.put("/{letterhead_id}", response_model=ReportLetterheadResponse)
def update_letterhead(
    letterhead_id: str,
    data: ReportLetterheadUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Rename/describe/toggle active (requires reports:manage_templates)."""
    _require(user.id, "reports:manage_templates", session)
    letterhead = _get_owned_letterhead(letterhead_id, ctx, session)

    if data.name is not None:
        letterhead.name = data.name
    # Fourth post-Phase-2 remediation (Observation 2) — ROOT CAUSE of
    # "description cannot be cleared": this block was
    # `if data.description is not None`, so sending `null` (or `""`, which
    # the schema normalizes to `null`) was treated as "do not touch" and
    # the previous text survived forever. `model_fields_set` is the only
    # way to distinguish "field omitted" from "field sent as null" — see
    # optional-letterhead-description-contract.md.
    if "description" in data.model_fields_set:
        letterhead.description = data.description
    if data.is_active is not None:
        if not data.is_active and letterhead.is_default:
            # Third remediation: a deactivated letterhead cannot be the
            # default (resolution would treat it as "no default" and block
            # V2 without anyone asking). Another default must be chosen
            # first — never reassign one at random.
            raise HTTPException(
                409,
                "No se puede desactivar el membrete predeterminado. "
                "Marca otro como predeterminado primero.",
            )
        letterhead.is_active = data.is_active

    session.add(letterhead)
    session.commit()
    session.refresh(letterhead)

    logger.info(
        f"Letterhead '{letterhead.name}' updated",
        extra={
            "event": "report_letterhead.updated",
            "letterhead_id": str(letterhead.id),
            "user_id": str(user.id),
        },
    )
    return _letterhead_response(letterhead, session)


@router.delete("/{letterhead_id}")
def delete_letterhead(
    letterhead_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
    hard_delete: bool = False,
):
    """Delete (`hard_delete=true`) or deactivate (default) a letterhead
    — requires reports:manage_templates.

    Third post-Phase-2 remediation — integrity policy (see
    letterhead-delete-deactivate-contract.md):

      * PHYSICAL delete only when NOTHING references it: it is not the
        tenant default, no template has it as preferred, and no
        `ReportVersion` uses it. Its versions are deleted with it (they
        belong to it and nobody else looks at them); logo `StorageObject`s
        are NOT touched — they may be shared and their lifecycle is
        storage's, not this domain's.
      * If there are references -> 409 with detail of what blocks it.
        Never a destructive cascade over reports or snapshots.
      * Deactivate is the exit when there is history: it stops being
        offered for new reports, keeps versions and logos, and does not
        alter any existing report. Deactivating the default is rejected
        (another must be chosen first).

    Change vs the previous remediation: having published versions no
    longer blocks deletion. That made any letterhead that had ever been
    saved undeletable — i.e. all of them — which is exactly brief problem
    D. What matters is who references it, not how many internal revisions
    it accumulated.
    """
    _require(user.id, "reports:manage_templates", session)
    letterhead = _get_owned_letterhead(letterhead_id, ctx, session)

    references = _blocking_references(session, letterhead)

    if hard_delete:
        if references:
            raise HTTPException(
                409,
                "No se puede eliminar «"
                + letterhead.name
                + "» porque "
                + "; ".join(references)
                + ". Desactívalo en su lugar: dejará de ofrecerse para reportes "
                "nuevos y los reportes ya creados no cambian.",
            )

        versions = session.exec(
            select(ReportLetterheadVersion).where(
                ReportLetterheadVersion.report_letterhead_id == letterhead.id
            )
        ).all()
        for version in versions:
            session.delete(version)
        # Explicit flush: without it SQLAlchemy may emit the parent DELETE
        # before its versions and the FK rejects it.
        session.flush()
        session.delete(letterhead)
        session.commit()

        logger.info(
            f"Letterhead '{letterhead.name}' permanently deleted",
            extra={
                "event": "report_letterhead.hard_deleted",
                "letterhead_id": letterhead_id,
                "deleted_versions": len(versions),
                "user_id": str(user.id),
            },
        )
        return {"message": "Membrete eliminado", "id": letterhead_id}

    if letterhead.is_default:
        raise HTTPException(
            409,
            "No se puede desactivar el membrete predeterminado. "
            "Marca otro como predeterminado primero.",
        )

    letterhead.is_active = False
    session.add(letterhead)
    session.commit()

    logger.info(
        f"Letterhead '{letterhead.name}' deactivated",
        extra={
            "event": "report_letterhead.deactivated",
            "letterhead_id": str(letterhead.id),
            "user_id": str(user.id),
        },
    )
    return {"message": "Membrete desactivado", "id": str(letterhead.id)}


@router.post("/{letterhead_id}/duplicate", response_model=ReportLetterheadResponse)
def duplicate_letterhead(
    letterhead_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new letterhead shell, cloning the source's ACTIVE (or, if
    none, latest PUBLISHED) version as a fresh ACTIVE version under the
    new shell (requires reports:manage_templates).

    Third remediation: the copy is born ACTIVE, not PUBLISHED, for the
    same reason as import (see `import_letterhead_version`) — a letterhead
    without an ACTIVE version is invisible to the editor
    (`GET .../versions/active` returns 404) and appears to have lost all
    its configuration."""
    _require(user.id, "reports:manage_templates", session)
    source = _get_owned_letterhead(letterhead_id, ctx, session)

    source_version = session.exec(
        select(ReportLetterheadVersion)
        .where(
            ReportLetterheadVersion.report_letterhead_id == source.id,
            ReportLetterheadVersion.status != ReportLetterheadVersionStatus.ARCHIVED,
        )
        .order_by(ReportLetterheadVersion.version_number.desc())
    ).first()

    new_letterhead = ReportLetterhead(
        tenant_id=ctx.tenant_id,
        name=f"{source.name} (copia)",
        description=source.description,
        created_by=user.id,
        is_active=True,
    )
    session.add(new_letterhead)
    session.flush()

    if source_version is not None:
        clone = ReportLetterheadVersion(
            tenant_id=ctx.tenant_id,
            report_letterhead_id=new_letterhead.id,
            version_number=1,
            schema_version=source_version.schema_version,
            configuration=source_version.configuration,
            status=ReportLetterheadVersionStatus.ACTIVE,
            created_by=user.id,
            activated_at=datetime.utcnow(),
        )
        session.add(clone)

    session.commit()
    session.refresh(new_letterhead)

    logger.info(
        f"Letterhead '{source.name}' duplicated as '{new_letterhead.name}'",
        extra={
            "event": "report_letterhead.duplicated",
            "source_letterhead_id": letterhead_id,
            "new_letterhead_id": str(new_letterhead.id),
            "user_id": str(user.id),
        },
    )
    return _letterhead_response(new_letterhead, session)


@router.post("/{letterhead_id}/default", response_model=ReportLetterheadResponse)
def set_default_letterhead(
    letterhead_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Mark a letterhead as the tenant default (requires reports:manage_templates).

    At most one default per tenant (also enforced by a partial unique index
    at the database level). Does not modify published versions, existing
    reports, or snapshots — see template-letterhead-association-contract.md.

    Third remediation: the letterhead must be active and have exactly one
    ACTIVE version. Marking as default something resolution cannot resolve
    was one way to end up with "default configured but V2 blocked /
    another letterhead comes out".
    """
    _require(user.id, "reports:manage_templates", session)
    letterhead = _get_owned_letterhead(letterhead_id, ctx, session)

    if not letterhead.is_active:
        raise HTTPException(
            409,
            "No se puede marcar como predeterminado un membrete desactivado. "
            "Reactívalo primero.",
        )
    if not _has_active_version(session, letterhead):
        raise HTTPException(
            409,
            "Este membrete todavía no tiene ninguna configuración guardada. "
            "Ábrelo, guarda su diseño y vuelve a marcarlo como predeterminado.",
        )

    previous_default = session.exec(
        select(ReportLetterhead).where(
            ReportLetterhead.tenant_id == ctx.tenant_id,
            ReportLetterhead.is_default == True,
            ReportLetterhead.id != letterhead.id,
        )
    ).first()
    if previous_default:
        previous_default.is_default = False
        session.add(previous_default)
        # Same per-statement partial-unique-index ordering concern as
        # activate_template_version — flush the demotion first.
        session.flush()

    letterhead.is_default = True
    session.add(letterhead)
    session.commit()
    session.refresh(letterhead)

    logger.info(
        f"Letterhead '{letterhead.name}' set as tenant default",
        extra={
            "event": "report_letterhead.default_set",
            "letterhead_id": letterhead_id,
            "user_id": str(user.id),
        },
    )
    return _letterhead_response(letterhead, session)


# ============================================================================
# ReportLetterheadVersion (append-only, immutable)
# ============================================================================

@router.get(
    "/{letterhead_id}/versions", response_model=ReportLetterheadVersionsListResponse
)
def list_letterhead_versions(
    letterhead_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """List all published versions of a letterhead, newest first (requires reports:manage_templates)."""
    _require(user.id, "reports:manage_templates", session)
    _get_owned_letterhead(letterhead_id, ctx, session)
    versions = session.exec(
        select(ReportLetterheadVersion)
        .where(ReportLetterheadVersion.report_letterhead_id == letterhead_id)
        .order_by(ReportLetterheadVersion.version_number.desc())
    ).all()
    return ReportLetterheadVersionsListResponse(
        versions=[_version_response(v) for v in versions]
    )


@router.get(
    "/{letterhead_id}/versions/active",
    response_model=ReportLetterheadVersionDetailResponse,
)
def get_active_letterhead_version(
    letterhead_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Second post-Phase-2 remediation (UX): a letterhead's current ACTIVE
    configuration, to preload the visual editor in "Edit" mode. 404 if the
    letterhead has no ACTIVE version yet (just created).

    Registered BEFORE `GET /{letterhead_id}/versions/{version_id}` on
    purpose: FastAPI/Starlette resolves routes in registration order, and
    "active" also matches the `{version_id}` pattern — if this endpoint
    were registered later it would be unreachable (the parametric route
    would intercept first, trying to use the string "active" as a UUID).
    """
    _require(user.id, "reports:manage_templates", session)
    letterhead = _get_owned_letterhead(letterhead_id, ctx, session)
    try:
        active = sole_active_version(session, letterhead)
    except LetterheadConfigurationError as exc:
        # Zero ACTIVE versions is the normal case for a freshly created
        # letterhead -> 404, which the editor translates to "start blank".
        # More than one is data corruption -> explicit 409, never pick one
        # at random (brief problem E).
        if _has_active_version(session, letterhead):
            raise HTTPException(409, exc.message) from None
        raise HTTPException(404, "This letterhead has no active version yet") from None
    return _version_detail_response(active, session)


@router.put(
    "/{letterhead_id}/versions/current",
    response_model=ReportLetterheadVersionDetailResponse,
)
def save_current_letterhead_version(
    letterhead_id: str,
    payload: ReportLetterheadVersionCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Second post-Phase-2 remediation (UX): letterhead visual editor
    "Save changes" — replacement for "Publish version" (which stayed
    PUBLISHED without activating). Creates a new `ReportLetterheadVersion`
    and activates it atomically, archiving the previous ACTIVE. No-op
    (returns the existing ACTIVE version without creating anything) if the
    sent configuration is identical to the already-active one — avoids
    history noise on a "Save" with no real changes.

    `POST .../versions` + `POST .../{id}/activate` (below) remain as-is
    for the secondary history/rollback flow ("New version from this",
    "Restore"). Registered before `GET .../versions/{version_id}` for the
    same route-order reason explained above (PUT does not collide with
    that GET route, but it is kept next to its sibling endpoint for
    clarity).
    """
    _require(user.id, "reports:manage_templates", session)
    letterhead = _get_owned_letterhead(letterhead_id, ctx, session)
    _validate_logo_references(payload.configuration, letterhead, session)

    new_configuration = payload.configuration.model_dump(mode="json")

    active = session.exec(
        select(ReportLetterheadVersion).where(
            ReportLetterheadVersion.report_letterhead_id == letterhead.id,
            ReportLetterheadVersion.status == ReportLetterheadVersionStatus.ACTIVE,
        )
    ).first()
    if active is not None and active.configuration == new_configuration:
        return _version_detail_response(active, session)

    if active is not None:
        # Demote before promoting — mismo orden que activate_letterhead_version,
        # required by the partial unique index "one ACTIVE per letterhead".
        active.status = ReportLetterheadVersionStatus.PUBLISHED
        session.add(active)
        session.flush()

    last_version = session.exec(
        select(ReportLetterheadVersion)
        .where(ReportLetterheadVersion.report_letterhead_id == letterhead.id)
        .order_by(ReportLetterheadVersion.version_number.desc())
    ).first()
    next_version_number = (last_version.version_number + 1) if last_version else 1

    new_version = ReportLetterheadVersion(
        tenant_id=letterhead.tenant_id,
        report_letterhead_id=letterhead.id,
        version_number=next_version_number,
        schema_version=2,
        configuration=new_configuration,
        status=ReportLetterheadVersionStatus.ACTIVE,
        created_by=user.id,
        activated_at=datetime.utcnow(),
    )
    session.add(new_version)
    session.commit()
    session.refresh(new_version)

    logger.info(
        f"Letterhead version {new_version.version_number} saved and activated for letterhead {letterhead_id}",
        extra={
            "event": "report_letterhead_version.saved_and_activated",
            "letterhead_id": letterhead_id,
            "version_id": str(new_version.id),
            "user_id": str(user.id),
        },
    )

    return _version_detail_response(new_version, session)


@router.get(
    "/{letterhead_id}/versions/{version_id}",
    response_model=ReportLetterheadVersionDetailResponse,
)
def get_letterhead_version(
    letterhead_id: str,
    version_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Get a specific letterhead version, including its full immutable configuration."""
    _require(user.id, "reports:manage_templates", session)
    _get_owned_letterhead(letterhead_id, ctx, session)
    version = _get_owned_letterhead_version(letterhead_id, version_id, ctx, session)
    return _version_detail_response(version, session)


@router.post(
    "/{letterhead_id}/versions", response_model=ReportLetterheadVersionDetailResponse
)
def create_letterhead_version(
    letterhead_id: str,
    payload: ReportLetterheadVersionCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Publish a new, immutable version of a letterhead's presentation.

    `payload.configuration` is validated against `ReportPresentationSnapshotV2`
    by FastAPI/Pydantic before this function runs. Append-only: this is the
    only way to add a version; there is no endpoint to edit one afterwards.
    """
    _require(user.id, "reports:manage_templates", session)
    letterhead = _get_owned_letterhead(letterhead_id, ctx, session)
    _validate_logo_references(payload.configuration, letterhead, session)

    last_version = session.exec(
        select(ReportLetterheadVersion)
        .where(ReportLetterheadVersion.report_letterhead_id == letterhead.id)
        .order_by(ReportLetterheadVersion.version_number.desc())
    ).first()
    next_version_number = (last_version.version_number + 1) if last_version else 1

    version = ReportLetterheadVersion(
        tenant_id=letterhead.tenant_id,
        report_letterhead_id=letterhead.id,
        version_number=next_version_number,
        schema_version=2,
        configuration=payload.configuration.model_dump(mode="json"),
        status=ReportLetterheadVersionStatus.PUBLISHED,
        created_by=user.id,
    )
    session.add(version)
    session.commit()
    session.refresh(version)

    logger.info(
        f"Letterhead version {version.version_number} published for letterhead {letterhead_id}",
        extra={
            "event": "report_letterhead_version.published",
            "letterhead_id": letterhead_id,
            "version_id": str(version.id),
            "user_id": str(user.id),
        },
    )

    return _version_detail_response(version, session)


@router.post(
    "/{letterhead_id}/versions/{version_id}/activate",
    response_model=ReportLetterheadVersionResponse,
)
def activate_letterhead_version(
    letterhead_id: str,
    version_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Mark a version as the letterhead's active/default version.

    At most one version is ACTIVE per letterhead at a time (also enforced
    by a partial unique index at the database level).
    """
    _require(user.id, "reports:manage_templates", session)
    _get_owned_letterhead(letterhead_id, ctx, session)
    version = _get_owned_letterhead_version(letterhead_id, version_id, ctx, session)

    previous_active = session.exec(
        select(ReportLetterheadVersion).where(
            ReportLetterheadVersion.report_letterhead_id == letterhead_id,
            ReportLetterheadVersion.status == ReportLetterheadVersionStatus.ACTIVE,
            ReportLetterheadVersion.id != version.id,
        )
    ).first()
    if previous_active:
        previous_active.status = ReportLetterheadVersionStatus.PUBLISHED
        session.add(previous_active)
        session.flush()

    version.status = ReportLetterheadVersionStatus.ACTIVE
    version.activated_at = datetime.utcnow()
    version.archived_at = None
    session.add(version)
    session.commit()
    session.refresh(version)

    logger.info(
        f"Letterhead version {version_id} activated for letterhead {letterhead_id}",
        extra={
            "event": "report_letterhead_version.activated",
            "letterhead_id": letterhead_id,
            "version_id": version_id,
            "user_id": str(user.id),
        },
    )
    return _version_response(version)


@router.post(
    "/{letterhead_id}/versions/{version_id}/archive",
    response_model=ReportLetterheadVersionResponse,
)
def archive_letterhead_version(
    letterhead_id: str,
    version_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Retire a version so it can no longer be selected for new reports.

    Existing reports that already reference this version are unaffected:
    their rendering snapshot was already embedded in their own JSON body at
    creation time and is never re-read from here.
    """
    _require(user.id, "reports:manage_templates", session)
    _get_owned_letterhead(letterhead_id, ctx, session)
    version = _get_owned_letterhead_version(letterhead_id, version_id, ctx, session)

    if version.status == ReportLetterheadVersionStatus.ACTIVE:
        raise HTTPException(
            409,
            "Cannot archive the active version. Activate a replacement version first.",
        )
    if version.status == ReportLetterheadVersionStatus.ARCHIVED:
        raise HTTPException(400, "Version is already archived")

    version.status = ReportLetterheadVersionStatus.ARCHIVED
    version.archived_at = datetime.utcnow()
    session.add(version)
    session.commit()
    session.refresh(version)

    logger.info(
        f"Letterhead version {version_id} archived for letterhead {letterhead_id}",
        extra={
            "event": "report_letterhead_version.archived",
            "letterhead_id": letterhead_id,
            "version_id": version_id,
            "user_id": str(user.id),
        },
    )
    return _version_response(version)


# ============================================================================
# Letterhead logo upload
# ============================================================================

@router.post("/{letterhead_id}/logo", response_model=ReportLetterheadLogoUploadResponse)
def upload_letterhead_logo(
    letterhead_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Upload a logo image for a letterhead version's `header`.

    Returns a `storage_object_id` to use as `logo_storage_id` when
    publishing a version. Does not touch any ReportLetterheadVersion —
    publishing is a separate, explicit step (create_letterhead_version).
    Validation/upload shared with the template-logo and tenant-logo
    endpoints via `ManagedTenantImageService`.
    """
    _require(user.id, "reports:manage_templates", session)
    letterhead = _get_owned_letterhead(letterhead_id, ctx, session)

    file_bytes = file.file.read()
    try:
        result = ManagedTenantImageService().upload(
            file_bytes=file_bytes,
            declared_content_type=file.content_type or "",
            tenant_id=letterhead.tenant_id,
            key_prefix=f"report-letterheads/{letterhead_id}/logos",
            created_by=user.id,
            session=session,
        )
    except InvalidImageError as exc:
        raise HTTPException(400, exc.message) from None
    except ImageRegistrationError:
        raise HTTPException(500, "Failed to register uploaded logo") from None

    # Céluma 1.3 Phase 4, Block C: letterhead/template assets are billable
    # once created and stay billable for as long as any version references
    # them — never decremented on supersession (§13). Increment on create,
    # same as official PDFs. The StorageObject insert already committed
    # inside ManagedTenantImageService.upload(); this is a second, small
    # commit for the counter (a pre-existing gap in this shared service's
    # transactional boundary — see managed-logo-upload-contract.md — not
    # something Block C widens or fixes).
    UsageService.record_storage_delta(
        session,
        letterhead.tenant_id,
        result.size_bytes,
        source="letterhead_asset",
        resource_type="letterhead_logo",
    )
    session.commit()

    logger.info(
        f"Letterhead logo uploaded for letterhead {letterhead_id}",
        extra={
            "event": "report_letterhead_logo.uploaded",
            "letterhead_id": letterhead_id,
            "storage_object_id": str(result.storage_object.id),
            "user_id": str(user.id),
        },
    )

    return ReportLetterheadLogoUploadResponse(
        storage_object_id=str(result.storage_object.id),
        url=result.url,
        content_type=result.content_type,
        size_bytes=result.size_bytes,
    )


# ============================================================================
# .celuma portable file format — post-Phase-2 remediation, R12/R13
# ============================================================================

MAX_CELUMA_FILE_BYTES = 8 * 1024 * 1024  # dominated by the base64 logo asset


@router.get(
    "/{letterhead_id}/versions/{version_id}/export",
    response_model=CelumaLetterheadEnvelope,
)
def export_letterhead_version_endpoint(
    letterhead_id: str,
    version_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Export a published letterhead version as a portable `.cell` file
    (requires reports:manage_templates). Never includes tenant_id,
    StorageObject id, bucket/key, or any URL — see cell-file-format-v2.md."""
    _require(user.id, "reports:manage_templates", session)
    letterhead = _get_owned_letterhead(letterhead_id, ctx, session)
    version = _get_owned_letterhead_version(letterhead_id, version_id, ctx, session)

    try:
        envelope = export_letterhead_version(letterhead, version, session)
    except CelumaPortabilityError as exc:
        # Third remediation: a referenced but unrecoverable logo no longer
        # produces a half `.cell` without a logo — fails with a message that
        # says what to repair. See cell-roundtrip-contract.md.
        raise HTTPException(409, exc.message) from None

    logger.info(
        f"Letterhead version {version_id} exported as .cell",
        extra={
            "event": "report_letterhead_version.exported",
            "letterhead_id": letterhead_id,
            "version_id": version_id,
            "user_id": str(user.id),
        },
    )

    # Fourth remediation (side finding): letterhead names naturally carry
    # accents ("Membrete Anatomía Patológica"), and an HTTP header only
    # accepts latin-1. Putting the raw name in `filename="..."` broke the
    # ENTIRE response with an encoding error — the export never even
    # reached the browser. Follow RFC 6266: `filename` with safe ASCII for
    # old clients, and `filename*` with the real name percent-encoded UTF-8.
    raw_name = f"{letterhead.name.replace(' ', '-')}-v{version.version_number}.cell"
    ascii_name = unicodedata.normalize("NFKD", raw_name).encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r'[^A-Za-z0-9._-]', "", ascii_name) or f"membrete-v{version.version_number}.cell"
    quoted_name = quote(raw_name, safe="")
    return JSONResponse(
        content=json.loads(envelope.model_dump_json()),
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted_name}'
            )
        },
    )


@router.get("/legacy/export", response_model=CelumaLetterheadEnvelope)
def export_legacy_letterhead_endpoint(
    session: Session = Depends(get_session),
    user: AppUser = Depends(current_user),
):
    """Export the frozen Legacy renderer's letterhead as a portable
    `.cell` file (requires reports:manage_templates). Deterministic —
    never reads or modifies Legacy. See legacy-parity-contract.md."""
    _require(user.id, "reports:manage_templates", session)
    envelope = build_legacy_letterhead_export()

    logger.info(
        "Legacy letterhead exported as .cell",
        extra={"event": "legacy_letterhead.exported", "user_id": str(user.id)},
    )

    return JSONResponse(
        content=json.loads(envelope.model_dump_json()),
        headers={"Content-Disposition": 'attachment; filename="legacy-ambassador-letterhead.cell"'},
    )


@router.post("/import", response_model=ReportLetterheadVersionDetailResponse)
def import_letterhead_endpoint(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Import a `.cell`/`.clm`/`.celuma` file as a new letterhead (requires
    reports:manage_templates). Extension is cosmetic — validation is purely
    on the JSON body's `format`/`format_version` fields, never the filename.
    Never reuses ids from the source tenant.

    Third remediation: the imported letterhead is born with its ACTIVE
    version (previously it was born PUBLISHED and stayed invisible to the
    editor — see `import_letterhead_version`), but NEVER as the tenant
    default: that remains an explicit administrator decision."""
    _require(user.id, "reports:manage_templates", session)

    file_bytes = file.file.read()
    if not file_bytes:
        raise HTTPException(400, "Uploaded file is empty")
    if len(file_bytes) > MAX_CELUMA_FILE_BYTES:
        raise HTTPException(400, ".cell file exceeds the maximum allowed size")

    try:
        raw = json.loads(file_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "File is not valid JSON — it may be corrupted") from None

    try:
        envelope = CelumaLetterheadEnvelope.model_validate(raw)
    except PydanticValidationError as exc:
        raise HTTPException(400, f".celuma file failed validation: {exc}") from None

    try:
        version = import_letterhead_version(
            envelope, tenant_id=ctx.tenant_id, created_by=user.id, session=session
        )
    except CelumaPortabilityError as exc:
        raise HTTPException(400, exc.message) from None

    logger.info(
        f"Letterhead imported from .celuma as letterhead {version.report_letterhead_id}",
        extra={
            "event": "report_letterhead.imported",
            "letterhead_id": str(version.report_letterhead_id),
            "version_id": str(version.id),
            "user_id": str(user.id),
        },
    )

    return _version_detail_response(version, session)
