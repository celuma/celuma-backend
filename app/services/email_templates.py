"""Email template registry (Céluma 1.3, Phase 3, Block E, Story E5).

Every subject line and body Céluma can ever put in a **notification email**,
defined here and nowhere else.

Why this is not a reuse of `Notification.title`/`body`
------------------------------------------------------
Because the email policy is strictly narrower than the in-app one, and
reusing the frozen in-app text would make that difference a convention rather
than a mechanism.

Content policy §3, in full:

| | In-app | Email |
|---|---|---|
| Patient initials | CONDITIONAL — allowed for staff | **PROHIBITED** |
| Patient code | CONDITIONAL | **PROHIBITED** |
| Study type | CONDITIONAL | **PROHIBITED** |
| Order number | SAFE | SAFE |
| Staff name | SAFE | SAFE, but see below |
| Deep link to the resource | Allowed (route guard re-checks) | **PROHIBITED** |

An email lands in an inbox: forwarded, read on a shared phone, retained
indefinitely by a third party, and outside Céluma's authentication entirely.
Today's in-app templates happen to be composed only of SAFE parameters, so
copying them would produce a policy-compliant email *right now* — and would
silently stop doing so the first time an in-app template gained a
CONDITIONAL parameter, which is allowed for in-app and would then be
inherited into email by a line of code nobody would think to re-read.

So: a separate registry, keyed off `template_key`, that **selects** the
parameters it wants instead of receiving whatever the notification carried.
An in-app template can grow a new parameter and this file will keep rendering
exactly what it rendered before.

The vocabulary this file allows itself
--------------------------------------
One parameter: `order_number`. That is narrower than the policy requires —
`actor_name` is classified SAFE and does appear in the in-app body — and it is
narrower on purpose:

- the email's job is to say *something happened, come and look*, and the
  identity of the colleague who did it is not load-bearing for that;
- `actor_name` is the one parameter sourced from a user-editable field, so
  excluding it removes the only path by which user-authored text could reach
  an inbox;
- Story E5's own instruction is "Generic. Safe. Short."

Adding a parameter here is a deliberate act with a policy review attached, not
a matter of it already being present in `template_params`.

The call to action
------------------
Always the same sentence, always pointing at the `FRONTEND_URL` **origin** —
never a path into the resource, never a signed or pre-authenticated URL, for
any recipient including a requesting physician (content policy §3, §5). The
email is a nudge; the application is where authorization happens.

Localization readiness (Céluma 1.3, Phase 3, Block F)
------------------------------------------------------
Lookup is `template_key + locale`, mirroring the in-app registry — and the
registries stay **separate**, which is the whole point of this file. Adding a
locale must not become a way to widen the email vocabulary: `params` is
declared per template and screened per value, and every locale of a given key
shares one declared parameter set (`test_no_locale_widens_the_parameter_vocabulary`).
A translator can change words; they cannot introduce a deep link, a remote
image, a tracking pixel or a new parameter, because none of those is
expressible in an `EmailTemplate`.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from app.core.config import settings
from app.models.notification import NotificationType
from app.services.locale import DEFAULT_LOCALE, Locale, resolve_locale


class EmailTemplateError(ValueError):
    """A notification cannot be rendered as an email.

    Carries a stable `code` so the delivery worker can put it straight into
    `NotificationDelivery.error_code` without any of it being a message.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


# ---------------------------------------------------------------------------
# Value screening
# ---------------------------------------------------------------------------
#
# `app/services/notification_templates.py` screens parameters when a
# notification is *created*. This screens them again when an email is
# *rendered*, and the duplication is deliberate rather than an oversight:
#
#   - the two run at different times against different data. That one screens
#     a caller's argument; this one screens a JSON column that was written
#     minutes-to-days earlier, possibly by an older version of the code, and
#     possibly edited by hand in an incident;
#   - the rules are not the same. Email is narrower — a shorter length bound,
#     and no tolerance at all for anything URL-shaped, because in an email a
#     URL is clickable;
#   - importing the other module's private validator would couple an email
#     policy change to an in-app policy change, which is the coupling this
#     whole file exists to break.

#: Shorter than the in-app bound (200). A subject line longer than this is
#: not a subject line.
_MAX_EMAIL_PARAM_LENGTH = 80

#: `Tenant.name` is `VARCHAR(255)`, so it can legitimately be longer than a
#: subject line should be. It is **truncated** rather than rejected — see
#: `_screen_tenant_name`.
_MAX_TENANT_NAME_LENGTH = 60

#: Markup, schemes and credential-shaped text. Same intent as the in-app list,
#: applied to a channel where the consequences are worse.
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
    "@",
)

