"""Targeted confirmation gates for the agent's file tools (founder rule 2026-10-24).

"We are not trying to arbitrarily limit her in any way. We are giving her
capabilities and liberty with targeted gates for above-average consequential
actions, then trusting the model from there."

Every file change is backed up and undoable (``intent.tools.filesystem``), so
the gate asks the user only where a mistake would be above-average costly or
the target is something the user never pointed at:

Never asks
  * reading, glob, grep, listing, history;
  * creating a new file or folder, copying to a new name;
  * edits and whole-file writes to a file the user referred to in this
    conversation (by name, by path, or by naming a folder it is in), or that
    Viola itself created or changed earlier in the conversation;
  * moving/renaming such a file;
  * undo / redo.

Asks first
  * deletes (files go to the Recycle Bin and are backed up, but the user should
    still say yes);
  * edits, overwrites and moves of files the user did not refer to, and a move
    or copy that would replace an existing file the user did not refer to;
  * moving or copying a folder with more than ``BULK_FILE_THRESHOLD`` files;
  * writing anywhere that runs code on its own: Startup folders, autorun.inf,
    .reg files, PowerShell profiles, LaunchAgents/systemd user units, and
    program files (.exe, .dll, .lnk, ...).

Untrusted content narrows this: while the conversation is tainted (web page,
email, a file the user did not name ...) the only changes that go ahead are
edits and writes to a file the user named in their current message (injected
text cannot put words in the user's message); every other change goes to the
confirm-or-cancel gate, as ``intent.content_sanitizer`` requires.

The decision needs to know who is asking and what the user said, so the agent
loop binds a :class:`FileGateContext` for the run. Without one (any other
caller) every change asks -- the gate fails closed.
"""

from __future__ import annotations

import contextvars
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BULK_FILE_THRESHOLD = 50

# Action classes the gate can return (all one-shot in the agent executor).
FILE_DELETE = "file_delete"
FILE_WRITE = "file_write"
FILE_MOVE = "file_move"
FILE_BULK = "file_bulk"
FILE_AUTOSTART = "file_autostart"
FILE_EXECUTABLE = "file_executable"
FILE_ACTION_CLASSES = frozenset({FILE_DELETE, FILE_WRITE, FILE_MOVE, FILE_BULK, FILE_AUTOSTART, FILE_EXECUTABLE})

_NEVER_ASK_ACTIONS = frozenset({"undo", "redo", "mkdir"})
_EDIT_ACTIONS = frozenset({"edit", "multi_edit", "append", "insert", "replace_lines", "replace_text"})
_WRITE_ACTIONS = frozenset({"write", "save", "create", "overwrite"})
_DELETE_ACTIONS = frozenset({"delete", "forget", "trash", "remove"})
_MOVE_ACTIONS = frozenset({"move", "rename"})
_COPY_ACTIONS = frozenset({"copy"})

# Places where a file runs code by itself (sign-in, shell start, double-click
# import). Shell rc files and XDG autostart entries are already refused
# outright by the filesystem write deny list; these ask instead.
_AUTOSTART_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"[\\/]start menu[\\/]programs[\\/]startup(?:[\\/]|$)",
        r"[\\/]autorun\.inf$",
        r"\.reg$",
        r"[\\/](?:windows)?powershell[\\/][^\\/]*profile\.ps1$",
        r"[\\/]library[\\/](?:launchagents|launchdaemons|startupitems)(?:[\\/]|$)",
        r"[\\/]\.config[\\/](?:autostart|systemd[\\/]user)(?:[\\/]|$)",
        r"[\\/]\.(?:zprofile|zlogin|bash_login|login|xprofile|xinitrc|xsessionrc)$",
        r"[\\/]crontab$",
    )
]
_EXECUTABLE_RE = re.compile(r"\.(?:exe|dll|scr|com|msi|msp|sys|cpl|ocx|pif|lnk|hta|jar)$", re.IGNORECASE)
_WELL_KNOWN_USER_FOLDERS = frozenset({"desktop", "documents", "downloads", "pictures", "music", "videos", "onedrive"})


@dataclass(frozen=True)
class FileGateContext:
    """Who is acting and what the user has said in this conversation."""

    user_id: str
    session_key: str
    user_text: str


_ACTIVE: contextvars.ContextVar[FileGateContext | None] = contextvars.ContextVar(
    "viola_file_gate_context", default=None
)


def bind_file_gate_context(context: FileGateContext | None) -> contextvars.Token[FileGateContext | None]:
    return _ACTIVE.set(context)


def reset_file_gate_context(token: contextvars.Token[FileGateContext | None]) -> None:
    try:
        _ACTIVE.reset(token)
    except ValueError:
        _ACTIVE.set(None)


