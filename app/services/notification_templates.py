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
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Tuple

from app.models.notification import NotificationType


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

NOTIFICATION_TEMPLATES: Dict[NotificationType, NotificationTemplate] = {
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
}

#: Every registered key, for schema validation and tests.
NOTIFICATION_TEMPLATE_KEYS: frozenset[str] = frozenset(
    template.key for template in NOTIFICATION_TEMPLATES.values()
)


def get_template(
    notification_type: NotificationType, template_key: str
) -> NotificationTemplate:
    """Resolve the registered template for `notification_type`.

    `template_key` must match the type's registered key. Requiring the caller
    to name both means a copy/paste that pairs the wrong key with a type is
    caught here rather than producing a plausible-looking but wrong
    notification.
    """
    template = NOTIFICATION_TEMPLATES.get(notification_type)
    if template is None:
        raise NotificationTemplateError(
            "unknown_template",
            f"No template registered for notification type {notification_type}",
        )
    if template.key != template_key:
        raise NotificationTemplateError(
            "template_key_mismatch",
            f"Template key does not match the registered template for {notification_type}",
        )
    return template
