"""
Regression tests for the report JSON contract (Céluma 1.3, Fase 1 — Workstream 5).

These protect the *current* behavior: the backend stores the report body as an
opaque `Dict[str, Any]` (see app/schemas/report.py) and never validates its
internal shape. Compatibility with old/partial documents is the frontend's
responsibility (src/models/report.ts). These tests confirm that every fixture
in tests/fixtures/reports/ — including the deliberately old/partial one —
deserializes cleanly through the current backend schemas, and that the
required top-level (Postgres-side) fields are still enforced.

No production code is changed by these tests.
"""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.report import ReportCreate, ReportDetailResponse, ReportVersionCreate

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "reports"
FIXTURE_FILES = sorted(p for p in FIXTURES_DIR.glob("*.json"))


def load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def report_body(fixture: dict) -> dict:
    """Strip the documentation-only _fixture_meta key before feeding to schemas."""
    return {k: v for k, v in fixture.items() if k != "_fixture_meta"}


class TestFixturesExist:
    def test_all_fourteen_matrix_cases_are_covered(self):
        """Sanity check that the fixture set matches the README mapping table."""
        names = {p.name for p in FIXTURE_FILES}
        expected = {
            "draft_single_sample_no_images.json",
            "published_multi_sample_with_images_all_sections.json",
            "empty_optional_sections.json",
            "long_content_multipage.json",
            "special_characters_accents.json",
            "legacy_oldest_structure.json",
            "no_patient_report.json",
        }
        assert expected.issubset(names)


class TestReportCreateContract:
    """Every fixture must be usable as the `report` field of ReportCreate —
    i.e. the historical JSON can be deserialized, regardless of shape."""

    @pytest.mark.parametrize("fixture_path", FIXTURE_FILES, ids=lambda p: p.stem)
    def test_fixture_is_accepted_as_report_body(self, fixture_path: Path):
        fixture = load_fixture(fixture_path.name)
        payload = ReportCreate(
            tenant_id="00000000-0000-0000-0000-000000000001",
            branch_id="00000000-0000-0000-0000-000000000002",
            order_id="00000000-0000-0000-0000-000000000003",
            report=report_body(fixture),
        )
        assert payload.report is not None
        assert "base" in payload.report
        assert "sections" in payload.report


class TestReportDetailResponseContract:
    """Round-trips each fixture through ReportDetailResponse, the schema used
    to serve GET /reports/{id} — this is what the frontend actually consumes."""

    @pytest.mark.parametrize("fixture_path", FIXTURE_FILES, ids=lambda p: p.stem)
    def test_fixture_reconstructs_as_detail_response(self, fixture_path: Path):
        fixture = load_fixture(fixture_path.name)
        meta = fixture.get("_fixture_meta", {})
        detail = ReportDetailResponse(
            id="00000000-0000-0000-0000-000000000010",
            version_no=1,
            status=meta.get("status", "DRAFT"),
            order_id="00000000-0000-0000-0000-000000000003",
            tenant_id="00000000-0000-0000-0000-000000000001",
            branch_id="00000000-0000-0000-0000-000000000002",
            report=report_body(fixture),
            template=None,
        )
        assert detail.report == report_body(fixture)

    def test_legacy_fixture_missing_order_and_signature_fields_still_validates(self):
        """The oldest-structure fixture has no base_order/section_order/
        signatureMetadata and is missing the (later-added) requesting_physician
        base field. The backend must not reject it — absent fields are the
        frontend normalizers' responsibility (src/models/report.ts)."""
        fixture = load_fixture("legacy_oldest_structure.json")
        body = report_body(fixture)
        assert "base_order" not in body
        assert "section_order" not in body
        assert "signatureMetadata" not in body
        assert "requesting_physician" not in body["base"]

        detail = ReportDetailResponse(
            id="00000000-0000-0000-0000-000000000010",
            status="PUBLISHED",
            order_id="00000000-0000-0000-0000-000000000003",
            tenant_id="00000000-0000-0000-0000-000000000001",
            branch_id="00000000-0000-0000-0000-000000000002",
            report=body,
        )
        assert detail.report["base"]["order_code"]["value"] == "SYN-0006"

    def test_empty_optional_sections_preserve_visibility_and_empty_content(self):
        fixture = load_fixture("empty_optional_sections.json")
        body = report_body(fixture)
        assert body["sections"]["section_diagnosis"]["is_visible"] is True
        assert body["sections"]["section_diagnosis"]["content"] == ""
        assert body["sections"]["section_extra_notes"]["is_visible"] is False

    def test_multi_sample_report_preserves_image_order(self):
        fixture = load_fixture("published_multi_sample_with_images_all_sections.json")
        body = report_body(fixture)
        images = body["sections"]["images"]["content"]
        assert [img["id"] for img in images] == ["img-a1", "img-b1", "img-c1", "img-c2"]

    def test_special_characters_are_preserved_verbatim(self):
        fixture = load_fixture("special_characters_accents.json")
        body = report_body(fixture)
        assert body["base"]["patient"]["value"] == "María José Muñóz Peña"
        assert "ñ" in body["base"]["requesting_physician"]["value"]

    def test_no_patient_report_has_empty_but_present_patient_field(self):
        fixture = load_fixture("no_patient_report.json")
        body = report_body(fixture)
        assert body["base"]["patient"]["value"] == ""
        assert "patient" in body["base"]  # present, just empty — not omitted


class TestRequiredTopLevelFieldsAreStillEnforced:
    """The backend does NOT validate the report body shape, but it still
    enforces its own Postgres-side required fields — this must not regress."""

    def test_report_create_requires_tenant_branch_order(self):
        with pytest.raises(ValidationError):
            ReportCreate(report={"base": {}, "sections": {}})  # type: ignore[call-arg]

    def test_report_detail_response_requires_id_status_order_tenant_branch(self):
        with pytest.raises(ValidationError):
            ReportDetailResponse(report={"base": {}, "sections": {}})  # type: ignore[call-arg]

    def test_report_version_create_requires_pdf_storage_id(self):
        with pytest.raises(ValidationError):
            ReportVersionCreate(report_id="x", version_no=1)  # type: ignore[call-arg]
