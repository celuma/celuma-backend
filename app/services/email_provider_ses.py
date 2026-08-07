"""AWS SES email provider (Céluma 1.3, Phase 3, Block E, Story E3).

**The only module in Céluma that knows notification email is sent by SES.**
`app/services/email.py` also uses boto3, but it predates this block and serves
the invitation/password-reset flows; nothing in the notification delivery path
imports it.

The whole job of this file is translation, in both directions:

    EmailMessage           ->  ses.send_email(...)
    botocore exception     ->  EmailProviderError(stable code)

The second direction is the load-bearing one. A `botocore` `ClientError`
message routinely quotes the envelope it rejected — which means a recipient
address — and content policy §7 forbids that reaching a log line or the
`error_code` column. `sanitize_delivery_error_code` would catch it (it refuses
address-shaped input wholesale), but only by throwing the diagnosis away and
storing `delivery_failed`. Mapping here means the column ends up with
`message_rejected`, which is both safe *and* useful.

Nothing in this module logs. Every failure leaves as an exception carrying a
code, and the worker owns the log line — so there is exactly one place where a
delivery failure is recorded, and it is a place with the delivery id in scope.
"""
from __future__ import annotations

from typing import Optional

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionError as BotoConnectionError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    PartialCredentialsError,
    ReadTimeoutError,
)

from app.core.config import settings
from app.services.email_provider import (
    ERROR_ACCOUNT_SENDING_PAUSED,
    ERROR_MAIL_FROM_DOMAIN_NOT_VERIFIED,
    ERROR_MESSAGE_REJECTED,
    ERROR_PROVIDER_ACCESS_DENIED,
    ERROR_PROVIDER_INVALID_PARAMETER,
    ERROR_PROVIDER_NOT_CONFIGURED,
    ERROR_PROVIDER_TIMEOUT,
    ERROR_PROVIDER_UNAVAILABLE,
    ERROR_PROVIDER_UNKNOWN,
    ERROR_SES_THROTTLED,
    EmailMessage,
    EmailProvider,
    EmailProviderError,
    EmailProviderHealth,
    EmailSendResult,
)

# ---------------------------------------------------------------------------
# Exception mapping
# ---------------------------------------------------------------------------
#
# Keyed on `ClientError.response["Error"]["Code"]`, which is a stable API
# contract — unlike the human-readable `Message` beside it, which is not, and
# which is the field that can echo an address back.
#
# Both the SES v1 (`ses`) and SESv2 (`sesv2`) spellings are listed where they
# differ, so switching API version later is a client change and not a silent
# loss of every mapping.

_CLIENT_ERROR_CODES: dict[str, str] = {
    # Rate limiting. SES v1 answers `Throttling`; the v2 API and several
    # shared AWS layers answer one of the others.
    "Throttling": ERROR_SES_THROTTLED,
    "ThrottlingException": ERROR_SES_THROTTLED,
    "TooManyRequestsException": ERROR_SES_THROTTLED,
    "LimitExceededException": ERROR_SES_THROTTLED,
    "RequestThrottled": ERROR_SES_THROTTLED,
    "SlowDown": ERROR_SES_THROTTLED,
    # The message itself was refused: an unverified recipient while the
    # account is in the SES sandbox, a suppressed address, a rejected body.
    "MessageRejected": ERROR_MESSAGE_REJECTED,
    # The sending identity's domain is not verified.
    "MailFromDomainNotVerified": ERROR_MAIL_FROM_DOMAIN_NOT_VERIFIED,
    "MailFromDomainNotVerifiedException": ERROR_MAIL_FROM_DOMAIN_NOT_VERIFIED,
    # The account cannot send at all right now.
    "AccountSendingPausedException": ERROR_ACCOUNT_SENDING_PAUSED,
    "SendingPausedException": ERROR_ACCOUNT_SENDING_PAUSED,
    "ConfigurationSetSendingPausedException": ERROR_ACCOUNT_SENDING_PAUSED,
    # Credentials exist but do not carry `ses:SendEmail`, or are expired.
    "AccessDenied": ERROR_PROVIDER_ACCESS_DENIED,
    "AccessDeniedException": ERROR_PROVIDER_ACCESS_DENIED,
    "UnrecognizedClientException": ERROR_PROVIDER_ACCESS_DENIED,
    "InvalidClientTokenId": ERROR_PROVIDER_ACCESS_DENIED,
    "ExpiredToken": ERROR_PROVIDER_ACCESS_DENIED,
    "ExpiredTokenException": ERROR_PROVIDER_ACCESS_DENIED,
    "SignatureDoesNotMatch": ERROR_PROVIDER_ACCESS_DENIED,
    # Céluma sent something malformed. A bug here, not an environment fault.
    "InvalidParameterValue": ERROR_PROVIDER_INVALID_PARAMETER,
    "ValidationError": ERROR_PROVIDER_INVALID_PARAMETER,
    "BadRequestException": ERROR_PROVIDER_INVALID_PARAMETER,
    "ConfigurationSetDoesNotExist": ERROR_PROVIDER_INVALID_PARAMETER,
    "ConfigurationSetDoesNotExistException": ERROR_PROVIDER_INVALID_PARAMETER,
    # SES is having a bad day.
    "ServiceUnavailable": ERROR_PROVIDER_UNAVAILABLE,
    "ServiceUnavailableException": ERROR_PROVIDER_UNAVAILABLE,
    "InternalFailure": ERROR_PROVIDER_UNAVAILABLE,
    "RequestTimeout": ERROR_PROVIDER_TIMEOUT,
}


