from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from core.logging_config import get_logger

logger = get_logger(__name__)


def _normalize_user_id(value: str | None, *, source: str) -> str:
    user_id = str(value or "").strip()
    if not user_id:
        raise ValueError("backend AppState requires a non-empty user_id from %s" % source)
    return user_id


class StateStoreProtocol(Protocol):
    """Protocol for persistent state store interface."""

    def load_restart_counters(self) -> dict[str, int]: ...

    def update_restart_counter(self, name: str, value: int) -> None: ...


# Flag to track if real persistence is available
_PERSISTENCE_AVAILABLE = False
_real_get_state_store: Any = None

try:
    from services.persistence.state_store import (
        get_state_store as _imported_get_state_store,
    )

    _PERSISTENCE_AVAILABLE = True
    _real_get_state_store = _imported_get_state_store
except Exception:  # pragma: no cover - persistence optional in tests
    pass  # _PERSISTENCE_AVAILABLE remains False


def _try_get_state_store() -> StateStoreProtocol | None:
    """Try to get the state store, returning None if unavailable."""
    if not _PERSISTENCE_AVAILABLE or _real_get_state_store is None:
        return None
    try:
        return _real_get_state_store()
    except Exception:
        return None


@dataclass
class _UserState:
    """Per-user session state for multi-user isolation.

    ``is_listening`` and ``last_transcript`` are user-specific — each
    SaaS user has their own voice pipeline state.
    """

    is_listening: bool = False
    last_transcript: str = ""


@dataclass
class AppState:
    """
    Thread-safe application state container.

    Extracted from the legacy ``viola_main`` entry point so that both the Qt
    desktop client and the web stack can share backend lifecycle/runtime state.

    ``is_playing`` remains device-level — it represents physical speaker
    output on the host machine.  ``is_listening`` and ``last_transcript``
    are per-user and live only in ``_UserState`` entries keyed by user_id.
    The top-level fields are retained as inert compatibility defaults for
    old smoke tests; voice code must use the user-scoped methods.
    """

    start_time: float = field(default_factory=time.time)
    version: str = field(default="test")
    platform: str = field(default_factory=lambda: sys.platform)
    last_transcript: str = ""
    is_listening: bool = False
    is_playing: bool = False
    runtime_profile: str = "desktop_full"
    runtime_capabilities: dict[str, Any] = field(default_factory=dict)
    restart_counters: dict[str, int] = field(default_factory=dict)
    breadcrumbs: list[str] = field(default_factory=list)
    _lock: threading.Lock | None = None
    ready_event: threading.Event | None = None
    _persistence: Any = field(default=None, init=False, repr=False)
    # Per-user session states for multi-user isolation
    _user_states: dict[str, _UserState] = field(default_factory=dict, init=False, repr=False)
    # Optional references for test fixtures and UI integration
    settings_manager: Any = field(default=None, repr=False)
    music_player: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._lock is None:
            object.__setattr__(self, "_lock", threading.Lock())
        if self.ready_event is None:
            object.__setattr__(self, "ready_event", threading.Event())
        store = _try_get_state_store()
        if store is not None:
            try:
                object.__setattr__(self, "_persistence", store)
                persisted = store.load_restart_counters()
                if persisted:
                    self.restart_counters.update(persisted)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("AppState persistence initialisation failed: %r", exc)

    _MAX_USER_STATES: int = 1000  # Hard cap on tracked user sessions

    def get_user_state(self, user_id: str) -> _UserState:
        """Return per-user state, creating on first access.

        Caps the number of tracked user states at ``_MAX_USER_STATES``.
        When the cap is reached, evicts users that are not listening
        (idle users) before adding new ones.
        """
        resolved_user_id = _normalize_user_id(user_id, source="get_user_state user_id")
        if self._lock is None:
            raise RuntimeError("AppState lock not initialised")
        with self._lock:
            if resolved_user_id not in self._user_states:
                # Evict idle users if at capacity
                if len(self._user_states) >= self._MAX_USER_STATES:
                    idle_ids = [uid for uid, us in self._user_states.items() if not us.is_listening]
                    # Remove oldest-inserted idle users (dict preserves insertion order)
                    excess = len(self._user_states) - self._MAX_USER_STATES + 1
                    for uid in idle_ids[:excess]:
                        del self._user_states[uid]
                self._user_states[resolved_user_id] = _UserState()
            return self._user_states[resolved_user_id]

    def set_listening(self, value: bool, user_id: str | None = None) -> None:
        """Thread-safe state mutation for listening flag.

        The caller must provide user_id; userless listening state is a
        cross-tenant bleed hazard on shared backends.
        """
        resolved_user_id = _normalize_user_id(user_id, source="set_listening user_id")
        if self._lock is None:
            raise RuntimeError("AppState lock not initialised")
        with self._lock:
            us = self._user_states.setdefault(resolved_user_id, _UserState())
            us.is_listening = value

    def set_playing(self, value: bool) -> None:
        """Thread-safe state mutation for playing flag.

        This is device-level — represents physical speaker output.
        """
        if self._lock is None:
            raise RuntimeError("AppState lock not initialised")
        with self._lock:
            self.is_playing = value

    def set_last_transcript(self, transcript: str, user_id: str | None = None) -> None:
        """Set the last transcript, scoped to a user when provided."""
        resolved_user_id = _normalize_user_id(user_id, source="set_last_transcript user_id")
        if self._lock is None:
            raise RuntimeError("AppState lock not initialised")
        with self._lock:
            us = self._user_states.setdefault(resolved_user_id, _UserState())
            us.last_transcript = transcript

    def is_user_listening(self, user_id: str) -> bool:
        """Return the listening flag for one user."""
        return self.get_user_state(user_id).is_listening

    def get_last_transcript(self, user_id: str) -> str:
        """Return the last transcript for one user."""
        return self.get_user_state(user_id).last_transcript

    def mark_ready(self) -> None:
        """Mark the service as ready to serve requests."""
        if self.ready_event:
            self.ready_event.set()

    def mark_not_ready(self) -> None:
        """Mark the service as not ready (starting up or shutting down)."""
        if self.ready_event:
            self.ready_event.clear()

    def is_ready(self) -> bool:
        """Return True when the service is ready to serve requests."""
        return bool(self.ready_event and self.ready_event.is_set())

    def increment_restart_counter(self, source: str) -> int:
        """
        Increment and return the restart counter for a monitored component.

        Thread-safe to ensure supervisor/watchdog threads can update counters
        without racing with UI reads.
        """

        if self._lock is None:
            raise RuntimeError("AppState lock not initialised")
        with self._lock:
            counter = self.restart_counters.get(source, 0) + 1
            self.restart_counters[source] = counter
            persistence = self._persistence
        if persistence is not None:
            try:
                persistence.update_restart_counter(source, counter)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Failed to persist restart counter '%s': %r", source, exc)
        try:
            from diagnostics.runtime_metrics import get_runtime_metrics

            get_runtime_metrics().record_restart_counter(source, counter)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to publish restart counter metric '%s': %r", source, exc)
        return counter

    def append_breadcrumb(self, message: str, *, max_entries: int = 100) -> None:
        """
        Append a supervisor breadcrumb for UI visibility.

        Keeps only the most recent ``max_entries`` entries to avoid unbounded
        memory use over long-running sessions.
        """

        if self._lock is None:
            raise RuntimeError("AppState lock not initialised")
        with self._lock:
            self.breadcrumbs.append(message)
            if len(self.breadcrumbs) > max_entries:
                del self.breadcrumbs[0 : len(self.breadcrumbs) - max_entries]
