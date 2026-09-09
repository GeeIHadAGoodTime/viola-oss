from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from PySide6.QtCore import QObject, QTimer, Signal

from config import env
from core.constants import LOCALHOST, TIMEOUT_DEFAULT, TIMEOUT_MEDIUM
from core.logging_config import get_logger
from ui.qt_native.api_client import ViolaAPIClient
from ui.qt_native.startup_types import HealthStatus, StartupPolicy, StartupState

logger = get_logger(__name__)

# Seconds between connection-probe retries in _wait_for_port
_PORT_POLL_INTERVAL_S: float = 0.4

# Delay before checking whether deferred voice init succeeded
_VOICE_HEALTH_CHECK_DELAY_S: float = 10.0


class _EmitDebugEventProtocol(Protocol):
    """Protocol matching emit_debug_event signature."""

    def __call__(self, name: str, payload: dict[str, object] | None = None, *, source: str = "qt") -> None: ...


_GetDebugBusFn = Callable[[], object]

_emit_debug_event_fn: _EmitDebugEventProtocol | None = None
_get_debug_bus_fn: _GetDebugBusFn | None = None

try:  # pragma: no cover - optional instrumentation
    from ui.qt_native.debug_events import (
        emit_debug_event as _imported_emit,
        get_debug_bus as _imported_get_bus,
    )

    _emit_debug_event_fn = _imported_emit
    _get_debug_bus_fn = _imported_get_bus
except Exception as e:  # pragma: no cover - environments without Qt debug bus
    logger.exception("Debug events module not available: %s", e)


def _emit_debug(signal_name: str, payload: dict[str, Any]) -> None:
    if _get_debug_bus_fn is not None:  # pragma: no branch - hot path
        try:
            bus = _get_debug_bus_fn()
            if bus:
                signal = getattr(bus, signal_name, None)
                emit = getattr(signal, "emit", None)
                if callable(emit):
                    emit(payload)
                    return
        except Exception as e:  # pragma: no cover - instrumentation must never fail
            logger.exception("Debug bus emit failed: %s", e)

    if _emit_debug_event_fn is not None:
        try:
            _emit_debug_event_fn(signal_name, payload, source="qt")
        except Exception as e:  # pragma: no cover - best-effort logging
            logger.exception("Debug event emit failed: %s", e)


@dataclass(slots=True)
class BackendStartupResult:
    boot: Any | None = None
    error: str | None = None
    detail: str | None = None


def _resolve_voice_preferences() -> dict[str, bool]:
    """
    Resolve voice preferences from SettingsManager.

    IMPORTANT: Bootstrap (factory.py) is the SINGLE source of truth for wake config.
    We derive wake intent purely from voice_mode here - bootstrap will do the actual
    resource check and write the final values to settings.

    DO NOT read wake_enabled from SettingsManager - that creates split truth.
    Instead, pass the INTENT (voice_mode == "wake_word") to bootstrap.
    """
    from ui.settings_manager import get_settings_manager

    settings_mgr = get_settings_manager()
    voice_mode = settings_mgr.get("voice_mode", "push_to_talk")

    # voice_enabled: any mode except "disabled"
    # wake_enabled: derive from voice_mode ONLY (not from SettingsManager!)
    # Bootstrap will check resource availability and set the final values
    if voice_mode == "disabled":
        voice_enabled = False
        wake_enabled = False
    else:
        voice_enabled = True
        # CRITICAL FIX: Derive wake_enabled purely from voice_mode
        # This ensures we pass correct intent to bootstrap, avoiding split truth
        wake_enabled = voice_mode == "wake_word"

    logger.info(
        "🎤 Voice preferences: voice=%s, wake=%s (mode=%s)",
        voice_enabled,
        wake_enabled,
        voice_mode,
    )

    return {"voice": voice_enabled, "wake": wake_enabled}


