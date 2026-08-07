"""Email configuration tests (Céluma 1.3, Phase 3, Block E, Story E1).

These exist because of a specific, shipped bug, not for coverage. Before this
block, `app/services/email.py`, `app/api/v1/auth.py` and `app/api/v1/users.py`
read their configuration as::

    getattr(settings, "email_sender", "noreply@celuma.com")
    getattr(settings, "frontend_url", "http://localhost:5173")

against fields that **did not exist on `Settings`**. `getattr` with a default
cannot fail, so nothing anywhere reported a problem: every environment used
the literal, in production as much as locally, and the deployed
`FRONTEND_URL` that `celuma-infra` has been setting on the task definition all
along was simply ignored.

`TestNoSilentFallback` is the regression guard. It asserts the fields are real
`Settings` attributes and that no `getattr(settings, ...)` fallback for them
survives anywhere in `app/` — so reintroducing the pattern fails CI rather
than quietly reinstating the bug.

No database, no network, no AWS.
"""
import inspect
import pathlib

import pytest
from pydantic import ValidationError

from app.core.config import (
    EMAIL_PROVIDERS,
    MAX_DELIVERY_POLL_INTERVAL_SECONDS,
    MIN_DELIVERY_POLL_INTERVAL_SECONDS,
    Settings,
    settings,
)

APP_ROOT = pathlib.Path(inspect.getfile(Settings)).parent.parent


def build_settings(**overrides) -> Settings:
    """A `Settings` with the two required fields supplied.

    Everything else falls back to the declared defaults (or `.env`, which is
    why every field under test is passed explicitly by the tests that care).
    """
    base = {
        "database_url": "postgresql+psycopg2://u:p@localhost:5432/x",
        "jwt_secret": "test-secret",
    }
    base.update(overrides)
    return Settings(**base)


class TestNoSilentFallback:
    """The exact bug, closed."""

    @pytest.mark.parametrize(
        "field",
        [
            "email_enabled",
            "email_provider",
            "email_sender",
            "email_sender_name",
            "email_ses_region",
            "frontend_url",
            "delivery_poll_interval_seconds",
        ],
    )
    def test_the_field_really_exists(self, field):
        """`getattr(settings, field, <default>)` silently returned the default
        for `email_sender` and `frontend_url` because the attribute was
        absent. Declared fields make a missing value a visible one."""
        assert field in Settings.model_fields
        assert hasattr(settings, field)

    def test_no_getattr_fallback_for_these_settings_survives_in_app(self):
        """Grep the application source, not just the two known call sites.

        A fallback anywhere reintroduces the failure mode, and the point of a
        source-level assertion is that it catches the third call site nobody
        remembered to look at."""
        offenders = []
        for path in APP_ROOT.rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            source = path.read_text(encoding="utf-8")
            for needle in (
                "getattr(settings, 'email_sender'",
                'getattr(settings, "email_sender"',
                "getattr(settings, 'frontend_url'",
                'getattr(settings, "frontend_url"',
                "getattr(settings, 'aws_region'",
                'getattr(settings, "aws_region"',
            ):
                if needle in source:
                    offenders.append(f"{path.name}: {needle}")
        assert offenders == []

    def test_email_sender_has_no_default(self):
        """`noreply@celuma.com` was never a verified SES identity, so the old
        default guaranteed every send was rejected. Unset must mean
        "unconfigured", which the worker reports and refuses to run on — not a
        wrong address it will keep trying forever."""
        assert Settings.model_fields["email_sender"].default is None

    def test_email_is_disabled_by_default(self):
        """No SES identity, DKIM record or IAM grant exists in any environment
        yet, and nothing creates a notification until Block F. A worker that
        started by default would only accumulate failed attempts."""
        assert Settings.model_fields["email_enabled"].default is False
        assert build_settings().email_enabled is False


class TestFieldValidation:
    """Invariants that are wrong however email is configured, so they are
    checked at import."""

    def test_a_known_provider_is_accepted(self):
        for name in EMAIL_PROVIDERS:
            assert build_settings(email_provider=name).email_provider == name

    def test_the_provider_name_is_normalized(self):
        assert build_settings(email_provider="  SES ").email_provider == "ses"

    def test_an_unknown_provider_is_refused(self):
        """Not defaulted to SES: a typo in an environment variable must not be
        resolved into "send real email"."""
        with pytest.raises(ValidationError) as exc:
            build_settings(email_provider="sendgrid")
        assert "EMAIL_PROVIDER" in str(exc.value)

    @pytest.mark.parametrize(
        "value",
        [MIN_DELIVERY_POLL_INTERVAL_SECONDS, 10, 15, MAX_DELIVERY_POLL_INTERVAL_SECONDS],
    )
    def test_a_sane_poll_interval_is_accepted(self, value):
        assert build_settings(
            delivery_poll_interval_seconds=value
        ).delivery_poll_interval_seconds == value

    @pytest.mark.parametrize("value", [0, -1, MAX_DELIVERY_POLL_INTERVAL_SECONDS + 1])
    def test_an_out_of_range_poll_interval_is_refused(self, value):
        """Zero would turn the loop into a busy-wait against PostgreSQL; an
        absurdly large value turns delivery into something that looks
        broken."""
        with pytest.raises(ValidationError):
            build_settings(delivery_poll_interval_seconds=value)

    def test_the_default_poll_interval_is_block_as_recommendation(self):
        assert build_settings().delivery_poll_interval_seconds == 10

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("https://app.celuma.mx", "https://app.celuma.mx"),
            ("https://app.celuma.mx/", "https://app.celuma.mx"),
            ("  http://localhost:5173  ", "http://localhost:5173"),
        ],
    )
    def test_the_frontend_url_is_normalized(self, value, expected):
        assert build_settings(frontend_url=value).frontend_url == expected

    @pytest.mark.parametrize("value", ["app.celuma.mx", "ftp://x.test", "/app", ""])
    def test_a_relative_or_non_http_frontend_url_is_refused(self, value):
        """It is rendered into an email as a link. A relative one is not a
        link, and a non-http scheme is not one a mail client should follow."""
        with pytest.raises(ValidationError):
            build_settings(frontend_url=value)

    @pytest.mark.parametrize(
        "value",
        ["Lab\r\nBcc: attacker@evil.test", "Lab\nX", 'Lab "quoted"', "Lab <x>"],
    )
    def test_a_sender_name_that_could_break_a_header_is_refused(self, value):
        """This string is interpolated into a `From:` header. A line break in
        it is header injection — an attacker-supplied `Bcc:`. Rejected, not
        stripped, because a silently altered sender name is a configuration
        that does not match what was asked for."""
        with pytest.raises(ValidationError):
            build_settings(email_sender_name=value)

    def test_an_empty_sender_name_is_refused(self):
        with pytest.raises(ValidationError):
            build_settings(email_sender_name="   ")

    def test_an_accented_sender_name_is_accepted(self):
        assert build_settings(email_sender_name="Céluma").email_sender_name == "Céluma"


