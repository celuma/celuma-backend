from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Optional, Set, Tuple

from PIL import Image, ImageOps, ImageCms, UnidentifiedImageError

try:
    import rawpy  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    rawpy = None  # type: ignore

try:
    import pillow_heif  # type: ignore

    # Only the primary still image is ever used: skip thumbnails, depth maps and
    # auxiliary images (HDR gain maps, portrait mattes) so they are never decoded.
    pillow_heif.options.THUMBNAILS = False
    pillow_heif.options.DEPTH_IMAGES = False
    pillow_heif.options.AUX_IMAGES = False
    pillow_heif.options.DECODE_THREADS = 2
except Exception:  # pragma: no cover - optional dependency
    pillow_heif = None  # type: ignore


RAW_EXTENSIONS = {
    ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".sr2", ".raf",
    ".rw2", ".orf", ".pef", ".dng",
}

# Standard sRGB profile for color normalization
SRGB_PROFILE = ImageCms.createProfile("sRGB")

# --- HEIC (Céluma 1.4, #21) --------------------------------------------------
# HEIC is recognised by CONTENT (ISO-BMFF `ftyp` brands), never by filename or
# MIME type. Only the HEIC still-image brands are accepted; AVIF and generic
# HEIF are deliberately not routed here (see specs/reports/sample-images.md).
HEIC_EXTENSIONS = (".heic", ".heif")
HEIC_STILL_BRANDS = {b"heic", b"heix"}
# Image sequences / multiview / scalable variants: rejected, not flattened.
HEIC_SEQUENCE_BRANDS = {b"msf1", b"hevc", b"hevx", b"heim", b"heis"}
# Decoded-size limits, checked from the container header BEFORE any pixel data
# is decoded. 64 MP covers current 48 MP phone sensors; 8-bit RGB at the cap is
# ~190 MB per working copy.
HEIC_MAX_PIXELS = 64_000_000
HEIC_MAX_DIMENSION = 16_384


class ImageProcessingError(Exception):
    """An upload cannot be turned into a usable image. ``message`` is Spanish
    and safe to show to the user; ``code`` is a stable machine identifier."""

    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass
class ProcessedImage:
    """Holds in-memory image renditions to upload."""
    original_bytes: Optional[bytes]
    processed_jpeg_bytes: bytes
    thumbnail_jpeg_bytes: bytes
    content_type_processed: str = "image/jpeg"
    content_type_thumbnail: str = "image/jpeg"
    #: "heic" when the renditions were converted from a HEIC container.
    source_format: Optional[str] = None


def _encode_jpeg(pil_image: Image.Image, quality: int = 90) -> bytes:
    buffer = BytesIO()
    pil_image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def _make_thumbnail(pil_image: Image.Image, size: Tuple[int, int] = (512, 512)) -> Image.Image:
    thumb = ImageOps.exif_transpose(pil_image.copy())
    thumb.thumbnail(size, Image.LANCZOS)
    return thumb


def _heif_brands(data: bytes) -> Optional[Set[bytes]]:
    """Return the major + compatible brands of an ISO-BMFF ``ftyp`` box, or
    None when ``data`` does not start with one."""
    if len(data) < 16 or data[4:8] != b"ftyp":
        return None
    box_size = int.from_bytes(data[0:4], "big")
    end = box_size if 16 <= box_size <= 4096 else 64
    end = min(end, len(data))
    brands = {data[8:12]}
    for i in range(16, end - 3, 4):
        brands.add(data[i:i + 4])
    return brands


def _is_heic_container(brands: Optional[Set[bytes]]) -> bool:
    return bool(brands and brands & (HEIC_STILL_BRANDS | HEIC_SEQUENCE_BRANDS))


