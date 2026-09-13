"""Céluma 1.3.1 Block A — the SINGLE clinical reviewer authorization contract.

Before 1.3.1 the approval guard was written inline in each route as a
disjunction:

    assigned reviewer  OR  has `reports:approve`

Because the `v1_0_0` seed granted `reports:approve` to `pathologist`, the
second branch admitted any pathologist in the tenant — and, because
`superuser` holds every permission in the catalogue, it admitted superuser
too. Block 0 reproduced all three consequences (non-reviewer pathologist
approves, unassigned reviewer approves, superuser approves). See
CELUMA-1.3.1-HOTFIX-PLAN.md, "Block 0 — Result", §4.

The corrected contract is a CONJUNCTION — the "double lock" plus contextual
ownership:

    ROLE_REVIEWER
      AND the reviewer capability for the action (`reports:approve` /
          `reports:sign`)
      AND assigned as reviewer for the report's order (`ReportReview`)
      AND same tenant
      AND a lifecycle state that permits the action

Two design notes that are easy to get wrong later:

*   **The permission check alone is not the fix.** 1.3.1 also removes
    `reports:approve` from `pathologist` (migration `v1_3_1`), but that
    migration is a data change to a globally-seeded row and a future
    migration could re-grant it. The role and assignment checks here are what
    make a broad permission grant non-exploitable, so never "simplify" this
    module back down to a permission test.

*   **Roles are additive.** A user may legitimately hold `reviewer` alongside
    `pathologist` or `admin` (`get_user_permissions` unions across roles), so
    every check below asks "does this user have what the reviewer contract
    requires", never "is this user ONLY a reviewer". Holding another role is
    never itself a reason to reject.

Admin and superuser are deliberately NOT admitted by any clinical function
here. They hold a separate, narrower administrative capability, added in
Block B: reopening an approved-but-unsigned report — see
`can_reopen_approved_report` and `authorize_reopen` at the bottom of this
module. That capability grants reopening and nothing else.

Domain errors are raised, not `HTTPException`: the API layer translates them,
matching `report_publishing.py` and `letterhead_resolution.py`.
"""
from __future__ import annotations

from typing import Optional

from sqlmodel import Session, and_, or_, select

from app.core.rbac import ROLE_REVIEWER, has_any_role, has_permission
from app.models.enums import ReportStatus, ReviewStatus
from app.models.report import Report, ReportVersion
from app.models.report_review import ReportReview
from app.models.user import AppUser


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------

class ReportAuthorizationError(Exception):
    """Base class. Carries `message` so routes can surface it verbatim."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotAReviewerError(ReportAuthorizationError):
    """The user does not hold `ROLE_REVIEWER`, or lacks the reviewer
    capability the action requires. Routes map this to 403.

    Deliberately one error for both halves of the double lock: telling a
    caller *which* half they failed distinguishes "you are not a reviewer"
    from "you are a reviewer without the capability", and that difference is
    of no use to a legitimate user while being mildly useful to an attacker
    probing the role model."""


class NotTheAssignedReviewerError(ReportAuthorizationError):
    """The user satisfies the double lock but holds no PENDING `ReportReview`
    for this report's order. Routes map this to 403."""


class ReportStateError(ReportAuthorizationError):
    """The report's lifecycle state does not permit the action. Routes map
    this to 400 (approval/signing) or 409 (presentation settings), matching
    what each route already returned before 1.3.1.

    **Raised only AFTER authorization has passed.** Every contract below
    checks role, capability and assignment first and the lifecycle state
    last, so an unauthorized caller always gets 403 and never learns the
    report's status from the error code. Reversing that order would turn
    these endpoints into a state oracle for anyone in the tenant."""


