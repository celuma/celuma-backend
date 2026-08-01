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
        elif settings.aws_region:
            # Céluma 1.3 Fase 2, Bloque E: without an explicit endpoint, boto3
            # can resolve the global `s3.amazonaws.com` endpoint for
            # presigned URLs, which AWS rejects for opt-in regions (e.g.
            # `mx-central-1`, the configured bucket region here) with
            # IllegalLocationConstraintException. Forcing the region-specific
            # endpoint makes presigned URLs (and everything else this client
            # does) work regardless of which region the bucket is in. First
            # actually exercised by this block — every earlier caller of
            # generate_presigned_url had no real frontend caller (see
            # pdf-storage-integrity-contract.md).
            client_kwargs["endpoint_url"] = f"https://s3.{settings.aws_region}.amazonaws.com"

        self._client = self._session.client("s3", **client_kwargs)

    @property
    def bucket(self) -> str:
        if not settings.s3_bucket_name:
            raise RuntimeError("S3 bucket name is not configured")
        return settings.s3_bucket_name

    @property
    def region(self) -> str:
        """Public accessor for the configured AWS region.

        Falls back to settings.aws_region and finally to 'mx-central-1'.
        """
        return (self._session.region_name or settings.aws_region or "mx-central-1")

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

    def generate_presigned_url(
        self,
        key: str,
        expires_in: Optional[int] = None,
        response_content_disposition: Optional[str] = None,
    ) -> str:
        expiry = expires_in if expires_in is not None else settings.media_presigned_expire_seconds
        params: dict[str, str] = {"Bucket": self.bucket, "Key": key}
        if response_content_disposition:
            params["ResponseContentDisposition"] = response_content_disposition
        return self._client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expiry,
        )

    def object_public_url(self, key: str) -> str:
        if settings.media_public_base_url:
            base = settings.media_public_base_url.rstrip("/")
            return f"{base}/{key}"
        region = settings.aws_region or "mx-central-1"
        return f"https://{self.bucket}.s3.{region}.amazonaws.com/{key}"

    def download_bytes(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        data: bytes = response["Body"].read()
        return data

    def download_text(self, key: str, encoding: str = "utf-8") -> str:
        return self.download_bytes(key).decode(encoding)

    def delete_object(self, key: str) -> None:
        """Delete an object from the configured bucket. No-op if it does not exist."""
        self._client.delete_object(Bucket=self.bucket, Key=key)


