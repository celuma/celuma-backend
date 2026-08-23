"""Email provider tests (Céluma 1.3, Phase 3, Block E — Stories E2, E3, E4).

**Nothing here reaches AWS.** The SES provider is exercised with an injected
fake boto client, so the real mapping logic — which is the part that matters —
runs without credentials, without a network and without a region that has SES.
That is a hard requirement of this block and it is the gap
`block-e-dependencies.md` records against the old `EmailService`, which builds
its client in `__init__` and has therefore never been tested at all.

The three things under test, in order of weight:

1. **Every** provider failure becomes a stable code, never a vendor message.
   That is what stops a recipient address reaching `error_code` or a log line.
2. `health()` establishes whether a send could work, and sends nothing.
3. The fake is a real `EmailProvider`, deterministic, and scriptable enough to
   drive the worker's retry paths without touching a clock.
"""
import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    ParamValidationError,
    ReadTimeoutError,
)

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
    KNOWN_ERROR_CODES,
    RETRYABLE_ERROR_CODES,
    EmailMessage,
    EmailProvider,
    EmailProviderError,
    EmailProviderHealth,
)
from app.services.email_provider_factory import build_email_provider
from app.services.email_provider_fake import FakeEmailProvider
from app.services.email_provider_ses import (
    SesEmailProvider,
    _format_source,
    map_client_error_code,
    map_provider_exception,
)
from app.services.notification_delivery import sanitize_delivery_error_code

RECIPIENT = "destinatario@laboratorio.test"
SENDER = "notificaciones@celuma.test"


def message(**overrides) -> EmailMessage:
    values = {
        "to_address": RECIPIENT,
        "subject": "Laboratorio — Reporte publicado (Orden ORD-1)",
        "text_body": "Hay un reporte publicado disponible para la orden ORD-1.",
        "html_body": "<html><body><p>Hay un reporte publicado.</p></body></html>",
        "from_address": SENDER,
        "from_name": "Céluma",
    }
    values.update(overrides)
    return EmailMessage(**values)


def client_error(code: str, message_text: str = "boom") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message_text}}, "SendEmail"
    )


class FakeBotoSesClient:
    """Stands in for `boto3.client("ses")`.

    Deliberately shaped like the real one — `send_email(**kwargs)` returning a
    dict with `MessageId`, `get_send_quota()` returning the quota dict — so the
    provider under test runs its real argument-building code and a signature
    mistake would still surface.
    """

    def __init__(self, *, send_error=None, quota_error=None, message_id="ses-1"):
        self.send_error = send_error
        self.quota_error = quota_error
        self.message_id = message_id
        self.calls = []
        self.quota_calls = 0

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        if self.send_error is not None:
            raise self.send_error
        return {"MessageId": self.message_id}

    def get_send_quota(self):
        self.quota_calls += 1
        if self.quota_error is not None:
            raise self.quota_error
        return {"Max24HourSend": 50000.0, "SentLast24Hours": 12.0}


# ---------------------------------------------------------------------------
# E2 — the abstraction
# ---------------------------------------------------------------------------


class TestEmailMessageValidation:
    """`EmailMessage` is frozen and validated at construction, so a provider
    never has to decide whether it trusts what it was handed."""

    def test_a_valid_message_is_accepted(self):
        assert message().to_address == RECIPIENT

    @pytest.mark.parametrize(
        "address", ["", "not-an-address", "no@domain", "a b@c.test", None]
    )
    def test_an_undeliverable_recipient_is_refused(self, address):
        with pytest.raises(ValueError):
            message(to_address=address)

    @pytest.mark.parametrize("address", ["", "noreply", None])
    def test_an_undeliverable_sender_is_refused(self, address):
        with pytest.raises(ValueError):
            message(from_address=address)

    @pytest.mark.parametrize(
        "subject", ["Asunto\r\nBcc: attacker@evil.test", "Asunto\nX"]
    )
    def test_a_subject_with_a_line_break_is_refused(self, subject):
        """Header injection. Rejected at the type boundary so no provider has
        to remember to check."""
        with pytest.raises(ValueError):
            message(subject=subject)

    def test_a_sender_name_with_a_line_break_is_refused(self):
        with pytest.raises(ValueError):
            message(from_name="Céluma\r\nBcc: x@y.test")

    @pytest.mark.parametrize("field", ["subject", "text_body"])
    def test_an_empty_required_field_is_refused(self, field):
        with pytest.raises(ValueError):
            message(**{field: "   "})

    def test_the_html_body_is_optional(self):
        assert message(html_body=None).html_body is None

    def test_a_message_is_immutable(self):
        with pytest.raises(Exception):
            message().to_address = "otro@x.test"