class ReportAlreadySignedError(ReportAuthorizationError):
    """Céluma 1.3.1 Block B — the report carries signature evidence, so it is
    final. Routes map this to 409.

    Deliberately distinct from `ReportStateError`. `Report.status` alone does
    not say whether a report was signed: signature state lives on
    `ReportVersion` (`signed_at` / `signed_by`) and publication adds
    `Report.published_at`. A report in the wrong *state* for an action may
    reach the right state later; a signed report never becomes reopenable in
    1.3.1 at all — amending it belongs to the Céluma 1.4 amendment
    workflow."""


# ---------------------------------------------------------------------------
# The individual locks
# ---------------------------------------------------------------------------

def is_reviewer(user_id, session: Session) -> bool:
    """Lock 1 — the canonical reviewer role (`reviewer`, seeded by v1_1_0)."""
    return has_any_role(user_id, {ROLE_REVIEWER}, session)


def find_pending_review(
    session: Session, report: Report, user_id
) -> Optional[ReportReview]:
    """Lock 3 — contextual ownership.

    Assignment is NOT a column on `report`: it is a `ReportReview` row, and
    it is scoped by `order_id`, not `report_id` (`report_id` is nullable
    because reviewers may be assigned before a report exists). Matching by
    order is therefore correct and is what the pre-1.3.1 code already did.

    Only a PENDING row counts. A reviewer who already decided must not be
    able to re-decide silently through the report route; that is what
    `POST /worklist/report-reviews/{id}/decision` is for, which handles
    changing an existing decision explicitly.
    """
    return session.exec(
        select(ReportReview).where(
            and_(
                ReportReview.tenant_id == report.tenant_id,
                ReportReview.order_id == report.order_id,
                ReportReview.reviewer_user_id == user_id,
                ReportReview.status == ReviewStatus.PENDING,
            )
        )
    ).first()


def find_any_review(
    session: Session, report: Report, user_id
) -> Optional[ReportReview]:
    """Lock 3, ignoring the decision status.

    Used by the actions that happen AFTER a decision has been recorded —
    signing (the report is APPROVED, so the reviewer's own row is APPROVED,
    not PENDING) and the presentation route (which must be able to tell a
    genuinely assigned reviewer "this is frozen now" rather than "you are not
    the reviewer"). Assignment, not the decision, is the ownership fact.
    """
    return session.exec(
        select(ReportReview).where(
            and_(
                ReportReview.tenant_id == report.tenant_id,
                ReportReview.order_id == report.order_id,
                ReportReview.reviewer_user_id == user_id,
            )
        )
    ).first()


def is_assigned_reviewer(session: Session, report: Report, user_id) -> bool:
    """Lock 3 as a predicate — for read-only callers (e.g. exposing
    `can_*` flags to the UI) that must not raise."""
    return find_any_review(session, report, user_id) is not None


# ---------------------------------------------------------------------------
# The composed contracts
# ---------------------------------------------------------------------------

def _require_reviewer_contract(
    session: Session,
    report: Report,
    user: AppUser,
    capability: str,
    *,
    require_pending: bool = True,
) -> ReportReview:
    """Locks 1-3. Tenant (lock 4) is enforced by the caller, which already
    loads the report under the tenant anchor and must answer 404 — not 403 —
    for another laboratory's id, so that a foreign id is never confirmed
    (`authorize_report_read_access`).

    `require_pending` distinguishes the two kinds of action: recording a
    decision needs an undecided row to write to, while acting on a report
    whose decision already exists (signing, presentation) only needs the
    assignment itself.
    """
    if not is_reviewer(user.id, session) or not has_permission(
        user.id, capability, session
    ):
        raise NotAReviewerError(
            f"Only the assigned reviewer can perform this action "
            f"(requires the '{ROLE_REVIEWER}' role and '{capability}')"
        )

    review = (
        find_pending_review(session, report, user.id)
        if require_pending
        else find_any_review(session, report, user.id)
    )
    if review is None:
        raise NotTheAssignedReviewerError(
            "You are not the assigned reviewer for this report"
        )
    return review


