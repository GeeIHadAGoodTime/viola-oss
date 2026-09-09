from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from core.logging_config import get_logger
from fastapi import Request
from routing.health import (
    UI_STALLED_STATUS,
    build_basic_health_payload,
    qt_event_loop_block,
    qt_loop_stalled,
    register_health_routes,
)
from ui.api.context import ApiContext

log = get_logger(__name__)

# The dependency block probes real subsystems (PortAudio enumeration under the
# process-wide lock, a state-store read, LLM provider selection). Several
# clients poll /health/details, so without de-duplication N concurrent probes
# do N full sweeps and hold N worker threads. One in-flight sweep at a time,
# reused for a couple of seconds, keeps that bounded. The age is reported in
# the payload so a reader is never guessing how fresh the numbers are.
_DEPENDENCY_SNAPSHOT_TTL_S = 2.0
_DEPENDENCY_LOCK = threading.Lock()
_DEPENDENCY_SNAPSHOT: tuple[float, dict[str, Any]] | None = None


def _dependency_snapshot(collect: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], int]:
    """Return the dependency block plus how old it is, in milliseconds.

    Callers arrive on a worker thread (``routing.health`` offloads sync health
    providers), so a plain lock is the right primitive here. Holding it across
    the sweep is deliberate: it collapses a burst of concurrent probes into one
    sweep instead of letting each one take its own PortAudio lock turn.
    """
    global _DEPENDENCY_SNAPSHOT
    with _DEPENDENCY_LOCK:
        cached = _DEPENDENCY_SNAPSHOT
        now = time.monotonic()
        if cached is not None:
            age_s = now - cached[0]
            if 0 <= age_s < _DEPENDENCY_SNAPSHOT_TTL_S:
                return cached[1], int(age_s * 1000)
        dependencies = collect()
        _DEPENDENCY_SNAPSHOT = (time.monotonic(), dependencies)
        return dependencies, 0


def reset_dependency_snapshot() -> None:
    """Drop the cached dependency block (tests, and process re-init)."""
    global _DEPENDENCY_SNAPSHOT
    with _DEPENDENCY_LOCK:
        _DEPENDENCY_SNAPSHOT = None


def _startup_surface() -> dict[str, Any]:
    """Report whether the API route table is finished mounting.

    Readiness flips true when the FastAPI lifespan completes, but a large part
    of the route surface is mounted afterwards by post-bind initializers. A
    caller asking "can Viola do the thing I am about to try?" needs to know
    that, so it is reported here rather than left implicit.
    """
    try:
        from diagnostics.startup_telemetry import snapshot

        snap = snapshot()
    except (ImportError, AttributeError, RuntimeError):
        log.debug("Startup telemetry unavailable for health details", exc_info=True)
        return {"available": False}
    subsystems = snap.get("subsystems") or {}
    return {
        "available": True,
        "route_surface_complete": bool(snap.get("route_surface_complete", True)),
        "pending_route_initializers": list(snap.get("pending_route_initializers") or []),
        "port_bound_ms": snap.get("port_bound_ms"),
        "process_uptime_ms": snap.get("process_uptime_ms"),
        "subsystem_states": {
            str(name): str((entry or {}).get("state", "unknown")) for name, entry in sorted(subsystems.items())
        },
        "failed_subsystems": sorted(
            str(name) for name, entry in subsystems.items() if (entry or {}).get("state") == "failed"
        ),
    }


def _health_error(message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "error", "message": message, **extra}


def _health_degraded(message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "degraded", "message": message, **extra}


def _check_database_health() -> dict[str, Any]:
    """Check database/persistence layer health."""
    try:
        from services.persistence.state_store import get_state_store

        store = get_state_store()
        # Verify we can read from the store.
        # get_setting requires (user_id, key) after auth refactor.
        store.get_setting("__system__", "health_check", default=None)
        return {"status": "ok"}
    except ImportError:
        return {"status": "ok", "note": "persistence_optional"}
    except Exception:
        log.exception("Database health check failed")
        return _health_error("Database health check is temporarily unavailable.")