def _resolve_probe_hosts(host: str) -> tuple[str, ...]:
    normalized = (host or "").strip()
    stripped = normalized.strip("[]")
    lowered = stripped.lower()

    # rationale: binding to wildcard hosts (0.0.0.0/::) cannot be probed directly; prefer loopback.
    if lowered in {"", "*", "0.0.0.0"}:  # nosec B104
        preferred = [LOCALHOST, "localhost"]
    elif lowered in {"::", "::0", "::ffff:0.0.0.0"}:
        preferred = ["::1", LOCALHOST, "localhost"]
    else:
        preferred = [stripped or LOCALHOST]
        if lowered == "localhost":
            preferred.append(LOCALHOST)

    if stripped and stripped not in preferred:
        preferred.append(stripped)

    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in preferred:
        candidate = candidate.strip()
        if not candidate:
            continue
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return tuple(ordered or (LOCALHOST,))


def _probe_port(host: str, port: int) -> bool:
    try:
        addr_info = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        logger.debug(
            "Port readiness probe failed to resolve %s:%s: %s",
            host,
            port,
            exc,
        )
        return False

    for family, socktype, proto, _canonname, sockaddr in addr_info:
        try:
            with socket.socket(family, socktype, proto) as sock:
                sock.settimeout(TIMEOUT_DEFAULT)
                if sock.connect_ex(sockaddr) == 0:
                    return True
        except OSError as exc:
            logger.debug(
                "Port readiness probe connect failed for %s:%s (%s)",
                host,
                port,
                exc,
            )
    return False


def _wait_for_port(host: str, port: int, timeout_s: float, phase: str) -> bool:
    start = time.monotonic()
    attempt = 0
    probe_hosts = _resolve_probe_hosts(host)

    while time.monotonic() - start < timeout_s:
        attempt += 1
        for probe_host in probe_hosts:
            elapsed = time.monotonic() - start
            _emit_debug(
                "backend_connection_attempt",
                {
                    "client": "qt",
                    "phase": phase,
                    "attempt": attempt,
                    "elapsed_s": round(elapsed, 2),
                    "probe_host": probe_host,
                },
            )
            if _probe_port(probe_host, port):
                _emit_debug(
                    "backend_connection_result",
                    {
                        "client": "qt",
                        "phase": phase,
                        "success": True,
                        "attempts": attempt,
                        "elapsed_s": round(elapsed, 2),
                        "probe_host": probe_host,
                    },
                )
                return True
        time.sleep(_PORT_POLL_INTERVAL_S)

    _emit_debug(
        "backend_connection_result",
        {
            "client": "qt",
            "phase": phase,
            "success": False,
            "attempts": attempt,
            "elapsed_s": round(time.monotonic() - start, 2),
            "probe_hosts": list(probe_hosts),
        },
    )
    return False


def _wire_deferred_aec(boot: Any) -> None:
    """Wire AEC reference to the wake detector after all components are ready.

    Called after VoiceOrchestrator.start() to catch cases where AEC wasn't
    wired during start() due to ChunkStamper not being available at that
    moment (e.g. import-time value capture of _active_chunk_stamper).

    By this point, ChunkStamper was created during create_fastapi_app()
    and is definitely available via get_active_chunk_stamper().
    """
    voice_orch = getattr(boot, "voice", None)
    if voice_orch is None:
        return

    # Check if AEC is already wired
    if getattr(voice_orch, "aec_reference_source", None) is not None:
        logger.debug("AEC already wired via VoiceOrchestrator.start()")
        return

    # Get the wake detector from the voice pipeline
    pipeline = getattr(voice_orch, "voice_pipeline", None)
    wake_detector = getattr(pipeline, "wake_detector", None)
    if wake_detector is None:
        return

    try:
        from core.voice_orchestrator import create_and_start_aec_adapter

        adapter = create_and_start_aec_adapter()
        if adapter is not None:
            voice_orch.aec_reference_source = adapter
            voice_orch._owns_aec_adapter = True
            # Wire to the wake detector
            wire_fn = getattr(wake_detector, "wire_aec_reference", None)
            if callable(wire_fn):
                success = wire_fn(adapter)
                if success:
                    logger.info("AEC_DEFERRED_WIRED: AEC reference wired to wake detector (deferred)")
                else:
                    logger.debug("AEC deferred wiring: already wired or not supported")
            else:
                logger.debug("Wake detector has no wire_aec_reference method")
        else:
            logger.warning("AEC_DEFERRED_FAILED: No AEC adapter available")
    except Exception as exc:
        logger.warning("AEC deferred wiring failed: %s", exc)


