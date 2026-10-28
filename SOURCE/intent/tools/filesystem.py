"""Filesystem tools for the agent.

Viola's file abilities match a coding agent's (founder rule 2026-10-24): read
whole files with line numbers and paging, exact-string edits (single or
several at once), whole-file writes, glob, grep, list, mkdir, move, copy and
delete -- across the user's home folder and drives. Liberty comes with three
defences that actually work instead of arbitrary caps:

* **Read before overwrite.** Replacing an existing file requires that it was
  read in this conversation and has not changed since (mtime/size, then
  SHA-256). That stops blind truncation without a line-count cap.
* **Undo for everything.** Every write, edit, move, copy, mkdir and delete is
  recorded per user and conversation with a backup of what it replaced, under
  ``<data>/viola_file_history``. ``undo_change`` puts things back (and the
  state it replaces is itself backed up, so nothing is lost). Deletes also go
  to the Recycle Bin / Trash rather than being destroyed.
* **Targeted gates.** Which calls ask the user first is decided in
  ``intent.file_gates`` (deletes, overwrites outside the files the user
  referenced, bulk moves, startup/executable locations).

Protected locations stay protected: system folders, credential files, code
load roots and Viola's own runtime stores (deny lists below).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from intent.tool_types import ToolResult

logger = get_logger(__name__)

# System folders. Windows names are matched by LOCATION (top level of a drive,
# or ``...\\Microsoft\\Windows``), not as any folder that happens to share the
# name -- a project folder called ``windows`` is the user's, not the OS's.
# ``system32`` / ``syswow64`` are distinctive enough to block anywhere.
_BLOCKED_TOP_LEVEL_DIRS_WINDOWS = {
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
}
_BLOCKED_ANYWHERE_DIRS_WINDOWS = {"system32", "syswow64"}
# Kept for callers that import the old name.
_BLOCKED_DIRS_WINDOWS = _BLOCKED_TOP_LEVEL_DIRS_WINDOWS | _BLOCKED_ANYWHERE_DIRS_WINDOWS
_BLOCKED_DIRS_UNIX = {
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/boot",
    "/proc",
    "/sys",
    "/dev",
    "/etc",
}

# Reads: whole files by default, like a coding agent. A read stops early only
# to keep one tool result inside the context budget (results over ~50K chars
# are spilled to disk by the conversation layer), and then says exactly how
# many lines remain and the offset to continue from.
_DEFAULT_READ_LINES = 2000
_MAX_READ_LINES = _DEFAULT_READ_LINES  # legacy name
_READ_CHAR_BUDGET = 40_000
_MAX_LINE_CHARS = 2000
_DEFAULT_LIST_ENTRIES = 500
_MAX_LIST_ENTRIES = 2000
_DEFAULT_GLOB_RESULTS = 200
_MAX_GLOB_RESULTS = 1000
_MAX_SEARCH_RESULTS = _DEFAULT_GLOB_RESULTS  # legacy name
_DEFAULT_GREP_MATCHES = 100
_MAX_GREP_MATCHES = 500
_MAX_GREP_CONTEXT = 10
_GREP_LINE_CHARS = 500
_GREP_CHAR_BUDGET = 40_000
_GLOB_TIME_BUDGET_S = 10.0
_GREP_TIME_BUDGET_S = 15.0
_WALK_ENTRY_CAP = 300_000
_GREP_FILE_CAP = 20_000
# Files loaded whole into memory: edits, and reads that can be hashed.
_MAX_EDIT_FILE_BYTES = 20 * 1024 * 1024
_MAX_GREP_FILE_BYTES = 20 * 1024 * 1024
# Line counting for very large reads streams the file; past this size the
# total is reported as unknown instead of scanning gigabytes.
_MAX_LINE_COUNT_BYTES = 256 * 1024 * 1024
# Backups: anything up to these sizes is copied before it is changed.
_MAX_BACKUP_FILE_BYTES = 100 * 1024 * 1024
_MAX_BACKUP_TREE_BYTES = 256 * 1024 * 1024
_MAX_BACKUP_TREE_FILES = 5000
_HISTORY_RETENTION_S = 14 * 24 * 3600
_MAX_SESSIONS_IN_MEMORY = 64

# Folders a glob/grep walk does not descend into (noise or OS-owned).
_WALK_PRUNE_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    "$recycle.bin",
    "system volume information",
    "viola_file_history",
}

# Local runtime stores can contain cookies, vault material, and auth/session
# state. They must not be exposed through generic filesystem tools.
_SENSITIVE_RUNTIME_DENY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"[\\/](?:browser_profiles|api_vault|agent_audit|codex-logs|subagents)(?:[\\/]|$)",
        re.IGNORECASE,
    ),
    re.compile(r"[\\/]payment_vault(?:_[^\\/]*)?\.enc$", re.IGNORECASE),
    re.compile(r"[\\/]\.secrets\.enc$", re.IGNORECASE),
    re.compile(
        r"[\\/](?:Google[\\/]Chrome|Microsoft[\\/]Edge|Chromium|BraveSoftware|Mozilla[\\/]Firefox)[\\/]",
        re.IGNORECASE,
    ),
    re.compile(r"[\\/](?:Cookies|Login Data|Web Data|Local State)(?:-journal)?$", re.IGNORECASE),
    re.compile(r"[\\/](?:Local Storage|Session Storage|IndexedDB)(?:[\\/]|$)", re.IGNORECASE),
]

# Code-load roots: directories Viola executes from at startup / discovery.
# A write into any of these converts a CONFIRM-tier file write into arbitrary
# code execution on the next load (plugin entry_point exec, SKILL.md inline
# shell, hooks/commands). The agent file tools MUST NOT be able to plant code
# into these roots — they are gated by signature/approval elsewhere, not by the
# generic write path. Matches the directory anywhere in the path (case-
# insensitive, both slash styles). SEC-031/SEC-032/SEC-034 (sweep 2026-06-09).
_CODE_LOAD_ROOT_DENY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"[\\/]plugins[\\/](?:user|builtin)(?:[\\/]|$)", re.IGNORECASE),
    re.compile(r"[\\/]\.claude(?:[\\/]|$)", re.IGNORECASE),
    re.compile(r"[\\/]skills(?:[\\/]|$)", re.IGNORECASE),
    re.compile(r"[\\/]plugin\.json$", re.IGNORECASE),
    re.compile(r"[\\/]SKILL\.md$", re.IGNORECASE),
]

# Shell-rc / autostart persistence: a write here runs chosen code on the next
# shell login or desktop session, the same code-exec-persistence class the
# code-load-root list above exists to stop (#2776). Matches the well-known
# per-user rc files plus any .desktop entry under the XDG autostart directory.
_SHELL_RC_AUTOSTART_DENY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"[\\/]\.bashrc$", re.IGNORECASE),
    re.compile(r"[\\/]\.zshrc$", re.IGNORECASE),
    re.compile(r"[\\/]\.bash_profile$", re.IGNORECASE),
    re.compile(r"[\\/]\.profile$", re.IGNORECASE),
    re.compile(r"[\\/]\.config[\\/]autostart[\\/][^\\/]*\.desktop$", re.IGNORECASE),
]

# Credential- and secret-bearing file patterns. A file the READ path refuses to
# disclose MUST also be one the WRITE/DELETE path refuses to overwrite or
# irreversibly destroy — otherwise the agent (or a misfire during a "clean up my
# Downloads" request) can wipe ~/.aws/credentials, ~/.npmrc, an id_rsa/id_ed25519
# key, a *.pem/*.key, a crypto wallet, or a .env.production even though it is
# forbidden to even read them. These live in ONE shared list included in BOTH
# deny lists below so the two can never drift into a non-superset again
# (2026-07-18 hardening: the write-deny list had drifted, leaving every
# credential class above deletable while read blocked it).
# Use [\\/] to match both forward and back slashes (cross-platform).
_CREDENTIAL_DENY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"[\\/]\.ssh[\\/]", re.IGNORECASE),
    re.compile(r"[\\/]\.env\.", re.IGNORECASE),  # .env.local, .env.production, etc.
    re.compile(r"[\\/]\.aws[\\/]", re.IGNORECASE),
    re.compile(r"[\\/]\.gnupg[\\/]", re.IGNORECASE),
    re.compile(r"[\\/]\.docker[\\/]config\.json$", re.IGNORECASE),
    re.compile(r"[\\/]credentials", re.IGNORECASE),
    re.compile(r"[\\/](?:\.netrc|_netrc)$", re.IGNORECASE),
    re.compile(r"[\\/]\.npmrc$", re.IGNORECASE),
    re.compile(r"[\\/]\.pypirc$", re.IGNORECASE),
    re.compile(r"id_rsa", re.IGNORECASE),
    re.compile(r"id_ed25519", re.IGNORECASE),
    re.compile(r"\.pem$", re.IGNORECASE),
    re.compile(r"\.key$", re.IGNORECASE),
    re.compile(r"\.keystore", re.IGNORECASE),
    re.compile(r"[\\/]wallet", re.IGNORECASE),
    re.compile(r"[\\/]\.?token", re.IGNORECASE),
    re.compile(r"[\\/]\.?secret", re.IGNORECASE),
]

# Patterns blocked for write/delete operations (protect sensitive files).
# Use [\\/] to match both forward and back slashes (cross-platform).
_WRITE_DENY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\.env$", re.IGNORECASE),
    re.compile(r"[\\/]\.git[\\/]", re.IGNORECASE),
    re.compile(r"[\\/]windows[\\/]", re.IGNORECASE),
    re.compile(r"[\\/]etc[\\/]", re.IGNORECASE),
    re.compile(r"[\\/]secrets(?:[\\/]|$)", re.IGNORECASE),
    # Viola's own undo history: the agent restores from it with action='undo'
    # and must never be able to rewrite or delete the backups themselves.
    re.compile(r"[\\/]viola_file_history(?:[\\/]|$)", re.IGNORECASE),
    *_CREDENTIAL_DENY_PATTERNS,
    *_CODE_LOAD_ROOT_DENY_PATTERNS,
    *_SENSITIVE_RUNTIME_DENY_PATTERNS,
    *_SHELL_RC_AUTOSTART_DENY_PATTERNS,
]

# Patterns blocked for read operations (protect credentials and secrets).
# Matches against the full resolved path, case-insensitive.
_READ_DENY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"[\\/]\.git[\\/]", re.IGNORECASE),
    re.compile(r"[\\/]\.env$", re.IGNORECASE),
    *_CREDENTIAL_DENY_PATTERNS,
    *_SENSITIVE_RUNTIME_DENY_PATTERNS,
]


def _is_read_denied(path: Path) -> str | None:
    """Check if a path is on the read deny list.

    Returns a human-readable reason or None if allowed.
    """
    path_str = str(path)
    for pattern in _READ_DENY_PATTERNS:
        if pattern.search(path_str):
            return "Access denied: reading sensitive credential files is not permitted"
    return None


def _is_write_denied(path: Path) -> str | None:
    """Check if a path is on the write deny list.

    Returns a human-readable reason or None if allowed.
    """
    path_str = str(path)
    for pattern in _WRITE_DENY_PATTERNS:
        if pattern.search(path_str):
            return "Writing to this path is blocked for safety: %s" % pattern.pattern
    return None


def _windows_system_dir(p: Path) -> str | None:
    parts = [part.lower() for part in p.parts]
    for blocked in _BLOCKED_ANYWHERE_DIRS_WINDOWS:
        if blocked in parts:
            return blocked
    # parts[0] is the drive/anchor ("c:\\"); parts[1] is the top-level folder.
    if len(parts) > 1 and parts[1] in _BLOCKED_TOP_LEVEL_DIRS_WINDOWS:
        return parts[1]
    for index in range(1, len(parts)):
        if parts[index] == "windows" and parts[index - 1] == "microsoft":
            return "windows"
    return None


# ---------------------------------------------------------------------------
# The user's own folders. "Desktop", "Documents", "Downloads" ... and relative
# paths mean the user's real folders, never Viola's install or working folder.
# 1.0.5 laptop X8.2: "save it to a file on my desktop" was written as the
# relative path ``Desktop\viola-share-test.txt`` and landed inside the program
# folder (lost on the next update) while Viola said "on your desktop".
# ---------------------------------------------------------------------------

# Windows KNOWNFOLDERIDs. SHGetKnownFolderPath returns the folder where the
# user really keeps it, including OneDrive / folder redirection.
_KNOWN_FOLDER_IDS: dict[str, str] = {
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    "videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
}
_KNOWN_FOLDER_ALIASES: dict[str, str] = {
    "desktop": "desktop",
    "documents": "documents",
    "my documents": "documents",
    "downloads": "downloads",
    "pictures": "pictures",
    "my pictures": "pictures",
    "photos": "pictures",
    "music": "music",
    "my music": "music",
    "videos": "videos",
    "my videos": "videos",
    "movies": "videos",
}
# freedesktop user-dirs.dirs keys.
_XDG_KEYS: dict[str, str] = {
    "desktop": "XDG_DESKTOP_DIR",
    "documents": "XDG_DOCUMENTS_DIR",
    "downloads": "XDG_DOWNLOAD_DIR",
    "pictures": "XDG_PICTURES_DIR",
    "music": "XDG_MUSIC_DIR",
    "videos": "XDG_VIDEOS_DIR",
}
_DEFAULT_FOLDER_NAMES: dict[str, str] = {
    "desktop": "Desktop",
    "documents": "Documents",
    "downloads": "Downloads",
    "pictures": "Pictures",
    "music": "Music",
    "videos": "Videos",
}
# Where a bare relative name ("notes.txt", "projects/plan.md") goes.
_RELATIVE_PATH_BASE_FOLDER = "documents"


def _windows_known_folder(folder_id: str) -> Path | None:
    import ctypes
    from ctypes import wintypes

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    guid = _GUID()
    ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
    if ole32.CLSIDFromString(ctypes.c_wchar_p(folder_id), ctypes.byref(guid)) != 0:
        return None
    path_ptr = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    result = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path_ptr))
    try:
        if result != 0 or not path_ptr.value:
            return None
        return Path(path_ptr.value)
    finally:
        ole32.CoTaskMemFree(path_ptr)


def _xdg_user_dir(key: str, home: Path) -> Path | None:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
    try:
        text = (config_home / "user-dirs.dirs").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'^\s*%s\s*=\s*"([^"]*)"' % re.escape(key), text, flags=re.MULTILINE)
    if not match:
        return None
    value = match.group(1).replace("$HOME", str(home))
    return Path(value) if value else None


def user_known_folder(name: str) -> Path | None:
    """The user's real Desktop / Documents / Downloads / ... folder, or None.

    Windows asks the shell (OneDrive and redirected folders included); Linux
    reads the freedesktop user-dirs file; otherwise ``~/<Name>``.
    """
    key = _KNOWN_FOLDER_ALIASES.get(str(name or "").strip().lower())
    if key is None:
        return None
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        return None
    if os.name == "nt":
        try:
            found = _windows_known_folder(_KNOWN_FOLDER_IDS[key])
        except (AttributeError, OSError, ValueError):
            found = None
        if found is not None:
            return found
    elif key in _XDG_KEYS:
        found = _xdg_user_dir(_XDG_KEYS[key], home)
        if found is not None:
            return found
    return home / _DEFAULT_FOLDER_NAMES[key]


def _split_first_component(text: str) -> tuple[str, str]:
    parts = re.split(r"[\\/]+", text, maxsplit=1)
    head = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    return head, rest


def resolve_user_path_text(path_str: str) -> str:
    """Turn what the model wrote into the path the user means.

    * ``~`` and environment variables expand as usual.
    * ``Desktop/x``, ``~/Desktop/x``, ``Documents``, ``Downloads/...`` (and
      Pictures, Music, Videos) go to the user's real known folder.
    * Any other relative path goes under the user's Documents folder.
    * Absolute paths are left exactly as written.
    """
    text = os.path.expandvars(str(path_str or "").strip())
    if not text:
        return text
    if text == "~" or text.startswith(("~/", "~\\")):
        rest = text[2:] if len(text) > 1 else ""
        head, tail = _split_first_component(rest)
        folder = user_known_folder(head) if head else None
        if folder is not None:
            return str(folder / tail) if tail else str(folder)
        return os.path.expanduser(text)
    text = os.path.expanduser(text)
    if os.path.isabs(text) or re.match(r"^[A-Za-z]:", text) or text.startswith(("\\\\", "//")):
        return text
    head, tail = _split_first_component(text)
    folder = user_known_folder(head)
    if folder is not None:
        return str(folder / tail) if tail else str(folder)
    base = user_known_folder(_RELATIVE_PATH_BASE_FOLDER)
    if base is None:
        return text
    return str(base / text)


def _validate_path(path_str: str) -> tuple[Path, str | None]:
    """Validate and resolve a filesystem path.

    Returns:
        (resolved_path, error_message). error_message is None if valid.
    """
    if not str(path_str or "").strip():
        return Path("."), "A path is required"
    try:
        p_raw = Path(resolve_user_path_text(str(path_str)))
    except (ValueError, OSError) as exc:
        return Path("."), "Invalid path: %s" % exc

    # Block symlinks to prevent traversal.
    # IMPORTANT: check BEFORE .resolve() — resolve() follows symlinks so
    # p.is_symlink() would always return False on the resolved path.
    try:
        if p_raw.is_symlink():
            return p_raw, "Symlinks are not allowed for security"
    except OSError as exc:
        return p_raw, "Invalid path: %s" % exc

    try:
        p = p_raw.resolve()
    except (ValueError, OSError) as exc:
        return Path("."), "Invalid path: %s" % exc

    if os.name == "nt":
        blocked = _windows_system_dir(p)
        if blocked:
            return p, "Access to system directory '%s' is denied" % blocked
    else:
        path_lower = str(p).lower()
        for blocked in _BLOCKED_DIRS_UNIX:
            if path_lower.startswith(blocked) and (len(path_lower) == len(blocked) or path_lower[len(blocked)] == "/"):
                return p, "Access to system directory '%s' is denied" % blocked

    return p, None


def _path_key(p: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def resolve_tool_path(path_str: str) -> Path | None:
    """Resolve a tool path the way the file tools do, or None if invalid.

    Used by ``intent.file_gates`` so the gate judges the same path the tool
    will touch.
    """
    if not str(path_str or "").strip():
        return None
    p, _err = _validate_path(path_str)
    return p


# ---------------------------------------------------------------------------
# Text encoding and line endings: preserved exactly on every edit.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TextFormat:
    codec: str  # python codec used for the body
    bom: bytes
    newline: str  # dominant line ending: "\r\n" or "\n"
    label: str  # human label, e.g. "utf-8", "utf-8-bom", "utf-16-le"


def _detect_newline(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf and crlf >= lf else "\n"


def _decode_text(raw: bytes, *, strict: bool) -> tuple[str | None, _TextFormat | None, str | None]:
    """Decode file bytes, remembering how to encode them back byte-for-byte.

    Returns ``(text, format, error)``. UTF-8 (with or without BOM) and BOM'd
    UTF-16 are recognised; other text falls back to Windows-1252 and then
    Latin-1, both of which round-trip every untouched byte exactly. Bytes with
    NULs and no UTF-16 BOM are treated as binary.
    """
    boms = (
        (b"\xef\xbb\xbf", "utf-8", "utf-8-bom"),
        (b"\xff\xfe", "utf-16-le", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be", "utf-16-be"),
    )
    for bom, codec, label in boms:
        if raw.startswith(bom):
            body = raw[len(bom) :]
            try:
                text = body.decode(codec)
            except UnicodeDecodeError:
                if strict:
                    return None, None, "File is not valid %s text, so editing it could corrupt it" % label
                text = body.decode(codec, errors="replace")
            return text, _TextFormat(codec, bom, _detect_newline(text), label), None
    if b"\x00" in raw[:8192]:
        return None, None, "binary"
    for codec in ("utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(codec)
        except UnicodeDecodeError:
            continue
        return text, _TextFormat(codec, b"", _detect_newline(text), codec), None
    return None, None, "binary"  # unreachable: latin-1 decodes every byte


def _encode_text(text: str, fmt: _TextFormat) -> tuple[bytes | None, str | None]:
    try:
        return fmt.bom + text.encode(fmt.codec), None
    except UnicodeEncodeError as exc:
        return None, (
            "This file is stored as %s, which cannot hold the character %r in the new text. "
            "Nothing was changed." % (fmt.label, exc.object[exc.start : exc.end])
        )


def _to_newline(text: str, newline: str) -> str:
    normalized = text.replace("\r\n", "\n")
    return normalized.replace("\n", "\r\n") if newline == "\r\n" else normalized


def _split_keepends(text: str) -> list[str]:
    """Split on newline only (keeping CRLF intact), so line numbers match ``_count_lines``."""
    if not text:
        return []
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _count_lines(text: str) -> int:
    """Count lines the way ``read_file`` does (``splitlines`` semantics)."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _file_sha256(p: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(p, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Per-conversation file state: what was read, what changed, and the backups.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileScope:
    """Whose conversation a file call belongs to (backups and read records)."""

    user_id: str
    session_key: str


