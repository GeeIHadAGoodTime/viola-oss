"""REST API endpoints for the local music library.

Provides browse, search, like/unlike, and playlist CRUD endpoints
for local audio files indexed by the LocalMusicProvider.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Callable

from contracts.api_response import failure_response
from core.logging_config import get_logger
from core.subprocess_utils import run_silent
from fastapi import Body, Depends, Path, Query
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_operator_auth
from ui.api.routes.common import RouteToolbox
from ui.api.routes.error_handler import handle_route_error

log = get_logger(__name__)


_DIALOG_TITLE = "Select Music Folder"

# A folder picker is a human interaction, so it must outlive a short RPC timeout.
_DIALOG_TIMEOUT_SECONDS = 600
_FOLDER_PICKER_FAILURE_MESSAGE = "Couldn't open the folder picker. Check your desktop permissions and try again."
_FOLDER_PICKER_UNAVAILABLE_MESSAGE = "No folder picker is available on this system."
_FOLDER_PICKER_PROTOCOL_MESSAGE = "The folder picker returned an invalid response. Please try again."
_WINDOWS_FOLDER_CANCELLED = "__VIOLA_FOLDER_CANCELLED__"
_WINDOWS_FOLDER_PICKED_PREFIX = "__VIOLA_FOLDER_PICKED__:"


class FolderDialogUnavailable(RuntimeError):
    """Raised when one picker strategy is unavailable and a fallback may work."""


class FolderDialogError(RuntimeError):
    """Raised when a native folder picker failed rather than being cancelled."""

    def __init__(
        self,
        operator_message: str,
        *,
        helper: str,
        platform_name: str,
        reason: str,
        returncode: int | None = None,
        stderr: str | None = None,
        stdout: str | None = None,
        http_status: int = 503,
        client_message: str = _FOLDER_PICKER_FAILURE_MESSAGE,
    ) -> None:
        super().__init__(operator_message)
        self.helper = helper
        self.platform_name = platform_name
        self.reason = reason
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout
        self.http_status = http_status
        self.client_message = client_message

    def response_details(self) -> dict[str, object]:
        details: dict[str, object] = {
            "http_status": self.http_status,
            "platform": self.platform_name,
            "helper": self.helper,
            "reason": self.reason,
        }
        if self.returncode is not None:
            details["returncode"] = self.returncode
        if self.stderr:
            details["stderr_tail"] = self.stderr[-300:]
        if self.stdout:
            details["stdout_tail"] = self.stdout[-300:]
        return details


def _text(value: object) -> str:
    return str(value or "").strip()


def _process_output(result: subprocess.CompletedProcess) -> tuple[str, str]:
    return _text(getattr(result, "stdout", "")), _text(getattr(result, "stderr", ""))


def _folder_dialog_failure(
    helper: str,
    platform_name: str,
    result: subprocess.CompletedProcess,
    *,
    reason: str = "native_helper_failed",
) -> FolderDialogError:
    stdout, stderr = _process_output(result)
    return FolderDialogError(
        "%s folder dialog failed with exit code %s" % (helper, result.returncode),
        helper=helper,
        platform_name=platform_name,
        reason=reason,
        returncode=int(result.returncode),
        stderr=stderr,
        stdout=stdout,
    )


def _folder_dialog_selected_path(
    helper: str,
    platform_name: str,
    result: subprocess.CompletedProcess,
    *,
    stdout: str | None = None,
) -> str:
    """Return a helper-selected path or expose its broken success protocol.

    Subprocess pickers reserve a zero exit for a completed selection.  Their
    documented cancel contracts use a distinct signal, so an empty successful
    stdout is not a quiet cancel: it is a helper/protocol failure that must
    reach the route and the desktop UI.
    """
    result_stdout, stderr = _process_output(result)
    folder = result_stdout if stdout is None else stdout.strip()
    if folder:
        return folder
    returncode = int(result.returncode)
    raise FolderDialogError(
        "%s folder dialog completed without a selected path" % helper,
        helper=helper,
        platform_name=platform_name,
        reason="native_helper_protocol_error",
        returncode=returncode,
        stderr=stderr,
        stdout=result_stdout,
        client_message=_FOLDER_PICKER_PROTOCOL_MESSAGE,
    )


def _folder_dialog_timeout(
    helper: str,
    platform_name: str,
    exc: subprocess.TimeoutExpired,
) -> FolderDialogError:
    return FolderDialogError(
        "%s folder dialog timed out after %s seconds" % (helper, exc.timeout),
        helper=helper,
        platform_name=platform_name,
        reason="native_helper_timeout",
        stderr=_text(getattr(exc, "stderr", "")),
        stdout=_text(getattr(exc, "stdout", "")),
        client_message="The folder picker timed out. Try again when you are ready to choose a folder.",
    )


def _darwin_user_cancelled(result: subprocess.CompletedProcess) -> bool:
    _stdout, stderr = _process_output(result)
    # AppleScript's documented user-cancel error is -128.  Text such as
    # "cancelled" on its own is not a protocol, and must not hide an unrelated
    # osascript failure as a quiet no-selection result.
    return "(-128)" in stderr or "error number -128" in stderr.lower()


def _linux_user_cancelled(result: subprocess.CompletedProcess) -> bool:
    _stdout, stderr = _process_output(result)
    # zenity and kdialog reserve status 1 without diagnostic output for a
    # dismissed selection.  Do not treat arbitrary stderr containing
    # "cancel" as that documented interaction result.
    return int(result.returncode) == 1 and not stderr


def _folder_dialog_darwin() -> str | None:
    """macOS native folder picker, via ``osascript``'s Cocoa ``choose folder``.

    Returns:
        The chosen POSIX path, or ``None`` when the user cancelled.

    Raises:
        FolderDialogUnavailable: when ``osascript`` itself could not be run, so
            the caller can fall through to another strategy rather than treat a
            broken mechanism as a cancelled dialog.
        FolderDialogError: when the picker ran but failed.
    """
    script = 'POSIX path of (choose folder with prompt "%s")' % _DIALOG_TITLE
    try:
        result = run_silent(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=_DIALOG_TIMEOUT_SECONDS,
        )  # proc-tree-ok: single osascript binary, no shell, no grandchildren
    except FileNotFoundError as exc:  # pragma: no cover - osascript ships with macOS
        raise FolderDialogUnavailable("osascript is not available") from exc
    except subprocess.TimeoutExpired as exc:
        raise _folder_dialog_timeout("osascript", "darwin", exc) from exc
    if result.returncode != 0:
        # osascript exits non-zero when the user dismisses the dialog. That is a
        # completed interaction. Other non-zero exits include automation/TCC
        # denial such as -1743, and must surface instead of looking cancelled.
        if _darwin_user_cancelled(result):
            return None
        reason = "authorization_denied" if "-1743" in _process_output(result)[1] else "native_helper_failed"
        raise _folder_dialog_failure("osascript", "darwin", result, reason=reason)
    return _folder_dialog_selected_path("osascript", "darwin", result)


def _folder_dialog_linux() -> str | None:
    """Linux native folder picker, via whichever desktop helper is installed.

    Returns:
        The chosen path, or ``None`` when the user cancelled.

    Raises:
        FolderDialogUnavailable: when neither helper is installed.
        FolderDialogError: when a helper ran but failed.
    """
    candidates = (
        ["zenity", "--file-selection", "--directory", "--title=%s" % _DIALOG_TITLE],
        ["kdialog", "--getexistingdirectory", os.path.expanduser("~")],
    )
    for argv in candidates:
        try:
            result = run_silent(
                argv,
                capture_output=True,
                text=True,
                timeout=_DIALOG_TIMEOUT_SECONDS,
            )  # proc-tree-ok: single dialog binary, no shell, no grandchildren
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired as exc:
            raise _folder_dialog_timeout(argv[0], "linux", exc) from exc
        if result.returncode != 0:
            if _linux_user_cancelled(result):
                return None
            raise _folder_dialog_failure(argv[0], "linux", result)
        return _folder_dialog_selected_path(argv[0], "linux", result)
    raise FolderDialogUnavailable("no folder picker available (tried zenity, kdialog)")


def _folder_dialog_windows() -> str | None:
    """Windows native folder picker, via the WinForms ``FolderBrowserDialog``.

    Returns:
        The chosen path, or ``None`` when the user cancelled.

    Raises:
        FolderDialogUnavailable: when PowerShell itself could not be run.
        FolderDialogError: when PowerShell ran but failed.
    """
    ps_cmd = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$f.Description = '%s'; "
        "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { "
        "Write-Output ('%s' + $f.SelectedPath) "
        "} else { Write-Output '%s' }" % (_DIALOG_TITLE, _WINDOWS_FOLDER_PICKED_PREFIX, _WINDOWS_FOLDER_CANCELLED)
    )
    try:
        result = run_silent(
            ["powershell", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=_DIALOG_TIMEOUT_SECONDS,
        )  # proc-tree-ok: single powershell binary, no shell, no grandchildren
    except FileNotFoundError as exc:  # pragma: no cover - powershell ships with Windows
        raise FolderDialogUnavailable("powershell is not available") from exc
    except subprocess.TimeoutExpired as exc:
        raise _folder_dialog_timeout("powershell", "win32", exc) from exc
    if result.returncode != 0:
        raise _folder_dialog_failure("powershell", "win32", result)
    stdout, _stderr = _process_output(result)
    if stdout == _WINDOWS_FOLDER_CANCELLED:
        return None
    if stdout.startswith(_WINDOWS_FOLDER_PICKED_PREFIX):
        folder = stdout.removeprefix(_WINDOWS_FOLDER_PICKED_PREFIX)
        return _folder_dialog_selected_path("powershell", "win32", result, stdout=folder)
    return _folder_dialog_selected_path("powershell", "win32", result, stdout="")


def _folder_dialog_tk() -> str | None:
    """Last-resort stdlib Tk picker, for running from source.

    The frozen desktop bundle deliberately drops ``tkinter`` (the installer
    diet: ``viola.spec``'s ``excludes`` and ``build_macos_app.sh``'s
    ``--exclude-module tkinter``), so this only ever runs from a source
    checkout. It stays because it is genuinely useful there.

    Raises:
        FolderDialogUnavailable: when tkinter is absent.
        FolderDialogError: when tkinter has no display to draw on.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise FolderDialogUnavailable("tkinter is not available") from exc
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(title=_DIALOG_TITLE)
        root.destroy()
    except Exception as exc:
        raise FolderDialogError(
            "tkinter folder dialog failed: %s" % exc,
            helper="tkinter",
            platform_name=sys.platform,
            reason="fallback_helper_failed",
        ) from exc
    return folder or None