def _start_backend(host: str, port: int, policy: StartupPolicy) -> BackendStartupResult:
    logger.info("🚀 Starting Viola backend on %s:%s", host, port)
    logger.info("🔧 Startup policy: %s", policy)

    # CRITICAL: Log the exact host/port being used to catch mismatches
    logger.info("📍 _start_backend called with host=%s, port=%s", host, port)

    # Verify environment variables match
    env_host = env.get("VIOLA_HOST", "NOT_SET")
    env_port = env.get("VIOLA_PORT", "NOT_SET")
    env_api_port = env.get("VIOLA_API_PORT", "NOT_SET")
    logger.info(
        "🔍 Environment check: VIOLA_HOST=%s, VIOLA_PORT=%s, VIOLA_API_PORT=%s",
        env_host,
        env_port,
        env_api_port,
    )

    # Verify settings match
    try:
        from config import settings

        settings_host = getattr(settings, "api_host", "NOT_SET")
        settings_port = getattr(settings, "api_port", "NOT_SET")
        settings_base_url = getattr(settings, "base_url", "NOT_SET")
        logger.info(
            "🔍 Settings check: api_host=%s, api_port=%s, base_url=%s",
            settings_host,
            settings_port,
            settings_base_url,
        )
    except Exception as settings_err:
        logger.warning("⚠️ Could not check settings: %s", settings_err)

    prefs = _resolve_voice_preferences()
    try:
        from backend.runtime import (
            bootstrap,
            run_uvicorn,
        )  # lazy: avoid 3s import chain at module level

        logger.info(
            "🔧 Calling bootstrap(host=%s, port=%s, voice=%s, wake=%s)",
            host,
            port,
            prefs["voice"],
            prefs["wake"],
        )
        boot = bootstrap(
            host=host,
            port=port,
            voice=prefs["voice"],
            wake=prefs["wake"],
        )
        logger.info("✅ Bootstrap completed")
        logger.info("🌐 Starting API server with run_uvicorn(host=%s, port=%s)…", host, port)
        try:
            # run_uvicorn now waits for server to be ready and raises RuntimeError on failure
            boot.uvicorn_server = run_uvicorn(boot.app, host, port)
            from config.settings import settings as _cfg

            _sch = "https" if _cfg.ssl_enabled else "http"
            logger.info(
                "✅ API server started and verified ready at %s://%s:%s",
                _sch,
                host,
                port,
            )
        except RuntimeError as server_err:
            # Server startup failed - provide detailed error
            error_msg = str(server_err)
            logger.error("❌ Server startup failed: %s", error_msg)
            return BackendStartupResult(
                boot=None,
                error="ServerStartupFailed",
                detail=error_msg,
            )
        except Exception as server_err:
            # Unexpected error during server startup
            logger.exception("❌ Unexpected error during server startup")
            return BackendStartupResult(
                boot=None,
                error=server_err.__class__.__name__,
                detail=f"Unexpected error: {server_err}",
            )

        # Server is now ready (run_uvicorn verified it), but we still check port for compatibility
        # This is now redundant but kept for backward compatibility with health probing
        socket_ready = _wait_for_port(
            host,
            port,
            timeout_s=min(5.0, policy.backend_start_timeout_s),
            phase="backend_verify",
        )
        if not socket_ready:
            # This shouldn't happen since run_uvicorn verified readiness, but log it
            logger.warning("⚠️ Port verification failed after server reported ready")
            # Still return success since server verified itself
            # return BackendStartupResult(
            #     boot=boot,
            #     error=None,
            #     detail="backend_socket_unconfirmed",
            # )

        if prefs["voice"]:

            def _create_and_start_voice() -> None:
                """Create VoiceOrchestrator post-startup — nobody speaks before UI loads."""
                try:
                    from bootstrap.factory import BootstrapFactory

                    player_for_voice = getattr(boot.music, "player", None) if boot.music else None
                    if player_for_voice is None and hasattr(boot.music, "materialize"):
                        materialized_music = boot.music.materialize()
                        player_for_voice = getattr(materialized_music, "player", None)
                    voice_orch = BootstrapFactory.create_voice_orchestrator(
                        boot.state,
                        boot.intent,  # LazyIntentBridge — triggers materialization
                        player_for_voice,
                        boot.tts,
                        wake_enabled=prefs["wake"],
                    )
                    if voice_orch is not None:
                        boot.voice = voice_orch
                        if getattr(boot, "app", None) is not None:
                            boot.app.state.voice_orchestrator = voice_orch
                        voice_orch.start()
                        _wire_deferred_aec(boot)
                        logger.info("✅ Voice orchestrator created and started (deferred)")
                    else:
                        logger.warning("⚠️ Voice orchestrator creation returned None")
                except Exception as voice_err:
                    logger.warning("⚠️ Deferred voice init failed: %s", voice_err)

            threading.Thread(
                target=_create_and_start_voice,
                daemon=True,
                name="deferred-voice-init",
            ).start()

            # Schedule a delayed health check to catch silent voice init failures.
            # If the orchestrator hasn't materialised after the delay, emit a
            # diagnostic event so viola-audio-debug and the UI can surface it.
            def _voice_health_check() -> None:
                if getattr(boot, "voice", None) is not None:
                    return  # Initialised successfully — stay silent
                logger.warning(
                    "VOICE_INIT_HEALTH: Voice orchestrator not initialised %.0fs "
                    "after launch — wake-word detection may be unavailable",
                    _VOICE_HEALTH_CHECK_DELAY_S,
                )
                _emit_debug(
                    "voice_error",
                    {
                        "message": ("Voice orchestrator failed to initialise within health-check window"),
                        "source": "startup_controller",
                        "component": "voice_orchestrator",
                        "phase": "deferred_health_check",
                        "delay_s": _VOICE_HEALTH_CHECK_DELAY_S,
                    },
                )

            health_timer = threading.Timer(
                _VOICE_HEALTH_CHECK_DELAY_S,
                _voice_health_check,
            )
            health_timer.daemon = True
            health_timer.name = "voice-init-health-check"
            health_timer.start()

        # AEC wiring handled inside _create_and_start_voice for deferred path

        return BackendStartupResult(boot=boot)
    except Exception as exc:
        logger.exception("❌ Backend startup failed")
        return BackendStartupResult(boot=None, error=exc.__class__.__name__, detail=str(exc))


