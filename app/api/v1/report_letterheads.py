"""Administration endpoints for the shared, tenant-owned letterhead
("membrete") domain — post-Fase-2 remediation, R6.

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
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse
from sqlmodel import Session, select
from pydantic import ValidationError as PydanticValidationError

from app.api.v1.auth import get_auth_ctx, AuthContext, current_user
from app.core.db import get_session
from app.core.rbac import has_permission
from app.models.report import ReportTemplate
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


def _letterhead_response(l: ReportLetterhead) -> ReportLetterheadResponse:
    return ReportLetterheadResponse(
        id=str(l.id),
        tenant_id=str(l.tenant_id),
        name=l.name,
        description=l.description,
        is_default=l.is_default,
        is_active=l.is_active,
        created_at=l.created_at,
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
    letterheads = session.exec(query).all()
    return ReportLetterheadsListResponse(
        letterheads=[_letterhead_response(l) for l in letterheads]
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
        **_letterhead_response(letterhead).model_dump(),
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
    if data.description is not None:
        letterhead.description = data.description
    if data.is_active is not None:
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
    return _letterhead_response(letterhead)


@router.delete("/{letterhead_id}")
def delete_letterhead(
    letterhead_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
    hard_delete: bool = False,
):
    """Delete a letterhead (requires reports:manage_templates).

    Hard delete is rejected if the letterhead has any published version,
    is the tenant default, or is referenced by any
    `ReportTemplate.preferred_letterhead_version_id` — mirrors the same
    guard already proven for `DELETE /templates/{id}`.
    """
    _require(user.id, "reports:manage_templates", session)
    letterhead = _get_owned_letterhead(letterhead_id, ctx, session)

    if hard_delete:
        has_versions = session.exec(
            select(ReportLetterheadVersion.id).where(
                ReportLetterheadVersion.report_letterhead_id == letterhead.id
            )
        ).first()
        if has_versions:
            raise HTTPException(
                409,
                "Cannot permanently delete a letterhead with published versions. "
                "Archive its versions or deactivate the letterhead instead.",
            )
        if letterhead.is_default:
            raise HTTPException(
                409,
                "Cannot permanently delete the tenant's default letterhead. "
                "Set another letterhead as default first.",
            )
        referenced = session.exec(
            select(ReportTemplate.id)
            .join(
                ReportLetterheadVersion,
                ReportLetterheadVersion.id == ReportTemplate.preferred_letterhead_version_id,
            )
            .where(ReportLetterheadVersion.report_letterhead_id == letterhead.id)
        ).first()
        if referenced:
            raise HTTPException(
                409,
                "Cannot permanently delete a letterhead referenced as a "
                "template's preferred letterhead.",
            )

        session.delete(letterhead)
        session.commit()

        logger.info(
            f"Letterhead '{letterhead.name}' permanently deleted",
            extra={
                "event": "report_letterhead.hard_deleted",
                "letterhead_id": letterhead_id,
                "user_id": str(user.id),
            },
        )
        return {"message": "Letterhead permanently deleted", "id": letterhead_id}
    else:
        letterhead.is_active = False
        session.add(letterhead)
        session.commit()

        logger.info(
            f"Letterhead '{letterhead.name}' soft deleted (deactivated)",
            extra={
                "event": "report_letterhead.soft_deleted",
                "letterhead_id": str(letterhead.id),
                "user_id": str(user.id),
            },
        )
        return {"message": "Letterhead deactivated", "id": str(letterhead.id)}


@router.post("/{letterhead_id}/duplicate", response_model=ReportLetterheadResponse)
def duplicate_letterhead(
    letterhead_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new letterhead shell, cloning the source's ACTIVE (or, if
    none, latest PUBLISHED) version as a fresh PUBLISHED version under the
    new shell (requires reports:manage_templates)."""
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
            status=ReportLetterheadVersionStatus.PUBLISHED,
            created_by=user.id,
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
    return _letterhead_response(new_letterhead)


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
    """
    _require(user.id, "reports:manage_templates", session)
    letterhead = _get_owned_letterhead(letterhead_id, ctx, session)

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
    return _letterhead_response(letterhead)


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
    """Segunda remediación post-Fase 2 (UX): la configuración ACTIVE actual
    de un membrete, para precargar el editor visual en modo "Editar". 404 si
    el membrete no tiene ninguna versión ACTIVE todavía (recién creado).

    Registrado ANTES de `GET /{letterhead_id}/versions/{version_id}` a
    propósito: FastAPI/Starlette resuelve rutas en orden de registro, y
    "active" también matchea el patrón `{version_id}` — si este endpoint se
    registrara después, quedaría inalcanzable (la ruta paramétrica lo
    interceptaría primero, intentando usar el string "active" como UUID).
    """
    _require(user.id, "reports:manage_templates", session)
    _get_owned_letterhead(letterhead_id, ctx, session)
    active = session.exec(
        select(ReportLetterheadVersion).where(
            ReportLetterheadVersion.report_letterhead_id == letterhead_id,
            ReportLetterheadVersion.status == ReportLetterheadVersionStatus.ACTIVE,
        )
    ).first()
    if not active:
        raise HTTPException(404, "This letterhead has no active version yet")
    return ReportLetterheadVersionDetailResponse(
        **_version_response(active).model_dump(),
        configuration=active.configuration,
    )


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
    """Segunda remediación post-Fase 2 (UX): "Guardar cambios" del editor
    visual de membretes — el reemplazo de "Publicar versión" (que se
    quedaba en PUBLISHED sin activar). Crea una `ReportLetterheadVersion`
    nueva y la activa atómicamente, archivando la anterior ACTIVE. No-op
    (devuelve la versión ACTIVE existente sin crear nada) si la
    configuración enviada es idéntica a la ya activa — evita ruido de
    historial en un "Guardar" sin cambios reales.

    `POST .../versions` + `POST .../{id}/activate` (abajo) siguen
    existiendo tal cual para el flujo secundario de historial/rollback
    ("Nueva versión desde esta", "Restaurar"). Registrado antes de
    `GET .../versions/{version_id}` por la misma razón de orden de rutas
    explicada arriba (el método PUT no colisiona con esa ruta GET, pero se
    mantiene junto a su endpoint hermano por claridad).
    """
    _require(user.id, "reports:manage_templates", session)
    letterhead = _get_owned_letterhead(letterhead_id, ctx, session)

    logo_storage_id = payload.configuration.header.logo_storage_id
    if logo_storage_id is not None:
        logo_object = session.get(StorageObject, logo_storage_id)
        if not logo_object:
            raise HTTPException(400, "logo_storage_id does not reference an existing object")
        if str(logo_object.tenant_id) != str(letterhead.tenant_id):
            raise HTTPException(
                400, "logo_storage_id does not reference an object owned by this tenant"
            )
    footer_logo_storage_id = payload.configuration.footer.logo_storage_id
    if footer_logo_storage_id is not None:
        footer_logo_object = session.get(StorageObject, footer_logo_storage_id)
        if not footer_logo_object:
            raise HTTPException(
                400, "footer.logo_storage_id does not reference an existing object"
            )
        if str(footer_logo_object.tenant_id) != str(letterhead.tenant_id):
            raise HTTPException(
                400, "footer.logo_storage_id does not reference an object owned by this tenant"
            )

    new_configuration = payload.configuration.model_dump(mode="json")

    active = session.exec(
        select(ReportLetterheadVersion).where(
            ReportLetterheadVersion.report_letterhead_id == letterhead.id,
            ReportLetterheadVersion.status == ReportLetterheadVersionStatus.ACTIVE,
        )
    ).first()
    if active is not None and active.configuration == new_configuration:
        return ReportLetterheadVersionDetailResponse(
            **_version_response(active).model_dump(),
            configuration=active.configuration,
        )

    if active is not None:
        # Demote before promoting — mismo orden que activate_letterhead_version,
        # necesario por el índice único parcial "una ACTIVE por membrete".
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

    return ReportLetterheadVersionDetailResponse(
        **_version_response(new_version).model_dump(),
        configuration=new_version.configuration,
    )


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
    return ReportLetterheadVersionDetailResponse(
        **_version_response(version).model_dump(),
        configuration=version.configuration,
    )


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

    logo_storage_id = payload.configuration.header.logo_storage_id
    if logo_storage_id is not None:
        logo_object = session.get(StorageObject, logo_storage_id)
        if not logo_object:
            raise HTTPException(400, "logo_storage_id does not reference an existing object")
        if str(logo_object.tenant_id) != str(letterhead.tenant_id):
            raise HTTPException(
                400, "logo_storage_id does not reference an object owned by this tenant"
            )

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

    return ReportLetterheadVersionDetailResponse(
        **_version_response(version).model_dump(),
        configuration=version.configuration,
    )


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
# .celuma portable file format — post-Fase-2 remediation, R12/R13
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

    envelope = export_letterhead_version(letterhead, version, session)

    logger.info(
        f"Letterhead version {version_id} exported as .cell",
        extra={
            "event": "report_letterhead_version.exported",
            "letterhead_id": letterhead_id,
            "version_id": version_id,
            "user_id": str(user.id),
        },
    )

    filename = f"{letterhead.name.replace(' ', '-')}-v{version.version_number}.cell"
    return JSONResponse(
        content=json.loads(envelope.model_dump_json()),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
    """Import a `.cell`/`.clm`/`.celuma` file as a new, unpublished-to-default
    letterhead (requires reports:manage_templates). Extension is cosmetic —
    validation is purely on the JSON body's `format`/`format_version`
    fields, never the filename. Never reuses ids from the source tenant;
    the caller must explicitly publish/activate/default it afterwards —
    import alone never makes it live for report creation."""
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

    return ReportLetterheadVersionDetailResponse(
        **_version_response(version).model_dump(),
        configuration=version.configuration,
    )