def authorize_approval(
    session: Session, report: Report, user: AppUser
) -> ReportReview:
    """CEL-131-01. Returns the caller's PENDING `ReportReview` so the route
    can record the decision on it without querying twice.

    Replaces `assigned reviewer OR reports:approve`.
    """
    review = _require_reviewer_contract(session, report, user, "reports:approve")
    if report.status != ReportStatus.IN_REVIEW:
        raise ReportStateError(
            f"Cannot approve report in {report.status} status"
        )
    return review


def authorize_request_changes(
    session: Session, report: Report, user: AppUser
) -> ReportReview:
    """The rejection half of the review decision. It carried the identical
    disjunction bug as approval (both branches of one decision must share one
    contract, or "request changes" becomes the unguarded way to move a report
    out of IN_REVIEW)."""
    review = _require_reviewer_contract(session, report, user, "reports:approve")
    if report.status != ReportStatus.IN_REVIEW:
        raise ReportStateError(
            f"Cannot request changes for report in {report.status} status"
        )
    return review


def authorize_signing(
    session: Session, report: Report, user: AppUser
) -> ReportReview:
    """CEL-131-02 / A3.

    The signing routes already enforced role + `reports:sign` before 1.3.1
    (which is why superuser was already rejected there). What they did NOT
    enforce was assignment: any reviewer in the tenant could sign any
    approved report, including one they had never been assigned to. This
    closes that gap while preserving every other signing invariant.

    Note the report is APPROVED by the time signing runs, so the reviewer's
    own `ReportReview` row is APPROVED, not PENDING — `find_pending_review`
    would find nothing. Assignment for signing therefore matches on the
    order regardless of decision status.
    """
    review = _require_reviewer_contract(
        session, report, user, "reports:sign", require_pending=False
    )

    if report.status != ReportStatus.APPROVED:
        raise ReportStateError(
            f"Cannot sign report in {report.status} status. "
            "Report must be approved first."
        )
    return review


# ---------------------------------------------------------------------------
# Reviewer presentation settings (A4/A5)
# ---------------------------------------------------------------------------

#: The report states in which the ASSIGNED REVIEWER may change presentation
#: settings.
#:
#: Céluma 1.3.1 manual-validation remediation (R1, CEL-131-02): DRAFT was
#: added here. Block A admitted only IN_REVIEW, reasoning that "in DRAFT the
#: pathologist owns the document". Real use showed that is the wrong split:
#: the letterhead and the two signature toggles are reviewer-owned in EVERY
#: state (Block A's own §7 says so), and refusing them until submission just
#: forced the reviewer to wait for a round trip they had no part in. The
#: release owner's decision is that an authorized assigned reviewer configures
#: presentation in DRAFT and IN_REVIEW alike.
#:
#: What did NOT change, and must not: this tuple governs
#: `authorize_presentation_change` ONLY. Approval
#: (`APPROVAL_EDITABLE_STATUSES`) and signing (`APPROVED`) keep their own
#: states, so nothing here lets a report be approved or signed from DRAFT —
#: see `authorize_approval` and `authorize_signing`, which read neither this
#: tuple nor each other's.
#:
#: Still deliberately not APPROVED: an approved report's presentation is
#: frozen, and the 1.3.1 route back is Block B's reopen, not a silent mutation
#: of an approved document. PUBLISHED/RETRACTED likewise.
PRESENTATION_EDITABLE_STATUSES = (ReportStatus.DRAFT, ReportStatus.IN_REVIEW)


def authorize_presentation_change(
    session: Session, report: Report, user: AppUser
) -> ReportReview:
    """A4/A5 — the narrow reviewer-only presentation contract.

    Block 0 established the constraint this works around: the `reviewer` role
    has no `reports:edit`, and signature/letterhead settings ride on the
    report-content path that requires it. Granting `reports:edit` to reviewer
    was rejected outright — it would let a reviewer rewrite arbitrary clinical
    content, which is a far larger authorization change than this hotfix
    intends and would defeat the separation the review step exists to create.

    So the capability reused here is `reports:approve` — the reviewer's
    existing authority over the report *as a reviewer* — and the mutation
    surface is allowlisted to three presentation fields by the caller
    (`ReportPresentationUpdate`). There is deliberately no generic
    "reviewer edits report" path.
    """
    review = _require_reviewer_contract(
        session, report, user, "reports:approve", require_pending=False
    )
    if report.status not in PRESENTATION_EDITABLE_STATUSES:
        raise ReportStateError(
            "Report presentation settings can only be changed while the "
            "report is a draft or in review (current status: "
            f"{report.status})."
        )
    return review