# In-process direct callers (tests, legacy helpers) that pass no scope. The MCP
# tools always pass the caller's real user/session scope.
_DIRECT_SCOPE = FileScope(user_id="_direct_call", session_key="direct")

# Tests point this at a temp folder; production resolves ``<data>/viola_file_history``.
_HISTORY_ROOT_OVERRIDE: Path | None = None


def _history_root() -> Path:
    if _HISTORY_ROOT_OVERRIDE is not None:
        return Path(_HISTORY_ROOT_OVERRIDE)
    from core.platform import get_data_dir

    return get_data_dir() / "viola_file_history"


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", value or "")[:40].strip("._") or "x"
    return "%s-%s" % (cleaned, hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:10])


@dataclass
class _ReadRecord:
    mtime_ns: int
    size: int
    sha256: str | None
    complete: bool


@dataclass
class _Change:
    id: str
    ts: float
    action: str
    path: str
    destination: str | None
    # Ordered (path, before) pairs. ``before`` says how to put that path back:
    # {"kind": "absent"} | {"kind": "backup", "ref": <history path>}
    # | {"kind": "moved", "ref": <where the content now lives>}
    # | {"kind": "trash"} (only the Recycle Bin / Trash holds it).
    entries: list[dict[str, Any]] = field(default_factory=list)
    undone: bool = False
    undo_of: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "action": self.action,
            "path": self.path,
            "destination": self.destination,
            "entries": self.entries,
            "undone": self.undone,
            "undo_of": self.undo_of,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> _Change:
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            ts=float(data.get("ts") or 0.0),
            action=str(data.get("action") or ""),
            path=str(data.get("path") or ""),
            destination=data.get("destination"),
            entries=list(data.get("entries") or []),
            undone=bool(data.get("undone")),
            undo_of=data.get("undo_of"),
        )


