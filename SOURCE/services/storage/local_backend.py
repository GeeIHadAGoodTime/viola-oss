"""
Local Filesystem Storage Backend.

Stores user files at ``{data_dir}/user_data/{user_id}/{category}/{filename}``.
Suitable for desktop/single-user deployments.

Security:
- Filenames are sanitized to prevent path traversal.
- Directories are created with owner-only permissions on non-Windows platforms.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from core.logging_config import get_logger

logger = get_logger(__name__)


def _sanitize_component(name: str) -> str:
    """Sanitize a path component to prevent traversal attacks.

    Strips path separators and rejects empty or dot-only names.

    Raises:
        ValueError: If the name is invalid.
    """
    # Strip any path separators
    cleaned = name.replace("/", "").replace("\\", "").replace("\0", "").strip()

    if not cleaned or cleaned in (".", ".."):
        raise ValueError("Invalid path component: %r" % name)

    return cleaned


class LocalStorageBackend:
    """Local filesystem storage with user-scoped directories.

    Files are stored at ``{base_dir}/user_data/{user_id}/{category}/{filename}``.
    """

    def __init__(self, base_dir: str | Path | None = None) -> None:
        """Initialize local storage.

        Args:
            base_dir: Root data directory. If None, uses ``config.settings.data_dir``.
        """
        if base_dir is None:
            from config.settings import settings

            base_dir = settings.data_dir

        self._base = Path(base_dir) / "user_data"

    def _user_dir(self, user_id: str, category: str) -> Path:
        """Build the directory path for a user's category."""
        safe_user = _sanitize_component(user_id)
        safe_category = _sanitize_component(category)
        return self._base / safe_user / safe_category

    def _ensure_dir(self, dir_path: Path) -> None:
        """Create directory with secure permissions."""
        dir_path.mkdir(parents=True, exist_ok=True)
        # Restrict to owner-only on non-Windows
        if sys.platform != "win32":
            try:
                os.chmod(dir_path, stat.S_IRWXU)
            except OSError:
                pass

    async def put(self, user_id: str, category: str, filename: str, data: bytes) -> str:
        """Store a file to the local filesystem."""
        safe_name = _sanitize_component(filename)
        target_dir = self._user_dir(user_id, category)
        self._ensure_dir(target_dir)

        file_path = target_dir / safe_name
        file_path.write_bytes(data)

        key = "%s/%s/%s" % (
            _sanitize_component(user_id),
            _sanitize_component(category),
            safe_name,
        )
        logger.debug("Stored file: %s (%d bytes)", key, len(data))
        return key

    async def get(self, user_id: str, category: str, filename: str) -> bytes | None:
        """Retrieve a file from the local filesystem."""
        safe_name = _sanitize_component(filename)
        file_path = self._user_dir(user_id, category) / safe_name

        if not file_path.is_file():
            return None

        return file_path.read_bytes()

    async def delete(self, user_id: str, category: str, filename: str) -> bool:
        """Delete a file from the local filesystem."""
        safe_name = _sanitize_component(filename)
        file_path = self._user_dir(user_id, category) / safe_name

        if not file_path.is_file():
            return False

        file_path.unlink()
        logger.debug("Deleted file: %s/%s/%s", user_id, category, safe_name)
        return True

    async def list_files(self, user_id: str, category: str) -> list[str]:
        """List files in a user's category directory."""
        target_dir = self._user_dir(user_id, category)

        if not target_dir.is_dir():
            return []

        return sorted(f.name for f in target_dir.iterdir() if f.is_file())
