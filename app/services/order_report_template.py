"""The clinical report template of an order (Céluma 1.4.1 hotfix).

Contract: `celuma-engineering/specs/laboratory/order-report-template.md`; decision record ADR 0022.

One function, `resolve`, answers "which clinical template does a NEW report of this order start from?"
for every reader: the order detail, the editor bootstrap (`GET /laboratory/orders/{id}/report-defaults`)
and `create_report`'s server-side check. Precedence:

1. the order's EXPLICIT choice (`order.report_template_id`), when a user made one;
2. the sample-type default, when the order's samples agree on exactly one template;
3. the study-type default (optional since this hotfix);
4. nothing: there is no laboratory-wide clinical-template fallback (the tenant default is a LETTERHEAD
   concept, resolved separately and unchanged by this module).

Rules that keep the result deterministic:

- Several samples suggesting DIFFERENT templates is `AMBIGUOUS`: no candidate is picked by list order,
  and the study-type default does not silently settle the disagreement. A user must choose explicitly.
- An explicit choice is never replaced by a default. If it points at a deactivated template the result
  is `INACTIVE` (blocked), not a fallback.
- A default that points at a deactivated template is ignored, exactly like an unset one: inactive
  templates are never offered for new reports.
- Removed samples are hidden by the ORM filter; legacy untyped samples contribute nothing.

This module only reads. It never writes, and it never touches an existing report: a report's template is
frozen into its own `rendering_snapshot` at creation and is not re-resolved afterwards.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional
from uuid import UUID

from sqlmodel import Session, select

from app.models.laboratory import Order, Sample
from app.models.report import ReportTemplate
from app.models.sample_type import SampleType
from app.models.study_type import StudyType
from app.services.order_management import ManagementError

SOURCE_ORDER = "ORDER"
SOURCE_SAMPLE_TYPE = "SAMPLE_TYPE"
SOURCE_STUDY_TYPE = "STUDY_TYPE"

#: A usable template was found.
RESOLVED = "RESOLVED"
#: The order's samples suggest different templates and nobody chose one.
AMBIGUOUS = "AMBIGUOUS"
#: The explicit choice points at a deactivated template.
INACTIVE = "INACTIVE"
#: Nothing applies (no explicit choice, no usable default).
NONE = "NONE"


@dataclass
class Resolution:
    status: str
    #: The template a new report must use (RESOLVED), or the inactive explicit one (INACTIVE).
    template: Optional[ReportTemplate] = None
    source: Optional[str] = None
    #: The stored explicit choice, whatever its state.
    explicit: Optional[ReportTemplate] = None
    #: AMBIGUOUS only: the distinct sample-type suggestions, ordered by name then id (display order only).
    candidates: List[ReportTemplate] = field(default_factory=list)
    #: What the catalog alone resolves to, ignoring the explicit choice — i.e. what clearing it restores.
    default_status: str = NONE
    default_template: Optional[ReportTemplate] = None
    default_source: Optional[str] = None

    @property
    def usable(self) -> Optional[ReportTemplate]:
        return self.template if self.status == RESOLVED else None


def _parse(raw) -> Optional[UUID]:
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _active_template(session: Session, tenant_id, template_id) -> Optional[ReportTemplate]:
    if not template_id:
        return None
    template = session.get(ReportTemplate, template_id)
    if template is None or str(template.tenant_id) != str(tenant_id) or not template.is_active:
        return None
    return template


def require_selectable(session: Session, tenant_id, raw_id) -> ReportTemplate:
    """The template a user may store as an order's explicit choice: one of the caller's tenant, active.

    An unknown, malformed or another laboratory's id is the same 404 (ids of other tenants cannot be
    probed); a deactivated template is a 409 naming it. Checked before anything is written."""
    template_id = _parse(raw_id)
    template = session.get(ReportTemplate, template_id) if template_id else None
    if template is None or str(template.tenant_id) != str(tenant_id):
        raise ManagementError(404, "report_template_not_found", "Report template not found")
    if not template.is_active:
        raise ManagementError(
            409, "report_template_inactive",
            f"La plantilla de reporte '{template.name}' está desactivada y no puede elegirse para una orden.",
        )
    return template


def _sample_type_templates(session: Session, order: Order) -> List[ReportTemplate]:
    type_ids = {
        row for row in session.exec(
            select(Sample.sample_type_id).where(Sample.order_id == order.id, Sample.sample_type_id.is_not(None))
        ).all()
    }
    found: dict = {}
    for type_id in type_ids:
        sample_type = session.get(SampleType, type_id)
        if sample_type is None or str(sample_type.tenant_id) != str(order.tenant_id):
            continue
        template = _active_template(session, order.tenant_id, sample_type.default_report_template_id)
        if template is not None:
            found[template.id] = template
    return sorted(found.values(), key=lambda t: ((t.name or "").casefold(), str(t.id)))


def _study_type_template(session: Session, order: Order) -> Optional[ReportTemplate]:
    if not order.study_type_id:
        return None
    study_type = session.get(StudyType, order.study_type_id)
    if study_type is None or str(study_type.tenant_id) != str(order.tenant_id):
        return None
    return _active_template(session, order.tenant_id, study_type.default_report_template_id)


def resolve(session: Session, order: Order) -> Resolution:
    """Which template a NEW report of `order` starts from (module docstring). Read-only."""
    candidates = _sample_type_templates(session, order)
    if len(candidates) == 1:
        default_status, default_template, default_source = RESOLVED, candidates[0], SOURCE_SAMPLE_TYPE
    elif len(candidates) > 1:
        default_status, default_template, default_source = AMBIGUOUS, None, None
    else:
        study_template = _study_type_template(session, order)
        if study_template is not None:
            default_status, default_template, default_source = RESOLVED, study_template, SOURCE_STUDY_TYPE
        else:
            default_status, default_template, default_source = NONE, None, None
    ambiguous = candidates if default_status == AMBIGUOUS else []

    explicit = None
    if order.report_template_id:
        row = session.get(ReportTemplate, order.report_template_id)
        # The write paths only ever store a template of the order's tenant; anything else is ignored.
        if row is not None and str(row.tenant_id) == str(order.tenant_id):
            explicit = row
    common = dict(explicit=explicit, candidates=ambiguous, default_status=default_status,
                  default_template=default_template, default_source=default_source)
    if explicit is not None:
        return Resolution(status=RESOLVED if explicit.is_active else INACTIVE, template=explicit,
                          source=SOURCE_ORDER, **common)
    return Resolution(status=default_status, template=default_template, source=default_source, **common)


ORDER_TEMPLATE_CHANGED = "order_report_template_changed"
ORDER_TEMPLATE_AMBIGUOUS = "order_report_template_ambiguous"
ORDER_TEMPLATE_INACTIVE = "order_report_template_inactive"


def ensure_new_report_template(session: Session, order: Order, template: Optional[ReportTemplate]) -> None:
    """`create_report`'s server-side check (called under the order lock, before any durable effect).

    A new report must be built from the order's effective template, so a stale editor (the order's choice
    or its samples changed after the bootstrap), an older client still reading the study type, or a direct
    API call cannot freeze a different structure than the order says. Answers 409 with a stable code; the
    author's content is still in their editor and reloading shows the current choice.

    When nothing resolves for the order (no explicit choice and no usable default) the caller's selected
    template stands: that is the pre-1.4.1 API contract, and there is no order-level decision to defend."""
    resolution = resolve(session, order)
    if resolution.status == AMBIGUOUS:
        raise ManagementError(
            409, ORDER_TEMPLATE_AMBIGUOUS,
            "Las muestras de esta orden sugieren plantillas de reporte distintas. Elige la plantilla en "
            "Editar orden antes de crear el reporte.",
        )
    if resolution.status == INACTIVE:
        raise ManagementError(
            409, ORDER_TEMPLATE_INACTIVE,
            "La plantilla elegida para esta orden está desactivada. Elige otra en Editar orden antes de "
            "crear el reporte.",
        )
    if resolution.status == RESOLVED and (template is None or template.id != resolution.template.id):
        raise ManagementError(
            409, ORDER_TEMPLATE_CHANGED,
            "La plantilla de reporte de esta orden cambió mientras editabas. Vuelve a cargar el reporte "
            "para trabajar con la plantilla vigente de la orden.",
        )