def _check_audio_device_health() -> dict[str, Any]:
    """Check audio device availability."""
    try:
        import sounddevice as sd

        from audio_core.portaudio_guard import sounddevice_guard

        with sounddevice_guard():
            devices = sd.query_devices()
        input_devices = [d for d in devices if d.get("max_input_channels", 0) > 0]
        output_devices = [d for d in devices if d.get("max_output_channels", 0) > 0]
        return {
            "status": "ok",
            "input_devices": len(input_devices),
            "output_devices": len(output_devices),
        }
    except ImportError:
        return _health_degraded(
            "Audio device checks are unavailable on this system.",
            note="sounddevice_not_installed",
        )
    except Exception:
        log.exception("Audio device health check failed")
        return _health_error("Audio device health check is temporarily unavailable.")


def _check_audio_capture_health() -> dict[str, Any]:
    """Check the hub-side audio capture provider (ProcTap / equivalent).

    Without this check a ProcTap startup failure is only logged.  The
    hub keeps running, but every spoke receives silence indefinitely
    because the capture callback that feeds the ChunkStamper never
    fires.  Surfacing the state here turns the silent-failure mode
    into a visible /health/details condition.
    """
    try:
        from audio_core.streaming.pipeline_wiring import get_capture_health

        snapshot = get_capture_health()
        state = snapshot.get("state", "not_started")

        if state == "failed":
            return _health_error(
                "Hub audio capture provider failed to start.",
                provider=snapshot.get("provider"),
                error=snapshot.get("error"),
                since_epoch=snapshot.get("since_epoch"),
            )
        if state == "degraded":
            # get_capture_provider() silently auto-selected TestToneProvider
            # because no real capture provider was available (#2598). The
            # source pipeline runs fine, but every spoke is receiving a
            # synthetic 440Hz tone instead of real system audio -- a
            # fake-success shape that used to be logger-warning-only.
            return _health_degraded(
                "Hub audio capture is using a synthetic test tone, not real system audio "
                "(no real capture provider was available on this platform).",
                provider=snapshot.get("provider"),
                reason=snapshot.get("fallback_reason") or "no_capture_provider_available",
                since_epoch=snapshot.get("since_epoch"),
            )
        if state == "ok":
            return {
                "status": "ok",
                "provider": snapshot.get("provider"),
                "since_epoch": snapshot.get("since_epoch"),
            }
        # "not_started" is a valid resting state (spoke-only builds,
        # pre-setup), surface it as OK with a note so operators know
        # why there's no provider.
        return {"status": "ok", "note": "capture_not_started"}
    except ImportError:
        return {"status": "ok", "note": "pipeline_wiring_unavailable"}
    except Exception:
        log.exception("Audio capture health check failed")
        return _health_error("Audio capture health check is temporarily unavailable.")


def _check_audio_output_health() -> dict[str, Any]:
    """Check the device-side audio output driver (sounddevice / equivalent).

    Without this check, a missing ``sounddevice`` install silently falls
    back to ``NullAudioOutput`` -- every write() call "succeeds" while all
    PCM is discarded, so the device produces no sound with nothing
    operator-visible distinguishing it from a real, working speaker.
    Surfacing the state here turns that silent-failure mode into a visible
    /health/details condition (peer of ``_check_audio_capture_health``, #2598).
    """
    try:
        from audio_core.streaming.pipeline_wiring import get_output_health

        snapshot = get_output_health()
        state = snapshot.get("state", "not_started")

        if state == "degraded":
            return _health_degraded(
                "Device audio output is discarding all audio (no real sound driver available); "
                "playback will report success but produce NO SOUND.",
                provider=snapshot.get("provider"),
                reason=snapshot.get("fallback_reason") or "sounddevice_unavailable",
                since_epoch=snapshot.get("since_epoch"),
            )
        if state == "ok":
            return {
                "status": "ok",
                "provider": snapshot.get("provider"),
                "since_epoch": snapshot.get("since_epoch"),
            }
        # "not_started" is a valid resting state (hub-only builds,
        # pre-setup), surface it as OK with a note so operators know
        # why there's no driver.
        return {"status": "ok", "note": "output_not_started"}
    except ImportError:
        return {"status": "ok", "note": "pipeline_wiring_unavailable"}
    except Exception:
        log.exception("Audio output health check failed")
        return _health_error("Audio output health check is temporarily unavailable.")


