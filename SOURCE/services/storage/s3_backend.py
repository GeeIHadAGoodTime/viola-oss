"""
S3/R2-Compatible Storage Backend.

Stores user files in an S3-compatible bucket with the key pattern
``{user_id}/{category}/{filename}``.

Configuration is via environment variables (read through config.settings):
- ``VIOLA_S3_BUCKET``   - bucket name (required to use this backend)
- ``VIOLA_S3_REGION``   - AWS region (default: us-east-1)
- ``VIOLA_S3_ENDPOINT`` - custom endpoint URL for R2, MinIO, etc.

boto3 is an *optional* dependency. The backend raises ImportError clearly
if boto3 is not installed when operations are attempted.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


def _sanitize_component(name: str) -> str:
    """Sanitize a path component for use as an S3 key segment."""
    cleaned = name.replace("/", "").replace("\\", "").replace("\0", "").strip()
    if not cleaned or cleaned in (".", ".."):
        raise ValueError("Invalid path component: %r" % name)
    return cleaned


class S3StorageBackend:
    """S3/R2-compatible object storage backend.

    Key pattern: ``{user_id}/{category}/{filename}``

    All operations run boto3 calls in a thread pool to avoid blocking
    the async event loop.
    """

    def __init__(
        self,
        bucket: str,
        region: str = "us-east-1",
        endpoint_url: str | None = None,
    ) -> None:
        """Initialize S3 backend.

        Args:
            bucket: S3 bucket name.
            region: AWS region.
            endpoint_url: Custom endpoint (for R2, MinIO, etc.).
        """
        self._bucket = bucket
        self._region = region
        self._endpoint_url = endpoint_url
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazy-initialize the boto3 S3 client."""
        if self._client is None:
            try:
                import boto3
            except ImportError:
                raise ImportError("boto3 is required for S3 storage backend. " "Install it with: pip install boto3")

            kwargs: dict[str, Any] = {
                "service_name": "s3",
                "region_name": self._region,
            }
            if self._endpoint_url:
                kwargs["endpoint_url"] = self._endpoint_url

            self._client = boto3.client(**kwargs)

        return self._client

    def _key(self, user_id: str, category: str, filename: str) -> str:
        """Build the S3 object key."""
        return "%s/%s/%s" % (
            _sanitize_component(user_id),
            _sanitize_component(category),
            _sanitize_component(filename),
        )

    async def put(self, user_id: str, category: str, filename: str, data: bytes) -> str:
        """Upload a file to S3."""
        key = self._key(user_id, category, filename)
        client = self._get_client()

        await asyncio.to_thread(
            client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=data,
        )

        logger.debug("S3 put: %s (%d bytes)", key, len(data))
        return key

    async def get(self, user_id: str, category: str, filename: str) -> bytes | None:
        """Download a file from S3."""
        key = self._key(user_id, category, filename)
        client = self._get_client()

        try:
            response = await asyncio.to_thread(
                client.get_object,
                Bucket=self._bucket,
                Key=key,
            )
            body = response["Body"]
            data = await asyncio.to_thread(body.read)
            return data
        except client.exceptions.NoSuchKey:
            return None
        except Exception as exc:
            # Catch ClientError for missing keys across providers
            exc_str = str(exc).lower()
            if "nosuchkey" in exc_str or "not found" in exc_str or "404" in exc_str:
                return None
            raise

    async def delete(self, user_id: str, category: str, filename: str) -> bool:
        """Delete a file from S3.

        Note: S3 DeleteObject is idempotent and doesn't error on missing keys.
        We return True unconditionally since S3 doesn't distinguish.
        """
        key = self._key(user_id, category, filename)
        client = self._get_client()

        await asyncio.to_thread(
            client.delete_object,
            Bucket=self._bucket,
            Key=key,
        )

        logger.debug("S3 delete: %s", key)
        return True

    async def list_files(self, user_id: str, category: str) -> list[str]:
        """List files in a user's category prefix."""
        prefix = "%s/%s/" % (
            _sanitize_component(user_id),
            _sanitize_component(category),
        )
        client = self._get_client()

        response = await asyncio.to_thread(
            client.list_objects_v2,
            Bucket=self._bucket,
            Prefix=prefix,
        )

        files: list[str] = []
        for obj in response.get("Contents", []):
            key: str = obj["Key"]
            # Extract filename from key (strip prefix)
            name = key[len(prefix) :]
            if name and "/" not in name:
                files.append(name)

        return sorted(files)
