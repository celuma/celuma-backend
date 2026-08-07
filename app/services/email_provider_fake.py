"""Fake email provider (Céluma 1.3, Phase 3, Block E, Story E4).

An `EmailProvider` that records what it was asked to send and never leaves the
process. **No AWS import, no network, no credentials** — importing this module
must stay possible on a machine that has never heard of AWS, which is what
lets the entire backend test suite run without them.

Why this lives in `app/` and not in `tests/`
--------------------------------------------
It is selectable in production configuration (`EMAIL_PROVIDER=fake`), which
makes it a runtime component, not test tooling:

- a developer can run the whole delivery pipeline locally — worker, claim,
  render, resolve — and read the messages out of the recorder, with no AWS
  account at all;
- the Block E local validation report exercises the real worker against it;
- putting it under `tests/` would mean the only way to run delivery locally is
  to have SES credentials, which is precisely the barrier that left the old
  `EmailService` untested.

The `FakeS3Service` in `tests/http/conftest.py` is the established precedent
for the shape; the difference is that this one is reachable by configuration
rather than only by monkeypatch, because there is a provider setting to reach
it with.

Determinism
-----------
Every behaviour is scripted explicitly and consumed in order. Nothing is
random, nothing is time-dependent, and nothing carries over between instances
— two tests that build their own provider cannot influence each other.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.services.email_provider import (
    ERROR_PROVIDER_UNAVAILABLE,
    ERROR_SES_THROTTLED,
    EmailMessage,
    EmailProvider,
    EmailProviderError,
    EmailProviderHealth,
    EmailSendResult,
)


@dataclass(frozen=True)
class RecordedEmail:
    """One message the fake was asked to send, kept whole.

    This is the one place in Céluma where a subject, a body and a recipient
    address are deliberately retained together — because the point of the fake
    is to let a test assert that a rendered subject contains no patient
    reference. It is in-memory only, never logged, never persisted, and the
    provider is not selectable in a deployed environment by default.
    """

    to_address: str
    subject: str
    text_body: str
    html_body: Optional[str]
    from_address: str
    from_name: str
    provider_message_id: str


@dataclass
class FakeEmailProvider(EmailProvider):
    """Records sends; fails exactly when it is told to.

    Four ways to script a failure, in the order they are consulted:

    1. `fail_addresses` — always fail for this recipient. Models a permanently
       bad mailbox, and is what a "one recipient fails, the others still get
       their mail" test needs.
    2. `fail_next_n` — fail the next N sends, then succeed. Models a transient
       outage, and is how a retry test drives a delivery to `SENT` on its
       third attempt without touching the clock.
    3. `throttle_after` — succeed this many times, then raise `ses_throttled`
       forever. Models a rate limit.
    4. Otherwise succeed.

    `health_result` overrides what `health()` reports, so a worker test can
    exercise the unhealthy-provider path without an unhealthy provider.
    """

    name: str = "fake"

    #: Every send that succeeded, in order.
    sent: List[RecordedEmail] = field(default_factory=list)
    #: Every send that failed, as `(to_address, error_code)`, in order. Kept
    #: separately so `len(sent)` means "delivered" without qualification.
    failed: List[tuple] = field(default_factory=list)

    #: Scripting.
    fail_addresses: Dict[str, str] = field(default_factory=dict)
    fail_next_n: int = 0
    fail_next_code: str = ERROR_PROVIDER_UNAVAILABLE
    throttle_after: Optional[int] = None
    health_result: Optional[EmailProviderHealth] = None

    #: Deterministic message ids: `fake-message-1`, `fake-message-2`, ...
    _ids: itertools.count = field(
        default_factory=lambda: itertools.count(1), repr=False
    )

    # -- EmailProvider -----------------------------------------------------

    def send(self, message: EmailMessage) -> EmailSendResult:
        address_code = self.fail_addresses.get(message.to_address)
        if address_code:
            self.failed.append((message.to_address, address_code))
            raise EmailProviderError(address_code)

        if self.fail_next_n > 0:
            self.fail_next_n -= 1
            self.failed.append((message.to_address, self.fail_next_code))
            raise EmailProviderError(self.fail_next_code)

        if self.throttle_after is not None and len(self.sent) >= self.throttle_after:
            self.failed.append((message.to_address, ERROR_SES_THROTTLED))
            raise EmailProviderError(ERROR_SES_THROTTLED)

        message_id = f"fake-message-{next(self._ids)}"
        self.sent.append(
            RecordedEmail(
                to_address=message.to_address,
                subject=message.subject,
                text_body=message.text_body,
                html_body=message.html_body,
                from_address=message.from_address,
                from_name=message.from_name,
                provider_message_id=message_id,
            )
        )
        return EmailSendResult(provider_message_id=message_id)

    def health(self) -> EmailProviderHealth:
        if self.health_result is not None:
            return self.health_result
        return EmailProviderHealth(
            provider=self.name,
            configured=True,
            credentials_present=True,
            reachable=True,
            context={"sent_count": len(self.sent)},
        )

    # -- test/dev affordances ----------------------------------------------

    def reset(self) -> None:
        """Forget every recorded message and every scripted failure."""
        self.sent.clear()
        self.failed.clear()
        self.fail_addresses.clear()
        self.fail_next_n = 0
        self.throttle_after = None
        self.health_result = None

    def sent_to(self, address: str) -> List[RecordedEmail]:
        return [record for record in self.sent if record.to_address == address]

    @property
    def sent_count(self) -> int:
        return len(self.sent)
