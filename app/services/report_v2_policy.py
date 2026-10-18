"""Céluma 1.4 (#22): Reports V2 is the only authoring path.

Legacy (V1, ``schema_version`` NULL/1) report versions stay readable and
their issued PDFs stay downloadable, but nothing may *author or finalize*
a new legacy artifact. ``require_v2_authoring`` is the single guard used by
every route that would otherwise continue an unsigned legacy report:
content saves, submit-for-review, official-PDF generation and signing.

Nothing here converts or rewrites a legacy version. Conversion would have
to be an explicit, lossless workflow of its own; until one exists the
report is blocked with an actionable message and its stored content,
snapshots and PDFs are left untouched. See
specs/reports/reports-v2-only.md in celuma-engineering.
"""
from __future__ import annotations

from fastapi import HTTPException

from app.models.enums import ReportStatus
from app.models.report import Report, ReportVersion

#: Pre-signing states. PUBLISHED/RETRACTED reports are historical artifacts
#: that no authoring route accepts anyway.
UNSIGNED_STATUSES = (ReportStatus.DRAFT, ReportStatus.IN_REVIEW, ReportStatus.APPROVED)

LEGACY_AUTHORING_RETIRED_CODE = "legacy_report_authoring_retired"


def is_legacy_version(version: ReportVersion | None) -> bool:
    """A missing version or one without the V2 marker is not V2 content."""
    return version is None or version.schema_version != 2


def require_v2_authoring(report: Report, version: ReportVersion | None) -> None:
    """Refuse (409) to continue an unsigned report whose version is not V2."""
    if report.status not in UNSIGNED_STATUSES or not is_legacy_version(version):
        return
    raise HTTPException(
        409,
        {
            "code": LEGACY_AUTHORING_RETIRED_CODE,
            "message": (
                "Este reporte se creó con el formato anterior, que ya no admite "
                "edición, envío a revisión ni firma. Su contenido y los PDF ya "
                "emitidos se conservan y siguen disponibles para consulta. "
                "Contacta al administrador de tu laboratorio o al soporte de "
                "Céluma para resolver este borrador."
            ),
        },
    )
