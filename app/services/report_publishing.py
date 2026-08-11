"""Second post-Phase-2 remediation (UX): "Sign and publish" as a single
product action — see signed-pdf-publication-workflow.md.

Before this remediation, "Generate official PDF" and "Sign and Publish"
were two buttons/two separate network calls, and the digital signature
(when the report requires it) was embedded in the JSON AFTER the PDF had
already been generated — i.e. the "official" PDF never reflected the
truly signed state. This module fixes both problems:

1. `embed_signature_metadata_if_required` runs BEFORE generating the PDF
   (not after), so the signature is part of the rendered content.
2. `claim_publish`/`clear_publish_claim`/`finalize_publish` implement a
   light claim (`publish_started_at`/`publish_started_by` columns on
   `ReportVersion`) that serializes concurrent sign-and-publish attempts
   without holding a DB row-lock during slow browser-driven generation
   (Playwright/Chromium) — same staleness pattern
   `ReportPdfGenerationService` already uses for
   `pdf_generation_status == GENERATING`.

`sign_report` (`POST /{report_id}/sign`) and `generate-pdf`
(`POST .../generate-pdf`) are not removed: they remain for compatibility
and internal use, but the main UI flow now exclusively uses
`POST /{report_id}/sign-and-publish`, which orchestrates both.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from sqlmodel import Session

from app.core.config import settings
from app.models.enums import ReportStatus
from app.models.report import Report, ReportVersion
from app.models.storage import StorageObject
from app.models.user import AppUser
from app.schemas.report import SignatureMetadata
from app.services.s3 import S3Service
from app.services.usage import UsageService

logger = logging.getLogger(__name__)


class ReportPublishError(Exception):
    """Carries a sanitized (code, message) pair — never clinical content."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class ReportPublishAlreadyInProgressError(ReportPublishError):
    pass


class ReportPublishConflictError(ReportPublishError):
    """Re-verification at finalize time found the report/version state
    changed concurrently (e.g. already published by another request, or
    generation somehow left the version not READY)."""

    pass


def embed_signature_metadata_if_required(
    session: Session, report_id: str, current_version: ReportVersion, user: AppUser
) -> None:
    """If the report requires a digital signature, embed the user's public
    signature URL in `signatureMetadata` inside the persisted JSON —
    BEFORE the official PDF is generated, so the resulting PDF reflects
    the already-signed state. No-op if `require_digital_signature` is
    false or the report has no persisted JSON.

    Extracted verbatim from the logic that previously lived only in
    `sign_report` (where it ran AFTER generating the PDF — the conceptual
    bug this remediation fixes). Both endpoints (`sign_report` and
    `sign_and_publish_report`) reuse it.
    """
    if current_version.json_storage_id is None:
        return
    json_storage = session.get(StorageObject, current_version.json_storage_id)
    if json_storage is None:
        return
    # Céluma 1.3 Phase 4, Block C: captured before the in-place rewrite
    # below mutates this same row — this is the one billable write path
    # that updates size_bytes on an existing StorageObject rather than
    # creating a new one (see storage-flow-accounting-matrix.md "report
    # JSON in-place rewrite").
    old_size_bytes = json_storage.size_bytes or 0

    s3 = S3Service()
    try:
        raw_json = s3.download_text(json_storage.object_key)
        report_doc = json.loads(raw_json)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Failed to load report JSON from S3 while signing",
            extra={
                "event": "report.sign_json_load_failed",
                "report_id": report_id,
                "object_key": json_storage.object_key,
                "error": str(exc),
            },
        )
        raise ReportPublishError(
            "JSON_LOAD_FAILED", "Failed to load report content for signing"
        ) from exc

    metadata_dict = report_doc.get("signatureMetadata") or {}
    try:
        signature_meta = SignatureMetadata.model_validate(metadata_dict)
    except Exception:
        # Tolerate legacy or malformed metadata: fall back to defaults.
        signature_meta = SignatureMetadata()

    if not signature_meta.require_digital_signature:
        return

    if user.signature_storage_id is None:
        raise ReportPublishError(
            "SIGNATURE_MISSING",
            "Cannot sign: the report requires a digital signature image but the signer has no signature uploaded",
        )
    sig_storage = session.get(StorageObject, user.signature_storage_id)
    if sig_storage is None:
        raise ReportPublishError(
            "SIGNATURE_STORAGE_MISSING", "Cannot sign: signer's signature storage object is missing"
        )
    # Public CDN URL (same pattern as avatars, sample images and
    # /users/me/signature) — presigned S3 URLs would fail in the browser
    # when the bucket is fronted by CloudFront with public access blocked
    # at the S3 level.
    signature_url = s3.object_public_url(sig_storage.object_key)
    report_doc["signatureMetadata"] = {
        **metadata_dict,
        "show_signature_section": True,
        "require_digital_signature": True,
        "signature_url": signature_url,
    }

    updated_bytes = json.dumps(report_doc, ensure_ascii=False).encode("utf-8")
    info = s3.upload_bytes(updated_bytes, key=json_storage.object_key, content_type="application/json")
    json_storage.etag = info.etag
    json_storage.size_bytes = info.size_bytes
    json_storage.version_id = info.version_id
    session.add(json_storage)

    tenant_id = json_storage.tenant_id
    if tenant_id is None:
        report = session.get(Report, report_id)
        tenant_id = report.tenant_id if report else None
    if tenant_id is not None:
        delta = (info.size_bytes or 0) - old_size_bytes
        UsageService.record_storage_delta(
            session, tenant_id, delta, source="report_json", resource_type="report_json"
        )

    session.commit()


