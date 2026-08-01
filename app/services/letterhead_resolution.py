"""Segunda remediación post-Fase 2 (UX): resolución compartida del membrete
lógico preferido de una plantilla.

Antes de esta remediación esta lógica estaba duplicada (con matices) entre
`create_report` (app/api/v1/reports.py) y el resolver de
`GET /study-types/{id}/report-defaults` (app/api/v1/study_types.py). Un solo
punto de verdad evita que ambos flujos diverjan.

Orden de resolución (ver report-letterhead-selection-ux.md):
    1. `template.preferred_letterhead_id` (el membrete lógico) -> su versión
       ACTIVE.
    2. `template.preferred_letterhead_version_id` (campo legado, de solo
       lectura para filas antiguas — nunca escrito por la app desde esta
       remediación).
    3. El membrete `is_default=true` del tenant -> su versión ACTIVE.

No maneja un `letterhead_version_id` explícito de la request: esa
precedencia (la más alta) tiene semántica de error distinta por caller
(404/409) y se queda en cada endpoint. Esta función solo resuelve la cadena
de fallback y nunca lanza — devuelve `None` si nada resuelve, dejando que el
caller decida si bloquear.
"""
from __future__ import annotations

from typing import Optional

from sqlmodel import Session, select

from app.models.report import ReportTemplate
from app.models.report_letterhead import ReportLetterhead
from app.models.report_letterhead_version import (
    ReportLetterheadVersion,
    ReportLetterheadVersionStatus,
)


def resolve_fallback_letterhead_version(
    session: Session,
    tenant_id: str,
    template: Optional[ReportTemplate],
) -> Optional[ReportLetterheadVersion]:
    if template is not None and template.preferred_letterhead_id is not None:
        active = session.exec(
            select(ReportLetterheadVersion).where(
                ReportLetterheadVersion.report_letterhead_id == template.preferred_letterhead_id,
                ReportLetterheadVersion.status == ReportLetterheadVersionStatus.ACTIVE,
            )
        ).first()
        if active is not None:
            return active

    if template is not None and template.preferred_letterhead_version_id is not None:
        candidate = session.get(
            ReportLetterheadVersion, template.preferred_letterhead_version_id
        )
        if candidate is not None and candidate.status != ReportLetterheadVersionStatus.ARCHIVED:
            return candidate

    default_letterhead = session.exec(
        select(ReportLetterhead).where(
            ReportLetterhead.tenant_id == tenant_id,
            ReportLetterhead.is_default == True,  # noqa: E712
        )
    ).first()
    if default_letterhead is not None:
        return session.exec(
            select(ReportLetterheadVersion).where(
                ReportLetterheadVersion.report_letterhead_id == default_letterhead.id,
                ReportLetterheadVersion.status == ReportLetterheadVersionStatus.ACTIVE,
            )
        ).first()

    return None
