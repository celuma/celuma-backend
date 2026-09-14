"""Céluma 1.3.1 Block A / A4 — the narrow reviewer presentation mutation.

Block 0 established the constraint this module exists to work around:

  * the `reviewer` role has no `reports:edit`, and must not be given it —
    that would let a reviewer rewrite arbitrary clinical content and destroy
    the separation the review step exists to create;
  * yet signature settings (`signatureMetadata`) and the letterhead
    (`rendering_snapshot.presentation`) both live inside the report JSON body
    and were therefore only reachable through `POST /{id}/new_version`, which
    requires `reports:edit`;
  * and both affect the final clinical document, so they are reviewer
    decisions, not author decisions.

The resolution is an ALLOWLIST, not a wider permission. Exactly three fields
are writable here:

    show_signature_section
    require_digital_signature
    letterhead (by logical letterhead version id)

Everything else in the body — every clinical section, the template snapshot,
the base fields — is carried through byte-for-byte. There is deliberately no
generic "reviewer edits report" path: if a reviewer needs a clinical change,
the flow is to request changes and let the author make it.

**Why this rewrites the current version in place rather than creating a new
one.** `embed_signature_metadata_if_required` in `report_publishing.py`
already does exactly this — it is described there as "the one billable write
path that updates size_bytes on an existing StorageObject rather than
creating a new one" — and it does so for the same kind of change (signature
metadata) at the adjacent moment in the lifecycle (signing). Following that
precedent keeps one in-place-rewrite pattern in the codebase instead of two
competing ones, and avoids version churn from toggling a switch. Nothing
historical is touched: previous versions keep their own S3 objects, and the
operation is confined to IN_REVIEW, so no published PDF or released artifact
exists yet for this report.

The lifecycle window (IN_REVIEW only) is enforced by
`report_authorization.authorize_presentation_change`, not here.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from sqlmodel import Session

from app.models.report_letterhead_version import ReportLetterheadVersionStatus
from app.models.report import Report, ReportVersion
from app.models.storage import StorageObject
from app.models.user import AppUser
from app.services.letterhead_resolution import (
    LetterheadArchivedError,
    LetterheadConfigurationError,
    LetterheadNotFoundError,
    resolve_effective_letterhead_version,
)
from app.services.s3 import S3Service
from app.services.usage_thresholds import record_storage_delta_with_thresholds

logger = logging.getLogger(__name__)


class ReportPresentationError(Exception):
    """Base class. `message` is surfaced verbatim by the route."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class PresentationContentMissingError(ReportPresentationError):
    """The report version has no persisted JSON body to update. Routes map
    this to 409 — there is nothing to configure yet."""


class PresentationLetterheadError(ReportPresentationError):
    """The requested letterhead does not exist, belongs to another tenant, is
    archived, is deactivated, or is not the ACTIVE version. Routes map this to
    409 (or 404 for "not found"), matching `_apply_draft_letterhead_change`."""


class PresentationLetterheadNotFoundError(PresentationLetterheadError):
    """Specifically "no such letterhead version for this tenant" → 404. The
    resolver deliberately does not distinguish "does not exist" from "belongs
    to another laboratory", so a foreign id is never confirmed."""


# ---------------------------------------------------------------------------
# The author-side boundary (1.3.1 Block A, corrected A5/A6)
# ---------------------------------------------------------------------------
#
# The presentation settings are reviewer-only in EVERY state, not merely once
# the report leaves DRAFT. The first cut of Block A let the author keep them
# in DRAFT on the reasoning that DRAFT belongs to the author; the product
# contract is the opposite, and for a good reason — these three fields decide
# what the final clinical document asserts about who signed it and under whose
# letterhead, which is the reviewer's responsibility at every point in the
# lifecycle, not just after submission.
#
# The author still owns clinical CONTENT in DRAFT. What the functions below
# guarantee is that a save through the content path
# (`POST /reports/{id}/new_version`, `reports:edit`) cannot move a
# presentation field, whatever the request body says — including a direct API
# call that bypasses the editor entirely.

SIGNATURE_METADATA_KEY = "signatureMetadata"

#: The keys the author may never set. `signature_url` is deliberately in the
#: carried-forward set too: it is written at signing time by
#: `report_publishing.embed_signature_metadata_if_required` and a later draft
#: save must not strip or forge it.
_PRESENTATION_KEYS = (
    "show_signature_section",
    "require_digital_signature",
    "signature_url",
)


