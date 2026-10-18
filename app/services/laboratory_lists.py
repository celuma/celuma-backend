"""Sample and order lists: query, filters, ordering and batch enrichment.

Céluma 1.4, issues #5/#6/#7. Contract:
`celuma-engineering/specs/lists/search-and-pagination.md`.

Before 1.4 every list handler loaded the tenant's whole collection and then
issued several queries *per row* (branch, order, patient, physician, sample
count, labels, assignees). Measured on the synthetic large tenant that was
39,777 statements for one samples request and 32,337 for one orders request.
Here the row set is selected by one query (with search, filters and ordering
applied in SQL) and its related data is loaded with a fixed number of
`IN (...)` queries, whatever the number of rows.

Tenant scope is the caller's: every query starts from `tenant_id`. Like the
pre-1.4 handlers, these lists are tenant-wide, not branch-filtered.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Optional, Sequence
from uuid import UUID

from sqlalchemy import String, cast, exists, func, or_, select as sa_select
from sqlmodel import Session, select

from app.models.assignment import Assignment
from app.models.enums import OrderStatus, SampleState
from app.models.laboratory import Label, Order, OrderLabel, Sample, SampleLabel
from app.models.order_management import OrderRequestingPhysician
from app.models.patient import Patient
from app.models.requesting_physician import RequestingPhysician
from app.models.sample_type import SampleType
from app.models.tenant import Branch
from app.models.user import AppUser
from app.schemas.laboratory import (
    BranchRef,
    FilterOption,
    LabelResponse,
    OrderFilterOptionsResponse,
    OrderListItem,
    OrderSlim,
    PatientRef,
    RequestingPhysicianRef,
    SampleFilterOptionsResponse,
    SampleListItem,
    UserRef,
)
from app.services import order_management
from app.services.list_search import SearchSpec, search_condition, sort_key
from app.services.sample_types import UNTYPED_FILTER_VALUE, sample_type_refs

SAMPLE_SORTS = ("sample_code", "order_code", "patient")
ORDER_SORTS = ("order_code", "patient", "requester", "status")


# ---------------------------------------------------------------------------
# Shared expressions
# ---------------------------------------------------------------------------


def patient_name_expr():
    """The list's patient display name: "first last" (as `PatientRef` builds it)."""
    return func.concat_ws(" ", Patient.first_name, Patient.last_name)


def patient_sort_expr():
    """Name, falling back to the code — the pre-1.4 client sorter's rule."""
    return func.coalesce(func.nullif(patient_name_expr(), ""), Patient.patient_code)


def physician_name_expr():
    return func.coalesce(
        func.nullif(RequestingPhysician.full_name, ""),
        func.concat_ws(" ", RequestingPhysician.first_name, RequestingPhysician.last_name),
    )


def _uuid_list(values: Iterable[str]) -> list[UUID]:
    return [UUID(str(v)) for v in values]


# Search over related entities. Each builder is an uncorrelated
# `IN (SELECT ...)`: the entity's text is matched once per entity, not once
# per list row (measured ~9x cheaper for typo-tolerant patient names).


def patients_matching(tenant_id, predicate):
    return sa_select(Patient.id).where(
        Patient.tenant_id == tenant_id,
        predicate(patient_name_expr()) | predicate(Patient.patient_code),
    )


def physicians_matching(tenant_id, predicate):
    return sa_select(RequestingPhysician.id).where(
        RequestingPhysician.tenant_id == tenant_id,
        predicate(physician_name_expr()) | predicate(RequestingPhysician.physician_code),
    )


def _labelled_matching(link_model, link_fk, tenant_id, predicate):
    return (
        sa_select(link_fk)
        .join(Label, Label.id == link_model.label_id)
        .where(Label.tenant_id == tenant_id, predicate(Label.name))
    )


def _assigned_matching(item_type: str, tenant_id, predicate):
    return (
        sa_select(Assignment.item_id)
        .join(AppUser, AppUser.id == Assignment.assignee_user_id)
        .where(
            Assignment.tenant_id == tenant_id,
            Assignment.item_type == item_type,
            Assignment.unassigned_at.is_(None),
            predicate(AppUser.full_name) | predicate(AppUser.email),
        )
    )


def _assigned_to_any(item_type: str, owner_id, user_ids: Sequence[UUID]):
    return exists(
        sa_select(1).where(
            Assignment.item_type == item_type,
            Assignment.item_id == owner_id,
            Assignment.unassigned_at.is_(None),
            Assignment.assignee_user_id.in_(user_ids),
        )
    )