def _open_folder_dialog() -> str | None:
    """Open a native folder picker dialog. Returns selected path or None if cancelled.

    Every platform gets its OWN native picker FIRST, and tkinter is only the
    last resort. That ordering is the whole point: the frozen desktop bundle
    excludes ``tkinter``, so the old shape -- try tkinter, then fall back to
    PowerShell *if and only if* ``sys.platform == "win32"`` -- left macOS and
    Linux with no picker at all. The endpoint still answered ``ok: true`` with
    ``folder: null``, so "Add music folder" silently did nothing on every
    shipped non-Windows build while working on Windows (#333).

    Runs in a thread via asyncio.to_thread() -- never call from the main event loop.
    """
    name: str
    dialog: Callable[[], str | None]
    if sys.platform == "darwin":
        name, dialog = "osascript", _folder_dialog_darwin
    elif sys.platform == "win32":
        name, dialog = "PowerShell", _folder_dialog_windows
    else:
        name, dialog = "zenity/kdialog", _folder_dialog_linux

    try:
        return dialog()
    except FolderDialogUnavailable as exc:
        log.warning("%s folder dialog unavailable (%s); trying tkinter", name, exc)
    except FolderDialogError:
        raise
    except Exception as exc:
        raise FolderDialogError(
            "%s folder dialog failed unexpectedly" % name,
            helper=name,
            platform_name=sys.platform,
            reason="unexpected_native_helper_failure",
        ) from exc

    try:
        return _folder_dialog_tk()
    except FolderDialogUnavailable as exc:
        raise FolderDialogError(
            _FOLDER_PICKER_UNAVAILABLE_MESSAGE,
            helper="tkinter",
            platform_name=sys.platform,
            reason="no_picker_available",
            client_message=_FOLDER_PICKER_UNAVAILABLE_MESSAGE,
        ) from exc