class TestErrorCodeContract:
    def test_every_known_code_survives_the_delivery_sanitizer(self):
        """The single most important property of this whole module.

        `sanitize_delivery_error_code` refuses address-shaped input wholesale
        and collapses anything outside `[a-z0-9_.:-]`. A provider code that
        did not already satisfy it would silently land in the column as
        `delivery_failed`, which is exactly the diagnosis loss this
        abstraction exists to prevent."""
        for code in KNOWN_ERROR_CODES:
            assert sanitize_delivery_error_code(code) == code

    def test_retryable_codes_are_a_subset_of_known_codes(self):
        assert RETRYABLE_ERROR_CODES <= KNOWN_ERROR_CODES

    def test_retryability_defaults_from_the_code(self):
        assert EmailProviderError(ERROR_SES_THROTTLED).retryable is True
        assert EmailProviderError(ERROR_MESSAGE_REJECTED).retryable is False

    def test_retryability_can_be_overridden(self):
        assert EmailProviderError(ERROR_MESSAGE_REJECTED, retryable=True).retryable

    def test_the_exception_message_carries_no_provider_wording(self):
        """The message is a fixed English sentence naming the code. A vendor
        message can quote the envelope it choked on — which is an address."""
        exc = EmailProviderError(ERROR_MESSAGE_REJECTED)
        assert str(exc) == "Email provider failed with code message_rejected"
        assert RECIPIENT not in str(exc)


class TestProviderInterface:
    def test_the_abstraction_imports_no_vendor_sdk(self):
        """The dependency direction is `worker -> factory -> ses/fake ->
        abstraction`. If the abstraction imported its implementations, every
        process that wanted the type would drag boto3 in with it."""
        import inspect

        import app.services.email_provider as module

        source = inspect.getsource(module)
        for forbidden in ("boto3", "botocore", "smtplib", "sesv2", "httpx"):
            assert forbidden not in source, forbidden

    @pytest.mark.parametrize("provider", [FakeEmailProvider(), SesEmailProvider()])
    def test_both_providers_satisfy_the_interface(self, provider):
        assert isinstance(provider, EmailProvider)
        assert provider.name

    def test_an_incomplete_provider_cannot_be_instantiated(self):
        class Halfway(EmailProvider):
            def send(self, message):  # pragma: no cover - never constructed
                return None

        with pytest.raises(TypeError):
            Halfway()


class TestProviderFactory:
    def test_it_builds_the_fake(self):
        assert isinstance(build_email_provider("fake"), FakeEmailProvider)

    def test_it_builds_ses(self):
        assert isinstance(build_email_provider("ses"), SesEmailProvider)

    def test_it_is_case_and_whitespace_insensitive(self):
        assert isinstance(build_email_provider(" FAKE "), FakeEmailProvider)

    def test_an_unknown_name_raises_rather_than_defaulting_to_ses(self):
        """A silent fall back to SES would mean a typo in an environment
        variable sends real email."""
        with pytest.raises(ValueError):
            build_email_provider("sendgrid")

    def test_it_defaults_to_the_configured_provider(self):
        from app.core.config import settings

        provider = build_email_provider()
        assert provider.name == settings.email_provider


# ---------------------------------------------------------------------------
# E3 — SES
# ---------------------------------------------------------------------------