def _process_heic(data: bytes, brands: Set[bytes]) -> ProcessedImage:
    """Decode a HEIC still image and build the JPEG + thumbnail renditions.

    Primary-image policy: a file is accepted when its container holds exactly
    one top-level image. Thumbnails, depth maps and auxiliary images (HDR gain
    maps, mattes) are attached to that image and are not top-level items, so an
    ordinary phone HEIC passes. More than one top-level image (burst, sequence,
    alternates) or a sequence brand is rejected: no frame is silently dropped.
    """
    if pillow_heif is None:  # pragma: no cover - dependency is pinned
        raise ImageProcessingError(
            "heic_unavailable",
            "La conversión de HEIC no está disponible en este servidor.",
            status_code=503,
        )
    if brands & HEIC_SEQUENCE_BRANDS:
        raise ImageProcessingError(
            "heic_sequence",
            "El archivo HEIC es una secuencia de imágenes. Exporta una sola foto "
            "(JPEG o HEIC de una imagen) e inténtalo de nuevo.",
        )
    try:
        heif = pillow_heif.open_heif(BytesIO(data), convert_hdr_to_8bit=True)
        count = len(heif)
        width, height = heif.size
    except Exception:
        raise ImageProcessingError(
            "heic_corrupt", "No se pudo leer el archivo HEIC: está dañado o incompleto."
        ) from None
    if count != 1:
        raise ImageProcessingError(
            "heic_sequence",
            f"El archivo HEIC contiene {count} imágenes (ráfaga o secuencia). "
            "Exporta una sola foto e inténtalo de nuevo.",
        )
    if (
        width < 1
        or height < 1
        or width * height > HEIC_MAX_PIXELS
        or max(width, height) > HEIC_MAX_DIMENSION
    ):
        raise ImageProcessingError(
            "heic_too_large",
            f"La imagen HEIC es demasiado grande ({width} × {height} px). El máximo es "
            f"{HEIC_MAX_PIXELS // 1_000_000} megapíxeles y {HEIC_MAX_DIMENSION} px por lado.",
        )
    try:
        pil = heif.to_pillow()
        pil.load()
        del heif  # release libheif's own copy of the pixels before the derivatives are built
        # pillow-heif applies HEIF/EXIF rotation itself and resets Orientation to
        # 1, so this is normally a no-op; it only guards the invariant. (Skipped when
        # already upright: exif_transpose would copy a 64 MP image for nothing.)
        if pil.getexif().get(0x0112, 1) not in (0, 1):
            pil = ImageOps.exif_transpose(pil)
        if pil.mode in ("RGBA", "LA", "La", "RGBa"):
            rgba = pil.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.split()[-1])
            background.info.update(
                {k: v for k, v in pil.info.items() if k == "icc_profile"}
            )
            pil = background
        elif pil.mode != "RGB":
            pil = pil.convert("RGB")
        # Embedded ICC profile -> sRGB (a Display P3 iPhone photo is the usual case).
        # An unreadable profile falls back to the decoded values, unconverted.
        pil = _normalize_color_profile(pil)
        processed = _encode_jpeg(pil, quality=90)
        # The full-resolution image is no longer needed: shrink it in place rather
        # than copying it for the thumbnail.
        pil.thumbnail((512, 512), Image.LANCZOS)
        thumb = _encode_jpeg(pil, quality=85)
    except ImageProcessingError:
        raise
    except Exception:
        raise ImageProcessingError(
            "heic_corrupt", "No se pudo leer el archivo HEIC: está dañado o incompleto."
        ) from None
    return ProcessedImage(
        original_bytes=None,  # non-RAW originals are not retained (current policy)
        processed_jpeg_bytes=processed,
        thumbnail_jpeg_bytes=thumb,
        source_format="heic",
    )


def process_image_bytes(filename: str, data: bytes) -> ProcessedImage:
    """Process an uploaded file and prepare processed and thumbnail renditions.

    - If the CONTENT is HEIC (by ``ftyp`` brand, whatever the filename/MIME say):
      decode with pillow-heif, then JPEG + thumbnail.
    - If RAW (by extension) and rawpy available: decode to RGB then JPEG + thumbnail.
    - If not RAW: open with Pillow, convert to RGB, JPEG + thumbnail.

    Raises ``ImageProcessingError`` when the file cannot be turned into an image.
    """
    lower = filename.lower()
    is_raw = any(lower.endswith(ext) for ext in RAW_EXTENSIONS)

    brands = _heif_brands(data)
    if _is_heic_container(brands):
        return _process_heic(data, brands)  # type: ignore[arg-type]
    if lower.endswith(HEIC_EXTENSIONS) and not is_raw:
        # Claimed HEIC but the bytes are not a HEIC container: do not let Pillow
        # quietly accept a mislabelled file under the HEIC flow.
        raise ImageProcessingError(
            "heic_extension_mismatch",
            "El archivo tiene extensión HEIC/HEIF pero su contenido no es una imagen "
            "HEIC compatible. Expórtalo como HEIC o JPEG e inténtalo de nuevo.",
        )

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
    try:
        pil = Image.open(BytesIO(data))
        pil = ImageOps.exif_transpose(pil).convert("RGB")
        processed = _encode_jpeg(pil, quality=90)
        thumb = _encode_jpeg(_make_thumbnail(pil), quality=85)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError):
        raise ImageProcessingError(
            "unreadable_image",
            "No se pudo leer la imagen: el archivo está dañado o su formato no es compatible.",
        ) from None
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

