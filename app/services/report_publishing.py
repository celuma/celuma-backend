"""Segunda remediación post-Fase 2 (UX): "Firmar y publicar" como una sola
acción de producto — ver signed-pdf-publication-workflow.md.

Antes de esta remediación, "Generar PDF oficial" y "Firmar y Publicar" eran
dos botones/dos llamadas de red separadas, y la firma digital (cuando el
reporte la requiere) se embebía en el JSON DESPUÉS de que el PDF ya se
hubiera generado — es decir, el PDF "oficial" nunca reflejaba el estado
realmente firmado. Este módulo corrige ambos problemas:

1. `embed_signature_metadata_if_required` se ejecuta ANTES de generar el
   PDF (no después), para que la firma sea parte del contenido renderizado.
2. `claim_publish`/`clear_publish_claim`/`finalize_publish` implementan un
   claim ligero (columnas `publish_started_at`/`publish_started_by` en
   `ReportVersion`) que serializa intentos concurrentes de firmar-y-publicar
   sin mantener un row-lock de base de datos durante la generación lenta y
   dirigida por navegador (Playwright/Chromium) — mismo patrón de
   staleness que `ReportPdfGenerationService` ya usa para
   `pdf_generation_status == GENERATING`.

No se elimina `sign_report` (`POST /{report_id}/sign`) ni `generate-pdf`
(`POST .../generate-pdf`): siguen existiendo para compatibilidad y uso
interno, pero el flujo principal de la UI pasa a usar exclusivamente
`POST /{report_id}/sign-and-publish`, que orquesta ambos.
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
    """Si el reporte requiere firma digital, embebe la URL pública de la
    firma del usuario en `signatureMetadata` dentro del JSON persistido —
    ANTES de que se genere el PDF oficial, para que el PDF resultante
    refleje el estado ya firmado. No-op si `require_digital_signature` es
    falso o si el reporte no tiene JSON persistido.

    Extraído verbatim de la lógica que antes vivía únicamente en
    `sign_report` (donde corría DESPUÉS de generar el PDF — el bug
    conceptual que esta remediación corrige). Ambos endpoints
    (`sign_report` y `sign_and_publish_report`) lo reutilizan.
    """
    if current_version.json_storage_id is None:
        return
    json_storage = session.get(StorageObject, current_version.json_storage_id)
    if json_storage is None:
        return

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
