from pydantic import field_validator
from pydantic_settings import BaseSettings

#: Céluma 1.3 Phase 3, Block E, Story E1: the providers `email_provider` may
#: name. Declared here rather than imported from the service layer so that
#: validating configuration never imports boto3 — a value typo must fail on a
#: string comparison, not on an SDK import.
EMAIL_PROVIDERS: tuple[str, ...] = ("ses", "fake")

#: Bounds on the worker's poll interval. The lower bound stops a typo (`0`)
#: turning the loop into a busy-wait against PostgreSQL; the upper bound stops
#: one turning delivery into something that looks broken. Block A's delivery
#: strategy §5 recommends 10–15 s, which is the default.
MIN_DELIVERY_POLL_INTERVAL_SECONDS = 1
MAX_DELIVERY_POLL_INTERVAL_SECONDS = 3600

#: Bounds on the reconciliation worker's interval (Céluma 1.3 Phase 4,
#: Block D). The lower bound is minutes rather than seconds because a
#: reconciliation cycle HEADs every billable object of every active tenant:
#: a typo'd `0` here would not merely busy-wait against PostgreSQL, it would
#: hammer S3. The upper bound (a week) keeps "effectively never" from being
#: expressible as a value that looks configured.
MIN_USAGE_RECONCILIATION_INTERVAL_SECONDS = 60
MAX_USAGE_RECONCILIATION_INTERVAL_SECONDS = 604800

#: Bounds on the staleness threshold. Below a minute, a legitimately slow
#: run would be "recovered" out from under itself while still working.
MIN_USAGE_RECONCILIATION_STALE_SECONDS = 60
MAX_USAGE_RECONCILIATION_STALE_SECONDS = 604800