def _check_music_backend_health(music: Any) -> dict[str, Any]:
    """Check music backend health."""
    try:
        if music is None:
            return _health_degraded(
                "Music playback has not finished initializing.",
                note="music_not_initialized",
            )
        is_materialized = getattr(music, "is_materialized", None)
        if callable(is_materialized) and not is_materialized():
            return _health_degraded(
                "Music playback is still warming.",
                note="music_lazy_initializing",
            )

        # The health probe runs with no request principal (its path skips
        # auth), so bind the desktop active principal before touching player
        # internals — player attribute reads resolve per-user state.
        from core.user_context import desktop_active_principal_scope

        with desktop_active_principal_scope():
            # Unwrap adapter wrapper to reach the actual player with _backend
            player = getattr(music, "player", music)
            backend = getattr(player, "_backend", None)
            if backend is None:
                return _health_degraded(
                    "Music playback is unavailable right now.",
                    note="no_backend",
                )

            backend_type = type(backend).__name__
            is_playing = getattr(backend, "is_playing", lambda: False)()

        return {
            "status": "ok",
            "backend_type": backend_type,
            "is_playing": is_playing,
        }
    except Exception:
        log.exception("Music backend health check failed")
        return _health_error("Music backend health check is temporarily unavailable.")


def _check_wake_detector_health() -> dict[str, Any]:
    """Check wake detector availability."""
    try:
        from voice.wake_detector.facade import WakeDetectorFacade

        facade = WakeDetectorFacade.get_instance()
        if facade is None:
            return _health_degraded(
                "Wake detection has not finished initializing.",
                reason="not_initialized",
                is_running=False,
            )
        if facade.is_circuit_open():
            return _health_degraded(
                "Wake detection is recovering from a recent failure.",
                reason="circuit_open",
                is_running=False,
            )
        # Health is answered by the detection loop's own work signal, never by
        # is_available() (which only says the object was constructed) or
        # is_running() (true for a thread parked forever in a blocking device
        # read). This branch used to report status "ok" whenever _impl existed
        # -- and reported "ok" even when is_running() was False -- so an app
        # that had gone permanently deaf still passed its own health check.
        from services.liveness import Health

        health = facade.health()
        if health is Health.WORKING:
            return {"status": "ok", "is_running": True}
        if health is Health.STOPPED:
            return {"status": "ok", "is_running": False, "reason": "stopped"}
        if health is Health.STALLED:
            return _health_degraded(
                "Wake detection stopped responding and is being restarted.",
                reason="stalled",
                is_running=False,
            )
        return _health_degraded(
            "Wake detection is currently unavailable.",
            reason="not_available",
            is_running=False,
        )
    except Exception:
        log.exception("Wake detector health check failed")
        return _health_error(
            "Wake detection health check is temporarily unavailable.",
            is_running=False,
        )


def _check_stt_health() -> dict[str, Any]:
    """Check STT transcriber availability."""
    try:
        from voice.transcription.factory import get_transcriber

        transcriber = get_transcriber()
        if transcriber is None:
            return _health_degraded(
                "Speech recognition has not finished initializing.",
                reason="not_initialized",
            )
        if hasattr(transcriber, "is_available") and not transcriber.is_available():
            return _health_degraded(
                "Speech recognition is still loading required resources.",
                reason="model_not_loaded",
            )
        return {"status": "ok"}
    except Exception:
        log.exception("STT health check failed")
        return _health_degraded(
            "Speech recognition health check is temporarily unavailable.",
            reason="health_check_failed",
        )