def signature_defaults_from_template(template: Optional[dict]) -> dict:
    """The template's signature defaults — the only thing that decides a new
    report's initial presentation. Mirrors the frontend's
    `resolveSignatureMetadata`, including its invariant that a digital
    signature cannot be required without the section that holds it."""
    raw = (template or {}).get(SIGNATURE_METADATA_KEY)
    if not isinstance(raw, dict):
        return {"show_signature_section": False, "require_digital_signature": False}
    show = bool(raw.get("show_signature_section", False))
    return {
        "show_signature_section": show,
        "require_digital_signature": show
        and bool(raw.get("require_digital_signature", False)),
    }


def _stored_signature_metadata(
    session: Session, version: Optional[ReportVersion]
) -> Optional[dict]:
    """The signature metadata actually persisted for a version, or None when
    the version has no stored body (or it cannot be read)."""
    if version is None or version.json_storage_id is None:
        return None
    storage = session.get(StorageObject, version.json_storage_id)
    if storage is None:
        return None
    try:
        stored = json.loads(S3Service().download_text(storage.object_key))
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not read the persisted signature metadata while creating a "
            "new content version; falling back to template defaults",
            extra={
                "event": "report.presentation_carry_forward_failed",
                "report_version_id": str(version.id),
            },
        )
        return None
    if not isinstance(stored, dict):
        return None
    raw = stored.get(SIGNATURE_METADATA_KEY)
    return raw if isinstance(raw, dict) else None


def enforce_author_presentation_boundary(
    session: Session,
    body: Optional[dict],
    *,
    current_version: Optional[ReportVersion] = None,
    template: Optional[dict] = None,
) -> Optional[dict]:
    """Return `body` with its presentation fields replaced by the authoritative
    ones, discarding whatever the caller submitted.

    Carry-forward, not rejection: the editor legitimately round-trips the whole
    document on every save, so a 403 on "the body contains signatureMetadata"
    would break ordinary authoring. Overwriting makes the field unreachable
    through this path by construction, which is the property that matters —
    and it holds for direct API calls just as much as for the editor.

    Authority order: what is already persisted for this report, else the
    template's defaults.
    """
    if body is None:
        return None

    stored = _stored_signature_metadata(session, current_version)
    if stored is None:
        authoritative = signature_defaults_from_template(template)
    else:
        authoritative = {k: stored[k] for k in _PRESENTATION_KEYS if k in stored}

    result = dict(body)
    result[SIGNATURE_METADATA_KEY] = authoritative
    return result


def author_requested_letterhead_change(
    requested_letterhead_version_id: Optional[str],
    current_version: Optional[ReportVersion],
) -> bool:
    """Whether a content-path save is trying to move the letterhead.

    Unlike the signature fields this one IS rejected rather than ignored: the
    letterhead is chosen explicitly and visibly, so silently keeping the old
    one would leave the caller believing a change landed. See
    `create_report_new_version`.
    """
    if requested_letterhead_version_id is None:
        return False
    current = (
        str(current_version.letterhead_version_id)
        if current_version is not None and current_version.letterhead_version_id
        else None
    )
    return str(requested_letterhead_version_id) != str(current or "")


def _resolve_letterhead(session: Session, tenant_id: str, letterhead_version_id: str):
    """Same validation chain `_apply_draft_letterhead_change` applies for a
    DRAFT save — kept identical on purpose so a reviewer cannot select a
    letterhead an author could not."""
    try:
        resolved = resolve_effective_letterhead_version(
            session, tenant_id, letterhead_version_id=letterhead_version_id
        )
    except LetterheadNotFoundError as exc:
        raise PresentationLetterheadNotFoundError(exc.message) from None
    except (LetterheadArchivedError, LetterheadConfigurationError) as exc:
        raise PresentationLetterheadError(exc.message) from None

    if resolved is None:
        raise PresentationLetterheadNotFoundError("Letterhead version not found")

    if not resolved.letterhead.is_active:
        raise PresentationLetterheadError(
            f"El membrete «{resolved.letterhead.name}» está desactivado y no "
            "puede asignarse a un reporte."
        )
    if resolved.version.status != ReportLetterheadVersionStatus.ACTIVE:
        raise PresentationLetterheadError(
            "Solo puede asignarse la versión activa de un membrete "
            f"(la versión indicada está en estado {resolved.version.status})."
        )
    return resolved