class _SessionState:
    def __init__(self, scope: FileScope) -> None:
        self.scope = scope
        self.lock = threading.RLock()
        self.reads: dict[str, _ReadRecord] = {}
        self.touched: set[str] = set()
        self.created_dirs: set[str] = set()
        self.changes: list[_Change] = []
        self._counter = 0
        self.dir = _history_root() / _safe_component(scope.user_id) / _safe_component(scope.session_key)
        self._load()

    # -- persistence -------------------------------------------------------

    @property
    def log_path(self) -> Path:
        return self.dir / "changes.jsonl"

    def _load(self) -> None:
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                change = _Change.from_json(json.loads(line))
            except (ValueError, TypeError):
                continue
            self.changes.append(change)
        self._counter = len(self.changes)
        for change in self.changes:
            if change.undone:
                continue
            for entry in change.entries:
                self.touched.add(_path_key(entry.get("path") or ""))
            if change.action == "mkdir":
                self.created_dirs.add(_path_key(change.path))

    def _persist(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            payload = "".join(json.dumps(change.to_json(), ensure_ascii=False) + "\n" for change in self.changes)
            tmp = self.log_path.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.log_path)
        except OSError:
            logger.exception("Could not save file change history in %s", self.dir)

    # -- helpers -------------------------------------------------------------

    def new_change(self, action: str, path: Path, destination: Path | None = None) -> _Change:
        self._counter += 1
        change_id = "c%d-%s" % (self._counter, uuid.uuid4().hex[:6])
        return _Change(
            id=change_id,
            ts=time.time(),
            action=action,
            path=str(path),
            destination=str(destination) if destination is not None else None,
        )

    def blob_path(self, change: _Change, source: Path) -> Path:
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", source.name)[:60] or "item"
        return self.dir / "blobs" / ("%s-%d-%s" % (change.id, len(change.entries), name))

    def record(self, change: _Change) -> None:
        self.changes.append(change)
        for entry in change.entries:
            self.touched.add(_path_key(entry.get("path") or ""))
        self._persist()

    def note_read(self, p: Path, raw: bytes | None, complete: bool) -> None:
        try:
            st = p.stat()
        except OSError:
            return
        self.reads[_path_key(p)] = _ReadRecord(
            mtime_ns=st.st_mtime_ns,
            size=st.st_size,
            sha256=_sha256(raw) if raw is not None else None,
            complete=complete,
        )

    def is_agent_touched(self, p: Path) -> bool:
        key = _path_key(p)
        if key in self.touched:
            return True
        for created in self.created_dirs:
            if key.startswith(created.rstrip("\\/") + os.sep):
                return True
        return False


_SESSIONS: OrderedDict[tuple[str, str], _SessionState] = OrderedDict()
_SESSIONS_LOCK = threading.Lock()


def _reset_sessions_for_tests() -> None:
    with _SESSIONS_LOCK:
        _SESSIONS.clear()


def _prune_old_history(user_dir: Path, keep: Path) -> None:
    cutoff = time.time() - _HISTORY_RETENTION_S
    try:
        children = list(user_dir.iterdir())
    except OSError:
        return
    for child in children:
        if child == keep or not child.is_dir():
            continue
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


def _state_for(scope: FileScope | None) -> _SessionState:
    scope = scope or _DIRECT_SCOPE
    key = (scope.user_id, scope.session_key)
    with _SESSIONS_LOCK:
        state = _SESSIONS.get(key)
        if state is not None:
            _SESSIONS.move_to_end(key)
            return state
        state = _SessionState(scope)
        _SESSIONS[key] = state
        while len(_SESSIONS) > _MAX_SESSIONS_IN_MEMORY:
            _SESSIONS.popitem(last=False)
    _prune_old_history(state.dir.parent, state.dir)
    return state


def peek_session_state(user_id: str, session_key: str) -> _SessionState | None:
    """The live file state for a conversation, without creating one."""
    with _SESSIONS_LOCK:
        return _SESSIONS.get((user_id, session_key))


def path_touched_in_session(user_id: str, session_key: str, path: Path) -> bool:
    """True when Viola created or changed ``path`` earlier in this conversation."""
    state = peek_session_state(user_id, session_key)
    if state is None:
        try:
            state = _state_for(FileScope(user_id, session_key))
        except (OSError, ValueError):
            return False
    with state.lock:
        return state.is_agent_touched(path)


# ---------------------------------------------------------------------------
# Backups, Recycle Bin, tree helpers.
# ---------------------------------------------------------------------------


