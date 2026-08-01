"""Exports the frozen Legacy renderer's hardcoded letterhead as a portable
`.cell` file — post-Fase-2 remediation, R13; extendido en la segunda
remediación (UX) con paridad visual (logo de pie, sin divisores, alineación
inferior del header).

These constants are a MANUAL, VERBATIM copy of
celuma-frontend/src/components/report/legacy/legacy_letterhead_config.ts —
never imported/generated from it (no cross-language coupling), and never
used to modify Legacy in any way. If legacy_letterhead_config.ts ever
changes, this file must be updated by hand to match — see
legacy-parity-contract.md for the full rationale.

The legacy layout does not map 1:1 onto ReportPresentationSnapshotV2: its
header shows ONLY the signing physician's block (bottom-aligned, bold,
8pt, no logo, no separate institution name) and its logo actually lives in
the FOOTER (left-aligned, next to a right-aligned address+contact block) —
the exact inverse of V2's default header-identity/footer-text layout. This
adapter is a best-effort, documented translation, not a pixel-perfect
reproduction (V2's header always renders a logo slot, even if empty/neutral
— Legacy's does not; V2's footer text is not bold). See
legacy-parity-validation-report.md for the full list of residual visual
differences after this mapping. Legacy itself is never rendered from this
data; only LegacyReportRendererV1's own hardcoded constants render Legacy
reports, unchanged.
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
    DividerConfig,
    ReportFooterConfig,
    ReportHeaderConfig,
    ReportMarginsCm,
    ReportPaperConfig,
    ReportPresentationSnapshotV2,
    ReportSignerSnapshot,
    ReportStyleConfig,
    ReportTypographyConfig,
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
    base64 under `assets.footer_logo` if the asset file is present (Legacy's
    logo lives in the footer, not the header — see module docstring);
    exports without a logo (no warning surface at this layer — the
    caller/UI is responsible for telling the user) if it is not."""
    assets: dict[str, CelumaLetterheadAsset] = {}
    if _LOGO_PATH.exists():
        logo_bytes = _LOGO_PATH.read_bytes()
        assets["footer_logo"] = CelumaLetterheadAsset(
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
            # Legacy's header is bottom-aligned (flex-end) with no divider
            # line below it — see module docstring for the residual
            # difference this leaves (V2 still always renders a header
            # logo slot; Legacy's header has none at all).
            content_alignment="BOTTOM",
            height_mm=28.0,
            divider=DividerConfig(enabled=False),
        ),
        footer=ReportFooterConfig(
            enabled=True,
            custom_text=_FOOTER_CONTACT,
            show_page_number=True,
            # Legacy's logo is footer-left, address+contact text footer-right,
            # no divider line above the footer.
            logo_storage_id=None,
            logo_position="LEFT",
            content_alignment="RIGHT",
            height_mm=20.0,
            divider=DividerConfig(enabled=False),
        ),
        style=ReportStyleConfig(
            primary_color=_COLOR,
            typography=ReportTypographyConfig(
                font_family="ARIAL", header_font_size_pt=8.0, footer_font_size_pt=7.0
            ),
        ),
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
