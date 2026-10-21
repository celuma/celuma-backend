"""HEIC decoding in ``process_image_bytes`` (Céluma 1.4, #21).

These tests decode real HEIC containers produced by the pillow-heif encoder; the
decoder is never mocked for a HEIC input (the only patches here prove ordering —
that a limit fires BEFORE pixel data is decoded).
"""
from io import BytesIO

import pytest
from PIL import Image, ImageCms

from app.services import image_processing as ip
from app.services.image_processing import ImageProcessingError, process_image_bytes
from tests.heic_fixtures import (
    display_p3_icc,
    heic_bytes,
    heic_multi_image,
    heic_ten_bit_gray,
    with_brand,
)


def _open(jpeg: bytes) -> Image.Image:
    img = Image.open(BytesIO(jpeg))
    img.load()
    return img


def _center(img: Image.Image):
    return img.convert("RGB").getpixel((img.width // 2, img.height // 2))


class TestValidHeic:
    def test_produces_jpeg_and_thumbnail(self):
        out = process_image_bytes("sample.heic", heic_bytes((640, 480)))
        processed, thumb = _open(out.processed_jpeg_bytes), _open(out.thumbnail_jpeg_bytes)
        assert processed.format == "JPEG" and processed.size == (640, 480)
        assert thumb.format == "JPEG" and thumb.size == (512, 384)
        assert out.source_format == "heic"
        assert out.original_bytes is None  # non-RAW originals are not retained
        r, g, b = _center(processed)
        assert abs(r - 200) < 12 and abs(g - 60) < 12 and abs(b - 40) < 12

    def test_small_image_is_not_upscaled_into_the_thumbnail(self):
        out = process_image_bytes("s.heic", heic_bytes((64, 48)))
        assert _open(out.thumbnail_jpeg_bytes).size == (64, 48)

    def test_thumbnail_is_derived_from_full_resolution_not_embedded_thumb(self):
        out = process_image_bytes("s.heic", heic_bytes((800, 600), thumbnail=32))
        assert _open(out.processed_jpeg_bytes).size == (800, 600)
        assert _open(out.thumbnail_jpeg_bytes).size == (512, 384)

    def test_decoded_by_content_whatever_the_filename(self):
        for name in ("photo.jpg", "photo.PNG", "photo", "photo.heic", "photo.HEIF"):
            out = process_image_bytes(name, heic_bytes())
            assert out.source_format == "heic", name

    def test_orientation_is_applied_once(self):
        # EXIF orientation 6 = rotate 90° clockwise: a 64x32 landscape becomes
        # 32x64 portrait and the red marker moves from top-left to top-right.
        out = process_image_bytes(
            "r.heic",
            heic_bytes((64, 32), color=(0, 0, 255), exif_orientation=6, mark_top_left=True),
        )
        img = _open(out.processed_jpeg_bytes)
        assert img.size == (32, 64)
        r, _, b = img.getpixel((img.width - 3, 3))
        assert r > 200 and b < 80, "marker should have moved to the top-right"
        r, _, b = img.getpixel((3, 3))
        assert b > 200 and r < 80
        # the thumbnail agrees with the full rendition
        assert _open(out.thumbnail_jpeg_bytes).size == (32, 64)

    def test_alpha_is_flattened_onto_white(self):
        out = process_image_bytes("a.heic", heic_bytes((32, 32), color=(0, 0, 0, 0), mode="RGBA"))
        r, g, b = _center(_open(out.processed_jpeg_bytes))
        assert min(r, g, b) > 240

    def test_embedded_profile_is_converted_to_srgb(self):
        p3 = display_p3_icc()
        raw = (255, 128, 0)  # saturated orange as encoded in Display P3
        out = process_image_bytes("p3.heic", heic_bytes((32, 32), color=raw, icc_profile=p3))
        got = _center(_open(out.processed_jpeg_bytes))
        # independent expectation: the same lcms conversion applied to the pixel
        src = Image.new("RGB", (1, 1), raw)
        want = ImageCms.profileToProfile(
            src, ImageCms.ImageCmsProfile(BytesIO(p3)), ip.SRGB_PROFILE, outputMode="RGB"
        ).getpixel((0, 0))
        assert all(abs(a - b) <= 10 for a, b in zip(got, want)), (got, want)
        assert got != raw and abs(got[1] - raw[1]) >= 5, "profile must change the values"
        # the derivative is plain sRGB: no profile is embedded in the JPEG
        assert "icc_profile" not in _open(out.processed_jpeg_bytes).info

    def test_unreadable_icc_profile_falls_back_to_decoded_values(self):
        out = process_image_bytes(
            "bad.heic", heic_bytes((32, 32), color=(10, 200, 30), icc_profile=b"not an icc profile")
        )
        g = _center(_open(out.processed_jpeg_bytes))[1]
        assert abs(g - 200) < 12

    def test_ten_bit_input_is_reduced_to_eight_bit_linearly(self):
        # Documented behaviour (specs/reports/sample-images.md): the decoder
        # reduces bit depth; Céluma applies no tone mapping or gain map.
        out = process_image_bytes("hdr.heic", heic_ten_bit_gray(512))
        r, g, b = _center(_open(out.processed_jpeg_bytes))
        assert abs(r - 128) <= 6 and abs(g - 128) <= 6 and abs(b - 128) <= 6


class TestRejections:
    def test_corrupt_container_is_rejected(self):
        valid = heic_bytes((128, 128))
        for bad in (valid[:40], valid[: len(valid) // 2], valid[:32] + b"\xff" * 300):
            with pytest.raises(ImageProcessingError) as exc:
                process_image_bytes("c.heic", bad)
            assert exc.value.code == "heic_corrupt"
            assert exc.value.status_code == 422

    def test_multiple_top_level_images_are_rejected_not_silently_dropped(self):
        with pytest.raises(ImageProcessingError) as exc:
            process_image_bytes("burst.heic", heic_multi_image([(255, 0, 0), (0, 255, 0)]))
        assert exc.value.code == "heic_sequence"

    def test_sequence_brand_is_rejected(self):
        data = with_brand(heic_bytes(), b"miaf", b"msf1")
        with pytest.raises(ImageProcessingError) as exc:
            process_image_bytes("seq.heic", data)
        assert exc.value.code == "heic_sequence"

    @pytest.mark.parametrize("name", ["x.heic", "x.HEIF"])
    def test_heic_name_with_other_content_is_rejected(self, name):
        buf = BytesIO()
        Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, "JPEG")
        with pytest.raises(ImageProcessingError) as exc:
            process_image_bytes(name, buf.getvalue())
        assert exc.value.code == "heic_extension_mismatch"

    def test_avif_brand_is_not_routed_to_the_heic_path(self):
        # AVIF is outside this change: it keeps whatever the generic Pillow
        # path did before (accept or reject), never the HEIC decoder.
        avif_head = b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00avifmif1"
        assert not ip._is_heic_container(ip._heif_brands(avif_head + b"\x00" * 32))

    def test_cr3_raw_container_is_not_routed_to_the_heic_path(self):
        cr3_head = b"\x00\x00\x00\x18ftypcrx \x00\x00\x00\x01crx isom"
        assert not ip._is_heic_container(ip._heif_brands(cr3_head + b"\x00" * 32))

    def test_dimension_limit_is_checked_from_the_header(self, monkeypatch):
        # (the x265 encoder cannot build a >16384 px fixture, so lower the cap)
        monkeypatch.setattr(ip, "HEIC_MAX_DIMENSION", 32)
        with pytest.raises(ImageProcessingError) as exc:
            process_image_bytes("wide.heic", heic_bytes((64, 8)))
        assert exc.value.code == "heic_too_large"

    def test_pixel_limit_fires_before_any_pixel_is_decoded(self, monkeypatch):
        monkeypatch.setattr(ip, "HEIC_MAX_PIXELS", 1000)

        def boom(*_a, **_k):  # pragma: no cover - must not run
            raise AssertionError("pixel data was decoded before the limit check")

        monkeypatch.setattr(ip.pillow_heif.HeifFile, "to_pillow", boom)
        with pytest.raises(ImageProcessingError) as exc:
            process_image_bytes("big.heic", heic_bytes((64, 48)))
        assert exc.value.code == "heic_too_large"

    def test_at_the_pixel_limit_is_accepted(self, monkeypatch):
        monkeypatch.setattr(ip, "HEIC_MAX_PIXELS", 64 * 48)
        assert process_image_bytes("ok.heic", heic_bytes((64, 48))).source_format == "heic"


class TestExistingFormatsUnchanged:
    def _jpeg(self):
        buf = BytesIO()
        Image.new("RGB", (40, 30), (9, 99, 199)).save(buf, "JPEG")
        return buf.getvalue()

    def _png(self):
        buf = BytesIO()
        Image.new("RGBA", (40, 30), (9, 99, 199, 255)).save(buf, "PNG")
        return buf.getvalue()

    def test_jpeg_and_png_still_convert(self):
        for name, data in (("a.jpg", self._jpeg()), ("b.png", self._png())):
            out = process_image_bytes(name, data)
            assert out.source_format is None and out.original_bytes is None
            assert _open(out.processed_jpeg_bytes).size == (40, 30)

    def test_corrupt_jpeg_is_a_deliberate_error_not_a_crash(self):
        with pytest.raises(ImageProcessingError) as exc:
            process_image_bytes("a.jpg", self._jpeg()[:20])
        assert exc.value.code == "unreadable_image"

    def test_garbage_is_a_deliberate_error(self):
        with pytest.raises(ImageProcessingError):
            process_image_bytes("a.png", b"definitely not an image")

    def test_pillow_bomb_limit_is_unchanged(self, monkeypatch):
        # No new pixel policy for JPEG/PNG: Pillow's own limit decides, and a
        # breach is now a 422-able error rather than an unhandled exception.
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
        with pytest.raises(ImageProcessingError):
            process_image_bytes("big.png", self._png())
