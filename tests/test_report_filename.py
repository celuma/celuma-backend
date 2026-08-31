"""H-0c — the canonical PDF filename contract.

    official    <ORDER_CODE>-<StudyTypePascalCase>.pdf
    local copy  <ORDER_CODE>-<StudyTypePascalCase>-v<VERSION>-LOCAL.pdf

`SHARED_CASES` is the parity table. The identical table exists in
`celuma-frontend/src/test/lib/report_filename.test.ts`; the two runtimes cannot
share an implementation, so they share this contract and both assert against it.
Any change here must be mirrored there.
"""
import pytest

from app.services.report_filename import (
    build_report_pdf_filename,
    pascal_case,
    report_pdf_filename_base,
    sanitize_order_code,
)

# (order_code, study_type, version, official, local)
SHARED_CASES = [
    ("CTM-35", "Citología Mamaria", 1,
     "CTM-35-CitologiaMamaria.pdf", "CTM-35-CitologiaMamaria-v1-LOCAL.pdf"),
    ("CTM-35", "Citología Urinaria", 2,
     "CTM-35-CitologiaUrinaria.pdf", "CTM-35-CitologiaUrinaria-v2-LOCAL.pdf"),
    ("BIO-7", "Biopsia de Riñón", 12,
     "BIO-7-BiopsiaDeRinon.pdf", "BIO-7-BiopsiaDeRinon-v12-LOCAL.pdf"),
    # leading/trailing and repeated whitespace collapse
    ("CTM-35", "  Citología   Mamaria  ", 1,
     "CTM-35-CitologiaMamaria.pdf", "CTM-35-CitologiaMamaria-v1-LOCAL.pdf"),
    # unsafe punctuation is removed, not transliterated into separators
    ("CTM-35", 'Citología: Mamaria/Urinaria?', 1,
     "CTM-35-CitologiaMamariaUrinaria.pdf",
     "CTM-35-CitologiaMamariaUrinaria-v1-LOCAL.pdf"),
    # mixed casing: interior casing is preserved, so acronyms survive
    ("PCR-1", "prueba PCR rápida", 3,
     "PCR-1-PruebaPCRRapida.pdf", "PCR-1-PruebaPCRRapida-v3-LOCAL.pdf"),
    # the order code keeps its normal hyphen and is not collapsed
    ("CTM-35", "Citología Mamaria", 1,
     "CTM-35-CitologiaMamaria.pdf", "CTM-35-CitologiaMamaria-v1-LOCAL.pdf"),
    # missing study type -> deterministic fallback, never a patient-derived name
    ("CTM-35", "", 1, "CTM-35-Reporte.pdf", "CTM-35-Reporte-v1-LOCAL.pdf"),
    # missing order code -> deterministic fallback
    ("", "Citología Mamaria", 1,
     "SIN-ORDEN-CitologiaMamaria.pdf", "SIN-ORDEN-CitologiaMamaria-v1-LOCAL.pdf"),
]


@pytest.mark.parametrize("code,study,version,official,local", SHARED_CASES)
def test_the_shared_contract(code, study, version, official, local):
    assert build_report_pdf_filename(code, study) == official
    assert build_report_pdf_filename(code, study, version, local_copy=True) == local


@pytest.mark.parametrize("code,study,version,official,local", SHARED_CASES)
def test_both_artifacts_share_one_canonical_base(code, study, version, official, local):
    """The point of the contract: the two files must look like the same
    report. Before H-0c they were `reporte-CTM-35-v1.pdf` and
    `Reporte Citologia Mamaria - Luigi Mario (copia local).pdf`."""
    base = report_pdf_filename_base(code, study)
    assert official == f"{base}.pdf"
    assert local == f"{base}-v{version}-LOCAL.pdf"
    assert official.removesuffix(".pdf") == local.split("-v")[0] or local.startswith(base)


