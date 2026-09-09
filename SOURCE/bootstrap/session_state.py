"""Claude-compatible session/control-plane state for the bootstrap surface.

Parity reference: ``bootstrap/state.ts:60-175, 1419-1458`` in the Claude
source tree. Claude's bootstrap maintains a SessionState object distinct
from "service bootstrap" — it carries:

* model usage / lineage / per-session model overrides
* bypass-permission scope tracking
* scheduled and session cron task records
* plan/auto-mode exit markers
* registered hook state
* plugin hook clearing toggles

Viola's :class:`bootstrap.factory.BootstrapFactory` initializes app
*services* (state DB, FastAPI, voice, music) — those are persistent
across users and not what Claude's SessionState models. This module is
the missing in-memory analogue: per-process session/control-plane state
that hooks, slash commands, and the agent loop can read and mutate
without dragging in the heavyweight service singletons.

The state is intentionally simple and ephemeral. Anything durable
(settings, cost tracking, conversation history) already lives in its own
canonical store; SessionState only carries the runtime control-plane
view a CLI shell needs to coordinate with hooks and slash commands.

Multi-tenant note: SessionState is per user. Cloud requests resolve the
user from ``core.user_context`` and fail closed when no user is bound.
Desktop-only startup code may fall back to the stable device user id.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from core.logging_config import get_logger

logger = get_logger(__name__)


PermissionScope = Literal["none", "tool", "session", "until-restart"]


@dataclass
class ModelLineageEntry:
    """One transition in the active session's model lineage."""

    model: str
    source: str  # "settings", "command", "hook", "compact", "clear", "auto", ...
    timestamp: float
    reason: str | None = None


@dataclass
class ScheduledTaskRecord:
    """A scheduled or session-cron task registered with the session.

    This is the control-plane view: an opaque task id, the cron spec, and
    the owner. The actual execution lives in the scheduler service.
    """

    task_id: str
    schedule: str  # cron expression or "session" for session-cron tasks
    owner: str  # user id or "session" sentinel
    created_at: float
    description: str | None = None


