"""Locale identifiers for notification rendering (Céluma 1.3, Phase 3, Block F).

**This is localization readiness, not localization.** Céluma 1.3 supports
exactly one locale, `es-MX`, and this block adds no translation, no language
selector, no user or tenant locale column and no translation framework. What
it adds is the *shape*: template lookup becomes `template_key + locale`, so
the day a second locale is genuinely needed it is a registry entry rather than
a refactor of every call site.

Why a typed identifier rather than a bare string
------------------------------------------------
Two failure modes are different and must stay different (Story F8):

    "en-US"     well-formed, currently unsupported  -> fall back to es-MX
    "../../foo" not a locale identifier at all      -> reject

Collapsing them into one "unknown, use the default" branch would make a
path-traversal-shaped value indistinguishable from a legitimate request for
Portuguese, and the registries are keyed by this value. `parse_locale` is
therefore strict about the *grammar* (BCP-47's language[-Script][-REGION]
subset Céluma needs) and separately permissive about the *catalogue*.

Future resolution order
-----------------------
Not implemented, deliberately — none of these fields exists today and Block F
is forbidden from adding them::

    user preferred locale  ->  tenant default locale  ->  DEFAULT_LOCALE

When they do exist, `resolve_locale` is the only function that changes: every
caller already passes a `Locale` and every registry is already keyed by one.
"""
from __future__ import annotations

import re
from typing import Final, FrozenSet, Optional

#: The only locale Céluma 1.3 renders. Every registry declares copy for it,
#: and `SUPPORTED_LOCALES` is asserted to contain it.
DEFAULT_LOCALE: Final[str] = "es-MX"

#: The catalogue. One entry, on purpose: an unused locale in a shipped
#: registry is copy nobody reviewed, in a language nobody at Céluma reads.
SUPPORTED_LOCALES: Final[FrozenSet[str]] = frozenset({DEFAULT_LOCALE})

#: BCP-47, narrowed to what Céluma will plausibly need: a two/three-letter
#: language, an optional four-letter script, an optional two-letter or
#: three-digit region. Deliberately not a full BCP-47 parser — extensions,
#: variants and private-use subtags have no meaning here, and accepting them
#: would widen what can be handed to a registry lookup for no benefit.
_LOCALE_GRAMMAR: Final[re.Pattern[str]] = re.compile(
    r"^[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|[0-9]{3}))?$"
)

#: A locale is a canonical string. A `NewType` would be erased at runtime and
#: a dataclass would need unwrapping at every dict lookup; what actually
#: carries the guarantee is that the only way to obtain one is `parse_locale`.
Locale = str


class InvalidLocaleError(ValueError):
    """The value is not a locale identifier.

    Distinct from "unsupported": an unsupported locale is a legitimate request
    Céluma cannot yet satisfy and falls back; an invalid one is a bug or an
    injection attempt and is rejected. Carries a stable `code` so it can be
    logged without echoing the offending value.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def parse_locale(value: object) -> Locale:
    """Return `value` as a canonical locale identifier, or raise.

    Validates the *grammar* only. `en-US` parses successfully and is simply
    not in `SUPPORTED_LOCALES`; that distinction is `resolve_locale`'s.
    """
    if not isinstance(value, str):
        raise InvalidLocaleError(
            "invalid_locale_type", "A locale identifier must be a string"
        )

    candidate = value.strip()
    if not candidate:
        raise InvalidLocaleError(
            "invalid_locale_empty", "A locale identifier must not be empty"
        )
    # Bounded before the regex: a pathological input should not be handed to a
    # pattern matcher at all, and no real identifier approaches this length.
    if len(candidate) > 35:
        raise InvalidLocaleError(
            "invalid_locale_length", "A locale identifier must not exceed 35 characters"
        )
    if not _LOCALE_GRAMMAR.match(candidate):
        raise InvalidLocaleError(
            "invalid_locale_format",
            "A locale identifier must look like 'es-MX' (language[-Script][-REGION])",
        )
    return candidate


def is_supported(value: Locale) -> bool:
    """Whether Céluma has copy registered for this (already-parsed) locale."""
    return value in SUPPORTED_LOCALES


def resolve_locale(requested: Optional[object]) -> Locale:
    """The locale a registry lookup should actually use.

        None                 -> DEFAULT_LOCALE
        supported            -> itself
        valid, unsupported   -> DEFAULT_LOCALE
        invalid              -> InvalidLocaleError

    `None` is not an error: most call sites have no locale to offer yet, and
    forcing them to spell out the default would put the string `"es-MX"` in
    six places instead of one.
    """
    if requested is None:
        return DEFAULT_LOCALE
    parsed = parse_locale(requested)
    return parsed if is_supported(parsed) else DEFAULT_LOCALE