def tree_stats(p: Path, file_cap: int = _MAX_BACKUP_TREE_FILES + 1) -> tuple[int, int, bool]:
    """(files, bytes, capped) for a directory tree, stopping at ``file_cap``."""
    files = 0
    total = 0
    for root, dirs, names in os.walk(p, followlinks=False):
        for name in names:
            files += 1
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
            if files >= file_cap:
                return files, total, True
    return files, total, False


def _backup_into(state: _SessionState, change: _Change, target: Path) -> dict[str, Any] | None:
    """Copy ``target`` into history; return its ``before`` entry or None if too big."""
    blob = state.blob_path(change, target)
    blob.parent.mkdir(parents=True, exist_ok=True)
    if target.is_dir():
        _files, total, capped = tree_stats(target)
        if capped or total > _MAX_BACKUP_TREE_BYTES:
            return None
        shutil.copytree(target, blob, symlinks=True)
    else:
        if target.stat().st_size > _MAX_BACKUP_FILE_BYTES:
            return None
        shutil.copy2(target, blob)
    return {"kind": "backup", "ref": str(blob)}


def _stash_current(state: _SessionState, change: _Change, target: Path) -> dict[str, Any]:
    """Move whatever is at ``target`` into history (clearing it) and describe it."""
    if not os.path.lexists(target):
        return {"kind": "absent"}
    blob = state.blob_path(change, target)
    blob.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target), str(blob))
    return {"kind": "backup", "ref": str(blob)}


def _drive_supports_recycle_bin(p: Path) -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        anchor = p.anchor or str(p)[:3]
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(anchor))
        return drive_type == 3  # DRIVE_FIXED; removable/network drives delete permanently
    except (AttributeError, OSError, ValueError):
        return False


def _send_to_trash(p: Path) -> bool:
    """Move ``p`` to the Recycle Bin / Trash. True only if it is gone from ``p``."""
    try:
        if os.name == "nt":
            if not _drive_supports_recycle_bin(p):
                return False
            import ctypes
            from ctypes import wintypes

            class _SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [
                    ("hwnd", wintypes.HWND),
                    ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_uint16),
                    ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR),
                ]

            fo_delete = 0x0003
            flags = (
                0x0040 | 0x0010 | 0x0004 | 0x0400 | 0x0200
            )  # ALLOWUNDO|NOCONFIRMATION|SILENT|NOERRORUI|NOCONFIRMMKDIR
            op = _SHFILEOPSTRUCTW(None, fo_delete, str(p) + "\0", None, flags, False, None, None)
            result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
            return result == 0 and not op.fAnyOperationsAborted and not os.path.lexists(p)
        if os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
            trash = Path.home() / ".Trash"
            trash.mkdir(exist_ok=True)
            target = trash / p.name
            if target.exists():
                target = trash / ("%s %s%s" % (p.stem, time.strftime("%H.%M.%S"), p.suffix))
            shutil.move(str(p), str(target))
            return not os.path.lexists(p)
        # freedesktop.org Trash specification.
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "Trash"
        (base / "files").mkdir(parents=True, exist_ok=True)
        (base / "info").mkdir(parents=True, exist_ok=True)
        name = p.name
        while (base / "files" / name).exists():
            name = "%s.%s" % (p.name, uuid.uuid4().hex[:6])
        from urllib.parse import quote

        info = "[Trash Info]\nPath=%s\nDeletionDate=%s\n" % (quote(str(p)), time.strftime("%Y-%m-%dT%H:%M:%S"))
        (base / "info" / (name + ".trashinfo")).write_text(info, encoding="utf-8")
        shutil.move(str(p), str(base / "files" / name))
        return not os.path.lexists(p)
    except Exception:
        logger.exception("Could not move %s to the trash", p)
        return False


