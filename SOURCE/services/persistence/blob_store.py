"""Content-addressed blob store interface for trace-v2 sidecars."""

from __future__ import annotations

import gzip
import hashlib
import os
import sqlite3
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

from cryptography.fernet import Fernet, InvalidToken

from core.platform import get_data_dir
from intent.log_redaction import redact_card_data, redact_pii

try:
    import zstandard as zstd
except ModuleNotFoundError:
    zstd = None

if TYPE_CHECKING:
    from collections.abc import Callable

    from services.persistence.trace_keys import KeyProvider

_BLOB_PREVIEW_CHARS = 240
_SQLITE_BUSY_TIMEOUT_MS = 5_000
_SQLITE_RETRY_ATTEMPTS = 5
_SQLITE_RETRY_BASE_DELAY_SECONDS = 0.025
_ZSTD_LEVEL = 3
_SHA256_HEX_LENGTH = 64
_LOWER_HEX_CHARS = frozenset("0123456789abcdef")
_FERNET_TOKEN_PREFIX = "gAAAA"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS blobs (
    sha256 TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    first_chars TEXT,
    ref_count INTEGER NOT NULL DEFAULT 0,
    first_seen_task_id TEXT,
    created_at TEXT NOT NULL,
    last_accessed_at TEXT
)
"""

_REF_COUNT_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_blobs_ref_count ON blobs(ref_count)"

_T = TypeVar("_T")


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _user_partition(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]


def _validate_sha256(sha256: str) -> None:
    if len(sha256) != _SHA256_HEX_LENGTH or any(char not in _LOWER_HEX_CHARS for char in sha256):
        raise ValueError("sha256 must be a 64-character lowercase hex string")


def _make_preview(content: bytes) -> str | None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None

    redacted = redact_pii(redact_card_data(text[:_BLOB_PREVIEW_CHARS]))
    return cast(str, redacted)[:_BLOB_PREVIEW_CHARS]


def _compress(content: bytes) -> bytes:
    if zstd is not None:
        return zstd.compress(content, level=_ZSTD_LEVEL)
    return gzip.compress(content)


def _decompress(content: bytes) -> bytes:
    if zstd is not None:
        try:
            return zstd.decompress(content)
        except zstd.ZstdError:
            return gzip.decompress(content)
    return gzip.decompress(content)


@dataclass(slots=True)
class BlobRef:
    """Reference to an encrypted, per-user sidecar blob."""

    sha256: str
    size: int
    content_type: str
    first_chars: str | None = None


class BlobStore:
    """Per-user encrypted blob sidecar API for trace-v2 payloads.

    Blob content is encrypted as sidecar bytes. The sqlite index keeps
    non-sensitive lookup metadata in cleartext, but stores ``first_chars`` as a
    Fernet-encrypted preview because it is user-content-derived. Preview
    decrypts are not content decrypts and do not write ``audit_decrypt`` rows;
    engineer-visible content reads are audited by their caller.
    """

    def __init__(
        self,
        user_id: str,
        key_provider: KeyProvider,
        root_dir: Path | None = None,
    ) -> None:
        """Create a blob store rooted under the user's v2 trace partition."""
        if not user_id:
            raise ValueError("user_id is required for BlobStore")

        self.user_id = user_id
        self._key_provider = key_provider
        self.root_dir = root_dir or get_data_dir() / "traces" / "by_user" / _user_partition(user_id) / "blobs"
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root_dir / "index.sqlite"
        self._write_lock = threading.Lock()
        self._cached_fernet: Fernet | None = None
        self._ensure_index()

    async def warm_up_async(self) -> None:
        """Pre-resolve the per-user Fernet key via the async key chain.

        After this returns, ``put`` and ``get`` reuse ``self._cached_fernet``
        instead of going through the sync ``KeyProvider.unwrap_trace_key``
        path. Call from agent_executor / trace setup BEFORE any sync
        ``put`` happens on the hot path so the cloud's main loop never
        trips the ASYNC-1 cross-loop guard.

        Idempotent.
        """
        if self._cached_fernet is None:
            self._cached_fernet = Fernet(await self._trace_key_async())

    def put(self, content: bytes, *, content_type: str = "application/json") -> BlobRef:
        """Store bytes and return a content-addressed blob reference.

        Sync entry point. If :meth:`warm_up_async` has been called, uses the
        cached Fernet (no cross-loop dispatch). Otherwise falls back to the
        sync ``self._trace_key()`` path which on cloud hits the ASYNC-1
        cross-loop guard — cloud callers MUST warm up first or use
        :meth:`put_async`.
        """
        fernet = self._cached_fernet or Fernet(self._trace_key())
        return self._put_with_key(content, fernet=fernet, content_type=content_type)

    async def put_async(self, content: bytes, *, content_type: str = "application/json") -> BlobRef:
        """Async-native variant of :meth:`put`.

        Awaits ``KeyProvider.unwrap_trace_key_async`` directly, avoiding the
        sync→async dispatch in ``self._trace_key()``. Use from agent loop /
        trace.append_event sites that already run in an async context.
        """
        fernet = Fernet(await self._trace_key_async())
        return self._put_with_key(content, fernet=fernet, content_type=content_type)

    def _put_with_key(
        self,
        content: bytes,
        *,
        fernet: Fernet,
        content_type: str,
    ) -> BlobRef:
        sha256 = hashlib.sha256(content).hexdigest()
        existing = self._get_index_row(sha256)
        if existing is not None:
            self.incref(sha256)
            return self._blob_ref_from_row(existing)

        first_chars = _make_preview(content)
        stored_first_chars = self._encrypt_first_chars(first_chars, fernet=fernet)
        encrypted = fernet.encrypt(_compress(content))
        self._atomic_write(self._blob_path(sha256), encrypted)

        def _insert() -> None:
            with closing(self._connect()) as conn:
                conn.execute(
                    """
                    INSERT INTO blobs (
                        sha256,
                        size,
                        content_type,
                        first_chars,
                        ref_count,
                        first_seen_task_id,
                        created_at,
                        last_accessed_at
                    )
                    VALUES (?, ?, ?, ?, 1, NULL, ?, NULL)
                    """,
                    (sha256, len(content), content_type, stored_first_chars, _utc_now_iso()),
                )

        conflict = False
        with self._write_lock:
            try:
                self._with_sqlite_retry(_insert)
            except sqlite3.IntegrityError:
                conflict = True

        if conflict:
            row = self._get_index_row(sha256)
            if row is not None:
                self.incref(sha256)
                return self._blob_ref_from_row(row)
            raise sqlite3.IntegrityError("blob index insert conflicted but row was not found")

        return BlobRef(sha256=sha256, size=len(content), content_type=content_type, first_chars=first_chars)

    def get(self, ref: BlobRef) -> bytes:
        """Return decrypted, decompressed blob bytes for a reference."""
        _validate_sha256(ref.sha256)
        encrypted = self._blob_path(ref.sha256).read_bytes()
        fernet = self._cached_fernet or Fernet(self._trace_key())
        compressed = fernet.decrypt(encrypted)
        content = _decompress(compressed)

        def _touch() -> None:
            with closing(self._connect()) as conn:
                conn.execute(
                    "UPDATE blobs SET last_accessed_at = ? WHERE sha256 = ?",
                    (_utc_now_iso(), ref.sha256),
                )

        with self._write_lock:
            self._with_sqlite_retry(_touch)
        return content

    def has(self, sha256: str) -> bool:
        """Return whether the user-local blob index contains a hash."""
        _validate_sha256(sha256)
        return self._get_index_row(sha256) is not None

    def incref(self, sha256: str) -> int:
        """Increment and return the reference count for a blob hash."""
        return self._adjust_ref_count(sha256, 1)

    def decref(self, sha256: str) -> int:
        """Decrement and return the reference count for a blob hash."""
        return self._adjust_ref_count(sha256, -1)

    def iter_orphans(self) -> Iterator[str]:
        """Yield blob hashes that have no live trace references."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT sha256 FROM blobs WHERE ref_count <= 0 ORDER BY created_at, sha256",
            ).fetchall()
        for row in rows:
            yield str(row["sha256"])

    def _ensure_index(self) -> None:
        def _ensure() -> None:
            with closing(self._connect()) as conn:
                conn.execute(_SCHEMA_SQL)
                conn.execute(_REF_COUNT_INDEX_SQL)

        with self._write_lock:
            self._with_sqlite_retry(_ensure)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._index_path),
            timeout=_SQLITE_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout = %d" % _SQLITE_BUSY_TIMEOUT_MS)
        return conn

    def _trace_key(self) -> bytes:
        return self._key_provider.unwrap_trace_key()

    async def _trace_key_async(self) -> bytes:
        return await self._key_provider.unwrap_trace_key_async()

    def _blob_path(self, sha256: str) -> Path:
        _validate_sha256(sha256)
        return self.root_dir / sha256[:2] / ("%s.blob.zst.enc" % sha256[2:])

    def _get_index_row(self, sha256: str) -> sqlite3.Row | None:
        _validate_sha256(sha256)

        def _fetch() -> sqlite3.Row | None:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT sha256, size, content_type, first_chars, ref_count FROM blobs WHERE sha256 = ?",
                    (sha256,),
                ).fetchone()
                return cast("sqlite3.Row | None", row)

        return self._with_sqlite_retry(_fetch)

    def _blob_ref_from_row(self, row: sqlite3.Row) -> BlobRef:
        sha256 = str(row["sha256"])
        return BlobRef(
            sha256=sha256,
            size=int(row["size"]),
            content_type=str(row["content_type"]),
            first_chars=self._read_first_chars(row["first_chars"]),
        )

    def _encrypt_first_chars(self, first_chars: str | None, *, fernet: Fernet | None = None) -> str | None:
        if first_chars is None:
            return None
        cipher = fernet or Fernet(self._trace_key())
        encrypted = cipher.encrypt(first_chars.encode("utf-8")).decode("ascii")
        return cast(str, encrypted)

    def _read_first_chars(self, stored_first_chars: Any) -> str | None:
        if stored_first_chars is None:
            return None
        if isinstance(stored_first_chars, bytes):
            try:
                stored_text = stored_first_chars.decode("ascii")
            except UnicodeDecodeError:
                raise ValueError("blob index first_chars could not be decoded") from None
        else:
            stored_text = str(stored_first_chars)

        if not stored_text.startswith(_FERNET_TOKEN_PREFIX):
            raise ValueError("blob index first_chars is not encrypted")
        try:
            decrypted = Fernet(self._trace_key()).decrypt(stored_text.encode("ascii")).decode("utf-8")
            return cast(str, decrypted)
        except (InvalidToken, UnicodeDecodeError, ValueError):
            raise ValueError("blob index first_chars could not be decrypted") from None

    def _adjust_ref_count(self, sha256: str, delta: int) -> int:
        _validate_sha256(sha256)

        def _adjust() -> int:
            with closing(self._connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT ref_count FROM blobs WHERE sha256 = ?", (sha256,)).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    raise KeyError(sha256)
                next_count = int(row["ref_count"]) + delta
                conn.execute("UPDATE blobs SET ref_count = ? WHERE sha256 = ?", (next_count, sha256))
                conn.execute("COMMIT")
                return next_count

        with self._write_lock:
            return self._with_sqlite_retry(_adjust)

    def _atomic_write(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as temp_file:
                temp_file.write(content)
                temp_path = Path(temp_file.name)
            os.replace(temp_path, path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def _with_sqlite_retry(self, operation: Callable[[], _T]) -> _T:
        for attempt in range(_SQLITE_RETRY_ATTEMPTS):
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == _SQLITE_RETRY_ATTEMPTS - 1:
                    raise
                time.sleep(_SQLITE_RETRY_BASE_DELAY_SECONDS * (2**attempt))
        raise RuntimeError("sqlite retry loop exhausted")
