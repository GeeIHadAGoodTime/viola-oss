"""
Storage Backend Protocol for User-Scoped File Storage.

Provides a unified interface for storing user files, with implementations
for local filesystem (desktop) and S3/R2 (cloud SaaS).

All paths are scoped by (user_id, category) to enforce per-user isolation.

Categories represent logical groups of user data:
- "recordings"  - phone call recordings
- "snapshots"   - state snapshots
- "wake_clips"  - wake word audio clips
- "uploads"     - user-uploaded files
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class StorageBackend(Protocol):
    """Protocol for user-scoped file storage backends.

    Implementations must handle path sanitization and ensure that a user
    cannot access another user's files via path traversal.
    """

    async def put(self, user_id: str, category: str, filename: str, data: bytes) -> str:
        """Store a file and return its storage key/path.

        Args:
            user_id: Owner user ID.
            category: Logical category (e.g. "recordings", "snapshots").
            filename: Filename (no path separators allowed).
            data: Raw file bytes.

        Returns:
            Storage key or path that can be used with get/delete.

        Raises:
            ValueError: If filename contains path separators.
        """
        ...

    async def get(self, user_id: str, category: str, filename: str) -> bytes | None:
        """Retrieve a file's contents.

        Args:
            user_id: Owner user ID.
            category: Logical category.
            filename: Filename.

        Returns:
            File bytes, or None if the file does not exist.
        """
        ...

    async def delete(self, user_id: str, category: str, filename: str) -> bool:
        """Delete a file.

        Args:
            user_id: Owner user ID.
            category: Logical category.
            filename: Filename.

        Returns:
            True if the file was deleted, False if it didn't exist.
        """
        ...

    async def list_files(self, user_id: str, category: str) -> list[str]:
        """List filenames in a user's category directory.

        Args:
            user_id: Owner user ID.
            category: Logical category.

        Returns:
            List of filenames (not full paths), sorted alphabetically.
        """
        ...