def _check_llm_health() -> dict[str, Any]:
    """Check LLM provider availability."""
    try:
        from services.llm.factory import ManagedLLMAuthRequired, get_llm_handler

        handler = get_llm_handler()
        if handler is None:
            return _health_degraded(
                "Language features have not finished initializing.",
                reason="not_initialized",
            )
        fallback_status = None
        get_fallback_status = getattr(handler, "get_fallback_status", None)
        if callable(get_fallback_status):
            fallback_status = get_fallback_status()
        provider_name = handler.get_provider_name() if hasattr(handler, "get_provider_name") else "unknown"
        available = handler.is_available() if hasattr(handler, "is_available") else False
        result: dict[str, Any] = {
            "status": "ok" if available else "degraded",
            "provider": provider_name,
        }
        if fallback_status:
            result["fallback"] = fallback_status
            if fallback_status.get("degraded"):
                active = fallback_status.get("active_provider") or {}
                result.update(
                    {
                        "status": "degraded",
                        "message": "LLM: degraded - running on backup provider",
                        "reason": "llm_provider_fallback",
                        "active_provider": active.get("name") or provider_name,
                    }
                )
        if not available:
            result["message"] = "Language features are temporarily unavailable."
        return result
    except ManagedLLMAuthRequired as exc:
        data = getattr(exc, "data", {}) or {}
        message = str(data.get("message") or "Sign in to use Viola-managed AI.")
        return _health_degraded(
            message,
            reason=getattr(exc, "error_code", "account_required"),
            recovery_hint="Sign in or switch AI source to BYOK, Codex, or local.",
        )
    except Exception:
        log.exception("LLM health check failed")
        return _health_error("Language service health check is temporarily unavailable.")


def _check_circuit_breaker_health() -> dict[str, Any]:
    """Check circuit breaker status across subsystems.

    Returns status of all known circuit breakers:
    - health_monitor: API client health circuit breaker
    - command_client: Command API circuit breaker
    - wake_detector: Wake detector circuit breaker (if using facade)
    """
    breakers: dict[str, Any] = {}

    # Check health monitor circuit breaker
    try:
        from ui.qt_native.health_monitor import HealthMonitor

        # Get singleton instance if it exists
        monitor = getattr(HealthMonitor, "_instance", None)
        if monitor is not None and hasattr(monitor, "_circuit_breaker"):
            cb = monitor._circuit_breaker
            breakers["health_monitor"] = {
                "state": cb.state.value,
                "consecutive_failures": cb.metrics.consecutive_failures,
            }
    except Exception as e:
        log.debug("Health monitor CB check failed: %s", e)

    # Check command client circuit breaker
    try:
        from ui.qt_native.command_client import CommandClient

        client = getattr(CommandClient, "_instance", None)
        if client is not None and hasattr(client, "_circuit_breaker"):
            cb = client._circuit_breaker
            breakers["command_client"] = {
                "state": cb.state.value,
                "consecutive_failures": cb.metrics.consecutive_failures,
            }
    except Exception as e:
        log.debug("Command client CB check failed: %s", e)

    # Check wake detector circuit breaker (via facade)
    try:
        from voice.wake_detector.facade import WakeDetectorFacade

        facade = WakeDetectorFacade.get_instance()
        if facade is not None:
            breakers["wake_detector"] = {
                "state": "open" if facade.is_circuit_open() else "closed",
            }
    except Exception as e:
        log.debug("Wake detector CB check failed: %s", e)

    # Determine overall status
    has_open = any(b.get("state") == "open" for b in breakers.values())
    has_half_open = any(b.get("state") == "half_open" for b in breakers.values())

    if has_open:
        status = "degraded"
    elif has_half_open:
        status = "recovering"
    else:
        status = "ok"

    return {
        "status": status,
        "breakers": breakers,
    }