def _remove_path(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        p.unlink()


# ---------------------------------------------------------------------------
# Read-only tools.
# ---------------------------------------------------------------------------


def _scope_state(scope: FileScope | None) -> _SessionState:
    return _state_for(scope)


async def list_directory(path: str = "~", offset: int = 0, limit: int = 0) -> ToolResult:
    """List files and directories at the given path."""
    p, err = _validate_path(path)
    if err:
        return ToolResult(ok=False, error=err)

    denied = _is_read_denied(p)
    if denied:
        logger.warning("Blocked directory listing of sensitive path: %s", p)
        return ToolResult(ok=False, error=denied)

    if not p.exists():
        return ToolResult(
            ok=False,
            error="Path does not exist: %s. Retry with '~/Downloads' "
            "instead of a hardcoded user path (~ expands to the real "
            "home directory)." % p,
        )
    if not p.is_dir():
        return ToolResult(ok=False, error="Not a directory: %s" % p)

    limit = _DEFAULT_LIST_ENTRIES if limit <= 0 else min(int(limit), _MAX_LIST_ENTRIES)
    offset = max(0, int(offset or 0))

    def _order(entry: Path) -> tuple[bool, str]:
        try:
            return (not entry.is_dir(), entry.name.lower())
        except OSError:
            return (True, entry.name.lower())

    try:
        children = sorted(p.iterdir(), key=_order)
    except PermissionError:
        return ToolResult(ok=False, error="Permission denied: %s" % p)
    except OSError as exc:
        return ToolResult(ok=False, error="OS error: %s" % exc)

    visible = []
    for entry in children:
        if _is_read_denied(entry):
            logger.warning("Omitted sensitive directory entry from listing: %s", entry)
            continue
        visible.append(entry)
    page = visible[offset : offset + limit]
    entries: list[dict[str, object]] = []
    for entry in page:
        try:
            is_dir = entry.is_dir()
            st = entry.stat()
        except OSError:
            entries.append({"name": entry.name, "type": "unknown"})
            continue
        info: dict[str, object] = {"name": entry.name, "type": "dir" if is_dir else "file"}
        if not is_dir:
            info["size"] = st.st_size
        info["modified"] = int(st.st_mtime)
        entries.append(info)

    data: dict[str, object] = {
        "path": str(p),
        "entries": entries,
        "count": len(entries),
        "total": len(visible),
        "truncated": offset + len(page) < len(visible),
    }
    if offset + len(page) < len(visible):
        data["next_offset"] = offset + len(page)
    return ToolResult(ok=True, data=data)


def _display_line(number: int, line: str) -> str:
    body = line.rstrip("\r\n")
    if len(body) > _MAX_LINE_CHARS:
        body = body[:_MAX_LINE_CHARS] + " ... [line cut at %d of %d chars]" % (_MAX_LINE_CHARS, len(body))
    return "%6d\t%s" % (number, body)


def _json_cost(text: str) -> int:
    # Approximate size once JSON-escaped (tabs, newlines, quotes, backslashes).
    return len(text) + text.count("\\") + text.count('"') + text.count("\t") + 1


def _read_file_sync(path: str, offset: int, limit: int, scope: FileScope | None) -> ToolResult:
    p, err = _validate_path(path)
    if err:
        return ToolResult(ok=False, error=err)

    denied = _is_read_denied(p)
    if denied:
        logger.warning("Blocked read of sensitive path: %s", p)
        return ToolResult(ok=False, error=denied)

    if not p.exists():
        return ToolResult(ok=False, error="File does not exist: %s" % p)
    if p.is_dir():
        return ToolResult(ok=False, error="%s is a folder; use action='list' to see what is in it" % p)
    if not p.is_file():
        return ToolResult(ok=False, error="Not a file: %s" % p)

    offset = max(0, int(offset or 0))
    limit = _DEFAULT_READ_LINES if not limit or limit <= 0 else int(limit)
    try:
        size = p.stat().st_size
        with open(p, "rb") as handle:
            head = handle.read(8192)
    except PermissionError:
        return ToolResult(ok=False, error="Permission denied: %s" % p)
    except OSError as exc:
        return ToolResult(ok=False, error="Read error: %s" % exc)

    _probe, probe_fmt, probe_err = _decode_text(head, strict=False)
    if probe_err == "binary":
        return ToolResult(
            ok=False,
            error="Binary file detected, cannot read as text: %s (%d bytes)" % (p, size),
            error_category="binary_file",
            data={"path": str(p), "size": size},
        )

    raw: bytes | None = None
    total_lines: int | None
    if size <= _MAX_EDIT_FILE_BYTES:
        try:
            raw = p.read_bytes()
        except OSError as exc:
            return ToolResult(ok=False, error="Read error: %s" % exc)
        text, fmt, _decode_err = _decode_text(raw, strict=False)
        if text is None or fmt is None:
            return ToolResult(ok=False, error="Binary file detected, cannot read as text: %s" % p)
        lines = _split_keepends(text)
        total_lines = len(lines)
    else:
        # Very large text file: stream it so memory stays flat.
        fmt = probe_fmt or _TextFormat("utf-8", b"", "\n", "utf-8")
        lines = []
        total_lines = None

    selected: list[str] = []
    budget_hit = False
    used = 0
    if raw is not None:
        window = lines[offset : offset + limit]
        for index, line in enumerate(window):
            shown = _display_line(offset + index + 1, line)
            cost = _json_cost(shown)
            if selected and used + cost > _READ_CHAR_BUDGET:
                budget_hit = True
                break
            selected.append(shown)
            used += cost
    else:
        count = 0
        stream_codec = {"utf-8-bom": "utf-8-sig", "utf-16-le": "utf-16", "utf-16-be": "utf-16"}.get(
            fmt.label, fmt.codec
        )
        with open(p, encoding=stream_codec, errors="replace", newline="") as handle:
            for count, line in enumerate(handle, start=1):
                if count <= offset:
                    continue
                if len(selected) >= limit or budget_hit:
                    if size > _MAX_LINE_COUNT_BYTES:
                        count = -1
                        break
                    continue
                shown = _display_line(count, line)
                cost = _json_cost(shown)
                if selected and used + cost > _READ_CHAR_BUDGET:
                    budget_hit = True
                    if size > _MAX_LINE_COUNT_BYTES:
                        count = -1
                        break
                    continue
                selected.append(shown)
                used += cost
        total_lines = count if count >= 0 else None

    end = offset + len(selected)
    complete = offset == 0 and total_lines is not None and end >= total_lines
    state = _state_for(scope) if scope is not None else None
    if state is not None:
        with state.lock:
            state.note_read(p, raw, complete)

    data: dict[str, object] = {
        "path": str(p),
        "content": "\n".join(selected),
        "start_line": offset + 1 if selected else offset,
        "end_line": end,
        "lines_returned": len(selected),
        "total_lines": total_lines,
        "offset": offset,
        "truncated": not complete,
        "encoding": fmt.label,
        "line_endings": "CRLF" if fmt.newline == "\r\n" else "LF",
    }
    remaining = (total_lines - end) if total_lines is not None else None
    if total_lines is None or end < total_lines:
        data["next_offset"] = end
        data["remaining_lines"] = remaining
        data["note"] = "Showing lines %d-%d of %s%s. %s more lines: read again with offset=%d." % (
            offset + 1,
            end,
            total_lines if total_lines is not None else "an unknown number",
            " (output size limit)" if budget_hit else "",
            remaining if remaining is not None else "More",
            end,
        )
    elif total_lines == 0:
        data["note"] = "The file is empty."
    return ToolResult(ok=True, data=data)


async def read_file(path: str, offset: int = 0, limit: int = 0, *, scope: FileScope | None = None) -> ToolResult:
    """Read a text file with line numbers; ``offset``/``limit`` page big files."""
    return await asyncio.to_thread(_read_file_sync, path, offset, limit, scope)


async def file_info(path: str) -> ToolResult:
    """Get metadata about a file or directory."""
    p, err = _validate_path(path)
    if err:
        return ToolResult(ok=False, error=err)

    denied = _is_read_denied(p)
    if denied:
        logger.warning("Blocked metadata lookup for sensitive path: %s", p)
        return ToolResult(ok=False, error=denied)

    if not p.exists():
        return ToolResult(ok=False, error="Path does not exist: %s" % p)

    try:
        st = p.stat()
        info: dict[str, object] = {
            "path": str(p),
            "type": "directory" if p.is_dir() else "file",
            "size": st.st_size,
            "modified": st.st_mtime,
            "permissions": stat.filemode(st.st_mode),
        }
        if p.is_file():
            info["extension"] = p.suffix
        return ToolResult(ok=True, data=info)
    except PermissionError:
        return ToolResult(ok=False, error="Permission denied: %s" % p)
    except OSError as exc:
        return ToolResult(ok=False, error="OS error: %s" % exc)


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile a glob (``*``, ``?``, ``[...]``, ``**``) over '/'-separated paths."""
    pat = pattern.replace("\\", "/").lstrip("/")
    out = []
    i = 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        elif pat[i] == "[":
            close = pat.find("]", i + 1)
            if close == -1:
                out.append(re.escape(pat[i]))
                i += 1
            else:
                body = pat[i + 1 : close]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[%s]" % body.replace("\\", "\\\\"))
                i = close + 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    flags = re.IGNORECASE if os.name == "nt" else 0
    return re.compile("^%s$" % "".join(out), flags)


def _walk(root: Path, deadline: float, prune_extra: Iterable[str] = ()) -> Iterable[tuple[Path, list[str], list[str]]]:
    prune = _WALK_PRUNE_DIRS - {name.lower() for name in prune_extra}
    scanned = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        here = Path(current)
        kept = []
        for name in dirs:
            child = here / name
            if name.lower() in prune or _is_read_denied(child):
                continue
            if os.name == "nt" and _windows_system_dir(child):
                continue
            kept.append(name)
        dirs[:] = sorted(kept, key=str.lower)
        scanned += len(dirs) + len(files)
        yield here, dirs, files
        if time.monotonic() > deadline or scanned > _WALK_ENTRY_CAP:
            return


def _glob_sync(pattern: str, path: str, limit: int) -> ToolResult:
    pattern = str(pattern or "").strip()
    if not pattern:
        return ToolResult(ok=False, error="A pattern is required, e.g. '*.pdf' or 'src/**/*.py'")
    p, err = _validate_path(path or "~")
    if err:
        return ToolResult(ok=False, error=err)
    denied = _is_read_denied(p)
    if denied:
        logger.warning("Blocked search under sensitive path: %s", p)
        return ToolResult(ok=False, error=denied)
    if not p.exists() or not p.is_dir():
        return ToolResult(
            ok=False,
            error="Not a valid directory: %s. Retry with '~/Downloads' "
            "instead of a hardcoded user path (~ expands to the real "
            "home directory)." % p,
        )
    limit = _DEFAULT_GLOB_RESULTS if limit <= 0 else min(int(limit), _MAX_GLOB_RESULTS)
    norm = pattern.replace("\\", "/")
    by_name = "/" not in norm.strip("/") and "**" not in norm
    regex = _glob_regex(norm)
    deadline = time.monotonic() + _GLOB_TIME_BUDGET_S
    started = time.monotonic()
    hits: list[tuple[float, Path, bool]] = []
    walk_limit_hit = False
    mentioned = [part for part in norm.lower().split("/") if part in _WALK_PRUNE_DIRS]
    for here, dirs, files in _walk(p, deadline, mentioned):
        rel_dir = here.relative_to(p).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        for name, is_dir in [(d, True) for d in dirs] + [(f, False) for f in files]:
            target = rel_dir + name
            if not regex.match(name if by_name else target):
                continue
            child = here / name
            if _is_read_denied(child):
                continue
            try:
                mtime = child.stat().st_mtime
            except OSError:
                mtime = 0.0
            hits.append((mtime, child, is_dir))
    if time.monotonic() > deadline:
        walk_limit_hit = True
    hits.sort(key=lambda item: item[0], reverse=True)
    shown = hits[:limit]
    matches = [
        {"path": str(child), "type": "dir" if is_dir else "file", "modified": int(mtime)}
        for mtime, child, is_dir in shown
    ]
    data: dict[str, object] = {
        "pattern": pattern,
        "search_root": str(p),
        "matches": matches,
        "count": len(matches),
        "total_found": len(hits),
        "truncated": len(hits) > len(shown) or walk_limit_hit,
        "capped": len(hits) > len(shown),
        "seconds": round(time.monotonic() - started, 2),
    }
    if walk_limit_hit:
        data["note"] = "Stopped after %.0fs; search a narrower folder for complete results." % _GLOB_TIME_BUDGET_S
    return ToolResult(ok=True, data=data)


async def glob_files(pattern: str, path: str = "~", limit: int = 0) -> ToolResult:
    """Find files by glob, newest first. A bare pattern ('*.pdf') matches at any depth."""
    return await asyncio.to_thread(_glob_sync, pattern, path, limit)


async def search_files(pattern: str, path: str = "~") -> ToolResult:
    """Legacy name for :func:`glob_files`."""
    return await glob_files(pattern, path)


def _grep_sync(pattern: str, path: str, include: str, context: int, offset: int, limit: int) -> ToolResult:
    if not str(pattern or ""):
        return ToolResult(ok=False, error="A regex pattern is required for grep")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return ToolResult(ok=False, error="Invalid regex %r: %s" % (pattern, exc))
    p, err = _validate_path(path or "~")
    if err:
        return ToolResult(ok=False, error=err)
    denied = _is_read_denied(p)
    if denied:
        return ToolResult(ok=False, error=denied)
    if not p.exists():
        return ToolResult(ok=False, error="Path does not exist: %s" % p)
    context = max(0, min(int(context or 0), _MAX_GREP_CONTEXT))
    limit = _DEFAULT_GREP_MATCHES if limit <= 0 else min(int(limit), _MAX_GREP_MATCHES)
    offset = max(0, int(offset or 0))
    include_regex = _glob_regex(include.replace("\\", "/")) if include else None
    include_by_name = bool(include) and "/" not in include.replace("\\", "/")
    deadline = time.monotonic() + _GREP_TIME_BUDGET_S

    def candidates() -> Iterable[Path]:
        if p.is_file():
            yield p
            return
        for here, _dirs, files in _walk(p, deadline):
            for name in sorted(files, key=str.lower):
                child = here / name
                if include_regex is not None:
                    target = name if include_by_name else child.relative_to(p).as_posix()
                    if not include_regex.match(target):
                        continue
                yield child

    matches: list[dict[str, object]] = []
    files_with_matches: list[str] = []
    seen = 0
    files_scanned = 0
    used = 0
    budget_hit = False
    more = False
    for child in candidates():
        if time.monotonic() > deadline or files_scanned >= _GREP_FILE_CAP:
            more = True
            break
        if _is_read_denied(child):
            continue
        try:
            if child.stat().st_size > _MAX_GREP_FILE_BYTES:
                continue
            raw = child.read_bytes()
        except OSError:
            continue
        files_scanned += 1
        text, _fmt, _decode_err = _decode_text(raw, strict=False)
        if text is None:
            continue
        lines = [line.rstrip("\r") for line in text.split("\n")]
        if lines and lines[-1] == "":
            lines.pop()
        file_hit = False
        for index, line in enumerate(lines):
            if not regex.search(line):
                continue
            file_hit = True
            seen += 1
            if seen <= offset:
                continue
            if len(matches) >= limit or budget_hit:
                more = True
                continue
            entry: dict[str, object] = {"path": str(child), "line": index + 1, "text": line[:_GREP_LINE_CHARS]}
            if context:
                lo = max(0, index - context)
                hi = min(len(lines), index + context + 1)
                entry["before"] = [s[:_GREP_LINE_CHARS] for s in lines[lo:index]]
                entry["after"] = [s[:_GREP_LINE_CHARS] for s in lines[index + 1 : hi]]
            cost = _json_cost(json.dumps(entry, ensure_ascii=False))
            if matches and used + cost > _GREP_CHAR_BUDGET:
                budget_hit = True
                more = True
                continue
            used += cost
            matches.append(entry)
        if file_hit:
            files_with_matches.append(str(child))
    data: dict[str, object] = {
        "pattern": pattern,
        "search_root": str(p),
        "matches": matches,
        "count": len(matches),
        "files_matched": len(files_with_matches),
        "files_scanned": files_scanned,
        "truncated": more,
    }
    if more:
        data["next_offset"] = offset + len(matches)
    return ToolResult(ok=True, data=data)


async def grep_files(
    pattern: str,
    path: str = "~",
    include: str = "",
    context: int = 0,
    offset: int = 0,
    limit: int = 0,
) -> ToolResult:
    """Search file contents with a regular expression (``(?i)`` ignores case)."""
    return await asyncio.to_thread(_grep_sync, pattern, path, include, context, offset, limit)


async def list_changes(*, scope: FileScope | None = None) -> ToolResult:
    """This conversation's file changes, newest first, with what undo would do."""
    state = _state_for(scope)
    with state.lock:
        rows = []
        for change in reversed(state.changes[-50:]):
            rows.append(
                {
                    "id": change.id,
                    "action": change.action,
                    "path": change.path,
                    "destination": change.destination,
                    "when": time.strftime("%H:%M:%S", time.localtime(change.ts)),
                    "undone": change.undone,
                    "undoable": not change.undone
                    and change.action not in {"undo", "redo"}
                    and all(e.get("before", {}).get("kind") != "trash" for e in change.entries),
                }
            )
    return ToolResult(ok=True, data={"changes": rows, "count": len(rows)})


# ---------------------------------------------------------------------------
# Changes. Every one is backed up first and recorded for undo.
# ---------------------------------------------------------------------------


def _target_for_write(path: str) -> tuple[Path, str | None]:
    p, err = _validate_path(path)
    if err:
        return p, err
    denied = _is_write_denied(p)
    if denied:
        logger.warning("Blocked write to protected path: %s", p)
        return p, denied
    return p, None


def _fail(message: str, category: str | None = None, **data: object) -> ToolResult:
    return ToolResult(ok=False, error=message, error_category=category, data=data or None)


def _load_text_for_edit(p: Path) -> tuple[bytes | None, str | None, _TextFormat | None, str | None]:
    if not p.is_file():
        return None, None, None, "Not a file: %s" % p
    try:
        size = p.stat().st_size
        if size > _MAX_EDIT_FILE_BYTES:
            return (
                None,
                None,
                None,
                "File is too large to edit in place (%d bytes; limit %d): %s" % (size, _MAX_EDIT_FILE_BYTES, p),
            )
        raw = p.read_bytes()
    except PermissionError:
        return None, None, None, "Permission denied: %s" % p
    except OSError as exc:
        return None, None, None, "Read error: %s" % exc
    text, fmt, err = _decode_text(raw, strict=True)
    if err == "binary":
        return None, None, None, "Binary file detected, cannot edit as text: %s" % p
    if err:
        return None, None, None, "%s: %s" % (err, p)
    return raw, text, fmt, None


def _freshness_error(state: _SessionState, p: Path, *, verb: str) -> ToolResult | None:
    """Read-before-overwrite: the file must have been read and be unchanged since."""
    record = state.reads.get(_path_key(p))
    if record is None:
        return _fail(
            "Not %s: %s already exists and has not been read in this conversation. Read it first "
            "(file_read action='read'), then try again -- or use action='edit' to change part of it." % (verb, p),
            "read_required",
            path=str(p),
        )
    try:
        st = p.stat()
    except OSError as exc:
        return _fail("Cannot check %s: %s" % (p, exc))
    if st.st_mtime_ns == record.mtime_ns and st.st_size == record.size:
        return None
    if record.sha256 is not None and st.st_size <= _MAX_EDIT_FILE_BYTES and _file_sha256(p) == record.sha256:
        return None
    return _fail(
        "Not %s: %s changed after it was read (by the user or another program). Read it again "
        "first so nothing they changed is lost." % (verb, p),
        "changed_since_read",
        path=str(p),
    )


def _commit_bytes(
    state: _SessionState,
    p: Path,
    new_raw: bytes,
    action: str,
    existed: bool,
    data: dict[str, object],
) -> ToolResult:
    """Back up ``p``, write ``new_raw``, record the change for undo."""
    change = state.new_change(action, p)
    before: dict[str, Any]
    if existed:
        try:
            backup = _backup_into(state, change, p)
        except OSError as exc:
            return _fail("Could not back up %s before changing it, so nothing was changed: %s" % (p, exc))
        if backup is None:
            return _fail(
                "%s is too large to back up (limit %d bytes), so it was not changed." % (p, _MAX_BACKUP_FILE_BYTES)
            )
        before = backup
    else:
        before = {"kind": "absent"}
    change.entries.append({"path": str(p), "before": before})
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as handle:
            handle.write(new_raw)
    except PermissionError:
        return _fail("Permission denied: %s" % p)
    except OSError as exc:
        return _fail("Write error: %s" % exc)
    state.record(change)
    state.note_read(p, new_raw, True)
    data.setdefault("path", str(p))
    data["bytes_written"] = len(new_raw)
    data["change_id"] = change.id
    data["undoable"] = True
    return ToolResult(ok=True, data=data)


def _write_file_sync(path: str, content: str, scope: FileScope | None) -> ToolResult:
    p, err = _target_for_write(path)
    if err:
        return _fail(err)
    if p.is_dir():
        return _fail("%s is a folder, not a file" % p)
    state = _state_for(scope)
    with state.lock:
        existed = p.exists()
        if existed:
            stale = _freshness_error(state, p, verb="written")
            if stale is not None:
                return stale
            try:
                with open(p, "rb") as handle:
                    head = handle.read(65536)
            except OSError as exc:
                return _fail("Cannot read %s: %s" % (p, exc))
            _head_text, fmt, _head_err = _decode_text(head, strict=False)
            text = content
            if fmt is not None and fmt.newline == "\r\n" and "\r\n" not in content:
                # Keep a Windows file's CRLF line endings.
                text = _to_newline(content, "\r\n")
            if fmt is not None:
                new_raw, enc_err = _encode_text(text, fmt)
                if enc_err or new_raw is None:
                    return _fail(enc_err or "Cannot encode the new text")
            else:
                new_raw = text.encode("utf-8")
        else:
            text = content
            new_raw = content.encode("utf-8")
        return _commit_bytes(
            state,
            p,
            new_raw,
            "write",
            existed,
            {"action": "write", "created": not existed, "total_lines": _count_lines(text)},
        )


async def write_file(path: str, content: str, *, scope: FileScope | None = None) -> ToolResult:
    """Create a file or replace a whole file.

    Replacing an existing file requires that it was read in this conversation
    and has not changed since. Encoding, BOM and CRLF line endings of an
    existing file are kept. The previous version is backed up for undo.
    """
    return await asyncio.to_thread(_write_file_sync, path, content, scope)


def _line_numbers_of(text: str, needle: str, cap: int = 10) -> list[int]:
    found: list[int] = []
    start = 0
    while len(found) < cap:
        index = text.find(needle, start)
        if index < 0:
            break
        found.append(text.count("\n", 0, index) + 1)
        start = index + max(1, len(needle))
    return found


def _apply_one_edit(
    text: str, fmt: _TextFormat, old: str, new: str, replace_all: bool
) -> tuple[str | None, int, str | None]:
    if old == new:
        return None, 0, "old_string and new_string are identical; nothing to change"
    if not old:
        return None, 0, "old_string is empty; give the exact text to replace (use action='append' to add at the end)"
    new = _to_newline(new, fmt.newline) if ("\n" in new) else new
    count = text.count(old)
    if count == 0:
        alt = _to_newline(old, fmt.newline)
        if alt != old:
            count = text.count(alt)
            if count:
                old = alt
    if count == 0:
        hint = ""
        stripped = old.strip()
        if stripped and stripped != old and stripped in text:
            hint = " The text without its leading/trailing whitespace appears at line(s) %s; check the indentation." % (
                ", ".join(str(n) for n in _line_numbers_of(text, stripped, 5))
            )
        elif "\t" in old or re.match(r"^\s*\d+\t", old):
            hint = " Do not include the line-number prefix from a read in old_string."
        return None, 0, "old_string was not found.%s" % hint
    if count > 1 and not replace_all:
        lines = _line_numbers_of(text, old)
        return (
            None,
            count,
            "old_string matches %d places (lines %s). Add surrounding text so it matches exactly one, "
            "or set replace_all=true to change every one." % (count, ", ".join(str(n) for n in lines)),
        )
    return text.replace(old, new) if replace_all else text.replace(old, new, 1), count if replace_all else 1, None


def _edit_sync(path: str, edits: list[dict[str, Any]], scope: FileScope | None) -> ToolResult:
    if not edits:
        return _fail("No edit given: pass old_string/new_string, or edits=[...]")
    p, err = _target_for_write(path)
    if err:
        return _fail(err)
    state = _state_for(scope)
    with state.lock:
        if not p.exists():
            first = edits[0]
            if len(edits) == 1 and not str(first.get("old_string") or ""):
                content = str(first.get("new_string") or "")
                return _commit_bytes(
                    state,
                    p,
                    content.encode("utf-8"),
                    "write",
                    False,
                    {"action": "edit", "created": True, "total_lines": _count_lines(content)},
                )
            return _fail("File does not exist: %s (to create it, use action='write')" % p, "not_found")
        _raw, text, fmt, load_err = _load_text_for_edit(p)
        if load_err or text is None or fmt is None:
            return _fail(load_err or "Cannot read %s" % p)
        replacements = 0
        for index, edit in enumerate(edits, start=1):
            updated, count, edit_err = _apply_one_edit(
                text,
                fmt,
                str(edit.get("old_string") or ""),
                str(edit.get("new_string") or ""),
                bool(edit.get("replace_all")),
            )
            if edit_err or updated is None:
                prefix = "Edit %d of %d: " % (index, len(edits)) if len(edits) > 1 else ""
                return _fail(
                    "%s%s Nothing was changed in %s." % (prefix, edit_err, p),
                    "edit_ambiguous" if count > 1 else "edit_not_found",
                    path=str(p),
                    edit_index=index,
                    matches=count,
                )
            text = updated
            replacements += count
        new_raw, enc_err = _encode_text(text, fmt)
        if enc_err or new_raw is None:
            return _fail(enc_err or "Cannot encode the new text")
        return _commit_bytes(
            state,
            p,
            new_raw,
            "edit",
            True,
            {
                "action": "edit",
                "replacements": replacements,
                "edits_applied": len(edits),
                "total_lines": _count_lines(text),
                "encoding": fmt.label,
                "line_endings": "CRLF" if fmt.newline == "\r\n" else "LF",
            },
        )


async def edit_file(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    *,
    scope: FileScope | None = None,
) -> ToolResult:
    """Replace the exact text ``old_string`` with ``new_string``.

    ``old_string`` must match exactly once unless ``replace_all``. An empty
    ``old_string`` on a missing file creates it with ``new_string``.
    """
    edit = {"old_string": old_string, "new_string": new_string, "replace_all": replace_all}
    return await asyncio.to_thread(_edit_sync, path, [edit], scope)


async def multi_edit_file(path: str, edits: list[dict[str, Any]], *, scope: FileScope | None = None) -> ToolResult:
    """Apply several exact-string edits in order; all succeed or none are written."""
    return await asyncio.to_thread(_edit_sync, path, list(edits or []), scope)


async def replace_text(path: str, old_text: str, new_text: str, *, scope: FileScope | None = None) -> ToolResult:
    """Legacy name: replace the single exact occurrence of ``old_text``."""
    return await edit_file(path, old_text, new_text, False, scope=scope)


def _as_lines(content: str, newline: str) -> list[str]:
    """Split inserted content into lines that each end with ``newline``."""
    if content == "":
        return []
    return [part + newline for part in content.splitlines()]


def _line_edit_sync(
    path: str,
    mode: str,
    content: str,
    start_line: int,
    end_line: int,
    scope: FileScope | None,
) -> ToolResult:
    p, err = _target_for_write(path)
    if err:
        return _fail(err)
    state = _state_for(scope)
    with state.lock:
        if mode == "append" and not p.exists():
            if content == "":
                return _fail("Nothing to append: content is empty")
            text = "".join(_as_lines(content, "\n"))
            return _commit_bytes(
                state,
                p,
                text.encode("utf-8"),
                "append",
                False,
                {
                    "action": "append",
                    "created": True,
                    "lines_added": _count_lines(text),
                    "total_lines": _count_lines(text),
                },
            )
        if not p.exists():
            return _fail("File does not exist: %s" % p, "not_found")
        _raw, text, fmt, load_err = _load_text_for_edit(p)
        if load_err or text is None or fmt is None:
            return _fail(load_err or "Cannot read %s" % p)
        newline = fmt.newline
        data: dict[str, object] = {"action": mode}
        if mode == "append":
            if content == "":
                return _fail("Nothing to append: content is empty")
            existing = text
            if existing and not existing.endswith(("\n", "\r")):
                existing += newline
            added = "".join(_as_lines(content, newline))
            text = existing + added
            data["lines_added"] = _count_lines(added)
        else:
            # Line numbers come from a read, so the file must not have moved under them.
            stale = _freshness_error(state, p, verb="changed")
            if stale is not None:
                return stale
            lines = _split_keepends(text)
            total = len(lines)
            if mode == "insert":
                if content == "":
                    return _fail("Nothing to insert: content is empty")
                if start_line < 1 or start_line > total + 1:
                    return _fail(
                        "Line %d is out of range: the file has %d lines (use 1 to %d)" % (start_line, total, total + 1)
                    )
                if lines and start_line == total + 1 and not lines[-1].endswith(("\n", "\r")):
                    lines[-1] += newline
                new_lines = _as_lines(content, newline)
                lines[start_line - 1 : start_line - 1] = new_lines
                data.update({"inserted_at_line": start_line, "lines_added": len(new_lines)})
            else:
                if end_line <= 0:
                    end_line = start_line
                if start_line < 1 or end_line < start_line or end_line > total:
                    return _fail("Lines %d-%d are out of range: the file has %d lines" % (start_line, end_line, total))
                new_lines = _as_lines(content, newline)
                if end_line == total and new_lines and not lines[-1].endswith(("\n", "\r")):
                    new_lines[-1] = new_lines[-1][: -len(newline)]
                lines[start_line - 1 : end_line] = new_lines
                data.update(
                    {
                        "start_line": start_line,
                        "end_line": end_line,
                        "lines_removed": end_line - start_line + 1,
                        "lines_added": len(new_lines),
                    }
                )
            text = "".join(lines)
        new_raw, enc_err = _encode_text(text, fmt)
        if enc_err or new_raw is None:
            return _fail(enc_err or "Cannot encode the new text")
        data["total_lines"] = _count_lines(text)
        return _commit_bytes(state, p, new_raw, mode, True, data)


async def append_to_file(path: str, content: str, *, scope: FileScope | None = None) -> ToolResult:
    """Append ``content`` to the end of a file, creating it if missing."""
    return await asyncio.to_thread(_line_edit_sync, path, "append", content, 0, 0, scope)


async def insert_lines(path: str, line: int, content: str, *, scope: FileScope | None = None) -> ToolResult:
    """Insert ``content`` before 1-based line ``line`` (``total + 1`` appends)."""
    return await asyncio.to_thread(_line_edit_sync, path, "insert", content, int(line), 0, scope)


async def replace_lines(
    path: str,
    start_line: int,
    end_line: int,
    content: str,
    *,
    scope: FileScope | None = None,
) -> ToolResult:
    """Replace 1-based inclusive lines ``start_line..end_line`` (empty content deletes them)."""
    return await asyncio.to_thread(
        _line_edit_sync, path, "replace_lines", content, int(start_line), int(end_line), scope
    )


def _make_directory_sync(path: str, scope: FileScope | None) -> ToolResult:
    p, err = _target_for_write(path)
    if err:
        return _fail(err)
    if p.exists():
        if p.is_dir():
            return ToolResult(ok=True, data={"path": str(p), "created": False, "note": "The folder already exists."})
        return _fail("%s exists and is a file" % p)
    state = _state_for(scope)
    with state.lock:
        # Record the top-most missing folder so undo removes everything mkdir made.
        top = p
        while not top.parent.exists() and top.parent != top:
            top = top.parent
        change = state.new_change("mkdir", top)
        change.entries.append({"path": str(top), "before": {"kind": "absent"}})
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _fail("Could not create %s: %s" % (p, exc))
        state.record(change)
        state.created_dirs.add(_path_key(top))
    return ToolResult(ok=True, data={"path": str(p), "created": True, "change_id": change.id})


async def make_directory(path: str, *, scope: FileScope | None = None) -> ToolResult:
    """Create a folder (and any missing parents)."""
    return await asyncio.to_thread(_make_directory_sync, path, scope)


def _resolve_destination(src: Path, destination: str) -> tuple[Path, str | None]:
    dst, err = _target_for_write(destination)
    if err:
        return dst, err
    if dst.is_dir():
        dst = dst / src.name
        denied = _is_write_denied(dst)
        if denied:
            return dst, denied
    if _path_key(dst) == _path_key(src):
        return dst, "Source and destination are the same: %s" % src
    if src.is_dir() and _path_key(dst).startswith(_path_key(src).rstrip("\\/") + os.sep):
        return dst, "Cannot put a folder inside itself: %s -> %s" % (src, dst)
    return dst, None


def _transfer_sync(mode: str, path: str, destination: str, scope: FileScope | None) -> ToolResult:
    if not str(destination or "").strip():
        return _fail("A destination is required for %s" % mode)
    if mode == "move":
        src, err = _target_for_write(path)
    else:
        src, err = _validate_path(path)
        err = err or _is_read_denied(src)
    if err:
        return _fail(err)
    if not os.path.lexists(src):
        return _fail("Path does not exist: %s" % src, "not_found")
    dst, dst_err = _resolve_destination(src, destination)
    if dst_err:
        return _fail(dst_err)
    if dst.exists() and (dst.is_dir() or src.is_dir()):
        return _fail("%s already exists; pick a new name or move it out of the way first" % dst)
    state = _state_for(scope)
    with state.lock:
        change = state.new_change(mode, src, dst)
        try:
            if mode == "move":
                change.entries.append({"path": str(src), "before": {"kind": "moved", "ref": str(dst)}})
            if dst.exists():
                backup = _backup_into(state, change, dst)
                if backup is None:
                    return _fail("%s is too large to back up before replacing it, so nothing was changed" % dst)
                change.entries.append({"path": str(dst), "before": backup})
            else:
                change.entries.append({"path": str(dst), "before": {"kind": "absent"}})
            dst.parent.mkdir(parents=True, exist_ok=True)
            if mode == "move":
                if dst.exists():
                    _remove_path(dst)
                shutil.move(str(src), str(dst))
            elif src.is_dir():
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)
        except PermissionError:
            return _fail("Permission denied: %s -> %s" % (src, dst))
        except OSError as exc:
            return _fail("%s failed: %s" % (mode.capitalize(), exc))
        state.record(change)
        if dst.is_file():
            state.note_read(dst, None, True)
    return ToolResult(
        ok=True,
        data={"action": mode, "path": str(src), "destination": str(dst), "change_id": change.id},
    )


