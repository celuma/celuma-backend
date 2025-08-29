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

    class Config:
        env_file = ".env"

settings = Settings()
