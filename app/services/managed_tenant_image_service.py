"""Shared validation + upload logic for tenant-owned images (logos).

Post-Fase-2 remediation, R5/R9 (managed-logo-upload-contract.md). Before
this service existed, the same MIME/size/dimension validation and
StorageObject-creation dance was hand-duplicated across the template-logo
endpoint (strict) and the tenant-logo endpoint (weaker: no Pillow
verification, no size/dimension caps). Both now share this implementation.

Note on URL resolution: this service intentionally still returns
`S3Service.object_public_url()`, not a presigned URL. An earlier draft of
this remediation assumed the media bucket's `BlockPublicAccess.BLOCK_ALL`
meant direct-to-S3 public URLs were broken — but `object_public_url()`
resolves through `MEDIA_PUBLIC_BASE_URL` (a CloudFront distribution with
Origin Access Control reading the bucket) whenever that setting is
configured, which it is in every real environment (see
celuma-infra/celuma_infra/backend_stack.py). This was verified against a
real uploaded logo before writing this service (HTTP 200, real image
bytes, via the CDN domain) — see
docs/celuma-1.3/post-phase-2-remediation/managed-logo-upload-contract.md.
Switching to presigned URLs here would be a regression (added expiry,
added latency, no benefit) — it does not fix anything actually broken.
"""
import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Optional
from uuid import UUID, uuid4

from PIL import Image, UnidentifiedImageError
from sqlmodel import Session

from app.models.storage import StorageObject
from app.services.s3 import S3Service

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5MB
MAX_IMAGE_DIMENSION_PX = 4000  # sanity cap, not a specific design requirement

_CONTENT_TYPE_TO_FORMAT = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/jpg": "JPEG",
    "image/webp": "WEBP",
}
_FORMAT_TO_CANONICAL = {
    "PNG": ("image/png", "png"),
    "JPEG": ("image/jpeg", "jpg"),
    "WEBP": ("image/webp", "webp"),
}


class InvalidImageError(ValueError):
    """Raised when an uploaded image fails validation. `message` is safe
    to surface directly to the client as an HTTP 400 body."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ImageRegistrationError(RuntimeError):
    """Raised when the image was uploaded to S3 but the StorageObject
    could not be committed. The caller should surface this as HTTP 500;
    the message is intentionally generic (no internal details)."""


@dataclass
class ManagedImageUploadResult:
    storage_object: StorageObject
    url: str
    content_type: str
    size_bytes: int


class ManagedTenantImageService:
    """Validates and uploads a tenant-owned image (logo), creating the
    backing `StorageObject`. Compensates (deletes the S3 object) if the DB
    commit fails, mirroring the pattern already proven in
    `upload_template_logo`."""

    def __init__(self, s3: Optional[S3Service] = None):
        self._s3 = s3 or S3Service()

    def validate(self, *, file_bytes: bytes, declared_content_type: str) -> tuple[str, str]:
        """Returns (canonical_content_type, extension) or raises InvalidImageError."""
        declared = (declared_content_type or "").split(";")[0].strip().lower()
        expected_format = _CONTENT_TYPE_TO_FORMAT.get(declared)
        if expected_format is None:
            raise InvalidImageError(
                "Only PNG, JPEG, or WEBP images are allowed (SVG is not supported)"
            )

        if not file_bytes:
            raise InvalidImageError("Uploaded file is empty")
        if len(file_bytes) > MAX_IMAGE_BYTES:
            raise InvalidImageError("Image file size must be less than 5MB")

        try:
            with Image.open(BytesIO(file_bytes)) as probe:
                probe.verify()
            with Image.open(BytesIO(file_bytes)) as img:
                actual_format = img.format
                width, height = img.size
        except (UnidentifiedImageError, OSError, ValueError):
            raise InvalidImageError("Uploaded file is not a valid image") from None

        if actual_format != expected_format:
            raise InvalidImageError(
                "Declared content type does not match the actual image content"
            )
        if width > MAX_IMAGE_DIMENSION_PX or height > MAX_IMAGE_DIMENSION_PX:
            raise InvalidImageError(
                f"Image dimensions must not exceed {MAX_IMAGE_DIMENSION_PX}px"
            )

        return _FORMAT_TO_CANONICAL[actual_format]

    def upload(
        self,
        *,
        file_bytes: bytes,
        declared_content_type: str,
        tenant_id: UUID,
        key_prefix: str,
        created_by: UUID,
        session: Session,
    ) -> ManagedImageUploadResult:
        content_type, ext = self.validate(
            file_bytes=file_bytes, declared_content_type=declared_content_type
        )

        key = f"{key_prefix.rstrip('/')}/{uuid4().hex}.{ext}"
        info = self._s3.upload_bytes(file_bytes, key=key, content_type=content_type)

        try:
            storage = StorageObject(
                provider="aws",
                region=self._s3.region,
                bucket=info.bucket,
                object_key=info.key,
                version_id=info.version_id,
                etag=info.etag,
                content_type=content_type,
                size_bytes=info.size_bytes,
                created_by=created_by,
                # Populated so the object is resolvable by id alone, with no
                # parent entity to check ownership through — see
                # storage.py's tenant_id comment.
                tenant_id=tenant_id,
            )
            session.add(storage)
            session.commit()
            session.refresh(storage)
        except Exception:
            session.rollback()
            try:
                self._s3.delete_object(key)
            except Exception:
                logger.error(
                    "Failed to compensate (delete orphaned S3 object) after "
                    "a StorageObject creation failure",
                    extra={
                        "event": "managed_tenant_image.compensation_failed",
                        "tenant_id": str(tenant_id),
                        "key": key,
                    },
                )
            raise ImageRegistrationError("Failed to register uploaded image") from None

        return ManagedImageUploadResult(
            storage_object=storage,
            url=self._s3.object_public_url(key),
            content_type=content_type,
            size_bytes=info.size_bytes,
        )
