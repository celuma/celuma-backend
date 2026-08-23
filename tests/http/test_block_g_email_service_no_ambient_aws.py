"""Account email must not require ambient AWS configuration.

Céluma 1.3, Phase 5, Block G-B — CI remediation.

**The failure this locks was CI-only.** GitHub Actions runs the suite with
exactly three variables — `DATABASE_URL`, `JWT_SECRET`, `JWT_EXPIRES_MIN`
(`.github/workflows/pr.yml`). No `AWS_REGION`, no `AWS_DEFAULT_REGION`, no
credentials. `EmailService.__init__` built a boto3 SES client eagerly, and
`create_invitation` instantiates that service on every invitation, so
`POST /api/v1/users/invitations` raised
`botocore.exceptions.NoRegionError: You must specify a region.` and returned a
500.

The test that caught it —
`test_usage_threshold_triggers.py::TestUserFlowsEndToEnd::test_accepting_an_invitation_consumes_a_seat`
— is about **seat accounting**. It has nothing to do with email, and it should
never have depended on AWS being configured.

It passed locally only by accident: a gitignored `.env` supplies `AWS_REGION`,
and `effective_email_ses_region` falls back to it (`email_ses_region or
aws_region`). Every developer machine had the variable; the runner did not.

The fix is the pattern `SesEmailProvider` already uses, and which
`block-e-dependencies.md` recorded the legacy `EmailService` as lacking: build
no client until a send actually happens.

**Nothing here reaches AWS.** Every assertion below is either about a client
that was never constructed, or about an injected fake.
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.services.email import EmailService

from .factories import auth_headers, create_branch, create_tenant, create_user

AWS_REGION_VARIABLES = ("AWS_REGION", "AWS_DEFAULT_REGION")


@pytest.fixture(name="no_ambient_aws")
def no_ambient_aws_fixture(monkeypatch):
    """A CI-shaped environment: no AWS region anywhere boto3 would look.

    Both the process environment and the Céluma settings that feed
    `effective_email_ses_region` are cleared, so a client construction would
    raise `NoRegionError` exactly as it did on the runner.
    """
    for variable in AWS_REGION_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(settings, "aws_region", None, raising=False)
    monkeypatch.setattr(settings, "email_ses_region", None, raising=False)
    monkeypatch.setattr(settings, "aws_access_key_id", None, raising=False)
    monkeypatch.setattr(settings, "aws_secret_access_key", None, raising=False)


class _ExplodingClient:
    """Any use of this is a real AWS call that should not be happening."""

    def send_email(self, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("send_email reached AWS during a test")


# ---------------------------------------------------------------------------
# The regression: the invitation endpoint, with no AWS configuration at all
# ---------------------------------------------------------------------------

class TestInvitationWithoutAmbientAws:
    @pytest.fixture(name="lab")
    def lab_fixture(self, session):
        tenant = create_tenant(session, name="No-AWS Lab")
        create_branch(session, tenant)
        admin = create_user(session, tenant, email="admin@no-aws.test", roles=("admin",))
        return tenant, admin

    def test_an_invitation_can_be_created_with_no_aws_region_anywhere(
        self, client, session, lab, no_ambient_aws
    ):
        """The exact CI failure, as an assertion.

        Before the fix this raised `NoRegionError` out of `EmailService()` and
        the endpoint returned 500.
        """
        _tenant, admin = lab

        response = client.post(
            "/api/v1/users/invitations",
            json={
                "email": "invited@no-aws.test",
                "full_name": "Invited Person",
                "role": "lab_tech",
            },
            headers=auth_headers(admin),
        )

        assert response.status_code == 200, response.text
        assert response.json()["email"] == "invited@no-aws.test"

    def test_the_invitation_is_persisted_even_though_no_email_is_sent(
        self, client, session, lab, no_ambient_aws
    ):
        # Seat accounting and invitation storage are the product behaviour.
        # Email is a side effect, and an unconfigured environment must not be
        # able to take the primary behaviour down with it.
        _tenant, admin = lab

        response = client.post(
            "/api/v1/users/invitations",
            json={
                "email": "persisted@no-aws.test",
                "full_name": "Persisted Person",
                "role": "lab_tech",
            },
            headers=auth_headers(admin),
        )

        assert response.status_code == 200, response.text
        assert response.json()["token"]


# ---------------------------------------------------------------------------
# The mechanism: no client until a send genuinely happens
# ---------------------------------------------------------------------------

class TestNoClientIsBuiltEagerly:
    def test_no_client_is_built_on_construction(self, no_ambient_aws):
        """Mirrors `test_no_client_is_built_until_a_send_happens` on
        `SesEmailProvider`. Constructing the service resolves nothing."""
        service = EmailService()

        assert service._client is None

    def test_construction_does_not_raise_without_a_region(self, no_ambient_aws):
        EmailService()  # must not raise NoRegionError

    def test_no_client_is_built_when_delivery_is_disabled(
        self, monkeypatch, no_ambient_aws
    ):
        monkeypatch.setattr(settings, "email_enabled", False, raising=False)
        monkeypatch.setattr(settings, "email_sender", "notificaciones@celuma.test", raising=False)
        service = EmailService()

        sent = service.send_invitation_email(
            recipient_email="nobody@no-aws.test",
            recipient_name="Nobody",
            lab_name="Lab",
            invitation_url="https://app.celuma.test/accept-invitation?token=x",
        )

        assert sent is False
        assert service._client is None, "a disabled send must not construct an SES client"

    def test_no_client_is_built_when_the_sender_is_unconfigured(
        self, monkeypatch, no_ambient_aws
    ):
        monkeypatch.setattr(settings, "email_enabled", True, raising=False)
        monkeypatch.setattr(settings, "email_sender", None, raising=False)
        service = EmailService()

        sent = service.send_password_reset_email(
            recipient_email="nobody@no-aws.test",
            recipient_name="Nobody",
            reset_url="https://app.celuma.test/reset?token=x",
        )

        assert sent is False
        assert service._client is None


# ---------------------------------------------------------------------------
# EMAIL_ENABLED is the guard, for this path too
# ---------------------------------------------------------------------------

class TestDeliveryDisabledIsHonoured:
    @pytest.mark.parametrize(
        "send",
        [
            lambda s: s.send_invitation_email(
                recipient_email="a@b.test",
                recipient_name="A",
                lab_name="Lab",
                invitation_url="https://app.celuma.test/x",
            ),
            lambda s: s.send_password_reset_email(
                recipient_email="a@b.test",
                recipient_name="A",
                reset_url="https://app.celuma.test/x",
            ),
        ],
        ids=["invitation", "password-reset"],
    )
    def test_a_disabled_environment_sends_nothing_even_with_a_sender(
        self, monkeypatch, send
    ):
        """`EMAIL_ENABLED=false` is documented as the single guard between
        Céluma and a real inbox. Since SES production access was granted, the
        sandbox no longer backs that claim, so the legacy account-email path
        has to honour the flag too — a configured `EMAIL_SENDER` alone must not
        be enough to reach a real person.

        The injected client asserts if it is ever used, so this also proves no
        AWS call occurs.
        """
        monkeypatch.setattr(settings, "email_enabled", False, raising=False)
        monkeypatch.setattr(settings, "email_sender", "notificaciones@celuma.test", raising=False)

        assert send(EmailService(client=_ExplodingClient())) is False


class TestAnInjectedClientIsUsed:
    def test_a_send_uses_the_injected_client_and_never_builds_one(self, monkeypatch):
        """Closes the gap `block-e-dependencies.md` recorded: the legacy
        service was untestable because it built its own client."""
        monkeypatch.setattr(settings, "email_enabled", True, raising=False)
        monkeypatch.setattr(settings, "email_sender", "notificaciones@celuma.test", raising=False)

        calls: list[dict] = []

        class RecordingClient:
            def send_email(self, **kwargs):
                calls.append(kwargs)
                return {"MessageId": "test-message-id"}

        injected = RecordingClient()
        service = EmailService(client=injected)

        sent = service.send_invitation_email(
            recipient_email="invited@no-aws.test",
            recipient_name="Invited",
            lab_name="Lab",
            invitation_url="https://app.celuma.test/accept-invitation?token=x",
        )

        assert sent is True
        assert service._client is injected
        assert len(calls) == 1
        assert calls[0]["Destination"] == {"ToAddresses": ["invited@no-aws.test"]}