def apply_presentation_change(
    session: Session,
    report: Report,
    version: ReportVersion,
    user: AppUser,
    *,
    show_signature_section: Optional[bool] = None,
    require_digital_signature: Optional[bool] = None,
    letterhead_version_id: Optional[str] = None,
) -> dict:
    """Apply the allowlisted presentation fields to the current version's
    persisted JSON. Every argument is optional; `None` means "not submitted",
    so a caller may change one toggle without restating the others.

    Returns the effective settings after the change, for the response body.
    The caller commits.
    """
    if version.json_storage_id is None:
        raise PresentationContentMissingError(
            "This report version has no saved content yet; there is nothing "
            "to configure."
        )
    json_storage = session.get(StorageObject, version.json_storage_id)
    if json_storage is None:
        raise PresentationContentMissingError(
            "This report version's saved content is missing."
        )

    old_size_bytes = json_storage.size_bytes or 0
    s3 = S3Service()
    try:
        report_doc = json.loads(s3.download_text(json_storage.object_key))
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Failed to load report JSON while updating presentation settings",
            extra={
                "event": "report.presentation_json_load_failed",
                "report_id": str(report.id),
                "object_key": json_storage.object_key,
                "error": str(exc),
            },
        )
        raise PresentationContentMissingError(
            "Failed to load report content."
        ) from exc

    if not isinstance(report_doc, dict):
        raise PresentationContentMissingError(
            "Report content is not in the expected format."
        )

    # --- signature settings (allowlisted) ---------------------------------
    signature_meta = dict(report_doc.get("signatureMetadata") or {})
    if show_signature_section is not None:
        signature_meta["show_signature_section"] = show_signature_section
    if require_digital_signature is not None:
        signature_meta["require_digital_signature"] = require_digital_signature

    # The editor's own invariant, enforced server-side so a direct API call
    # cannot persist "digital signature on, signature section off" — a state
    # the renderer has no place to draw.
    if not signature_meta.get("show_signature_section", False):
        signature_meta["require_digital_signature"] = False

    report_doc["signatureMetadata"] = signature_meta

    # --- letterhead (allowlisted) -----------------------------------------
    # The letterhead link lives on the VERSION, not the report — it is the
    # audit twin of `template_version_id` and records which letterhead
    # version produced this version's `presentation` block.
    applied_letterhead_version_id = (
        str(version.letterhead_version_id) if version.letterhead_version_id else None
    )
    if letterhead_version_id is not None and str(letterhead_version_id) != str(
        applied_letterhead_version_id or ""
    ):
        resolved = _resolve_letterhead(
            session, str(report.tenant_id), letterhead_version_id
        )
        snapshot = report_doc.get("rendering_snapshot")
        if isinstance(snapshot, dict):
            # Surgical replacement of `presentation` only. `template` is
            # preserved byte-for-byte: the letterhead is presentation, the
            # clinical template is something else entirely.
            new_snapshot = dict(snapshot)
            new_snapshot["presentation"] = resolved.presentation.model_dump(
                mode="json"
            )
            report_doc["rendering_snapshot"] = new_snapshot
        version.letterhead_version_id = resolved.version.id
        session.add(version)
        applied_letterhead_version_id = str(resolved.version.id)

        logger.info(
            "Reviewer changed report letterhead",
            extra={
                "event": "report.reviewer_letterhead_changed",
                "report_id": str(report.id),
                "new_letterhead_version_id": applied_letterhead_version_id,
                "user_id": str(user.id),
            },
        )

    # --- persist (in-place rewrite of the same object key) ----------------
    updated_bytes = json.dumps(report_doc, ensure_ascii=False).encode("utf-8")
    info = s3.upload_bytes(
        updated_bytes, key=json_storage.object_key, content_type="application/json"
    )
    json_storage.etag = info.etag
    json_storage.size_bytes = info.size_bytes
    json_storage.version_id = info.version_id
    session.add(json_storage)

    record_storage_delta_with_thresholds(
        session,
        report.tenant_id,
        (info.size_bytes or 0) - old_size_bytes,
        source="report_json",
        resource_type="report_json",
        actor_id=user.id,
    )

    return {
        "show_signature_section": bool(
            signature_meta.get("show_signature_section", False)
        ),
        "require_digital_signature": bool(
            signature_meta.get("require_digital_signature", False)
        ),
        "letterhead_version_id": applied_letterhead_version_id,
    }