class TestSesRegionFallback:
    """Céluma runs in `mx-central-1`, where SES is not offered — so unlike
    `S3Service`, the SES client cannot simply reuse `aws_region`."""

    def test_the_explicit_ses_region_wins(self):
        resolved = build_settings(
            aws_region="mx-central-1", email_ses_region="us-east-1"
        ).effective_email_ses_region
        assert resolved == "us-east-1"

    def test_it_falls_back_to_the_application_region(self):
        """So an environment in a region where SES *is* available needs no
        extra configuration."""
        resolved = build_settings(
            aws_region="us-east-1", email_ses_region=None
        ).effective_email_ses_region
        assert resolved == "us-east-1"

    def test_both_unset_resolves_to_none(self):
        assert (
            build_settings(aws_region=None, email_ses_region=None)
            .effective_email_ses_region
            is None
        )


class TestCrossFieldValidation:
    """`validate_email_configuration` runs at *worker startup*, not at import.

    Raising at import for a missing `EMAIL_SENDER` would mean a misconfigured
    mailbox stops the API from booting, inverting architectural principle
    §4.3/§4.7 — a clinical operation must never depend on email. So it returns
    problems and the worker declines to start.
    """

    def test_disabled_email_reports_nothing(self):
        """Nothing is checked when nothing will be sent. An environment with
        email off is not misconfigured; it is configured to be off."""
        assert build_settings(email_enabled=False, email_sender=None).validate_email_configuration() == []

    def test_a_complete_configuration_reports_nothing(self):
        assert (
            build_settings(
                email_enabled=True,
                email_provider="ses",
                email_sender="notificaciones@celuma.mx",
                email_ses_region="us-east-1",
            ).validate_email_configuration()
            == []
        )

    def test_a_missing_sender_is_reported(self):
        problems = build_settings(
            email_enabled=True, email_sender=None, email_ses_region="us-east-1"
        ).validate_email_configuration()
        assert any("EMAIL_SENDER" in problem for problem in problems)

    @pytest.mark.parametrize(
        "value",
        [
            "not-an-address",
            "Céluma <a@b.test>",
            "a@b.test, c@d.test",
            "a@b.test\r\nBcc: x@y.test",
        ],
    )
    def test_a_sender_that_is_not_a_bare_address_is_reported(self, value):
        """SES's `Source` takes the address; the display name is assembled
        separately from `EMAIL_SENDER_NAME`. A combined value here would be
        double-wrapped, and a comma-separated one is two senders."""
        problems = build_settings(
            email_enabled=True, email_sender=value, email_ses_region="us-east-1"
        ).validate_email_configuration()
        assert any("EMAIL_SENDER" in problem for problem in problems)

    def test_ses_without_any_region_is_reported(self):
        problems = build_settings(
            email_enabled=True,
            email_provider="ses",
            email_sender="a@b.test",
            aws_region=None,
            email_ses_region=None,
        ).validate_email_configuration()
        assert any("EMAIL_SES_REGION" in problem for problem in problems)

    def test_the_fake_provider_needs_no_region(self):
        """Local development must not require an AWS concept to run the
        delivery pipeline end to end."""
        assert (
            build_settings(
                email_enabled=True,
                email_provider="fake",
                email_sender="a@b.test",
                aws_region=None,
                email_ses_region=None,
            ).validate_email_configuration()
            == []
        )

    def test_no_problem_message_echoes_a_configured_value(self):
        """Every message names a variable. None quotes its value — a
        misconfigured sender address must not reach a log line, since the
        worker logs these verbatim."""
        secret_address = "leaky.address@private.test"
        problems = build_settings(
            email_enabled=True,
            email_sender=f"{secret_address}, second@x.test",
            email_ses_region="us-east-1",
        ).validate_email_configuration()

        assert problems
        for problem in problems:
            assert secret_address not in problem
            assert "@" not in problem