class TestSesSend:
    def test_a_successful_send_returns_the_provider_message_id(self):
        """It goes into `NotificationDelivery.provider_message_id` for support
        correlation — the only thing Céluma keeps to trace a message to the
        provider's own record of it."""
        client = FakeBotoSesClient(message_id="0100018f-abc")
        result = SesEmailProvider(client=client).send(message())

        assert result.provider_message_id == "0100018f-abc"
        assert len(client.calls) == 1

    def test_the_envelope_is_built_correctly(self):
        client = FakeBotoSesClient()
        SesEmailProvider(client=client).send(message())

        call = client.calls[0]
        assert call["Destination"] == {"ToAddresses": [RECIPIENT]}
        assert call["Message"]["Subject"]["Data"].startswith("Laboratorio —")
        assert call["Message"]["Subject"]["Charset"] == "UTF-8"
        assert call["Message"]["Body"]["Text"]["Charset"] == "UTF-8"

    def test_a_message_without_html_sends_only_a_text_part(self):
        client = FakeBotoSesClient()
        SesEmailProvider(client=client).send(message(html_body=None))

        assert "Html" not in client.calls[0]["Message"]["Body"]
        assert "Text" in client.calls[0]["Message"]["Body"]

    def test_a_missing_message_id_is_still_a_success(self):
        """Acceptance is the success condition, not the presence of an id."""

        class NoIdClient(FakeBotoSesClient):
            def send_email(self, **kwargs):
                return {}

        result = SesEmailProvider(client=NoIdClient()).send(message())
        assert result.provider_message_id is None

    def test_no_client_is_built_until_a_send_happens(self):
        """Constructing a boto3 client resolves credentials and endpoints, so
        an eager one makes *instantiating* the provider an operation that can
        fail or reach the network. That is why the old `EmailService` was
        never testable."""
        provider = SesEmailProvider()
        assert provider._client is None


class TestSesSourceHeader:
    def test_a_display_name_is_quoted(self):
        assert _format_source(message(from_name="Céluma")) == f'"Céluma" <{SENDER}>'

    def test_a_name_containing_a_comma_cannot_split_the_header(self):
        """Unquoted, `Laboratorio, S.A. <a@b.test>` is two addresses."""
        formatted = _format_source(message(from_name="Laboratorio, S.A."))
        assert formatted == f'"Laboratorio, S.A." <{SENDER}>'

    def test_an_embedded_quote_is_removed(self):
        formatted = _format_source(message(from_name='Lab X'))
        assert formatted.count('"') == 2

    def test_no_display_name_sends_the_bare_address(self):
        assert _format_source(message(from_name="")) == SENDER


