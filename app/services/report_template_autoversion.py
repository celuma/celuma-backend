"""Segunda remediación post-Fase 2 (UX): auto-versionamiento de plantillas.

Antes de esta remediación, guardar `template_json` (estructura clínica) y
publicar una `ReportTemplateVersion` (que además debía activarse a mano)
eran dos flujos completamente separados en dos pantallas distintas — ver
internal-versioning-contract.md. Esto convierte el guardado clínico normal
en la única acción que el usuario necesita: internamente sigue creando una
revisión inmutable y activándola, pero nunca lo pide explícitamente.
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
from app.services.letterhead_resolution import resolve_fallback_letterhead_version

logger = logging.getLogger(__name__)


def _hash_template_block(template_block: Optional[Dict[str, Any]]) -> str:
    canonical = json.dumps(template_block or {}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def snapshot_and_activate_template_version(
    session: Session, template: ReportTemplate, actor_id: Optional[UUID]
) -> Optional[ReportTemplateVersion]:
    """Crea y activa una `ReportTemplateVersion` reflejando el
    `template_json` actual, si cambió respecto a la versión ACTIVE actual.

    No-op (devuelve la versión ACTIVE existente sin tocar nada) si el
    `template_json` es idéntico al ya reflejado en esa versión — evita
    ensuciar el historial en guardados de nombre/descripción u otros
    guardados sin cambio clínico real.

    Devuelve `None` sin crear nada si no hay ningún membrete resoluble
    (`resolve_fallback_letterhead_version`) — el guardado clínico en sí
    sigue funcionando con normalidad; la creación de reportes V2 permanece
    bloqueada hasta que se configure un membrete, exactamente como antes de
    esta remediación (ver remaining-release-risks.md).
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

    resolved_letterhead_version = resolve_fallback_letterhead_version(
        session, str(template.tenant_id), template
    )
    if resolved_letterhead_version is None:
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
            presentation=resolved_letterhead_version.configuration,
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