@dataclass
class SessionState:
    """Per-session control-plane state for the bootstrap surface."""

    session_id: str | None = None
    parent_session_id: str | None = None
    active_model: str | None = None
    model_lineage: list[ModelLineageEntry] = field(default_factory=list)
    permission_mode: str = "default"
    bypass_permission_scope: PermissionScope = "none"
    bypass_expires_at: float | None = None  # absolute timestamp when "session" scope expires
    plan_mode_active: bool = False
    auto_mode_active: bool = False
    plan_exit_marker: bool = False  # set by /clear or /compact to signal plan-mode reset
    auto_exit_marker: bool = False
    scheduled_tasks: dict[str, ScheduledTaskRecord] = field(default_factory=dict)
    registered_hooks: list[str] = field(default_factory=list)  # hook event names with at least one handler
    plugin_hooks_cleared: bool = False
    session_only_flags: dict[str, Any] = field(default_factory=dict)

    def record_model_change(
        self,
        model: str,
        *,
        source: str,
        reason: str | None = None,
    ) -> None:
        """Record a model transition in the lineage and update active model."""

        entry = ModelLineageEntry(
            model=model,
            source=source,
            timestamp=time.time(),
            reason=reason,
        )
        self.model_lineage.append(entry)
        self.active_model = model

    def clear_model_override(self, *, source: str, reason: str | None = None) -> None:
        """Record a return to the default configured model."""

        entry = ModelLineageEntry(
            model="default",
            source=source,
            timestamp=time.time(),
            reason=reason,
        )
        self.model_lineage.append(entry)
        self.active_model = None

    def enable_bypass(self, scope: PermissionScope, *, expires_at: float | None = None) -> None:
        """Promote bypass-permission scope. Wider scopes win."""

        order = {"none": 0, "tool": 1, "session": 2, "until-restart": 3}
        if order[scope] > order[self.bypass_permission_scope]:
            self.bypass_permission_scope = scope
        if expires_at is not None:
            self.bypass_expires_at = expires_at

    def set_permission_mode(self, mode: str) -> None:
        """Reflect the active permission mode in session control-plane state."""

        self.permission_mode = mode
        self.plan_mode_active = mode == "plan"
        if mode == "bypassPermissions":
            self.bypass_permission_scope = "until-restart"
            self.bypass_expires_at = None
        else:
            self.bypass_permission_scope = "none"
            self.bypass_expires_at = None

    def bypass_active(self, *, now: float | None = None) -> bool:
        """Return True if bypass-permission is currently in effect."""

        if self.bypass_permission_scope == "none":
            return False
        if self.bypass_expires_at is None:
            return True
        return (now or time.time()) < self.bypass_expires_at

    def register_hook(self, event_name: str) -> None:
        if event_name not in self.registered_hooks:
            self.registered_hooks.append(event_name)

    def clear_registered_hooks(self) -> None:
        self.registered_hooks.clear()

    def clear_plugin_hooks(self) -> None:
        """Mark plugin hooks as cleared (handlers should be reloaded later)."""

        self.plugin_hooks_cleared = True

    def add_scheduled_task(self, record: ScheduledTaskRecord) -> None:
        self.scheduled_tasks[record.task_id] = record

    def remove_scheduled_task(self, task_id: str) -> ScheduledTaskRecord | None:
        return self.scheduled_tasks.pop(task_id, None)

    def reset_session_only(self) -> None:
        """Clear ephemeral session-only flags and exit markers (/clear path)."""

        self.session_only_flags.clear()
        self.plan_exit_marker = False
        self.auto_exit_marker = False

    def start_new_session(self, session_id: str, *, parent_session_id: str | None = None) -> None:
        """Switch to a fresh session id after a clear/new boundary."""

        self.session_id = session_id
        self.parent_session_id = parent_session_id
        self.session_only_flags.clear()
        self.plan_mode_active = False
        self.auto_mode_active = False
        self.plan_exit_marker = True
        self.auto_exit_marker = True

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot for diagnostics / /status output."""

        return {
            "session_id": self.session_id,
            "parent_session_id": self.parent_session_id,
            "active_model": self.active_model,
            "model_lineage": [
                {
                    "model": entry.model,
                    "source": entry.source,
                    "timestamp": entry.timestamp,
                    "reason": entry.reason,
                }
                for entry in self.model_lineage
            ],
            "permission_mode": self.permission_mode,
            "bypass_permission_scope": self.bypass_permission_scope,
            "bypass_expires_at": self.bypass_expires_at,
            "plan_mode_active": self.plan_mode_active,
            "auto_mode_active": self.auto_mode_active,
            "plan_exit_marker": self.plan_exit_marker,
            "auto_exit_marker": self.auto_exit_marker,
            "scheduled_tasks": [
                {
                    "task_id": record.task_id,
                    "schedule": record.schedule,
                    "owner": record.owner,
                    "created_at": record.created_at,
                    "description": record.description,
                }
                for record in self.scheduled_tasks.values()
            ],
            "registered_hooks": list(self.registered_hooks),
            "plugin_hooks_cleared": self.plugin_hooks_cleared,
            "session_only_flags": dict(self.session_only_flags),
        }


_LOCK = threading.RLock()
_SESSION_STATES_BY_USER: dict[str, SessionState] = {}


def _is_cloud_surface() -> bool:
    try:
        from config.settings import settings
    except (ImportError, RuntimeError, AttributeError, ValueError):
        return False
    surface = str(getattr(settings, "app_surface", "desktop") or "desktop").strip().lower()
    deployment = str(getattr(settings, "deployment_mode", "") or "").strip().lower()
    return surface == "cloud" or deployment == "cloud"


def _normalize_user_id(value: str | None, *, source: str) -> str:
    user_id = str(value or "").strip()
    if not user_id:
        raise ValueError("SessionState requires a non-empty user_id from %s" % source)
    return user_id


def _resolve_user_id(user_id: str | None = None) -> str:
    """Resolve the SessionState owner from explicit or ambient identity."""

    if user_id is not None:
        return _normalize_user_id(user_id, source="explicit user_id")
    try:
        from core.user_context import get_current_user_id

        return _normalize_user_id(get_current_user_id(), source="current user context")
    except (LookupError, ValueError) as exc:
        if _is_cloud_surface():
            raise RuntimeError("SessionState requires authenticated user_id on cloud surface") from exc
    try:
        from core.user_context import get_device_user_id

        return _normalize_user_id(get_device_user_id(), source="device user_id")
    except Exception as exc:
        raise RuntimeError("SessionState requires user_id or desktop device user_id") from exc


def get_session_state(*, user_id: str | None = None) -> SessionState:
    """Return the current user's session control-plane state."""

    resolved_user_id = _resolve_user_id(user_id)
    with _LOCK:
        state = _SESSION_STATES_BY_USER.get(resolved_user_id)
        if state is None:
            state = SessionState()
            _SESSION_STATES_BY_USER[resolved_user_id] = state
        return state


def reset_session_state(*, session_id: str | None = None, user_id: str | None = None) -> SessionState:
    """Reset one user's session state. Returns the fresh state.

    Called by /clear and bootstrap teardown. Preserves the new
    ``session_id`` when provided so the next turn knows which session it
    is starting under.
    """

    resolved_user_id = _resolve_user_id(user_id)
    with _LOCK:
        state = SessionState(session_id=session_id)
        _SESSION_STATES_BY_USER[resolved_user_id] = state
        return state


def update_session_state(updates: Mapping[str, Any], *, user_id: str | None = None) -> SessionState:
    """Apply a small update dict to the current user's session state."""

    with _LOCK:
        state = get_session_state(user_id=user_id)
        for key, value in updates.items():
            if hasattr(state, key):
                setattr(state, key, value)
        return state


def clear_all_session_hook_state() -> None:
    """Clear hook metadata from every user-scoped SessionState."""

    with _LOCK:
        for state in _SESSION_STATES_BY_USER.values():
            state.clear_registered_hooks()
            state.clear_plugin_hooks()


__all__ = [
    "ModelLineageEntry",
    "PermissionScope",
    "ScheduledTaskRecord",
    "SessionState",
    "clear_all_session_hook_state",
    "get_session_state",
    "reset_session_state",
    "update_session_state",
]