def active_file_gate_context() -> FileGateContext | None:
    return _ACTIVE.get()


def user_texts_from_messages(messages: Any, current: str | None = None) -> str:
    """Join the user's own words from a provider message chain (text blocks only)."""
    from intent.content_sanitizer import _user_request_text

    texts: list[str] = []
    for message in messages or []:
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        text = _user_request_text(message)
        if text:
            texts.append(text)
    if current:
        texts.append(str(current))
    return "\n".join(texts)


# ---------------------------------------------------------------------------
# Path facts.
# ---------------------------------------------------------------------------


def _norm(value: str) -> str:
    return value.replace("\\", "/").lower()


def _contains_path(text: str, form: str, *, whole: bool = False) -> bool:
    """``form`` appears in ``text`` as a whole path (not inside a longer name).

    With ``whole`` the match must also end there: ``j:/code/blog`` does not count
    inside ``j:/code/blog/index.md`` (that names a file, not the folder).
    """
    if not form:
        return False
    for match in re.finditer(re.escape(form), text):
        before = text[match.start() - 1] if match.start() else " "
        if before.isalnum() or before in "_-":
            continue
        after = text[match.end() : match.end() + 2]
        nxt = after[:1]
        if whole and nxt == "/" and len(after) > 1 and (after[1].isalnum() or after[1] in "._-~"):
            continue
        if not nxt or nxt.isspace() or nxt in "/\"'`,;:)!?]":
            return True
        if nxt == "." and (len(after) < 2 or not after[1].isalnum()):
            return True
    return False


def _name_mentioned(name: str, text: str) -> bool:
    """A file name with an extension, as a whole name (same rule as the taint exemption)."""
    stem, dot, extension = name.rpartition(".")
    if not dot or not stem.strip(".") or not extension:
        return False
    pattern = r"(?<![\w.\-])" + re.escape(name) + r"(?![\w\-]|\.\w)"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _home() -> Path | None:
    try:
        return Path.home().resolve()
    except (OSError, RuntimeError):
        return None


def _is_home_or_above(folder: Path, home: Path | None) -> bool:
    """True for the home folder itself and every folder that contains it."""
    if home is None:
        return False
    key = _norm(str(folder)).rstrip("/")
    home_key = _norm(str(home)).rstrip("/")
    return home_key == key or home_key.startswith(key + "/")


def _ancestors_below_home(p: Path) -> list[Path]:
    """Folders above ``p`` up to (not including) home, or up to the drive root outside home.

    Home and the folders that contain it (/home, C:/Users) are never part
    of the walk: with home at /home/runner, "list my home folder" named
    /home as "home folder" and so made ~ count as user-named.
    """
    home = _home()
    out: list[Path] = []
    current = p.parent
    while current != current.parent:
        if _is_home_or_above(current, home):
            break
        out.append(current)
        current = current.parent
    return out


def _path_forms(candidate: Path, home: Path | None) -> set[str]:
    forms = {_norm(str(candidate))}
    if home is not None:
        try:
            forms.add("~/" + candidate.relative_to(home).as_posix().lower())
        except ValueError:
            pass
    parts = list(candidate.parts[1:])
    for k in range(2, min(4, len(parts)) + 1):
        forms.add("/".join(parts[-k:]).lower())
    return forms


def _folder_named(folder: Path, user_text: str, text: str, home: Path | None, *, whole: bool) -> bool:
    """The user's words name this folder: by path, or as "<name> folder" / "folder <name>"."""
    if any(_contains_path(text, form, whole=whole) for form in _path_forms(folder, home)):
        return True
    name = re.escape(folder.name)
    if not name:
        return False
    if re.search(r"(?<![\w.\-])%s\s+(?:folder|directory|dir)\b" % name, user_text, re.IGNORECASE):
        return True
    if re.search(
        r"\b(?:folder|directory)\s+(?:called\s+|named\s+)?[\"']?%s(?![\w\-])" % name, user_text, re.IGNORECASE
    ):
        return True
    return bool(
        home is not None
        and folder.name.lower() in _WELL_KNOWN_USER_FOLDERS
        and _norm(str(folder.parent)) == _norm(str(home))
        and re.search(r"\bmy\s+%s\b" % name, user_text, re.IGNORECASE)
    )


def is_user_referenced_path(p: Path, user_text: str) -> bool:
    """True when the user's own words point at ``p`` or a folder containing it."""
    if not user_text or not user_text.strip():
        return False
    text = _norm(user_text)
    home = _home()
    if any(_contains_path(text, form) for form in _path_forms(p, home)):
        return True
    if _name_mentioned(p.name, user_text):
        return True
    # The target itself counts too: "move the photos folder" names that folder.
    return any(_folder_named(folder, user_text, text, home, whole=False) for folder in [p, *_ancestors_below_home(p)])


