"""Filesystem tools for the agent.

Provides safe, sandboxed filesystem operations with path validation
and protection against accessing system directories.
"""

from __future__ import annotations

import glob
import os
import re
import stat
from pathlib import Path

from core.logging_config import get_logger
from intent.tool_types import ToolResult

logger = get_logger(__name__)

# Directories that are always blocked (case-insensitive on Windows)
_BLOCKED_DIRS_WINDOWS = {
    "windows",
    "system32",
    "syswow64",
    "program files",
    "program files (x86)",
    "programdata",
}
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

_MAX_LIST_ENTRIES = 100
_MAX_READ_LINES = 100
_MAX_SEARCH_RESULTS = 20
# Upper bound for files the partial-edit operations load whole into memory.
_MAX_EDIT_FILE_BYTES = 5 * 1024 * 1024

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


def _validate_path(path_str: str) -> tuple[Path, str | None]:
    """Validate and resolve a filesystem path.

    Returns:
        (resolved_path, error_message). error_message is None if valid.
    """
    try:
        p_raw = Path(os.path.expanduser(path_str))
    except (ValueError, OSError) as exc:
        return Path("."), "Invalid path: %s" % exc

    # Block symlinks to prevent traversal.
    # IMPORTANT: check BEFORE .resolve() — resolve() follows symlinks so
    # p.is_symlink() would always return False on the resolved path.
    if p_raw.is_symlink():
        return p_raw, "Symlinks are not allowed for security"

    try:
        p = p_raw.resolve()
    except (ValueError, OSError) as exc:
        return Path("."), "Invalid path: %s" % exc

    # Block system directories
    path_lower = str(p).lower()
    parts_lower = [part.lower() for part in p.parts]

    if os.name == "nt":
        for blocked in _BLOCKED_DIRS_WINDOWS:
            if blocked in parts_lower:
                return p, "Access to system directory '%s' is denied" % blocked
    else:
        for blocked in _BLOCKED_DIRS_UNIX:
            if path_lower.startswith(blocked) and (len(path_lower) == len(blocked) or path_lower[len(blocked)] == "/"):
                return p, "Access to system directory '%s' is denied" % blocked

    return p, None


async def list_directory(path: str = "~") -> ToolResult:
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

    try:
        entries = []
        for i, entry in enumerate(sorted(p.iterdir(), key=lambda e: e.name)):
            if i >= _MAX_LIST_ENTRIES:
                entries.append("... and more (capped at %d entries)" % _MAX_LIST_ENTRIES)
                break
            if _is_read_denied(entry):
                logger.warning("Omitted sensitive directory entry from listing: %s", entry)
                continue
            kind = "dir" if entry.is_dir() else "file"
            try:
                size = entry.stat().st_size if entry.is_file() else None
            except OSError:
                size = None
            entry_info = {"name": entry.name, "type": kind}
            if size is not None:
                entry_info["size"] = size
            entries.append(entry_info)

        # R3-P1-E (2026-05-30): retired next_step prose hint. The model
        # reads {path, entries, count} as structured data and decides
        # whether to call read_file or file_info — runtime-injected
        # instructions like "Do NOT stop here — the user asked you…" are
        # exactly the model-steering this codebase has been deleting wave
        # after wave (compass's media_tools `next_step` deletion was the
        # precedent). Guidance about when to stop / continue belongs in
        # the once-cached system prompt.
        result_data: dict[str, object] = {}
        result_data["path"] = str(p)
        result_data["entries"] = entries
        result_data["count"] = len(entries)
        return ToolResult(ok=True, data=result_data)
    except PermissionError:
        return ToolResult(ok=False, error="Permission denied: %s" % p)
    except OSError as exc:
        return ToolResult(ok=False, error="OS error: %s" % exc)


