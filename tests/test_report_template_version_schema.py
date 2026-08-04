"""Schema tests for the V2 rendering snapshot contract (Céluma 1.3, Phase 2,
Block B, Story B1).

`ReportRenderingSnapshotV2` is the strict contract backing
`ReportTemplateVersion.configuration`. These tests protect its validation
rules directly (independent of persistence/HTTP), per Céluma1.3-Fase2.md §9.
"""
import pytest
from pydantic import ValidationError

from app.schemas.report_template_version import (
    MAX_MARGIN_CM,
    MIN_MARGIN_CM,
    ReportRenderingSnapshotV2,
)


def valid_snapshot(**overrides) -> dict:
    base = {
        "schema_version": 2,
        "template": {
            "base": {"diagnosis": {"label": "Diagnóstico", "type": "text"}},
            "sections": {},
            "base_order": ["diagnosis"],
            "section_order": [],
        },
        "presentation": {
            "paper": {
                "size": "LETTER",
                "orientation": "PORTRAIT",
                "margins_cm": {"top": 2.0, "right": 2.0, "bottom": 2.0, "left": 2.0},
            },
            "header": {
                "enabled": True,
                "logo_storage_id": "11111111-1111-1111-1111-111111111111",
                "institution_name": "Céluma Labs",
                "subtitle": "Diagnóstico Anatomopatológico",
                "address": "Av. Siempre Viva 123, CDMX",
                "phone": "+52 55 1234 5678",
                "email": "contacto@celuma.example",
            },
            "footer": {
                "enabled": True,
                "custom_text": "Documento confidencial",
                "show_page_number": True,
            },
            "style": {"primary_color": "#336699"},
            "signer": {
                "display_name": "Dra. Ejemplo",
                "specialty": "Patología",
                "license_number": "ABC123",
                "affiliation": "Céluma",
            },
        },
    }
    base.update(overrides)
    return base


class TestValidSnapshot:
    def test_full_valid_snapshot_is_accepted(self):
        snapshot = ReportRenderingSnapshotV2.model_validate(valid_snapshot())
        assert snapshot.schema_version == 2
        assert snapshot.presentation.paper.size == "LETTER"

    def test_optional_fields_can_be_omitted(self):
        payload = valid_snapshot()
        payload["presentation"]["header"] = {
            "enabled": False,
            "logo_storage_id": None,
            "institution_name": None,
            "subtitle": None,
            "address": None,
            "phone": None,
            "email": None,
        }
        payload["presentation"]["signer"] = None
        snapshot = ReportRenderingSnapshotV2.model_validate(payload)
        assert snapshot.presentation.header.institution_name is None
        assert snapshot.presentation.signer is None

    def test_style_and_signer_are_optional_at_construction(self):
        payload = valid_snapshot()
        del payload["presentation"]["style"]
        del payload["presentation"]["signer"]
        snapshot = ReportRenderingSnapshotV2.model_validate(payload)
        assert snapshot.presentation.style.primary_color == "#4A4A4A"
        assert snapshot.presentation.signer is None


class TestMargins:
    @pytest.mark.parametrize("value", [MIN_MARGIN_CM, MAX_MARGIN_CM, (MIN_MARGIN_CM + MAX_MARGIN_CM) / 2])
    def test_margins_within_bounds_are_accepted(self, value):
        payload = valid_snapshot()
        payload["presentation"]["paper"]["margins_cm"] = {
            "top": value,
            "right": value,
            "bottom": value,
            "left": value,
        }
        ReportRenderingSnapshotV2.model_validate(payload)

    def test_margin_below_minimum_is_rejected(self):
        payload = valid_snapshot()
        payload["presentation"]["paper"]["margins_cm"]["top"] = MIN_MARGIN_CM - 0.1
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)

    def test_margin_above_maximum_is_rejected(self):
        payload = valid_snapshot()
        payload["presentation"]["paper"]["margins_cm"]["left"] = MAX_MARGIN_CM + 0.1
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)


