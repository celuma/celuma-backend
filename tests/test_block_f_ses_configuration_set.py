"""SES configuration-set association and the explicit-region rule.

Céluma 1.3, Phase 5, Block F SES closure — F-016 (parts A and B) and F-005.

**F-016.** Block F found that adding an SES configuration set to
`celuma-infra` would not, on its own, produce a single observable event.
`SesEmailProvider.send` called `send_email(...)` with no
`ConfigurationSetName`, so no Céluma send would ever be *associated* with the
configuration set, and SES publishes SEND / DELIVERY / BOUNCE / COMPLAINT
events per association rather than per account. The infrastructure would have
existed and observed nothing. That was the least obvious third of F-016 and it
is what these tests lock.

The association is deliberately optional. An environment that has not opted in
must produce **exactly** the call it produced before this block — not a call
carrying an empty parameter, which SES rejects outright. So the assertions
below are about the *absence of a key*, not about a falsy value.

**F-005.** `effective_email_ses_region` falls back to `aws_region`, which is
correct in general and wrong for Céluma in particular: the application region
is `mx-central-1`, where SES has no endpoint at all. The fallback therefore
built a client that could not resolve a host, surfacing as
`provider_unavailable` on every attempt — a network-shaped error for what is
really a missing setting, discovered one failed delivery at a time. Validation
now requires the explicit variable, so it fails as a configuration problem
before anything is sent. The property itself is unchanged; the fake provider is
untouched, because local development must not require an AWS concept.
"""
from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from app.core.config import Settings
from app.services.email_provider import (
    ERROR_MESSAGE_REJECTED,
    ERROR_PROVIDER_INVALID_PARAMETER,
    EmailMessage,
    EmailProviderError,
)
from app.services.email_provider_ses import SesEmailProvider

CONFIGURATION_SET = "celuma-transactional"


class RecordingSesClient:
    """Shaped like `boto3.client("ses")`, and records what it was handed.

    Kept local to this module rather than imported from
    `tests/test_email_provider.py`: these tests are about the *keyword
    arguments* of the call, so the double must not be one whose shape someone
    could change for an unrelated reason.
    """

    def __init__(self, *, send_error=None, message_id="ses-message-id-1"):
        self.send_error = send_error
        self.message_id = message_id
        self.calls: list[dict] = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        if self.send_error is not None:
            raise self.send_error
        return {"MessageId": self.message_id}

    def get_send_quota(self):
        return {"Max24HourSend": 200.0, "SentLast24Hours": 0.0}


def message(**overrides) -> EmailMessage:
    values = {
        "to_address": "success@simulator.amazonses.com",
        "subject": "Céluma — notificación de prueba",
        "text_body": "Cuerpo de prueba.",
        "html_body": "<html><body><p>Cuerpo de prueba.</p></body></html>",
        "from_address": "notificaciones@celuma.test",
        "from_name": "Céluma",
    }
    values.update(overrides)
    return EmailMessage(**values)