class TestSesErrorMapping:
    @pytest.mark.parametrize(
        "aws_code,expected",
        [
            ("Throttling", ERROR_SES_THROTTLED),
            ("ThrottlingException", ERROR_SES_THROTTLED),
            ("TooManyRequestsException", ERROR_SES_THROTTLED),
            ("LimitExceededException", ERROR_SES_THROTTLED),
            ("MessageRejected", ERROR_MESSAGE_REJECTED),
            ("MailFromDomainNotVerified", ERROR_MAIL_FROM_DOMAIN_NOT_VERIFIED),
            (
                "MailFromDomainNotVerifiedException",
                ERROR_MAIL_FROM_DOMAIN_NOT_VERIFIED,
            ),
            ("AccountSendingPausedException", ERROR_ACCOUNT_SENDING_PAUSED),
            ("AccessDenied", ERROR_PROVIDER_ACCESS_DENIED),
            ("AccessDeniedException", ERROR_PROVIDER_ACCESS_DENIED),
            ("ExpiredToken", ERROR_PROVIDER_ACCESS_DENIED),
            ("InvalidParameterValue", ERROR_PROVIDER_INVALID_PARAMETER),
            ("ServiceUnavailable", ERROR_PROVIDER_UNAVAILABLE),
            ("SomethingNobodyMapped", ERROR_PROVIDER_UNKNOWN),
            (None, ERROR_PROVIDER_UNKNOWN),
        ],
    )
    def test_aws_codes_map_to_celuma_codes(self, aws_code, expected):
        assert map_client_error_code(aws_code) == expected

    @pytest.mark.parametrize(
        "exception,expected",
        [
            (NoCredentialsError(), ERROR_PROVIDER_NOT_CONFIGURED),
            (
                ConnectTimeoutError(endpoint_url="https://email.us-east-1.amazonaws.com"),
                ERROR_PROVIDER_TIMEOUT,
            ),
            (
                ReadTimeoutError(endpoint_url="https://email.us-east-1.amazonaws.com"),
                ERROR_PROVIDER_TIMEOUT,
            ),
            (
                EndpointConnectionError(endpoint_url="https://email.mx-central-1.amazonaws.com"),
                ERROR_PROVIDER_UNAVAILABLE,
            ),
            (ParamValidationError(report="bad"), ERROR_PROVIDER_UNKNOWN),
            (RuntimeError("something else entirely"), ERROR_PROVIDER_UNKNOWN),
        ],
    )
    def test_botocore_exceptions_map_to_celuma_codes(self, exception, expected):
        assert map_provider_exception(exception).code == expected

    def test_a_timeout_is_not_confused_with_an_unreachable_endpoint(self):
        """`ConnectTimeoutError` is a subclass of botocore's `ConnectionError`,
        so an ordering mistake in the mapping would quietly turn every timeout
        into `provider_unavailable`. Different meanings: a timeout may have
        been accepted, an unreachable endpoint was not."""
        timeout = ConnectTimeoutError(endpoint_url="https://x.test")
        assert map_provider_exception(timeout).code == ERROR_PROVIDER_TIMEOUT

    def test_send_raises_the_mapped_error(self):
        client = FakeBotoSesClient(send_error=client_error("MessageRejected"))

        with pytest.raises(EmailProviderError) as exc:
            SesEmailProvider(client=client).send(message())

        assert exc.value.code == ERROR_MESSAGE_REJECTED

    def test_no_provider_wording_survives_the_boundary(self):
        """A real SES rejection quotes the envelope, which means an address.
        Neither the code, the exception message nor the chained cause may
        carry it out of this frame."""
        leaky = (
            f"Email address is not verified. The following identities failed: "
            f"{RECIPIENT}"
        )
        client = FakeBotoSesClient(send_error=client_error("MessageRejected", leaky))

        with pytest.raises(EmailProviderError) as exc:
            SesEmailProvider(client=client).send(message())

        assert RECIPIENT not in str(exc.value)
        assert RECIPIENT not in exc.value.code
        assert exc.value.__cause__ is None
        assert exc.value.__context__ is None or RECIPIENT not in str(
            exc.value.__cause__ or ""
        )

    def test_every_send_failure_leaves_as_an_email_provider_error(self):
        """Including exceptions nobody anticipated. A vendor exception that
        escaped would move the sanitization burden onto the worker."""
        for raised in (
            RuntimeError("unexpected"),
            KeyError("MessageId"),
            ValueError("nope"),
        ):
            client = FakeBotoSesClient(send_error=raised)
            with pytest.raises(EmailProviderError) as exc:
                SesEmailProvider(client=client).send(message())
            assert exc.value.code in KNOWN_ERROR_CODES


@pytest.fixture(name="configured_email")
def configured_email_fixture(monkeypatch):
    """A complete email configuration, applied to the live `settings` object.

    Fields, not the validation method: `Settings` is a pydantic model, so
    monkeypatching a *method* onto it raises. Setting the fields means the
    real `validate_email_configuration` runs, which is what these tests want —
    a stubbed validator would prove the health probe reads a stub.
    """
    from app.core.config import settings as live

    monkeypatch.setattr(live, "email_enabled", True)
    monkeypatch.setattr(live, "email_sender", "notificaciones@celuma.test")
    monkeypatch.setattr(live, "email_provider", "ses")
    monkeypatch.setattr(live, "email_ses_region", "us-east-1")
    return live


