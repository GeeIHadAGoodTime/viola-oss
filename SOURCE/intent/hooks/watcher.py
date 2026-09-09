"""FileChanged + CwdChanged watcher for the hook system.

Parity reference: ``src/utils/hooks/fileChangedWatcher.ts``. Claude fires a
``FileChanged`` hook whenever a watched path on disk is modified after
hooks declare it via ``watchPaths``, and a ``CwdChanged`` event when the
agent changes its working directory.

Viola intentionally keeps this watcher single-process and best-effort. We
poll mtime instead of using a platform-specific event loop because:

* The hook surface is opt-in — only paths a hook has asked for are watched.
* Polling at a coarse interval (default 2s) is enough for the hook
  envelope contract.
* No new third-party dependency is needed; ``watchdog`` would bring in
  PyObjC on macOS, which we don't want for Tier 3 desktop work.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from core.logging_config import get_logger
from intent.hooks.dispatcher import HookDispatchState, dispatch_lifecycle
from intent.hooks.schema import HookEvent, HookEventName, HookResult

logger = get_logger(__name__)

_DEFAULT_POLL_SECONDS = 2.0

WatcherDispatchFn = Callable[[HookEvent], Awaitable[HookResult] | HookResult]


@dataclass
class _WatchEntry:
    path: Path
    last_mtime: float | None = None
    missing_logged: bool = False


@dataclass
class WatcherState:
    """Per-session paths and last cwd registered with the watcher."""

    paths: dict[str, _WatchEntry] = field(default_factory=dict)
    cwd: str | None = None


class HookFileWatcher:
    """Polling watcher for hook ``watchPaths`` and cwd changes."""

    def __init__(
        self,
        *,
        poll_seconds: float = _DEFAULT_POLL_SECONDS,
        dispatch_fn: WatcherDispatchFn | None = None,
        dispatch_state: HookDispatchState | None = None,
    ) -> None:
        self._poll_seconds = max(0.1, float(poll_seconds))
        self._dispatch_fn = dispatch_fn
        self._dispatch_state = dispatch_state
        self._sessions: dict[str, WatcherState] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def register_paths(self, session_id: str | None, paths: Iterable[str]) -> None:
        """Add ``paths`` to the watch set for ``session_id``."""

        state = self._sessions.setdefault(session_id or "__default__", WatcherState())
        for raw_path in paths:
            text = str(raw_path or "").strip()
            if not text:
                continue
            resolved = str(Path(text).expanduser())
            if resolved in state.paths:
                continue
            entry = _WatchEntry(path=Path(resolved))
            entry.last_mtime = _safe_mtime(entry.path)
            state.paths[resolved] = entry

    def set_cwd(self, session_id: str | None, cwd: str | None) -> bool:
        """Update the current cwd for ``session_id``. Returns True if it changed."""

        normalized = str(Path(cwd).expanduser()) if cwd else None
        state = self._sessions.setdefault(session_id or "__default__", WatcherState())
        if state.cwd == normalized:
            return False
        state.cwd = normalized
        return True

    def clear(self, session_id: str | None = None) -> None:
        if session_id is None:
            self._sessions.clear()
            return
        self._sessions.pop(session_id or "__default__", None)

    async def start(self) -> None:
        if self.is_running:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="hook-file-watcher")

    async def stop(self) -> None:
        if not self.is_running:
            return
        self._stopping.set()
        if self._task is not None:
            await asyncio.wait([self._task], timeout=self._poll_seconds * 2)
        self._task = None

    async def _run(self) -> None:
        try:
            while not self._stopping.is_set():
                await self._poll_once()
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_seconds)
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            return

    async def _poll_once(self) -> None:
        for session_id, state in list(self._sessions.items()):
            # F-005 (a): Claude emits change/add/unlink, not modified/removed.
            # File appearance (mtime newly available) is "add", subsequent
            # mtime increase is "change", disappearance is "unlink".
            changed: list[tuple[str, str]] = []
            for path_key, entry in list(state.paths.items()):
                mtime = _safe_mtime(entry.path)
                if mtime is None:
                    if entry.last_mtime is not None and not entry.missing_logged:
                        entry.missing_logged = True
                        changed.append((path_key, "unlink"))
                    continue
                # mtime came back — restore the bookkeeping.
                was_missing = entry.missing_logged
                entry.missing_logged = False
                if entry.last_mtime is None:
                    entry.last_mtime = mtime
                    changed.append((path_key, "add"))
                elif mtime > entry.last_mtime:
                    entry.last_mtime = mtime
                    changed.append((path_key, "add" if was_missing else "change"))
            if not changed:
                continue
            await self._dispatch_changes(session_id, changed)

    async def _dispatch_changes(self, session_id: str, changes: list[tuple[str, str]]) -> None:
        from pathlib import Path as _Path

        for path, kind in changes:
            # F-005 (b): the matcher applies to the basename (Claude's
            # ``fileChangedWatcher.ts:28-77`` selects matchers by file name).
            # The full path stays in the payload for handlers that need it.
            payload = {
                "file_path": path,
                "path": path,
                "file_name": _Path(path).name,
                "event": kind,
                "kind": kind,
            }
            event = HookEvent(
                name=HookEventName.FILE_CHANGED,
                payload=payload,
                session_id=session_id if session_id != "__default__" else None,
            )
            try:
                await self._safe_dispatch(event)
            except (RuntimeError, OSError, ValueError) as exc:
                logger.warning("File watcher dispatch failed: %s", exc)

    async def emit_cwd_change(self, session_id: str | None, new_cwd: str | None) -> None:
        """Emit a ``CwdChanged`` event for ``session_id``.

        F-005 (c): Claude's ``CwdChanged`` carries both ``old_cwd`` and
        ``new_cwd`` (entrypoints/sdk/coreSchemas.ts:727-743). The previous
        implementation only emitted ``{"cwd": new_cwd}``, which collapsed
        the transition and forced hooks to track the previous value
        themselves.
        """

        state = self._sessions.setdefault(session_id or "__default__", WatcherState())
        old_cwd = state.cwd
        if not self.set_cwd(session_id, new_cwd):
            return
        event = HookEvent(
            name=HookEventName.CWD_CHANGED,
            payload={
                "cwd": new_cwd or "",
                "new_cwd": new_cwd or "",
                "old_cwd": old_cwd or "",
            },
            session_id=session_id,
        )
        try:
            await self._safe_dispatch(event)
        except Exception as exc:
            logger.warning("CwdChanged dispatch failed: %s", exc)

    async def _safe_dispatch(self, event: HookEvent) -> HookResult:
        if self._dispatch_fn is not None:
            outcome = self._dispatch_fn(event)
            if asyncio.iscoroutine(outcome):
                outcome = await outcome
            if isinstance(outcome, HookResult):
                return outcome
            return HookResult()
        # Fallback: dispatch through the default lifecycle registry.
        loop = asyncio.get_running_loop()

        def _dispatch_blocking() -> HookResult:
            return dispatch_lifecycle(
                event.name,
                dict(event.payload),
                session_id=event.session_id,
                dispatch_state=self._dispatch_state,
            )

        return await loop.run_in_executor(None, _dispatch_blocking)


def _safe_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


__all__ = ["HookFileWatcher", "WatcherState"]