async def move_path(path: str, destination: str, *, scope: FileScope | None = None) -> ToolResult:
    """Move or rename a file or folder. An existing folder destination receives it."""
    return await asyncio.to_thread(_transfer_sync, "move", path, destination, scope)


async def copy_path(path: str, destination: str, *, scope: FileScope | None = None) -> ToolResult:
    """Copy a file or folder."""
    return await asyncio.to_thread(_transfer_sync, "copy", path, destination, scope)


def _delete_sync(path: str, recursive: bool, scope: FileScope | None) -> ToolResult:
    p, err = _target_for_write(path)
    if err:
        return _fail(err)
    if not os.path.lexists(p):
        return _fail("File does not exist: %s" % p, "not_found")
    is_dir = p.is_dir()
    if is_dir and not recursive:
        return _fail("Cannot delete directories, only files: %s" % p)
    state = _state_for(scope)
    with state.lock:
        change = state.new_change("delete", p)
        try:
            backup = _backup_into(state, change, p)
        except OSError as exc:
            logger.warning("Backup before delete failed for %s: %s", p, exc)
            backup = None
        trashed = _send_to_trash(p)
        if not trashed:
            if backup is None:
                return _fail(
                    "Not deleted: %s could not go to the Recycle Bin and is too large to back up, so deleting "
                    "it could not be undone." % p
                )
            try:
                _remove_path(p)
            except PermissionError:
                return _fail("Permission denied: %s" % p)
            except OSError as exc:
                return _fail("Delete error: %s" % exc)
        change.entries.append({"path": str(p), "before": backup or {"kind": "trash"}})
        state.record(change)
        state.reads.pop(_path_key(p), None)
    return ToolResult(
        ok=True,
        data={
            "deleted": str(p),
            "type": "dir" if is_dir else "file",
            "recycle_bin": trashed,
            "undo_available": backup is not None,
            "change_id": change.id,
        },
    )


