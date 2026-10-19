"""Céluma 1.4 (#13): amendments of signed reports — the shared rules.

Owner decisions (2026-10-18, issue #13):

* D1 — one amendment type: a COMPLETE new emission that supersedes the
  delivered one when it is signed; corrections and supplements (IHC) differ
  only in the recorded category and reason.
* D2 — the amended document identifies itself (amendment number, category,
  reason, the replaced emission's version and signature date) and carries a
  REQUIRED author-written change note. No automatic diff.
* D3 — the last signed emission stays delivered until the amendment is signed;
  an internal "amendment in progress" indicator; an open amendment may be
  cancelled by a reopening actor with their password and a reason.
* D4 — republication notifies the publication recipients in-app only.
* D5 — V2 signed originals only; legacy originals are not amendable.
* D6 — the report follows the amendment cycle (DRAFT -> review -> approval ->
  signing) and its order follows the report.

The identification block lives in the version's JSON body under
``amendment`` so it is frozen with the signed emission and rendered by the
same renderer as the official PDF. Every field but ``change_note`` is
server-owned: a content save can only change the note.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app.models.enums import ReportStatus
from app.models.report import Report, ReportVersion
from app.models.report_amendment import (
    AMENDMENT_STATE_OPEN,
    ReportAmendment,
)

#: JSON body key of the identification block.
AMENDMENT_KEY = "amendment"
#: Upper bound of the author-written change note.
CHANGE_NOTE_MAX_LENGTH = 4000

LEGACY_ORIGINAL_CODE = "amendment_legacy_original"
CHANGE_NOTE_REQUIRED_CODE = "amendment_change_note_required"
NO_SIGNED_EMISSION_CODE = "amendment_no_signed_emission"
NOT_AMENDABLE_CODE = "amendment_report_not_published"
NO_OPEN_AMENDMENT_CODE = "amendment_not_open"
PUBLISH_IN_PROGRESS_CODE = "amendment_publish_in_progress"


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def delivered_emission(session: Session, report: Report) -> Optional[ReportVersion]:
    """The emission delivered to readers: the latest SIGNED version.

    Signing and publication commit together, so the highest signed
    `version_no` is the last successfully published emission. An amendment
    draft is never signed until it republishes, so it can never be selected
    here — which is what keeps the original delivered while it is drafted
    (D3/D6). Never "the highest version_no": that would be the draft.
    """
    return session.exec(
        select(ReportVersion)
        .where(ReportVersion.report_id == report.id, ReportVersion.signed_at.is_not(None))
        .order_by(ReportVersion.version_no.desc())
    ).first()


def open_amendment(session: Session, report: Report) -> Optional[ReportAmendment]:
    return session.exec(
        select(ReportAmendment).where(
            ReportAmendment.report_id == report.id,
            ReportAmendment.state == AMENDMENT_STATE_OPEN,
        )
    ).first()


def next_amendment_no(session: Session, report: Report) -> int:
    numbers = session.exec(
        select(ReportAmendment.amendment_no).where(ReportAmendment.report_id == report.id)
    ).all()
    return (max(numbers) if numbers else 0) + 1


def correlation(amendment: ReportAmendment, source: ReportVersion) -> dict:
    """Ids every amendment event and audit row carries."""
    return {
        "amendment_id": str(amendment.id),
        "amendment_no": amendment.amendment_no,
        "amends_version_id": str(source.id),
        "amends_version_no": source.version_no,
    }


def identification_block(
    amendment: ReportAmendment, source: ReportVersion, change_note: str = ""
) -> dict:
    """The server-owned identification frozen into the amended emission (D2)."""
    return {
        "amendment_no": amendment.amendment_no,
        "category": amendment.category,
        "reason": amendment.reason,
        "amends_version_no": source.version_no,
        "amends_signed_at": _iso(source.signed_at),
        "change_note": change_note,
    }


def change_note_of(body: Optional[dict]) -> str:
    block = (body or {}).get(AMENDMENT_KEY)
    if not isinstance(block, dict):
        return ""
    note = block.get("change_note")
    return note.strip() if isinstance(note, str) else ""


def carry_forward_identification(
    previous_body: Optional[dict], requested_body: Optional[dict], *, in_amendment: bool
) -> Optional[dict]:
    """Apply the identification boundary to a content save.

    Outside an amendment the key is stripped, so no client can forge an
    "amended" document. Inside one, every server-owned field is copied from
    the previous version and only ``change_note`` is taken from the request
    (trimmed, bounded). Mirrors `enforce_author_presentation_boundary`.
    """
    if requested_body is None:
        return None
    body = dict(requested_body)
    requested_block = body.pop(AMENDMENT_KEY, None)
    if not in_amendment:
        return body
    previous_block = (previous_body or {}).get(AMENDMENT_KEY)
    if not isinstance(previous_block, dict):
        return body
    note = ""
    if isinstance(requested_block, dict) and isinstance(requested_block.get("change_note"), str):
        note = requested_block["change_note"].strip()[:CHANGE_NOTE_MAX_LENGTH]
    body[AMENDMENT_KEY] = {**previous_block, "change_note": note}
    return body


def version_amendment(session: Session, version: Optional[ReportVersion]) -> Optional[ReportAmendment]:
    if version is None or version.amendment_id is None:
        return None
    return session.get(ReportAmendment, version.amendment_id)


def externally_delivered_emission(session: Session, report: Report) -> Optional[ReportVersion]:
    """What portals may deliver: the latest signed emission, unless the report
    was retracted. Report status is deliberately NOT the gate: during an
    amendment the report is DRAFT/IN_REVIEW/APPROVED while its last signed
    emission stays delivered (D3/D6), and portals never learn of the draft."""
    if report.status == ReportStatus.RETRACTED:
        return None
    signed = delivered_emission(session, report)
    if signed is not None:
        return signed
    # A published report with no signature evidence on any version can only be
    # historical data from before signing recorded it; it keeps being served
    # exactly as before (its latest version). Amendments cannot exist for it:
    # opening one requires a signed emission.
    if report.status == ReportStatus.PUBLISHED:
        return session.exec(
            select(ReportVersion)
            .where(ReportVersion.report_id == report.id)
            .order_by(ReportVersion.version_no.desc())
        ).first()
    return None


def supersession_reference(session: Session, version: ReportVersion) -> Optional[dict]:
    """For a delivered amended emission, the reference portals show (D3):
    "amended report N, supersedes the emission signed on {date}". Internal
    version numbers stay out of the portal contract."""
    amendment = version_amendment(session, version)
    if amendment is None:
        return None
    source = session.get(ReportVersion, amendment.source_version_id)
    return {
        "amendment_no": amendment.amendment_no,
        "supersedes_signed_at": _iso(source.signed_at) if source else None,
    }