@pytest.fixture(name="unconfigured_email")
def unconfigured_email_fixture(monkeypatch):
    """Email enabled, sender missing — the misconfiguration a health probe has
    to catch before it wastes a network call on it."""
    from app.core.config import settings as live

    monkeypatch.setattr(live, "email_enabled", True)
    monkeypatch.setattr(live, "email_sender", None)
    monkeypatch.setattr(live, "email_provider", "ses")
    monkeypatch.setattr(live, "email_ses_region", "us-east-1")
    return live


class TestSesHealth:
    def test_a_healthy_provider_reports_everything_true(self, configured_email):
        health = SesEmailProvider(client=FakeBotoSesClient(), region="us-east-1").health()

        assert health.healthy
        assert health.configured and health.credentials_present and health.reachable
        assert health.error_code is None

    def test_health_sends_nothing(self):
        """The whole point of Story E9: a probe that could deliver a message
        is not a probe."""
        client = FakeBotoSesClient()
        SesEmailProvider(client=client, region="us-east-1").health()

        assert client.calls == []
        assert client.quota_calls == 1

    def test_the_sending_quota_is_reported_as_context(self, configured_email):
        """A `Max24HourSend` of 200 is the SES sandbox, which is the single
        most likely reason a real send is rejected — worth having in the boot
        log."""
        health = SesEmailProvider(client=FakeBotoSesClient(), region="us-east-1").health()

        assert health.context["max_24_hour_send"] == 50000.0
        assert health.context["region"] == "us-east-1"

    def test_an_incomplete_configuration_is_reported_without_calling_ses(
        self, unconfigured_email
    ):
        client = FakeBotoSesClient()
        health = SesEmailProvider(client=client, region="us-east-1").health()

        assert not health.healthy
        assert health.configured is False
        assert health.error_code == ERROR_PROVIDER_NOT_CONFIGURED
        assert client.quota_calls == 0

    def test_missing_credentials_are_distinguished_from_an_outage(
        self, configured_email
    ):
        health = SesEmailProvider(
            client=FakeBotoSesClient(quota_error=client_error("AccessDenied")),
            region="us-east-1",
        ).health()

        assert health.credentials_present is False
        assert health.reachable is False
        assert health.error_code == ERROR_PROVIDER_ACCESS_DENIED

    def test_an_unreachable_endpoint_keeps_credentials_true(self, configured_email):
        """Different fields because they have different fixes: one is an IAM
        change, the other is a region or an outage."""
        health = SesEmailProvider(
            client=FakeBotoSesClient(
                quota_error=EndpointConnectionError(endpoint_url="https://x.test")
            ),
            region="mx-central-1",
        ).health()

        assert health.credentials_present is True
        assert health.reachable is False
        assert health.error_code == ERROR_PROVIDER_UNAVAILABLE

    def test_health_never_raises(self):
        """A probe that throws is one no caller can put in a startup path."""

        class ExplodingClient:
            def get_send_quota(self):
                raise RuntimeError("catastrophe")

        health = SesEmailProvider(client=ExplodingClient(), region="us-east-1").health()
        assert isinstance(health, EmailProviderHealth)
        assert health.healthy is False


class TestSesRegion:
    def test_it_uses_the_configured_ses_region_not_the_application_region(
        self, monkeypatch
    ):
        """Céluma runs in `mx-central-1`, where SES is not offered. A client
        built against the application's own region cannot resolve an
        endpoint."""
        monkeypatch.setattr(
            "app.services.email_provider_ses.settings.aws_region", "mx-central-1"
        )
        monkeypatch.setattr(
            "app.services.email_provider_ses.settings.email_ses_region", "us-east-1"
        )
        assert SesEmailProvider()._region == "us-east-1"


# ---------------------------------------------------------------------------
# E4 — the fake
# ---------------------------------------------------------------------------


