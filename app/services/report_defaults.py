"""The letterhead half of a NEW report's editor bootstrap, shared by both `report-defaults` endpoints.

`GET /study-types/{id}/report-defaults` (the pre-1.4.1 bootstrap, kept for compatibility) and
`GET /laboratory/orders/{id}/report-defaults` (Céluma 1.4.1) differ only in HOW they pick the clinical
template. Once a template (or none) is picked, the letterhead is resolved here exactly as `create_report`
resolves it — `resolve_effective_letterhead_version` (explicit -> template preferred -> tenant default) — so
the editor's preview and the frozen report cannot diverge, and choosing a clinical template never changes
the letterhead precedence itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from app.models.report import ReportTemplate
from app.models.report_template_version import ReportTemplateVersion, ReportTemplateVersionStatus
from app.services.letterhead_resolution import LetterheadResolutionError, resolve_effective_letterhead_version
from app.services.letterhead_resources import resolve_letterhead_resources


@dataclass
class LetterheadDefaults:
    resolved: bool = False
    #: The letterhead configuration is inconsistent (`LETTERHEAD_MISCONFIGURED`); the message says why.
    misconfigured: bool = False
    misconfigured_detail: Optional[str] = None
    letterhead_version_id: Optional[str] = None
    letterhead_name: Optional[str] = None
    letterhead_id: Optional[str] = None
    letterhead_resolution_source: Optional[str] = None
    letterhead_presentation: Optional[Dict[str, Any]] = None
    letterhead_resolved_resources: Optional[Dict[str, Any]] = None

    def response_fields(self) -> Dict[str, Any]:
        return {
            "letterhead_version_id": self.letterhead_version_id,
            "letterhead_name": self.letterhead_name,
            "letterhead_id": self.letterhead_id,
            "letterhead_resolution_source": self.letterhead_resolution_source,
            "letterhead_presentation": self.letterhead_presentation,
            "letterhead_resolved_resources": self.letterhead_resolved_resources,
        }


def active_template_version_id(session: Session, template: Optional[ReportTemplate]) -> Optional[str]:
    """Diagnostic/provenance only (Céluma 1.3.1 Block C): never a precondition for authoring."""
    if template is None:
        return None
    active = session.exec(
        select(ReportTemplateVersion).where(
            ReportTemplateVersion.report_template_id == template.id,
            ReportTemplateVersion.status == ReportTemplateVersionStatus.ACTIVE,
        )
    ).first()
    return str(active.id) if active is not None else None


def resolve_letterhead_defaults(
    session: Session, tenant_id: str, template: Optional[ReportTemplate]
) -> LetterheadDefaults:
    """Resolve the letterhead a new report built from `template` would get. Never raises for an unconfigured
    tenant: an inconsistent configuration is reported in `misconfigured_detail`, a missing one as
    `resolved=False`."""
    try:
        resolved = resolve_effective_letterhead_version(session, tenant_id, template=template)
    except LetterheadResolutionError as exc:
        return LetterheadDefaults(misconfigured=True, misconfigured_detail=exc.message)
    if resolved is None:
        return LetterheadDefaults()
    resources = resolve_letterhead_resources(resolved.version.configuration, tenant_id, session)
    return LetterheadDefaults(
        resolved=True,
        letterhead_version_id=resolved.letterhead_version_id,
        letterhead_name=resolved.letterhead.name,
        letterhead_id=resolved.letterhead_id,
        letterhead_resolution_source=resolved.source.value,
        letterhead_presentation=resolved.presentation.model_dump(mode="json"),
        letterhead_resolved_resources=resources.model_dump() if resources else None,
    )