def _check_cookie_persistence_health() -> dict[str, Any]:
    """Check Qt WebEngine cookie persistence (Layer 2 regression guard).

    The Qt profile lives in the same process but is created on the Qt thread.
    We read a cached boolean that is set at profile creation time, avoiding
    any cross-thread Qt object access.
    """
    try:
        from ui.qt_native.webview_window import is_profile_persistent

        result = is_profile_persistent()
        if result is None:
            # Qt window hasn't initialized yet — not an error
            return {"status": "ok", "note": "qt_profile_pending"}
        if result:
            return {"status": "ok"}
        return _health_error(
            "Browser session storage is not persisting correctly.",
            note="off_the_record_profile",
        )
    except ImportError:
        # Qt not available (headless / test / Linux without PyQt6)
        return {"status": "ok", "note": "qt_not_available"}
    except Exception:
        log.exception("Cookie persistence health check failed")
        return _health_degraded(
            "Browser session storage checks are temporarily unavailable.",
            note="service_unavailable",
        )


def register_health(context: ApiContext) -> None:
    from fastapi.responses import JSONResponse

    router = context.router
    app = context.app
    state = context.bindings.state
    music = context.bindings.music

    @app.get("/v1/ready")
    async def readiness_check():
        """Readiness probe for load balancers. Returns 503 during startup/shutdown."""
        if not state or not _health_ready():
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return {"status": "ready"}

    def _health_ready() -> bool:
        ready_attr = getattr(state, "is_ready", None)
        if callable(ready_attr):
            try:
                return bool(ready_attr())
            except Exception as exc:  # pragma: no cover
                log.debug("Health readiness check failed: %s", exc)
                return False
        return False

    def _status_provider(_: Request | None = None) -> dict[str, Any]:
        payload = build_basic_health_payload(state=state)
        from diagnostics.voice_status import get_voice_status

        payload["voice_status"] = get_voice_status()
        return payload

    def _collect_dependencies() -> dict[str, Any]:
        return {
            # Listed first because it is the one dependency whose death makes
            # every other green answer here meaningless: the app the user is
            # looking at is frozen. This process reported "ok" through an
            # entire outage before #4650 because nothing asked the message
            # loop whether it was still dispatching.
            "qt_event_loop": qt_event_loop_block(),
            "database": _check_database_health(),
            "audio_device": _check_audio_device_health(),
            "audio_capture": _check_audio_capture_health(),
            "audio_output": _check_audio_output_health(),
            "music_backend": _check_music_backend_health(music),
            "wake_detector": _check_wake_detector_health(),
            "stt_transcriber": _check_stt_health(),
            "llm_provider": _check_llm_health(),
            "circuit_breakers": _check_circuit_breaker_health(),
            "cookie_persistence": _check_cookie_persistence_health(),
        }

    def _details_provider(_: Request | None = None) -> dict[str, Any]:
        payload = _status_provider()

        dependencies, age_ms = _dependency_snapshot(_collect_dependencies)
        payload["dependencies"] = dependencies
        payload["dependencies_age_ms"] = age_ms

        # Calculate overall status based on dependencies
        has_error = any(d.get("status") == "error" for d in dependencies.values())
        has_degraded = any(d.get("status") == "degraded" for d in dependencies.values())

        if has_error:
            payload["status"] = "error"
        elif has_degraded:
            payload["status"] = "degraded"
        else:
            payload["status"] = "ok"

        # A caller asking "can Viola do the thing I am about to try?" also
        # needs to know whether the endpoint for that thing has even been
        # mounted yet. Post-bind initializers mount a large part of the API
        # after the port is bound and after readiness flips true, so an "ok"
        # here with routes still landing would be a false green.
        startup = _startup_surface()
        payload["startup"] = startup
        if not startup.get("route_surface_complete", True) and payload["status"] == "ok":
            payload["status"] = "degraded"
            payload["message"] = "Viola is still mounting parts of its API; some features are not available yet."

        # A frozen UI outranks every other reason this payload could be
        # unhappy, and it gets its own status word so a watchdog can route it
        # without reading prose (#4650).
        loop_block = dependencies.get("qt_event_loop") or {}
        if qt_loop_stalled(loop_block):
            payload["status"] = UI_STALLED_STATUS
            payload["message"] = str(loop_block.get("reason") or "The Viola window has stopped responding.")

        return payload

    register_health_routes(
        router,
        ready_check=_health_ready,
        status_provider=_status_provider,
        details_provider=_details_provider,
    )


__all__ = ["register_health", "reset_dependency_snapshot"]
