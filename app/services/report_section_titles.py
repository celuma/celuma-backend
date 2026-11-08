"""The label-only correction for Céluma's two predefined description sections."""

from unicodedata import normalize


_TITLES = {
    "section_macroscopic": ("Macroscópica", "Descripción macroscópica"),
    "section_microscopic": ("Microscópica", "Descripción microscópica"),
}


def rename_description_labels(document: dict) -> dict:
    """Return the original document on a no-op. Preserve custom labels and all content."""
    sections = document.get("sections")
    if not isinstance(sections, dict):
        return document

    changed = {}
    for key, (old, new) in _TITLES.items():
        section = sections.get(key)
        label = section.get("label") if isinstance(section, dict) else None
        if isinstance(label, str) and normalize("NFC", label) == old:
            changed[key] = {**section, "label": new}

    if not changed:
        return document
    return {**document, "sections": {**sections, **changed}}


def rename_report_description_labels(body: dict) -> dict:
    """Correct both the body labels and the report's own rendering snapshot."""
    updated = rename_description_labels(body)
    snapshot = body.get("rendering_snapshot")
    template = snapshot.get("template") if isinstance(snapshot, dict) else None
    if isinstance(template, dict):
        renamed = rename_description_labels(template)
        if renamed is not template:
            return {**updated, "rendering_snapshot": {**snapshot, "template": renamed}}
    return updated
