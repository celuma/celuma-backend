"""Second post-Phase-2 remediation (UX): template auto-versioning.

Before this remediation, saving `template_json` (clinical structure) and
publishing a `ReportTemplateVersion` (which also had to be activated by
hand) were two completely separate flows on two different screens — see
internal-versioning-contract.md. This turns the normal clinical save into
the only action the user needs: internally it still creates an immutable
revision and activates it, but never asks for that explicitly.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from pydantic import ValidationError as PydanticValidationError
from sqlmodel import Session, select

from app.models.report import ReportTemplate
from app.models.report_template_version import ReportTemplateVersion, ReportTemplateVersionStatus
from app.schemas.report_template_version import ReportRenderingSnapshotV2
from app.services.letterhead_resolution import (
    LetterheadResolutionError,
    resolve_effective_letterhead_version,
)

logger = logging.getLogger(__name__)


def _hash_template_block(template_block: Optional[Dict[str, Any]]) -> str:
    canonical = json.dumps(template_block or {}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def snapshot_and_activate_template_version(
    session: Session, template: ReportTemplate, actor_id: Optional[UUID]
) -> Optional[ReportTemplateVersion]:
    """Create and activate a `ReportTemplateVersion` reflecting the current
    `template_json`, if it changed relative to the current ACTIVE version.

    No-op (returns the existing ACTIVE version untouched) if `template_json`
    is identical to what that version already reflects — avoids polluting
    history on name/description saves or other saves with no real clinical
    change.

    Returns `None` without creating anything if there is no resolvable
    letterhead (`resolve_effective_letterhead_version`) — the clinical save
    itself still works normally; V2 report creation remains blocked until a
    letterhead is configured, exactly as before this remediation (see
    remaining-release-risks.md).
    """
    active_version = session.exec(
        select(ReportTemplateVersion).where(
            ReportTemplateVersion.report_template_id == template.id,
            ReportTemplateVersion.status == ReportTemplateVersionStatus.ACTIVE,
        )
    ).first()

    new_hash = _hash_template_block(template.template_json)
    if active_version is not None:
        existing_block = (active_version.configuration or {}).get("template")
        if _hash_template_block(existing_block) == new_hash:
            return active_version

    # Third remediation: resolution is deterministic and reports its source.
    # A misconfigured tenant (two ACTIVE versions, default with none active)
    # raises `LetterheadResolutionError` instead of returning a random
    # version; here that MUST NOT break a clinical save, so it degrades to
    # "do not auto-version" and is logged — V2 creation will block with the
    # same message, which is where the user should see it.
    try:
        resolved = resolve_effective_letterhead_version(
            session, str(template.tenant_id), template=template
        )
    except LetterheadResolutionError as exc:
        logger.warning(
            "Skipping template auto-version: letterhead resolution failed",
            extra={
                "event": "report_template_version.autoversion_unresolvable_letterhead",
                "template_id": str(template.id),
                "reason": exc.message,
            },
        )
        return None
    if resolved is None:
        logger.info(
            "Skipping template auto-version: no resolvable letterhead yet",
            extra={
                "event": "report_template_version.autoversion_skipped",
                "template_id": str(template.id),
            },
        )
        return None

    try:
        snapshot = ReportRenderingSnapshotV2(
            schema_version=2,
            template=template.template_json,
            presentation=resolved.presentation,
        )
    except PydanticValidationError:
        logger.exception(
            "Skipping template auto-version: resolved letterhead configuration "
            "failed re-validation",
            extra={
                "event": "report_template_version.autoversion_invalid_presentation",
                "template_id": str(template.id),
            },
        )
        return None

    if active_version is not None:
        # Demote before promoting (same ordering as activate_template_version):
        # both rows are covered by the same partial-unique "one ACTIVE per
        # template" index, checked per-statement, not per-transaction.
        active_version.status = ReportTemplateVersionStatus.PUBLISHED
        session.add(active_version)
        session.flush()

    last_version = session.exec(
        select(ReportTemplateVersion)
        .where(ReportTemplateVersion.report_template_id == template.id)
        .order_by(ReportTemplateVersion.version_number.desc())
    ).first()
    next_version_number = (last_version.version_number + 1) if last_version else 1

    new_version = ReportTemplateVersion(
        tenant_id=template.tenant_id,
        report_template_id=template.id,
        version_number=next_version_number,
        schema_version=2,
        configuration=snapshot.model_dump(mode="json"),
        status=ReportTemplateVersionStatus.ACTIVE,
        created_by=actor_id,
        activated_at=datetime.utcnow(),
    )
    session.add(new_version)
    session.commit()
    session.refresh(new_version)

    logger.info(
        "Template auto-versioned and activated",
        extra={
            "event": "report_template_version.autoversioned",
            "template_id": str(template.id),
            "version_id": str(new_version.id),
            "version_number": new_version.version_number,
        },
    )
    return new_version