def map_client_error_code(aws_code: Optional[str]) -> str:
    """One AWS error code to one Céluma code.

    Unmapped codes become `provider_unknown` rather than being normalized into
    the column: an unrecognized code is by definition one nobody has decided is
    safe to store, and `provider_unknown` plus the delivery id is enough to go
    and read the AWS-side logs.
    """
    return _CLIENT_ERROR_CODES.get(aws_code or "", ERROR_PROVIDER_UNKNOWN)


def map_provider_exception(exc: BaseException) -> EmailProviderError:
    """Every botocore failure, reduced to a stable code.

    Ordering matters: `ConnectTimeoutError` and friends are `BotoCoreError`
    subclasses, and `ClientError` is not, so the specific classes are tested
    before the two base classes.
    """
    if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
        return EmailProviderError(ERROR_PROVIDER_NOT_CONFIGURED)
    if isinstance(exc, (ConnectTimeoutError, ReadTimeoutError)):
        # Genuinely ambiguous: SES may already have accepted the message. The
        # delivery lifecycle retries it, which can produce a duplicate — the
        # same trade Story E7 resolves for stale claims, and one this block
        # deliberately resolves the other way here, because unlike a stale
        # claim a timeout is bounded, immediate and far more often a true
        # non-delivery. Documented in the architecture decision.
        return EmailProviderError(ERROR_PROVIDER_TIMEOUT)
    if isinstance(exc, (EndpointConnectionError, BotoConnectionError)):
        return EmailProviderError(ERROR_PROVIDER_UNAVAILABLE)
    if isinstance(exc, ClientError):
        response = getattr(exc, "response", None) or {}
        aws_code = (response.get("Error") or {}).get("Code")
        return EmailProviderError(map_client_error_code(aws_code))
    if isinstance(exc, BotoCoreError):
        return EmailProviderError(ERROR_PROVIDER_UNKNOWN)
    return EmailProviderError(ERROR_PROVIDER_UNKNOWN)


