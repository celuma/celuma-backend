from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Optional, Tuple

from PIL import Image, ImageOps

try:
    import rawpy  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    rawpy = None  # type: ignore


RAW_EXTENSIONS = {
    ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".sr2", ".raf",
    ".rw2", ".orf", ".pef", ".dng",
}


@dataclass
class ProcessedImage:
    """Holds in-memory image renditions to upload."""
    original_bytes: Optional[bytes]
    processed_jpeg_bytes: bytes
    thumbnail_jpeg_bytes: bytes
    content_type_processed: str = "image/jpeg"
    content_type_thumbnail: str = "image/jpeg"


def _encode_jpeg(pil_image: Image.Image, quality: int = 90) -> bytes:
    buffer = BytesIO()
    pil_image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def _make_thumbnail(pil_image: Image.Image, size: Tuple[int, int] = (512, 512)) -> Image.Image:
    thumb = ImageOps.exif_transpose(pil_image.copy())
    thumb.thumbnail(size, Image.LANCZOS)
    return thumb


def process_image_bytes(filename: str, data: bytes) -> ProcessedImage:
    """Process an uploaded file and prepare processed and thumbnail renditions.

    - If RAW (by extension) and rawpy available: decode to RGB then JPEG + thumbnail.
    - If not RAW: open with Pillow, convert to RGB, JPEG + thumbnail.
    """
    lower = filename.lower()
    is_raw = any(lower.endswith(ext) for ext in RAW_EXTENSIONS)

    if is_raw and rawpy is not None:
        try:
            raw = rawpy.imread(BytesIO(data))  # type: ignore[attr-defined]
            rgb = raw.postprocess(use_auto_wb=True, no_auto_bright=True)
            pil = Image.fromarray(rgb)
            pil = ImageOps.exif_transpose(pil).convert("RGB")
            processed = _encode_jpeg(pil, quality=90)
            thumb = _encode_jpeg(_make_thumbnail(pil), quality=85)
            return ProcessedImage(
                original_bytes=data,
                processed_jpeg_bytes=processed,
                thumbnail_jpeg_bytes=thumb,
            )
        except Exception:
            # Fallback to regular image processing if RAW decoding fails
            pass

    # Fallback for regular images
    pil = Image.open(BytesIO(data))
    pil = ImageOps.exif_transpose(pil).convert("RGB")
    processed = _encode_jpeg(pil, quality=90)
    thumb = _encode_jpeg(_make_thumbnail(pil), quality=85)
    return ProcessedImage(
        original_bytes=None,  # original not required for non-RAW
        processed_jpeg_bytes=processed,
        thumbnail_jpeg_bytes=thumb,
    )


