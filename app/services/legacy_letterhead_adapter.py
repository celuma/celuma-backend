"""Exports the frozen Legacy renderer's hardcoded letterhead as a portable
`.cell` file — post-Phase-2 remediation, R13; extended in the second
remediation (UX) with visual parity (footer logo, no dividers, bottom
header alignment).

These constants are a MANUAL, VERBATIM copy of
celuma-frontend/src/components/report/legacy/legacy_letterhead_config.ts —
never imported/generated from it (no cross-language coupling), and never
used to modify Legacy in any way. If legacy_letterhead_config.ts ever
changes, this file must be updated by hand to match — see
legacy-parity-contract.md for the full rationale.

The legacy layout does not map 1:1 onto V2's DEFAULT layout: its header
shows ONLY the signing physician's block (bottom-aligned, bold, 8pt, no
logo, no separate institution name) and its logo actually lives in the
FOOTER (left-aligned, next to a right-aligned address+contact block) — the
exact inverse of V2's default header-identity/footer-text layout.

Until the third remediation this was a "best-effort" translation: the
letterhead carried the data, but the V2 renderer could not express several
of those visual decisions (it reserved a logo box in the header even when
there was no logo, used fixed band heights, and had no typographic weight
for the footer). The FOURTH remediation connected those capabilities —
`logo_mode`, `offset_mm`/`height_mm`/`content_gap_mm`/`padding_mm`,
`signer_placement`, `layout=SPLIT`, and `ReportTypographyConfig` weights —
so every field this adapter emits today has a real effect in
`VersionedReportRendererV2`. See v2-legacy-parity-capabilities.md
(capabilities), legacy-adapter-v2-contract.md (this field-by-field
mapping), and legacy-dom-parity-report.md / legacy-pdf-parity-report.md
(measured residual differences).

Legacy itself is never rendered from this data; only
LegacyReportRendererV1's own hardcoded constants render Legacy reports,
unchanged.
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
        # ------------------------------------------------------------------
        # Fourth post-Phase-2 remediation. Every value below is a DIRECT
        # reading of the constants and styles in
        # legacy_report_renderer_v1.tsx, not an approximation:
        #
        #   MARGIN_L_MM / MARGIN_R_MM = 18   -> margins_cm.left/right = 1.8
        #   HEADER_H_MM = 28                 -> header.height_mm = 28
        #   FOOTER_H_MM = 20                 -> footer.height_mm = 20
        #   header.top = 0 / footer.bottom=0 -> offset_mm = 0 on both bands
        #   body.top = HEADER_H_MM           -> header.content_gap_mm = 0
        #   body.bottom = FOOTER_H_MM        -> footer.content_gap_mm = 0
        #   body.paddingTop = 4mm            -> paper.body_padding_top_mm = 4
        #   header.paddingBottom = 4mm       -> header.padding_mm = 4
        #   (footer declares no padding)     -> footer.padding_mm = 0
        #
        # margins_cm.top/bottom stay out of the layout math (explicit
        # `offset_mm` replaces them); they are left at the value that
        # describes the band so the letterhead card remains readable.
        # See legacy-adapter-v2-contract.md.
        # ------------------------------------------------------------------
        paper=ReportPaperConfig(
            size="LETTER",
            orientation="PORTRAIT",
            margins_cm=ReportMarginsCm(top=2.8, right=1.8, bottom=2.0, left=1.8),
            body_padding_top_mm=4.0,
        ),
        header=ReportHeaderConfig(
            enabled=True,
            logo_storage_id=None,
            # The Legacy header has NO logo and does NOT reserve space for
            # one: its logotype lives in the footer. `NONE` is what stops
            # V2 from drawing Céluma's neutral isotype in its place.
            logo_mode="NONE",
            # The four Legacy header lines are, in order, name / specialty /
            # affiliation / licenses — i.e. the full institutional signer
            # block, with a single typography. They are emitted with
            # `signer_placement="INLINE"` instead of splitting them across
            # institution_name/subtitle/address, because those fields have
            # different sizes per line and Legacy's postal address belongs
            # to the FOOTER, not the header.
            institution_name=None,
            subtitle=None,
            address=None,
            phone=None,  # legacy contact string mixes letters ("Tel."/"Cel.") — not a valid phone value
            email=None,
            signer_placement="INLINE",
            # Legacy's header is bottom-aligned (flex-end) with no divider
            # line below it.
            content_alignment="BOTTOM",
            height_mm=28.0,
            offset_mm=0.0,
            content_gap_mm=0.0,
            padding_mm=4.0,
            divider=DividerConfig(enabled=False),
        ),
        footer=ReportFooterConfig(
            enabled=True,
            # Address and contact are TWO lines in Legacy (`<br/>`). The
            # contract forbids markup in free text, so they travel as a
            # real newline and the renderer prints them with
            # `white-space: pre-line`.
            custom_text=f"{_FOOTER_ADDRESS}\n{_FOOTER_CONTACT}",
            # Legacy never printed a page number.
            show_page_number=False,
            # Legacy's logo is footer-left, address+contact text footer-right,
            # no divider line above the footer. `logo_storage_id` is filled
            # on import from `assets.footer_logo`; `CUSTOM` guarantees that
            # if it does not resolve, no substitute is drawn.
            logo_storage_id=None,
            logo_mode="CUSTOM",
            logo_position="LEFT",
            content_alignment="RIGHT",
            layout="SPLIT",
            height_mm=20.0,
            offset_mm=0.0,
            content_gap_mm=0.0,
            padding_mm=0.0,
            # `height: calc(20mm - 4mm)`, `max-width: 35%` on the logo and
            # `max-width: 65%` on the text — literal from Legacy.
            logo_height_mm=16.0,
            logo_max_width_pct=35.0,
            text_max_width_pct=65.0,
            divider=DividerConfig(enabled=False),
        ),
        style=ReportStyleConfig(
            primary_color=_COLOR,
            typography=ReportTypographyConfig(
                font_family="ARIAL",
                base_font_size_pt=10.0,
                # The Legacy header is 8pt bold on all FOUR lines
                # (`fontSize: 8pt` + `fontWeight: bold` on the band);
                # the footer is 7pt also bold.
                header_font_size_pt=8.0,
                header_secondary_font_size_pt=8.0,
                header_font_weight=700,
                footer_font_size_pt=7.0,
                footer_font_weight=700,
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