class Settings(BaseSettings):
    app_name: str = "celuma"
    env: str = "dev"
    database_url: str
    jwt_secret: str
    jwt_expires_min: int = 480

    # AWS S3 configuration
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_region: str | None = None
    s3_bucket_name: str | None = None
    s3_endpoint_url: str | None = None  # Optional for custom endpoints / localstack

    # Media configuration
    media_public_base_url: str | None = None  # Optional CDN/base URL for public access
    media_presigned_expire_seconds: int = 3600

    # Céluma 1.3 Phase 2, Block D, Story D1: the CORS origin list used to
    # be a literal hardcoded in app/main.py (Block C, Story C11 — see the
    # comment there for why a bare "*" cannot be one of the origins while
    # `allow_credentials=True`). Moved here so the production frontend origin
    # can be configured without a code change. Comma-separated, no bare "*".
    cors_allowed_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:4173,http://127.0.0.1:4173"
    )

    # Third post-Phase-2 remediation: the in-memory rate limiter in
    # app/main.py had its two numbers hardcoded. The default (100/60s per
    # IP) is unchanged, so production behavior does not change; it becomes
    # configurable because the real E2E suite fires all its traffic from a
    # single IP and exhausted the window mid-run — producing 429s on tests
    # that had nothing wrong. See remediation-3-e2e-report.md.
    rate_limit_max_requests: int = 100
    rate_limit_window_seconds: int = 60

    # Céluma 1.3 Phase 2, Block E: official PDF generation. `pdf_generator_base_url`
    # is the origin of the frontend the headless browser navigates to render
    # `/internal/report-render/...` — intentionally has no localhost default so an
    # environment can never silently fall back to the wrong origin; it must be set
    # explicitly in `.env`. `pdf_render_token_secret` is distinct from `jwt_secret`:
    # falls back to `jwt_secret` only if unset, but a dedicated secret is
    # recommended so render tokens and user sessions never share a key.
    pdf_generator_base_url: str | None = None
    pdf_generation_timeout_seconds: int = 30
    pdf_render_token_expires_seconds: int = 90
    pdf_render_token_secret: str | None = None
    pdf_max_size_bytes: int = 25 * 1024 * 1024  # 25 MB
    pdf_max_page_count: int = 100

    # Second post-Phase-2 remediation (UX): staleness window for the
    # sign-and-publish claim (publish_started_at/by on ReportVersion),
    # mirroring pdf_generation_timeout_seconds's `* 3` staleness pattern.
    # Larger than pure PDF generation because the claim also spans the
    # signature-embedding JSON rewrite and the final publish transaction.
    report_publish_timeout_seconds: int = 45

    # Céluma 1.3 Phase 3, Block D: the NotificationDelivery retry lifecycle.
    # Block D creates PENDING rows and owns the state machine; nothing
    # processes them yet, so these values configure a machine that Block E
    # will drive. They are settings rather than module constants for the same
    # reason `pdf_generation_timeout_seconds` is: an operator tuning retry
    # pressure against a real provider must not need a code change.
    #
    # `max_attempts` bounds retry amplification (the risk named in Block A's
    # analysis). Backoff is deterministic — min(base * 2^(attempts-1), max) —
    # i.e. 60s, 2m, 4m, 8m, capped at 1h; no jitter, because with one
    # in-process poller there is no thundering herd to spread, and Block E can
    # add provider-aware jitter when there is a provider to be aware of.
    #
    # `stale_sending_seconds` is how long a row may sit claimed (SENDING)
    # before it is treated as abandoned by a crashed worker. 900s follows the
    # existing `* 3`-style staleness convention scaled to a send: generous
    # relative to any single provider call, tight enough that a crash does not
    # strand a delivery for hours.
    notification_delivery_max_attempts: int = 5
    notification_delivery_base_backoff_seconds: int = 60
    notification_delivery_max_backoff_seconds: int = 3600
    notification_delivery_stale_sending_seconds: int = 900
    notification_delivery_claim_batch_size: int = 50

    # Céluma 1.3 Phase 3, Block E, Story E1: email delivery.
    #
    # These are the settings `app/services/email.py`, `app/api/v1/auth.py` and
    # `app/api/v1/users.py` have been reading through a defaulted attribute
    # lookup against fields that **did not exist** — so the lookup could not
    # fail, and every environment silently used the hardcoded literal instead.
    # Including production, where `celuma-infra` has been setting FRONTEND_URL
    # on the task definition all along (backend_stack.py, environment_vars)
    # and the backend ignored it. Declaring them makes the deployed values
    # take effect and makes a missing one a visible failure instead of a wrong
    # default.
    #
    # Every defaulted-lookup fallback for them was removed in the same block,
    # and `tests/test_email_configuration.py` greps `app/` to keep them gone.
    #
    # `email_enabled` is the master switch for the delivery worker, and it
    # defaults to **False** on purpose:
    #
    #   - No SES identity, DKIM record or `ses:SendEmail` IAM grant exists in
    #     any environment yet (block-e-dependencies.md §15), so a worker that
    #     started by default would do nothing but accumulate failed attempts
    #     against real delivery rows.
    #   - Nothing creates a notification in production until Block F, so there
    #     is nothing to deliver.
    #   - It is what keeps the worker out of the test suite without making
    #     production code test-aware: `TestClient` runs FastAPI's lifespan, so
    #     a worker gated on anything *other* than configuration would start
    #     under pytest.
    #
    # Turning email on is therefore a deliberate, per-environment act.
    email_enabled: bool = False
    email_provider: str = "ses"
    # No default. A fallback sender is the exact bug this block is closing:
    # `noreply@celuma.com` is not a verified SES identity, so the silent
    # default guaranteed every send was rejected. Unset now means "email is
    # not configured", which the worker reports and refuses to run on.
    email_sender: str | None = None
    email_sender_name: str = "Céluma"
    # Céluma runs in `mx-central-1`, where Amazon SES is not offered, so the
    # SES client cannot simply reuse `aws_region` the way `S3Service` does.
    # Falls back to `aws_region` when unset (see `effective_email_ses_region`)
    # so a region where SES *is* available needs no extra configuration.
    email_ses_region: str | None = None
    # The origin the "log in to Céluma" call to action points at. Only ever
    # used as a bare origin: content policy §3 forbids a notification email
    # from deep-linking into a protected resource or carrying a signed URL.
    frontend_url: str = "http://localhost:5173"
    # Block A's delivery strategy §5 recommends 10–15 s for the in-process
    # poller. It is a setting rather than a constant for the same reason the
    # retry values above are: tuning delivery latency against a real provider
    # must not need a code change.
    delivery_poll_interval_seconds: int = 10

    # Céluma 1.3 Phase 4, Block D: usage reconciliation.
    #
    # `usage_reconciliation_enabled` is the master switch for the in-process
    # reconciliation worker, and it defaults to **False** for the same three
    # reasons `email_enabled` does:
    #
    #   - Shipping this code must not start scheduled S3 traffic in any
    #     existing environment. Turning reconciliation on is a deliberate,
    #     per-environment act.
    #   - The manual admin endpoint (`POST /api/v1/tenant/usage/reconcile`)
    #     works regardless of this flag, so operators have the capability
    #     from day one without a background loop running.
    #   - `TestClient` runs FastAPI's lifespan, so a worker gated on
    #     anything other than configuration would start under pytest.
    #
    # The interval is hours, not seconds: a reconciliation HEADs every
    # billable object and lists four key prefixes per tenant, which is
    # expensive and — because drift accrues slowly — pointless to repeat
    # often. 6h is roughly "four times a day, none of them during a
    # deploy".
    #
    # `stale_seconds` is how long a RUNNING row may sit before it is treated
    # as abandoned by a dead process and failed by stale-run recovery. It
    # must comfortably exceed the slowest realistic single-tenant run (a
    # large tenant's HEAD sweep) — 1h, following the same generous-but-
    # bounded convention as `notification_delivery_stale_sending_seconds`.
    #
    # Repair and S3 verification are separately switchable: an operator
    # investigating an incident may legitimately want detection with no
    # counter mutation (`repair=false`), or an accounting-only pass when S3
    # is degraded (`s3_verify=false`).
    usage_reconciliation_enabled: bool = False
    usage_reconciliation_interval_seconds: int = 21600  # 6 hours
    usage_reconciliation_stale_seconds: int = 3600  # 1 hour
    usage_reconciliation_repair_enabled: bool = True
    usage_reconciliation_s3_verify_enabled: bool = True

    class Config:
        env_file = ".env"

    # -- Céluma 1.3 Phase 3, Block E, Story E1: configuration validation ----
    #
    # Two tiers, deliberately.
    #
    # **Field validators** (below) run at import, because they check
    # invariants that are always wrong however email is configured — an
    # unknown provider name, a poll interval of zero — and a process that
    # cannot answer "which provider?" has no safe way to continue.
    #
    # **Cross-field validation** (`validate_email_configuration`) runs at
    # *worker startup*, not at import, because it depends on `email_enabled`.
    # Raising at import for a missing `EMAIL_SENDER` would mean a
    # misconfigured mailbox stops the API from booting — inverting
    # architectural principle §4.3/§4.7, which say a clinical operation must
    # never depend on email. Instead the worker refuses to start, logs why,
    # and everything else in Céluma runs untouched.

    @field_validator("email_provider")
    @classmethod
    def _validate_email_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in EMAIL_PROVIDERS:
            raise ValueError(
                f"EMAIL_PROVIDER must be one of {', '.join(EMAIL_PROVIDERS)} "
                f"(got {normalized!r})"
            )
        return normalized

    @field_validator("delivery_poll_interval_seconds")
    @classmethod
    def _validate_delivery_poll_interval(cls, value: int) -> int:
        if not (
            MIN_DELIVERY_POLL_INTERVAL_SECONDS
            <= value
            <= MAX_DELIVERY_POLL_INTERVAL_SECONDS
        ):
            raise ValueError(
                "DELIVERY_POLL_INTERVAL_SECONDS must be between "
                f"{MIN_DELIVERY_POLL_INTERVAL_SECONDS} and "
                f"{MAX_DELIVERY_POLL_INTERVAL_SECONDS} seconds (got {value})"
            )
        return value

    @field_validator("usage_reconciliation_interval_seconds")
    @classmethod
    def _validate_usage_reconciliation_interval(cls, value: int) -> int:
        if not (
            MIN_USAGE_RECONCILIATION_INTERVAL_SECONDS
            <= value
            <= MAX_USAGE_RECONCILIATION_INTERVAL_SECONDS
        ):
            raise ValueError(
                "USAGE_RECONCILIATION_INTERVAL_SECONDS must be between "
                f"{MIN_USAGE_RECONCILIATION_INTERVAL_SECONDS} and "
                f"{MAX_USAGE_RECONCILIATION_INTERVAL_SECONDS} seconds (got {value})"
            )
        return value

    @field_validator("usage_reconciliation_stale_seconds")
    @classmethod
    def _validate_usage_reconciliation_stale(cls, value: int) -> int:
        if not (
            MIN_USAGE_RECONCILIATION_STALE_SECONDS
            <= value
            <= MAX_USAGE_RECONCILIATION_STALE_SECONDS
        ):
            raise ValueError(
                "USAGE_RECONCILIATION_STALE_SECONDS must be between "
                f"{MIN_USAGE_RECONCILIATION_STALE_SECONDS} and "
                f"{MAX_USAGE_RECONCILIATION_STALE_SECONDS} seconds (got {value})"
            )
        return value

    @field_validator("frontend_url")
    @classmethod
    def _validate_frontend_url(cls, value: str) -> str:
        candidate = value.strip().rstrip("/")
        if not candidate.startswith(("http://", "https://")):
            raise ValueError(
                "FRONTEND_URL must be an absolute http(s) origin "
                "(it is rendered into email as a link)"
            )
        return candidate

    @field_validator("email_sender_name")
    @classmethod
    def _validate_email_sender_name(cls, value: str) -> str:
        # This string is interpolated into a `From:` header. A line break in
        # it is header injection — an attacker-controlled `Bcc:` — so it is
        # rejected rather than stripped, and the quote characters that would
        # break the display-name quoting go with it.
        candidate = value.strip()
        if not candidate:
            raise ValueError("EMAIL_SENDER_NAME must not be empty")
        if any(char in candidate for char in "\r\n\"<>"):
            raise ValueError(
                "EMAIL_SENDER_NAME must not contain line breaks, quotes or "
                "angle brackets (it is rendered into a From header)"
            )
        return candidate

    @property
    def effective_email_ses_region(self) -> str | None:
        return self.email_ses_region or self.aws_region

    def validate_email_configuration(self) -> list[str]:
        """Every reason email delivery cannot run, or an empty list.

        Returns rather than raises: the only caller is the worker's startup
        path, which must degrade to "delivery is off" instead of taking the
        API down with it. Each message names a variable and is safe to log —
        no value is echoed back, so a misconfigured sender address never
        reaches a log line.
        """
        problems: list[str] = []
        if not self.email_enabled:
            return problems

        if not (self.email_sender or "").strip():
            problems.append("EMAIL_SENDER is not set")
        elif "@" not in self.email_sender or any(
            char in self.email_sender for char in "\r\n <>,"
        ):
            problems.append("EMAIL_SENDER is not a bare email address")

        if self.email_provider == "ses" and not self.effective_email_ses_region:
            problems.append(
                "EMAIL_SES_REGION (or AWS_REGION) is not set, and the SES "
                "provider cannot resolve an endpoint without one"
            )
        return problems

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        origins = [origin.strip() for origin in self.cors_allowed_origins.split(",")]
        origins = [origin for origin in origins if origin]
        if "*" in origins:
            raise ValueError(
                "CORS_ALLOWED_ORIGINS must not contain a bare '*' — combined with "
                "allow_credentials=True this breaks every credentialed cross-origin "
                "request (see app/main.py CORSMiddleware comment)."
            )
        return origins

    @property
    def effective_pdf_render_token_secret(self) -> str:
        return self.pdf_render_token_secret or self.jwt_secret

settings = Settings()