def _label_refs(rows) -> dict:
    out: dict = defaultdict(list)
    for owner_id, label in rows:
        out[owner_id].append(
            LabelResponse(id=str(label.id), name=label.name, color=label.color,
                          tenant_id=str(label.tenant_id), created_at=label.created_at)
        )
    return out


def _assignee_refs(session: Session, item_type: str, ids: Sequence[UUID]) -> dict:
    out: dict = defaultdict(list)
    if not ids:
        return out
    rows = session.exec(
        select(Assignment.item_id, AppUser)
        .join(AppUser, AppUser.id == Assignment.assignee_user_id)
        .where(
            Assignment.item_type == item_type,
            Assignment.item_id.in_(ids),
            Assignment.unassigned_at.is_(None),
        )
        .order_by(AppUser.full_name, AppUser.id)
    ).all()
    seen: set = set()
    for item_id, user in rows:
        if (item_id, user.id) in seen:
            continue
        seen.add((item_id, user.id))
        out[item_id].append(UserRef(id=str(user.id), name=user.full_name, email=user.email, avatar_url=user.avatar_url))
    return out


def _by_id(session: Session, model, ids: Iterable) -> dict:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return {row.id: row for row in session.exec(select(model).where(model.id.in_(ids))).all()}


def _branch_ref(branch_id, branches: dict) -> BranchRef:
    branch = branches.get(branch_id)
    return BranchRef(id=str(branch_id), name=branch.name if branch else "", code=branch.code if branch else None)


def patient_ref(patient: Optional[Patient]) -> Optional[PatientRef]:
    if not patient:
        return None
    return PatientRef(id=str(patient.id), full_name=f"{patient.first_name} {patient.last_name}",
                      patient_code=patient.patient_code)


def physician_ref(physician: Optional[RequestingPhysician]) -> Optional[RequestingPhysicianRef]:
    if not physician:
        return None
    return RequestingPhysicianRef(
        id=str(physician.id),
        full_name=physician.full_name or f"{physician.first_name} {physician.last_name}".strip(),
        physician_code=physician.physician_code,
        specialty=physician.specialty,
        institution=physician.institution,
        email=physician.email,
    )


def _options(rows, *, text_of) -> list[FilterOption]:
    return [FilterOption(value=str(value), text=text_of(rest)) for value, *rest in rows]


# ---------------------------------------------------------------------------
# Samples
# ---------------------------------------------------------------------------


def sample_list_statement(
    tenant_id,
    *,
    q: Optional[str] = None,
    states: Sequence[str] = (),
    sample_type_ids: Sequence[str] = (),
    order_ids: Sequence[str] = (),
    patient_ids: Sequence[str] = (),
    label_ids: Sequence[str] = (),
    assignee_ids: Sequence[str] = (),
    sort: str = "sample_code",
    descending: bool = False,
):
    """Every sample of the tenant matching the query, in a total order."""
    stmt = (
        select(Sample)
        .join(Order, Order.id == Sample.order_id)
        .outerjoin(Patient, Patient.id == Order.patient_id)
        .outerjoin(SampleType, SampleType.id == Sample.sample_type_id)
        .where(Sample.tenant_id == tenant_id)
    )
    if states:
        stmt = stmt.where(Sample.state.in_(states))
    if sample_type_ids:
        # Catalog identities, plus "none" = samples without a type ("Sin tipo").
        wanted = [UUID(v) for v in sample_type_ids if v != UNTYPED_FILTER_VALUE]
        clauses = []
        if wanted:
            clauses.append(Sample.sample_type_id.in_(wanted))
        if UNTYPED_FILTER_VALUE in sample_type_ids:
            clauses.append(Sample.sample_type_id.is_(None))
        stmt = stmt.where(or_(*clauses))
    if order_ids:
        stmt = stmt.where(Sample.order_id.in_(_uuid_list(order_ids)))
    if patient_ids:
        stmt = stmt.where(Order.patient_id.in_(_uuid_list(patient_ids)))
    if label_ids:
        stmt = stmt.where(
            exists(sa_select(1).where(SampleLabel.sample_id == Sample.id,
                                      SampleLabel.label_id.in_(_uuid_list(label_ids))))
        )
    if assignee_ids:
        stmt = stmt.where(_assigned_to_any("sample", Sample.id, _uuid_list(assignee_ids)))

    condition = search_condition(q, SearchSpec(
        scalars=(
            Sample.sample_code,
            SampleType.name,
            SampleType.code,
            cast(Sample.state, String),
            Order.order_code,
            Order.requested_by,
        ),
        related=(
            lambda pred: Order.patient_id.in_(patients_matching(tenant_id, pred)),
            lambda pred: Sample.id.in_(_labelled_matching(SampleLabel, SampleLabel.sample_id, tenant_id, pred)),
            lambda pred: Sample.id.in_(_assigned_matching("sample", tenant_id, pred)),
        ),
    ))
    if condition is not None:
        stmt = stmt.where(condition)

    key = {
        "sample_code": sort_key(Sample.sample_code),
        "order_code": sort_key(Order.order_code),
        "patient": sort_key(patient_sort_expr()),
    }[sort]
    return stmt.order_by(key.desc() if descending else key.asc(), Sample.id)


