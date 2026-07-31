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

    # Céluma 1.3 Fase 2, Bloque D, Historia D1: the CORS origin list used to
    # be a literal hardcoded in app/main.py (Bloque C, Historia C11 — see the
    # comment there for why a bare "*" cannot be one of the origins while
    # `allow_credentials=True`). Moved here so the production frontend origin
    # can be configured without a code change. Comma-separated, no bare "*".
    cors_allowed_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:4173,http://127.0.0.1:4173"
    )

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

settings = Settings()
