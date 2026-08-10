"""Backend-controlled notification template registry (Céluma 1.3, Phase 3,
Block B).

Every user-facing string a notification can ever carry is defined here, in
Spanish, as a fixed template parameterized only by explicitly SAFE values.
Production call sites cannot supply a rendered `title`/`body` — they name a
template and pass parameters, which this module validates before rendering.

Why a registry rather than caller-supplied strings
--------------------------------------------------
The content/privacy policy
(docs/celuma-1.3/phase-3-block-a/notification-content-privacy-policy.md)
prohibits diagnosis, clinical descriptions, patient names and user-authored
free text from ever reaching a notification. A signature that accepts a
`title: str` makes that policy a code-review convention; a registry makes it
a type error. The two known leak vectors the policy names — pathologist-
renamed report titles and free-text fields (comments, retraction reasons) —
are both closed structurally: there is no parameter through which they could
arrive, and every parameter that does arrive is length-bounded and screened
for markup, URLs and token-shaped values.

`template_key` and `template_params` are persisted alongside the rendered
text (content policy §8, the "hybrid" option), so a future localization pass
can re-render from structured data without rewriting what a user actually
saw.

Localization readiness (Céluma 1.3, Phase 3, Block F)
------------------------------------------------------
Lookup is now `template_key + locale`, not `template_key` alone. Céluma 1.3
still ships exactly one locale — `es-MX`, see `app/services/locale.py` — so
this changes no rendered string; it changes the *shape*, so a second locale
becomes a registry entry rather than a refactor of every call site.

Two rules the structure enforces rather than documents:

**A published key is immutable.** Once `report_published_v1` has rendered a
notification that is now sitting frozen in somebody's inbox, its copy never
changes in place. Corrected copy ships as `report_published_v2`, added to
`NOTIFICATION_TEMPLATE_REGISTRY` beside the `_v1` entry, and
`CURRENT_TEMPLATE_KEY` is repointed at it. The `_v1` entry stays resolvable
forever — a historical notification, an audit query, or a delivery row that
outlived a copy revision must all still be able to look up what produced the
text. Deprecation here means "no longer selected for new notifications",
never "removed".

**No call site picks a locale.** `NotificationCommand` has no locale field,
exactly as it has no `title` field: the two would be the same class of bypass.
The service renders in `DEFAULT_LOCALE` and records which locale that was on
`Notification.locale`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from app.models.notification import NotificationType
from app.services.locale import DEFAULT_LOCALE, Locale, resolve_locale


class NotificationTemplateError(ValueError):
    """A caller supplied a template key or parameter set this registry
    rejects. Carries a stable `code` so it can be logged without echoing the
    offending value."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


# ---------------------------------------------------------------------------
# Safe-parameter validation
# ---------------------------------------------------------------------------

#: Hard ceiling applied on top of each parameter's own declared maximum, so a
#: template that forgets to set a tight bound still cannot produce an
#: unbounded title.
ABSOLUTE_MAX_PARAM_LENGTH = 200

#: Substrings that must never appear in a rendered notification. Markup would
#: be rendered by the frontend; a scheme-bearing URL would turn a
#: notification into a navigation vector; token-shaped material must never be
#: embedded at all (content policy §5).
_FORBIDDEN_SUBSTRINGS = (
    "<",
    ">",
    "&#",
    "://",
    "javascript:",
    "data:",
    "onerror=",
    "onload=",
    "bearer ",
)

#: Rejects anything shaped like a JWT (three dot-separated base64url runs) —
#: the render token, access tokens and presigned-URL signatures all match.
_TOKEN_SHAPED = re.compile(r"[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")


@dataclass(frozen=True)
class TemplateParam:
    """One declared, safe parameter of a template."""

    name: str
    max_length: int
    required: bool = True


@dataclass(frozen=True)
class NotificationTemplate:
    """A fixed Spanish title/body pair plus its declared parameters."""

    key: str
    notification_type: NotificationType
    title: str
    body: str | None
    params: Tuple[TemplateParam, ...]

    @property
    def allowed_param_names(self) -> frozenset[str]:
        return frozenset(param.name for param in self.params)

    @property
    def required_param_names(self) -> frozenset[str]:
        return frozenset(param.name for param in self.params if param.required)


