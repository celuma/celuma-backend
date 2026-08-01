"""Exports the frozen Legacy renderer's hardcoded letterhead as a portable
`.celuma` file — post-Fase-2 remediation, R13.

These constants are a MANUAL, VERBATIM copy of
celuma-frontend/src/components/report/legacy/legacy_letterhead_config.ts —
never imported/generated from it (no cross-language coupling), and never
used to modify Legacy in any way. If legacy_letterhead_config.ts ever
changes, this file must be updated by hand to match — see
legacy-letterhead-export-contract.md for the full rationale.

The legacy layout does not map 1:1 onto ReportPresentationSnapshotV2 (it
has no separate "signer" block, and its footer packs address+phone+email
into one free-text blob) — this is a best-effort, documented translation,
not a pixel-perfect reproduction. Legacy itself is never rendered from
this data; only LegacyReportRendererV1's own hardcoded constants render
Legacy reports, unchanged.
"""
import base64
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from app.schemas.report_letterhead import (
    CELUMA_FORMAT,
    CELUMA_FORMAT_VERSION,
    CelumaLetterheadAsset,
    CelumaLetterheadEnvelope,
    CelumaLetterheadPayload,
    CelumaLetterheadSource,
)
from app.schemas.report_template_version import (
    ReportFooterConfig,
    ReportHeaderConfig,
    ReportMarginsCm,
    ReportPaperConfig,
    ReportPresentationSnapshotV2,
    ReportSignerSnapshot,
    ReportStyleConfig,
)

# Verbatim copy of legacy_letterhead_config.ts (see module docstring).
_PHYSICIAN_NAME = "Dra. Arisbeth Villanueva Pérez."
_PHYSICIAN_SPECIALTY = "Anatomía Patológica, Nefropatología y Citología Exfoliativa"
_PHYSICIAN_AFFILIATION = "Centro Médico Nacional de Occidente IMSS. INCMNSZ"
_PHYSICIAN_LICENSES = "DGP3833349 | DGP. ESP 6133871"
_FOOTER_ADDRESS = (
    "Francisco Rojas González No. 654 Col. Ladrón de Guevara, Guadalajara, Jalisco, México C.P. 44600"
)
_FOOTER_CONTACT = (
    "Tel. 33 2015 0100, 33 2015 0101. Cel. 33 2823-1959  patologiaynefropatologia@gmail.com"
)
_COLOR = "#002060"

_LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "legacy_letterhead_logo.png"
_LEGACY_LETTERHEAD_NAME = "Membrete legado (embajador)"


def build_legacy_letterhead_export() -> CelumaLetterheadEnvelope:
    """Deterministic — same output every call. Embeds the legacy logo as
    base64 if the asset file is present; exports without a logo (no
    warning surface at this layer — the caller/UI is responsible for
    telling the user) if it is not."""
    assets: dict[str, CelumaLetterheadAsset] = {}
    if _LOGO_PATH.exists():
        logo_bytes = _LOGO_PATH.read_bytes()
        assets["logo"] = CelumaLetterheadAsset(
            media_type="image/png",
            sha256=hashlib.sha256(logo_bytes).hexdigest(),
            data_base64=base64.b64encode(logo_bytes).decode("ascii"),
        )

    presentation = ReportPresentationSnapshotV2(
        paper=ReportPaperConfig(
            size="LETTER",
            orientation="PORTRAIT",
            margins_cm=ReportMarginsCm(top=2.5, right=1.5, bottom=2.5, left=1.5),
        ),
        header=ReportHeaderConfig(
            enabled=True,
            logo_storage_id=None,
            institution_name=_PHYSICIAN_NAME,
            subtitle=_PHYSICIAN_SPECIALTY,
            address=_FOOTER_ADDRESS,
            phone=None,  # legacy contact string mixes letters ("Tel."/"Cel.") — not a valid phone value
            email=None,
        ),
        footer=ReportFooterConfig(
            enabled=True,
            custom_text=_FOOTER_CONTACT,
            show_page_number=True,
        ),
        style=ReportStyleConfig(primary_color=_COLOR),
        signer=ReportSignerSnapshot(
            display_name=_PHYSICIAN_NAME,
            specialty=_PHYSICIAN_SPECIALTY,
            license_number=_PHYSICIAN_LICENSES,
            affiliation=_PHYSICIAN_AFFILIATION,
        ),
    )

    return CelumaLetterheadEnvelope(
        format=CELUMA_FORMAT,
        format_version=CELUMA_FORMAT_VERSION,
        exported_at=datetime.now(timezone.utc).isoformat(),
        source=CelumaLetterheadSource(),
        letterhead=CelumaLetterheadPayload(
            name=_LEGACY_LETTERHEAD_NAME,
            description=(
                "Membrete histórico del tenant embajador, congelado en "
                "LegacyReportRendererV1. Exportado para portabilidad — "
                "nunca usado para modificar Legacy."
            ),
            presentation=presentation,
        ),
        assets=assets,
    )
