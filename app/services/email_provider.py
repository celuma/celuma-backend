"""Email provider abstraction (Céluma 1.3, Phase 3, Block E, Story E2).

The boundary between "Céluma wants to send an email" and "a specific vendor
sends it". Everything upstream of this module — the delivery worker, the
template registry, the lifecycle service — depends **only** on what is
declared here. No vendor SDK is imported below, and no vendor vocabulary
appears in any name: `app/services/email_provider_ses.py` is the only module
in the codebase allowed to know Amazon's mail service exists, and
`app/services/email_provider_fake.py` is the only one allowed to know nothing
does. `tests/test_email_provider.py` asserts that from this file's source.

Why an abstraction at all, when there is exactly one real provider
--------------------------------------------------------------------
Not for a hypothetical second vendor. For three properties that are real
today:

1. **The whole backend test suite must run with no AWS credentials.** That is
   an explicit Block E requirement, and the existing `EmailService` fails it —
   `block-e-dependencies.md` records that it has never been tested at all,
   precisely because it constructs its vendor client eagerly in `__init__`. A
   provider the worker *receives*, rather than constructs, makes the fake a
   substitution instead of a monkeypatch.
2. **A provider error must reach the delivery lifecycle as a *code*, never as
   a message.** `sanitize_delivery_error_code` reduces anything address-shaped
   to `delivery_failed`, so passing `str(exc)` throws away exactly the
   diagnosis it was meant to preserve (lifecycle contract §5). Making
   `EmailProviderError.code` the only thing that crosses this boundary means
   the mapping happens once, at the adapter, and nothing downstream can leak a
   raw exception by accident.
3. **Health must be checkable without sending.** A credentials or identity
   problem should be visible from a probe, not from five failed deliveries.

Privacy
-------
Nothing in this module logs. `EmailMessage` carries a recipient address, a
subject and a body — all three are forbidden from log lines by content policy
§7 — so the type deliberately has no `__str__`/`__repr__` convenience that
would put them in a traceback, and callers pass it straight to a provider.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Stable error codes
# ---------------------------------------------------------------------------
#
# Every one of these is already what `sanitize_delivery_error_code` would
# produce (lower-case, `[a-z0-9_.:-]`, under 64 characters), so a code written
# here reaches the `error_code` column unchanged. That is the point: the
# sanitizer is a backstop against a raw provider message, not the thing that
# decides what a failure is called.
#
# `retryable` is carried alongside for **observability**, not control flow —
# see `EmailProviderError` for why.

#: The provider refused the message itself (bad address, suppressed
#: recipient, malformed content). Retrying sends the same message again.
ERROR_MESSAGE_REJECTED = "message_rejected"
#: Rate limited. The canonical retryable failure.
ERROR_SES_THROTTLED = "ses_throttled"
#: The sending identity or its MAIL FROM domain is not verified.
ERROR_MAIL_FROM_DOMAIN_NOT_VERIFIED = "mail_from_domain_not_verified"
#: The account's sending ability is paused provider-side.
ERROR_ACCOUNT_SENDING_PAUSED = "account_sending_paused"
#: The call did not complete in time. Ambiguous by nature — the provider may
#: or may not have accepted the message. See the worker's handling.
ERROR_PROVIDER_TIMEOUT = "provider_timeout"
#: The endpoint could not be reached at all.
ERROR_PROVIDER_UNAVAILABLE = "provider_unavailable"
#: Credentials are missing, expired or lack the send permission.
ERROR_PROVIDER_ACCESS_DENIED = "provider_access_denied"
#: The provider has no usable configuration (no credentials, no region, no
#: sender). Distinguished from access-denied because the fix is different.
ERROR_PROVIDER_NOT_CONFIGURED = "provider_not_configured"
#: A parameter the provider rejected. A Céluma bug, not an environment one.
ERROR_PROVIDER_INVALID_PARAMETER = "provider_invalid_parameter"
#: Anything unmapped. Never a raw message — see `EmailProviderError`.
ERROR_PROVIDER_UNKNOWN = "provider_unknown"

#: Codes for which another attempt has a realistic chance of a different
#: outcome. Used for logging and for the health probe's wording only.
RETRYABLE_ERROR_CODES: frozenset[str] = frozenset(
    {
        ERROR_SES_THROTTLED,
        ERROR_PROVIDER_TIMEOUT,
        ERROR_PROVIDER_UNAVAILABLE,
        ERROR_ACCOUNT_SENDING_PAUSED,
    }
)

#: Every code this abstraction is allowed to produce. A provider returning
#: something outside this set is a bug in that provider, and a test asserts
#: the set is closed.
KNOWN_ERROR_CODES: frozenset[str] = frozenset(
    {
        ERROR_MESSAGE_REJECTED,
        ERROR_SES_THROTTLED,
        ERROR_MAIL_FROM_DOMAIN_NOT_VERIFIED,
        ERROR_ACCOUNT_SENDING_PAUSED,
        ERROR_PROVIDER_TIMEOUT,
        ERROR_PROVIDER_UNAVAILABLE,
        ERROR_PROVIDER_ACCESS_DENIED,
        ERROR_PROVIDER_NOT_CONFIGURED,
        ERROR_PROVIDER_INVALID_PARAMETER,
        ERROR_PROVIDER_UNKNOWN,
    }
)


class EmailProviderError(RuntimeError):
    """A send did not succeed.

    `code` is one of the constants above and is the **only** thing a caller
    may persist or log. The exception's message is a fixed English sentence
    naming the code — never the provider's own wording, which can quote the
    envelope it choked on (and therefore a recipient address, which content
    policy §7 forbids from reaching a column or a log line).

    `retryable` is deliberately **advisory**. The delivery lifecycle's
    `mark_delivery_failed` schedules a backoff from the attempt count alone,
    and changing that to honour a per-error terminal flag would mean changing
    a Block D contract this block is not allowed to change. So a permanent
    failure such as `message_rejected` still consumes its remaining attempts —
    wasteful, bounded, and visible in the log line, where `retryable=false`
    says exactly what happened. Recorded as a known limitation in
    `phase-3-block-e-architecture-decision.md`.
    """

    def __init__(self, code: str, *, retryable: Optional[bool] = None):
        self.code = code
        self.retryable = (
            code in RETRYABLE_ERROR_CODES if retryable is None else retryable
        )
        super().__init__(f"Email provider failed with code {code}")


#: A conservative address screen, matching `normalize_recipient_address` in
#: the delivery service. Applied here as well because a provider must never be
#: handed something a caller assembled by hand.
_ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
#: Characters that would break out of a header. Rejected, never stripped.
_HEADER_FORBIDDEN = ("\r", "\n")


@dataclass(frozen=True)
class EmailMessage:
    """One rendered message, ready to hand to a provider.

    Frozen and validated at construction, so a provider implementation never
    has to decide whether it trusts what it was given. Everything here is
    already policy-checked: `subject` and the bodies come out of
    `app/services/email_templates.py`, which is the only thing allowed to
    produce them, and `to_address` is a `NotificationDelivery.recipient_address`
    snapshot the database normalized at materialization time.
    """

    to_address: str
    subject: str
    text_body: str
    html_body: Optional[str] = None
    #: `Name <addr>` is assembled by the provider from these two, so a
    #: display name never has to be quoted by a caller.
    from_address: str = ""
    from_name: str = ""

    def __post_init__(self) -> None:
        if not _ADDRESS_RE.match(self.to_address or ""):
            raise ValueError("EmailMessage.to_address is not a deliverable address")
        if not _ADDRESS_RE.match(self.from_address or ""):
            raise ValueError("EmailMessage.from_address is not a deliverable address")
        for label, value in (
            ("subject", self.subject),
            ("from_name", self.from_name),
        ):
            if any(char in (value or "") for char in _HEADER_FORBIDDEN):
                raise ValueError(
                    f"EmailMessage.{label} must not contain a line break "
                    "(header injection)"
                )
        if not (self.subject or "").strip():
            raise ValueError("EmailMessage.subject must not be empty")
        if not (self.text_body or "").strip():
            raise ValueError("EmailMessage.text_body must not be empty")


@dataclass(frozen=True)
class EmailSendResult:
    """What a successful send returns.

    `provider_message_id` goes straight into
    `NotificationDelivery.provider_message_id` for support correlation
    (block-e-dependencies §11). It is optional because a provider is not
    required to have one — the send is still a success without it.
    """

    provider_message_id: Optional[str] = None


@dataclass(frozen=True)
class EmailProviderHealth:
    """The answer to "could this provider send, if asked?" — established
    **without sending anything** (Story E9).

    Three separate facts rather than one boolean, because they fail for
    different reasons and have different fixes:

    | Field | False means |
    |---|---|
    | `configured` | Céluma's own settings are incomplete — a missing sender, no region. Fixable in `.env`. |
    | `credentials_present` | The process has no usable AWS credentials. Fixable in the task role or the environment. |
    | `reachable` | The provider answered a read-only call. False means an outage, a wrong region, or a permission the role is missing. |

    `error_code` is one of the module's stable codes, never a provider
    message. `detail` is a fixed English sentence safe to log.
    """

    provider: str
    configured: bool
    credentials_present: bool
    reachable: bool
    error_code: Optional[str] = None
    detail: Optional[str] = None
    #: Free-form, provider-supplied, non-sensitive facts worth logging — e.g.
    #: the region a client resolved. Never an address, never a credential.
    context: dict = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return self.configured and self.credentials_present and self.reachable


class EmailProvider(ABC):
    """What the delivery worker depends on. Two methods, nothing else.

    Implementations must:

    - raise `EmailProviderError` with a `KNOWN_ERROR_CODES` code for **every**
      failure, including ones they did not anticipate — a provider that lets a
      vendor exception escape has moved the sanitization burden onto its
      caller, which is the failure mode this boundary exists to prevent;
    - never log a recipient address, a subject, a body or a raw vendor
      exception;
    - never send anything from `health()`.
    """

    #: Short, stable identifier used in log lines and health output.
    name: str = "email"

    @abstractmethod
    def send(self, message: EmailMessage) -> EmailSendResult:
        """Deliver `message`, or raise `EmailProviderError`.

        Success means the provider **accepted** the message, not that it
        reached a mailbox. Bounce and complaint handling is out of Phase 3
        scope entirely (no webhook, no SNS topic), so `SENT` in Céluma 1.3
        means "accepted by the provider" and nothing stronger. That is stated
        here because it is the single most likely thing for a reader to assume
        wrongly.
        """

    @abstractmethod
    def health(self) -> EmailProviderHealth:
        """Report whether a send *could* succeed, without performing one.

        Must not raise: a health probe that throws is a health probe that
        takes down whatever called it. Every failure is reported through the
        returned value.
        """
