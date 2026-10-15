"""Report lists: query, filters, ordering and batch enrichment.

Céluma 1.4, issues #5/#6/#7 — the reports counterpart of
`app/services/laboratory_lists.py`. The pre-1.4 handler issued five queries
per report, one of them a scan of `report_version` by `report_id`; measured
on the synthetic large tenant that was 16,807 statements per request.

Read-only: listing never changes report state, versions or published PDFs.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional, Sequence

from sqlalchemy import String, cast, exists, select as sa_select
from sqlmodel import Session, select

from app.models.enums import ReportStatus
from app.models.laboratory import Order
from app.models.patient import Patient
from app.models.report import Report, ReportVersion
from app.models.report_review import ReportReview
from app.models.tenant import Branch
from app.models.user import AppUser
from app.schemas.report import (
    BranchRef,
    OrderRef,
    PatientRef,
    ReportFilterOption,
    ReportFilterOptionsResponse,
    ReportListItem,
    ReviewerWithStatus,
)
from app.services.laboratory_lists import _uuid_list, patients_matching, patient_sort_expr
from app.services.list_search import SearchSpec, search_condition, sort_key

REPORT_SORTS = ("order_code", "patient", "title", "status")


def _reviewed_orders_matching(tenant_id, predicate):
    """Orders with a reviewer whose name or email matches (one pass over reviews)."""
    return (
        sa_select(ReportReview.order_id)
        .join(AppUser, AppUser.id == ReportReview.reviewer_user_id)
        .where(ReportReview.tenant_id == tenant_id,
               predicate(AppUser.full_name) | predicate(AppUser.email))
    )


def report_list_statement(
    tenant_id,
    *,
    q: Optional[str] = None,
    statuses: Sequence[str] = (),
    patient_ids: Sequence[str] = (),
    reviewer_ids: Sequence[str] = (),
    sort: str = "order_code",
    descending: bool = False,
    extra_where: Sequence = (),
):
    """Every report of the tenant matching the query, in a total order."""
    stmt = (
        select(Report)
        .join(Order, Order.id == Report.order_id)
        .outerjoin(Patient, Patient.id == Order.patient_id)
        .where(Report.tenant_id == tenant_id, *extra_where)
    )
    if statuses:
        stmt = stmt.where(Report.status.in_(statuses))
    if patient_ids:
        stmt = stmt.where(Order.patient_id.in_(_uuid_list(patient_ids)))
    if reviewer_ids:
        stmt = stmt.where(exists(sa_select(1).where(
            ReportReview.order_id == Report.order_id,
            ReportReview.reviewer_user_id.in_(_uuid_list(reviewer_ids)),
        )))

    condition = search_condition(q, SearchSpec(
        scalars=(
            Report.title,
            Order.order_code,
            Order.requested_by,
        ),
        related=(
            lambda pred: Order.patient_id.in_(patients_matching(tenant_id, pred)),
            lambda pred: Report.order_id.in_(_reviewed_orders_matching(tenant_id, pred)),
        ),
    ))
    if condition is not None:
        stmt = stmt.where(condition)

    key = {
        "order_code": sort_key(Order.order_code),
        "patient": sort_key(patient_sort_expr()),
        "title": sort_key(Report.title),
        "status": cast(Report.status, String).collate("C"),
    }[sort]
    return stmt.order_by(key.desc() if descending else key.asc(), Report.id)


def build_report_items(
    session: Session, reports: Sequence[Report], *, include_reviewers: bool = True
) -> list[ReportListItem]:
    """List items for `reports` (in order) with a fixed number of queries."""
    ids = [r.id for r in reports]

    def by_id(model, keys):
        keys = {k for k in keys if k is not None}
        if not keys:
            return {}
        return {row.id: row for row in session.exec(select(model).where(model.id.in_(keys))).all()}

    branches = by_id(Branch, (r.branch_id for r in reports))
    orders = by_id(Order, (r.order_id for r in reports))
    patients = by_id(Patient, (o.patient_id for o in orders.values()))

    # The current version; should two rows ever claim `is_current`, the
    # highest version number wins (the pre-1.4 handler took an arbitrary one).
    current: dict = {}
    if ids:
        for version in session.exec(
            select(ReportVersion)
            .where(ReportVersion.report_id.in_(ids), ReportVersion.is_current == True)  # noqa: E712
            .order_by(ReportVersion.report_id, ReportVersion.version_no.desc())
        ).all():
            current.setdefault(version.report_id, version)

    reviewers: dict = defaultdict(list)
    if include_reviewers and orders:
        rows = session.exec(
            select(ReportReview, AppUser)
            .join(AppUser, AppUser.id == ReportReview.reviewer_user_id)
            .where(ReportReview.order_id.in_(list(orders)))
            .order_by(ReportReview.assigned_at, ReportReview.id)
        ).all()
        for review, user in rows:
            reviewers[review.order_id].append(ReviewerWithStatus(
                id=str(user.id),
                name=user.full_name,
                email=user.email,
                avatar_url=user.avatar_url,
                status=review.status.lower(),
                review_id=str(review.id),
            ))

    items = []
    for r in reports:
        branch = branches.get(r.branch_id)
        order = orders.get(r.order_id)
        patient = patients.get(order.patient_id) if order and order.patient_id else None
        version = current.get(r.id)
        items.append(ReportListItem(
            id=str(r.id),
            status=r.status,
            tenant_id=str(r.tenant_id),
            branch=BranchRef(id=str(r.branch_id), name=branch.name if branch else "",
                             code=branch.code if branch else None),
            order=OrderRef(
                id=str(r.order_id),
                order_code=order.order_code if order else "",
                status=order.status if order else "",
                requested_by=order.requested_by if order else None,
                patient=PatientRef(
                    id=str(patient.id),
                    full_name=f"{patient.first_name} {patient.last_name}",
                    patient_code=patient.patient_code,
                ) if patient else None,
            ),
            title=r.title,
            published_at=r.published_at,
            created_at=str(r.created_at) if getattr(r, "created_at", None) else None,
            created_by=str(r.created_by) if r.created_by else None,
            signed_by=str(version.signed_by) if version and version.signed_by else None,
            signed_at=version.signed_at if version else None,
            version_no=version.version_no if version else None,
            has_pdf=bool(version and version.pdf_storage_id),
            reviewers=(reviewers.get(r.order_id) or None) if include_reviewers else None,
        ))
    return items


def report_filter_options(session: Session, tenant_id) -> ReportFilterOptionsResponse:
    """Filter catalogs over the tenant's whole report collection — never a page."""
    in_tenant = Report.tenant_id == tenant_id
    statuses = set(session.exec(select(Report.status).where(in_tenant).distinct()).all())
    patients = session.exec(
        sa_select(Patient.id, patient_sort_expr())
        .where(Patient.tenant_id == tenant_id, exists(
            sa_select(1).select_from(Report).join(Order, Order.id == Report.order_id)
            .where(in_tenant, Order.patient_id == Patient.id)))
        .order_by(sort_key(patient_sort_expr()), Patient.id)
    ).all()
    reviewers = session.exec(
        sa_select(AppUser.id, AppUser.full_name)
        .where(exists(
            sa_select(1).select_from(ReportReview).join(Report, Report.order_id == ReportReview.order_id)
            .where(ReportReview.reviewer_user_id == AppUser.id, in_tenant)))
        .order_by(sort_key(AppUser.full_name), AppUser.id)
    ).all()
    return ReportFilterOptionsResponse(
        statuses=[s.value for s in ReportStatus if s in statuses],
        patients=[ReportFilterOption(value=str(i), text=name or "") for i, name in patients],
        reviewers=[ReportFilterOption(value=str(i), text=name) for i, name in reviewers],
    )