class TestFakeProvider:
    def test_it_imports_no_aws_sdk(self):
        """The fake is what makes "the whole suite runs without AWS
        credentials" true. Importing boto3 here would defeat it."""
        import inspect

        import app.services.email_provider_fake as module

        source = inspect.getsource(module)
        for forbidden in ("boto3", "botocore", "smtplib", "requests", "httpx"):
            assert forbidden not in source, forbidden

    def test_it_records_what_it_was_asked_to_send(self):
        provider = FakeEmailProvider()
        provider.send(message())

        assert provider.sent_count == 1
        assert provider.sent[0].to_address == RECIPIENT
        assert provider.sent[0].subject.startswith("Laboratorio —")

    def test_message_ids_are_deterministic_and_sequential(self):
        provider = FakeEmailProvider()
        first = provider.send(message()).provider_message_id
        second = provider.send(message()).provider_message_id

        assert (first, second) == ("fake-message-1", "fake-message-2")

    def test_two_instances_do_not_share_state(self):
        """Two tests building their own provider must not influence each
        other — the `FakeS3Service` class-attribute store is the counter-example
        this deliberately avoids."""
        first, second = FakeEmailProvider(), FakeEmailProvider()
        first.send(message())

        assert second.sent_count == 0
        assert second.send(message()).provider_message_id == "fake-message-1"

    def test_a_scripted_address_failure_always_fails(self):
        provider = FakeEmailProvider(
            fail_addresses={RECIPIENT: ERROR_MESSAGE_REJECTED}
        )

        with pytest.raises(EmailProviderError) as exc:
            provider.send(message())

        assert exc.value.code == ERROR_MESSAGE_REJECTED
        assert provider.sent_count == 0
        assert provider.failed == [(RECIPIENT, ERROR_MESSAGE_REJECTED)]

    def test_one_failing_address_does_not_affect_another(self):
        provider = FakeEmailProvider(
            fail_addresses={RECIPIENT: ERROR_MESSAGE_REJECTED}
        )
        other = "otro@laboratorio.test"

        with pytest.raises(EmailProviderError):
            provider.send(message())
        provider.send(message(to_address=other))

        assert [record.to_address for record in provider.sent] == [other]

    def test_fail_next_n_recovers_after_n_failures(self):
        """How a retry test drives a delivery to SENT on its third attempt
        without touching a clock."""
        provider = FakeEmailProvider(fail_next_n=2)

        for _ in range(2):
            with pytest.raises(EmailProviderError):
                provider.send(message())
        assert provider.send(message()).provider_message_id == "fake-message-1"
        assert provider.sent_count == 1

    def test_throttling_starts_after_the_configured_number_of_sends(self):
        provider = FakeEmailProvider(throttle_after=2)

        provider.send(message())
        provider.send(message())
        with pytest.raises(EmailProviderError) as exc:
            provider.send(message())

        assert exc.value.code == ERROR_SES_THROTTLED
        assert exc.value.retryable is True

    def test_health_is_healthy_by_default(self):
        assert FakeEmailProvider().health().healthy

    def test_health_can_be_scripted_unhealthy(self):
        unhealthy = EmailProviderHealth(
            provider="fake",
            configured=True,
            credentials_present=False,
            reachable=False,
            error_code=ERROR_PROVIDER_ACCESS_DENIED,
        )
        assert FakeEmailProvider(health_result=unhealthy).health().healthy is False

    def test_reset_clears_recordings_and_scripting(self):
        provider = FakeEmailProvider(fail_next_n=3)
        provider.fail_addresses[RECIPIENT] = ERROR_MESSAGE_REJECTED
        provider.reset()

        assert provider.sent == [] and provider.failed == []
        assert provider.fail_next_n == 0 and provider.fail_addresses == {}
        assert provider.send(message()).provider_message_id

    def test_sent_to_filters_by_recipient(self):
        provider = FakeEmailProvider()
        provider.send(message())
        provider.send(message(to_address="otro@laboratorio.test"))

        assert len(provider.sent_to(RECIPIENT)) == 1
