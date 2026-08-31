"""Céluma 1.3 — the canonical download filename for a report PDF.

ONE contract, two artifacts:

    official    <ORDER_CODE>-<StudyTypePascalCase>.pdf
    local copy  <ORDER_CODE>-<StudyTypePascalCase>-v<VERSION>-LOCAL.pdf

Before H-0c the two were built by unrelated string concatenation and did not
look like the same report: the official download was `reporte-CTM-35-v1.pdf`
while the local copy was named from the report's display TITLE, e.g.
`Reporte Citologia Mamaria - Luigi Mario (copia local).pdf` — which also put
the patient's name in a filename.

Deliberate properties of this contract:

* **No patient identity.** The filename identifies the clinical artifact by
  order code and study type only. Patient identity stays inside the report and
  in Céluma's domain data. Orders without a patient name the same way.
* **The official filename carries no version.** It names the canonical official
  artifact. Provenance comes from the report id, version, storage object key,
  sha256 and audit history — never from a human-visible filename.
* **The version marks the LOCAL copy**, together with the `LOCAL` suffix, so a
  local copy can never be mistaken for the official document.
* **The storage object key is untouched.** This is a download-time name,
  exposed through `Content-Disposition`; historical S3 objects are never
  renamed and no migration is involved.

`celuma-frontend/src/lib/report_filename.ts` mirrors this module exactly.
Parity tests on both sides share the same case table.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

# Characters that are unsafe in a filename on any platform Céluma targets,
# plus everything else non-alphanumeric that could survive normalization.
_UNSAFE = re.compile(r'[/\\:*?"<>|]')
_NON_WORD = re.compile(r"[^0-9A-Za-z]+")
_ORDER_CODE_UNSAFE = re.compile(r"[^0-9A-Za-z_-]+")

# Deterministic bound on the study-type component. Long enough for any real
# study name, short enough that the whole filename stays practical. The
# extension and the `-v<N>-LOCAL` suffix are NEVER truncated — only this part
# is, and always at the same length, so the same input always yields the same
# output.
_MAX_STUDY_TYPE_CHARS = 60
_MAX_ORDER_CODE_CHARS = 40

_FALLBACK_ORDER_CODE = "SIN-ORDEN"
_FALLBACK_STUDY_TYPE = "Reporte"


def strip_diacritics(value: str) -> str:
    """`Citología` -> `Citologia`, `Riñón` -> `Rinon`.

    Decompose, drop the combining marks, recompose. `ñ` is `n` + a combining
    tilde after NFD, so this handles it without a character map.
    """
    decomposed = unicodedata.normalize("NFD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def pascal_case(value: str) -> str:
    """`"  Citología   Mamaria  "` -> `CitologiaMamaria`.

    Trims, collapses whitespace, removes diacritics, splits on every
    non-alphanumeric boundary, capitalizes each word and concatenates. Every
    word is capitalized, including short connectors: `Biopsia de Riñón`
    becomes `BiopsiaDeRinon`.
    """
    ascii_value = strip_diacritics(value or "")
    words = [w for w in _NON_WORD.split(ascii_value) if w]
    if not words:
        return ""
    # `.capitalize()` would lowercase the rest of an all-caps word (`PCR` ->
    # `Pcr`); this preserves interior casing and only forces the first letter.
    return "".join(w[0].upper() + w[1:] for w in words)


def sanitize_order_code(order_code: Optional[str]) -> str:
    """Preserves the human-readable code as-is (`CTM-35` stays `CTM-35`) and
    only replaces characters that are unsafe in a filename. Never collapses
    the hyphen that real order codes contain."""
    raw = strip_diacritics((order_code or "").strip())
    safe = _ORDER_CODE_UNSAFE.sub("-", raw).strip("-")
    # Collapse runs introduced by substitution, so `CTM//35` cannot become
    # `CTM--35`.
    safe = re.sub(r"-{2,}", "-", safe)
    return safe[:_MAX_ORDER_CODE_CHARS] or _FALLBACK_ORDER_CODE


def build_report_pdf_filename(
    order_code: Optional[str],
    study_type: Optional[str],
    version: Optional[int] = None,
    local_copy: bool = False,
) -> str:
    """The canonical download filename. See the module docstring.

    `version` is required for a local copy and IGNORED for the official one —
    the official artifact's name must not expose an internal version number.
    A local copy with no known version falls back to `v1`, so it still cannot
    collide with the official name.
    """
    code = sanitize_order_code(order_code)
    study = pascal_case(study_type or "")[:_MAX_STUDY_TYPE_CHARS] or _FALLBACK_STUDY_TYPE
    base = f"{code}-{study}"
    if local_copy:
        return f"{base}-v{version if version and version > 0 else 1}-LOCAL.pdf"
    return f"{base}.pdf"


def report_pdf_filename_base(
    order_code: Optional[str], study_type: Optional[str]
) -> str:
    """The shared stem both artifacts are built from — the thing that makes the
    official PDF and its local copy recognisably the same report."""
    return f"{sanitize_order_code(order_code)}-" + (
        pascal_case(study_type or "")[:_MAX_STUDY_TYPE_CHARS] or _FALLBACK_STUDY_TYPE
    )
