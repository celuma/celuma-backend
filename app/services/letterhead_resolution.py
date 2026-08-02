"""Resolución determinista del membrete efectivo — tercera remediación
post-Fase 2.

Antes de esta remediación la resolución vivía en
`resolve_fallback_letterhead_version`, que elegía la versión con
`.first()` sin `ORDER BY` ni invariantes: con datos ligeramente
inconsistentes (dos versiones ACTIVE por una restauración manual, un
membrete default sin versión activa, una preferencia apuntando a otro
tenant) devolvía una versión arbitraria o `None` en silencio. De ahí el
síntoma "marco un membrete como predeterminado y a veces sale otro".

`resolve_effective_letterhead_version` es ahora el ÚNICO punto de verdad;
lo usan la creación de reportes, `GET /study-types/{id}/report-defaults`,
el auto-versionado de plantillas y cualquier flujo futuro. Devuelve
siempre de dónde salió el membrete (`resolution_source`), lo que hace
diagnosticable el resultado y comprobable en pruebas.

Orden de precedencia (ver deterministic-letterhead-resolution-contract.md):

    1. EXPLICIT           — `letterhead_version_id` o `letterhead_id` que
                            venga en la request.
    2. TEMPLATE_PREFERRED — `template.preferred_letterhead_id`, o el campo
                            legado `preferred_letterhead_version_id` para
                            filas antiguas.
    3. TENANT_DEFAULT     — el único `ReportLetterhead.is_default=true`
                            del tenant.
    4. None               — nada resoluble; el caller BLOQUEA la creación
                            V2 (nunca cae a Legacy).

Invariantes verificadas antes de devolver nada: mismo tenant, membrete
activo, exactamente una versión ACTIVE, configuración válida. Una
violación levanta `LetterheadConfigurationError` — nunca se elige
arbitrariamente entre candidatos.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from pydantic import ValidationError as PydanticValidationError
from sqlmodel import Session, select

from app.models.report import ReportTemplate
from app.models.report_letterhead import ReportLetterhead
from app.models.report_letterhead_version import (
    ReportLetterheadVersion,
    ReportLetterheadVersionStatus,
)
from app.schemas.report_template_version import ReportPresentationSnapshotV2

logger = logging.getLogger(__name__)


class LetterheadResolutionSource(str, Enum):
    """De dónde salió el membrete resuelto. Se expone en la API (y en la UI
    en modo administrativo, traducido) para que "¿por qué salió este
    membrete?" tenga siempre una respuesta."""

    EXPLICIT = "EXPLICIT"
    TEMPLATE_PREFERRED = "TEMPLATE_PREFERRED"
    TENANT_DEFAULT = "TENANT_DEFAULT"