async def read_file(path: str, offset: int = 0, limit: int = _MAX_READ_LINES) -> ToolResult:
    """Read contents of a text file."""
    p, err = _validate_path(path)
    if err:
        return ToolResult(ok=False, error=err)

    denied = _is_read_denied(p)
    if denied:
        logger.warning("Blocked read of sensitive path: %s", p)
        return ToolResult(ok=False, error=denied)

    if not p.exists():
        return ToolResult(ok=False, error="File does not exist: %s" % p)
    if not p.is_file():
        return ToolResult(ok=False, error="Not a file: %s" % p)

    # Detect binary files
    try:
        with open(p, "rb") as f:
            chunk = f.read(1024)
            if b"\x00" in chunk:
                return ToolResult(ok=False, error="Binary file detected, cannot read as text: %s" % p)
    except PermissionError:
        return ToolResult(ok=False, error="Permission denied: %s" % p)

    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        total_lines = len(lines)
        # Clamp limit
        limit = min(limit, _MAX_READ_LINES)
        selected = lines[offset : offset + limit]
        content = "".join(selected)
        end = offset + len(selected)

        data: dict[str, object] = {
            "path": str(p),
            "content": content,
            "total_lines": total_lines,
            "offset": offset,
            "lines_returned": len(selected),
            # Structured truncation facts (not prose): a read never returns
            # more than _MAX_READ_LINES lines, so the model must know when it
            # has NOT seen the whole file before it edits it.
            "truncated": offset > 0 or end < total_lines,
        }
        if end < total_lines:
            data["next_offset"] = end
        return ToolResult(ok=True, data=data)
    except PermissionError:
        return ToolResult(ok=False, error="Permission denied: %s" % p)
    except OSError as exc:
        return ToolResult(ok=False, error="Read error: %s" % exc)


def _is_write_denied(path: Path) -> str | None:
    """Check if a path is on the write deny list.

    Returns a human-readable reason or None if allowed.
    """
    path_str = str(path)
    for pattern in _WRITE_DENY_PATTERNS:
        if pattern.search(path_str):
            return "Writing to this path is blocked for safety: %s" % pattern.pattern
    return None


