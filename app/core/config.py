from pydantic_settings import BaseSettings

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

    class Config:
        env_file = ".env"

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