async def delete_path(path: str, *, recursive: bool = True, scope: FileScope | None = None) -> ToolResult:
    """Delete a file or folder: Recycle Bin / Trash plus a backup for undo."""
    return await asyncio.to_thread(_delete_sync, path, recursive, scope)


async def delete_file(path: str, *, scope: FileScope | None = None) -> ToolResult:
    """Delete a single file (not directories). Legacy entry point."""
    return await asyncio.to_thread(_delete_sync, path, False, scope)


def _restore_entry(state: _SessionState, undo: _Change, path: Path, before: dict[str, Any], recorded: set[str]) -> None:
    """Put ``path`` back to ``before``, first stashing what is there now into history."""
    kind = before.get("kind")
    key = _path_key(path)
    if kind == "moved":
        source = Path(str(before.get("ref")))
        current = _stash_current(state, undo, path)
        if key not in recorded:
            undo.entries.append({"path": str(source), "before": {"kind": "moved", "ref": str(path)}})
            undo.entries.append({"path": str(path), "before": current})
            recorded.update({key, _path_key(source)})
        if not os.path.lexists(source):
            raise FileNotFoundError("%s is no longer at %s" % (path.name, source))
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(path))
        return
    current = _stash_current(state, undo, path)
    if key not in recorded:
        undo.entries.append({"path": str(path), "before": current})
        recorded.add(key)
    if kind == "backup":
        blob = Path(str(before.get("ref")))
        path.parent.mkdir(parents=True, exist_ok=True)
        if blob.is_dir():
            shutil.copytree(blob, path, symlinks=True)
        else:
            shutil.copy2(blob, path)
    elif kind == "trash":
        raise FileNotFoundError("%s is only in the Recycle Bin; restore it from there" % path)


