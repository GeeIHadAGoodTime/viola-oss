"""Compatibility facade for the markdown-first memory directory."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import core.db_backend as db_backend
from core.asyncio_safe import is_event_loop_closed_error, run_async_synchronously
from core.logging_config import get_logger
from services.memory.dir import MemoryDir, get_memory_dir

logger = get_logger(__name__)

_STORE_LOCK = threading.Lock()
_STORE_SINGLETON: MemoryStore | None = None
_MEMORY_SCHEMA_VERSION = 7
_PBKDF2_ITERATIONS = 600_000
_PBKDF2_LEGACY_ITERATIONS = 100_000
_LEGACY_SALT = b"viola-memory-store-v1"
_KEY_MISMATCH_MARKER = "[encrypted -- key mismatch]"

_VALID_CATEGORIES = frozenset({"preference", "fact", "correction", "routine", "note", "context"})

_SENSITIVE_CONTENT_RE = re.compile(
    r"(?i)"
    r"\b(?:password|passwd|passcode)\b\s*(?:is|=|:)\s*\S+"
    r"|\bpin(?:\s+(?:number|code))?\s*(?:is|=|:)?\s*\d{4,6}\b"
    r"|\b(?:api[_\-\s]?key|access[_\-\s]?token|refresh[_\-\s]?token|auth[_\-\s]?token|"
    r"id[_\-\s]?token|session[_\-\s]?token|oauth[_\-\s]?token)\b"
    r"\s*(?:is|=|:)\s*[A-Za-z0-9._~+/=\-]{16,}"
    r"|\bbearer\s+[A-Za-z0-9._~+/=\-]{12,}"
    r"|(?:sk|ghp|gho|glpat|xox[bpas])[_\-][A-Za-z0-9_\-]{10,}"
    r"|(?:secret|token)\s*(?:is|=|:)\s*\S{16,}"
    r"|(?:credit|debit)\s*card[\w\s]*\d[\d\-\s]{12,18}\d"
    r"|\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))[\-\s]?\d{4}[\-\s]?\d{4}[\-\s]?\d{3,4}\b"
    r"|\b(?:ssn|social\s+security\s+(?:number|num|no\.?))\b.*\d{3}[\-\s]?\d{2}[\-\s]?\d{4}"
    r"|\b\d{3}[\-\s]\d{2}[\-\s]\d{4}\b"
    r"|\b(?:cvv|cvc|cvv2|cvc2|security\s+code)\b.*\b\d{3,4}\b"
    r"|\b(?:routing\s+(?:number|num|no\.?)|account\s+(?:number|num|no\.?)|bank\s+account)\b.*\b\d{6,17}\b"
)
_CRISIS_CONTENT_RE = re.compile(
    r"(?i)"
    r"(?:i\s+want\s+to\s+(?:kill|hurt|harm)\s+(?:myself|my\s*self))"
    r"|(?:end\s+(?:it|my\s+life)\s+all)"
    r"|(?:(?:commit|plan(?:ning)?)\s+suicide)"
)
_PII_CONTENT_RE = re.compile(
    r"(?i)"
    # Bounded, non-overlapping email match. The local part excludes '@' and
    # whitespace; each domain label excludes '@', '.', and whitespace, so no
    # quantifier can overlap across the '@' or a '.'. The old `\S+@\S+\.\S+`
    # form let the three `\S+` runs overlap and backtracked super-linearly on a
    # long unbroken non-whitespace token (2026-07-05 prod event-loop wedge).
    r"(?P<email>[^\s@]{1,254}@[^\s@.]{1,63}(?:\.[^\s@.]{1,63}){1,8})"
    r"|(?P<ssn>\b\d{3}[\-\s]\d{2}[\-\s]\d{4}\b|(?<=\bssn\s)\d{9}|(?<=\bssn:\s)\d{9})"
    r"|(?P<cc>\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b)"
    r"|(?P<phone>\b\d{3}[-.]?\d{3}[-.]?\d{4}\b)"
)
_PROMPT_SENSITIVE_RE = re.compile(
    r"(?i)"
    r"\b(?:password|passwd|passcode|pin|secret|token|api[_\-\s]?key|cvv|cvc|cvv2|cvc2|security\s+code)\b"
    # Bound the secret-value run so no unbounded quantifier remains (defense in
    # depth against the 2026-07-05 ReDoS class; a real secret is never this long).
    r"\s*(?:is|=|:)?\s*[^\s,;]{1,256}"
    r"|\b(?:ssn|social\s+security\s+(?:number|num|no\.?))\b[^\r\n]{0,80}?\d{3}[\-\s]?\d{2}[\-\s]?\d{4}"
    r"|(?:credit|debit)\s*card[^\r\n]{0,80}?\d[\d\-\s]{12,18}\d"
    r"|\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))[\-\s]?\d{4}[\-\s]?\d{4}[\-\s]?\d{3,4}\b"
    r"|\b(?:routing\s+(?:number|num|no\.?)|account\s+(?:number|num|no\.?)|bank\s+account)\b"
    r"[^\r\n]{0,80}?\d{6,17}"
    r"|(?:sk|ghp|gho|glpat|xox[bpas])-[A-Za-z0-9]{10,}"
)

SENSITIVE_CONTENT_RE = _SENSITIVE_CONTENT_RE
CRISIS_CONTENT_RE = _CRISIS_CONTENT_RE


class MemoryEncryptionDisabledError(Exception):
    """Raised when a memory or conversation write cannot be encrypted."""


def get_database_url() -> str | None:
    return db_backend.get_database_url()


async def get_pg_pool() -> Any:
    return await db_backend.get_pg_pool()


def _memory_encryption_backend_key(db_path: Path | None, *, pg_url: str | None) -> tuple[Any, ...]:
    if db_path is not None:
        return ("sqlite", str(db_path.resolve()))
    if pg_url is not None:
        return ("postgresql", pg_url, id(get_pg_pool), id(db_backend.get_pg_pool))
    return ("sqlite", None)


def _run_async(coro: object) -> object:
    """Run memory encryption metadata coroutines from sync compatibility paths."""
    try:
        return run_async_synchronously(coro)
    except RuntimeError as exc:
        message = str(exc)
        if (
            "Cannot synchronously wait on the shared asyncio worker loop from itself" in message
            or "pool is bound to a different event loop" in message
        ):
            logger.warning("memory encryption metadata lookup blocked by event-loop guard; returning unavailable")
            try:
                coro.close()  # type: ignore[attr-defined]
            except Exception:
                pass
            return None
        raise


class _MemoryEncryption:
    """Compatibility encryption shim for conversation/task persistence."""

    # Per-backend cache: backend_key -> encryption instance. A single agent turn
    # touches more than one backend (SQLite conversation state + Postgres memory),
    # so a single shared slot thrashed and re-ran the 600k-round PBKDF2 key
    # derivation on every backend switch — 200-400ms GIL holds that froze the GUI.
    # Keying the cache by backend derives each backend's keys exactly once.
    # `_instance` is kept as the most-recent pointer for back-compat: existing tests
    # reset the cache by setting it to None, which is honored in _get_memory_encryption.
    _instances: dict[tuple[Any, ...], _MemoryEncryption] = {}
    _instance: _MemoryEncryption | None = None
    _DECRYPT_CACHE_SIZE = 256

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path
        self._pg_url = None if db_path is not None else get_database_url()
        self._key_mismatch_count = 0
        self._key_mismatch_last_log = 0.0
        self._secret_load_failed_loop_closed = False
        self._backend_key = _memory_encryption_backend_key(db_path, pg_url=self._pg_url)
        self._fernet = None
        self._fernet_legacy = None
        self._fernet_previous = None
        self._fernet_previous_legacy = None
        self._fernet_decrypt_only: tuple[Any, ...] = ()

        # SEC-028: the memory-encryption key is its own secret. It must NEVER
        # fall back to the session-token signing secret — key reuse means a
        # compromise of one subsystem grants the other.
        secret = os.environ.get("VIOLA_MEMORY_ENCRYPTION_KEY", "")
        previous_secret = os.environ.get("VIOLA_MEMORY_ENCRYPTION_KEY_PREVIOUS", "")
        if not secret and self._is_desktop_profile():
            secret = self._get_or_create_dev_secret()
            if secret:
                logger.info("Using generated per-install memory encryption secret for desktop/dev profile")
        if not secret:
            logger.critical("No memory encryption key configured; memory and conversation writes will be rejected")
        else:
            try:
                from functools import lru_cache
                from hashlib import pbkdf2_hmac

                from cryptography.fernet import Fernet

                salt = self._get_or_create_salt()
                if not salt:
                    raise MemoryEncryptionDisabledError("memory encryption salt is unavailable")

                def _derive_fernet(secret_value: str, salt_value: bytes, iterations: int) -> Fernet:
                    key_bytes = pbkdf2_hmac("sha256", secret_value.encode(), salt_value, iterations)
                    return Fernet(base64.urlsafe_b64encode(key_bytes))

                self._fernet = _derive_fernet(secret, salt, _PBKDF2_ITERATIONS)
                self._fernet_legacy = _derive_fernet(secret, _LEGACY_SALT, _PBKDF2_LEGACY_ITERATIONS)
                if previous_secret and previous_secret != secret:
                    self._fernet_previous = _derive_fernet(previous_secret, salt, _PBKDF2_ITERATIONS)
                    self._fernet_previous_legacy = _derive_fernet(
                        previous_secret,
                        _LEGACY_SALT,
                        _PBKDF2_LEGACY_ITERATIONS,
                    )
                decrypt_only: list[Any] = []
                for legacy_value in self._decrypt_only_legacy_secrets(secret):
                    decrypt_only.append(_derive_fernet(legacy_value, salt, _PBKDF2_ITERATIONS))
                    decrypt_only.append(_derive_fernet(legacy_value, _LEGACY_SALT, _PBKDF2_LEGACY_ITERATIONS))
                self._fernet_decrypt_only = tuple(decrypt_only)
                self._decrypt_cached = lru_cache(maxsize=self._DECRYPT_CACHE_SIZE)(self._decrypt_impl)
            except ImportError:
                logger.critical("cryptography package not available; encrypted memory writes will be rejected")
            except Exception:
                logger.exception("Failed to initialize memory encryption")
                self._fernet = None
                self._fernet_legacy = None
                self._fernet_previous = None
                self._fernet_previous_legacy = None
                self._fernet_decrypt_only = ()

    @staticmethod
    def _decrypt_only_legacy_secrets(active_secret: str) -> tuple[str, ...]:
        """Secrets that may have encrypted rows before SEC-028 key separation.

        Before 2026-06-09 the memory store fell back to the session-token
        signing secret when no dedicated memory key was configured. Installs
        that wrote rows under that fallback must stay readable, so the shared
        secret remains a DECRYPT-ONLY candidate. It is never used to encrypt:
        new writes always use the dedicated memory secret.
        """
        shared = os.environ.get("VIOLA_SECURITY_TOKEN_SECRET", "")
        if shared and shared != active_secret:
            return (shared,)
        return ()

    @property
    def encryption_available(self) -> bool:
        return self._fernet is not None

    def _is_desktop_profile(self) -> bool:
        try:
            from config.settings import settings as cfg

            return str(getattr(cfg, "app_surface", "desktop")).lower() != "cloud"
        except Exception:
            return True

    def _sqlite_metadata_path(self) -> Path:
        if self._db_path is not None:
            return self._db_path
        try:
            from config.settings import settings as cfg

            return Path(cfg.data_dir) / "data" / "persistence" / "state.sqlite3"
        except Exception:
            return Path.cwd() / "data" / "persistence" / "state.sqlite3"

    def _get_or_create_salt(self) -> bytes | None:
        if self._pg_url is not None:
            try:
                return cast(bytes | None, _run_async(self._pg_get_or_create_salt()))
            except Exception as exc:
                if is_event_loop_closed_error(exc):
                    logger.warning("Retrying memory encryption salt load after closed event loop")
                    return cast(bytes | None, _run_async(self._pg_get_or_create_salt()))
                raise

        db_path = self._sqlite_metadata_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS memory_metadata (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
            row = conn.execute("SELECT value FROM memory_metadata WHERE key = 'encryption_salt'").fetchone()
            if row is not None:
                return bytes(row[0])
            salt = secrets.token_bytes(32)
            conn.execute("INSERT INTO memory_metadata (key, value) VALUES ('encryption_salt', ?)", (salt,))
            conn.commit()
            logger.info("Generated new random encryption salt for memory store")
            return salt
        finally:
            conn.close()

    async def _pg_get_or_create_salt(self) -> bytes:
        pool = await get_pg_pool()
        async with pool.acquire() as conn, conn.transaction():
            from core.db_backend import assert_pg_relations

            schema = str(await conn.fetchval("SELECT current_schema()"))
            await assert_pg_relations(conn, ("%s.memory_metadata" % schema,), owner="MemoryStore")
            row = await conn.fetchrow("SELECT value FROM memory_metadata WHERE key = 'encryption_salt'")
            if row is not None:
                return bytes(row["value"])
            salt = secrets.token_bytes(32)
            await conn.execute("INSERT INTO memory_metadata (key, value) VALUES ('encryption_salt', $1)", salt)
            logger.info("Generated new random encryption salt for memory store")
            return salt

    def _get_or_create_dev_secret(self) -> str:
        """Per-install memory secret from the OS keystore, or its file fallback (SEC-022).

        The secret lives in the OS keystore via ``services.memory.key_provider``
        — never in a cleartext row next to the encrypted data. Legacy installs
        that stored ``encryption_dev_secret`` in ``memory_metadata`` are
        migrated: the row is copied into the keystore, verified by read-back,
        and only then deleted (keystore presence is the idempotency marker).

        When the OS keystore is unavailable (issue #341 — a locked/unreachable
        macOS Keychain, a headless Linux box with no Secret Service, etc.),
        ``key_provider`` falls back to a permissioned local file under the
        user's data directory before ever giving up, so a keystore outage no
        longer hard-fails memory encryption. Only when BOTH the keystore and
        the file fallback are unavailable does this method fall back to
        serving directly from an EXISTING legacy row (never locking users out
        of their memory, and never deleting the row until a verified copy
        exists elsewhere); a brand-new install with no legacy row and no
        working keystore/file fallback fails closed.
        """
        from services.memory import key_provider

        try:
            legacy_secret = self._read_legacy_dev_secret()
            secret, migrated = key_provider.get_or_create_keystore_secret(legacy_secret)
            if secret:
                if migrated:
                    self._delete_legacy_dev_secret()
                return secret
            if legacy_secret:
                logger.warning(
                    "OS keystore and file fallback both unavailable; serving memory encryption "
                    "secret from legacy metadata row"
                )
                return legacy_secret
            return ""
        except Exception as exc:  # noqa: BLE001, RUF100 - DB/keystore boundary; degrades to fail-closed unavailable
            if is_event_loop_closed_error(exc):
                self._secret_load_failed_loop_closed = True
            logger.error("Failed to load/create generated memory secret: %s", exc)
            return ""

    def _read_legacy_dev_secret(self) -> str:
        """Read (never create) the legacy cleartext dev-secret metadata row."""
        if self._pg_url is not None:
            return cast(str, _run_async(self._pg_read_legacy_dev_secret()) or "")
        db_path = self._sqlite_metadata_path()
        if not db_path.exists():
            return ""
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("SELECT value FROM memory_metadata WHERE key = 'encryption_dev_secret'").fetchone()
        except sqlite3.OperationalError:
            return ""  # no memory_metadata table on this install -> no legacy row
        finally:
            conn.close()
        if row is not None:
            return bytes(row[0]).decode("utf-8")
        return ""

    def _delete_legacy_dev_secret(self) -> None:
        """Remove the legacy cleartext row after its keystore copy verified."""
        try:
            if self._pg_url is not None:
                _run_async(self._pg_delete_legacy_dev_secret())
                return
            db_path = self._sqlite_metadata_path()
            if not db_path.exists():
                return
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("DELETE FROM memory_metadata WHERE key = 'encryption_dev_secret'")
                conn.commit()
                logger.info("Removed legacy cleartext memory secret row after keystore migration")
            finally:
                conn.close()
        except Exception:  # noqa: BLE001, RUF100 - cleanup retried next start; never breaks memory access
            logger.warning("Could not remove legacy memory secret row; will retry next start", exc_info=True)

    async def _pg_read_legacy_dev_secret(self) -> str:
        pool = await get_pg_pool()
        async with pool.acquire() as conn, conn.transaction():
            from core.db_backend import assert_pg_relations

            schema = str(await conn.fetchval("SELECT current_schema()"))
            await assert_pg_relations(conn, ("%s.memory_metadata" % schema,), owner="MemoryStore")
            row = await conn.fetchrow("SELECT value FROM memory_metadata WHERE key = 'encryption_dev_secret'")
            if row is not None:
                return bytes(row["value"]).decode("utf-8")
            return ""

    async def _pg_delete_legacy_dev_secret(self) -> None:
        pool = await get_pg_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM memory_metadata WHERE key = 'encryption_dev_secret'")
            logger.info("Removed legacy cleartext memory secret row after keystore migration")

    def encrypt(self, plaintext: str) -> str:
        if self._fernet is None:
            raise MemoryEncryptionDisabledError(
                "Cannot store memory: encryption is not configured. Set VIOLA_MEMORY_ENCRYPTION_KEY."
            )
        return self._fernet.encrypt(plaintext.encode()).decode()

    def _decrypt_impl(self, ciphertext: str) -> str:
        if self._fernet is None:
            return ciphertext

        from cryptography.fernet import InvalidToken

        def _try_decrypt(candidate: Any) -> str | None:
            try:
                return candidate.decrypt(ciphertext.encode()).decode()
            except (InvalidToken, UnicodeDecodeError):
                return None

        for candidate in (
            self._fernet,
            self._fernet_legacy,
            self._fernet_previous,
            self._fernet_previous_legacy,
            *tuple(getattr(self, "_fernet_decrypt_only", ()) or ()),
        ):
            if candidate is None:
                continue
            plaintext = _try_decrypt(candidate)
            if plaintext is not None:
                return plaintext
        if ciphertext.startswith("gAAAAA"):
            self._key_mismatch_count += 1
            now = time.monotonic()
            if self._key_mismatch_count == 1 or now - self._key_mismatch_last_log >= 60.0:
                self._key_mismatch_last_log = now
                logger.warning(
                    "Memory decryption failed; possible encryption key change (count=%d)",
                    self._key_mismatch_count,
                )
            return _KEY_MISMATCH_MARKER
        return ciphertext

    def decrypt(self, ciphertext: str) -> str:
        cached = getattr(self, "_decrypt_cached", None)
        if cached is not None:
            return cached(ciphertext)
        return self._decrypt_impl(ciphertext)

    @property
    def key_mismatch_count(self) -> int:
        return self._key_mismatch_count


def _get_memory_encryption(db_path: Path | None = None) -> _MemoryEncryption:
    # Back-compat reset contract: tests clear the cache by setting `_instance = None`.
    if _MemoryEncryption._instance is None:
        _MemoryEncryption._instances = {}
    current_pg_url = None if db_path is not None else get_database_url()
    expected_backend_key = _memory_encryption_backend_key(db_path, pg_url=current_pg_url)
    cached = _MemoryEncryption._instances.get(expected_backend_key)
    if cached is not None and not (current_pg_url is not None and not cached.encryption_available):
        _MemoryEncryption._instance = cached
        return cached
    instance = _MemoryEncryption(db_path=db_path)
    if current_pg_url is not None and not instance.encryption_available:
        instance = _MemoryEncryption(db_path=db_path)
    _MemoryEncryption._instances[expected_backend_key] = instance
    _MemoryEncryption._instance = instance
    return instance


@dataclass
class Memory:
    id: int
    user_id: str
    content: str
    category: str
    source: str
    confidence: float
    created_at: str
    updated_at: str
    accessed_at: str
    access_count: int
    superseded_by: int | None
    active: bool
    importance_score: float = 0.0
    critical: bool = False
    verified: bool = False
    path: str = ""
    line_number: int = 0


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def redact_pii_output(text: str) -> str:
    return _PII_CONTENT_RE.sub("[redacted]", text)


def redact_sensitive_prompt_text(text: str) -> str:
    redacted = redact_pii_output(str(text or ""))
    return _PROMPT_SENSITIVE_RE.sub("[redacted sensitive]", redacted)


def _map_category(category: str) -> str:
    value = (category or "fact").strip().lower()
    if value not in _VALID_CATEGORIES:
        raise ValueError("Invalid memory category %r" % category)
    return value


def _category_from_path(path: str) -> str:
    if path in {"MEMORY.md", "memory/MEMORY.md", "VIOLA_MEMORY.md"}:
        return "note"
    if path in {"VIOLA.md", "USER_MEMORY.md"}:
        return "fact"
    stem = Path(path).stem.lower()
    return {
        "preferences": "preference",
        "facts": "fact",
        "corrections": "correction",
        "routines": "routine",
        "context": "context",
        "notes": "note",
    }.get(stem, "note")


def _topic_for_category(category: str) -> str:
    # Claude-parity: every category routes to a topic file. Notes used to
    # land in MEMORY.md ("memory") which let raw agent memory content leak
    # into the always-loaded index. Route notes to topic:notes so MEMORY.md
    # stays a pure index of one-line pointers.
    return {
        "preference": "topic:preferences",
        "fact": "topic:facts",
        "correction": "topic:corrections",
        "routine": "topic:routines",
        "context": "topic:context",
        "note": "topic:notes",
    }.get(category, "topic:notes")


def _content_from_markdown_line(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("- "):
        return stripped[2:].strip()
    return stripped


_CONTEXT_MEMORY_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "user",
        "with",
    }
)


def _canonical_context_memory_content(content: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (content or "").lower()))


def _context_memory_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(token) > 1 and token not in _CONTEXT_MEMORY_STOPWORDS
    }


def _context_memory_relevance_score(query: str, content: str) -> float:
    query_tokens = _context_memory_tokens(query)
    content_tokens = _context_memory_tokens(content)
    if not query_tokens or not content_tokens:
        return 0.0

    query_canonical = _canonical_context_memory_content(query)
    content_canonical = _canonical_context_memory_content(content)
    if query_canonical and query_canonical == content_canonical:
        return 100.0 + len(query_tokens)

    overlap = query_tokens & content_tokens
    if not overlap:
        return 0.0

    phrase_match = bool(
        query_canonical
        and content_canonical
        and (query_canonical in content_canonical or content_canonical in query_canonical)
    )
    if len(query_tokens) >= 3 and len(overlap) < 2 and not phrase_match:
        return 0.0

    coverage = len(overlap) / len(query_tokens)
    density = len(overlap) / len(content_tokens)
    phrase_bonus = 5.0 if phrase_match else 0.0
    return (len(overlap) * 10.0) + (coverage * 3.0) + density + phrase_bonus


class MemoryStore:
    """Legacy ``MemoryStore`` API backed by :class:`services.memory.dir.MemoryDir`."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root
        self._lock = threading.RLock()

    def _dir(self, user_id: str) -> MemoryDir:
        return get_memory_dir(user_id, root=self._root)

    @staticmethod
    def _memory_id(file_index: int, line_number: int) -> int:
        return file_index * 100000 + line_number

    @staticmethod
    def _decode_memory_id(memory_id: int) -> tuple[int, int]:
        return divmod(int(memory_id), 100000)

    def _iter_memories(self, user_id: str) -> list[Memory]:
        directory = self._dir(user_id)
        now = _utcnow_iso()
        rows: list[Memory] = []
        files = directory.list()
        for file_index, info in enumerate(files, start=1):
            path = str(info["path"])
            if not path.startswith("memory/topics/"):
                continue
            text = directory.read(path)
            category = _category_from_path(path)
            frontmatter_end = 0
            lines = text.splitlines()
            if lines and lines[0].strip() == "---":
                for index, line in enumerate(lines[1:], start=2):
                    if line.strip() == "---":
                        frontmatter_end = index
                        break
            for line_number, line in enumerate(lines, start=1):
                if frontmatter_end and line_number <= frontmatter_end:
                    continue
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                content = _content_from_markdown_line(stripped)
                if not content:
                    continue
                rows.append(
                    Memory(
                        id=self._memory_id(file_index, line_number),
                        user_id=user_id,
                        content=content,
                        category=category,
                        source="markdown",
                        confidence=1.0,
                        created_at=str(info.get("modified_at") or now),
                        updated_at=str(info.get("modified_at") or now),
                        accessed_at=now,
                        access_count=0,
                        superseded_by=None,
                        active=True,
                        path=path,
                        line_number=line_number,
                    )
                )
        return rows

    def add(
        self,
        user_id: str,
        content: str,
        category: str = "fact",
        source: str = "explicit",
        confidence: float = 1.0,
        critical: bool = False,
    ) -> int:
        del source, confidence, critical
        clean = content.strip()
        if len(clean) < 5:
            raise ValueError("Memory content too short (min 5 chars)")
        if _SENSITIVE_CONTENT_RE.search(clean):
            raise ValueError("Cannot store sensitive credentials or payment data in memory")
        if _CRISIS_CONTENT_RE.search(clean):
            raise ValueError("Crisis or self-harm statements should not be stored in memory")
        category = _map_category(category)
        result = self._dir(user_id).store_semantic_memory(clean, category=category, agent_initiated=True)
        files = self._dir(user_id).list()
        file_index = next(
            (index for index, info in enumerate(files, start=1) if info.get("path") == result["path"]),
            1,
        )
        return self._memory_id(file_index, int(result["line_number"]))

    async def add_async(
        self,
        user_id: str,
        content: str,
        category: str = "fact",
        source: str = "explicit",
        confidence: float = 1.0,
        critical: bool = False,
    ) -> int:
        return await asyncio.to_thread(self.add, user_id, content, category, source, confidence, critical)

    def search(self, user_id: str, query: str, limit: int = 10) -> list[Memory]:
        tokens = [token for token in re.findall(r"[A-Za-z0-9_'-]+", (query or "").lower()) if len(token) > 1]
        if not tokens:
            return []
        scored: list[tuple[int, Memory]] = []
        for memory in self._iter_memories(user_id):
            lower = memory.content.lower()
            score = sum(1 for token in tokens if token in lower)
            if score:
                scored.append((score, memory))
        scored.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
        return [memory for _score, memory in scored[: max(1, int(limit))]]

    async def search_async(self, user_id: str, query: str, limit: int = 10) -> list[Memory]:
        return await asyncio.to_thread(self.search, user_id, query, limit)

    def get_all(self, user_id: str, category: str | None = None) -> list[Memory]:
        category_value = _map_category(category) if category else None
        memories = self._iter_memories(user_id)
        if category_value:
            memories = [memory for memory in memories if memory.category == category_value]
        return memories

    async def get_all_async(self, user_id: str, category: str | None = None) -> list[Memory]:
        return await asyncio.to_thread(self.get_all, user_id, category)

    def get_recent(self, user_id: str, limit: int = 10) -> list[Memory]:
        return self.get_all(user_id)[: max(1, int(limit))]

    def get_by_category(self, user_id: str, category: str, limit: int = 10) -> list[Memory]:
        return self.get_all(user_id, category=category)[: max(1, int(limit))]

    def get_by_id(self, user_id: str, memory_id: int) -> Memory | None:
        return next((memory for memory in self._iter_memories(user_id) if memory.id == int(memory_id)), None)

    async def get_by_id_async(self, user_id: str, memory_id: int) -> Memory | None:
        return await asyncio.to_thread(self.get_by_id, user_id, memory_id)

    def get_context_memories(self, user_id: str, query: str, budget: int = 5) -> list[Memory]:
        """Return memories clearly relevant to ``query``.

        Claude-parity (S10-MEM-002): returns ``[]`` when the query is empty,
        has no meaningful tokens, or no memory clearly matches. We do NOT fall
        back to recent memories — injecting unrelated context biases the model
        (recent memories surfacing for irrelevant queries was the original
        regression). Empty is a valid answer; see Claude Code's
        ``findRelevantMemories`` which also returns ``[]`` rather than padding.

        Within the relevance set we still use the local token-overlap
        scoring (``_context_memory_relevance_score``) to rank candidates
        deterministically — Claude's selector LLM path is out of scope here
        because this code runs in the live request hot loop with no LLM call
        allowed. Selector-style metadata recall lives in ``dir.py``'s
        ``find_relevant_memories_by_metadata`` for callers that want the
        Claude-style frontmatter manifest path.
        """
        limit = max(1, int(budget))
        if not _context_memory_tokens(query):
            return []

        results = self.search(user_id, query, limit=limit * 4)
        if not results:
            return []

        seen: set[str] = set()
        ranked: list[tuple[float, float, float, int, str, int, Memory]] = []
        for index, memory in enumerate(results):
            canonical = _canonical_context_memory_content(memory.content)
            if not canonical or canonical in seen:
                continue
            score = _context_memory_relevance_score(query, memory.content)
            if score <= 0.0:
                continue
            seen.add(canonical)
            ranked.append(
                (
                    score,
                    memory.importance_score,
                    memory.confidence,
                    memory.access_count,
                    memory.updated_at,
                    -index,
                    memory,
                )
            )

        ranked.sort(reverse=True)
        return [memory for *_rank, memory in ranked[:limit]]

    def dump_all(self, user_id: str) -> dict[str, list[Memory]]:
        grouped: dict[str, list[Memory]] = {}
        for memory in self.get_all(user_id):
            grouped.setdefault(memory.category, []).append(memory)
        return grouped

    def update_memory(
        self,
        user_id: str,
        memory_id: int,
        content: str | None = None,
        critical: bool | None = None,
    ) -> bool:
        del critical
        memory = self.get_by_id(user_id, memory_id)
        if memory is None:
            return False
        if content is None:
            return True
        clean = content.strip()
        if len(clean) < 5:
            raise ValueError("Memory content too short (min 5 chars)")
        if _SENSITIVE_CONTENT_RE.search(clean):
            raise ValueError("Cannot store sensitive credentials or payment data in memory")
        replacement = clean if clean.startswith(("-", "#")) else "- %s" % clean
        directory = self._dir(user_id)
        lines = directory.read(memory.path).splitlines()
        if memory.line_number < 1 or memory.line_number > len(lines):
            return False
        lines[memory.line_number - 1] = replacement
        directory.write("\n".join(lines), where=memory.path, position="replace", agent_initiated=True)
        return True

    def deactivate(self, user_id: str, memory_id: int) -> bool:
        memory = self.get_by_id(user_id, memory_id)
        if memory is None:
            return False
        result = self._dir(user_id).delete_line(memory.path, memory.line_number, agent_initiated=True)
        return bool(result.get("ok"))

    async def deactivate_async(self, user_id: str, memory_id: int) -> bool:
        return await asyncio.to_thread(self.deactivate, user_id, memory_id)

    def supersede(self, user_id: str, old_id: int, new_content: str, category: str = "fact") -> int:
        self.deactivate(user_id, old_id)
        return self.add(user_id, new_content, category=category)

    def touch(self, user_id: str, memory_id: int) -> None:
        del user_id, memory_id

    async def touch_async(self, user_id: str, memory_id: int) -> None:
        await asyncio.to_thread(self.touch, user_id, memory_id)

    def delete_all_for_user(self, user_id: str) -> int:
        count = self.count_active(user_id)
        directory = self._dir(user_id)
        for info in directory.list():
            relative_path = str(info["path"])
            if relative_path == "VIOLA.md":
                path = directory.viola_path
            else:
                path = directory.root / relative_path.removeprefix("memory/")
            path.write_text("", encoding="utf-8")
        marker = directory.root / ".legacy_memory_migrated"
        marker.write_text(_utcnow_iso(), encoding="utf-8")
        directory._audit("delete_all", "memory/", "deleted=%d" % count, agent_initiated=False)
        return count

    async def delete_user_memories(self, user_id: str) -> int:
        return await asyncio.to_thread(self.delete_all_for_user, user_id)

    def count_active(self, user_id: str) -> int:
        return len(self._iter_memories(user_id))

    def get_memory_index(self, user_id: str) -> list[tuple[str, str, int]]:
        return [(memory.category, memory.content[:120], memory.id) for memory in self.get_all(user_id)]

    def stats(self, user_id: str) -> dict[str, Any]:
        return self._dir(user_id).stats()

    def get_conflicts(self, user_id: str) -> list[Memory]:
        del user_id
        return []

    def promote_verified_memories(self, user_id: str, memory_ids: list[int]) -> int:
        del user_id, memory_ids
        return 0

    def close(self) -> None:
        return


def get_memory_store(root: Path | None = None) -> MemoryStore:
    global _STORE_SINGLETON
    if root is not None:
        return MemoryStore(root=root)
    with _STORE_LOCK:
        if _STORE_SINGLETON is None:
            _STORE_SINGLETON = MemoryStore()
        return _STORE_SINGLETON


def reset_memory_store_for_tests() -> None:
    global _STORE_SINGLETON
    with _STORE_LOCK:
        _STORE_SINGLETON = None
    _MemoryEncryption._instances = {}
    _MemoryEncryption._instance = None


__all__ = [
    "CRISIS_CONTENT_RE",
    "SENSITIVE_CONTENT_RE",
    "Memory",
    "MemoryEncryptionDisabledError",
    "MemoryStore",
    "_MemoryEncryption",
    "_get_memory_encryption",
    "get_memory_store",
    "redact_pii_output",
    "redact_sensitive_prompt_text",
    "reset_memory_store_for_tests",
]