def _count_lines(text: str) -> int:
    """Count lines the way ``read_file`` does (``readlines`` semantics)."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _existing_line_count(p: Path) -> int | None:
    """Line count of an existing regular file, or None when it cannot be read."""
    try:
        if not p.is_file():
            return None
        if p.stat().st_size > _MAX_EDIT_FILE_BYTES:
            # Too large to count cheaply; treat as "longer than one read".
            return _MAX_READ_LINES + 1
        data = p.read_bytes()
    except OSError:
        return None
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


async def write_file(path: str, content: str, overwrite_existing: bool = False) -> ToolResult:
    """Write content to a file (creates parent directories if needed).

    A whole-file write that would make an existing file SHORTER than a single
    ``read_file`` call can show (more than ``_MAX_READ_LINES`` lines) is
    refused unless ``overwrite_existing`` is True. A read returns at most
    ``_MAX_READ_LINES`` lines, so "read it, add a line, write it back" on a
    long file would otherwise silently drop everything past the part that was
    read. Partial edits go through ``append_to_file`` / ``insert_lines`` /
    ``replace_lines`` / ``replace_text`` instead, which never touch the rest.
    """
    p, err = _validate_path(path)
    if err:
        return ToolResult(ok=False, error=err)

    denied = _is_write_denied(p)
    if denied:
        logger.warning("Blocked write to protected path: %s", p)
        return ToolResult(ok=False, error=denied)

    if not overwrite_existing:
        existing_lines = _existing_line_count(p)
        new_lines = _count_lines(content)
        if existing_lines is not None and existing_lines > _MAX_READ_LINES and new_lines < existing_lines:
            logger.warning(
                "Refused whole-file write that would shrink %s from %d to %d lines",
                p,
                existing_lines,
                new_lines,
            )
            return ToolResult(
                ok=False,
                error=(
                    "Not written: this would replace all %d lines of %s with %d lines. "
                    "A read shows at most %d lines at a time, so a whole-file write can drop "
                    "content that was never read. To change part of the file, use "
                    "action='append', 'insert', 'replace_lines' or 'replace_text'. Only if the "
                    "user asked to replace the entire file, write again with overwrite=true."
                )
                % (existing_lines, p, new_lines, _MAX_READ_LINES),
                error_category="would_truncate_file",
                data={
                    "path": str(p),
                    "existing_lines": existing_lines,
                    "new_lines": new_lines,
                    "written": False,
                },
            )

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ToolResult(
            ok=True,
            data={"path": str(p), "bytes_written": len(content.encode("utf-8"))},
        )
    except PermissionError:
        return ToolResult(ok=False, error="Permission denied: %s" % p)
    except OSError as exc:
        return ToolResult(ok=False, error="Write error: %s" % exc)


# ---------------------------------------------------------------------------
# Partial edits: change part of a file without rewriting the rest from a read.
# ---------------------------------------------------------------------------


def _load_for_edit(path: str, *, must_exist: bool) -> tuple[Path, str | None, str | None]:
    """Validate a path for an in-place edit and load its full text.

    Returns ``(path, text, error)``. ``text`` is None when the file does not
    exist (allowed only when ``must_exist`` is False). The file is read whole
    (bounded by ``_MAX_EDIT_FILE_BYTES``) with newlines preserved exactly, and
    as strict UTF-8: a file that does not decode is refused rather than
    rewritten with replacement characters.
    """
    p, err = _validate_path(path)
    if err:
        return p, None, err
    denied = _is_write_denied(p)
    if denied:
        logger.warning("Blocked edit of protected path: %s", p)
        return p, None, denied
    if not p.exists():
        if must_exist:
            return p, None, "File does not exist: %s" % p
        return p, None, None
    if not p.is_file():
        return p, None, "Not a file: %s" % p
    try:
        size = p.stat().st_size
        if size > _MAX_EDIT_FILE_BYTES:
            return (
                p,
                None,
                "File is too large to edit safely (%d bytes; limit %d): %s" % (size, _MAX_EDIT_FILE_BYTES, p),
            )
        raw = p.read_bytes()
    except PermissionError:
        return p, None, "Permission denied: %s" % p
    except OSError as exc:
        return p, None, "Read error: %s" % exc
    if b"\x00" in raw[:1024]:
        return p, None, "Binary file detected, cannot edit as text: %s" % p
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return p, None, "File is not UTF-8 text, so editing it could corrupt it: %s" % p
    return p, text, None


def _newline_style(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _as_lines(content: str, newline: str) -> list[str]:
    """Split inserted content into lines that each end with ``newline``."""
    if content == "":
        return []
    parts = content.splitlines()
    return [part + newline for part in parts]


def _save_edit(p: Path, text: str, data: dict[str, object]) -> ToolResult:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # newline="" writes the text byte-for-byte (no \n -> \r\n translation),
        # so untouched lines keep their original line endings.
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    except PermissionError:
        return ToolResult(ok=False, error="Permission denied: %s" % p)
    except OSError as exc:
        return ToolResult(ok=False, error="Write error: %s" % exc)
    data.setdefault("path", str(p))
    data["total_lines"] = _count_lines(text)
    data["bytes_written"] = len(text.encode("utf-8"))
    return ToolResult(ok=True, data=data)


async def append_to_file(path: str, content: str) -> ToolResult:
    """Append ``content`` to the end of a file, creating it if missing."""
    if content == "":
        return ToolResult(ok=False, error="Nothing to append: content is empty")
    p, text, err = _load_for_edit(path, must_exist=False)
    if err:
        return ToolResult(ok=False, error=err)
    existing = text or ""
    newline = _newline_style(existing)
    if existing and not existing.endswith(("\n", "\r")):
        existing += newline
    added = "".join(_as_lines(content, newline))
    return _save_edit(p, existing + added, {"action": "append", "lines_added": _count_lines(added)})


async def insert_lines(path: str, line: int, content: str) -> ToolResult:
    """Insert ``content`` before 1-based line ``line`` (``total + 1`` appends)."""
    if content == "":
        return ToolResult(ok=False, error="Nothing to insert: content is empty")
    p, text, err = _load_for_edit(path, must_exist=True)
    if err or text is None:
        return ToolResult(ok=False, error=err or "File does not exist: %s" % p)
    lines = text.splitlines(keepends=True)
    total = len(lines)
    if line < 1 or line > total + 1:
        return ToolResult(
            ok=False,
            error="Line %d is out of range: the file has %d lines (use 1 to %d)" % (line, total, total + 1),
        )
    newline = _newline_style(text)
    if lines and line == total + 1 and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += newline
    new_lines = _as_lines(content, newline)
    lines[line - 1 : line - 1] = new_lines
    return _save_edit(
        p,
        "".join(lines),
        {"action": "insert", "inserted_at_line": line, "lines_added": len(new_lines)},
    )


async def replace_lines(path: str, start_line: int, end_line: int, content: str) -> ToolResult:
    """Replace 1-based inclusive lines ``start_line..end_line`` with ``content``.

    Empty ``content`` deletes those lines. Every other line is kept as-is.
    """
    p, text, err = _load_for_edit(path, must_exist=True)
    if err or text is None:
        return ToolResult(ok=False, error=err or "File does not exist: %s" % p)
    lines = text.splitlines(keepends=True)
    total = len(lines)
    if end_line <= 0:
        end_line = start_line
    if start_line < 1 or end_line < start_line or end_line > total:
        return ToolResult(
            ok=False,
            error="Lines %d-%d are out of range: the file has %d lines" % (start_line, end_line, total),
        )
    newline = _newline_style(text)
    new_lines = _as_lines(content, newline)
    # Keep the file's "no newline at end of file" state when the last line
    # itself is replaced.
    if end_line == total and new_lines and not lines[-1].endswith(("\n", "\r")):
        new_lines[-1] = new_lines[-1][: -len(newline)]
    lines[start_line - 1 : end_line] = new_lines
    return _save_edit(
        p,
        "".join(lines),
        {
            "action": "replace_lines",
            "start_line": start_line,
            "end_line": end_line,
            "lines_removed": end_line - start_line + 1,
            "lines_added": len(new_lines),
        },
    )


async def replace_text(path: str, old_text: str, new_text: str) -> ToolResult:
    """Replace the single exact occurrence of ``old_text`` with ``new_text``."""
    if not old_text:
        return ToolResult(ok=False, error="old_text is required for replace_text")
    p, text, err = _load_for_edit(path, must_exist=True)
    if err or text is None:
        return ToolResult(ok=False, error=err or "File does not exist: %s" % p)
    count = text.count(old_text)
    if count == 0 and "\r\n" in text and "\r\n" not in old_text:
        # The model usually quotes text with \n even when the file uses \r\n.
        old_text = old_text.replace("\n", "\r\n")
        new_text = new_text.replace("\r\n", "\n").replace("\n", "\r\n")
        count = text.count(old_text)
    if count == 0:
        return ToolResult(ok=False, error="old_text was not found in %s; nothing was changed" % p)
    if count > 1:
        return ToolResult(
            ok=False,
            error=(
                "old_text appears %d times in %s; include more surrounding text so it matches "
                "exactly once. Nothing was changed." % (count, p)
            ),
        )
    return _save_edit(p, text.replace(old_text, new_text, 1), {"action": "replace_text", "replacements": 1})


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
        info = {
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


async def search_files(pattern: str, path: str = "~") -> ToolResult:
    """Search for files matching a glob pattern."""
    p, err = _validate_path(path)
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

    try:
        matches = []
        search_pattern = os.path.join(str(p), "**", pattern)
        for match_str in glob.iglob(search_pattern, recursive=True):
            if len(matches) >= _MAX_SEARCH_RESULTS:
                break
            try:
                match = Path(match_str)
                if _is_read_denied(match):
                    logger.warning("Omitted sensitive search match: %s", match)
                    continue
                matches.append(
                    {
                        "path": str(match),
                        "type": "dir" if match.is_dir() else "file",
                    }
                )
            except (PermissionError, OSError) as exc:
                logger.warning(
                    "Skipping inaccessible search match %s under %s: %s",
                    match_str,
                    p,
                    exc,
                )
                continue

        # R3-P1-E: retired next_step prose hint (see _list_dir handler above).
        result_data: dict[str, object] = {}
        result_data["pattern"] = pattern
        result_data["search_root"] = str(p)
        result_data["matches"] = matches
        result_data["count"] = len(matches)
        result_data["capped"] = len(matches) >= _MAX_SEARCH_RESULTS
        return ToolResult(ok=True, data=result_data)
    except PermissionError:
        return ToolResult(ok=False, error="Permission denied searching: %s" % p)
    except OSError as exc:
        return ToolResult(ok=False, error="Search error: %s" % exc)


async def delete_file(path: str) -> ToolResult:
    """Delete a single file (not directories)."""
    p, err = _validate_path(path)
    if err:
        return ToolResult(ok=False, error=err)

    denied = _is_write_denied(p)
    if denied:
        logger.warning("Blocked delete of protected path: %s", p)
        return ToolResult(ok=False, error=denied)

    if not p.exists():
        return ToolResult(ok=False, error="File does not exist: %s" % p)
    if p.is_dir():
        return ToolResult(ok=False, error="Cannot delete directories, only files: %s" % p)

    try:
        p.unlink()
        return ToolResult(ok=True, data={"deleted": str(p)})
    except PermissionError:
        return ToolResult(ok=False, error="Permission denied: %s" % p)
    except OSError as exc:
        return ToolResult(ok=False, error="Delete error: %s" % exc)
