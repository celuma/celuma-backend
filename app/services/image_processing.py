from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Optional, Tuple

from PIL import Image, ImageOps, ImageCms

try:
    import rawpy  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    rawpy = None  # type: ignore


RAW_EXTENSIONS = {
    ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".sr2", ".raf",
    ".rw2", ".orf", ".pef", ".dng",
}

# Standard sRGB profile for color normalization
SRGB_PROFILE = ImageCms.createProfile("sRGB")


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


def _normalize_color_profile(pil_image: Image.Image) -> Image.Image:
    """Normalize image color profile to sRGB for consistent display.
    
    This handles HDR images and images with embedded ICC profiles
    by converting them to standard sRGB color space.
    """
    try:
        # Check if image has an embedded ICC profile
        icc_profile = pil_image.info.get("icc_profile")
        if icc_profile:
            # Create profile from embedded data
            input_profile = ImageCms.ImageCmsProfile(BytesIO(icc_profile))
            # Convert to sRGB
            pil_image = ImageCms.profileToProfile(
                pil_image,
                input_profile,
                SRGB_PROFILE,
                renderingIntent=ImageCms.Intent.PERCEPTUAL,
                outputMode="RGB"
            )
    except Exception:
        # If color conversion fails, just ensure RGB mode
        if pil_image.mode != "RGB":
            pil_image = pil_image.convert("RGB")
    
    return pil_image


@dataclass
class ProcessedAvatar:
    """Holds processed avatar image data."""
    jpeg_bytes: bytes
    content_type: str = "image/jpeg"


def process_avatar_bytes(
    data: bytes, 
    max_size: Tuple[int, int] = (512, 512),
    quality: int = 90
) -> ProcessedAvatar:
    """Process an avatar image for optimal web display.
    
    This function:
    1. Normalizes HDR/wide-gamut images to sRGB color space
    2. Applies EXIF rotation
    3. Resizes to maximum dimensions while preserving aspect ratio
    4. Converts to JPEG for universal compatibility
    
    Args:
        data: Raw image bytes
        max_size: Maximum dimensions (width, height)
        quality: JPEG compression quality (1-100)
        
    Returns:
        ProcessedAvatar with normalized JPEG bytes
    """
    # Open image
    pil = Image.open(BytesIO(data))
    
    # Apply EXIF rotation
    pil = ImageOps.exif_transpose(pil)
    
    # Convert to RGB mode (handles RGBA, P, L modes)
    if pil.mode != "RGB":
        # For RGBA, composite on white background
        if pil.mode == "RGBA":
            background = Image.new("RGB", pil.size, (255, 255, 255))
            background.paste(pil, mask=pil.split()[-1])
            pil = background
        else:
            pil = pil.convert("RGB")
    
    # Normalize color profile (handles HDR)
    pil = _normalize_color_profile(pil)
    
    # Resize if larger than max_size
    pil.thumbnail(max_size, Image.LANCZOS)
    
    # Encode to JPEG
    jpeg_bytes = _encode_jpeg(pil, quality=quality)
    
    return ProcessedAvatar(jpeg_bytes=jpeg_bytes)

