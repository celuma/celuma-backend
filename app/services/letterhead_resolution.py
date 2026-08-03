"""Deterministic effective-letterhead resolution — third post-Phase-2
remediation.

Before this remediation, resolution lived in
`resolve_fallback_letterhead_version`, which picked a version with
`.first()` and no `ORDER BY` or invariants: with slightly inconsistent
data (two ACTIVE versions from a manual restore, a default letterhead
with no active version, a preference pointing at another tenant) it
returned an arbitrary version or silent `None`. That produced the
symptom "I mark a letterhead as default and sometimes another one
comes out".

`resolve_effective_letterhead_version` is now the ONLY source of truth;
it is used by report creation, `GET /study-types/{id}/report-defaults`,
template auto-versioning, and any future flow. It always returns where
the letterhead came from (`resolution_source`), which makes the result
diagnosable and assertable in tests.

Precedence order (see deterministic-letterhead-resolution-contract.md):

    1. EXPLICIT           — `letterhead_version_id` or `letterhead_id`
                            from the request.
    2. TEMPLATE_PREFERRED — `template.preferred_letterhead_id`, or the
                            legacy `preferred_letterhead_version_id`
                            field for old rows.
    3. TENANT_DEFAULT     — the single `ReportLetterhead.is_default=true`
                            for the tenant.
    4. None               — nothing resolvable; the caller BLOCKS V2
                            creation (never falls back to Legacy).

Invariants checked before returning anything: same tenant, active
letterhead, exactly one ACTIVE version, valid configuration. A
violation raises `LetterheadConfigurationError` — candidates are never
picked arbitrarily.
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
    """Where the resolved letterhead came from. Exposed in the API (and in
    the UI in admin mode, translated) so "why did this letterhead come
    out?" always has an answer."""

    EXPLICIT = "EXPLICIT"
    TEMPLATE_PREFERRED = "TEMPLATE_PREFERRED"
    TENANT_DEFAULT = "TENANT_DEFAULT"


class LetterheadResolutionError(Exception):
    """Base for resolution failures. `message` is safe for the client."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class LetterheadNotFoundError(LetterheadResolutionError):
    """An EXPLICIT request reference does not exist, belongs to another
    tenant, or is archived. The caller maps it to 404/409 — never silently
    degrade to the fallback chain: if the user asked for a specific
    letterhead, using another would be worse than failing."""


class LetterheadArchivedError(LetterheadResolutionError):
    """The explicit reference points at an archived version. Distinct from
    "does not exist": the resource is there but was deliberately retired,
    so the caller responds 409, not 404."""


class LetterheadConfigurationError(LetterheadResolutionError):
    """Tenant data is inconsistent and there is no correct answer: more
    than one ACTIVE version, a default letterhead with no ACTIVE version,
    or a `configuration` that no longer validates against the contract.
    Fail explicitly instead of picking at random."""


@dataclass(frozen=True)
class ResolvedLetterhead:
    """Everything a caller needs, resolved in one shot."""

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
# Internal helpers
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
    """The ONLY ACTIVE version of the letterhead. Never `.first()`: zero or
    more than one is an explicit configuration error."""
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
    """The logical letterhead if — and only if — it exists, belongs to this
    tenant, and is active. `None` for anything else: a dangling preference
    must never fail creation, only stop applying (fall through to the next
    step in the chain)."""
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
# Single entry point
# ---------------------------------------------------------------------------

def resolve_effective_letterhead_version(
    session: Session,
    tenant_id: str,
    *,
    template: Optional[ReportTemplate] = None,
    letterhead_id: Optional[str] = None,
    letterhead_version_id: Optional[str] = None,
) -> Optional[ResolvedLetterhead]:
    """Resolve the effective letterhead, or `None` if the tenant has none
    usable (the caller must block V2 creation, never fall back to Legacy).

    Raises `LetterheadNotFoundError` if an explicit request reference is
    not usable, and `LetterheadConfigurationError` if tenant data is
    inconsistent.
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

        # Legacy field: read-only, for rows from before the second
        # remediation. The app never writes it.
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
            # Contradictory but user-recoverable state: treat as "no default"
            # (V2 blocked with an actionable reason), not as a 500.
            logger.warning(
                "Tenant default letterhead is deactivated — treating as no default",
                extra={
                    "event": "letterhead_resolution.default_inactive",
                    "letterhead_id": str(default_letterhead.id),
                },
            )
            return None
        return _resolve(session, default_letterhead, LetterheadResolutionSource.TENANT_DEFAULT)

    # --- 4. Nothing resolvable --------------------------------------------
    return None
