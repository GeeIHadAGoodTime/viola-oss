"""
User-Scoped Storage Service.

Factory module that returns the appropriate storage backend based on
environment configuration:

- If ``VIOLA_S3_BUCKET`` is set -> S3StorageBackend (cloud/SaaS)
- Otherwise                     -> LocalStorageBackend (desktop)

Usage:
    >>> from services.storage import get_storage_backend
    >>> storage = get_storage_backend()
    >>> await storage.put("user123", "recordings", "call.wav", audio_bytes)
    >>> data = await storage.get("user123", "recordings", "call.wav")
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.logging_config import get_logger

if TYPE_CHECKING:
    from services.storage.backend import StorageBackend

logger = get_logger(__name__)

_backend: StorageBackend | None = None


def get_storage_backend() -> StorageBackend:
    """Get or create the global storage backend singleton.

    Selection logic:
    - If ``VIOLA_S3_BUCKET`` env var is set, uses S3StorageBackend
    - Otherwise, uses LocalStorageBackend with ``config.settings.data_dir``

    Returns:
        Configured StorageBackend instance.
    """
    global _backend

    if _backend is not None:
        return _backend

    from config import env

    s3_bucket = env.get("VIOLA_S3_BUCKET")

    if s3_bucket:
        from services.storage.s3_backend import S3StorageBackend

        region = env.get("VIOLA_S3_REGION", "us-east-1") or "us-east-1"
        endpoint = env.get("VIOLA_S3_ENDPOINT")

        _backend = S3StorageBackend(
            bucket=s3_bucket,
            region=region,
            endpoint_url=endpoint,
        )
        logger.info(
            "Storage backend: S3 (bucket=%s, region=%s)",
            s3_bucket,
            region,
        )
    else:
        from services.storage.local_backend import LocalStorageBackend

        _backend = LocalStorageBackend()
        logger.info("Storage backend: local filesystem")

    return _backend


def reset_storage_backend() -> None:
    """Reset the global storage backend singleton (for testing)."""
    global _backend
    _backend = None
