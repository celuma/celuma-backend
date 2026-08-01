"""Céluma 1.3 Fase 2, Bloque E: official PDF generation service.

Owns the whole reporte-aprobado -> PDF generado -> validado -> almacenado ->
hasheado -> asociado-a-ReportVersion pipeline. See
pdf-generation-contract.md and pdf-storage-integrity-contract.md.
"""
from __future__ import annotations

import hashlib
import io
import logging
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlmodel import Session, select

from app.core.config import settings
from app.core.security import create_render_token
from app.models.enums import ReportStatus
from app.models.report import Report, ReportVersion
from app.models.storage import StorageObject
from app.services.s3 import S3Service

logger = logging.getLogger(__name__)

# Bumped whenever the render pipeline (Playwright/Chromium pin, page.pdf()
# options) changes in a way that could affect output — recorded on every
# ReportVersion so a historical artifact's provenance is always traceable.
PDF_GENERATOR_VERSION = "playwright-1.49/chromium"

_READY = "READY"
_GENERATING = "GENERATING"
_FAILED = "FAILED"
_IMMUTABLE_REPORT_STATUSES = (ReportStatus.PUBLISHED, ReportStatus.RETRACTED)


class ReportPdfGenerationError(Exception):
    """Carries a sanitized (code, message) pair — never clinical content."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class ReportPdfAlreadyInProgressError(ReportPdfGenerationError):
    pass


class ReportPdfImmutableError(ReportPdfGenerationError):
    pass


class ReportPdfGenerationService:
    """One instance per request; not thread-shared."""

    def __init__(self, session: Session):
        self.session = session

    def generate(
        self,
        report: Report,
        version: ReportVersion,
        triggered_by_user_id: UUID | None,
        force: bool = False,
    ) -> ReportVersion:
        """Generate (or idempotently re-confirm) the official PDF for one
        ReportVersion. Raises ReportPdfGenerationError subclasses on any
        rejection or failure; the caller (the HTTP endpoint) maps those to
        the appropriate status code.

        `force=True` (segunda remediación post-Fase 2, UX) bypasses the
        READY short-circuit below — used exclusively by
        `report_publishing.sign_and_publish`, which may need to embed
        signature metadata into the report's JSON body immediately before
        calling this, and a stale READY from an earlier (pre-signature)
        generation must never be trusted as-is: the official PDF must
        always reflect the just-signed content. Does not bypass the
        GENERATING-in-progress guard below — a real concurrent generation
        still wins.
        """
        if report.status in _IMMUTABLE_REPORT_STATUSES:
            # Checked before the READY idempotency short-circuit below,
            # deliberately: "Rechazar si está PUBLISHED con PDF listo" (E7) —
            # a published report's PDF must never be touched again by this
            # endpoint, not even a no-op re-confirmation. Idempotency (below)
            # only applies pre-publish (e.g. a double-click on "Generar PDF"
            # while still APPROVED).
            raise ReportPdfImmutableError(
                "REPORT_IMMUTABLE", "Cannot generate a PDF for a published or retracted report"
            )

        if version.pdf_generation_status == _READY and not force:
            # Idempotent: already generated, nothing to do. Covers a
            # deliberate re-check or a double-click from the UI, always
            # pre-publish (see the immutable-status check above).
            return version

        now = datetime.utcnow()
        if version.pdf_generation_status == _GENERATING:
            started = version.pdf_generation_started_at
            stale_after = timedelta(seconds=settings.pdf_generation_timeout_seconds * 3)
            if started and (now - started) < stale_after:
                raise ReportPdfAlreadyInProgressError(
                    "GENERATION_IN_PROGRESS", "A generation attempt is already running for this version"
                )
            # Otherwise: GENERATING is orphaned (crash/timeout) — fall through
            # and retry, overwriting the stale attempt.

        version.pdf_generation_status = _GENERATING
        version.pdf_generation_started_at = now
        version.pdf_error_code = None
        version.pdf_error_message = None
        self.session.add(version)
        self.session.commit()
        self.session.refresh(version)

        logger.info(
            "PDF generation started",
            extra={
                "event": "report_pdf.generation_started",
                "tenant_id": str(report.tenant_id),
                "report_id": str(report.id),
                "report_version_id": str(version.id),
            },
        )
        start_time = datetime.utcnow()

        try:
            pdf_bytes = self._render_pdf(report, version)
            self._validate_pdf_bytes(pdf_bytes)
            page_count = self._count_pages(pdf_bytes)
            sha256 = hashlib.sha256(pdf_bytes).hexdigest()
            storage = self._persist(report, version, pdf_bytes, sha256, triggered_by_user_id)
        except ReportPdfGenerationError as exc:
            self._mark_failed(version, exc.code, exc.message)
            logger.error(
                "PDF generation failed",
                extra={
                    "event": "report_pdf.generation_failed",
                    "tenant_id": str(report.tenant_id),
                    "report_id": str(report.id),
                    "report_version_id": str(version.id),
                    "error_code": exc.code,
                    "duration_ms": int((datetime.utcnow() - start_time).total_seconds() * 1000),
                },
            )
            raise
        except Exception as exc:  # noqa: BLE001 - deliberately broad: any
            # unexpected failure must still land the version in FAILED, never
            # leave it stuck in GENERATING.
            self._mark_failed(version, "UNEXPECTED_ERROR", "An unexpected error occurred during PDF generation")
            logger.exception(
                "Unexpected PDF generation failure",
                extra={
                    "event": "report_pdf.generation_failed",
                    "tenant_id": str(report.tenant_id),
                    "report_id": str(report.id),
                    "report_version_id": str(version.id),
                    "error_code": "UNEXPECTED_ERROR",
                },
            )
            raise ReportPdfGenerationError(
                "UNEXPECTED_ERROR", "An unexpected error occurred during PDF generation"
            ) from exc

        version.pdf_storage_id = storage.id
        version.pdf_generation_status = _READY
        version.pdf_generated_at = datetime.utcnow()
        version.pdf_sha256 = sha256
        version.pdf_size_bytes = len(pdf_bytes)
        version.pdf_page_count = page_count
        version.pdf_generator_version = PDF_GENERATOR_VERSION
        self.session.add(version)
        self.session.commit()
        self.session.refresh(version)

        logger.info(
            "PDF generation completed",
            extra={
                "event": "report_pdf.generation_completed",
                "tenant_id": str(report.tenant_id),
                "report_id": str(report.id),
                "report_version_id": str(version.id),
                "size_bytes": version.pdf_size_bytes,
                "page_count": version.pdf_page_count,
                "duration_ms": int((datetime.utcnow() - start_time).total_seconds() * 1000),
            },
        )
        return version

    def _mark_failed(self, version: ReportVersion, code: str, message: str) -> None:
        version.pdf_generation_status = _FAILED
        version.pdf_error_code = code
        version.pdf_error_message = message
        self.session.add(version)
        self.session.commit()

    # -- render -----------------------------------------------------------

    def _render_pdf(self, report: Report, version: ReportVersion) -> bytes:
        if not settings.pdf_generator_base_url:
            raise ReportPdfGenerationError(
                "CONFIG_MISSING", "PDF_GENERATOR_BASE_URL is not configured"
            )

        token = create_render_token(
            str(version.id), str(report.tenant_id), settings.pdf_render_token_expires_seconds
        )
        base = settings.pdf_generator_base_url.rstrip("/")
        url = f"{base}/internal/report-render/{report.id}/{version.version_no}#token={token}"
        timeout_ms = settings.pdf_generation_timeout_seconds * 1000

        from playwright.sync_api import sync_playwright
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(args=["--no-sandbox"])
                try:
                    page = browser.new_page()
                    page.goto(url, timeout=timeout_ms, wait_until="load")
                    page.wait_for_selector(
                        'html[data-report-ready="true"]', timeout=timeout_ms, state="attached"
                    )
                    pdf_bytes = page.pdf(prefer_css_page_size=True, print_background=True)
                finally:
                    browser.close()
        except PlaywrightTimeoutError as exc:
            raise ReportPdfGenerationError(
                "RENDER_TIMEOUT", "Timed out waiting for the report to finish rendering"
            ) from exc
        except ReportPdfGenerationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ReportPdfGenerationError(
                "RENDER_FAILED", "Headless browser failed to render the report"
            ) from exc

        return pdf_bytes

    # -- validate (E5) ------------------------------------------------------

    def _validate_pdf_bytes(self, data: bytes) -> None:
        if not data:
            raise ReportPdfGenerationError("EMPTY_PDF", "Generated PDF is empty")
        if not data.startswith(b"%PDF"):
            raise ReportPdfGenerationError("INVALID_PDF_HEADER", "Generated file is not a valid PDF")
        if len(data) > settings.pdf_max_size_bytes:
            raise ReportPdfGenerationError(
                "PDF_TOO_LARGE", "Generated PDF exceeds the maximum allowed size"
            )
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise ReportPdfGenerationError(
                    "PDF_ENCRYPTED", "Generated PDF is unexpectedly encrypted"
                )
            page_count = len(reader.pages)
        except ReportPdfGenerationError:
            raise
        except (PdfReadError, ValueError) as exc:
            raise ReportPdfGenerationError(
                "PDF_CORRUPT", "Generated PDF could not be parsed"
            ) from exc
        if page_count == 0:
            raise ReportPdfGenerationError("EMPTY_PDF", "Generated PDF has zero pages")
        if page_count > settings.pdf_max_page_count:
            raise ReportPdfGenerationError(
                "PDF_TOO_MANY_PAGES", "Generated PDF exceeds the maximum allowed page count"
            )

    def _count_pages(self, data: bytes) -> int:
        return len(PdfReader(io.BytesIO(data)).pages)

    # -- persist (E6) ---------------------------------------------------

    def _persist(
        self,
        report: Report,
        version: ReportVersion,
        data: bytes,
        sha256: str,
        triggered_by_user_id: UUID | None,
    ) -> StorageObject:
        s3 = S3Service()
        # Non-deterministic key (unlike the legacy .../report.pdf key used by
        # the old manual upload endpoints): a second generation attempt after
        # a FAILED one never overwrites a previous object in place.
        key = (
            f"reports/{report.tenant_id}/{report.id}/versions/{version.version_no}"
            f"/official/{uuid4().hex}.pdf"
        )
        try:
            info = s3.upload_bytes(data, key=key, content_type="application/pdf")
        except Exception as exc:  # noqa: BLE001
            raise ReportPdfGenerationError(
                "S3_UPLOAD_FAILED", "Failed to upload generated PDF to storage"
            ) from exc

        try:
            storage = StorageObject(
                provider="aws",
                region=s3.region,
                bucket=info.bucket,
                object_key=info.key,
                version_id=info.version_id,
                etag=info.etag,
                sha256_hex=sha256,
                content_type="application/pdf",
                size_bytes=info.size_bytes,
                created_by=triggered_by_user_id,
                tenant_id=report.tenant_id,
            )
            self.session.add(storage)
            self.session.commit()
            self.session.refresh(storage)
        except Exception as exc:  # noqa: BLE001
            self.session.rollback()
            try:
                s3.delete_object(key)
            except Exception:  # noqa: BLE001
                logger.error(
                    "Failed to compensate (delete orphaned S3 object) after a "
                    "StorageObject creation failure",
                    extra={
                        "event": "report_pdf.compensation_failed",
                        "tenant_id": str(report.tenant_id),
                        "report_id": str(report.id),
                        "report_version_id": str(version.id),
                        "key": key,
                    },
                )
            raise ReportPdfGenerationError(
                "STORAGE_RECORD_FAILED", "Failed to persist storage record for generated PDF"
            ) from exc

        logger.info(
            "PDF uploaded",
            extra={
                "event": "report_pdf.uploaded",
                "tenant_id": str(report.tenant_id),
                "report_id": str(report.id),
                "report_version_id": str(version.id),
                "size_bytes": info.size_bytes,
            },
        )
        return storage


def load_locked_version(
    session: Session, report_id: str, version_no: int
) -> tuple[Report, ReportVersion] | tuple[None, None]:
    """Load (report, version) for update, row-locking the version so two
    concurrent generation requests for the same version serialize instead of
    racing. Returns (None, None) if either doesn't exist."""
    report = session.get(Report, report_id)
    if not report:
        return None, None
    version = session.exec(
        select(ReportVersion)
        .where(ReportVersion.report_id == report.id, ReportVersion.version_no == version_no)
        .with_for_update()
    ).first()
    if not version:
        return None, None
    return report, version