def claim_publish(session: Session, version: ReportVersion, actor_id: UUID) -> None:
    """Atomically claims the right to sign-and-publish this version. Caller
    must hold the row lock (`load_locked_version`) when calling this —
    mirrors `ReportPdfGenerationService`'s GENERATING claim exactly,
    committing immediately so the row lock releases before the slow work
    starts. Raises `ReportPublishAlreadyInProgressError` if a fresh claim
    already exists; a stale (crashed) claim is silently overwritten and
    retried, same staleness policy as PDF generation.
    """
    now = datetime.utcnow()
    if version.publish_started_at is not None:
        stale_after = timedelta(seconds=settings.report_publish_timeout_seconds * 3)
        if (now - version.publish_started_at) < stale_after:
            raise ReportPublishAlreadyInProgressError(
                "PUBLISH_IN_PROGRESS",
                "A sign-and-publish attempt is already running for this report version",
            )
    version.publish_started_at = now
    version.publish_started_by = actor_id
    session.add(version)
    session.commit()
    session.refresh(version)


def clear_publish_claim(session: Session, version: ReportVersion) -> None:
    """Releases the claim without touching signed_by/signed_at/status —
    used on any failure path so the report stays retryable."""
    version.publish_started_at = None
    version.publish_started_by = None
    session.add(version)
    session.commit()
    session.refresh(version)


def finalize_publish(
    session: Session,
    report: Report,
    version: ReportVersion,
    actor_id: UUID,
    changelog: Optional[str],
) -> None:
    """Re-verifies state and stages the atomic sign+publish (does NOT
    commit — the caller adds its audit log / timeline event in the same
    transaction and commits once). Caller must hold the row lock
    (re-fetched via `load_locked_version` after generation) when calling
    this, since generation itself does not hold one across its own work.

    Raises `ReportPublishConflictError` if the report is no longer
    APPROVED or the version isn't READY — guarantees `signed_by`/
    `signed_at`/`PUBLISHED` are only ever set together, and never when a
    concurrent request already won the race.
    """
    if report.status != ReportStatus.APPROVED:
        raise ReportPublishConflictError(
            "ALREADY_PUBLISHED_OR_CHANGED",
            "Report is no longer awaiting publication (concurrent update)",
        )
    if version.pdf_generation_status != "READY":
        raise ReportPublishConflictError(
            "PDF_NOT_READY", "PDF generation did not complete successfully"
        )

    now = datetime.utcnow()
    version.signed_by = actor_id
    version.signed_at = now
    if changelog:
        version.changelog = changelog
    version.publish_started_at = None
    version.publish_started_by = None

    report.status = ReportStatus.PUBLISHED
    report.published_at = now
    session.add(version)
    session.add(report)
