"""Sample-status presentation labels (Céluma 1.3, pre-release remediation).

`SampleState` (`app/models/enums.py`) is the enum that drives business logic,
transitions, audit metadata and idempotency. It must never be rendered
directly as user-facing Spanish text — the raw English member name
(`PROCESSING`, `DAMAGED`, ...) is not a translation.

This is the one place that maps a `SampleState` to its Spanish presentation
label. Every notification integration that needs to say a sample's state in
prose imports `sample_status_label` from here rather than re-declaring its
own copy — the frontend's own `SAMPLE_STATE_CONFIG`
(`src/components/ui/status_configs.tsx`) is a separate, unavoidable
duplication across the stack boundary (the backend renders and freezes
`Notification.title`/`body` server-side; the frontend cannot re-render
them), not a duplication this module is meant to close.

Locale-aware by shape, matching Block F's `template_key + locale` model
(`app/services/locale.py`): `SAMPLE_STATUS_LABELS_BY_LOCALE` is keyed by
locale first, so a future locale registers its own vocabulary instead of
patching this function.
"""
from __future__ import annotations

from typing import Dict, Final

from app.models.enums import SampleState
from app.services.locale import DEFAULT_LOCALE, Locale, resolve_locale

#: `es-MX` labels for every `SampleState` member. Kept in sync with the enum
#: by `test_every_sample_status_has_an_es_mx_label` (structural test).
SAMPLE_STATUS_LABELS_BY_LOCALE: Final[Dict[Locale, Dict[SampleState, str]]] = {
    DEFAULT_LOCALE: {
        SampleState.RECEIVED: "Recibida",
        SampleState.PROCESSING: "En proceso",
        SampleState.READY: "Lista",
        SampleState.DAMAGED: "Insuficiente",
        SampleState.CANCELLED: "Cancelada",
    },
}


def sample_status_label(state: SampleState, locale: str | None = None) -> str:
    """The Spanish presentation label for `state` in `locale`.

    Follows the same fallback as `resolve_locale`/`get_template`: an
    unsupported-but-well-formed locale falls back to `DEFAULT_LOCALE` rather
    than raising, since a notification must always render *something*.
    """
    resolved = resolve_locale(locale)
    by_state = SAMPLE_STATUS_LABELS_BY_LOCALE.get(resolved) or SAMPLE_STATUS_LABELS_BY_LOCALE[DEFAULT_LOCALE]
    return by_state[state]
