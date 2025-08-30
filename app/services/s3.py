from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import boto3
from botocore.client import Config as BotoConfig
from app.core.config import settings


@dataclass
class S3ObjectInfo:
    """Information about an object stored in S3."""
    bucket: str
    key: str
    size_bytes: Optional[int]
    content_type: Optional[str]
    etag: Optional[str]
    version_id: Optional[str]


class S3Service:
    """Thin wrapper around boto3 for uploading and generating URLs."""

    def __init__(self) -> None:
        session_kwargs: dict[str, str] = {}
        if settings.aws_access_key_id and settings.aws_secret_access_key:
            session_kwargs["aws_access_key_id"] = settings.aws_access_key_id
            session_kwargs["aws_secret_access_key"] = settings.aws_secret_access_key

        self._session = boto3.session.Session(
            region_name=settings.aws_region,
            **session_kwargs,
        )

        client_kwargs: dict[str, object] = {
            "config": BotoConfig(signature_version="s3v4"),
        }
        if settings.s3_endpoint_url:
            client_kwargs["endpoint_url"] = settings.s3_endpoint_url

        self._client = self._session.client("s3", **client_kwargs)

    @property
    def bucket(self) -> str:
        if not settings.s3_bucket_name:
            raise RuntimeError("S3 bucket name is not configured")
        return settings.s3_bucket_name

    def upload_bytes(
        self,
        data: bytes,
        key: str,
        content_type: Optional[str] = None,
        acl: Optional[str] = None,
    ) -> S3ObjectInfo:
        extra_args: dict[str, str] = {}
        if content_type:
            extra_args["ContentType"] = content_type
        if acl:
            extra_args["ACL"] = acl

        response = self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            **({"ContentType": content_type} if content_type else {}),
            **({"ACL": acl} if acl else {}),
        )

        etag = response.get("ETag", None)
        version_id = response.get("VersionId", None)

        head = self._client.head_object(Bucket=self.bucket, Key=key)
        size = int(head.get("ContentLength", 0))

        return S3ObjectInfo(
            bucket=self.bucket,
            key=key,
            size_bytes=size,
            content_type=content_type,
            etag=etag.strip('"') if isinstance(etag, str) else None,
            version_id=version_id if isinstance(version_id, str) else None,
        )

    def generate_presigned_url(self, key: str, expires_in: Optional[int] = None) -> str:
        expiry = expires_in if expires_in is not None else settings.media_presigned_expire_seconds
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expiry,
        )

    def object_public_url(self, key: str) -> str:
        if settings.media_public_base_url:
            base = settings.media_public_base_url.rstrip("/")
            return f"{base}/{key}"
        region = settings.aws_region or "us-east-1"
        return f"https://{self.bucket}.s3.{region}.amazonaws.com/{key}"