HealthProbeCallable = Callable[[], HealthStatus]


class StartupCoordinator(QObject):
    """
    Orchestrates backend startup and health probing for the Qt application.

    The coordinator centralises state transitions, ensuring the UI can react
    without blocking the event loop with modal dialogs.
    """

    state_changed = Signal(str, dict)
    health_updated = Signal(dict)
    ready = Signal(object)
    degraded = Signal(dict)
    failed = Signal(str, dict)
    health_result = Signal(object)

    def __init__(
        self,
        *,
        api_client: ViolaAPIClient,
        host: str,
        port: int,
        policy: StartupPolicy | None = None,
        health_probe: HealthProbeCallable | None = None,
    ) -> None:
        super().__init__()
        self.api_client = api_client
        self.host = host
        self.port = port
        self.policy = policy or StartupPolicy()
        self._state = StartupState.IDLE
        self._backend = None
        self._health_timer: QTimer | None = None
        self._backend_thread: threading.Thread | None = None
        self._start_time: float = 0.0
        # Monotonic timestamp of the RUNNING -> DEGRADED edge, or None while the
        # backend is answering. Kept so a degraded window is measurable rather
        # than merely announced once.
        self._degraded_since: float | None = None
        self._readiness_retry_count = 0
        self._readiness_failure_reported = False
        self._health_probe = health_probe or self._default_health_probe
        self._health_worker_active = False
        # A retry may begin while the previous bounded HTTP probe is still
        # unwinding.  Tagging its result prevents that stale answer from
        # changing the newly started retry cycle.
        self._health_generation = 0
        self.health_result.connect(self._handle_health_result)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._backend_thread and self._backend_thread.is_alive():
            logger.debug("StartupCoordinator already running")
            return

        self._health_generation += 1
        self._health_worker_active = False
        self._start_time = time.monotonic()
        self._degraded_since = None
        self._readiness_retry_count = 0
        self._readiness_failure_reported = False
        self._set_state(
            StartupState.STARTING_BACKEND,
            {
                "host": self.host,
                "port": self.port,
                "policy": {
                    "backend_start_timeout_s": self.policy.backend_start_timeout_s,
                    "readiness_timeout_s": self.policy.readiness_timeout_s,
                    "initial_probe_delay_ms": self.policy.initial_probe_delay_ms,
                    "readiness_backoff_ms": list(self.policy.readiness_backoff_ms),
                    "passive_poll_interval_ms": self.policy.passive_poll_interval_ms,
                },
            },
        )

        self._backend_thread = threading.Thread(
            target=self._run_backend_worker, name="ViolaBackendStartup", daemon=True
        )
        self._backend_thread.start()

        self._ensure_timer_running()

    def backend(self) -> Any | None:
        return self._backend

    def state(self) -> StartupState:
        return self._state

    def retry(self) -> bool:
        """Retry a terminal startup failure without weakening readiness policy.

        A readiness timeout can happen after the backend process has already
        booted.  In that case re-open the same bounded health-probe cycle instead
        of starting a duplicate backend on the same port.  If backend boot itself
        failed, a regular ``start`` is the safe retry path.
        """
        if self._state != StartupState.FAILED:
            return False

        self.api_client.backend_ready = False
        if self._backend is None:
            logger.info("Retrying failed backend startup")
            self.start()
            return True

        self._health_generation += 1
        self._health_worker_active = False
        self._start_time = time.monotonic()
        self._degraded_since = None
        self._readiness_retry_count = 0
        self._readiness_failure_reported = False
        self._set_state(
            StartupState.WAITING_READY,
            {
                "host": self.host,
                "port": self.port,
                "retry": True,
                "detail": "retrying_readiness_probe",
            },
        )
        self._ensure_timer_running()
        logger.info("Retrying backend readiness probe without restarting the active backend")
        return True

    def stop(self) -> None:
        if self._health_timer:
            self._health_timer.stop()
        if self._backend_thread and self._backend_thread.is_alive():
            logger.debug("Waiting for backend thread to finish…")
            self._backend_thread.join(timeout=TIMEOUT_MEDIUM)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _ensure_timer_running(self) -> None:
        if self._health_timer is None:
            self._health_timer = QTimer(self)
            self._health_timer.setSingleShot(True)
            self._health_timer.timeout.connect(self._queue_health_probe)
        self._schedule_health_probe(self.policy.initial_probe_delay_ms)

    def _schedule_health_probe(self, interval_ms: int) -> None:
        if self._health_timer is None or self._state == StartupState.FAILED:
            return
        self._health_timer.start(max(1, int(interval_ms)))

    def _next_readiness_delay_ms(self, elapsed_s: float) -> int:
        delays = self.policy.readiness_backoff_ms or (30000,)
        index = min(self._readiness_retry_count, len(delays) - 1)
        self._readiness_retry_count += 1
        delay_ms = int(delays[index])
        remaining_ms = max(1, int((self.policy.readiness_timeout_s - elapsed_s) * 1000))
        return min(delay_ms, remaining_ms)

    def _fail_readiness_timeout(self, status: HealthStatus, elapsed_s: float) -> None:
        if self._readiness_failure_reported or self._state == StartupState.FAILED:
            return
        self._readiness_failure_reported = True
        if self._health_timer is not None:
            self._health_timer.stop()
        summary = status.as_summary()
        detail = "Backend did not report ready from %s within %.1fs" % (
            self.policy.health_ready_endpoint,
            self.policy.readiness_timeout_s,
        )
        payload = {
            "health": summary,
            "elapsed_s": round(elapsed_s, 2),
            "timeout_s": self.policy.readiness_timeout_s,
            "error": "readiness_timeout",
            "detail": detail,
        }
        self._set_state(StartupState.FAILED, payload)
        self.failed.emit("readiness_timeout", payload)
        logger.error(
            "Backend readiness timed out after %.1fs endpoint=%s status=%s http=%s",
            elapsed_s,
            self.policy.health_ready_endpoint,
            status.status,
            status.http_status,
        )

    def _run_backend_worker(self) -> None:
        logger.debug(
            "[DIAG_STARTUP] _run_backend_worker BEGIN host=%s port=%s",
            self.host,
            self.port,
        )
        t0 = time.monotonic()
        result = _start_backend(self.host, self.port, self.policy)
        elapsed_ms = (time.monotonic() - t0) * 1000
        if result.boot is not None:
            logger.debug(
                "[DIAG_STARTUP] _run_backend_worker SUCCESS in %.0fms (detail=%s)",
                elapsed_ms,
                result.detail,
            )
            self._backend = result.boot
            self._set_state(
                StartupState.WAITING_READY,
                {
                    "detail": result.detail or "backend_started",
                    "host": self.host,
                    "port": self.port,
                },
            )
            _emit_debug(
                "app_starting",
                {
                    "client": "qt",
                    "phase": "backend_started",
                    "detail": result.detail,
                },
            )
            return

        logger.debug(
            "[DIAG_STARTUP] _run_backend_worker FAILED in %.0fms error=%s detail=%s",
            elapsed_ms,
            result.error,
            result.detail,
        )
        error_payload = {
            "host": self.host,
            "port": self.port,
            "error": result.error or "unknown_error",
            "detail": result.detail,
        }
        self._set_state(StartupState.FAILED, error_payload)
        self.failed.emit("backend_start", error_payload)
        logger.error(
            "Backend startup failed: error=%s detail=%s",
            result.error,
            result.detail,
        )

    def _queue_health_probe(self) -> None:
        if self._state == StartupState.FAILED:
            return
        elapsed = time.monotonic() - self._start_time if self._start_time else 0.0
        # readiness_timeout applies to the STARTUP window only. Once the backend
        # has answered ready at least once, a later outage is DEGRADED (still
        # polled, still recoverable) -- routing it into FAILED here would stop
        # the probe timer and strand the UI in a state with no way back.
        post_startup = self._state in (StartupState.RUNNING, StartupState.DEGRADED)
        if not post_startup and elapsed >= self.policy.readiness_timeout_s:
            self._fail_readiness_timeout(
                HealthStatus(
                    ready=False,
                    status="timeout",
                    details={
                        "endpoint": self.policy.health_ready_endpoint,
                        "error": "readiness_timeout",
                        "message": "Backend readiness probe timed out before the next retry",
                    },
                    http_status=0,
                ),
                elapsed,
            )
            return
        if self._health_worker_active:
            return
        self._health_worker_active = True
        generation = self._health_generation

        def _worker() -> None:
            try:
                status = self._health_probe()
            except Exception as exc:  # pragma: no cover - diagnostics only
                logger.debug("Health probe raised: %s", exc)
                status = HealthStatus(
                    ready=False,
                    status="error",
                    details={
                        "endpoint": getattr(self.policy, "health_details_endpoint", ""),
                        "error": exc.__class__.__name__,
                        "message": str(exc),
                    },
                    http_status=0,
                )
            self.health_result.emit((generation, status))

        thread = threading.Thread(target=_worker, name="ViolaStartupHealthProbe", daemon=True)
        thread.start()

    def _handle_health_result(self, result: HealthStatus | tuple[int, HealthStatus | None] | None) -> None:
        generation = self._health_generation
        status = result
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
            generation, status = result
        if generation != self._health_generation:
            logger.debug("Ignoring stale startup health result from generation %s", generation)
            return
        self._health_worker_active = False
        if status is None:
            status = HealthStatus(
                ready=False,
                status="unknown",
                details={"endpoint": getattr(self.policy, "health_details_endpoint", "")},
                http_status=0,
            )
        self._apply_health_status(status)

    def _apply_health_status(self, status: HealthStatus) -> None:
        if self._state == StartupState.FAILED:
            return
        logger.debug(
            "[DIAG_STARTUP] _apply_health_status ready=%s http=%s status=%s state=%s details=%s",
            status.ready,
            status.http_status,
            status.status,
            self._state.value,
            status.details,
        )
        summary = status.as_summary()
        self.health_updated.emit(summary)
        if status.ready:
            self._readiness_retry_count = 0
            if self._state != StartupState.RUNNING:
                recovering = self._state == StartupState.DEGRADED
                # Measure BEFORE resetting the clock, or the number is always 0.0.
                elapsed_s = round(time.monotonic() - self._start_time, 2)
                self.api_client.backend_ready = True
                self._start_time = time.monotonic()
                self._degraded_since = None
                self._set_state(
                    StartupState.RUNNING,
                    {
                        "health": summary,
                        "elapsed_s": elapsed_s,
                        "recovered_from_degraded": recovering,
                    },
                )
                if self._backend is not None:
                    self.ready.emit(self._backend)
            self._schedule_health_probe(self.policy.passive_poll_interval_ms)
            return

        self.api_client.backend_ready = False
        elapsed = time.monotonic() - self._start_time
        # A backend that answered once and has now stopped answering is NOT
        # still "running". Before this branch existed the state machine had no
        # edge out of RUNNING, so the UI kept reporting a healthy backend for as
        # long as the process lived, no matter what the probe came back with --
        # the exact shape that let a release ship unable to answer a command.
        # DEGRADED is recoverable (the probe keeps polling and the ready branch
        # above restores RUNNING) and deliberately does NOT fall through to the
        # readiness-timeout failure path, because FAILED is terminal: it stops
        # the probe timer, so a transient outage would strand the UI in a state
        # it could never leave even after the backend came back.
        if self._state in (StartupState.RUNNING, StartupState.DEGRADED):
            if self._state == StartupState.RUNNING:
                self._degraded_since = time.monotonic()
                payload = {
                    "health": summary,
                    "http_status": status.http_status,
                    "backend_status": status.status,
                    "ran_for_s": round(elapsed, 2),
                }
                self._set_state(StartupState.DEGRADED, payload)
                self.degraded.emit(payload)
            # Already degraded: health_updated above carries the live probe each
            # poll, so do not re-announce the transition on every tick.
            self._schedule_health_probe(self.policy.passive_poll_interval_ms)
            return
        if elapsed >= self.policy.readiness_timeout_s and self._state != StartupState.FAILED:
            self._fail_readiness_timeout(status, elapsed)
            return

        delay_ms = self._next_readiness_delay_ms(elapsed)
        logger.debug(
            "[DIAG_STARTUP] readiness retry scheduled in %sms attempt=%s endpoint=%s",
            delay_ms,
            self._readiness_retry_count,
            self.policy.health_ready_endpoint,
        )
        self._schedule_health_probe(delay_ms)

    def _default_health_probe(self) -> HealthStatus:
        endpoint = (
            self.policy.health_details_endpoint
            if self._state == StartupState.RUNNING
            else self.policy.health_ready_endpoint
        )
        probe_url = "%s%s" % (self.api_client.base_url, endpoint)
        t0 = time.monotonic()
        status = self.api_client.get_health_status(endpoint=endpoint, timeout=TIMEOUT_DEFAULT)
        latency_ms = (time.monotonic() - t0) * 1000
        logger.debug(
            "[DIAG_STARTUP] health_probe url=%s http=%s ready=%s latency=%.0fms body=%s",
            probe_url,
            status.http_status,
            status.ready,
            latency_ms,
            status.details,
        )
        if endpoint == self.policy.health_details_endpoint and status.details.get("endpoint") != endpoint:
            logger.debug(
                "[DIAG_STARTUP] health_probe endpoint mismatch, trying /health/live fallback",
            )
            fallback = self.api_client.get_health_status(endpoint="/health/live", timeout=TIMEOUT_DEFAULT)
            logger.debug(
                "[DIAG_STARTUP] health_probe fallback http=%s ready=%s body=%s",
                fallback.http_status,
                fallback.ready,
                fallback.details,
            )
            if fallback.ready or fallback.http_status:
                return fallback
        return status

    def _set_state(self, state: StartupState, payload: dict[str, Any] | None) -> None:
        if self._state == state and (payload or {}) == {}:
            return
        self._state = state
        payload = payload or {}
        payload.update({"state": state.value})
        logger.debug("Startup state -> %s (fields=%s)", state.value, sorted(payload.keys()))
        self.state_changed.emit(state.value, payload)


__all__ = ["StartupCoordinator"]
