"""Synthetic HEIC fixtures for the sample-image tests (Céluma 1.4, #21).

Every byte string here is generated at test time with the same pillow-heif
encoder that ships in the wheel — real HEIC containers, nothing hand-written
and no patient or real-photo data. Decoding them exercises libheif for real; no
test replaces the decoder with a JPEG stand-in.
"""
from __future__ import annotations

import struct
from io import BytesIO
from typing import Iterable, Optional

import pillow_heif
from PIL import Image


def heic_bytes(
    size: tuple[int, int] = (64, 48),
    color=(200, 60, 40),
    *,
    mode: str = "RGB",
    exif_orientation: Optional[int] = None,
    icc_profile: Optional[bytes] = None,
    thumbnail: Optional[int] = None,
    quality: int = 90,
    mark_top_left: bool = False,
) -> bytes:
    """One top-level HEIC image. ``mark_top_left`` paints a 1/4-size red block
    in the top-left corner so a test can tell how the image was rotated."""
    img = Image.new(mode, size, color)
    if mark_top_left:
        block = Image.new(mode, (size[0] // 4, size[1] // 4), (255, 0, 0) + ((255,) if mode == "RGBA" else ()))
        img.paste(block, (0, 0))
    heif = pillow_heif.from_pillow(img)
    if thumbnail:
        heif[0].info["thumbnails"] = [thumbnail]
    save_kwargs = {"quality": quality}
    if exif_orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        save_kwargs["exif"] = exif.tobytes()
    if icc_profile is not None:
        heif[0].info["icc_profile"] = icc_profile
        heif[0].info["icc_profile_type"] = "prof"
    buffer = BytesIO()
    heif.save(buffer, **save_kwargs)
    return buffer.getvalue()


def heic_ten_bit_gray(level_10bit: int, size: tuple[int, int] = (32, 32)) -> bytes:
    """A 10-bit HEIC filled with one grey code value (0-1023)."""
    heif = pillow_heif.HeifFile()
    sample = struct.pack("<H", level_10bit << 6)  # 16-bit container, 10 significant bits
    heif.add_frombytes("RGB;16", size, sample * (3 * size[0] * size[1]))
    buffer = BytesIO()
    heif.save(buffer, quality=95)
    return buffer.getvalue()


def heic_multi_image(colors: Iterable[tuple[int, int, int]], size: tuple[int, int] = (32, 32)) -> bytes:
    """Several top-level images in one container (burst / sequence-like)."""
    heif = pillow_heif.HeifFile()
    for color in colors:
        heif.add_from_pillow(Image.new("RGB", size, color))
    buffer = BytesIO()
    heif.save(buffer, quality=80)
    return buffer.getvalue()


def with_brand(data: bytes, old: bytes, new: bytes) -> bytes:
    """Swap one compatible brand inside the ``ftyp`` box."""
    assert len(old) == len(new) == 4
    head, tail = data[:64], data[64:]
    assert old in head, f"{old!r} not in ftyp"
    return head.replace(old, new, 1) + tail


def display_p3_icc() -> bytes:
    """A minimal ICC v2 matrix/TRC profile with Display P3 primaries (D50-adapted)
    and gamma 2.2 — enough for lcms to convert to sRGB, built by hand so the test
    needs no profile file from the host."""

    def s15f16(v: float) -> bytes:
        return struct.pack(">i", int(round(v * 65536)))

    def xyz_tag(x: float, y: float, z: float) -> bytes:
        return b"XYZ \x00\x00\x00\x00" + s15f16(x) + s15f16(y) + s15f16(z)

    gamma = b"curv\x00\x00\x00\x00" + struct.pack(">I", 1) + struct.pack(">H", int(2.2 * 256)) + b"\x00\x00"
    desc_text = b"Test P3\x00"
    desc = b"desc\x00\x00\x00\x00" + struct.pack(">I", len(desc_text)) + desc_text + b"\x00" * (4 + 4 + 2 + 1 + 67)
    cprt = b"text\x00\x00\x00\x00" + b"Public domain test profile\x00"
    tags = [
        (b"desc", desc),
        (b"cprt", cprt),
        (b"wtpt", xyz_tag(0.9642, 1.0, 0.8249)),
        (b"rXYZ", xyz_tag(0.5151, 0.2412, -0.0011)),
        (b"gXYZ", xyz_tag(0.2920, 0.6922, 0.0419)),
        (b"bXYZ", xyz_tag(0.1571, 0.0666, 0.7841)),
        (b"rTRC", gamma),
        (b"gTRC", gamma),
        (b"bTRC", gamma),
    ]
    table = struct.pack(">I", len(tags))
    offset = 128 + 4 + 12 * len(tags)
    body = b""
    for sig, payload in tags:
        pad = (-len(payload)) % 4
        table += sig + struct.pack(">II", offset + len(body), len(payload))
        body += payload + b"\x00" * pad
    size = 128 + len(table) + len(body)
    header = (
        struct.pack(">I", size) + b"\x00\x00\x00\x00" + b"\x02\x20\x00\x00" + b"mntr" + b"RGB " + b"XYZ "
        + b"\x00" * 12 + b"acsp" + b"\x00" * 4 + b"\x00" * 4 + b"\x00" * 4 + b"\x00" * 8 + b"\x00" * 4
        + b"\x00" * 4 + s15f16(0.9642) + s15f16(1.0) + s15f16(0.8249)
    )
    header = header.ljust(128, b"\x00")
    return header + table + body
