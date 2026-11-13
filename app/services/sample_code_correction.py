"""Narrow post-registration sample-code correction (Céluma 1.4, issue #10).

Registration generates codes automatically and never accepts a typed one. This is the ONLY way an
issued sample code changes afterwards, and it changes nothing else of the sample: identity, links,
state, notes, images and labels stay.

Rules (contract: `celuma-engineering/specs/laboratory/order-sample-management.md`):

- Lifecycle: the linked order must be directly modifiable (`order_lifecycle.lock_and_guard`: window
  Reception through Diagnosis AND no review/approval/signing/publication evidence on any related
  report), checked under the order lock so a concurrent submission cannot slip past.
- New code: 1-100 characters of `A-Za-z0-9._-`. Manual corrections may reuse existing, issued or
  retired codes. Automatic numbering still skips occupied or retired codes and its counters and
  ledger are untouched by corrections.
- Audit: a `sample_code_correction` row (actor, reason, old and new code, time), a timeline event and
  an `audit_log` row, in the same transaction.
- Historical issued artifacts are never rewritten: published/signed reports and PDFs keep the code they
  were issued with (such an order is blocked by the lifecycle guard anyway). Editable (draft) report
  data reads the code from the sample at render time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException
from sqlmodel import Session, select

from app.models.enums import EventType
from app.models.events import OrderEvent
from app.models.laboratory import Sample
from app.models.order_management import SampleCodeCorrection
from app.services import numbering, order_lifecycle
from app.services.order_management import ManagementError
from app.services.sample_types import record_audit

CODE_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
REASON_LIMITS = (3, 1000)


def clean_reason(reason: str, *, what: str = "reason") -> str:
    reason = (reason or "").strip()
    lo, hi = REASON_LIMITS
    if not lo <= len(reason) <= hi:
        raise ManagementError(422, "reason_invalid", f"The {what} must be between {lo} and {hi} characters")
    return reason


def clean_code(code: str) -> str:
    code = (code or "").strip()
    if not CODE_RE.fullmatch(code):
        raise ManagementError(
            422, "sample_code_invalid",
            "A sample code is 1 to 100 characters: letters, digits, '.', '_' and '-' only",
        )
    return code


@dataclass
class CorrectionResult:
    sample: Sample
    old_code: str
    unchanged: bool


def correct_sample_code(session: Session, *, sample_id, tenant_id, user, new_code: str, reason: str) -> CorrectionResult:
    new_code = clean_code(new_code)
    reason = clean_reason(reason)
    sample = session.get(Sample, sample_id)
    if sample is None or str(sample.tenant_id) != str(tenant_id):
        raise HTTPException(404, "Sample not found")

    # Let the generator skip this candidate while the correction is in flight.
    # The code lock is taken (waiting) BEFORE the order lock. Generation only ever TRIES a
    # code lock, then takes the order's key-share lock for its inserts; because it never waits for a
    # code lock while we wait for the order, no cycle can form.
    numbering.lock_code(session, tenant_id, numbering.SAMPLE, new_code, wait=True)
    # Lifecycle/evidence guard under the order lock review, signing and removal share.
    # Céluma 1.4.4: sample scope, so a formally retracted report no longer locks the correction.
    order, eligibility = order_lifecycle.lock_and_guard_with_eligibility(
        session, sample.order_id, tenant_id, order_lifecycle.SCOPE_SAMPLE)
    sample = session.exec(
        select(Sample).where(Sample.id == sample_id, Sample.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    ).first()
    if sample is None:  # removed while we waited for the lock
        raise HTTPException(404, "Sample not found")

    old_code = sample.sample_code
    if new_code == old_code:
        session.rollback()  # nothing to write: release the locks
        return CorrectionResult(sample, old_code, True)

    sample.sample_code = new_code
    session.add(sample)
    now = datetime.utcnow()
    session.add(SampleCodeCorrection(
        tenant_id=sample.tenant_id, sample_id=sample.id, order_id=sample.order_id,
        old_code=old_code, new_code=new_code, reason=reason, corrected_by=user.id, corrected_at=now,
    ))
    session.add(OrderEvent(
        tenant_id=sample.tenant_id, branch_id=sample.branch_id, order_id=sample.order_id, sample_id=sample.id,
        event_type=EventType.SAMPLE_CODE_CORRECTED, description="",
        event_metadata={
            "sample_id": str(sample.id), "old_code": old_code, "new_code": new_code, "reason": reason,
            "order_code": order.order_code,
        },
        created_by=user.id,
    ))
    record_audit(
        session, tenant_id=sample.tenant_id, user_id=user.id, action="SAMPLE.CODE_CORRECTED",
        entity_type="sample", entity_id=sample.id, description="Sample code corrected",
        old_values={"sample_code": old_code}, new_values={"sample_code": new_code},
        metadata={"reason": reason, "order_id": str(sample.order_id),
                  **order_lifecycle.exception_metadata(eligibility)},
    )
    session.commit()
    session.refresh(sample)
    return CorrectionResult(sample, old_code, False)