def _undo_sync(path: str, redo: bool, scope: FileScope | None) -> ToolResult:
    state = _state_for(scope)
    target_key = None
    if str(path or "").strip():
        resolved, _err = _validate_path(path)
        target_key = _path_key(resolved)
    with state.lock:
        candidates = [
            change
            for change in reversed(state.changes)
            if not change.undone and ((change.action == "undo") if redo else (change.action not in {"undo", "redo"}))
        ]
        if target_key is not None:
            candidates = [
                change
                for change in candidates
                if any(_path_key(entry.get("path") or "") == target_key for entry in change.entries)
            ]
        if not candidates:
            what = "to redo" if redo else "to undo"
            where = " for %s" % path if target_key else ""
            return _fail("Nothing %s%s in this conversation." % (what, where), "nothing_to_undo")
        change = candidates[0]
        if any(entry.get("before", {}).get("kind") == "trash" for entry in change.entries):
            return _fail(
                "%s was too large to back up; it is in the Recycle Bin, so restore it from there." % change.path,
                "restore_from_recycle_bin",
            )
        undo = state.new_change("redo" if redo else "undo", Path(change.path))
        undo.undo_of = change.id
        recorded: set[str] = set()
        try:
            for entry in change.entries:
                _restore_entry(state, undo, Path(str(entry.get("path"))), dict(entry.get("before") or {}), recorded)
        except (OSError, shutil.Error) as exc:
            # Keep whatever was stashed so far recorded: nothing is lost.
            undo.action = "undo_partial"
            state.record(undo)
            return _fail("Undo did not finish: %s. What was there is saved in the change history." % exc)
        change.undone = True
        if redo:
            undo.action = "redo"
            original = next((c for c in state.changes if c.id == change.undo_of), None)
            if original is not None:
                original.undone = False
            undo.undone = True  # a redo is not itself redone
        state.record(undo)
        for entry in change.entries:
            restored = Path(str(entry.get("path")))
            if restored.is_file():
                state.note_read(restored, None, True)
            else:
                state.reads.pop(_path_key(restored), None)
    verb = "Redid" if redo else "Undid"
    return ToolResult(
        ok=True,
        data={
            "action": "redo" if redo else "undo",
            "reverted": change.action if not redo else "undo",
            "path": change.path,
            "destination": change.destination,
            "change_id": change.id,
            "message": "%s %s of %s" % (verb, change.action, change.path),
        },
    )


async def undo_change(path: str = "", *, scope: FileScope | None = None) -> ToolResult:
    """Revert the latest change (to ``path`` if given) made in this conversation."""
    return await asyncio.to_thread(_undo_sync, path, False, scope)


async def redo_change(path: str = "", *, scope: FileScope | None = None) -> ToolResult:
    """Re-apply the latest undone change."""
    return await asyncio.to_thread(_undo_sync, path, True, scope)


# Re-exported for callers that matched on these helpers.
__all__ = [
    "FileScope",
    "append_to_file",
    "copy_path",
    "delete_file",
    "delete_path",
    "edit_file",
    "file_info",
    "glob_files",
    "grep_files",
    "insert_lines",
    "list_changes",
    "list_directory",
    "make_directory",
    "move_path",
    "multi_edit_file",
    "path_touched_in_session",
    "peek_session_state",
    "read_file",
    "redo_change",
    "replace_lines",
    "replace_text",
    "resolve_tool_path",
    "search_files",
    "tree_stats",
    "undo_change",
    "write_file",
]
