"""Claude-style per-account markdown memory directories."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from hashlib import pbkdf2_hmac
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from core.platform import get_data_dir
from services.memory.hygiene import (
    HYGIENE_SETTINGS,
    MemoryHygienePolicy,
    canonical_memory_text,
    validate_entry_content,
    validate_file_content,
    word_overlap,
)

logger = get_logger(__name__)

VIOLA_FILE = "VIOLA.md"
MEMORY_INDEX = "MEMORY.md"
TOPICS_DIR = "topics"
AUDIT_LOG = "audit.log"
ARCHIVE_DIR = "archive"

USER_MEMORY = "USER_MEMORY.md"
VIOLA_MEMORY = "VIOLA_MEMORY.md"
USER_PENDING = "USER_PENDING.md"

MEMORY_VIOLA_MD_MAX_BYTES = "memory_viola_md_max_bytes"
MEMORY_MAIN_MD_MAX_BYTES = "memory_main_md_max_bytes"
MEMORY_TOPIC_MD_MAX_BYTES = "memory_topic_md_max_bytes"

_DEFAULT_VIOLA_MD_MAX_BYTES = 32 * 1024
# MEMORY.md is the always-loaded index. Cap matches Claude Code's
# MAX_ENTRYPOINT_BYTES (25 KiB) / MAX_ENTRYPOINT_LINES (200) so that the index
# stays concise. Topic files keep the larger cap because their content is only
# loaded on relevance-based recall, not on every turn.
_DEFAULT_MAIN_MD_MAX_BYTES = 25 * 1024
_DEFAULT_MAIN_MD_MAX_LINES = 200
_DEFAULT_TOPIC_MD_MAX_BYTES = 128 * 1024
_ARCHIVE_TARGET_RATIO = 0.80
_RECENT_LINE_PROTECTION_SECONDS = 60.0

_PBKDF2_ITERATIONS = 600_000
_PBKDF2_LEGACY_ITERATIONS = 100_000
_LEGACY_SALT = b"viola-memory-store-v1"
_KEY_MISMATCH_MARKER = "[encrypted -- key mismatch]"
_ENCRYPTED_MARKDOWN_PREFIX = "viola-memory-fernet-v1:"
_RECENT_MEMORY_LINES: dict[str, list[tuple[float, str]]] = {}
# Per-user in-memory transient store of recent archive events. self_knowledge
# reads from this to surface a one-time mention in the next agent turn after a
# memory cap-hit. Architecture choice: self_knowledge is the right channel for
# state ("memory was recently archived for this user") rather than a push-style
# notification. MemoryDir is synchronous and the messaging hub is async; routing
# through self_knowledge avoids that mismatch entirely and keeps the UX subtle —
# Viola sees the state and can mention it organically if relevant.
import threading

_ARCHIVE_NOTICE_TTL_SECONDS = 300.0  # 5 minutes — long enough to span a conversation
_archive_notices_lock = threading.Lock()
_archive_notices: dict[str, dict[str, Any]] = {}


def record_archive_notice(user_id: str, file_rel: str, bytes_moved: int) -> None:
    """Record an archive event so self_knowledge can surface it in the next turn."""
    if not user_id:
        return
    now = time.time()
    with _archive_notices_lock:
        entry = _archive_notices.get(user_id) or {"events": []}
        # Prune events older than TTL
        entry["events"] = [ev for ev in entry.get("events", []) if (now - ev["when"]) < _ARCHIVE_NOTICE_TTL_SECONDS]
        entry["events"].append({"when": now, "file": file_rel, "bytes": int(bytes_moved)})
        _archive_notices[user_id] = entry


def get_recent_archive_notice(user_id: str) -> dict[str, Any] | None:
    """Return summary of recent archive events for ``user_id`` or None if none/expired."""
    if not user_id:
        return None
    now = time.time()
    with _archive_notices_lock:
        entry = _archive_notices.get(user_id)
        if not entry:
            return None
        recent = [ev for ev in entry.get("events", []) if (now - ev["when"]) < _ARCHIVE_NOTICE_TTL_SECONDS]
        if not recent:
            _archive_notices.pop(user_id, None)
            return None
        # Keep the pruned list so future reads benefit
        entry["events"] = recent
        return {
            "event_count": len(recent),
            "total_bytes": sum(ev["bytes"] for ev in recent),
            "newest_when": max(ev["when"] for ev in recent),
            "files": sorted({ev["file"] for ev in recent}),
        }


def clear_archive_notices(user_id: str | None = None) -> None:
    """Clear archive notices; used by tests and reset flows."""
    with _archive_notices_lock:
        if user_id is None:
            _archive_notices.clear()
        else:
            _archive_notices.pop(user_id, None)


@dataclass(frozen=True)
class _ArchivePlan:
    archive_path: Path
    archived_text: str
    cap_bytes: int
    target_bytes: int
    projected_bytes: int
    live_bytes: int

    @property
    def bytes_moved(self) -> int:
        return len(self.archived_text.encode("utf-8"))


@dataclass(frozen=True)
class MemoryFile:
    path: str
    bytes: int
    lines: int
    modified_at: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _text_bytes(text: str) -> int:
    return len((text or "").encode("utf-8"))


def _line_key(line: str) -> str:
    return line.rstrip("\r\n")


def safe_account_id(user_id: str) -> str:
    cleaned = (user_id or "").strip()
    if not cleaned:
        raise ValueError("user_id is required for memory isolation")
    return re.sub(r"[^A-Za-z0-9_.-]", "_", cleaned)


def account_root(user_id: str, root: Path | None = None) -> Path:
    base = auto_memory_base_dir(root)
    return base / "users" / safe_account_id(user_id)


def _topic_slug(topic: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", (topic or "").strip().lower()).strip("-")
    if not slug:
        raise ValueError("topic name is required")
    return slug


def _entry_id(line_number: int) -> str:
    return "line:%d" % line_number


def _parse_markdown_entries(content: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    in_fence = False
    lines = content.splitlines()
    frontmatter_end = 0
    if lines and lines[0].strip() == _FRONTMATTER_DELIMITER:
        for index, line in enumerate(lines[1:], start=2):
            if line.strip() == _FRONTMATTER_DELIMITER:
                frontmatter_end = index
                break
    for line_number, line in enumerate(lines, start=1):
        if frontmatter_end and line_number <= frontmatter_end:
            continue
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", stripped)
        if heading:
            title = heading.group(2).strip()
            entries.append(
                {
                    "id": "section:%s" % title,
                    "kind": "section",
                    "level": len(heading.group(1)),
                    "line_number": line_number,
                    "text": title,
                    "markdown": stripped,
                }
            )
            continue
        bullet = re.match(r"^[-*]\s+(.+)$", stripped)
        numbered = re.match(r"^\d+\.\s+(.+)$", stripped)
        if bullet or numbered:
            text = (bullet or numbered).group(1).strip()
            entries.append(
                {
                    "id": _entry_id(line_number),
                    "kind": "bullet",
                    "line_number": line_number,
                    "text": text,
                    "markdown": stripped,
                }
            )
            continue
        entries.append(
            {
                "id": _entry_id(line_number),
                "kind": "line",
                "line_number": line_number,
                "text": stripped,
                "markdown": stripped,
            }
        )
    return entries


# --- Frontmatter scan (Claude-parity recall) ---------------------------------
#
# Claude Code's recall path scans only the first N lines of each topic file,
# extracts YAML frontmatter (description, type, etc.), formats a one-line
# manifest, and asks a selector LLM to pick up to 5 relevant filenames. This
# avoids substring-search over rendered markdown (which lets unrelated content
# leak into context any time a keyword happens to match).
#
# Viola mirrors the scan + manifest. Selection is deterministic / non-LLM
# here (no live LLM calls allowed from this path); the manifest is exposed
# so callers can plug in a selector. ``find_relevant_memories_by_metadata``
# provides a baseline metadata-driven selector that only matches against
# name / description / type and returns ``[]`` when nothing is clearly
# useful — matching Claude's "be selective; empty is valid" contract.

_FRONTMATTER_MAX_LINES = 30
_MAX_MEMORY_FILES = 200
_FRONTMATTER_DELIMITER = "---"
# Frontmatter keys we extract - kept narrow on purpose. Anything else is
# ignored; we never present full file content to the selector.
_FRONTMATTER_KEYS = ("name", "description", "type")
_FALSE_ENV_VALUES = {"0", "false", "no", "off", "n"}
_TRUE_ENV_VALUES = {"1", "true", "yes", "on", "y"}
_SEMANTIC_MEMORY_TOPICS = {
    "preference": ("preferences", "Preferences", "user", "User preferences and personal choices"),
    "fact": ("facts", "Facts", "user", "User facts and personal details"),
    "correction": ("corrections", "Corrections", "feedback", "User feedback and corrections"),
    "routine": ("routines", "Routines", "user", "User routines and recurring habits"),
    "context": ("context", "Context", "project", "Project or task context not derivable from the repo"),
    "note": ("notes", "Notes", "reference", "Useful pointers and miscellaneous notes"),
}


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized in _TRUE_ENV_VALUES:
        return True
    if normalized in _FALSE_ENV_VALUES:
        return False
    return None


def _settings_attr(name: str, default: Any = None) -> Any:
    try:
        from config.settings import settings
    except ImportError:
        return default
    return getattr(settings, name, default)


def auto_memory_disabled_reason() -> str | None:
    """Return a reason when automatic memory features should not run."""
    env_disable = _env_bool("VIOLA_DISABLE_AUTO_MEMORY")
    if env_disable is None:
        env_disable = _env_bool("CLAUDE_CODE_DISABLE_AUTO_MEMORY")
    if env_disable is True:
        return "disabled_by_env"
    if env_disable is False:
        return None

    if _env_bool("VIOLA_SIMPLE") is True or _env_bool("CLAUDE_CODE_SIMPLE") is True:
        return "simple_mode"

    app_surface = str(_settings_attr("app_surface", "desktop") or "desktop").strip().lower()
    remote_mode = app_surface in {"cloud", "remote"} or _env_bool("VIOLA_REMOTE") is True
    remote_memory_dir = os.environ.get("VIOLA_REMOTE_MEMORY_DIR", "").strip()
    configured_dir = (
        os.environ.get("VIOLA_AUTO_MEMORY_DIRECTORY", "").strip()
        or str(_settings_attr("auto_memory_directory", "") or "").strip()
    )
    if remote_mode and not remote_memory_dir and not configured_dir:
        return "remote_without_memory_dir"

    if _settings_attr("auto_memory_enabled", True) is False:
        return "disabled_by_settings"

    return None


def is_auto_memory_enabled() -> bool:
    return auto_memory_disabled_reason() is None


def _expand_memory_path(raw: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(raw.strip()))
    return Path(expanded).resolve()


def _sanitize_project_key(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", normalized).strip("-")
    return slug or "project"


@lru_cache(maxsize=16)
def _canonical_git_project_key(cwd: str) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=cwd,
            capture_output=True,
            check=True,
            text=True,
            timeout=2.0,
        )  # proc-tree-ok: single git binary, no shell, no grandchildren
        common_dir = Path(result.stdout.strip()).resolve()
        root = common_dir.parent if common_dir.name == ".git" else Path(cwd).resolve()
    except (OSError, subprocess.SubprocessError, ValueError):
        root = Path(cwd).resolve()
    return _sanitize_project_key(str(root))


def _project_scoped_memory_enabled() -> bool:
    env_value = _env_bool("VIOLA_AUTO_MEMORY_PROJECT_SCOPE_ENABLED")
    if env_value is not None:
        return env_value
    return bool(_settings_attr("auto_memory_project_scope_enabled", False))


def auto_memory_base_dir(root: Path | None = None) -> Path:
    """Resolve the base directory for per-user memory roots.

    ``root`` remains an explicit test/migration override. Without it, Viola
    stays on the existing per-user app-data layout by default. Project-scoped
    memory is opt-in for code-agent parity so multiple worktrees can share a
    project memory root while still nesting data under ``users/<user_id>``.
    """
    if root is not None:
        return Path(root)

    remote_memory_dir = os.environ.get("VIOLA_REMOTE_MEMORY_DIR", "").strip()
    override = (
        os.environ.get("VIOLA_AUTO_MEMORY_DIRECTORY", "").strip()
        or str(_settings_attr("auto_memory_directory", "") or "").strip()
        or remote_memory_dir
    )
    if override:
        return _expand_memory_path(override)

    if _project_scoped_memory_enabled():
        return get_data_dir() / "projects" / _canonical_git_project_key(os.getcwd())

    return get_data_dir()


def _frontmatter_block(content: str) -> str | None:
    lines = content.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        return None
    block: list[str] = []
    for line in lines[1 : _FRONTMATTER_MAX_LINES + 1]:
        if line.strip() == _FRONTMATTER_DELIMITER:
            return "\n".join(block)
        block.append(line)
    # No closing delimiter - treat as no frontmatter to be conservative.
    return None


def _parse_frontmatter_yaml(block: str) -> dict[str, Any] | None:
    try:
        import yaml
    except ImportError:
        return None

    try:
        parsed = yaml.safe_load(block)
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_frontmatter_fallback(block: str) -> dict[str, Any]:
    """Fallback parser for flat frontmatter when PyYAML is unavailable."""
    payload: dict[str, Any] = {}
    lines = block.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        index += 1
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$", line)
        if not match:
            continue
        key = match.group(1).strip().lower()
        value = match.group(2).strip().strip("\"'")
        if value in {"|", ">"}:
            folded = value == ">"
            block_lines: list[str] = []
            while index < len(lines):
                next_line = lines[index]
                if next_line and not next_line.startswith((" ", "\t")):
                    break
                block_lines.append(next_line.strip())
                index += 1
            value = " ".join(block_lines) if folded else "\n".join(block_lines)
        payload[key] = value
    return payload


def _frontmatter_value_to_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        parts = [str(item).strip() for item in value if str(item).strip()]
        text = " ".join(parts)
    elif isinstance(value, dict):
        return None
    else:
        text = str(value).strip()
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _parse_frontmatter(content: str) -> dict[str, str]:
    """Parse YAML frontmatter from the head of a markdown file."""
    block = _frontmatter_block(content)
    if block is None:
        return {}
    parsed = _parse_frontmatter_yaml(block)
    if parsed is None:
        parsed = _parse_frontmatter_fallback(block)
    normalized = {str(key).strip().lower(): value for key, value in parsed.items()}
    payload: dict[str, str] = {}
    for key in _FRONTMATTER_KEYS:
        value = _frontmatter_value_to_text(normalized.get(key))
        if value:
            payload[key] = value
    return payload


def _semantic_memory_topic(category: str) -> tuple[str, str, str, str]:
    normalized = (category or "").strip().lower()
    return _SEMANTIC_MEMORY_TOPICS.get(normalized, _SEMANTIC_MEMORY_TOPICS["note"])


def _yaml_quoted(value: str) -> str:
    return json.dumps(str(value or "").strip(), ensure_ascii=True)


@dataclass(frozen=True)
class MemoryHeader:
    """Lightweight metadata for one topic file in the manifest."""

    filename: str
    file_path: str
    mtime_ms: float
    description: str | None
    type: str | None
    name: str | None


class _LegacyMemoryDecryptor:
    """Best-effort reader for the pre-markdown encrypted SQLite rows."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._fernets: list[Any] = []
        self._load()

    def _read_metadata(self, key: str) -> bytes | None:
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS memory_metadata (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
                row = conn.execute("SELECT value FROM memory_metadata WHERE key = ?", (key,)).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        value = row[0]
        if isinstance(value, bytes):
            return value
        return str(value).encode("utf-8")

    def _decrypt_only_candidate_secrets(self) -> list[str]:
        """Every secret a legacy row may have been encrypted under (decrypt-only).

        Includes the pre-SEC-028 session-token fallback and the pre-SEC-022
        cleartext metadata row so old installs stay readable; new encryption
        never uses these paths (see services/memory/store.py).
        """
        candidates: list[str] = []
        for value in (
            os.environ.get("VIOLA_MEMORY_ENCRYPTION_KEY", ""),
            os.environ.get("VIOLA_MEMORY_ENCRYPTION_KEY_PREVIOUS", ""),
            os.environ.get("VIOLA_SECURITY_TOKEN_SECRET", ""),
        ):
            if value and value not in candidates:
                candidates.append(value)
        dev_secret = self._read_metadata("encryption_dev_secret")
        if dev_secret:
            decoded = dev_secret.decode("utf-8", errors="replace")
            if decoded and decoded not in candidates:
                candidates.append(decoded)
        try:
            from services.memory.key_provider import get_file_fallback_secret, get_keystore_secret
        except ImportError:
            keystore_secret = None
            fallback_secret = None
        else:
            # get_keystore_secret / get_file_fallback_secret handle backend
            # failures themselves (return None). The file fallback (#341)
            # covers rows written while the OS keystore was unavailable.
            keystore_secret = get_keystore_secret()
            fallback_secret = get_file_fallback_secret()
        if keystore_secret and keystore_secret not in candidates:
            candidates.append(keystore_secret)
        if fallback_secret and fallback_secret not in candidates:
            candidates.append(fallback_secret)
        return candidates

    def _load(self) -> None:
        candidates = self._decrypt_only_candidate_secrets()
        if not candidates:
            return
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            logger.warning("cryptography unavailable; encrypted legacy memories cannot be migrated")
            return

        salt = self._read_metadata("encryption_salt")
        for secret in candidates:
            if salt:
                key = pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
                self._fernets.append(Fernet(base64.urlsafe_b64encode(key)))
            legacy_key = pbkdf2_hmac("sha256", secret.encode("utf-8"), _LEGACY_SALT, _PBKDF2_LEGACY_ITERATIONS)
            self._fernets.append(Fernet(base64.urlsafe_b64encode(legacy_key)))

    def decrypt(self, value: str) -> str:
        if not value.startswith("gAAAAA"):
            return value
        for fernet in self._fernets:
            try:
                return str(fernet.decrypt(value.encode("utf-8")).decode("utf-8"))
            except Exception:
                continue
        return _KEY_MISMATCH_MARKER


class MemoryDir:
    """Manage one account's transparent markdown memory directory."""

    def __init__(self, user_id: str, root: Path | None = None) -> None:
        self.account_id = (user_id or "").strip()
        self.user_id = safe_account_id(user_id)
        self.account_dir = account_root(user_id, root)
        self.root = self.account_dir / "memory"
        self.viola_path = self.account_dir / VIOLA_FILE
        self.index_path = self.root / MEMORY_INDEX
        self.topics_dir = self.root / TOPICS_DIR
        self.audit_path = self.root / AUDIT_LOG
        self.hygiene_path = self.root / HYGIENE_SETTINGS
        self.hygiene_policy = MemoryHygienePolicy.load(self.hygiene_path)
        self._data_root = Path(root) if root is not None else get_data_dir()
        self._ensure()

    def _ensure(self) -> None:
        self.account_dir.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.topics_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_layout_once()
        self.viola_path.touch(exist_ok=True)
        self.index_path.touch(exist_ok=True)
        self.audit_path.touch(exist_ok=True)
        self._chmod_private()
        self._migrate_legacy_once()

    def _chmod_private(self) -> None:
        if os.name == "nt":
            username = os.environ.get("USERNAME", "")
            userdomain = os.environ.get("USERDOMAIN", "")
            if username:
                principal = "%s\\%s" % (userdomain, username) if userdomain else username
                try:
                    from core.subprocess_utils import run_silent

                    for path in (self.account_dir, self.root, self.topics_dir):
                        run_silent(
                            [
                                "icacls",
                                str(path),
                                "/inheritance:r",
                                "/grant:r",
                                "%s:(OI)(CI)F" % principal,
                            ],
                            capture_output=True,
                            timeout=10,
                            check=False,
                        )  # proc-tree-ok: single icacls binary, no shell, no grandchildren
                except Exception:
                    logger.debug("Could not set private ACL on memory directory")
            return
        try:
            self.account_dir.chmod(0o700)
            self.root.chmod(0o700)
            self.topics_dir.chmod(0o700)
        except OSError:
            logger.debug("Could not set private mode on memory directory")

    def _merge_or_move_file(self, source: Path, target: Path, label: str, operations: list[str]) -> None:
        if not source.exists() or source.resolve() == target.resolve():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        raw_target = target.name == AUDIT_LOG
        if not target.exists():
            if raw_target:
                shutil.move(str(source), str(target))
            else:
                source_text = source.read_text(encoding="utf-8", errors="replace")
                self._write_memory_file(target, source_text)
                source.unlink()
            operations.append("%s moved %s -> %s" % (label, source.name, target.relative_to(self.account_dir)))
            return
        source_text = source.read_text(encoding="utf-8", errors="replace")
        target_text = (
            target.read_text(encoding="utf-8", errors="replace")
            if raw_target
            else self._read_memory_file(target, errors="replace")
        )
        if source_text.strip() and source_text.strip() not in target_text:
            separator = "" if not target_text or target_text.endswith("\n") else "\n"
            merged = target_text + separator + "\n<!-- migrated from %s -->\n%s" % (source.name, source_text)
            if raw_target:
                target.write_text(merged, encoding="utf-8")
            else:
                self._write_memory_file(target, merged)
        source.unlink()
        operations.append("%s merged %s -> %s" % (label, source.name, target.relative_to(self.account_dir)))

    def _move_topics_dir(self, source: Path, operations: list[str]) -> None:
        if not source.exists() or source.resolve() == self.topics_dir.resolve():
            return
        if not source.is_dir():
            return
        self.topics_dir.mkdir(parents=True, exist_ok=True)
        for item in sorted(source.iterdir()):
            target = self.topics_dir / item.name
            if item.is_dir():
                if not target.exists():
                    shutil.move(str(item), str(target))
                    operations.append("topic directory moved %s" % item.name)
                continue
            self._merge_or_move_file(item, target, "topic", operations)
        try:
            source.rmdir()
        except OSError:
            logger.debug("Old topics directory not empty after migration: %s", source)

    def _migrate_layout_once(self) -> None:
        marker = self.root / ".layout_v2_migrated"
        operations: list[str] = []

        for source in (self.root / USER_MEMORY, self.account_dir / USER_MEMORY):
            self._merge_or_move_file(source, self.viola_path, "viola file", operations)
        for source in (self.root / VIOLA_MEMORY, self.account_dir / VIOLA_MEMORY):
            self._merge_or_move_file(source, self.index_path, "memory index", operations)
        for source in (self.account_dir / TOPICS_DIR,):
            self._move_topics_dir(source, operations)
        for source in (self.account_dir / AUDIT_LOG,):
            self._merge_or_move_file(source, self.audit_path, "audit log", operations)

        marker.write_text(_now_iso(), encoding="utf-8")
        if operations:
            self._audit("migration", "memory/", "; ".join(operations), agent_initiated=False)

    def _normalize_memory_path(self, path: str | None, *, allow_viola: bool = False) -> Path:
        raw = (path or MEMORY_INDEX).strip().replace("\\", "/")
        if not raw:
            raw = MEMORY_INDEX
        if raw in {USER_MEMORY, VIOLA_FILE}:
            if allow_viola:
                return self.viola_path
            raise ValueError("VIOLA.md is user-owned and is not writable through agent memory")
        if raw == VIOLA_MEMORY:
            raw = MEMORY_INDEX
        if raw.startswith("memory/"):
            raw = raw[len("memory/") :]
        candidate = Path(raw)
        if self.hygiene_policy.topic_slug_normalization_enabled:
            candidate = self._normalize_topic_candidate(candidate)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("memory path must stay inside the account memory directory")
        if candidate.suffix.lower() != ".md":
            candidate = candidate.with_suffix(".md")
        resolved = (self.root / candidate).resolve()
        try:
            resolved.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ValueError("memory path must stay inside the account memory directory") from exc
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    def _normalize_topic_candidate(self, candidate: Path) -> Path:
        parts = candidate.parts
        if not parts or parts[0].lower() != TOPICS_DIR:
            return candidate
        if len(parts) < 2:
            raise ValueError("topic file name is required")
        topic = _topic_slug(Path(parts[-1]).stem)
        return Path(TOPICS_DIR) / ("%s.md" % topic)

    def _existing_topic_alias_path(self, raw_path: str, target: Path) -> Path:
        raw = (raw_path or "").strip().replace("\\", "/")
        if target.exists() or "/" in raw:
            return target
        topic_target = self._normalize_memory_path("%s/%s" % (TOPICS_DIR, raw), allow_viola=True)
        return topic_target if topic_target.exists() else target

    def _path_for_where(self, where: str) -> Path:
        target = (where or "memory").strip()
        if target in {"viola", "memory", "index", MEMORY_INDEX, VIOLA_MEMORY}:
            return self.index_path
        if target in {"user", "viola_file", VIOLA_FILE, USER_MEMORY}:
            return self.viola_path
        if target.startswith("topic:"):
            topic = _topic_slug(target.split(":", 1)[1])
            return self._normalize_memory_path("%s/%s" % (TOPICS_DIR, topic))
        return self._normalize_memory_path(target)

    @staticmethod
    def _looks_like_index_pointer(content: str) -> bool:
        """Heuristic for the Claude two-step write contract.

        An index pointer is a single-line bullet referencing a topic file —
        ``- [Title](topics/foo.md) — hook``. Anything else is raw content and
        should not be written into MEMORY.md by the agent. We accept the
        common shapes without parsing markdown:
            - ``[Title](topics/foo.md)`` link form
            - ``topics/foo.md`` bare path form (older clients)
        """
        stripped = (content or "").strip()
        if not stripped:
            return False
        lines = [line for line in stripped.splitlines() if line.strip()]
        if len(lines) != 1:
            return False
        only = lines[0].strip()
        # Strip leading bullet markers
        only = re.sub(r"^[-*]\s+|^\d+\.\s+", "", only)
        if "](" in only and ".md)" in only:
            return True
        # Bare topic-path form: starts with topics/ or contains /topics/
        if re.match(r"^(?:memory/)?topics/[A-Za-z0-9_.-]+\.md\b", only):
            return True
        return False

    def _relative(self, path: Path) -> str:
        resolved = path.resolve()
        if resolved == self.viola_path.resolve():
            return VIOLA_FILE
        try:
            return "memory/%s" % resolved.relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return path.name

    def _memory_encryption(self) -> Any:
        from services.memory.store import _get_memory_encryption

        return _get_memory_encryption(db_path=self._legacy_db_path())

    def _encode_memory_text(self, content: str) -> str:
        encrypted = self._memory_encryption().encrypt(content)
        return "%s%s\n" % (_ENCRYPTED_MARKDOWN_PREFIX, encrypted)

    def _read_memory_file(self, path: Path, *, errors: str = "strict") -> str:
        raw = path.read_text(encoding="utf-8", errors=errors)
        if raw.startswith(_ENCRYPTED_MARKDOWN_PREFIX):
            token = raw[len(_ENCRYPTED_MARKDOWN_PREFIX) :].strip()
            return self._memory_encryption().decrypt(token)
        return raw

    def _scratch_sibling(self, path: Path, suffix: str) -> Path:
        """Return a short, unique scratch path next to ``path``.

        Same directory (so ``os.replace`` onto ``path`` stays atomic — a rename
        across filesystems is not), and deliberately INDEPENDENT of ``path.name``.

        Windows enforces a 259-character path limit unless the machine opted into
        long paths (``LongPathsEnabled``, off by default), so every character a
        scratch name spends is a character taken from the user's own directory
        depth and topic-file name. The earlier ``.<target-name>.<32-hex>.tmp``
        shape cost ``len(name) + 38`` characters, which is MORE than the file it
        was standing in for: it could make ``open()`` raise
        ``FileNotFoundError`` (Windows maps ERROR_PATH_NOT_FOUND to errno 2) on a
        path the file itself fits in, so making the write atomic made a
        previously-writable memory file unwritable. This shape is a fixed 27
        characters, which is shorter than every real memory file name it stands
        in for (``MEMORY.md`` plus a directory is already longer), so an atomic
        write can now reach every path a direct write can.
        """
        return path.with_name(".viola-%s.%s" % (uuid.uuid4().hex[:16], suffix))

    def _encode_and_write(self, path: Path, content: str) -> None:
        """Encode + durably write ``content`` to EXACTLY ``path``.

        No temp indirection: the caller decides whether ``path`` is a final
        destination or a scratch file it will rename itself. Callers that need
        the write to be atomic use ``_write_memory_file``; callers that are
        already writing INTO their own scratch file (``_atomic_write_with_archive``)
        use this directly, so a scratch file never gets a scratch file of its own.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(self._encode_memory_text(content))
            handle.flush()
            os.fsync(handle.fileno())

    def _write_memory_file(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write. A memory file is a single Fernet token when encryption
        # is on (see _encode_memory_text): a torn write — crash, power loss,
        # disk-full, or a concurrent overwrite — leaves a truncated token, and
        # _read_memory_file then returns the key-mismatch marker for the ENTIRE
        # file, silently losing every memory in it. Write to a scratch file in
        # the same directory, fsync, then os.replace (atomic on POSIX and
        # Windows), so a reader/crash never observes a half-written file.
        tmp = self._scratch_sibling(path, "tmp")
        try:
            self._encode_and_write(tmp, content)
            os.replace(tmp, path)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                logger.debug("Could not remove temporary memory file: %s", tmp)

    def _cap_setting_for_path(self, target: Path) -> tuple[str, int] | None:
        resolved = target.resolve()
        if resolved == self.viola_path.resolve():
            return (MEMORY_VIOLA_MD_MAX_BYTES, _DEFAULT_VIOLA_MD_MAX_BYTES)
        if resolved == self.index_path.resolve():
            return (MEMORY_MAIN_MD_MAX_BYTES, _DEFAULT_MAIN_MD_MAX_BYTES)
        try:
            resolved.relative_to(self.topics_dir.resolve())
        except ValueError:
            return None
        if target.suffix.lower() != ".md":
            return None
        return (MEMORY_TOPIC_MD_MAX_BYTES, _DEFAULT_TOPIC_MD_MAX_BYTES)

    def _read_cap_setting(self, key: str, default: int) -> int:
        value: object = default
        try:
            from ui.settings_manager import get_settings_manager

            manager = get_settings_manager()
            try:
                value = manager.get(key, default, user_id=self.account_id)
            except TypeError:
                value = manager.get(key, default)
        except Exception as exc:
            logger.debug("Memory cap setting %s unavailable; using default: %s", key, exc)
            value = default
        if isinstance(value, bool):
            return default
        try:
            cap = int(value)
        except (TypeError, ValueError):
            return default
        return max(0, cap)

    def _cap_bytes_for_path(self, target: Path) -> int:
        setting = self._cap_setting_for_path(target)
        if setting is None:
            return 0
        key, default = setting
        return self._read_cap_setting(key, default)

    def _archive_path_for(self, target: Path) -> Path:
        now = datetime.now(UTC)
        return self.root / ARCHIVE_DIR / now.strftime("%Y-%m") / target.name

    def _prune_recent_memory_lines(self, now_monotonic: float) -> None:
        cutoff = now_monotonic - _RECENT_LINE_PROTECTION_SECONDS
        stale_keys: list[str] = []
        for key, rows in _RECENT_MEMORY_LINES.items():
            fresh = [(when, line) for when, line in rows if when >= cutoff]
            if fresh:
                _RECENT_MEMORY_LINES[key] = fresh
            else:
                stale_keys.append(key)
        for key in stale_keys:
            _RECENT_MEMORY_LINES.pop(key, None)

    def _record_recent_memory_lines(self, target: Path, content: str) -> None:
        lines = [_line_key(line) for line in content.splitlines() if line.strip()]
        if not lines:
            return
        now_monotonic = time.monotonic()
        self._prune_recent_memory_lines(now_monotonic)
        key = str(target.resolve())
        rows = _RECENT_MEMORY_LINES.setdefault(key, [])
        rows.extend((now_monotonic, line) for line in lines)

    def _recent_line_indices(self, target: Path, lines: list[str], now_monotonic: float) -> set[int]:
        self._prune_recent_memory_lines(now_monotonic)
        recent = _RECENT_MEMORY_LINES.get(str(target.resolve()), [])
        if not recent:
            return set()
        protected: set[int] = set()
        used: set[int] = set()
        for _when, recent_line in sorted(recent, key=lambda item: item[0], reverse=True):
            for index in range(len(lines) - 1, -1, -1):
                if index in used:
                    continue
                if _line_key(lines[index]) == recent_line:
                    protected.add(index)
                    used.add(index)
                    break
        return protected

    def _trim_oldest_lines_for_cap(
        self,
        target: Path,
        source_text: str,
        compose_live_text: Any,
        cap_bytes: int,
    ) -> tuple[str, str, int, int]:
        projected_bytes = _text_bytes(compose_live_text(source_text))
        if cap_bytes <= 0 or projected_bytes <= cap_bytes:
            return source_text, "", projected_bytes, projected_bytes
        lines = source_text.splitlines(keepends=True)
        if not lines:
            return source_text, "", projected_bytes, projected_bytes

        target_bytes = max(1, int(cap_bytes * _ARCHIVE_TARGET_RATIO))
        protected = self._recent_line_indices(target, lines, time.monotonic())
        archived_indices: set[int] = set()
        live_bytes = projected_bytes
        for index in range(len(lines)):
            if index in protected:
                continue
            archived_indices.add(index)
            candidate = "".join(line for line_index, line in enumerate(lines) if line_index not in archived_indices)
            live_bytes = _text_bytes(compose_live_text(candidate))
            if live_bytes <= target_bytes:
                break

        if not archived_indices:
            return source_text, "", projected_bytes, projected_bytes

        live_text = "".join(line for index, line in enumerate(lines) if index not in archived_indices)
        archived_text = "".join(line for index, line in enumerate(lines) if index in archived_indices)
        return live_text, archived_text, projected_bytes, _text_bytes(compose_live_text(live_text))

    def _archive_plan_for_write(
        self,
        target: Path,
        source_text: str,
        compose_live_text: Any,
    ) -> tuple[str, _ArchivePlan | None]:
        cap_bytes = self._cap_bytes_for_path(target)
        if cap_bytes <= 0:
            return source_text, None
        live_source, archived_text, projected_bytes, live_bytes = self._trim_oldest_lines_for_cap(
            target,
            source_text,
            compose_live_text,
            cap_bytes,
        )
        if not archived_text:
            return live_source, None
        return live_source, _ArchivePlan(
            archive_path=self._archive_path_for(target),
            archived_text=archived_text,
            cap_bytes=cap_bytes,
            target_bytes=max(1, int(cap_bytes * _ARCHIVE_TARGET_RATIO)),
            projected_bytes=projected_bytes,
            live_bytes=live_bytes,
        )

    def _atomic_write_with_archive(self, target: Path, live_text: str, plan: _ArchivePlan) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        plan.archive_path.parent.mkdir(parents=True, exist_ok=True)
        live_tmp = self._scratch_sibling(target, "tmp")
        live_backup = self._scratch_sibling(target, "bak")
        archive_tmp = self._scratch_sibling(plan.archive_path, "tmp")
        archive_backup = self._scratch_sibling(plan.archive_path, "bak")

        existing_archive = (
            self._read_memory_file(plan.archive_path, errors="replace") if plan.archive_path.exists() else ""
        )
        separator = "" if not existing_archive or existing_archive.endswith("\n") else "\n"
        archive_payload = "%s%s<!-- archived from %s at %s; moved_bytes=%d -->\n%s" % (
            existing_archive,
            separator,
            self._relative(target),
            _now_iso(),
            plan.bytes_moved,
            plan.archived_text,
        )
        if archive_payload and not archive_payload.endswith("\n"):
            archive_payload += "\n"

        # live_tmp / archive_tmp ARE the scratch files this method renames into
        # place below, so they are filled directly. Routing them through
        # _write_memory_file instead gave each scratch file a scratch file of its
        # own (".<name>.<nonce>.tmp.<nonce>.tmp"), doubling the path-length cost
        # of every archiving write and costing two redundant fsync+rename cycles
        # per file. On Windows that pushed the write past the 259-character path
        # limit and raised FileNotFoundError, so a memory write that hit its size
        # cap failed outright and the content the user was saving was dropped.
        try:
            self._encode_and_write(live_tmp, live_text)
            self._encode_and_write(archive_tmp, archive_payload)
        except Exception:
            # Nothing has been renamed yet, so the live and archive files are
            # untouched and there is nothing to roll back — but a half-finished
            # pair of scratch files would otherwise sit in the user's memory
            # directory forever (encrypted, unreadable, never cleaned up).
            for scratch in (live_tmp, archive_tmp):
                try:
                    if scratch.exists():
                        scratch.unlink()
                except OSError:
                    logger.debug("Could not remove temporary memory archive file: %s", scratch)
            raise

        live_backed_up = False
        archive_backed_up = False
        try:
            if target.exists():
                target.replace(live_backup)
                live_backed_up = True
            if plan.archive_path.exists():
                plan.archive_path.replace(archive_backup)
                archive_backed_up = True
            archive_tmp.replace(plan.archive_path)
            live_tmp.replace(target)
        except Exception as exc:
            try:
                if live_backed_up and live_backup.exists():
                    live_backup.replace(target)
                elif not live_backed_up and target.exists():
                    target.unlink()
                if archive_backed_up and archive_backup.exists():
                    archive_backup.replace(plan.archive_path)
                elif not archive_backed_up and plan.archive_path.exists():
                    plan.archive_path.unlink()
            except Exception:
                logger.exception("Memory archive rollback failed for %s", target)
                self._audit(
                    "hygiene:archive_failed",
                    self._relative(target),
                    "archive rollback failed after error: %s" % exc,
                    agent_initiated=False,
                    source="hygiene",
                )
            raise
        finally:
            for temp_path in (live_tmp, archive_tmp, live_backup, archive_backup):
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except OSError:
                    logger.debug("Could not remove temporary memory archive file: %s", temp_path)

    def _audit_archive(self, target: Path, plan: _ArchivePlan, *, agent_initiated: bool) -> str:
        audit_id = self._audit(
            "hygiene:archive_cap",
            self._relative(target),
            ("cap_bytes=%d target_bytes=%d projected_bytes=%d live_bytes=%d " "archived_bytes=%d archive=%s")
            % (
                plan.cap_bytes,
                plan.target_bytes,
                plan.projected_bytes,
                plan.live_bytes,
                plan.bytes_moved,
                self._relative(plan.archive_path),
            ),
            agent_initiated=agent_initiated,
            source="hygiene",
        )
        # Surface to the agent via self_knowledge — see record_archive_notice
        # for rationale. Logs at info level for ops visibility regardless of
        # whether the agent ends up mentioning it.
        record_archive_notice(self.account_id or self.user_id, self._relative(target), plan.bytes_moved)
        logger.info(
            "Archived older memory content for %s to %s (%d bytes)",
            self._relative(target),
            self._relative(plan.archive_path),
            plan.bytes_moved,
        )
        return audit_id

    def _write_text_with_archive(
        self,
        target: Path,
        content: str,
        plan: _ArchivePlan | None,
        *,
        agent_initiated: bool,
    ) -> str | None:
        if plan is None:
            self._write_memory_file(target, content)
            return None
        self._atomic_write_with_archive(target, content, plan)
        return self._audit_archive(target, plan, agent_initiated=agent_initiated)

    def _audit_payload(
        self,
        action: str,
        path: str,
        diff: str,
        *,
        agent_initiated: bool,
        source: str | None = None,
    ) -> dict[str, Any]:
        audit_id = "%s-%s" % (datetime.now(UTC).strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:8])
        return {
            "id": audit_id,
            "when": _now_iso(),
            "action": action,
            "path": path,
            "diff": diff[:4000],
            "source": source or ("agent" if agent_initiated else "user"),
            "agent_initiated": bool(agent_initiated),
        }

    def _append_audit_entry(self, entry: dict[str, Any]) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _rotate_audit_if_needed(self) -> dict[str, Any] | None:
        limit = self.hygiene_policy.audit_rotate_bytes
        if limit <= 0 or not self.audit_path.exists():
            return None
        size = self.audit_path.stat().st_size
        if size <= limit:
            return None
        keep = max(1, self.hygiene_policy.audit_rotate_keep)
        for index in range(keep, 0, -1):
            source = self.audit_path if index == 1 else self.audit_path.with_name("%s.%d" % (AUDIT_LOG, index - 1))
            target = self.audit_path.with_name("%s.%d" % (AUDIT_LOG, index))
            if not source.exists():
                continue
            if index == keep and target.exists():
                target.unlink()
            source.replace(target)
        return {"bytes": size, "archive": "%s.1" % AUDIT_LOG}

    def _audit(
        self,
        action: str,
        path: str,
        diff: str,
        *,
        agent_initiated: bool,
        source: str | None = None,
    ) -> str:
        rotation = self._rotate_audit_if_needed()
        if rotation is not None:
            rotated = self._audit_payload(
                "hygiene:audit_rotate",
                "memory/%s" % AUDIT_LOG,
                "rotated %d bytes to %s" % (rotation["bytes"], rotation["archive"]),
                agent_initiated=False,
                source="hygiene",
            )
            self._append_audit_entry(rotated)
        entry = self._audit_payload(action, path, diff, agent_initiated=agent_initiated, source=source)
        self._append_audit_entry(entry)
        return str(entry["id"])

    def _audit_hygiene_rejection(self, target: Path, message: str, *, agent_initiated: bool) -> None:
        self._audit(
            "hygiene:reject",
            self._relative(target),
            message,
            agent_initiated=agent_initiated,
            source="hygiene",
        )

    def _validate_entry_or_audit(self, target: Path, content: str, *, agent_initiated: bool) -> None:
        try:
            validate_entry_content(content, self.hygiene_policy)
        except ValueError as exc:
            self._audit_hygiene_rejection(target, str(exc), agent_initiated=agent_initiated)
            raise

    def _validate_file_or_audit(self, target: Path, content: str, *, agent_initiated: bool) -> None:
        policy = self.hygiene_policy
        if self._cap_setting_for_path(target) is not None and policy.memory_file_max_bytes > 0:
            policy = replace(policy, memory_file_max_bytes=0)
        try:
            validate_file_content(self._relative(target), content, policy)
        except ValueError as exc:
            self._audit_hygiene_rejection(target, str(exc), agent_initiated=agent_initiated)
            raise

    def _find_duplicate_entry(self, content: str) -> dict[str, Any] | None:
        if not self.hygiene_policy.dedup_enabled:
            return None
        incoming = canonical_memory_text(content)
        if not incoming:
            return None
        for item in self._markdown_files(include_viola=False):
            text = self._read_memory_file(item)
            for entry in _parse_markdown_entries(text):
                if entry.get("kind") == "section":
                    continue
                existing = canonical_memory_text(str(entry.get("text") or ""))
                if not existing:
                    continue
                overlap = 1.0 if incoming == existing else word_overlap(incoming, existing)
                if overlap >= self.hygiene_policy.dedup_word_overlap:
                    return {
                        "path": self._relative(item),
                        "line_number": int(entry["line_number"]),
                        "id": entry["id"],
                        "text": entry["text"],
                        "overlap": overlap,
                    }
        return None

    def _prune_empty_topic_file(self, target: Path, *, agent_initiated: bool) -> bool:
        if not self.hygiene_policy.empty_topic_cleanup_enabled or not target.exists():
            return False
        try:
            target.resolve().relative_to(self.topics_dir.resolve())
        except ValueError:
            return False
        if self._read_memory_file(target, errors="replace").strip():
            return False
        target.unlink()
        self._audit(
            "hygiene:prune_empty_topic",
            self._relative(target),
            "deleted empty topic file",
            agent_initiated=agent_initiated,
            source="hygiene",
        )
        return True

    def read_viola_file(self) -> str:
        if not self.viola_path.exists():
            return ""
        return self._read_memory_file(self.viola_path)

    def write_viola_file(self, content: str, *, agent_initiated: bool = False) -> dict[str, Any]:
        if agent_initiated:
            raise ValueError("VIOLA.md is user-owned; suggest edits to the user instead of writing it")
        old = self.read_viola_file()
        payload = content if content.endswith("\n") or not content else content + "\n"
        payload, archive_plan = self._archive_plan_for_write(self.viola_path, payload, lambda text: text)
        self._validate_file_or_audit(self.viola_path, payload, agent_initiated=False)
        archive_audit_id = self._write_text_with_archive(
            self.viola_path,
            payload,
            archive_plan,
            agent_initiated=False,
        )
        self._record_recent_memory_lines(self.viola_path, payload)
        audit_id = self._audit(
            "write:viola",
            VIOLA_FILE,
            "bytes %d -> %d" % (len(old.encode("utf-8")), len(payload.encode("utf-8"))),
            agent_initiated=False,
        )
        result: dict[str, Any] = {"ok": True, "audit_id": audit_id, "path": VIOLA_FILE}
        if archive_plan is not None:
            result.update(
                {
                    "archived": True,
                    "archive_audit_id": archive_audit_id,
                    "archive_path": self._relative(archive_plan.archive_path),
                    "archived_bytes": archive_plan.bytes_moved,
                }
            )
        return result

    def read(self, path: str = MEMORY_INDEX, query: str | None = None) -> str:
        if query and query.strip():
            return self._grep(query)
        target = self._existing_topic_alias_path(path, self._normalize_memory_path(path, allow_viola=True))
        if not target.exists():
            return ""
        return self._read_memory_file(target)

    def search_body(self, query: str, path: str | None = None) -> str:
        """Search memory file bodies explicitly.

        Relevance recall uses frontmatter manifests; this method remains for
        exact manual search and delete workflows that need line numbers.
        """
        return self._grep(query, path=path)

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        lines = content.splitlines()
        if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
            return content
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == _FRONTMATTER_DELIMITER:
                return "\n".join(lines[index + 1 :]).strip("\n")
        return content

    @staticmethod
    def _semantic_frontmatter(name: str, description: str, memory_type: str) -> str:
        return (
            "---\n"
            "name: %s\n"
            "description: %s\n"
            "type: %s\n"
            "---\n" % (_yaml_quoted(name), _yaml_quoted(description), (memory_type or "user").strip())
        )

    def _ensure_semantic_frontmatter(
        self,
        target: Path,
        *,
        name: str,
        description: str,
        memory_type: str,
        agent_initiated: bool,
    ) -> None:
        current = self._read_memory_file(target, errors="replace") if target.exists() else ""
        body = self._strip_frontmatter(current)
        frontmatter = self._semantic_frontmatter(name, description, memory_type)
        new = frontmatter + (body.rstrip("\n") + "\n" if body.strip() else "")
        if new == current:
            return
        self._validate_file_or_audit(target, new, agent_initiated=agent_initiated)
        self._write_memory_file(target, new)
        self._audit(
            "write:frontmatter",
            self._relative(target),
            "semantic metadata name=%s type=%s" % (name, memory_type),
            agent_initiated=agent_initiated,
        )

    def store_semantic_memory(
        self,
        content: str,
        *,
        category: str = "fact",
        agent_initiated: bool = True,
    ) -> dict[str, Any]:
        """Store a user memory as a Claude-style topic file plus index pointer."""
        clean = (content or "").strip()
        if not clean:
            raise ValueError("content is required")
        topic, title, memory_type, description = _semantic_memory_topic(category)
        target = self._path_for_where("topic:%s" % topic)
        self._ensure_semantic_frontmatter(
            target,
            name=title.lower(),
            description=description,
            memory_type=memory_type,
            agent_initiated=agent_initiated,
        )
        payload = clean if re.match(r"^[-*]\s+|\d+\.\s+", clean) else "- %s" % clean
        result = self.write(payload, where="topic:%s" % topic, position="append", agent_initiated=agent_initiated)
        pointer = self.add_index_pointer(
            title, "topics/%s.md" % topic, hook=description, agent_initiated=agent_initiated
        )
        result.update(
            {
                "index_pointer": pointer,
                "frontmatter": {
                    "name": title.lower(),
                    "description": description,
                    "type": memory_type,
                },
            }
        )
        return result

    def _search_scope_path(self, path: str | None) -> Path | None:
        raw = (path or "").strip().replace("\\", "/")
        if not raw or raw in {"memory", "memory/", MEMORY_INDEX, VIOLA_MEMORY, "memory/%s" % MEMORY_INDEX}:
            return None
        target = self._normalize_memory_path(raw, allow_viola=True)
        return self._existing_topic_alias_path(raw, target)

    def _grep(self, query: str, path: str | None = None) -> str:
        needle = query.strip().lower()
        tokens = [token for token in re.findall(r"[A-Za-z0-9_'-]+", needle) if len(token) > 1]
        matches: list[str] = []
        scope_path = self._search_scope_path(path)
        items = (
            [scope_path] if scope_path is not None and scope_path.exists() else self._markdown_files(include_viola=True)
        )
        if scope_path is not None and not scope_path.exists():
            items = []
        for item in items:
            text = self._read_memory_file(item)
            for line_number, line in enumerate(text.splitlines(), start=1):
                lower = line.lower()
                if needle in lower or (tokens and any(token in lower for token in tokens)):
                    matches.append("%s:%d: %s" % (self._relative(item), line_number, line))
        return "\n".join(matches)

    def write(
        self,
        content: str,
        where: str = "memory",
        position: str = "append",
        *,
        agent_initiated: bool = True,
    ) -> dict[str, Any]:
        if not content and position != "replace":
            raise ValueError("content is required")
        target = self._path_for_where(where)
        if target.resolve() == self.viola_path.resolve() and agent_initiated:
            raise ValueError("VIOLA.md is user-owned; suggest edits to the user instead of writing it")
        # Claude-parity: MEMORY.md is the always-loaded index, not a memory
        # file. Agent writes that land in MEMORY.md must be one-line index
        # pointers (e.g. ``- [Title](topics/foo.md) — hook``). Raw memory
        # content goes to a topic file via ``where="topic:<name>"``. The
        # two-step contract is enforced here as defense-in-depth — the prompt
        # describes it, this guard makes it impossible to bypass.
        if (
            target.resolve() == self.index_path.resolve()
            and agent_initiated
            and position != "replace"
            and not self._looks_like_index_pointer(content)
        ):
            raise ValueError(
                "MEMORY.md is an index — write memory content to a topic file "
                "(e.g. where='topic:preferences'). Only one-line index pointers "
                "like '- [Title](topics/preferences.md) — hook' belong in MEMORY.md."
            )
        old = self._read_memory_file(target) if target.exists() else ""
        payload = (content.rstrip("\n") + "\n") if content else ""
        if position in {"append", "prepend"}:
            self._validate_entry_or_audit(target, content, agent_initiated=agent_initiated)
            duplicate = self._find_duplicate_entry(content)
            if duplicate is not None:
                audit_id = self._audit(
                    "hygiene:dedup",
                    self._relative(target),
                    "skipped duplicate of %s:%d overlap=%.2f"
                    % (duplicate["path"], duplicate["line_number"], duplicate["overlap"]),
                    agent_initiated=agent_initiated,
                    source="hygiene",
                )
                return {
                    "ok": True,
                    "deduped": True,
                    "line_number": duplicate["line_number"],
                    "audit_id": audit_id,
                    "path": duplicate["path"],
                    "requested_path": self._relative(target),
                    "duplicate_of": duplicate,
                }
        if position == "append":

            def compose_append(text: str) -> str:
                separator = "" if not text or text.endswith("\n") else "\n"
                return text + separator + payload

            old, archive_plan = self._archive_plan_for_write(target, old, compose_append)
            line_number = len(old.splitlines()) + 1
            new = compose_append(old)
        elif position == "prepend":
            line_number = 1

            def compose_prepend(text: str) -> str:
                separator = "" if not text else "\n"
                return payload + separator + text

            old, archive_plan = self._archive_plan_for_write(target, old, compose_prepend)
            new = compose_prepend(old)
        elif position == "replace":
            line_number = 1
            new, archive_plan = self._archive_plan_for_write(target, payload, lambda text: text)
        else:
            raise ValueError("position must be append, prepend, or replace")
        self._validate_file_or_audit(target, new, agent_initiated=agent_initiated)
        archive_audit_id = self._write_text_with_archive(
            target,
            new,
            archive_plan,
            agent_initiated=agent_initiated,
        )
        if position in {"append", "prepend"}:
            self._record_recent_memory_lines(target, payload)
        audit_id = self._audit(
            "write:%s" % position,
            self._relative(target),
            "+ " + content[:1000],
            agent_initiated=agent_initiated,
        )
        result = {"ok": True, "line_number": line_number, "audit_id": audit_id, "path": self._relative(target)}
        if archive_plan is not None:
            result.update(
                {
                    "archived": True,
                    "archive_audit_id": archive_audit_id,
                    "archive_path": self._relative(archive_plan.archive_path),
                    "archived_bytes": archive_plan.bytes_moved,
                }
            )
        return result

    def edit(self, path: str, find: str, replace: str, *, agent_initiated: bool = True) -> dict[str, Any]:
        if not find:
            raise ValueError("find text is required")
        target = self._normalize_memory_path(path, allow_viola=not agent_initiated)
        if target.resolve() == self.viola_path.resolve() and agent_initiated:
            raise ValueError("VIOLA.md is user-owned; suggest edits to the user instead of writing it")
        old = self._read_memory_file(target) if target.exists() else ""
        if find not in old:
            return {"ok": False, "error": "find text not found", "path": self._relative(target)}
        new = old.replace(find, replace, 1)
        if replace:
            self._validate_entry_or_audit(target, replace, agent_initiated=agent_initiated)
        self._validate_file_or_audit(target, new, agent_initiated=agent_initiated)
        self._write_memory_file(target, new)
        audit_id = self._audit(
            "edit",
            self._relative(target),
            "- %s\n+ %s" % (find[:1000], replace[:1000]),
            agent_initiated=agent_initiated,
        )
        return {"ok": True, "audit_id": audit_id, "path": self._relative(target)}

    def delete_line(self, path: str, line_number: int, *, agent_initiated: bool = True) -> dict[str, Any]:
        target = self._normalize_memory_path(path, allow_viola=not agent_initiated)
        if target.resolve() == self.viola_path.resolve() and agent_initiated:
            raise ValueError("VIOLA.md is user-owned; suggest edits to the user instead of writing it")
        lines = self._read_memory_file(target).splitlines()
        if line_number < 1 or line_number > len(lines):
            return {"ok": False, "error": "line_number out of range", "path": self._relative(target)}
        removed = lines.pop(line_number - 1)
        self._write_memory_file(target, ("\n".join(lines) + "\n") if lines else "")
        audit_id = self._audit(
            "delete_line",
            self._relative(target),
            "- %s" % removed[:1000],
            agent_initiated=agent_initiated,
        )
        pruned = self._prune_empty_topic_file(target, agent_initiated=agent_initiated)
        return {
            "ok": True,
            "audit_id": audit_id,
            "path": self._relative(target),
            "removed": removed,
            "pruned_empty_topic": pruned,
        }

    def delete_section(self, path: str, section_title: str, *, agent_initiated: bool = True) -> dict[str, Any]:
        title = section_title.strip().lower()
        if not title:
            raise ValueError("section_title is required")
        target = self._normalize_memory_path(path, allow_viola=not agent_initiated)
        if target.resolve() == self.viola_path.resolve() and agent_initiated:
            raise ValueError("VIOLA.md is user-owned; suggest edits to the user instead of writing it")
        lines = self._read_memory_file(target).splitlines()
        start = -1
        level = 0
        for index, line in enumerate(lines):
            match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if match and match.group(2).strip().lower() == title:
                start = index
                level = len(match.group(1))
                break
        if start < 0:
            return {"ok": False, "error": "section not found", "path": self._relative(target)}
        end = len(lines)
        for index in range(start + 1, len(lines)):
            match = re.match(r"^(#{1,6})\s+", lines[index])
            if match and len(match.group(1)) <= level:
                end = index
                break
        removed = lines[start:end]
        remaining = lines[:start] + lines[end:]
        self._write_memory_file(target, ("\n".join(remaining) + "\n") if remaining else "")
        audit_id = self._audit(
            "delete_section",
            self._relative(target),
            "- " + "\n- ".join(removed[:20]),
            agent_initiated=agent_initiated,
        )
        pruned = self._prune_empty_topic_file(target, agent_initiated=agent_initiated)
        return {
            "ok": True,
            "audit_id": audit_id,
            "path": self._relative(target),
            "removed_lines": len(removed),
            "pruned_empty_topic": pruned,
        }

    def delete_entry(self, identifier: str, *, agent_initiated: bool = False) -> dict[str, Any]:
        return self._delete_entry_from_path(MEMORY_INDEX, identifier, agent_initiated=agent_initiated)

    def delete_topic_entry(self, name: str, identifier: str, *, agent_initiated: bool = False) -> dict[str, Any]:
        topic = _topic_slug(Path(name).stem)
        return self._delete_entry_from_path(
            "%s/%s.md" % (TOPICS_DIR, topic), identifier, agent_initiated=agent_initiated
        )

    def _delete_entry_from_path(self, path: str, identifier: str, *, agent_initiated: bool) -> dict[str, Any]:
        ident = (identifier or "").strip()
        if ident.startswith("line:"):
            ident = ident.split(":", 1)[1]
        if ident.isdigit():
            return self.delete_line(path, int(ident), agent_initiated=agent_initiated)
        if ident.startswith("section:"):
            ident = ident.split(":", 1)[1]
        return self.delete_section(path, ident, agent_initiated=agent_initiated)

    def delete_topic_file(self, name: str, *, agent_initiated: bool = False) -> dict[str, Any]:
        topic = _topic_slug(Path(name).stem)
        target = self._normalize_memory_path("%s/%s.md" % (TOPICS_DIR, topic))
        if not target.exists():
            return {"ok": False, "error": "topic file not found", "path": self._relative(target)}
        content = self._read_memory_file(target, errors="replace")
        target.unlink()
        audit_id = self._audit(
            "delete_topic_file",
            self._relative(target),
            "- deleted %d bytes" % len(content.encode("utf-8")),
            agent_initiated=agent_initiated,
        )
        return {"ok": True, "audit_id": audit_id, "path": self._relative(target)}

    def add_index_pointer(
        self,
        title: str,
        topic_path: str,
        hook: str = "",
        *,
        agent_initiated: bool = True,
    ) -> dict[str, Any]:
        """Append a one-line index pointer to MEMORY.md.

        Claude-parity: MEMORY.md is the index of topic files, not a memory
        store. Saving a memory is a two-step process — write the content to
        ``topic:<name>`` (or directly to ``topics/<name>.md``), then call this
        method to add the pointer to the index.

        The resulting line is ``- [title](topics/foo.md) — hook`` (the em-dash
        and hook are omitted when ``hook`` is empty). The pointer write is
        treated as agent-initiated by default because the contract is the
        agent's responsibility; pass ``agent_initiated=False`` to record a
        user-initiated migration / hand-edit.
        """
        clean_title = (title or "").strip()
        clean_topic = (topic_path or "").strip().replace("\\", "/")
        if not clean_title:
            raise ValueError("index pointer title is required")
        if not clean_topic:
            raise ValueError("index pointer topic_path is required")
        if clean_topic.startswith("memory/"):
            clean_topic = clean_topic[len("memory/") :]
        if not clean_topic.endswith(".md"):
            clean_topic = clean_topic + ".md"
        pointer = "- [%s](%s)" % (clean_title, clean_topic)
        hook_text = (hook or "").strip()
        if hook_text:
            pointer = "%s — %s" % (pointer, hook_text)
        return self.write(pointer, where="memory", position="append", agent_initiated=agent_initiated)

    def organize(
        self,
        path: str = MEMORY_INDEX,
        content: str | None = None,
        *,
        agent_initiated: bool = True,
    ) -> dict[str, Any]:
        target = self._normalize_memory_path(path, allow_viola=not agent_initiated)
        if target.resolve() == self.viola_path.resolve() and agent_initiated:
            raise ValueError("VIOLA.md is user-owned; suggest edits to the user instead of writing it")
        old = self._read_memory_file(target) if target.exists() else ""
        new = content if content is not None else "\n".join(line.rstrip() for line in old.splitlines()).strip() + "\n"
        self._validate_file_or_audit(target, new, agent_initiated=agent_initiated)
        self._write_memory_file(target, new)
        audit_id = self._audit("organize", self._relative(target), "full rewrite", agent_initiated=agent_initiated)
        pruned = self._prune_empty_topic_file(target, agent_initiated=agent_initiated)
        return {"ok": True, "audit_id": audit_id, "path": self._relative(target), "pruned_empty_topic": pruned}

    def list(self) -> list[dict[str, Any]]:
        return [file.__dict__ for file in self._list_files(include_viola=True)]

    def list_entries(self) -> dict[str, Any]:
        index_content = self._read_memory_file(self.index_path) if self.index_path.exists() else ""
        topics: list[dict[str, Any]] = []
        for topic_path in sorted(self.topics_dir.glob("*.md")):
            content = self._read_memory_file(topic_path)
            stat = topic_path.stat()
            topics.append(
                {
                    "name": topic_path.stem,
                    "filename": topic_path.name,
                    "path": self._relative(topic_path),
                    "content": content,
                    "entries": _parse_markdown_entries(content),
                    "bytes": stat.st_size,
                    "lines": len(content.splitlines()),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                }
            )
        index_stat = self.index_path.stat() if self.index_path.exists() else None
        return {
            "index": {
                "path": self._relative(self.index_path),
                "content": index_content,
                "entries": _parse_markdown_entries(index_content),
                "bytes": index_stat.st_size if index_stat else 0,
                "lines": len(index_content.splitlines()),
                "modified_at": datetime.fromtimestamp(index_stat.st_mtime, UTC).isoformat() if index_stat else "",
            },
            "topics": topics,
        }

    def _entry_rows(self, *, include_viola: bool) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in self._markdown_files(include_viola=include_viola):
            content = self._read_memory_file(path)
            for entry in _parse_markdown_entries(content):
                if entry.get("kind") == "section":
                    continue
                rows.append(
                    {
                        "path": self._relative(path),
                        "line_number": int(entry["line_number"]),
                        "id": entry["id"],
                        "kind": entry["kind"],
                        "text": entry["text"],
                        "canonical": canonical_memory_text(str(entry["text"])),
                    }
                )
        return rows

    def _duplicate_report(self) -> list[dict[str, Any]]:
        rows = [row for row in self._entry_rows(include_viola=False) if row["canonical"]]
        duplicates: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            for prior in rows[:index]:
                overlap = (
                    1.0
                    if row["canonical"] == prior["canonical"]
                    else word_overlap(row["canonical"], prior["canonical"])
                )
                if overlap < self.hygiene_policy.dedup_word_overlap:
                    continue
                duplicates.append(
                    {
                        "path": row["path"],
                        "line_number": row["line_number"],
                        "text": row["text"],
                        "duplicate_of": {
                            "path": prior["path"],
                            "line_number": prior["line_number"],
                            "text": prior["text"],
                        },
                        "overlap": overlap,
                    }
                )
                break
        return duplicates

    def stats(self) -> dict[str, Any]:
        files = self._list_files(include_viola=True)
        memory_files = self._list_files(include_viola=False)
        topic_files = [file for file in memory_files if file.path.startswith("memory/%s/" % TOPICS_DIR)]
        memory_entries = self._entry_rows(include_viola=False)
        viola_entries = self._entry_rows(include_viola=True)
        duplicates = self._duplicate_report()
        audit_archives = sorted(self.root.glob("%s.*" % AUDIT_LOG))
        oversize_files: list[dict[str, Any]] = []
        for file in files:
            if (
                self.hygiene_policy.memory_file_max_bytes > 0 and file.bytes > self.hygiene_policy.memory_file_max_bytes
            ) or (
                self.hygiene_policy.memory_file_max_lines > 0 and file.lines > self.hygiene_policy.memory_file_max_lines
            ):
                oversize_files.append(file.__dict__)
        return {
            "account_id": self.user_id,
            "root": str(self.account_dir),
            "settings_path": "memory/%s" % HYGIENE_SETTINGS,
            "settings": self.hygiene_policy.as_public_dict(),
            "files": {
                "total_markdown": len(files),
                "memory_markdown": len(memory_files),
                "topic_files": len(topic_files),
                "total_bytes": sum(file.bytes for file in files),
                "total_lines": sum(file.lines for file in files),
                "items": [file.__dict__ for file in files],
            },
            "entries": {
                "memory_entries": len(memory_entries),
                "viola_and_memory_entries": len(viola_entries),
            },
            "duplicates": {
                "count": len(duplicates),
                "sample": duplicates[:25],
            },
            "oversize_files": oversize_files,
            "audit": {
                "bytes": self.audit_path.stat().st_size if self.audit_path.exists() else 0,
                "archives": [
                    {
                        "path": "memory/%s" % path.name,
                        "bytes": path.stat().st_size,
                    }
                    for path in audit_archives
                ],
            },
        }

    def _list_files(self, *, include_viola: bool) -> list[MemoryFile]:
        rows: list[MemoryFile] = []
        for path in self._markdown_files(include_viola=include_viola):
            text = self._read_memory_file(path)
            stat = path.stat()
            rows.append(
                MemoryFile(
                    path=self._relative(path),
                    bytes=stat.st_size,
                    lines=len(text.splitlines()),
                    modified_at=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                )
            )
        return rows

    def _markdown_files(self, *, include_viola: bool) -> list[Path]:
        files: list[Path] = []
        if include_viola:
            files.append(self.viola_path)
        files.append(self.index_path)
        files.extend(sorted(self.topics_dir.glob("*.md")))
        return [path for path in files if path.exists()]

    def scan_memory_files(self) -> list[MemoryHeader]:
        """Scan topic files and return frontmatter-only headers (newest first).

        Claude-parity: this is the recall scan path. We only read the first
        ``_FRONTMATTER_MAX_LINES`` of each topic file to extract metadata —
        the full body never enters the selector's context. MEMORY.md is
        excluded (it's already loaded into context as the index).
        """
        headers: list[MemoryHeader] = []
        for path in sorted(self.topics_dir.rglob("*.md")):
            if path.name == MEMORY_INDEX:
                continue
            try:
                stat = path.stat()
                # Read whole file then slice — topic files are already small;
                # encryption is per-file so partial reads aren't safe.
                raw = self._read_memory_file(path, errors="replace")
                head = "\n".join(raw.splitlines()[: _FRONTMATTER_MAX_LINES + 2])
                frontmatter = _parse_frontmatter(head)
            except OSError:
                continue
            relative = path.relative_to(self.topics_dir).as_posix()
            headers.append(
                MemoryHeader(
                    filename=relative,
                    file_path=str(path),
                    mtime_ms=stat.st_mtime * 1000.0,
                    description=frontmatter.get("description") or None,
                    type=frontmatter.get("type") or None,
                    name=frontmatter.get("name") or None,
                )
            )
        headers.sort(key=lambda h: h.mtime_ms, reverse=True)
        return headers[:_MAX_MEMORY_FILES]

    @staticmethod
    def format_memory_manifest(headers: list[MemoryHeader]) -> str:
        """Format scan output as a one-line-per-file manifest.

        Mirrors Claude's ``formatMemoryManifest``: a selector LLM sees only
        the manifest, never the file bodies. Format:
        ``- [type] filename (iso-timestamp): description``.
        """
        lines: list[str] = []
        for header in headers:
            ts = datetime.fromtimestamp(header.mtime_ms / 1000.0, UTC).isoformat()
            tag = "[%s] " % header.type if header.type else ""
            if header.description:
                lines.append("- %s%s (%s): %s" % (tag, header.filename, ts, header.description))
            else:
                lines.append("- %s%s (%s)" % (tag, header.filename, ts))
        return "\n".join(lines)

    def find_relevant_memories_by_metadata(self, query: str, limit: int = 5) -> list[MemoryHeader]:
        """Deterministic metadata-only selector for recall.

        Claude-parity: returns at most ``limit`` headers whose name /
        description / type metadata contains tokens from ``query``. Returns
        ``[]`` when the query is empty, when no topic files exist, or when
        nothing matches — matching the "include only clearly-useful memories"
        rule. This is the non-LLM baseline; callers that have an LLM
        available can replace it with a side-query selector against the
        manifest from :meth:`format_memory_manifest`.
        """
        clean = (query or "").strip().lower()
        if not clean:
            return []
        tokens = [token for token in re.findall(r"[a-z0-9_'-]+", clean) if len(token) > 1]
        if not tokens:
            return []
        headers = self.scan_memory_files()
        if not headers:
            return []
        scored: list[tuple[int, float, MemoryHeader]] = []
        for header in headers:
            haystack = " ".join(
                value.lower() for value in (header.name, header.description, header.type, header.filename) if value
            )
            if not haystack:
                continue
            score = sum(1 for token in tokens if token in haystack)
            if score:
                scored.append((score, header.mtime_ms, header))
        if not scored:
            return []
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [header for _score, _mtime, header in scored[: max(1, int(limit))]]

    def audit(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        lines = self.audit_path.read_text(encoding="utf-8").splitlines()[-max(1, limit) :]
        entries: list[dict[str, Any]] = []
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                entries.append(payload)
        return entries

    def _legacy_db_path(self) -> Path:
        return self._data_root / "data" / "persistence" / "state.sqlite3"

    def _migrate_legacy_once(self) -> None:
        marker = self.root / ".legacy_memory_migrated"
        if marker.exists():
            return
        db_path = self._legacy_db_path()
        if not db_path.exists():
            marker.write_text(_now_iso(), encoding="utf-8")
            return
        migrated, skipped = self._migrate_legacy_sqlite(db_path)
        marker.write_text(_now_iso(), encoding="utf-8")
        if migrated or skipped:
            self._audit(
                "migration",
                "memory/topics/",
                "migrated=%d skipped=%d from %s" % (migrated, skipped, db_path),
                agent_initiated=False,
            )

    def _migrate_legacy_sqlite(self, db_path: Path) -> tuple[int, int]:
        decryptor = _LegacyMemoryDecryptor(db_path)
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            return (0, 0)
        migrated = 0
        skipped = 0
        try:
            has_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'").fetchone()
            if has_table is None:
                return (0, 0)
            rows = conn.execute(
                """
                SELECT id, content, category, source, created_at
                FROM memories
                WHERE user_id = ? AND active = 1
                ORDER BY category, created_at
                """,
                (self.account_id,),
            ).fetchall()
            grouped: dict[str, list[str]] = {}
            for row in rows:
                content = decryptor.decrypt(str(row["content"]))
                if not content or content == _KEY_MISMATCH_MARKER:
                    skipped += 1
                    continue
                category = str(row["category"] or "fact").strip().lower()
                topic = _category_topic(category)
                grouped.setdefault(topic, []).append("- %s" % content.strip())
                migrated += 1
            for topic, lines in grouped.items():
                target = self._normalize_memory_path("%s/%s.md" % (TOPICS_DIR, topic))
                existing = self._read_memory_file(target) if target.exists() else ""
                header = "# Migrated %s\n\n" % topic.replace("-", " ").title()
                payload = header + "\n".join(lines) + "\n"
                self._write_memory_file(target, existing + ("\n" if existing else "") + payload)
        except sqlite3.Error:
            logger.warning("Legacy memory migration failed for %s", db_path)
            return (migrated, skipped)
        finally:
            conn.close()
        return (migrated, skipped)


def _category_topic(category: str) -> str:
    return {
        "preference": "preferences",
        "fact": "facts",
        "correction": "corrections",
        "routine": "routines",
        "context": "context",
        "note": "notes",
    }.get(category, re.sub(r"[^A-Za-z0-9_.-]+", "-", category) or "notes")


def get_memory_dir(user_id: str, root: Path | None = None) -> MemoryDir:
    return MemoryDir(user_id=user_id, root=root)


__all__ = [
    "ARCHIVE_DIR",
    "AUDIT_LOG",
    "MEMORY_INDEX",
    "MEMORY_MAIN_MD_MAX_BYTES",
    "MEMORY_TOPIC_MD_MAX_BYTES",
    "MEMORY_VIOLA_MD_MAX_BYTES",
    "TOPICS_DIR",
    "USER_MEMORY",
    "VIOLA_FILE",
    "VIOLA_MEMORY",
    "MemoryDir",
    "MemoryFile",
    "MemoryHeader",
    "account_root",
    "auto_memory_base_dir",
    "auto_memory_disabled_reason",
    "get_memory_dir",
    "is_auto_memory_enabled",
    "safe_account_id",
]