def _validate_value(param: TemplateParam, value: Any) -> str:
    """Coerce and screen a single parameter value.

    Only flat scalars are accepted. A dict/list would let a caller smuggle a
    structure whose repr lands in the rendered string, so nested values are
    rejected outright rather than stringified.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise NotificationTemplateError(
            "unsafe_param_type",
            f"Parameter '{param.name}' must be a string or integer",
        )

    text = str(value)
    if not text.strip():
        raise NotificationTemplateError(
            "empty_param", f"Parameter '{param.name}' must not be empty"
        )

    limit = min(param.max_length, ABSOLUTE_MAX_PARAM_LENGTH)
    if len(text) > limit:
        raise NotificationTemplateError(
            "param_too_long",
            f"Parameter '{param.name}' exceeds its maximum length of {limit}",
        )

    if "\n" in text or "\r" in text:
        raise NotificationTemplateError(
            "unsafe_param_content",
            f"Parameter '{param.name}' must not contain line breaks",
        )

    lowered = text.lower()
    if any(snippet in lowered for snippet in _FORBIDDEN_SUBSTRINGS):
        raise NotificationTemplateError(
            "unsafe_param_content",
            f"Parameter '{param.name}' contains markup, a URL or credential-like text",
        )

    if _TOKEN_SHAPED.search(text):
        raise NotificationTemplateError(
            "unsafe_param_content",
            f"Parameter '{param.name}' looks like a token and must never be rendered",
        )

    return text


def validate_params(
    template: NotificationTemplate, params: Mapping[str, Any]
) -> Dict[str, str]:
    """Return the validated, stringified parameter set for `template`.

    Rejects unknown names, missing required names, oversized values, markup,
    URLs, token-shaped values and nested structures.
    """
    unknown = set(params) - template.allowed_param_names
    if unknown:
        raise NotificationTemplateError(
            "unknown_param",
            f"Unknown template parameter(s): {sorted(unknown)}",
        )

    missing = template.required_param_names - set(params)
    if missing:
        raise NotificationTemplateError(
            "missing_param",
            f"Missing required template parameter(s): {sorted(missing)}",
        )

    by_name = {param.name: param for param in template.params}
    return {name: _validate_value(by_name[name], value) for name, value in params.items()}


def render(
    template: NotificationTemplate, params: Mapping[str, Any]
) -> Tuple[str, str | None, Dict[str, str]]:
    """Validate `params` and render the frozen Spanish title/body.

    Returns `(title, body, validated_params)`. The body is `None` when the
    template declares none, or when an optional parameter it needs is absent
    — a template never renders a placeholder that has no value.
    """
    safe = validate_params(template, params)

    title = template.title.format(**safe)
    body: str | None = None
    if template.body is not None:
        try:
            body = template.body.format(**safe)
        except KeyError:
            body = None

    if len(title) > 255:
        raise NotificationTemplateError(
            "rendered_title_too_long", "Rendered title exceeds 255 characters"
        )
    if body is not None and len(body) > 1000:
        raise NotificationTemplateError(
            "rendered_body_too_long", "Rendered body exceeds 1000 characters"
        )
    return title, body, safe


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
#
# One template per approved NotificationType. Copy is deliberately generic:
# it names the workflow event and the order/case number (SAFE per the content
# policy's field classification) and nothing else. Opening the notification
# is what takes the user to the resource, where normal tenant/RBAC checks
# apply.
#
# Keys carry a `_v1` suffix so corrected copy can ship as `_v2` without
# rewriting the frozen text of notifications already delivered.

_ORDER_NUMBER = TemplateParam("order_number", max_length=50)
_ACTOR_NAME = TemplateParam("actor_name", max_length=120)

#: The `es-MX` copy, one template per approved NotificationType.
#:
#: Keyed by type for readability and because Céluma 1.3 has exactly one live
#: key per type. `NOTIFICATION_TEMPLATE_REGISTRY` below is the structure
#: lookups actually go through, and it is derived from this plus any retired
#: keys — so a `_v2` revision adds an entry to `_RETIRED_TEMPLATES` and edits
#: this map, and nothing else moves.
_TEMPLATES_ES_MX: Dict[NotificationType, NotificationTemplate] = {
    NotificationType.REPORT_SUBMITTED: NotificationTemplate(
        key="report_submitted_v1",
        notification_type=NotificationType.REPORT_SUBMITTED,
        title="Reporte listo para revisión — Orden {order_number}",
        body="El reporte fue enviado a revisión por {actor_name}.",
        params=(_ORDER_NUMBER, _ACTOR_NAME),
    ),
    NotificationType.REPORT_PDF_READY: NotificationTemplate(
        key="report_pdf_ready_v1",
        notification_type=NotificationType.REPORT_PDF_READY,
        title="PDF oficial listo — Orden {order_number}",
        body="El PDF oficial del reporte está listo para firma.",
        params=(_ORDER_NUMBER,),
    ),
    NotificationType.REPORT_PUBLISHED: NotificationTemplate(
        key="report_published_v1",
        notification_type=NotificationType.REPORT_PUBLISHED,
        title="Reporte publicado — Orden {order_number}",
        body="El reporte fue publicado y firmado por {actor_name}.",
        params=(_ORDER_NUMBER, _ACTOR_NAME),
    ),
    NotificationType.REPORT_RETRACTED: NotificationTemplate(
        key="report_retracted_v1",
        notification_type=NotificationType.REPORT_RETRACTED,
        title="Reporte retractado — Orden {order_number}",
        # The retraction reason is deliberately absent: it is user-authored
        # free text (content policy §4).
        body="El reporte fue retractado por {actor_name}.",
        params=(_ORDER_NUMBER, _ACTOR_NAME),
    ),
    NotificationType.ASSIGNMENT_ADDED: NotificationTemplate(
        key="assignment_added_v1",
        notification_type=NotificationType.ASSIGNMENT_ADDED,
        title="Nueva asignación — Orden {order_number}",
        body="{actor_name} te asignó a esta orden.",
        params=(_ORDER_NUMBER, _ACTOR_NAME),
    ),
    NotificationType.SAMPLE_STATUS_CHANGED: NotificationTemplate(
        key="sample_status_changed_v2",
        notification_type=NotificationType.SAMPLE_STATUS_CHANGED,
        title="Muestra actualizada — Orden {order_number}",
        body="La muestra {sample_code} cambió a estado {new_status_label}.",
        params=(
            _ORDER_NUMBER,
            TemplateParam("sample_code", max_length=50),
            TemplateParam("new_status_label", max_length=30),
        ),
    ),
}

#: Context-specific `ASSIGNMENT_ADDED` keys, pre-release remediation.
#:
#: `assignment_added_v1` (above) stays the type's single `CURRENT_TEMPLATE_KEY`
#: entry and keeps being used for **order**-context assignment — its meaning
#: does not change, so every historical notification created under it, for
#: any of the three contexts, stays byte-identical (template immutability).
#:
#: `CURRENT_TEMPLATE_KEY` is 1:1 per `NotificationType` and cannot hold three
#: simultaneous "current" keys for one type, so the sample and reviewer
#: contexts are not routed through it: their integration functions
#: (`notify_sample_assignments_added`, `notify_order_reviewers_added` in
#: `notification_integrations/assignments.py`) import these constants
#: directly.
ASSIGNMENT_ADDED_SAMPLE_TEMPLATE_KEY = "assignment_added_sample_v1"
ASSIGNMENT_ADDED_REVIEW_TEMPLATE_KEY = "assignment_added_review_v1"

_ASSIGNMENT_CONTEXT_TEMPLATES_ES_MX: Dict[str, NotificationTemplate] = {
    ASSIGNMENT_ADDED_SAMPLE_TEMPLATE_KEY: NotificationTemplate(
        key=ASSIGNMENT_ADDED_SAMPLE_TEMPLATE_KEY,
        notification_type=NotificationType.ASSIGNMENT_ADDED,
        title="Nueva asignación de muestra — Orden {order_number}",
        body="{actor_name} te asignó la muestra {sample_code} de esta orden.",
        params=(_ORDER_NUMBER, _ACTOR_NAME, TemplateParam("sample_code", max_length=50)),
    ),
    ASSIGNMENT_ADDED_REVIEW_TEMPLATE_KEY: NotificationTemplate(
        key=ASSIGNMENT_ADDED_REVIEW_TEMPLATE_KEY,
        notification_type=NotificationType.ASSIGNMENT_ADDED,
        title="Nueva revisión asignada — Orden {order_number}",
        body="{actor_name} te asignó la revisión del reporte de esta orden.",
        params=(_ORDER_NUMBER, _ACTOR_NAME),
    ),
}

#: Superseded template keys, kept resolvable forever.
#:
#: `sample_status_changed_v1` is retired here (pre-release remediation): it
#: persisted the raw `SampleState` enum value as final Spanish text
#: (`new_state`, e.g. "PROCESSING") instead of a translated label. It is not
#: edited in place — a notification created under it is frozen in somebody's
#: inbox — so `sample_status_changed_v2` (above) is registered instead and
#: `CURRENT_TEMPLATE_KEY` is repointed at it. This entry stays resolvable
#: forever for the historical rows and any delivery row that outlives the
#: revision.
_RETIRED_TEMPLATES: Dict[str, Dict[Locale, NotificationTemplate]] = {
    "sample_status_changed_v1": {
        DEFAULT_LOCALE: NotificationTemplate(
            key="sample_status_changed_v1",
            notification_type=NotificationType.SAMPLE_STATUS_CHANGED,
            title="Muestra actualizada — Orden {order_number}",
            body="La muestra {sample_code} cambió a estado {new_state}.",
            params=(
                _ORDER_NUMBER,
                TemplateParam("sample_code", max_length=50),
                TemplateParam("new_state", max_length=30),
            ),
        ),
    },
}


def _build_registry() -> Dict[str, Dict[Locale, NotificationTemplate]]:
    """`template_key -> locale -> template`, the shape every lookup uses.

    Assembled rather than written out because with one locale the literal form
    would be six dictionaries of one entry each, and the invariant that matters
    — every current key has copy in every supported locale — is better checked
    than transcribed.
    """
    registry: Dict[str, Dict[Locale, NotificationTemplate]] = {}
    for template in _TEMPLATES_ES_MX.values():
        registry.setdefault(template.key, {})[DEFAULT_LOCALE] = template
    for template in _ASSIGNMENT_CONTEXT_TEMPLATES_ES_MX.values():
        registry.setdefault(template.key, {})[DEFAULT_LOCALE] = template
    for key, by_locale in _RETIRED_TEMPLATES.items():
        registry.setdefault(key, {}).update(by_locale)
    return registry


#: The localization-ready lookup structure: `template_key -> locale ->
#: template`. Includes retired keys, so a historical notification's key still
#: resolves after its copy has been superseded.
NOTIFICATION_TEMPLATE_REGISTRY: Dict[str, Dict[Locale, NotificationTemplate]] = (
    _build_registry()
)

#: The key new notifications of each type are created with. **This is where
#: template-version selection lives**: shipping `_v2` copy means registering it
#: and repointing this map; the `_v1` entry is not touched.
CURRENT_TEMPLATE_KEY: Dict[NotificationType, str] = {
    notification_type: template.key
    for notification_type, template in _TEMPLATES_ES_MX.items()
}

#: The current default-locale template per type.
#:
#: Retained under its Block B name and shape because it is the natural way to
#: ask "what copy does this event produce today", which is what every existing
#: consumer wants. It is a *view*: `NOTIFICATION_TEMPLATE_REGISTRY` is the
#: registry, and this cannot see a retired key.
NOTIFICATION_TEMPLATES: Dict[NotificationType, NotificationTemplate] = dict(
    _TEMPLATES_ES_MX
)

#: Every registered key, current and retired, for schema validation and tests.
NOTIFICATION_TEMPLATE_KEYS: frozenset[str] = frozenset(NOTIFICATION_TEMPLATE_REGISTRY)


def get_template(
    notification_type: NotificationType,
    template_key: str,
    locale: Optional[str] = None,
) -> NotificationTemplate:
    """Resolve the registered template for `(notification_type, template_key)`
    in `locale`.

    Requiring the caller to name both type and key means a copy/paste that
    pairs the wrong key with a type is caught here rather than producing a
    plausible-looking but wrong notification.

    `locale` follows `resolve_locale`: `None` and any well-formed but
    unsupported identifier both resolve to `DEFAULT_LOCALE`; a malformed one
    raises `InvalidLocaleError` rather than silently becoming the default.
    Falling back is then a second, separate step — a key registered in some
    locale but not this one still yields default-locale copy rather than
    nothing, which is what keeps a partially translated registry usable.
    """
    resolved = resolve_locale(locale)

    by_locale = NOTIFICATION_TEMPLATE_REGISTRY.get(template_key)
    if by_locale is None:
        raise NotificationTemplateError(
            "unknown_template",
            f"No template registered for key {template_key!r}",
        )

    template = by_locale.get(resolved) or by_locale.get(DEFAULT_LOCALE)
    if template is None:
        raise NotificationTemplateError(
            "unknown_template",
            f"Template {template_key!r} has no copy in any supported locale",
        )
    if template.notification_type != notification_type:
        raise NotificationTemplateError(
            "template_key_mismatch",
            f"Template key does not match the registered template for {notification_type}",
        )
    return template