class TestVersionRule:
    def test_the_official_name_never_carries_a_version(self):
        """§4: the official filename names the canonical artifact. Provenance
        comes from the report id, version, object key, sha256 and audit
        history — never the human-visible filename."""
        for v in (1, 2, 12, 999):
            name = build_report_pdf_filename("CTM-35", "Citología Mamaria", v)
            assert name == "CTM-35-CitologiaMamaria.pdf"
            assert f"-v{v}" not in name

    @pytest.mark.parametrize("version,expected", [
        (1, "CTM-35-CitologiaMamaria-v1-LOCAL.pdf"),
        (2, "CTM-35-CitologiaMamaria-v2-LOCAL.pdf"),
        (12, "CTM-35-CitologiaMamaria-v12-LOCAL.pdf"),
    ])
    def test_the_local_copy_always_carries_its_version(self, version, expected):
        assert build_report_pdf_filename(
            "CTM-35", "Citología Mamaria", version, local_copy=True) == expected

    def test_a_local_copy_without_a_version_still_cannot_collide(self):
        local = build_report_pdf_filename("CTM-35", "Citología Mamaria", None, local_copy=True)
        assert local == "CTM-35-CitologiaMamaria-v1-LOCAL.pdf"
        assert local != build_report_pdf_filename("CTM-35", "Citología Mamaria")


class TestFilenameSafety:
    @pytest.mark.parametrize("bad", ['/', '\\', ':', '*', '?', '"', '<', '>', '|'])
    def test_unsafe_characters_never_survive(self, bad):
        name = build_report_pdf_filename(f"CT{bad}M-1", f"Citolog{bad}ia", 1, local_copy=True)
        assert bad not in name

    def test_path_traversal_is_impossible(self):
        name = build_report_pdf_filename("../../etc", "../passwd", 1, local_copy=True)
        assert "/" not in name and ".." not in name
        assert name.endswith("-v1-LOCAL.pdf")

    def test_the_extension_is_well_formed(self):
        for code, study, *_ in SHARED_CASES:
            assert build_report_pdf_filename(code, study).endswith(".pdf")
            assert build_report_pdf_filename(code, study).count(".pdf") == 1

    def test_a_very_long_study_type_is_bounded_and_deterministic(self):
        """§7: bounded, never truncating the extension or the LOCAL suffix,
        and the same input always yields the same output."""
        long_study = "Citología " * 40
        official = build_report_pdf_filename("CTM-35", long_study)
        local = build_report_pdf_filename("CTM-35", long_study, 3, local_copy=True)
        assert official == build_report_pdf_filename("CTM-35", long_study)  # deterministic
        assert official.endswith(".pdf")
        assert local.endswith("-v3-LOCAL.pdf")
        assert len(official) < 100 and len(local) < 100

    def test_the_order_code_keeps_its_hyphen(self):
        assert sanitize_order_code("CTM-35") == "CTM-35"
        assert sanitize_order_code("  CTM-35  ") == "CTM-35"
        assert sanitize_order_code("CTM//35") == "CTM-35"


class TestNoPatientIdentity:
    """§3/§16 — the filename must never carry patient-identifying data."""

    def test_a_patient_name_cannot_reach_the_filename(self):
        # The builder simply has no patient input; this pins that by asserting
        # the output for a real-looking case contains nothing personal.
        name = build_report_pdf_filename("CTM-35", "Citología Mamaria", 1, local_copy=True)
        for token in ("Luigi", "Mario", "luigi", "mario", "@", "paciente"):
            assert token not in name
        assert name == "CTM-35-CitologiaMamaria-v1-LOCAL.pdf"

    def test_the_report_display_title_is_not_used(self):
        """The old local filename came from the report title, which embeds the
        patient name. The contract takes order code + study type only."""
        title = "Reporte Citologia Mamaria - Luigi Mario"
        name = build_report_pdf_filename("CTM-35", "Citología Mamaria", 1, local_copy=True)
        assert title not in name
        assert "Luigi" not in name and "Mario" not in name

    def test_no_uuid_or_storage_key_appears(self):
        name = build_report_pdf_filename("CTM-35", "Citología Mamaria", 1, local_copy=True)
        assert "-" in name  # sanity: it is the canonical shape
        import re
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}", name)
        assert "reports/" not in name and "s3" not in name.lower()


class TestPascalCase:
    @pytest.mark.parametrize("raw,expected", [
        ("Citología Mamaria", "CitologiaMamaria"),
        ("Citología Urinaria", "CitologiaUrinaria"),
        ("Biopsia de Riñón", "BiopsiaDeRinon"),
        ("  Citología   Mamaria  ", "CitologiaMamaria"),
        ("", ""),
        ("   ", ""),
        # diacritics stripped; interior casing preserved, so an all-caps word
        # stays all-caps rather than being title-cased into `Aeiou`.
        ("ÁÉÍÓÚ ñÑ", "AEIOUNN"),
    ])
    def test_normalization(self, raw, expected):
        assert pascal_case(raw) == expected