# ---------------------------------------------------------------------------
# Reopen authorization (A8) — the policy, shared by Block B's transition
# ---------------------------------------------------------------------------

def can_reopen_approved_report(
    session: Session, report: Report, user: AppUser
) -> bool:
    """A8 — the authorization policy Block B must reuse for

        APPROVED + UNSIGNED  ->  DRAFT

    The transition itself is `authorize_reopen` below (Block B), which calls
    this predicate and adds the lifecycle and signature guards. This stays a
    non-raising predicate so read-only callers — exposing a `can_reopen` flag
    to the UI, for instance — can ask the same question without catching
    exceptions.

    Authorized:
      * the assigned reviewer (via the reviewer contract);
      * `admin` / `superuser`, through the ADMINISTRATIVE capability
        `admin:manage_catalog`-adjacent report management — expressed here as
        `reports:manage_templates`, which in the current seed is held by
        exactly `admin` and `superuser` and by no clinical role.

    NOT authorized: a non-reviewer pathologist. `pathologist` holds neither
    `reports:manage_templates` nor (after v1_3_1) `reports:approve`.

    **The administrative half grants reopening ONLY.** It must never be
    accepted by `authorize_approval`, `authorize_signing`, or
    `authorize_presentation_change`. Admin regaining clinical authority
    through a reopen capability is precisely the bypass CEL-131-01 exists to
    remove, so Block B must call this predicate and nothing wider.

    Signed/closed reports are out of scope for 1.3.1 entirely (Céluma 1.4
    amendment workflow); the signature guard lives in `authorize_reopen`, not
    in this authorization predicate — being *allowed* to reopen and there
    being something reopenable are different questions.
    """
    if is_reviewer(user.id, session) and has_permission(
        user.id, "reports:approve", session
    ):
        if is_assigned_reviewer(session, report, user.id):
            return True
    return has_permission(user.id, "reports:manage_templates", session)


# ---------------------------------------------------------------------------
# Block B — the APPROVED → DRAFT transition (CEL-131-03)
# ---------------------------------------------------------------------------

#: The ONLY report state a reopen may start from. A1/A8 scope CEL-131-03 to
#: `APPROVED + UNSIGNED`; every other state is refused, including the two
#: immutable ones (PUBLISHED / RETRACTED) and the two that are already
#: editable or under review (DRAFT / IN_REVIEW), for which reopening is
#: meaningless rather than merely unauthorized.
REOPENABLE_REPORT_STATUSES = (ReportStatus.APPROVED,)


def find_signature_evidence(
    session: Session, report: Report
) -> Optional[ReportVersion]:
    """Any version of this report that carries a signature.

    Block B must NOT decide "is this report signed?" from `Report.status`.
    Status and signature are different facts stored in different places:
    signing writes `signed_by` / `signed_at` on the `ReportVersion` and only
    then moves the report to PUBLISHED, so a crash or a future code path
    between those two writes would leave a signed report still reading
    APPROVED. Asking the versions directly is the evidence the domain model
    actually offers.

    Every version is examined, not only the current one: a reopen must never
    resurrect a report any part of whose history was signed.
    """
    return session.exec(
        select(ReportVersion).where(
            and_(
                ReportVersion.report_id == report.id,
                or_(
                    ReportVersion.signed_at.is_not(None),
                    ReportVersion.signed_by.is_not(None),
                ),
            )
        )
    ).first()