class SesEmailProvider(EmailProvider):
    """`EmailProvider` over the SES v1 `SendEmail` API.

    The client is built **lazily**, on first use, rather than in `__init__`.
    That is not a micro-optimization: constructing a boto3 client resolves
    credentials and endpoints, so an eager one makes merely *importing* or
    *instantiating* this class an operation that can fail or reach the
    network. `app/services/email.py` does it eagerly, and that is exactly why
    `block-e-dependencies.md` records it as having never been tested.

    `client` may be injected, which is how every test in this block exercises
    the real mapping logic with no AWS credentials and no network — the same
    seam `FakeS3Service` provides for S3, moved from a monkeypatch to a
    constructor argument because there is a constructor to put it on.
    """

    name = "ses"

    def __init__(self, *, client=None, region: Optional[str] = None):
        self._client = client
        self._region = region if region is not None else settings.effective_email_ses_region

    # -- client ------------------------------------------------------------

    def _get_client(self):
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _build_client(self):
        """A `ses` client for the configured region.

        `email_ses_region` rather than `aws_region` because Céluma runs in
        `mx-central-1`, where SES is not offered — a client built against the
        application's own region would fail to resolve an endpoint. Credentials
        follow the same explicit-then-default-chain pattern as `S3Service`: use
        the configured keys when both are present, otherwise let boto3 resolve
        the task role, which is how the deployed environment is meant to work.
        """
        session_kwargs: dict[str, str] = {}
        if settings.aws_access_key_id and settings.aws_secret_access_key:
            session_kwargs["aws_access_key_id"] = settings.aws_access_key_id
            session_kwargs["aws_secret_access_key"] = settings.aws_secret_access_key

        boto_session = boto3.session.Session(
            region_name=self._region,
            **session_kwargs,
        )
        return boto_session.client("ses")

    # -- send --------------------------------------------------------------

    def send(self, message: EmailMessage) -> EmailSendResult:
        body: dict = {
            "Text": {"Data": message.text_body, "Charset": "UTF-8"},
        }
        if message.html_body:
            body["Html"] = {"Data": message.html_body, "Charset": "UTF-8"}

        try:
            response = self._get_client().send_email(
                Source=_format_source(message),
                Destination={"ToAddresses": [message.to_address]},
                Message={
                    "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                    "Body": body,
                },
            )
        except Exception as exc:  # noqa: BLE001 — every failure becomes a code
            # Deliberately broad. A vendor exception that escaped this method
            # would reach the worker as something the worker must then decide
            # how to sanitize — which is the responsibility this class exists
            # to hold. `exc` is not chained into the raised error and is never
            # stringified, so its message (which can quote an address) does not
            # survive this frame.
            raise map_provider_exception(exc) from None

        return EmailSendResult(provider_message_id=(response or {}).get("MessageId"))

    # -- health ------------------------------------------------------------

    def health(self) -> EmailProviderHealth:
        """Probe SES without sending (Story E9).

        `GetSendQuota` is the probe: it is a read-only SES call that requires
        working credentials and the `ses:GetSendQuota` permission, so it
        answers "are we configured, authenticated and able to reach SES?"
        while being incapable of delivering a message to anyone.

        Never raises — a health probe that throws is one no caller can put in
        a startup path.

        Note on `configured`: it reflects
        `Settings.validate_email_configuration()`, which reports nothing while
        `EMAIL_ENABLED` is false — an environment with email switched off is
        not misconfigured, it is configured to be off. So a direct call with
        email disabled reports `configured=True` and goes on to make the
        read-only probe. That is the intended reading ("could this provider
        reach SES?"), and it does not affect the production path: the only
        caller is the worker's start-up, which runs after the enabled and
        validity gates.
        """
        configuration_problems = settings.validate_email_configuration()
        if configuration_problems:
            return EmailProviderHealth(
                provider=self.name,
                configured=False,
                credentials_present=False,
                reachable=False,
                error_code=ERROR_PROVIDER_NOT_CONFIGURED,
                detail="; ".join(configuration_problems),
                context={"region": self._region},
            )

        try:
            client = self._get_client()
        except Exception as exc:  # noqa: BLE001
            mapped = map_provider_exception(exc)
            return EmailProviderHealth(
                provider=self.name,
                configured=True,
                credentials_present=False,
                reachable=False,
                error_code=mapped.code,
                detail="The SES client could not be constructed",
                context={"region": self._region},
            )

        try:
            quota = client.get_send_quota() or {}
        except Exception as exc:  # noqa: BLE001
            mapped = map_provider_exception(exc)
            credentials_present = mapped.code not in (
                ERROR_PROVIDER_NOT_CONFIGURED,
                ERROR_PROVIDER_ACCESS_DENIED,
            )
            return EmailProviderHealth(
                provider=self.name,
                configured=True,
                credentials_present=credentials_present,
                reachable=False,
                error_code=mapped.code,
                detail="SES did not answer a read-only quota call",
                context={"region": self._region},
            )

        return EmailProviderHealth(
            provider=self.name,
            configured=True,
            credentials_present=True,
            reachable=True,
            context={
                "region": self._region,
                # Non-sensitive account-level numbers, genuinely useful in a
                # startup log line: a max_24h of 200 is the SES sandbox, which
                # is the single most likely reason a real send is rejected.
                "max_24_hour_send": quota.get("Max24HourSend"),
                "sent_last_24_hours": quota.get("SentLast24Hours"),
            },
        )


def _format_source(message: EmailMessage) -> str:
    """`Name <addr>` when a display name is set, otherwise the bare address.

    The name is quoted so a comma or a period in a laboratory's name cannot
    split the header into two addresses. Line breaks were already rejected by
    `EmailMessage.__post_init__` and by the `EMAIL_SENDER_NAME` validator, so
    this is the third and last place header injection is closed off.
    """
    if not message.from_name:
        return message.from_address
    escaped = message.from_name.replace("\\", "").replace('"', "")
    return f'"{escaped}" <{message.from_address}>'