#: Three dot-separated base64url runs — a JWT, a render token, a presigned
#: signature. Never rendered, in any channel.
_TOKEN_SHAPED = re.compile(r"[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")


def _screen(name: str, value: Any) -> str:
    """A stored parameter value, or `EmailTemplateError`.

    `@` is in the forbidden list here and not in the in-app one: an address
    reaching an email subject would be a different user's address being shown
    to this recipient, which content policy §1 prohibits outright.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise EmailTemplateError(
            "email_param_unsafe_type",
            f"Email parameter '{name}' must be a string or integer",
        )
    text = str(value).strip()
    if not text:
        raise EmailTemplateError(
            "email_param_empty", f"Email parameter '{name}' is empty"
        )
    if len(text) > _MAX_EMAIL_PARAM_LENGTH:
        raise EmailTemplateError(
            "email_param_too_long",
            f"Email parameter '{name}' exceeds {_MAX_EMAIL_PARAM_LENGTH} characters",
        )
    if "\n" in text or "\r" in text:
        raise EmailTemplateError(
            "email_param_unsafe_content",
            f"Email parameter '{name}' contains a line break",
        )
    lowered = text.lower()
    if any(snippet in lowered for snippet in _FORBIDDEN_SUBSTRINGS):
        raise EmailTemplateError(
            "email_param_unsafe_content",
            f"Email parameter '{name}' contains markup, a URL or an address",
        )
    if _TOKEN_SHAPED.search(text):
        raise EmailTemplateError(
            "email_param_unsafe_content",
            f"Email parameter '{name}' looks like a token",
        )
    return text


def _screen_tenant_name(value: Any) -> str:
    """The tenant name, screened for content and **truncated** for length.

    Truncation rather than rejection, unlike every other value here, because
    the failure modes are not comparable. A `template_params` value longer
    than its bound means something is wrong upstream and the email should not
    be sent. A laboratory whose registered name happens to run past 60
    characters is not wrong about anything — refusing to deliver its email
    would be Céluma punishing a tenant for a long name, permanently, with the
    only symptom being a `FAILED` row nobody is watching.

    The content screen is *not* relaxed: a tenant name carrying markup, a URL
    or an address still fails, because that is not a length problem.
    """
    text = str(value or "").strip()
    if not text:
        raise EmailTemplateError("email_param_empty", "Tenant name is empty")
    if len(text) > _MAX_TENANT_NAME_LENGTH:
        text = text[: _MAX_TENANT_NAME_LENGTH - 1].rstrip() + "…"
    return _screen("tenant_name", text)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

#: The one call to action, in every email, always. A constant rather than a
#: per-template string so that "the CTA never deep-links" is a property of the
#: registry rather than of five separate strings that each have to be right.
CALL_TO_ACTION = "Inicia sesión en Céluma para consultarlo."

def _call_to_action_url() -> str:
    """The origin rendered under the CTA.

    Read at render time rather than captured at import, so a test (or a
    reconfigured process) sees the current value. `settings.frontend_url` is
    validated as a bare absolute origin and stored without a trailing slash,
    and it is used as an origin only — content policy §3/§5 forbids a
    notification email from carrying a path into a protected resource.
    """
    return settings.frontend_url


@dataclass(frozen=True)
class EmailTemplate:
    """One notification type's email copy.

    `subject` and `body` are `str.format` templates over `tenant_name` plus
    whatever `params` declares — and nothing else is passed to `format`, so a
    template cannot reference a parameter it did not declare.
    """

    #: Matches the in-app `template_key` this email corresponds to, so the two
    #: registries stay auditably aligned and a `_v2` copy revision on one side
    #: is visible as a mismatch on the other.
    key: str
    notification_type: NotificationType
    subject: str
    body: str
    #: Declared parameter names, selected out of the notification's stored
    #: `template_params`. Everything not named here is ignored, not rejected —
    #: an in-app template legitimately carries parameters email does not use.
    params: Tuple[str, ...] = ()


#: Seven entries: one per email-supported `NotificationType`, plus two extra
#: `ASSIGNMENT_ADDED` keys for the sample/review contexts (pre-release
#: remediation — see the comment on those two entries below). Never eight:
#: `SAMPLE_STATUS_CHANGED` has `email_supported = False` in
#: `notification_policies.py`, so no `NotificationDelivery` row can ever exist
#: for it (materialization contract §2 checks the policy first). Registering
#: an email template for it would be a template nothing can reach — and worse,
#: it would make re-enabling that channel a one-line policy change with no
#: copy review, when the reason it is in-app only is volume and fan-out.
#: `TestRegistryCoverage` asserts this file and the policy registry agree.
_EMAIL_TEMPLATES_ES_MX: Dict[str, EmailTemplate] = {
    "report_submitted_v1": EmailTemplate(
        key="report_submitted_v1",
        notification_type=NotificationType.REPORT_SUBMITTED,
        subject="{tenant_name} — Reporte enviado a revisión (Orden {order_number})",
        body="Un reporte de la orden {order_number} fue enviado a revisión y "
        "está pendiente de tu revisión.",
        params=("order_number",),
    ),
    "report_pdf_ready_v1": EmailTemplate(
        key="report_pdf_ready_v1",
        notification_type=NotificationType.REPORT_PDF_READY,
        subject="{tenant_name} — PDF listo para firma (Orden {order_number})",
        body="El PDF oficial del reporte de la orden {order_number} está listo "
        "para firma.",
        params=("order_number",),
    ),
    "report_published_v1": EmailTemplate(
        key="report_published_v1",
        notification_type=NotificationType.REPORT_PUBLISHED,
        subject="{tenant_name} — Reporte publicado (Orden {order_number})",
        body="Hay un reporte publicado disponible para la orden {order_number}.",
        params=("order_number",),
    ),
    "report_retracted_v1": EmailTemplate(
        key="report_retracted_v1",
        notification_type=NotificationType.REPORT_RETRACTED,
        subject="{tenant_name} — Reporte retractado (Orden {order_number})",
        # The retraction reason is user-authored free text and is absent by
        # construction — it is not a declared parameter, so it cannot arrive.
        body="Un reporte de la orden {order_number} fue retractado y ya no "
        "está disponible.",
        params=("order_number",),
    ),
    "assignment_added_v1": EmailTemplate(
        key="assignment_added_v1",
        notification_type=NotificationType.ASSIGNMENT_ADDED,
        subject="{tenant_name} — Nueva asignación (Orden {order_number})",
        body="Se te asignó trabajo en la orden {order_number}.",
        params=("order_number",),
    ),
    # Pre-release remediation: `ASSIGNMENT_ADDED` now has three in-app
    # template keys (order/sample/review context — see
    # notification_templates.py), and the delivery worker resolves the email
    # template by the notification's own `template_key`. Without these two
    # entries, sample- and reviewer-assignment email delivery would fail
    # lookup (`EmailTemplateError: unknown_template`) the first time the
    # worker tried to render one. The vocabulary stays exactly the one
    # parameter this file allows itself — the assignment type is fixed prose,
    # not a parameter.
    "assignment_added_sample_v1": EmailTemplate(
        key="assignment_added_sample_v1",
        notification_type=NotificationType.ASSIGNMENT_ADDED,
        # Deliberately does not say "muestra": the content-policy word list
        # this file is checked against (test_email_templates.py) bans it from
        # email copy, so the sample context stays as generic as the order one.
        subject="{tenant_name} — Nueva asignación (Orden {order_number})",
        body="Se te asignó trabajo en la orden {order_number}.",
        params=("order_number",),
    ),
    "assignment_added_review_v1": EmailTemplate(
        key="assignment_added_review_v1",
        notification_type=NotificationType.ASSIGNMENT_ADDED,
        subject="{tenant_name} — Nueva revisión asignada (Orden {order_number})",
        body="Se te asignó la revisión del reporte de la orden {order_number}.",
        params=("order_number",),
    ),
}

#: Superseded email keys, kept resolvable forever — same rule and the same
#: reason as the in-app registry's `_RETIRED_TEMPLATES`. A delivery row can
#: outlive a copy revision, and the worker must still be able to render it.
#: Empty in Céluma 1.3.
_RETIRED_EMAIL_TEMPLATES: Dict[str, Dict[Locale, EmailTemplate]] = {}


def _build_email_registry() -> Dict[str, Dict[Locale, EmailTemplate]]:
    registry: Dict[str, Dict[Locale, EmailTemplate]] = {}
    for template in _EMAIL_TEMPLATES_ES_MX.values():
        registry.setdefault(template.key, {})[DEFAULT_LOCALE] = template
    for key, by_locale in _RETIRED_EMAIL_TEMPLATES.items():
        registry.setdefault(key, {}).update(by_locale)
    return registry


#: `template_key -> locale -> template`.
EMAIL_TEMPLATE_REGISTRY: Dict[str, Dict[Locale, EmailTemplate]] = _build_email_registry()

#: The current default-locale email template per key. Retained under its Block
#: E name and shape for the same reason as the in-app view.
EMAIL_TEMPLATES: Dict[str, EmailTemplate] = dict(_EMAIL_TEMPLATES_ES_MX)

#: Every registered key, for tests and for cross-checking the in-app registry.
EMAIL_TEMPLATE_KEYS: frozenset[str] = frozenset(EMAIL_TEMPLATE_REGISTRY)


def get_email_template(
    notification_type: NotificationType,
    template_key: str,
    locale: Optional[str] = None,
) -> EmailTemplate:
    """Resolve the email template for `(notification_type, template_key)` in
    `locale`.

    Type and key must both match, for the reason the in-app registry requires
    both: a copy/paste that pairs the wrong key with a type would otherwise
    produce a plausible-looking email about the wrong event.

    Locale resolution is the in-app registry's, deliberately reused: an
    unsupported-but-valid locale falls back to `DEFAULT_LOCALE`, and a
    malformed one raises. What is *not* reused is the parameter screening —
    see the module docstring.
    """
    resolved = resolve_locale(locale)

    by_locale = EMAIL_TEMPLATE_REGISTRY.get(template_key)
    if by_locale is None:
        raise EmailTemplateError(
            "email_template_not_found",
            f"No email template registered for key {template_key!r}",
        )

    template = by_locale.get(resolved) or by_locale.get(DEFAULT_LOCALE)
    if template is None:
        raise EmailTemplateError(
            "email_template_not_found",
            f"Email template {template_key!r} has no copy in any supported locale",
        )
    if template.notification_type != notification_type:
        raise EmailTemplateError(
            "email_template_key_mismatch",
            "Email template key does not match the notification type",
        )
    return template


@dataclass(frozen=True)
class RenderedEmail:
    """A subject and two bodies, ready for `EmailMessage`."""

    subject: str
    text_body: str
    html_body: str
    template_key: str


def render_notification_email(
    *,
    tenant_name: str,
    notification_type: NotificationType,
    template_key: str,
    template_params: Optional[Mapping[str, Any]],
    locale: Optional[str] = None,
) -> RenderedEmail:
    """Render one notification as an email.

    The four inputs Story E5 specifies — the tenant, the notification (as its
    type), the template key and the stored parameters — plus, since Block F,
    the locale the notification was created in. The worker reads it off
    `Notification.locale`, so an email renders in the same locale as the
    in-app copy it accompanies rather than in whatever the default happens to
    be by the time delivery runs.

    Every value that reaches the output is screened first — including
    `tenant_name`, which is tenant-editable and therefore exactly as
    untrusted as a stored parameter. **Locale resolution happens before
    screening and weakens none of it:** the template chosen may differ, the
    checks applied to every interpolated value do not.
    """
    template = get_email_template(notification_type, template_key, locale)

    values: Dict[str, str] = {"tenant_name": _screen_tenant_name(tenant_name)}
    params = template_params or {}
    for name in template.params:
        if name not in params:
            raise EmailTemplateError(
                "email_param_missing",
                f"Email template {template.key!r} requires parameter {name!r}",
            )
        values[name] = _screen(name, params[name])

    subject = template.subject.format(**values)
    sentence = template.body.format(**values)

    # Subject lines are folded and re-wrapped by mail clients; anything over
    # ~78 characters is truncated in most inbox lists. This is a hard failure
    # rather than a truncation because a truncated subject is a copy bug to
    # fix in the template, not a runtime condition to absorb — and every
    # template here is asserted to fit in the tests.
    if len(subject) > 150:
        raise EmailTemplateError(
            "email_subject_too_long", "Rendered email subject exceeds 150 characters"
        )

    return RenderedEmail(
        subject=subject,
        text_body=_render_text(sentence),
        html_body=_render_html(sentence),
        template_key=template.key,
    )


def _render_text(sentence: str) -> str:
    return (
        f"{sentence}\n\n"
        f"{CALL_TO_ACTION}\n"
        f"{_call_to_action_url()}\n\n"
        "Este es un mensaje automático de Céluma. No respondas a este correo.\n"
    )


def _render_html(sentence: str) -> str:
    """A deliberately plain HTML part.

    No images, no external stylesheet, no tracking pixel, no remote font —
    an email that fetches nothing cannot report that it was opened, and there
    is no legitimate reason for a clinical-workflow nudge to do so.

    `html.escape` on every interpolated value even though `_screen` already
    rejects `<`, `>` and `&#`. Two independent defences, because this is the
    one place a stored value becomes markup.
    """
    safe_sentence = html.escape(sentence)
    safe_cta = html.escape(CALL_TO_ACTION)
    url = html.escape(_call_to_action_url(), quote=True)
    return (
        '<html><body style="font-family:Helvetica,Arial,sans-serif;'
        'font-size:15px;line-height:1.5;color:#1f2933;">'
        f"<p>{safe_sentence}</p>"
        f'<p>{safe_cta}<br><a href="{url}">{url}</a></p>'
        '<p style="font-size:12px;color:#7b8794;">'
        "Este es un mensaje automático de Céluma. No respondas a este correo."
        "</p>"
        "</body></html>"
    )