# Where files arrive from outside: whatever is in them was written by someone
# else, so naming the folder does not vouch for its contents (taint stays).
_OUTSIDE_ARRIVAL_DIR_NAMES = frozenset(
    {
        "downloads",
        "download",
        "temp",
        "tmp",
        "attachments",
        "content.outlook",
        "inetcache",
        "outlook temp",
        "olk",
    }
)


def _temp_roots() -> set[str]:
    import tempfile

    roots = {tempfile.gettempdir()}
    for var in ("TEMP", "TMP"):
        value = os.environ.get(var)
        if value:
            roots.add(value)
    return roots


def is_outside_arrival_location(p: Path) -> bool:
    """Downloads, temp folders, browser download and mail-attachment caches."""
    if any(part.lower() in _OUTSIDE_ARRIVAL_DIR_NAMES for part in p.parts):
        return True
    key = _norm(str(p))
    for root in _temp_roots():
        try:
            root_key = _norm(str(Path(root).resolve()))
        except (OSError, RuntimeError):
            continue
        if key == root_key or key.startswith(root_key.rstrip("/") + "/"):
            return True
    return False


def user_named_folder_contains(p: Path, user_text: str, *, include_self: bool) -> bool:
    """Taint rule: ``p`` sits in a folder the user named directly in their own message.

    Only a folder named as a folder counts (a path ending at it, or "<name>
    folder"); a full path to some file inside it names that file, not the
    folder. Home and drive roots never count, and nothing under Downloads,
    temp or mail-attachment caches counts, because files there arrive from
    outside.
    """
    if not user_text or not user_text.strip() or is_outside_arrival_location(p):
        return False
    text = _norm(user_text)
    home = _home()
    folders = ([p] if include_self else []) + _ancestors_below_home(p)
    for folder in folders:
        if folder == folder.parent or _is_home_or_above(folder, home):
            continue
        if _folder_named(folder, user_text, text, home, whole=True):
            return True
    return False


def is_autostart_location(p: Path) -> bool:
    text = str(p)
    return any(pattern.search(text) for pattern in _AUTOSTART_PATTERNS)


def is_executable_file(p: Path) -> bool:
    return _EXECUTABLE_RE.search(p.name) is not None


def _resolve(raw: Any) -> Path | None:
    from intent.tools.filesystem import resolve_tool_path

    return resolve_tool_path(str(raw or ""))


def _final_destination(src: Path, dest: Path) -> Path:
    try:
        if dest.is_dir():
            return dest / src.name
    except OSError:
        pass
    return dest


def _folder_file_count(p: Path) -> int:
    try:
        if not p.is_dir():
            return 0
    except OSError:
        return 0
    from intent.tools.filesystem import tree_stats

    files, _bytes, _capped = tree_stats(p, BULK_FILE_THRESHOLD + 1)
    return files


def _tainted() -> bool:
    try:
        from intent.content_sanitizer import context_is_tainted

        return context_is_tainted()
    except (ImportError, AttributeError):
        return True  # cannot tell: treat as tainted, so every change asks


def _action_of(args: Mapping[str, Any] | None) -> str:
    action = str((args or {}).get("action") or "").strip().lower()
    return action or "write"


# ---------------------------------------------------------------------------
# The gate.
# ---------------------------------------------------------------------------


def _named_in_current_request(p: Path) -> bool:
    """The user's message for this turn names this file (by name or full path)."""
    try:
        from intent.content_sanitizer import active_taint_tracker

        words = str(getattr(active_taint_tracker(), "user_request", "") or "")
    except ImportError:  # pragma: no cover - same package
        return False
    if not words.strip():
        return False
    if _name_mentioned(p.name, words):
        return True
    text = _norm(words)
    return any(_contains_path(text, form) for form in _path_forms(p, _home()) if "/" in form)


def base_file_write_class(action: str) -> str | None:
    """Context-free class: what this action is when nothing vouches for it."""
    if action in _NEVER_ASK_ACTIONS:
        return None
    if action in _DELETE_ACTIONS:
        return FILE_DELETE
    if action in _MOVE_ACTIONS:
        return FILE_MOVE
    # Edits, writes, copies and anything unknown fail closed into confirmation.
    return FILE_WRITE


