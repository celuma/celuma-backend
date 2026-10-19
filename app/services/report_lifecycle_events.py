"""Céluma 1.4 (#13): canonical names and shapes of report lifecycle events.

Timeline consumers identify a transition by `event_metadata["action"]` (and
`event_type`), never by `description`, which is only a Spanish fallback for a
consumer that does not know the action. The full table is in celuma-engineering
`specs/reports/lifecycle.md` ("Lifecycle events").

Publication used to be stored as `REPORT_APPROVED` with `published: true`,
which the timeline rendered as a second approval. From 1.4 it uses the
`REPORT_PUBLISHED` member that has existed in the native `eventtype` enum since
`v1_0_0` (no enum migration) and that `order_lifecycle` already counts as
review evidence. Reopening for amendment and cancelling an amendment use the
generic `STATUS_CHANGED`, like the ordinary reopen (R2). Historical rows are not
rewritten.

Every builder here is called only after the transition was staged, inside its
transaction, so a refused or failed transition never produces one.
"""
from __future__ import annotations

from typing import Optional

from app.models.enums import EventType, ReportStatus
from app.models.events import OrderEvent
from app.models.report import Report, ReportVersion
from app.models.report_amendment import ReportAmendment
from app.models.user import AppUser
from app.services import report_amendments

ACTION_REPORT_REOPENED = "REPORT_REOPENED"
ACTION_REPORT_PUBLISHED = "REPORT_PUBLISHED"
ACTION_REPORT_REOPENED_FOR_AMENDMENT = "REPORT_REOPENED_FOR_AMENDMENT"
ACTION_REPORT_REPUBLISHED = "REPORT_REPUBLISHED"
ACTION_REPORT_AMENDMENT_CANCELLED = "REPORT_AMENDMENT_CANCELLED"

AUDIT_REOPEN_FOR_AMENDMENT = "REPORT.REOPEN_FOR_AMENDMENT"
AUDIT_AMENDMENT_CANCEL = "REPORT.AMENDMENT_CANCEL"

REOPEN_KIND_UNSIGNED = "UNSIGNED"
PUBLICATION_KIND_INITIAL = "INITIAL"
PUBLICATION_KIND_AMENDMENT = "AMENDMENT"


def _status_value(status) -> Optional[str]:
    return getattr(status, "value", status)


def _actor_name(user: AppUser) -> str:
    return user.full_name or user.username


def publication_correlation(
    version: ReportVersion,
    amendment: Optional[ReportAmendment] = None,
    source: Optional[ReportVersion] = None,
) -> dict:
    """Ids shared by the publication audit row and its timeline event."""
    values = {
        "publication_kind": PUBLICATION_KIND_AMENDMENT if amendment else PUBLICATION_KIND_INITIAL,
        "report_version_id": str(version.id),
        "version_no": version.version_no,
        "signed_at": version.signed_at.isoformat() if version.signed_at else None,
        "pdf_sha256": version.pdf_sha256,
    }
    if amendment is not None and source is not None:
        values.update(report_amendments.correlation(amendment, source))
        values["reason"] = amendment.reason
        values["category"] = amendment.category
    return values


def build_publication_event(
    report: Report,
    version: ReportVersion,
    signer: AppUser,
    *,
    old_status,
    changelog: Optional[str],
    amendment: Optional[ReportAmendment] = None,
    source: Optional[ReportVersion] = None,
) -> OrderEvent:
    """The single timeline row of a successful sign-and-publish: an initial
    publication, or the republication of an amendment (which supersedes
    `source`, the emission delivered until now)."""
    republication = amendment is not None
    return OrderEvent(
        tenant_id=report.tenant_id,
        branch_id=report.branch_id,
        order_id=report.order_id,
        event_type=EventType.REPORT_PUBLISHED,
        description=(
            "Reporte enmendado firmado y republicado" if republication
            else "Reporte firmado y publicado"
        ),
        event_metadata={
            "action": ACTION_REPORT_REPUBLISHED if republication else ACTION_REPORT_PUBLISHED,
            "report_id": str(report.id),
            "report_title": report.title,
            "from_status": _status_value(old_status),
            "to_status": _status_value(ReportStatus.PUBLISHED),
            **publication_correlation(version, amendment, source),
            "signer_id": str(signer.id),
            "signer_name": _actor_name(signer),
            "changelog": changelog or None,
            "confirmation": "password",
            # Kept from the pre-1.4 shape for any consumer that still reads it.
            "published": True,
        },
        created_by=signer.id,
    )


def build_amendment_reopen_event(
    report: Report,
    amendment: ReportAmendment,
    source: ReportVersion,
    draft: ReportVersion,
    actor: AppUser,
    *,
    old_status,
) -> OrderEvent:
    return OrderEvent(
        tenant_id=report.tenant_id,
        branch_id=report.branch_id,
        order_id=report.order_id,
        event_type=EventType.STATUS_CHANGED,
        description=f"Reporte reabierto para enmienda {amendment.amendment_no}",
        event_metadata={
            "action": ACTION_REPORT_REOPENED_FOR_AMENDMENT,
            "report_id": str(report.id),
            "report_title": report.title,
            "from_status": _status_value(old_status),
            "to_status": _status_value(report.status),
            **report_amendments.correlation(amendment, source),
            "report_version_id": str(draft.id),
            "version_no": draft.version_no,
            "category": amendment.category,
            "reason": amendment.reason,
            "reopened_by": str(actor.id),
            "reopened_by_name": _actor_name(actor),
            "confirmation": "password",
        },
        created_by=actor.id,
    )


def build_amendment_cancel_event(
    report: Report,
    amendment: ReportAmendment,
    source: ReportVersion,
    actor: AppUser,
    *,
    old_status,
) -> OrderEvent:
    return OrderEvent(
        tenant_id=report.tenant_id,
        branch_id=report.branch_id,
        order_id=report.order_id,
        event_type=EventType.STATUS_CHANGED,
        description=f"Enmienda {amendment.amendment_no} cancelada",
        event_metadata={
            "action": ACTION_REPORT_AMENDMENT_CANCELLED,
            "report_id": str(report.id),
            "report_title": report.title,
            "from_status": _status_value(old_status),
            "to_status": _status_value(report.status),
            **report_amendments.correlation(amendment, source),
            # The emission delivered again (unchanged) after the cancellation.
            "report_version_id": str(source.id),
            "version_no": source.version_no,
            "reason": amendment.cancel_reason,
            "cancelled_by": str(actor.id),
            "cancelled_by_name": _actor_name(actor),
            "confirmation": "password",
        },
        created_by=actor.id,
    )


def amendment_context(
    amendment: Optional[ReportAmendment], source: Optional[ReportVersion]
) -> dict:
    """Correlation added to the intervening transitions (version created,
    submitted, approved, changes requested, ordinary reopen) while an
    amendment is open, so its whole history links to the source emission."""
    if amendment is None or source is None:
        return {}
    return report_amendments.correlation(amendment, source)