def _get_repo():
    """Get the initialized local library repo singleton."""
    from music.providers.local.db import get_local_library_repo

    repo = get_local_library_repo()
    repo.initialize()
    return repo


def _serialize_track(track: dict) -> dict:
    """Return a stable, caller-safe track payload."""
    return {
        "id": track.get("id"),
        "file_path": track.get("file_path"),
        "file_name": track.get("file_name"),
        "title": track.get("title") or track.get("file_name"),
        "artist": track.get("artist"),
        "album": track.get("album"),
        "duration_seconds": track.get("duration_seconds"),
        "format": track.get("format"),
        "file_size": track.get("file_size"),
        "file_mtime": track.get("file_mtime"),
        "album_art_embedded": track.get("album_art_embedded"),
        "artwork_data": track.get("artwork_data"),
        "media_type": track.get("media_type"),
        "indexed_at": track.get("indexed_at"),
    }


def register_local_library_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    """Register all local music library REST endpoints."""
    router = context.router

    # =========================================================================
    # Library browse & search
    # =========================================================================

    @router.get("/v1/local/library", dependencies=[Depends(require_operator_auth)])
    async def browse_library(
        q: str | None = Query(None, description="Search query"),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        async def _inner():
            try:

                def _sync():
                    repo = _get_repo()
                    if q:
                        from music.providers.local.provider import LocalMusicProvider

                        provider = LocalMusicProvider()
                        provider._get_repo = lambda: repo  # type: ignore[assignment]
                        results = provider.search_tracks("default", q, limit=limit)
                        tracks = [
                            {
                                "id": t.id,
                                "title": t.title,
                                "artist": t.artist_name,
                                "album": t.album_name,
                                "duration_ms": t.duration_ms,
                                "file_path": t.extras.get("file_path"),
                            }
                            for t in results.items
                        ]
                        return {
                            "ok": True,
                            "data": {
                                "tracks": tracks,
                                "total": results.total,
                                "query": q,
                            },
                        }

                    all_tracks = repo.get_all_tracks()
                    page = all_tracks[offset : offset + limit]
                    tracks = [
                        {
                            "id": t["id"],
                            "title": t.get("title") or t.get("file_name"),
                            "artist": t.get("artist"),
                            "album": t.get("album"),
                            "duration_seconds": t.get("duration_seconds"),
                            "format": t.get("format"),
                            "file_path": t["file_path"],
                        }
                        for t in page
                    ]
                    return {
                        "ok": True,
                        "data": {
                            "tracks": tracks,
                            "total": len(all_tracks),
                            "offset": offset,
                            "limit": limit,
                        },
                    }

                return await asyncio.to_thread(_sync)
            except Exception as exc:
                log.debug("Browse library failed: %s", exc)
                return handle_route_error(exc, "browse_library")

        return await toolbox.record_and_call(_inner, route="/v1/local/library", method="GET")

    @router.get("/v1/local/library/{track_id}", dependencies=[Depends(require_operator_auth)])
    async def get_track(track_id: int = Path(...)):
        async def _inner():
            try:

                def _sync():
                    repo = _get_repo()
                    track = repo.get_track_by_id(track_id)
                    if not track:
                        return {
                            "ok": False,
                            "error": {
                                "code": "not_found",
                                "message": "Track not found",
                            },
                            "data": None,
                        }
                    return {"ok": True, "data": _serialize_track(track), "error": None}

                return await asyncio.to_thread(_sync)
            except Exception as exc:
                log.debug("Get track failed: %s", exc)
                return handle_route_error(exc, "get_track")

        return await toolbox.record_and_call(_inner, route="/v1/local/library/{track_id}", method="GET")

    @router.post("/v1/local/library/rescan", dependencies=[Depends(require_operator_auth)])
    async def rescan_library():
        async def _inner():
            try:
                # local_music_folder is a USER preference (settings.json /
                # DEFAULT_SETTINGS), so it is read from SettingsManager — the
                # runtime source of truth — never from AppConfig. Reading
                # AppConfig first (as this route used to) inverts the canon
                # precedence: any AppConfig/env value would silently shadow the
                # folder the user picked in the UI, so the rescan would index
                # the wrong directory. See .claude/rules/settings-config.md.
                from ui.settings_manager import get_settings_manager

                sm = get_settings_manager()
                folder = sm.get("local_music_folder")
                if not folder:
                    return {
                        "ok": False,
                        "error": {
                            "code": "not_configured",
                            "message": "No local music folder configured",
                        },
                        "data": None,
                    }

                def _run_scan():
                    from music.providers.local.scanner import scan_and_index

                    repo = _get_repo()
                    return scan_and_index(folder, repo)

                stats = await asyncio.to_thread(_run_scan)
                return {
                    "ok": True,
                    "data": {
                        "scanned": stats["scanned"],
                        "upserted": stats["upserted"],
                        "removed": stats["removed"],
                        "errors": stats["errors"],
                    },
                    "error": None,
                }
            except Exception as exc:
                log.debug("Rescan failed: %s", exc)
                return handle_route_error(exc, "rescan_library")

        return await toolbox.record_and_call(_inner, route="/v1/local/library/rescan", method="POST")

    # =========================================================================
    # Likes
    # =========================================================================

    @router.get("/v1/local/likes", dependencies=[Depends(require_operator_auth)])
    async def get_likes():
        async def _inner():
            try:

                def _sync():
                    repo = _get_repo()
                    liked = repo.get_liked_songs()
                    tracks = [
                        {
                            "id": t["id"],
                            "title": t.get("title") or t.get("file_name"),
                            "artist": t.get("artist"),
                            "album": t.get("album"),
                            "duration_seconds": t.get("duration_seconds"),
                            "file_path": t["file_path"],
                            "liked_at": t.get("liked_at"),
                        }
                        for t in liked
                    ]
                    return {
                        "ok": True,
                        "data": {"tracks": tracks, "count": len(tracks)},
                        "error": None,
                    }

                return await asyncio.to_thread(_sync)
            except Exception as exc:
                log.debug("Get likes failed: %s", exc)
                return handle_route_error(exc, "get_likes")

        return await toolbox.record_and_call(_inner, route="/v1/local/likes", method="GET")

    @router.post("/v1/local/likes/{track_id}", dependencies=[Depends(require_operator_auth)])
    async def like_track(track_id: int = Path(...)):
        async def _inner():
            try:

                def _sync():
                    repo = _get_repo()
                    track = repo.get_track_by_id(track_id)
                    if not track:
                        return {
                            "ok": False,
                            "error": {
                                "code": "not_found",
                                "message": "Track not found",
                            },
                            "data": None,
                        }
                    repo.add_like(track_id)
                    log.info("Liked local track: %s (id=%d)", track.get("title"), track_id)
                    return {
                        "ok": True,
                        "data": {"track_id": track_id, "liked": True},
                        "error": None,
                    }

                return await asyncio.to_thread(_sync)
            except Exception as exc:
                log.debug("Like track failed: %s", exc)
                return handle_route_error(exc, "like_track")

        return await toolbox.record_and_call(_inner, route="/v1/local/likes/{track_id}", method="POST")

    @router.delete("/v1/local/likes/{track_id}", dependencies=[Depends(require_operator_auth)])
    async def unlike_track(track_id: int = Path(...)):
        async def _inner():
            try:

                def _sync():
                    repo = _get_repo()
                    repo.remove_like(track_id)
                    log.info("Unliked local track: id=%d", track_id)
                    return {
                        "ok": True,
                        "data": {"track_id": track_id, "liked": False},
                        "error": None,
                    }

                return await asyncio.to_thread(_sync)
            except Exception as exc:
                log.debug("Unlike track failed: %s", exc)
                return handle_route_error(exc, "unlike_track")

        return await toolbox.record_and_call(_inner, route="/v1/local/likes/{track_id}", method="DELETE")

    # =========================================================================
    # Playlists
    # =========================================================================

    @router.get("/v1/local/playlists", dependencies=[Depends(require_operator_auth)])
    async def list_playlists():
        async def _inner():
            try:

                def _sync():
                    repo = _get_repo()
                    playlists = repo.list_playlists()
                    return {
                        "ok": True,
                        "data": {
                            "playlists": [
                                {
                                    "id": p["id"],
                                    "name": p["name"],
                                    "track_count": p.get("track_count", 0),
                                    "created_at": p.get("created_at"),
                                    "updated_at": p.get("updated_at"),
                                }
                                for p in playlists
                            ],
                            "count": len(playlists),
                        },
                        "error": None,
                    }

                return await asyncio.to_thread(_sync)
            except Exception as exc:
                log.debug("List playlists failed: %s", exc)
                return handle_route_error(exc, "list_playlists")

        return await toolbox.record_and_call(_inner, route="/v1/local/playlists", method="GET")

    @router.post("/v1/local/playlists", dependencies=[Depends(require_operator_auth)])
    async def create_playlist(body: dict = Body(...)):
        async def _inner():
            try:
                name = body.get("name", "").strip()
                if not name:
                    return {
                        "ok": False,
                        "error": {
                            "code": "invalid_name",
                            "message": "Playlist name is required",
                        },
                        "data": None,
                    }

                def _sync():
                    repo = _get_repo()
                    existing = repo.get_playlist_by_name(name)
                    if existing:
                        return {
                            "ok": False,
                            "error": {
                                "code": "duplicate_name",
                                "message": "A playlist with this name already exists",
                            },
                            "data": None,
                        }
                    playlist_id = repo.create_playlist(name)
                    log.info("Created local playlist: %s (id=%d)", name, playlist_id)
                    return {
                        "ok": True,
                        "data": {"id": playlist_id, "name": name},
                        "error": None,
                    }

                return await asyncio.to_thread(_sync)
            except Exception as exc:
                log.debug("Create playlist failed: %s", exc)
                return handle_route_error(exc, "create_playlist")

        return await toolbox.record_and_call(_inner, route="/v1/local/playlists", method="POST")

    @router.delete("/v1/local/playlists/{playlist_id}", dependencies=[Depends(require_operator_auth)])
    async def delete_playlist(playlist_id: int = Path(...)):
        async def _inner():
            try:

                def _sync():
                    repo = _get_repo()
                    playlist = repo.get_playlist_by_id(playlist_id)
                    if not playlist:
                        return {
                            "ok": False,
                            "error": {
                                "code": "not_found",
                                "message": "Playlist not found",
                            },
                            "data": None,
                        }
                    repo.delete_playlist(playlist_id)
                    log.info("Deleted local playlist: id=%d", playlist_id)
                    return {
                        "ok": True,
                        "data": {"id": playlist_id, "deleted": True},
                        "error": None,
                    }

                return await asyncio.to_thread(_sync)
            except Exception as exc:
                log.debug("Delete playlist failed: %s", exc)
                return handle_route_error(exc, "delete_playlist")

        return await toolbox.record_and_call(_inner, route="/v1/local/playlists/{playlist_id}", method="DELETE")

    @router.get("/v1/local/playlists/{playlist_id}/songs", dependencies=[Depends(require_operator_auth)])
    async def get_playlist_songs(playlist_id: int = Path(...)):
        async def _inner():
            try:

                def _sync():
                    repo = _get_repo()
                    playlist = repo.get_playlist_by_id(playlist_id)
                    if not playlist:
                        return {
                            "ok": False,
                            "error": {
                                "code": "not_found",
                                "message": "Playlist not found",
                            },
                            "data": None,
                        }
                    songs = repo.get_playlist_songs(playlist_id)
                    tracks = [
                        {
                            "id": s["id"],
                            "title": s.get("title") or s.get("file_name"),
                            "artist": s.get("artist"),
                            "album": s.get("album"),
                            "duration_seconds": s.get("duration_seconds"),
                            "file_path": s["file_path"],
                            "position": s.get("position"),
                        }
                        for s in songs
                    ]
                    return {
                        "ok": True,
                        "data": {
                            "playlist_id": playlist_id,
                            "playlist_name": playlist["name"],
                            "tracks": tracks,
                            "count": len(tracks),
                        },
                        "error": None,
                    }

                return await asyncio.to_thread(_sync)
            except Exception as exc:
                log.debug("Get playlist songs failed: %s", exc)
                return handle_route_error(exc, "get_playlist_songs")

        return await toolbox.record_and_call(_inner, route="/v1/local/playlists/{playlist_id}/songs", method="GET")

    @router.post("/v1/local/playlists/{playlist_id}/songs", dependencies=[Depends(require_operator_auth)])
    async def add_to_playlist(playlist_id: int = Path(...), body: dict = Body(...)):
        async def _inner():
            try:
                track_id = body.get("track_id")
                if track_id is None:
                    return {
                        "ok": False,
                        "error": {
                            "code": "missing_track_id",
                            "message": "track_id is required",
                        },
                        "data": None,
                    }
                tid = int(track_id)

                def _sync():
                    repo = _get_repo()
                    playlist = repo.get_playlist_by_id(playlist_id)
                    if not playlist:
                        return {
                            "ok": False,
                            "error": {
                                "code": "not_found",
                                "message": "Playlist not found",
                            },
                            "data": None,
                        }
                    track = repo.get_track_by_id(tid)
                    if not track:
                        return {
                            "ok": False,
                            "error": {
                                "code": "not_found",
                                "message": "Track not found",
                            },
                            "data": None,
                        }
                    position = repo.get_next_position(playlist_id)
                    repo.add_to_playlist(playlist_id, tid, position)
                    log.info(
                        "Added track %d to playlist %d at position %d",
                        tid,
                        playlist_id,
                        position,
                    )
                    return {
                        "ok": True,
                        "data": {
                            "playlist_id": playlist_id,
                            "track_id": tid,
                            "position": position,
                        },
                        "error": None,
                    }

                return await asyncio.to_thread(_sync)
            except Exception as exc:
                log.debug("Add to playlist failed: %s", exc)
                return handle_route_error(exc, "add_to_playlist")

        return await toolbox.record_and_call(_inner, route="/v1/local/playlists/{playlist_id}/songs", method="POST")

    @router.delete(
        "/v1/local/playlists/{playlist_id}/songs/{track_id}",
        dependencies=[Depends(require_operator_auth)],
    )
    async def remove_from_playlist(playlist_id: int = Path(...), track_id: int = Path(...)):
        async def _inner():
            try:

                def _sync():
                    repo = _get_repo()
                    repo.remove_from_playlist(playlist_id, track_id)
                    log.info("Removed track %d from playlist %d", track_id, playlist_id)
                    return {
                        "ok": True,
                        "data": {
                            "playlist_id": playlist_id,
                            "track_id": track_id,
                            "removed": True,
                        },
                        "error": None,
                    }

                return await asyncio.to_thread(_sync)
            except Exception as exc:
                log.debug("Remove from playlist failed: %s", exc)
                return handle_route_error(exc, "remove_from_playlist")

        return await toolbox.record_and_call(
            _inner,
            route="/v1/local/playlists/{playlist_id}/songs/{track_id}",
            method="DELETE",
        )

    @router.put(
        "/v1/local/playlists/{playlist_id}/reorder",
        dependencies=[Depends(require_operator_auth)],
    )
    async def reorder_playlist_track(playlist_id: int = Path(...), body: dict = Body(...)):
        async def _inner():
            try:
                track_id = body.get("track_id")
                new_position = body.get("new_position")
                if track_id is None or new_position is None:
                    return {
                        "ok": False,
                        "error": {
                            "code": "missing_fields",
                            "message": "track_id and new_position are required",
                        },
                        "data": None,
                    }
                tid = int(track_id)
                pos = int(new_position)
                if pos < 0:
                    return {
                        "ok": False,
                        "error": {
                            "code": "invalid_position",
                            "message": "new_position must be >= 0",
                        },
                        "data": None,
                    }

                def _sync():
                    repo = _get_repo()
                    playlist = repo.get_playlist_by_id(playlist_id)
                    if not playlist:
                        return {
                            "ok": False,
                            "error": {
                                "code": "not_found",
                                "message": "Playlist not found",
                            },
                            "data": None,
                        }
                    moved = repo.reorder_track(playlist_id, tid, pos)
                    if not moved:
                        return {
                            "ok": False,
                            "error": {
                                "code": "track_not_in_playlist",
                                "message": "Track is not in this playlist",
                            },
                            "data": None,
                        }
                    log.info(
                        "Reordered track %d to position %d in playlist %d",
                        tid,
                        pos,
                        playlist_id,
                    )
                    return {
                        "ok": True,
                        "data": {
                            "playlist_id": playlist_id,
                            "track_id": tid,
                            "new_position": pos,
                        },
                        "error": None,
                    }

                return await asyncio.to_thread(_sync)
            except Exception as exc:
                log.debug("Reorder playlist track failed: %s", exc)
                return handle_route_error(exc, "reorder_playlist_track")

        return await toolbox.record_and_call(_inner, route="/v1/local/playlists/{playlist_id}/reorder", method="PUT")

    # =========================================================================
    # Folder browser (native dialog)
    # =========================================================================

    @router.post("/v1/local/browse-folder", dependencies=[Depends(require_operator_auth)])
    async def browse_folder():
        """Open a native folder picker dialog and return the selected path."""
        import asyncio

        async def _inner():
            try:
                folder = await asyncio.to_thread(_open_folder_dialog)
                return {"ok": True, "data": {"folder": folder}, "error": None}
            except FolderDialogError as exc:
                log.warning("Browse folder picker failed: %s", exc)
                return failure_response(
                    "folder_picker_failed",
                    exc.client_message,
                    details=exc.response_details(),
                    data={"folder": None},
                )
            except Exception as exc:
                log.exception("Browse folder failed")
                return handle_route_error(exc, "browse_folder")

        return await toolbox.record_and_call(_inner, route="/v1/local/browse-folder", method="POST")

    log.info("Local library routes registered")


__all__ = ["FolderDialogError", "FolderDialogUnavailable", "register_local_library_routes"]