def file_write_action_class(args: Mapping[str, Any] | None) -> str | None:
    """Confirmation class for one ``file_write`` call, or None to just do it."""
    action = _action_of(args)
    base = base_file_write_class(action)
    if base is None or action in _DELETE_ACTIONS:
        return base
    context = active_file_gate_context()
    if context is None or not context.user_id:
        return base
    known = _EDIT_ACTIONS | _WRITE_ACTIONS | _MOVE_ACTIONS | _COPY_ACTIONS
    if action not in known:
        return base
    path = _resolve((args or {}).get("path"))
    if path is None:
        return base
    if _tainted():
        # Untrusted content is in the conversation, so only a change the user
        # asked for in this very message, to a file they named in it, goes
        # ahead (1.0.5 laptop A6.2a: "add a line ... to viola-test.txt on my
        # desktop" asked, while an earlier web page sat in the chat). Injected
        # text cannot put a file name into the user's own words. Everything
        # else asks, as before.
        if action not in _EDIT_ACTIONS | _WRITE_ACTIONS or not _named_in_current_request(path):
            return base
        if is_autostart_location(path):
            return FILE_AUTOSTART
        if is_executable_file(path):
            return FILE_EXECUTABLE
        return None
    destination = None
    if action in _MOVE_ACTIONS | _COPY_ACTIONS:
        destination = _resolve((args or {}).get("destination"))
        if destination is None:
            return base
        destination = _final_destination(path, destination)

    targets = [path] if destination is None else [destination] + ([path] if action in _MOVE_ACTIONS else [])
    if any(is_autostart_location(target) for target in targets):
        return FILE_AUTOSTART
    if action in _EDIT_ACTIONS | _WRITE_ACTIONS and is_executable_file(path):
        return FILE_EXECUTABLE
    if destination is not None and _folder_file_count(path) > BULK_FILE_THRESHOLD:
        return FILE_BULK

    def referenced(target: Path) -> bool:
        if is_user_referenced_path(target, context.user_text):
            return True
        from intent.tools.filesystem import path_touched_in_session

        return path_touched_in_session(context.user_id, context.session_key, target)

    if action in _MOVE_ACTIONS:
        if not referenced(path):
            return FILE_MOVE
        if destination is not None and destination.exists() and not referenced(destination):
            return FILE_MOVE
        return None
    if action in _COPY_ACTIONS:
        if destination is not None and destination.exists() and not referenced(destination):
            return FILE_WRITE
        return None
    try:
        exists = path.exists()
    except OSError:
        return base
    if not exists or referenced(path):
        return None
    return FILE_WRITE


def describe_file_action(action_class: str, args: Mapping[str, Any] | None) -> str | None:
    """Plain-words description for the confirm prompt, or None if not a file class."""
    params = args or {}
    raw_path = str(params.get("path") or params.get("filename") or "").strip()
    target = raw_path or "the file"
    action = _action_of(params)
    destination = str(params.get("destination") or "").strip()
    if action_class == FILE_DELETE:
        path = _resolve(raw_path) if raw_path else None
        if path is not None and path.is_dir():
            count = _folder_file_count(path)
            if count > BULK_FILE_THRESHOLD:
                inside = "more than %d files" % BULK_FILE_THRESHOLD
            else:
                inside = "1 file" if count == 1 else "%d files" % count
            return "delete the folder %s and the %s in it (it goes to the Recycle Bin and can be undone)" % (
                raw_path,
                inside,
            )
        return "delete %s (it goes to the Recycle Bin and can be undone)" % target if raw_path else "delete the file"
    if action_class == FILE_MOVE:
        return "move %s to %s" % (target, destination or "a new place")
    if action_class == FILE_BULK:
        verb = "copy" if action in _COPY_ACTIONS else "move"
        return "%s the folder %s (more than %d files) to %s" % (verb, target, BULK_FILE_THRESHOLD, destination)
    if action_class == FILE_AUTOSTART:
        where = destination if action in _MOVE_ACTIONS | _COPY_ACTIONS and destination else target
        return "write %s, a file that can run code automatically" % where
    if action_class == FILE_EXECUTABLE:
        return "write the program file %s" % target
    if action_class == FILE_WRITE:
        if action == "append":
            return "add to the end of %s" % target
        if action == "insert":
            return "insert lines into %s" % target
        if action in {"replace_lines", "replace_text"}:
            return "edit part of %s" % target
        if action in {"edit", "multi_edit"}:
            return "edit %s" % target
        if action in _COPY_ACTIONS:
            return "copy %s over the existing %s" % (target, destination or "file")
        return "write %s" % raw_path if raw_path else "write the file"
    return None


__all__ = [
    "BULK_FILE_THRESHOLD",
    "FILE_ACTION_CLASSES",
    "FileGateContext",
    "active_file_gate_context",
    "base_file_write_class",
    "bind_file_gate_context",
    "describe_file_action",
    "file_write_action_class",
    "is_autostart_location",
    "is_executable_file",
    "is_user_referenced_path",
    "reset_file_gate_context",
    "user_texts_from_messages",
]
