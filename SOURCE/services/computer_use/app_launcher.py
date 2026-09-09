"""Native app launch helpers for the unified computer tool.

Windows resolves a target through ``Get-StartApps`` (the Start menu index),
then ``where.exe``, then Windows Search. macOS resolves it through the
installed ``.app`` bundles and launches via ``open -a`` (LaunchServices),
which is the system's own launcher — the same role ``Get-StartApps`` +
``explorer.exe shell:AppsFolder`` play on Windows.

macOS parity note (#333): before this module grew a darwin branch, every
macOS launch died in ``get_start_apps`` — ``proc_tree.run(["powershell", ...])``
raises ``FileNotFoundError`` on a Mac, and nothing caught it — so "open
Safari" was a hard failure on every Mac install while working on Windows.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from scripts import proc_tree
from services.computer_use import input as computer_input, window_manager

logger = get_logger(__name__)

START_APPS_CACHE_TTL_SECONDS = 6 * 60 * 60
START_APPS_TIMEOUT_SECONDS = 5
WINDOW_POLL_SECONDS = 3.0
WINDOW_POLL_INTERVAL_SECONDS = 0.25

# Ranking thresholds, shared by every platform's resolver so a Mac and a PC
# accept/reject the same fuzzy match for the same spoken app name.
MATCH_SCORE_THRESHOLD = 0.62
SUGGESTION_SCORE_THRESHOLD = 0.35

# macOS: where LaunchServices-registered apps actually live, and the binary
# that asks LaunchServices to start one.
DARWIN_OPEN_BINARY = "/usr/bin/open"
DARWIN_APP_DIRECTORIES = (
    "/Applications",
    "/Applications/Utilities",
    "/System/Applications",
    "/System/Applications/Utilities",
    "~/Applications",
)
DARWIN_SPOTLIGHT_BINARY = "/usr/bin/mdfind"
DARWIN_MAX_INDEXED_APPS = 2000

_START_APPS_CACHE: list[StartApp] | None = None
_START_APPS_CACHE_AT = 0.0
_DARWIN_APPS_CACHE: list[StartApp] | None = None
_DARWIN_APPS_CACHE_AT = 0.0


@dataclass(frozen=True)
class StartApp:
    """One app returned by the Windows Get-StartApps cmdlet."""

    name: str
    app_id: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "app_id": self.app_id}


def _normalize_app_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _coerce_start_apps(raw: Any) -> list[StartApp]:
    rows = raw if isinstance(raw, list) else [raw]
    apps: list[StartApp] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or row.get("name") or "").strip()
        app_id = str(row.get("AppID") or row.get("AppId") or row.get("app_id") or "").strip()
        if name and app_id:
            apps.append(StartApp(name=name, app_id=app_id))
    return apps


def _rank_apps(query: str, apps: list[StartApp]) -> tuple[StartApp | None, list[StartApp]]:
    """Rank ``apps`` against ``query``; return the best match and suggestions.

    Shared by the Windows Start-menu resolver and the macOS bundle resolver so
    both platforms apply the same acceptance bar to the same spoken name.
    """
    ranked = sorted(
        ((_score_app(query, app), app) for app in apps),
        key=lambda item: item[0],
        reverse=True,
    )
    suggestions = [app for score, app in ranked[:5] if score >= SUGGESTION_SCORE_THRESHOLD]
    best = ranked[0][1] if ranked and ranked[0][0] >= MATCH_SCORE_THRESHOLD else None
    return best, suggestions


def clear_start_apps_cache() -> None:
    """Clear the in-process StartApps cache. Intended for tests."""
    global _START_APPS_CACHE, _START_APPS_CACHE_AT
    _START_APPS_CACHE = None
    _START_APPS_CACHE_AT = 0.0


def get_start_apps(*, force_refresh: bool = False) -> list[StartApp]:
    """Return cached Windows Start menu apps using Get-StartApps."""
    global _START_APPS_CACHE, _START_APPS_CACHE_AT
    now = time.monotonic()
    if (
        not force_refresh
        and _START_APPS_CACHE is not None
        and now - _START_APPS_CACHE_AT < START_APPS_CACHE_TTL_SECONDS
    ):
        return list(_START_APPS_CACHE)

    # PowerShell is a full interpreter that can spawn grandchildren (unlike a leaf
    # binary), so this is routed through proc_tree.run's tree-killing runner rather
    # than raw subprocess.run. raise_on_timeout=True preserves the prior contract: a
    # timeout propagates as subprocess.TimeoutExpired to this function's caller
    # (there is no local try/except here today).
    completed = proc_tree.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress",
        ],
        timeout=START_APPS_TIMEOUT_SECONDS,
        raise_on_timeout=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        msg = "Get-StartApps failed: %s" % (detail or "exit code %d" % completed.returncode)
        raise RuntimeError(msg)

    output = (completed.stdout or "").strip()
    if not output:
        apps: list[StartApp] = []
    else:
        apps = _coerce_start_apps(json.loads(output))
    _START_APPS_CACHE = apps
    _START_APPS_CACHE_AT = now
    return list(apps)


def _score_app(query: str, app: StartApp) -> float:
    normalized_query = _normalize_app_name(query)
    normalized_name = _normalize_app_name(app.name)
    if not normalized_query or not normalized_name:
        return 0.0
    if normalized_query == normalized_name:
        return 1.0
    if normalized_name.startswith(normalized_query):
        return 0.95
    if normalized_query in normalized_name:
        return 0.88
    return SequenceMatcher(a=normalized_query, b=normalized_name).ratio()


def resolve_start_app(app_name: str, *, force_refresh: bool = False) -> tuple[StartApp | None, list[StartApp]]:
    """Return the best StartApps match and up to five suggestions."""
    query = app_name.strip()
    if not query:
        return None, []
    apps = get_start_apps(force_refresh=force_refresh)
    best, suggestions = _rank_apps(query, apps)
    if best is not None:
        return best, suggestions
    if not force_refresh:
        return resolve_start_app(app_name, force_refresh=True)
    return None, suggestions


def _window_handles(payload: dict[str, object]) -> set[int]:
    handles: set[int] = set()
    windows = payload.get("windows")
    if not isinstance(windows, list):
        return handles
    for item in windows:
        if not isinstance(item, dict):
            continue
        try:
            handles.add(int(item.get("handle") or item.get("window_handle") or 0))
        except (TypeError, ValueError):
            continue
    handles.discard(0)
    return handles


def _candidate_window(app_name: str, before_handles: set[int]) -> dict[str, object] | None:
    try:
        payload = window_manager.list_windows(include_minimized=True, include_titles=True)
    except Exception:
        logger.debug("Could not poll windows after app launch")
        return None
    windows = payload.get("windows")
    if not isinstance(windows, list):
        return None
    normalized_app = _normalize_app_name(app_name)
    first_new: dict[str, object] | None = None
    for item in windows:
        if not isinstance(item, dict):
            continue
        try:
            handle = int(item.get("handle") or 0)
        except (TypeError, ValueError):
            handle = 0
        if handle and handle not in before_handles and first_new is None:
            first_new = item
        title = _normalize_app_name(str(item.get("title") or ""))
        executable = _normalize_app_name(str(item.get("executable") or ""))
        if normalized_app and (normalized_app in title or normalized_app in executable):
            return item
    return first_new


def _poll_for_window(app_name: str, before_handles: set[int]) -> dict[str, object] | None:
    deadline = time.monotonic() + WINDOW_POLL_SECONDS
    while time.monotonic() < deadline:
        match = _candidate_window(app_name, before_handles)
        if match is not None:
            return match
        time.sleep(WINDOW_POLL_INTERVAL_SECONDS)
    return None


def _launch_start_app(app: StartApp) -> None:
    subprocess.Popen(["explorer.exe", "shell:AppsFolder\\%s" % app.app_id])


def _where_executable(app_name: str) -> str:
    candidates = [app_name.strip()]
    if candidates[0] and not candidates[0].lower().endswith(".exe"):
        candidates.append("%s.exe" % candidates[0])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            completed = subprocess.run(
                ["where.exe", candidate],
                capture_output=True,
                check=False,
                text=True,
                timeout=START_APPS_TIMEOUT_SECONDS,
            )  # proc-tree-ok: where.exe is a single Windows PATH-lookup binary, no shell, no grandchildren
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if completed.returncode != 0:
            continue
        for line in (completed.stdout or "").splitlines():
            path = line.strip()
            if path and Path(path).suffix.lower() == ".exe":
                return path
    return ""


def clear_darwin_apps_cache() -> None:
    """Drop the cached macOS installed-app index."""
    global _DARWIN_APPS_CACHE, _DARWIN_APPS_CACHE_AT
    _DARWIN_APPS_CACHE = None
    _DARWIN_APPS_CACHE_AT = 0.0


def _darwin_scan_app_directories() -> dict[str, str]:
    """Return ``{bundle_path: display_name}`` for the standard macOS app folders.

    This is the deterministic leg: it needs no Spotlight index and no
    subprocess, so a Mac with indexing disabled still resolves the apps a user
    actually has.
    """
    found: dict[str, str] = {}
    for raw_directory in DARWIN_APP_DIRECTORIES:
        directory = Path(raw_directory).expanduser()
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            # Missing or unreadable directory (e.g. no ~/Applications) is normal.
            continue
        for entry in entries:
            if entry.suffix == ".app":
                found.setdefault(str(entry), entry.stem)
    return found


def _darwin_spotlight_apps() -> dict[str, str]:
    """Return ``{bundle_path: display_name}`` for apps Spotlight knows about.

    Catches bundles installed outside the standard folders. Best-effort: a
    missing or disabled Spotlight index simply contributes nothing.
    """
    found: dict[str, str] = {}
    try:
        completed = proc_tree.run(
            [
                DARWIN_SPOTLIGHT_BINARY,
                "kMDItemContentType == 'com.apple.application-bundle'",
            ],
            timeout=START_APPS_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError):
        logger.debug("Spotlight app enumeration unavailable")
        return found
    if completed.returncode != 0:
        return found
    for line in (completed.stdout or "").splitlines()[:DARWIN_MAX_INDEXED_APPS]:
        path = line.strip()
        if path.endswith(".app"):
            found.setdefault(path, Path(path).stem)
    return found


def get_darwin_apps(*, force_refresh: bool = False) -> list[StartApp]:
    """Return the cached macOS installed-app index (``.app`` bundles).

    The macOS counterpart of :func:`get_start_apps`: ``app_id`` carries the
    bundle's absolute path, which is what ``open -a`` takes.
    """
    global _DARWIN_APPS_CACHE, _DARWIN_APPS_CACHE_AT
    now = time.monotonic()
    if (
        not force_refresh
        and _DARWIN_APPS_CACHE is not None
        and now - _DARWIN_APPS_CACHE_AT < START_APPS_CACHE_TTL_SECONDS
    ):
        return list(_DARWIN_APPS_CACHE)

    found = _darwin_scan_app_directories()
    for path, name in _darwin_spotlight_apps().items():
        found.setdefault(path, name)

    apps = [StartApp(name=name, app_id=path) for path, name in sorted(found.items(), key=lambda item: item[1].lower())]
    _DARWIN_APPS_CACHE = apps
    _DARWIN_APPS_CACHE_AT = now
    return list(apps)


def resolve_darwin_app(app_name: str, *, force_refresh: bool = False) -> tuple[StartApp | None, list[StartApp]]:
    """Return the best installed-``.app`` match and up to five suggestions."""
    query = app_name.strip()
    if not query:
        return None, []
    apps = get_darwin_apps(force_refresh=force_refresh)
    best, suggestions = _rank_apps(query, apps)
    if best is not None:
        return best, suggestions
    if not force_refresh:
        return resolve_darwin_app(app_name, force_refresh=True)
    return None, suggestions


def _darwin_open(target: str) -> bool:
    """Ask LaunchServices to open ``target`` (a ``.app`` path or an app name)."""
    if not target:
        return False
    try:
        completed = proc_tree.run(
            [DARWIN_OPEN_BINARY, "-a", target],
            timeout=START_APPS_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError):
        logger.debug("Could not invoke the macOS open binary")
        return False
    if completed.returncode != 0:
        logger.debug(
            "open -a failed for %s: %s",
            target,
            (completed.stderr or completed.stdout or "").strip(),
        )
        return False
    return True


def _darwin_settle_launch(
    name: str,
    before_handles: set[int],
    *,
    bring_to_front: bool,
    **payload_extra: object,
) -> dict[str, object]:
    """Wait for the launched app's window, optionally raise it, build the payload.

    Raising the window is best-effort by design: LaunchServices has already
    started the app, so a focus failure must not turn a successful launch into
    a reported failure.
    """
    match = _poll_for_window(name, before_handles)
    if bring_to_front and match is not None:
        try:
            window_manager.focus_window(handle=int(match.get("handle") or 0))
        except Exception:  # noqa: BLE001, RUF100 - best-effort raise; must not fail the launch
            logger.debug("Could not focus launched app window")
    return _window_payload(name, match, **payload_extra)


def _darwin_launch_app(requested: str, before_handles: set[int], *, bring_to_front: bool) -> dict[str, object]:
    """Launch ``requested`` on macOS via LaunchServices.

    Two legs, mirroring the Windows resolver's Start-menu-then-PATH shape:
    the installed-bundle index first (so suggestions are real installed apps),
    then a bare ``open -a <name>`` so LaunchServices' own name resolution gets
    a shot at anything the index missed.
    """
    app, suggestions = resolve_darwin_app(requested)
    if app is not None and _darwin_open(app.app_id):
        return _darwin_settle_launch(
            app.name,
            before_handles,
            bring_to_front=bring_to_front,
            launch_method="open_bundle",
            app_id=app.app_id,
            matched_app_name=app.name,
        )

    if _darwin_open(requested):
        return _darwin_settle_launch(
            requested,
            before_handles,
            bring_to_front=bring_to_front,
            launch_method="open_name",
        )

    return {
        "ok": False,
        "action": "launch_app",
        "app_name": requested,
        "error_category": "COMPUTER_USE_APP_NOT_FOUND",
        "reason": "No matching installed application bundle was found",
        "suggestions": [item.to_dict() for item in suggestions],
    }


def _launch_via_windows_search(app_name: str) -> bool:
    if sys.platform != "win32":
        # Windows Search (Win+S) does not exist elsewhere. With the macOS
        # native key synthesis in place, "windows+s" would land as Cmd+S in
        # the frontmost app (Save dialog) — refuse instead of misfiring.
        logger.debug("Windows Search launch fallback is Windows-only; skipping on %s", sys.platform)
        return False
    try:
        computer_input.press_key("windows+s")
        computer_input.type_text(app_name)
        computer_input.press_key("enter")
        return True
    except Exception:
        logger.debug("Windows Search launch fallback failed")
        return False


def _window_payload(app_name: str, match: dict[str, object] | None, **extra: object) -> dict[str, object]:
    title = str(match.get("title") or match.get("window_title") or "") if match else ""
    try:
        hwnd: int | None = int(match.get("handle") or match.get("window_handle") or 0) if match else None
    except (TypeError, ValueError):
        hwnd = None
    if hwnd == 0:
        hwnd = None
    return {
        "ok": True,
        "action": "launch_app",
        "app_name": app_name,
        "window_title": title,
        "hwnd": hwnd,
        **extra,
    }


def launch_app(app_name: str, *, bring_to_front: bool = True) -> dict[str, object]:
    """Launch a native app.

    Windows: StartApps name, PATH executable, then Windows Search.
    macOS: installed ``.app`` bundle, then LaunchServices name resolution.
    """
    requested = app_name.strip()
    if not requested:
        return {
            "ok": False,
            "action": "launch_app",
            "error_category": "COMPUTER_USE_INVALID_ARGUMENTS",
            "reason": "app_name is required",
            "suggestions": [],
        }

    try:
        before = _window_handles(window_manager.list_windows(include_minimized=True, include_titles=True))
    except Exception:
        logger.debug("Could not capture pre-launch window list")
        before = set()

    if sys.platform == "darwin":
        # Every leg below this point is Windows-only (powershell Get-StartApps,
        # where.exe, Win+S) and raises or no-ops on a Mac (#333).
        return _darwin_launch_app(requested, before, bring_to_front=bring_to_front)

    app, suggestions = resolve_start_app(requested)
    if app is not None:
        _launch_start_app(app)
        match = _poll_for_window(app.name, before)
        if bring_to_front and match is not None:
            try:
                window_manager.focus_window(handle=int(match.get("handle") or 0))
            except Exception:
                logger.debug("Could not focus launched app window")
        return _window_payload(
            app.name,
            match,
            launch_method="start_apps",
            app_id=app.app_id,
            matched_app_name=app.name,
        )

    executable = _where_executable(requested)
    if executable:
        subprocess.Popen([executable])
        match = _poll_for_window(requested, before)
        if bring_to_front and match is not None:
            try:
                window_manager.focus_window(handle=int(match.get("handle") or 0))
            except Exception:
                logger.debug("Could not focus launched executable window")
        return _window_payload(requested, match, launch_method="where", executable=executable)

    if _launch_via_windows_search(requested):
        match = _poll_for_window(requested, before)
        if match is not None:
            return _window_payload(requested, match, launch_method="windows_search")

    return {
        "ok": False,
        "action": "launch_app",
        "app_name": requested,
        "error_category": "COMPUTER_USE_APP_NOT_FOUND",
        "reason": "No matching Start menu app or PATH executable was found",
        "suggestions": [item.to_dict() for item in suggestions],
    }