def build_sample_items(session: Session, samples: Sequence[Sample]) -> list[SampleListItem]:
    """List items for `samples` (in order) with a fixed number of queries."""
    ids = [s.id for s in samples]
    type_refs = sample_type_refs(session, (s.sample_type_id for s in samples))
    orders = _by_id(session, Order, (s.order_id for s in samples))
    patients = _by_id(session, Patient, (o.patient_id for o in orders.values()))
    branches = _by_id(session, Branch, (s.branch_id for s in samples))
    labels = _label_refs(session.exec(
        select(SampleLabel.sample_id, Label)
        .join(Label, Label.id == SampleLabel.label_id)
        .where(SampleLabel.sample_id.in_(ids))
        .order_by(Label.name, Label.id)
    ).all()) if ids else {}
    assignees = _assignee_refs(session, "sample", ids)

    items = []
    for s in samples:
        order = orders.get(s.order_id)
        patient = patients.get(order.patient_id) if order and order.patient_id else None
        items.append(SampleListItem(
            id=str(s.id),
            sample_code=s.sample_code,
            sample_type=type_refs.get(s.sample_type_id),
            state=s.state,
            tenant_id=str(s.tenant_id),
            branch=_branch_ref(s.branch_id, branches),
            order=OrderSlim(
                id=str(s.order_id),
                order_code=order.order_code if order else "",
                status=order.status if order else "",
                requested_by=order.requested_by if order else None,
                patient=patient_ref(patient),
            ),
            labels=labels.get(s.id) or None,
            assignees=assignees.get(s.id) or None,
        ))
    return items