class LetterheadResolutionError(Exception):
    """Base de los fallos de resolución. `message` es apto para el cliente."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class LetterheadNotFoundError(LetterheadResolutionError):
    """Una referencia EXPLÍCITA de la request no existe, es de otro tenant o
    está archivada. El caller la traduce a 404/409 — jamás se degrada
    silenciosamente a la cadena de fallback: si el usuario pidió un membrete
    concreto, usar otro sería peor que fallar."""


class LetterheadArchivedError(LetterheadResolutionError):
    """La referencia explícita apunta a una versión archivada. Distinta de
    "no existe": el recurso está ahí pero fue retirado a propósito, así que
    el caller responde 409, no 404."""


class LetterheadConfigurationError(LetterheadResolutionError):
    """Los datos del tenant son inconsistentes y no existe una respuesta
    correcta: más de una versión ACTIVE, un membrete default sin ninguna
    ACTIVE, o una `configuration` que ya no valida contra el contrato. Se
    falla explícitamente en vez de elegir al azar."""


@dataclass(frozen=True)
class ResolvedLetterhead:
    """Todo lo que un caller necesita, resuelto de una vez."""

    letterhead: ReportLetterhead
    version: ReportLetterheadVersion
    source: LetterheadResolutionSource
    presentation: ReportPresentationSnapshotV2

    @property
    def letterhead_id(self) -> str:
        return str(self.letterhead.id)

    @property
    def letterhead_version_id(self) -> str:
        return str(self.version.id)


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _validated_presentation(
    version: ReportLetterheadVersion,
) -> ReportPresentationSnapshotV2:
    try:
        return ReportPresentationSnapshotV2.model_validate(version.configuration)
    except PydanticValidationError as exc:
        raise LetterheadConfigurationError(
            "La configuración del membrete almacenada no es válida "
            f"(versión {version.id}). Corrígela publicando una versión nueva."
        ) from exc


def sole_active_version(
    session: Session, letterhead: ReportLetterhead
) -> ReportLetterheadVersion:
    """La ÚNICA versión ACTIVE del membrete. Nunca `.first()`: si hay cero o
    más de una, es un error de configuración explícito."""
    actives = session.exec(
        select(ReportLetterheadVersion)
        .where(
            ReportLetterheadVersion.report_letterhead_id == letterhead.id,
            ReportLetterheadVersion.tenant_id == letterhead.tenant_id,
            ReportLetterheadVersion.status == ReportLetterheadVersionStatus.ACTIVE,
        )
        .order_by(ReportLetterheadVersion.version_number.desc())
    ).all()

    if len(actives) == 1:
        return actives[0]
    if not actives:
        raise LetterheadConfigurationError(
            f"El membrete «{letterhead.name}» no tiene ninguna versión activa. "
            "Ábrelo y guarda su configuración para activarlo."
        )
    raise LetterheadConfigurationError(
        f"El membrete «{letterhead.name}» tiene {len(actives)} versiones activas "
        "a la vez. Es un estado inconsistente: archiva las sobrantes antes de "
        "volver a usarlo."
    )


def _usable_letterhead(
    session: Session, letterhead_id, tenant_id: str
) -> Optional[ReportLetterhead]:
    """El membrete lógico si — y solo si — existe, es de este tenant y está
    activo. `None` para cualquier otra cosa: una preferencia colgada nunca
    debe hacer fallar la creación, solo dejar de aplicar (se cae al
    siguiente escalón de la cadena)."""
    if letterhead_id is None:
        return None
    try:
        letterhead = session.get(ReportLetterhead, letterhead_id)
    except (ValueError, TypeError):
        return None
    if letterhead is None:
        return None
    if str(letterhead.tenant_id) != str(tenant_id):
        return None
    if not letterhead.is_active:
        return None
    return letterhead


def _resolve(
    session: Session,
    letterhead: ReportLetterhead,
    source: LetterheadResolutionSource,
) -> ResolvedLetterhead:
    version = sole_active_version(session, letterhead)
    return ResolvedLetterhead(
        letterhead=letterhead,
        version=version,
        source=source,
        presentation=_validated_presentation(version),
    )


# ---------------------------------------------------------------------------
# Punto de entrada único
# ---------------------------------------------------------------------------

def resolve_effective_letterhead_version(
    session: Session,
    tenant_id: str,
    *,
    template: Optional[ReportTemplate] = None,
    letterhead_id: Optional[str] = None,
    letterhead_version_id: Optional[str] = None,
) -> Optional[ResolvedLetterhead]:
    """Resuelve el membrete efectivo, o `None` si el tenant no tiene ninguno
    utilizable (el caller debe bloquear la creación V2, nunca caer a Legacy).

    Levanta `LetterheadNotFoundError` si una referencia explícita de la
    request no es utilizable, y `LetterheadConfigurationError` si los datos
    del tenant son inconsistentes.
    """
    # --- 1. EXPLICIT ------------------------------------------------------
    if letterhead_version_id is not None:
        version = None
        try:
            version = session.get(ReportLetterheadVersion, letterhead_version_id)
        except (ValueError, TypeError):
            version = None
        if version is None or str(version.tenant_id) != str(tenant_id):
            raise LetterheadNotFoundError("Letterhead version not found")
        if version.status == ReportLetterheadVersionStatus.ARCHIVED:
            raise LetterheadArchivedError(
                "Cannot create a report from an archived letterhead version"
            )
        letterhead = session.get(ReportLetterhead, version.report_letterhead_id)
        if letterhead is None or str(letterhead.tenant_id) != str(tenant_id):
            raise LetterheadNotFoundError("Letterhead not found")
        return ResolvedLetterhead(
            letterhead=letterhead,
            version=version,
            source=LetterheadResolutionSource.EXPLICIT,
            presentation=_validated_presentation(version),
        )

    if letterhead_id is not None:
        letterhead = _usable_letterhead(session, letterhead_id, tenant_id)
        if letterhead is None:
            raise LetterheadNotFoundError("Letterhead not found")
        return _resolve(session, letterhead, LetterheadResolutionSource.EXPLICIT)

    # --- 2. TEMPLATE_PREFERRED -------------------------------------------
    if template is not None:
        preferred = _usable_letterhead(
            session, template.preferred_letterhead_id, tenant_id
        )
        if preferred is not None:
            return _resolve(session, preferred, LetterheadResolutionSource.TEMPLATE_PREFERRED)
        if template.preferred_letterhead_id is not None:
            logger.warning(
                "Template preferred letterhead is not usable — falling through to the "
                "tenant default",
                extra={
                    "event": "letterhead_resolution.preferred_unusable",
                    "template_id": str(template.id),
                    "preferred_letterhead_id": str(template.preferred_letterhead_id),
                },
            )

        # Campo legado: solo lectura, para filas anteriores a la segunda
        # remediación. Nunca lo escribe la app.
        if template.preferred_letterhead_version_id is not None:
            legacy_version = session.get(
                ReportLetterheadVersion, template.preferred_letterhead_version_id
            )
            if (
                legacy_version is not None
                and str(legacy_version.tenant_id) == str(tenant_id)
                and legacy_version.status != ReportLetterheadVersionStatus.ARCHIVED
            ):
                owning = _usable_letterhead(
                    session, legacy_version.report_letterhead_id, tenant_id
                )
                if owning is not None:
                    return ResolvedLetterhead(
                        letterhead=owning,
                        version=legacy_version,
                        source=LetterheadResolutionSource.TEMPLATE_PREFERRED,
                        presentation=_validated_presentation(legacy_version),
                    )

    # --- 3. TENANT_DEFAULT ------------------------------------------------
    defaults = session.exec(
        select(ReportLetterhead)
        .where(
            ReportLetterhead.tenant_id == tenant_id,
            ReportLetterhead.is_default == True,  # noqa: E712
        )
        .order_by(ReportLetterhead.created_at.asc())
    ).all()
    if len(defaults) > 1:
        raise LetterheadConfigurationError(
            f"El laboratorio tiene {len(defaults)} membretes marcados como "
            "predeterminados a la vez. Deja solo uno antes de crear reportes."
        )
    if defaults:
        default_letterhead = defaults[0]
        if not default_letterhead.is_active:
            # Estado contradictorio pero recuperable por el usuario: se trata
            # como "sin default" (V2 bloqueado con motivo accionable), no
            # como un 500.
            logger.warning(
                "Tenant default letterhead is deactivated — treating as no default",
                extra={
                    "event": "letterhead_resolution.default_inactive",
                    "letterhead_id": str(default_letterhead.id),
                },
            )
            return None
        return _resolve(session, default_letterhead, LetterheadResolutionSource.TENANT_DEFAULT)

    # --- 4. Nada resoluble ------------------------------------------------
    return None