def authorize_reopen(
    session: Session, report: Report, user: AppUser
) -> ReportVersion:
    """CEL-131-03 — the full guard for `APPROVED + UNSIGNED → DRAFT`.

    Returns the report's current `ReportVersion`, which the caller needs
    anyway and which this function has already proved is unsigned.

    The authorization half is `can_reopen_approved_report` and nothing wider
    (A8): the assigned reviewer through the clinical contract, `admin` and
    `superuser` through `reports:manage_templates`. That administrative half
    grants reopening ONLY — it must never be accepted by `authorize_approval`,
    `authorize_signing` or `authorize_presentation_change`.

    Order matters, and follows the rule the rest of this module already
    states: authorization first, lifecycle second. An unauthorized caller is
    refused before any state is examined, so they cannot use the error code to
    learn whether a report is approved, signed or published.
    """
    if not can_reopen_approved_report(session, report, user):
        raise NotAReviewerError(
            "Only the assigned reviewer or an administrator can reopen this report"
        )

    if report.status not in REOPENABLE_REPORT_STATUSES:
        raise ReportStateError(
            f"Cannot reopen report in {report.status} status. "
            "Only an approved report that has not been signed can be reopened."
        )

    # Checked even though APPROVED implies neither: `published_at` is the
    # report-level record of a publication, and a reopen must be refused on
    # any finalisation evidence, not only on the one the happy path writes.
    if find_signature_evidence(session, report) is not None or (
        report.published_at is not None
    ):
        raise ReportAlreadySignedError(
            "This report has already been signed and cannot be reopened. "
            "Correcting a signed report is not supported in this version."
        )

    current_version = session.exec(
        select(ReportVersion).where(
            and_(
                ReportVersion.report_id == report.id,
                ReportVersion.is_current == True,  # noqa: E712
            )
        )
    ).first()
    if current_version is None:
        raise ReportStateError(
            "This report has no current version and cannot be reopened"
        )
    return current_version


# ---------------------------------------------------------------------------
# Block B remediation (B-3) — the author's clinical content path
# ---------------------------------------------------------------------------

#: The ONLY states in which a new clinical content version may be authored
#: (`POST /reports/{id}/new_version`).
#:
#: Expressed as an ALLOWLIST, not as a list of forbidden states. A future
#: lifecycle state is then refused by default and has to be admitted
#: deliberately, which is the right direction to fail for a guard protecting
#: approved clinical content.
#:
#: **Why APPROVED is not here.** Céluma 1.3 Phase 2, Block B (Story B9) froze
#: content only for PUBLISHED and RETRACTED, leaving DRAFT / IN_REVIEW /
#: APPROVED "fully editable" (phase-2-block-b-implementation-summary.md §B9).
#: That was coherent while approval was the last step before signing and there
#: was no way back from it. Block B gave the lifecycle an explicit reverse gear
#: (`POST /{id}/reopen`), and at that point leaving APPROVED editable became a
#: hole straight through it: a holder of `reports:edit` could replace the
#: clinical content of an approved report and have it signed without the new
#: content ever being reviewed — the approval would attest to text that no
#: longer existed. The reopen route is now the only way back, and it costs a
#: fresh review and a fresh approval, which is the point.
#:
#: **Why IN_REVIEW stays.** Nothing has been approved yet, so an edit there
#: bypasses no decision: the reviewer's pending approval still applies to
#: whatever the content is when they make it. Removing it would also break the
#: documented letterhead rule that an IN_REVIEW content save is refused
#: specifically for carrying a letterhead (403), not for existing at all.
CONTENT_EDITABLE_STATUSES = (ReportStatus.DRAFT, ReportStatus.IN_REVIEW)


def is_content_editable(report: Report) -> bool:
    """Whether a new clinical content version may be authored for `report`.

    A pure lifecycle predicate — it says nothing about WHO may author it.
    `reports:edit` and the tenant anchor are checked by the caller, before
    this, and neither is replaced by it.
    """
    return report.status in CONTENT_EDITABLE_STATUSES