def sample_filter_options(session: Session, tenant_id) -> SampleFilterOptionsResponse:
    """Filter catalogs over the tenant's whole sample collection — never a page."""
    in_tenant = Sample.tenant_id == tenant_id
    states = set(session.exec(select(Sample.state).where(in_tenant).distinct()).all())
    types = session.exec(
        sa_select(SampleType.id, SampleType.name, SampleType.is_active)
        .where(SampleType.tenant_id == tenant_id, exists(
            sa_select(1).where(Sample.sample_type_id == SampleType.id, in_tenant)))
        .order_by(sort_key(SampleType.name), SampleType.id)
    ).all()
    has_untyped = session.exec(
        select(exists(sa_select(1).where(in_tenant, Sample.sample_type_id.is_(None))))
    ).one()
    orders = session.exec(
        sa_select(Order.id, Order.order_code)
        .where(exists(sa_select(1).where(Sample.order_id == Order.id, in_tenant)), Order.tenant_id == tenant_id)
        .order_by(sort_key(Order.order_code), Order.id)
    ).all()
    patients = session.exec(
        sa_select(Patient.id, patient_sort_expr())
        .where(Patient.tenant_id == tenant_id, exists(
            sa_select(1).select_from(Sample).join(Order, Order.id == Sample.order_id)
            .where(in_tenant, Order.patient_id == Patient.id)))
        .order_by(sort_key(patient_sort_expr()), Patient.id)
    ).all()
    labels = session.exec(
        sa_select(Label.id, Label.name, Label.color)
        .where(Label.tenant_id == tenant_id, exists(
            sa_select(1).select_from(SampleLabel).join(Sample, Sample.id == SampleLabel.sample_id)
            .where(SampleLabel.label_id == Label.id, in_tenant)))
        .order_by(sort_key(Label.name), Label.id)
    ).all()
    assignees = session.exec(
        sa_select(AppUser.id, AppUser.full_name)
        .where(exists(
            sa_select(1).select_from(Assignment).join(Sample, Sample.id == Assignment.item_id)
            .where(Assignment.item_type == "sample", Assignment.unassigned_at.is_(None),
                   Assignment.assignee_user_id == AppUser.id, in_tenant)))
        .order_by(sort_key(AppUser.full_name), AppUser.id)
    ).all()
    return SampleFilterOptionsResponse(
        states=[s.value for s in SampleState if s in states],
        sample_types=[
            FilterOption(value=str(i), text=name if active else f"{name} (inactivo)")
            for i, name, active in types
        ],
        has_untyped_samples=bool(has_untyped),
        orders=_options(orders, text_of=lambda rest: rest[0]),
        patients=_options(patients, text_of=lambda rest: rest[0] or ""),
        labels=[FilterOption(value=str(i), text=name, color=color) for i, name, color in labels],
        assignees=_options(assignees, text_of=lambda rest: rest[0]),
    )


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def order_list_statement(
    tenant_id,
    *,
    q: Optional[str] = None,
    statuses: Sequence[str] = (),
    patient_ids: Sequence[str] = (),
    physician_ids: Sequence[str] = (),
    has_report: Optional[bool] = None,
    has_invoice: Optional[bool] = None,
    label_ids: Sequence[str] = (),
    assignee_ids: Sequence[str] = (),
    sort: str = "order_code",
    descending: bool = False,
    extra_where: Sequence = (),
):
    """Every order of the tenant matching the query, in a total order."""
    stmt = (
        select(Order)
        .outerjoin(Patient, Patient.id == Order.patient_id)
        .where(Order.tenant_id == tenant_id, *extra_where)
    )
    if statuses:
        stmt = stmt.where(Order.status.in_(statuses))
    if patient_ids:
        stmt = stmt.where(Order.patient_id.in_(_uuid_list(patient_ids)))
    if physician_ids:
        wanted = _uuid_list(physician_ids)
        stmt = stmt.where(or_(
            Order.id.in_(sa_select(OrderRequestingPhysician.order_id).where(
                OrderRequestingPhysician.requesting_physician_id.in_(wanted))),
            Order.requesting_physician_id.in_(wanted),  # legacy single column (pre-1.4 rows not in the set table)
        ))
    if has_report is not None:
        stmt = stmt.where(Order.report_id.is_not(None) if has_report else Order.report_id.is_(None))
    if has_invoice is not None:
        stmt = stmt.where(Order.invoice_id.is_not(None) if has_invoice else Order.invoice_id.is_(None))
    if label_ids:
        stmt = stmt.where(
            exists(sa_select(1).where(OrderLabel.order_id == Order.id,
                                      OrderLabel.label_id.in_(_uuid_list(label_ids))))
        )
    if assignee_ids:
        stmt = stmt.where(_assigned_to_any("lab_order", Order.id, _uuid_list(assignee_ids)))

    condition = search_condition(q, SearchSpec(
        scalars=(
            Order.order_code,
            Order.requested_by,
            Order.expediente,
            Order.notes,
        ),
        related=(
            lambda pred: Order.patient_id.in_(patients_matching(tenant_id, pred)),
            lambda pred: Order.requesting_physician_id.in_(physicians_matching(tenant_id, pred)),
            lambda pred: Order.id.in_(
                sa_select(OrderRequestingPhysician.order_id).where(
                    OrderRequestingPhysician.requesting_physician_id.in_(physicians_matching(tenant_id, pred)))),
            lambda pred: Order.id.in_(_labelled_matching(OrderLabel, OrderLabel.order_id, tenant_id, pred)),
            lambda pred: Order.id.in_(_assigned_matching("lab_order", tenant_id, pred)),
        ),
    ))
    if condition is not None:
        stmt = stmt.where(condition)

    key = {
        "order_code": sort_key(Order.order_code),
        "patient": sort_key(patient_sort_expr()),
        # Several physicians, no primary: sort by the alphabetically first name (deterministic, no hierarchy).
        "requester": sort_key(func.coalesce(
            sa_select(func.min(physician_name_expr()))
            .select_from(OrderRequestingPhysician)
            .join(RequestingPhysician, RequestingPhysician.id == OrderRequestingPhysician.requesting_physician_id)
            .where(OrderRequestingPhysician.order_id == Order.id)
            .correlate(Order)
            .scalar_subquery(),
            sa_select(func.min(physician_name_expr()))
            .select_from(RequestingPhysician)
            .where(RequestingPhysician.id == Order.requesting_physician_id)
            .correlate(Order)
            .scalar_subquery(),
            Order.requested_by,
        )),
        "status": cast(Order.status, String).collate("C"),
    }[sort]
    return stmt.order_by(key.desc() if descending else key.asc(), Order.id)


