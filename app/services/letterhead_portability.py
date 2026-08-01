"""Export/import a `ReportLetterheadVersion` as a portable `.celuma` file —
post-Fase-2 remediation, R12/R13. See celuma-letterhead-file-format.md.
"""
import base64
import hashlib
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlmodel import Session

from app.models.report_letterhead import ReportLetterhead
from app.models.report_letterhead_version import (
    ReportLetterheadVersion,
    ReportLetterheadVersionStatus,
)
from app.models.storage import StorageObject
from app.schemas.report_letterhead import (
    CELUMA_FORMAT,
    CELUMA_FORMAT_VERSION,
    MAX_CELUMA_LOGO_BYTES,
    CelumaLetterheadAsset,
    CelumaLetterheadEnvelope,
    CelumaLetterheadPayload,
    CelumaLetterheadSource,
)
from app.schemas.report_template_version import ReportPresentationSnapshotV2
from app.services.managed_tenant_image_service import (
    ImageRegistrationError,
    InvalidImageError,
    ManagedTenantImageService,
)
from app.services.s3 import S3Service


class CelumaPortabilityError(ValueError):
    """Raised for any `.celuma` validation failure — corrupt file, unknown
    format/version, hash mismatch, oversized asset. `message` is safe to
    surface directly to the client as an HTTP 400 body."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def export_letterhead_version(
    letterhead: ReportLetterhead,
    version: ReportLetterheadVersion,
    session: Session,
    s3: Optional[S3Service] = None,
) -> CelumaLetterheadEnvelope:
    """Builds a portable envelope for a published letterhead version. Never
    includes tenant_id, StorageObject id, bucket/key, or any URL — the logo
    (if any) is embedded as base64 bytes with a sha256 hash instead."""
    s3 = s3 or S3Service()
    presentation = ReportPresentationSnapshotV2.model_validate(version.configuration)

    assets: dict[str, CelumaLetterheadAsset] = {}
    logo_storage_id = presentation.header.logo_storage_id
    exported_header = presentation.header
    if logo_storage_id:
        storage = session.get(StorageObject, logo_storage_id)
        if storage is not None:
            logo_bytes = s3.download_bytes(storage.object_key)
            assets["logo"] = CelumaLetterheadAsset(
                media_type=storage.content_type or "image/png",
                sha256=hashlib.sha256(logo_bytes).hexdigest(),
                data_base64=base64.b64encode(logo_bytes).decode("ascii"),
            )
        # `logo_storage_id` is a StorageObject id owned by the exporting
        # tenant — it is meaningless (and a data leak) to another tenant,
        # so the exported presentation never carries it. Import
        # regenerates a fresh StorageObject and a fresh id.
        exported_header = exported_header.model_copy(update={"logo_storage_id": None})

    exported_presentation = presentation.model_copy(update={"header": exported_header})

    return CelumaLetterheadEnvelope(
        format=CELUMA_FORMAT,
        format_version=CELUMA_FORMAT_VERSION,
        exported_at=datetime.now(timezone.utc).isoformat(),
        source=CelumaLetterheadSource(),
        letterhead=CelumaLetterheadPayload(
            name=letterhead.name,
            description=letterhead.description,
            presentation=exported_presentation,
        ),
        assets=assets,
    )


def import_letterhead_version(
    envelope: CelumaLetterheadEnvelope,
    *,
    tenant_id: UUID,
    created_by: UUID,
    session: Session,
    s3: Optional[S3Service] = None,
) -> ReportLetterheadVersion:
    """Validates and imports a `.celuma` envelope as a brand-new letterhead
    (never publishes into an existing one, never reuses ids from the
    source tenant). Re-validates the logo through the same
    ManagedTenantImageService path a fresh upload would use — the
    embedded content_type/hash in the file is never trusted blindly."""
    if envelope.format != CELUMA_FORMAT:
        raise CelumaPortabilityError(f"Unrecognized file format: {envelope.format!r}")
    if envelope.format_version != CELUMA_FORMAT_VERSION:
        raise CelumaPortabilityError(
            f"Unsupported .celuma format version: {envelope.format_version}"
        )

    presentation = envelope.letterhead.presentation
    logo_storage_id: Optional[str] = None
    asset = envelope.assets.get("logo")
    if asset is not None:
        if len(asset.data_base64) > MAX_CELUMA_LOGO_BYTES * 2:
            # base64 inflates size ~4/3x; a generous pre-decode guard avoids
            # decoding an absurdly large payload just to reject it.
            raise CelumaPortabilityError("Embedded logo asset is too large")
        try:
            logo_bytes = base64.b64decode(asset.data_base64, validate=True)
        except Exception:
            raise CelumaPortabilityError("Embedded logo asset is not valid base64") from None

        actual_hash = hashlib.sha256(logo_bytes).hexdigest()
        if actual_hash != asset.sha256:
            raise CelumaPortabilityError(
                "Embedded logo asset failed integrity check (sha256 mismatch) — "
                "the file may be corrupted"
            )

        try:
            result = ManagedTenantImageService(s3).upload(
                file_bytes=logo_bytes,
                declared_content_type=asset.media_type,
                tenant_id=tenant_id,
                key_prefix="report-letterheads/imported/logos",
                created_by=created_by,
                session=session,
            )
        except InvalidImageError as exc:
            raise CelumaPortabilityError(f"Embedded logo is invalid: {exc.message}") from None
        except ImageRegistrationError as exc:
            raise CelumaPortabilityError(str(exc)) from None
        logo_storage_id = str(result.storage_object.id)

    imported_header = presentation.header.model_copy(update={"logo_storage_id": logo_storage_id})
    imported_presentation = presentation.model_copy(update={"header": imported_header})

    new_letterhead = ReportLetterhead(
        tenant_id=tenant_id,
        name=envelope.letterhead.name,
        description=envelope.letterhead.description,
        created_by=created_by,
        is_active=True,
        is_default=False,
    )
    session.add(new_letterhead)
    session.flush()

    new_version = ReportLetterheadVersion(
        tenant_id=tenant_id,
        report_letterhead_id=new_letterhead.id,
        version_number=1,
        schema_version=2,
        configuration=imported_presentation.model_dump(mode="json"),
        status=ReportLetterheadVersionStatus.PUBLISHED,
        created_by=created_by,
    )
    session.add(new_version)
    session.commit()
    session.refresh(new_version)
    return new_version
