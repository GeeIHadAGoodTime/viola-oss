"""
Subsystem kill-switches for the public launch.

What this is:
  A single, fail-closed source of truth for "is subsystem X currently
  serving traffic?" Each subsystem (phone, connectors, paid_checkout,
  voice, tools, telemetry, agent_browser, music, vision, agent, cloud_sync)
  is gated by a kill-switch.
  Kill-switches are stored in Redis when available (cross-instance,
  cross-restart) and fall back to in-process state. They are flippable
  in milliseconds by ops via the admin API without a redeploy.

Why a dedicated module:
  ``config/settings.py`` carries durable build/deploy preferences, and
  flipping a setting there requires a config push or a redeploy. The
  launch needs the inverse — a way to disable phone calls in 200ms when
  Telnyx is on fire, then re-enable it once the upstream is healed. That
  is what kill-switches are.

Design choices:

  * Fail-closed when Redis is configured but unreachable: a missing
    backend MUST NOT default to "enabled" for a kill-switched subsystem,
    or the entire mechanism is theatre. Single-instance dev mode (no
    Redis configured) is allowed to read the in-process map.

  * Subsystems are an explicit allowlist; new ones must be registered
    in ``SUBSYSTEMS`` below. Unknown names raise. This stops silent
    typo-by-typo drift between admin UI, code, and ops runbooks.

  * Every read is logged at DEBUG; every write produces an audit log at
    INFO with actor (admin user id), subsystem, new state, reason. We
    do not write secrets to logs.

  * The default for every subsystem is ENABLED. Flipping to disabled is
    an operational action with a recorded reason and TTL.

  * Disabled subsystems return HTTP 503 with body
    ``{"error": "subsystem_disabled", "subsystem": "<name>", "reason": "<reason>"}``
    and the ``Retry-After: <seconds>`` header equal to the remaining TTL.

Read-side usage in route handlers:

  from backend.launch_kill_switches import require_subsystem
  @router.post("/api/v1/phone/calls", dependencies=[Depends(require_subsystem("phone"))])
  async def make_call(...): ...

Write-side: admin route in ``backend.launch_kill_switches.create_router``
mounted by ``backend.cloud_app``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from threading import RLock
from typing import Any

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)


class KillSwitchStoreUnavailable(RuntimeError):
    """Raised when a configured kill-switch store cannot be read or written."""


# -----------------------------------------------------------------------------
# Subsystem registry
# -----------------------------------------------------------------------------

# Each entry is (name, reason_required, default_ttl_seconds_when_disabled).
SUBSYSTEMS: dict[str, dict[str, Any]] = {
    # Phone calls (Telnyx + Pipecat). Recurring failure: Telnyx outage
    # cascades into 8000+ failures/minute (per incident 2026-05-08).
    "phone": {"description": "Telnyx + Pipecat phone calls"},
    # External connectors (OAuth bridges, Calendar, Gmail, Drive).
    "connectors": {"description": "OAuth connector flows + provider APIs"},
    # Stripe checkout. Disabling this still allows /api/v1/billing/status
    # and webhooks; it only blocks new checkout sessions.
    "paid_checkout": {"description": "Stripe checkout session minting"},
    # Voice / wake-word / Pipecat capture in browser context.
    "voice": {"description": "WebSocket voice pipeline + wake"},
    # Cloud Playwright MCP browser session viewer/takeover stream.
    "agent_browser": {"description": "Cloud agent-browser stream and takeover WebSocket"},
    # Cloud music playback and cross-device player control. Disabling this
    # stops new state-changing play/control requests while leaving reads up.
    "music": {"description": "Cloud music playback and player control"},
    # Cloud screen-share/vision analysis. Disabling this stops image ingestion
    # and vision LLM calls while leaving unrelated cloud surfaces available.
    "vision": {"description": "Cloud screen-share and vision analysis"},
    # Background agent task creation. Disabling this short-circuits
    # /v1/command -> agent_executor; existing in-flight tasks keep
    # running. Use for cost runaway containment.
    "agent": {"description": "Background agent task execution"},
    # Tool dispatch generally (covers Gmail send, calendar create, SMS,
    # media playback). Coarser than ``agent``; intended for "stop all
    # side-effects" moments.
    "tools": {"description": "All side-effecting tool calls"},
    # Cloud sync push/pull (Tier-2). Disabling pauses background sync
    # while keeping reads working from desktop cache.
    "cloud_sync": {"description": "Tier-2 cloud sync push/pull"},
    # Opt-in telemetry send and public telemetry ingest. Disabling pauses
    # desktop uploads and rejects public ingest before rate-limit/body work.
    "telemetry": {"description": "Opt-in telemetry send + public ingest"},
}

ENV_KILL_PREFIX = "VIOLA_KILL_"  # VIOLA_KILL_PHONE=1 disables at boot.


# -----------------------------------------------------------------------------
# State
# -----------------------------------------------------------------------------


@dataclass
class _SwitchState:
    enabled: bool
    disabled_at: int = 0
    disabled_until: int = 0  # 0 == until manually re-enabled
    reason: str = ""
    actor: str = ""


class KillSwitchStore:
    """In-process + optional Redis-backed switch store."""

    _REDIS_KEY = "viola:kill_switches:v1"

    def __init__(self, *, redis_backend: Any | None = None) -> None:
        self._redis = redis_backend
        self._lock = RLock()
        self._cache: dict[str, _SwitchState] = {name: _SwitchState(enabled=True) for name in SUBSYSTEMS}
        self._last_redis_load: float = 0.0
        self._redis_error: str = ""
        self._apply_boot_env()

    def _apply_boot_env(self) -> None:
        """Allow ops to ship a container with a subsystem killed from boot."""
        for name in SUBSYSTEMS:
            env_name = ENV_KILL_PREFIX + name.upper()
            if os.environ.get(env_name, "").strip().lower() in {"1", "true", "yes"}:
                self._cache[name] = _SwitchState(
                    enabled=False,
                    disabled_at=int(time.time()),
                    reason="boot env %s set" % env_name,
                    actor="boot",
                )
                logger.warning("Kill-switch %s disabled at boot via %s", name, env_name)

    def _load_from_redis_locked(self) -> None:
        if self._redis is None:
            self._redis_error = ""
            return
        # Refresh at most every 0.5s to avoid hammering Redis under load.
        now = time.monotonic()
        if now - self._last_redis_load < 0.5:
            return
        try:
            raw = self._redis.get(self._REDIS_KEY)
            if not raw:
                self._last_redis_load = now
                self._redis_error = ""
                return
            payload = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
            for name in SUBSYSTEMS:
                if name not in payload:
                    continue
                entry = payload[name] or {}
                # Honor TTL: a switch with disabled_until in the past
                # auto-recovers (operator no longer has to remember).
                if entry.get("disabled_until") and entry["disabled_until"] <= time.time():
                    self._cache[name] = _SwitchState(enabled=True)
                else:
                    self._cache[name] = _SwitchState(
                        enabled=bool(entry.get("enabled", True)),
                        disabled_at=int(entry.get("disabled_at") or 0),
                        disabled_until=int(entry.get("disabled_until") or 0),
                        reason=str(entry.get("reason") or ""),
                        actor=str(entry.get("actor") or ""),
                    )
            self._last_redis_load = now
            self._redis_error = ""
        except Exception as exc:
            self._last_redis_load = now
            self._redis_error = "%s: %s" % (type(exc).__name__, exc)
            logger.exception("Failed to load kill switches from Redis: %s", exc)

    def _save_to_redis_locked(self) -> None:
        if self._redis is None:
            self._redis_error = ""
            return
        try:
            payload = {
                name: {
                    "enabled": state.enabled,
                    "disabled_at": state.disabled_at,
                    "disabled_until": state.disabled_until,
                    "reason": state.reason,
                    "actor": state.actor,
                }
                for name, state in self._cache.items()
            }
            self._redis.set(self._REDIS_KEY, json.dumps(payload))
            self._redis_error = ""
        except Exception as exc:
            self._redis_error = "%s: %s" % (type(exc).__name__, exc)
            logger.exception("Failed to persist kill switches to Redis: %s", exc)
            raise KillSwitchStoreUnavailable("kill-switch store unavailable") from exc

    def _redis_unavailable_state(self, subsystem: str) -> dict[str, Any]:
        return {
            "name": subsystem,
            "description": SUBSYSTEMS[subsystem].get("description", ""),
            "enabled": False,
            "disabled_at": int(time.time()),
            "disabled_until": 0,
            "reason": "kill-switch store unavailable",
            "actor": "system",
            "store_error": self._redis_error,
        }

    def is_enabled(self, subsystem: str) -> bool:
        if subsystem not in SUBSYSTEMS:
            raise ValueError("unknown subsystem: %s" % subsystem)
        with self._lock:
            self._load_from_redis_locked()
            if self._redis is not None and self._redis_error:
                logger.warning(
                    "Kill-switch store unavailable; failing closed for %s: %s",
                    subsystem,
                    self._redis_error,
                )
                return False
            state = self._cache[subsystem]
            # Auto-recover if TTL passed. ``<=`` so that a 1-second TTL
            # observed exactly at the boundary still recovers (avoiding
            # integer-truncation flakiness — int(time.time()) drops
            # subseconds, so a same-second comparison must count).
            if not state.enabled and state.disabled_until and state.disabled_until <= int(time.time()):
                logger.info("Kill-switch %s TTL elapsed, auto-recovering", subsystem)
                self._cache[subsystem] = _SwitchState(enabled=True)
                self._save_to_redis_locked()
                return True
            return state.enabled

    def get_state(self, subsystem: str) -> dict[str, Any]:
        if subsystem not in SUBSYSTEMS:
            raise ValueError("unknown subsystem: %s" % subsystem)
        with self._lock:
            self._load_from_redis_locked()
            if self._redis is not None and self._redis_error:
                return self._redis_unavailable_state(subsystem)
            state = self._cache[subsystem]
            return {
                "name": subsystem,
                "description": SUBSYSTEMS[subsystem].get("description", ""),
                "enabled": state.enabled,
                "disabled_at": state.disabled_at,
                "disabled_until": state.disabled_until,
                "reason": state.reason,
                "actor": state.actor,
            }

    def set_enabled(
        self,
        subsystem: str,
        *,
        enabled: bool,
        actor: str,
        reason: str = "",
        ttl_seconds: int = 0,
    ) -> dict[str, Any]:
        if subsystem not in SUBSYSTEMS:
            raise ValueError("unknown subsystem: %s" % subsystem)
        if not enabled and not reason:
            raise ValueError("reason is required to disable a subsystem")
        with self._lock:
            now = int(time.time())
            previous = self._cache[subsystem]
            if enabled:
                self._cache[subsystem] = _SwitchState(enabled=True, actor=actor)
            else:
                self._cache[subsystem] = _SwitchState(
                    enabled=False,
                    disabled_at=now,
                    disabled_until=(now + ttl_seconds) if ttl_seconds > 0 else 0,
                    reason=reason,
                    actor=actor,
                )
            try:
                self._save_to_redis_locked()
            except KillSwitchStoreUnavailable:
                self._cache[subsystem] = previous
                raise

        logger.info(
            "kill_switch %s -> enabled=%s actor=%s reason=%s ttl=%ds",
            subsystem,
            enabled,
            actor,
            reason or "-",
            ttl_seconds,
        )
        return self.get_state(subsystem)

    def all_states(self) -> list[dict[str, Any]]:
        return [self.get_state(name) for name in SUBSYSTEMS]


# Module-level singleton with lazy init. Real Redis backend is wired
# during cloud_app startup; tests can override before first access.
_STORE: KillSwitchStore | None = None
_STORE_LOCK = RLock()


def get_store() -> KillSwitchStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = KillSwitchStore(redis_backend=None)
        return _STORE


def configure_store(*, redis_backend: Any | None) -> KillSwitchStore:
    """Initialize/replace the singleton (idempotent)."""
    global _STORE
    with _STORE_LOCK:
        _STORE = KillSwitchStore(redis_backend=redis_backend)
        return _STORE


# -----------------------------------------------------------------------------
# Dependency for FastAPI route handlers
# -----------------------------------------------------------------------------


def require_subsystem(name: str):
    """Return a FastAPI dependency that 503s if the subsystem is killed.

    Usage:
      from backend.launch_kill_switches import require_subsystem
      @router.post(..., dependencies=[Depends(require_subsystem("phone"))])
    """
    if name not in SUBSYSTEMS:
        raise ValueError("require_subsystem: unknown subsystem %s" % name)

    async def _checker() -> None:
        store = get_store()
        if not store.is_enabled(name):
            state = store.get_state(name)
            retry_after = 0
            if state["disabled_until"]:
                retry_after = max(1, state["disabled_until"] - int(time.time()))
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "subsystem_disabled",
                    "subsystem": name,
                    "reason": state.get("reason", ""),
                    "retry_after": retry_after,
                },
                headers={"Retry-After": str(retry_after or 60)},
            )

    _checker.__name__ = "require_subsystem_%s" % name
    return _checker


# -----------------------------------------------------------------------------
# Admin router
# -----------------------------------------------------------------------------


def create_kill_switch_router():
    """FastAPI router for /admin/api/kill_switches.

    Mounted from ``backend/cloud_app.py``; auth is provided by the
    parent admin router's ``Depends(verify_admin_token)``.
    """
    router = APIRouter(prefix="/admin/api/kill_switches", tags=["kill_switches"])

    @router.get("")  # mt-ok: admin-only via cloud_app include_router verify_admin_token
    async def _list_all() -> dict[str, Any]:
        return {"subsystems": get_store().all_states()}

    @router.get("/{subsystem}")  # mt-ok: admin-only via cloud_app include_router verify_admin_token
    async def _get_one(subsystem: str) -> dict[str, Any]:
        try:
            return get_store().get_state(subsystem)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/{subsystem}/disable")  # mt-ok: admin-only via cloud_app include_router verify_admin_token
    async def _disable(subsystem: str, request: Request) -> dict[str, Any]:
        body = await request.json()
        reason = (body or {}).get("reason", "")
        ttl = int((body or {}).get("ttl_seconds", 0))
        actor = getattr(request.state, "admin_user_id", "") or "admin"
        try:
            return get_store().set_enabled(subsystem, enabled=False, reason=reason, ttl_seconds=ttl, actor=actor)
        except KillSwitchStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/{subsystem}/enable")  # mt-ok: admin-only via cloud_app include_router verify_admin_token
    async def _enable(subsystem: str, request: Request) -> dict[str, Any]:
        actor = getattr(request.state, "admin_user_id", "") or "admin"
        try:
            return get_store().set_enabled(subsystem, enabled=True, actor=actor)
        except KillSwitchStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