def build_order_items(session: Session, orders: Sequence[Order]) -> list[OrderListItem]:
    """List items for `orders` (in order) with a fixed number of queries."""
    ids = [o.id for o in orders]
    branches = _by_id(session, Branch, (o.branch_id for o in orders))
    patients = _by_id(session, Patient, (o.patient_id for o in orders))
    physician_sets = order_management.physicians_by_order(session, orders)
    sample_counts = dict(session.exec(
        select(Sample.order_id, func.count()).where(Sample.order_id.in_(ids)).group_by(Sample.order_id)
    ).all()) if ids else {}
    labels = _label_refs(session.exec(
        select(OrderLabel.order_id, Label)
        .join(Label, Label.id == OrderLabel.label_id)
        .where(OrderLabel.order_id.in_(ids))
        .order_by(Label.name, Label.id)
    ).all()) if ids else {}
    assignees = _assignee_refs(session, "lab_order", ids)

    return [
        OrderListItem(
            id=str(o.id),
            order_code=o.order_code,
            status=o.status,
            tenant_id=str(o.tenant_id),
            branch=_branch_ref(o.branch_id, branches),
            patient=patient_ref(patients.get(o.patient_id)),
            requesting_physician=(
                physician_ref(physician_sets[o.id][0]) if len(physician_sets[o.id]) == 1 else None),
            requesting_physicians=[physician_ref(p) for p in physician_sets[o.id]],
            expediente=o.expediente,
            requested_by=o.requested_by,
            notes=o.notes,
            created_at=str(o.created_at) if getattr(o, "created_at", None) else None,
            report_id=str(o.report_id) if o.report_id else None,
            invoice_id=str(o.invoice_id) if o.invoice_id else None,
            study_type_id=str(o.study_type_id) if o.study_type_id else None,
            sample_count=sample_counts.get(o.id, 0),
            has_report=o.report_id is not None,
            has_invoice=o.invoice_id is not None,
            labels=labels.get(o.id) or None,
            assignees=assignees.get(o.id) or None,
        )
        for o in orders
    ]


def order_filter_options(session: Session, tenant_id) -> OrderFilterOptionsResponse:
    """Filter catalogs over the tenant's whole order collection — never a page."""
    in_tenant = Order.tenant_id == tenant_id
    statuses = set(session.exec(select(Order.status).where(in_tenant).distinct()).all())
    patients = session.exec(
        sa_select(Patient.id, patient_sort_expr())
        .where(Patient.tenant_id == tenant_id,
               exists(sa_select(1).where(Order.patient_id == Patient.id, in_tenant)))
        .order_by(sort_key(patient_sort_expr()), Patient.id)
    ).all()
    physicians = session.exec(
        sa_select(RequestingPhysician.id, physician_name_expr())
        .where(RequestingPhysician.tenant_id == tenant_id,
               or_(exists(sa_select(1).where(Order.requesting_physician_id == RequestingPhysician.id, in_tenant)),
                   exists(sa_select(1).select_from(OrderRequestingPhysician)
                          .join(Order, Order.id == OrderRequestingPhysician.order_id)
                          .where(OrderRequestingPhysician.requesting_physician_id == RequestingPhysician.id, in_tenant))))
        .order_by(sort_key(physician_name_expr()), RequestingPhysician.id)
    ).all()
    labels = session.exec(
        sa_select(Label.id, Label.name, Label.color)
        .where(Label.tenant_id == tenant_id, exists(
            sa_select(1).select_from(OrderLabel).join(Order, Order.id == OrderLabel.order_id)
            .where(OrderLabel.label_id == Label.id, in_tenant)))
        .order_by(sort_key(Label.name), Label.id)
    ).all()
    assignees = session.exec(
        sa_select(AppUser.id, AppUser.full_name)
        .where(exists(
            sa_select(1).select_from(Assignment).join(Order, Order.id == Assignment.item_id)
            .where(Assignment.item_type == "lab_order", Assignment.unassigned_at.is_(None),
                   Assignment.assignee_user_id == AppUser.id, in_tenant)))
        .order_by(sort_key(AppUser.full_name), AppUser.id)
    ).all()
    has_requester = session.exec(
        select(exists(sa_select(1).where(
            in_tenant,
            Order.requesting_physician_id.is_not(None) | (func.coalesce(Order.requested_by, "") != "")
            | exists(sa_select(1).where(OrderRequestingPhysician.order_id == Order.id)),
        )))
    ).one()
    return OrderFilterOptionsResponse(
        statuses=[s.value for s in OrderStatus if s in statuses],
        patients=_options(patients, text_of=lambda rest: rest[0] or ""),
        requesting_physicians=_options(physicians, text_of=lambda rest: rest[0] or ""),
        labels=[FilterOption(value=str(i), text=name, color=color) for i, name, color in labels],
        assignees=_options(assignees, text_of=lambda rest: rest[0]),
        has_requester=bool(has_requester),
    )