class TestPaperAndOrientation:
    def test_unsupported_paper_size_is_rejected(self):
        payload = valid_snapshot()
        payload["presentation"]["paper"]["size"] = "A4"
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)

    def test_unsupported_orientation_is_rejected(self):
        payload = valid_snapshot()
        payload["presentation"]["paper"]["orientation"] = "LANDSCAPE"
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)


class TestColor:
    def test_invalid_color_is_rejected(self):
        payload = valid_snapshot()
        payload["presentation"]["style"]["primary_color"] = "blue"
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)

    def test_color_missing_hash_is_rejected(self):
        payload = valid_snapshot()
        payload["presentation"]["style"]["primary_color"] = "336699"
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)

    def test_valid_hex_color_is_accepted(self):
        payload = valid_snapshot()
        payload["presentation"]["style"]["primary_color"] = "#000000"
        ReportRenderingSnapshotV2.model_validate(payload)


class TestMarkupRejection:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("institution_name", "<script>alert(1)</script>"),
            ("subtitle", "javascript:alert(1)"),
            ("address", "<img src=x onerror=alert(1)>"),
        ],
    )
    def test_header_fields_reject_html_js(self, field, value):
        payload = valid_snapshot()
        payload["presentation"]["header"][field] = value
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)

    def test_footer_custom_text_rejects_html(self):
        payload = valid_snapshot()
        payload["presentation"]["footer"]["custom_text"] = "<b>bold</b>"
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)

    def test_signer_display_name_rejects_html(self):
        payload = valid_snapshot()
        payload["presentation"]["signer"]["display_name"] = "<script>x</script>"
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)


class TestInstitutionalDataLength:
    def test_institution_name_too_long_is_rejected(self):
        payload = valid_snapshot()
        payload["presentation"]["header"]["institution_name"] = "A" * 256
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)

    def test_address_too_long_is_rejected(self):
        payload = valid_snapshot()
        payload["presentation"]["header"]["address"] = "A" * 501
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)

    def test_footer_custom_text_too_long_is_rejected(self):
        payload = valid_snapshot()
        payload["presentation"]["footer"]["custom_text"] = "A" * 1001
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)


class TestLogoReference:
    def test_invalid_logo_reference_is_rejected(self):
        payload = valid_snapshot()
        payload["presentation"]["header"]["logo_storage_id"] = "not-a-uuid"
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)

    def test_null_logo_reference_is_accepted(self):
        payload = valid_snapshot()
        payload["presentation"]["header"]["logo_storage_id"] = None
        ReportRenderingSnapshotV2.model_validate(payload)


class TestRequiredAndUnknownFields:
    def test_missing_paper_is_rejected(self):
        payload = valid_snapshot()
        del payload["presentation"]["paper"]
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)

    def test_missing_template_is_rejected(self):
        payload = valid_snapshot()
        del payload["template"]
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)

    def test_unknown_top_level_field_is_rejected(self):
        payload = valid_snapshot()
        payload["unexpected_field"] = "value"
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)

    def test_unknown_presentation_field_is_rejected(self):
        payload = valid_snapshot()
        payload["presentation"]["unexpected_field"] = "value"
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)

    def test_unknown_header_field_is_rejected(self):
        payload = valid_snapshot()
        payload["presentation"]["header"]["unexpected_field"] = "value"
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)

    def test_unsupported_schema_version_is_rejected(self):
        payload = valid_snapshot()
        payload["schema_version"] = 3
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)


class TestTemplateSizeLimit:
    def test_oversized_template_is_rejected(self):
        payload = valid_snapshot()
        payload["template"] = {"sections": {f"s{i}": "x" * 1000 for i in range(600)}}
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)

    def test_non_dict_template_is_rejected(self):
        payload = valid_snapshot()
        payload["template"] = ["not", "a", "dict"]
        with pytest.raises(ValidationError):
            ReportRenderingSnapshotV2.model_validate(payload)
