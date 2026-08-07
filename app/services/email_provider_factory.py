"""Provider selection (Céluma 1.3, Phase 3, Block E, Story E2).

One function, one job: turn `EMAIL_PROVIDER` into an `EmailProvider`.

It is its own module rather than a function on `email_provider.py` because the
abstraction must not import its implementations — `email_provider_ses.py`
imports `boto3`, and a module that every caller imports in order to get a type
would then drag the AWS SDK into processes that never send anything, including
the test suite. The dependency direction stays:

    worker  ->  factory  ->  ses / fake  ->  abstraction

`Settings.email_provider` is already validated against `EMAIL_PROVIDERS` at
import, so the `ValueError` below is unreachable through configuration. It is
kept as the honest answer to a direct call with a bad name, rather than a
silent fall back to SES — which would mean a typo in an environment variable
sends real email.
"""
from __future__ import annotations

from app.core.config import settings
from app.services.email_provider import EmailProvider


def build_email_provider(name: str | None = None) -> EmailProvider:
    """The provider `name` (default: `EMAIL_PROVIDER`) names.

    Imports are function-local, so selecting the fake provider never imports
    boto3 and vice versa.
    """
    selected = (name or settings.email_provider or "").strip().lower()

    if selected == "fake":
        from app.services.email_provider_fake import FakeEmailProvider

        return FakeEmailProvider()

    if selected == "ses":
        from app.services.email_provider_ses import SesEmailProvider

        return SesEmailProvider()

    raise ValueError(f"Unknown email provider: {selected!r}")