def build_settings(**overrides) -> Settings:
    """A `Settings` built from explicit values only.

    `_env_file=None` keeps a developer's `.env` out of the assertions — these
    tests are about defaults and precedence, and a machine that happens to
    export `EMAIL_SES_REGION` must not make them pass.
    """
    values = {
        "jwt_secret": "irrelevant-in-tests",
        "email_provider": "ses",
        "email_sender": "notificaciones@celuma.test",
        "aws_region": "mx-central-1",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


# ---------------------------------------------------------------------------
# F-016 A — the setting
# ---------------------------------------------------------------------------


class TestConfigurationSetSetting:
    def test_it_defaults_to_unset(self):
        """Nothing about an existing environment changes by upgrading."""
        assert build_settings().email_configuration_set is None

    def test_it_accepts_a_name(self):
        assert (
            build_settings(email_configuration_set=CONFIGURATION_SET)
            .email_configuration_set
            == CONFIGURATION_SET
        )

    def test_it_is_not_required_for_a_valid_configuration(self):
        """The association is observability, not a precondition for sending.
        A complete configuration without it must report no problems."""
        assert (
            build_settings(
                email_enabled=True, email_ses_region="us-east-1",
            ).validate_email_configuration()
            == []
        )

    def test_it_is_not_required_by_the_fake_provider(self):
        assert (
            build_settings(
                email_enabled=True,
                email_provider="fake",
                aws_region=None,
                email_ses_region=None,
            ).validate_email_configuration()
            == []
        )


# ---------------------------------------------------------------------------
# F-016 B — the send association
# ---------------------------------------------------------------------------


class TestSendAssociation:
    def test_absent_configuration_set_sends_no_such_parameter(self):
        """The key must be **missing**, not present-and-empty. SES rejects an
        empty `ConfigurationSetName`, so a provider that always passed the
        parameter would break every environment that has not opted in."""
        client = RecordingSesClient()

        SesEmailProvider(client=client, configuration_set=None).send(message())

        (call,) = client.calls
        assert "ConfigurationSetName" not in call

    def test_an_empty_configuration_set_is_treated_as_absent(self):
        """`EMAIL_CONFIGURATION_SET=` in an env file arrives as an empty
        string, which must mean "not configured" rather than "configure me
        with nothing"."""
        client = RecordingSesClient()

        SesEmailProvider(client=client, configuration_set="   ").send(message())

        (call,) = client.calls
        assert "ConfigurationSetName" not in call

    def test_a_configured_set_is_passed_exactly(self):
        client = RecordingSesClient()

        SesEmailProvider(
            client=client, configuration_set=CONFIGURATION_SET,
        ).send(message())

        (call,) = client.calls
        assert call["ConfigurationSetName"] == CONFIGURATION_SET

    def test_the_rest_of_the_call_is_unchanged_by_the_association(self):
        """Adding the association must not disturb Source, Destination or
        Message — the parts that decide what the recipient actually gets."""
        without = RecordingSesClient()
        with_set = RecordingSesClient()

        SesEmailProvider(client=without, configuration_set=None).send(message())
        SesEmailProvider(
            client=with_set, configuration_set=CONFIGURATION_SET,
        ).send(message())

        (plain,) = without.calls
        (associated,) = with_set.calls
        assert associated.pop("ConfigurationSetName") == CONFIGURATION_SET
        assert associated == plain

    def test_it_reads_the_setting_when_not_passed_explicitly(self, monkeypatch):
        """`build_email_provider` constructs `SesEmailProvider()` with no
        arguments, so the setting is the only route the deployed path has."""
        from app.core.config import settings as live

        monkeypatch.setattr(live, "email_configuration_set", CONFIGURATION_SET)
        client = RecordingSesClient()

        SesEmailProvider(client=client).send(message())

        (call,) = client.calls
        assert call["ConfigurationSetName"] == CONFIGURATION_SET

    def test_an_explicit_argument_beats_the_setting(self, monkeypatch):
        from app.core.config import settings as live

        monkeypatch.setattr(live, "email_configuration_set", "from-settings")
        client = RecordingSesClient()

        SesEmailProvider(client=client, configuration_set="explicit").send(message())

        (call,) = client.calls
        assert call["ConfigurationSetName"] == "explicit"


# ---------------------------------------------------------------------------
# The correlation mechanism must survive the change
# ---------------------------------------------------------------------------


class TestProviderMessageIdIsUnaffected:
    """`provider_message_id` is the durable correlation between a Céluma
    delivery row and an SES event, and Block F classified it FULL
    CORRELATION. The configuration-set work must not disturb it — the whole
    point of associating sends is to make that id findable in the event
    stream."""

    def test_message_id_is_returned_without_a_configuration_set(self):
        client = RecordingSesClient(message_id="0100-abc")

        result = SesEmailProvider(client=client, configuration_set=None).send(message())

        assert result.provider_message_id == "0100-abc"

    def test_message_id_is_returned_with_a_configuration_set(self):
        client = RecordingSesClient(message_id="0100-def")

        result = SesEmailProvider(
            client=client, configuration_set=CONFIGURATION_SET,
        ).send(message())

        assert result.provider_message_id == "0100-def"


# ---------------------------------------------------------------------------
# Error mapping must survive the change
# ---------------------------------------------------------------------------


class TestErrorMappingIsUnchanged:
    def test_a_rejected_message_still_maps_to_its_code(self):
        client = RecordingSesClient(
            send_error=ClientError(
                {"Error": {"Code": "MessageRejected", "Message": "boom"}}, "SendEmail",
            )
        )

        with pytest.raises(EmailProviderError) as raised:
            SesEmailProvider(
                client=client, configuration_set=CONFIGURATION_SET,
            ).send(message())

        assert raised.value.code == ERROR_MESSAGE_REJECTED

    def test_a_missing_configuration_set_maps_to_invalid_parameter(self):
        """The failure this feature can newly cause: a name that does not
        exist in the SES region. It must arrive as a stable code, not as a
        vendor string — the mapping already existed, and this proves it is
        reachable now that the parameter is actually sent."""
        client = RecordingSesClient(
            send_error=ClientError(
                {
                    "Error": {
                        "Code": "ConfigurationSetDoesNotExist",
                        "Message": "Configuration set <name> does not exist.",
                    }
                },
                "SendEmail",
            )
        )

        with pytest.raises(EmailProviderError) as raised:
            SesEmailProvider(
                client=client, configuration_set="does-not-exist",
            ).send(message())

        assert raised.value.code == ERROR_PROVIDER_INVALID_PARAMETER

    def test_no_provider_message_survives_the_error(self):
        """A vendor message can quote a recipient address. It must not reach
        the raised error, with or without an association."""
        secret = "patient.address@private.test"
        client = RecordingSesClient(
            send_error=ClientError(
                {"Error": {"Code": "MessageRejected", "Message": secret}}, "SendEmail",
            )
        )

        with pytest.raises(EmailProviderError) as raised:
            SesEmailProvider(
                client=client, configuration_set=CONFIGURATION_SET,
            ).send(message())

        assert secret not in str(raised.value)
        assert raised.value.__cause__ is None


# ---------------------------------------------------------------------------
# F-005 — the SES region must be explicit
# ---------------------------------------------------------------------------


class TestExplicitSesRegionIsRequired:
    def test_inheriting_the_application_region_is_now_reported(self):
        """The F-005 case: `AWS_REGION=mx-central-1`, no `EMAIL_SES_REGION`.
        Previously this validated clean and then failed at send time with
        `provider_unavailable`, because SES has no `mx-central-1` endpoint."""
        problems = build_settings(
            email_enabled=True, aws_region="mx-central-1", email_ses_region=None,
        ).validate_email_configuration()

        assert any("EMAIL_SES_REGION" in problem for problem in problems)

    def test_it_is_reported_even_when_the_app_region_would_offer_ses(self):
        """Not a `mx-central-1` special case. Inheriting the region is
        refused wherever the application runs, so the deployed value is always
        something someone chose."""
        problems = build_settings(
            email_enabled=True, aws_region="us-east-1", email_ses_region=None,
        ).validate_email_configuration()

        assert any("EMAIL_SES_REGION" in problem for problem in problems)

    def test_an_explicit_region_reports_nothing(self):
        assert (
            build_settings(
                email_enabled=True,
                aws_region="mx-central-1",
                email_ses_region="us-east-1",
            ).validate_email_configuration()
            == []
        )

    def test_a_blank_region_is_not_a_region(self):
        problems = build_settings(
            email_enabled=True, email_ses_region="   ",
        ).validate_email_configuration()

        assert any("EMAIL_SES_REGION" in problem for problem in problems)

    def test_the_fake_provider_still_needs_no_region(self):
        assert (
            build_settings(
                email_enabled=True,
                email_provider="fake",
                aws_region=None,
                email_ses_region=None,
            ).validate_email_configuration()
            == []
        )

    def test_disabled_email_reports_nothing(self):
        """An environment with email off is configured to be off, not
        misconfigured — including one with no SES region at all."""
        assert (
            build_settings(
                email_enabled=False, email_ses_region=None,
            ).validate_email_configuration()
            == []
        )

    def test_the_message_names_the_variable_and_quotes_no_value(self):
        problems = build_settings(
            email_enabled=True, aws_region="mx-central-1", email_ses_region=None,
        ).validate_email_configuration()

        (problem,) = [p for p in problems if "EMAIL_SES_REGION" in p]
        assert "mx-central-1" not in problem

    def test_the_property_still_falls_back(self):
        """Unchanged on purpose. Other callers rely on it, and the guarantee
        being added is at the validation boundary — not a redesign of the
        general AWS region model."""
        assert (
            build_settings(
                aws_region="us-east-1", email_ses_region=None,
            ).effective_email_ses_region
            == "us-east-1"
        )


# ---------------------------------------------------------------------------
# The master switch still governs everything
# ---------------------------------------------------------------------------


class TestEmailDisabledStillSendsNothing:
    def test_a_disabled_environment_starts_no_worker(self, monkeypatch):
        """`EMAIL_ENABLED=false` must mean zero SES calls even once a
        configuration set and an identity exist in AWS. Provisioning the
        capability and enabling it stay separate acts."""
        import asyncio

        from app.core.config import settings as live
        from app.services.notification_delivery_worker import start_worker, stop_worker

        monkeypatch.setattr(live, "email_enabled", False)
        monkeypatch.setattr(live, "email_provider", "ses")
        monkeypatch.setattr(live, "email_ses_region", "us-east-1")
        monkeypatch.setattr(live, "email_configuration_set", CONFIGURATION_SET)

        assert asyncio.run(start_worker()) is None
        asyncio.run(stop_worker())

    def test_a_disabled_environment_reports_no_configuration_problems(self):
        assert (
            build_settings(
                email_enabled=False,
                email_configuration_set=CONFIGURATION_SET,
                email_ses_region=None,
            ).validate_email_configuration()
            == []
        )
