"""
Monitoring client for health checks and system status.
Handles backend health, weather, and configuration endpoints.
"""

from __future__ import annotations

import time
from typing import Any

from core.constants import TIMEOUT_DEFAULT, TIMEOUT_SHUTDOWN
from core.logging_config import get_logger
from ui.qt_native.startup_types import HealthStatus

from .api_client_mixin import APIClientMixin

logger = get_logger(__name__)

_STARTUP_HEALTH_TIMEOUT_SECONDS = 5.0
_STARTUP_HEALTH_GRACE_SECONDS = 90.0
_STARTUP_HEALTH_LOG_BACKOFF_SECONDS = 10.0


class MonitorClient(APIClientMixin):
    """Client for monitoring and status operations."""

    def __init__(self, session):
        self.session = session
        self.base_url = None  # Set by parent client
        self.backend_ready = False
        self._config_snapshot: dict[str, Any] | None = None
        self._config_snapshot_at: float = 0.0
        self._startup_health_deadline = time.monotonic() + _STARTUP_HEALTH_GRACE_SECONDS
        self._last_startup_health_error_log_at: float = 0.0
        self._last_startup_health_error_key: tuple[str, str] | None = None
        self._suppressed_startup_health_errors = 0
        self._startup_health_failure_logged = False
        self._startup_health_success_logged = False

    def _effective_health_timeout(self, endpoint: str, timeout: float) -> float:
        if (
            endpoint in {"/health", "/health/ready", "/health/details", "/health/startup"}
            and not self.backend_ready
            and time.monotonic() < self._startup_health_deadline
        ):
            return max(float(timeout), _STARTUP_HEALTH_TIMEOUT_SECONDS)
        return timeout

    def _log_startup_health_error(
        self,
        *,
        endpoint: str,
        exc_name: str,
        timeout: float,
        elapsed_ms: float,
        message: str,
    ) -> None:
        key = (endpoint, exc_name)
        now = time.monotonic()
        if not self._startup_health_failure_logged:
            self._startup_health_failure_logged = True
            self._last_startup_health_error_key = key
            self._last_startup_health_error_log_at = now
            self._suppressed_startup_health_errors = 0
            logger.warning(
                "Health status check failed during startup: %s endpoint=%s timeout=%.1fs elapsed=%.0fms "
                "base_url=%s message=%s",
                exc_name,
                endpoint,
                timeout,
                elapsed_ms,
                self.base_url,
                message,
            )
            return

        if key != self._last_startup_health_error_key:
            self._last_startup_health_error_key = key
            self._last_startup_health_error_log_at = now
            self._suppressed_startup_health_errors = 0
            logger.debug(
                "Health status startup failure changed: %s endpoint=%s timeout=%.1fs elapsed=%.0fms "
                "base_url=%s message=%s",
                exc_name,
                endpoint,
                timeout,
                elapsed_ms,
                self.base_url,
                message,
            )
            return

        if now - self._last_startup_health_error_log_at >= _STARTUP_HEALTH_LOG_BACKOFF_SECONDS:
            count = self._suppressed_startup_health_errors + 1
            window_s = now - self._last_startup_health_error_log_at
            self._last_startup_health_error_log_at = now
            self._suppressed_startup_health_errors = 0
            logger.debug(
                "Health status startup failures coalesced: %s x%d over last %.0fs endpoint=%s "
                "timeout=%.1fs base_url=%s last_elapsed=%.0fms",
                exc_name,
                count,
                window_s,
                endpoint,
                timeout,
                self.base_url,
                elapsed_ms,
            )
            return
        self._suppressed_startup_health_errors += 1

    def _log_startup_health_success(self, status: HealthStatus) -> None:
        if self._startup_health_success_logged:
            return
        self._startup_health_success_logged = True
        suppressed = self._suppressed_startup_health_errors
        self._suppressed_startup_health_errors = 0
        logger.info(
            "Health status check succeeded: endpoint=%s http=%s status=%s latency=%.0fms suppressed_failures=%d",
            status.details.get("endpoint"),
            status.http_status,
            status.coerce_status(),
            status.latency_ms or 0.0,
            suppressed,
        )

    def _weather_error_envelope(self) -> dict[str, Any]:
        """Return standardized weather error envelope."""
        return {
            "ok": False,
            "status": "error",
            "error": {
                "code": "weather_unavailable",
                "message": "Weather temporarily unavailable",
            },
            "data": {},
        }

    def get_weather(self) -> dict[str, Any] | None:
        """Fetch current weather information."""
        endpoint = "/v1/weather"
        try:
            response = self.session.get(f"{self.base_url}{endpoint}", timeout=TIMEOUT_SHUTDOWN)
            # Don't raise on non-2xx - backend now always returns 200 with status field
            if response.status_code >= 500:
                logger.debug(
                    "Weather endpoint returned %s - treating as unavailable",
                    response.status_code,
                )
                return self._weather_error_envelope()
            payload = response.json()
            envelope = self._normalise_envelope(payload)
            if envelope is None:
                logger.debug("Weather endpoint returned invalid envelope - treating as unavailable")
                return self._weather_error_envelope()
            # Return full envelope so UI can check ok/error status
            return dict(envelope)
        except Exception as exc:
            logger.debug("Weather fetch failed: %s", exc)
            return self._weather_error_envelope()

    def get_weather_async(self, *, on_result=None, on_error=None) -> Any | None:
        """Asynchronous wrapper for get_weather."""

        return self._submit_worker(
            self.get_weather,
            on_result=on_result,
            on_error=on_error,
        )

    def health_check(self) -> bool:
        """Check if backend is alive."""
        endpoint = "/health/ready"
        self._emit_api_event(
            "api_request_started",
            {"endpoint": endpoint, "method": "GET"},
        )
        try:
            status = self.get_health_status(endpoint=endpoint, timeout=TIMEOUT_DEFAULT)
            payload = {
                "endpoint": endpoint,
                "method": "GET",
                "status": status.http_status,
                "status_token": status.coerce_status(),
            }
            status_ready = bool(status.ready)
            self.backend_ready = status_ready
            if status_ready:
                self._emit_api_event("api_request_finished", payload)
            else:
                payload["error"] = status.coerce_status()
                self._emit_api_event("api_request_failed", payload)
            return status_ready
        except Exception as exc:
            self.backend_ready = False
            self._emit_api_event(
                "api_request_failed",
                {
                    "endpoint": endpoint,
                    "method": "GET",
                    "error": type(exc).__name__,
                },
            )
            return False

    def health_check_async(self, *, on_result=None, on_error=None) -> Any | None:
        """Run health_check on a worker thread."""

        return self._submit_worker(self.health_check, on_result=on_result, on_error=on_error)

    def get_health_status(self, *, endpoint: str = "/health/details", timeout: float = TIMEOUT_DEFAULT) -> HealthStatus:
        """Fetch backend health metadata and normalise into HealthStatus."""
        url = f"{self.base_url}{endpoint}"
        started = time.perf_counter()
        try:
            effective_timeout = self._effective_health_timeout(endpoint, timeout)
            response = self.session.get(url, timeout=effective_timeout)
            latency_ms = (time.perf_counter() - started) * 1000
            try:
                payload = response.json() if response.content else {}
            except ValueError:
                payload = {}
            ready = response.status_code == 200 and bool(payload.get("ready", payload.get("status") == "ok"))
            status = payload.get("status", "ok" if ready else "starting")
            if isinstance(payload, dict):
                payload.setdefault("endpoint", endpoint)
            else:
                payload = {"endpoint": endpoint}
            status = HealthStatus(
                ready=ready,
                status=status,
                details=payload,
                http_status=response.status_code,
                latency_ms=latency_ms,
            )
            self.backend_ready = ready
            if ready:
                self._log_startup_health_success(status)
            return status
        except Exception as exc:
            # Connection timeouts/refusals during startup are expected; coalesce them.
            # Only use exception() (with stack trace) for unexpected errors
            exc_name = type(exc).__name__
            is_expected_startup_error = exc_name in (
                "ConnectTimeout",
                "ConnectionRefusedError",
                "ConnectionError",
                "MaxRetryError",
                "ReadTimeout",
            )
            if is_expected_startup_error:
                elapsed_ms = (time.perf_counter() - started) * 1000
                self._log_startup_health_error(
                    endpoint=endpoint,
                    exc_name=exc_name,
                    timeout=self._effective_health_timeout(endpoint, timeout),
                    elapsed_ms=elapsed_ms,
                    message=str(exc),
                )
            else:
                logger.warning("Health status check failed: %s: %s", exc_name, exc)
            return self._create_health_error_status(endpoint, exc_name, str(exc))

    def get_health_details(self) -> HealthStatus:
        """Convenience wrapper for /health/details endpoint."""
        status = self.get_health_status(endpoint="/health/details", timeout=TIMEOUT_DEFAULT)
        if status.details.get("endpoint") != "/health/details":
            fallback = self.get_health_status(endpoint="/health", timeout=TIMEOUT_DEFAULT)
            if fallback.ready or fallback.http_status:
                return fallback
        return status

    def get_startup_status(self) -> dict[str, Any] | None:
        """Return startup timing telemetry from the backend."""
        try:
            response = self.session.get(
                f"{self.base_url}/health/startup",
                timeout=self._effective_health_timeout("/health/startup", TIMEOUT_DEFAULT),
            )
            if response.status_code == 200:
                payload = response.json()
                return payload if isinstance(payload, dict) else None
        except Exception as exc:
            logger.debug("Startup status check failed: %s", exc)
        return None

    def _initial_health_probe(self) -> bool:
        """Probe backend health with exponential backoff."""
        for attempt in range(3):
            if self.health_check():
                return True
            if attempt < 2:
                time.sleep(0.5)
        logger.warning("Backend health probe failed")
        return False

    def get_public_config(
        self,
        *,
        timeout: float = 2.0,
        use_cache: bool = True,
        max_age: float | None = 30.0,
    ) -> dict[str, Any] | None:
        """Fetch a sanitized configuration snapshot from the backend."""
        import copy

        if use_cache and self._config_snapshot is not None and max_age is not None:
            age = time.time() - self._config_snapshot_at
            if age <= max_age:
                try:
                    return copy.deepcopy(self._config_snapshot)
                except Exception as e:
                    logger.exception("Failed to deepcopy config snapshot: %s", e)
                    return self._config_snapshot

        try:
            response = self.session.get(f"{self.base_url}/config", timeout=timeout)
            response.raise_for_status()
        except Exception as e:
            logger.exception("Failed to fetch config from backend: %s", e)
            return None

        try:
            payload = response.json()
        except ValueError:
            return None

        envelope = self._normalise_envelope(payload)
        if not envelope:
            return None
        if not bool(envelope.get("ok", False)):
            return None

        data = envelope.get("data")
        if not isinstance(data, dict):
            return None

        self._config_snapshot = data
        self._config_snapshot_at = time.time()
        try:
            return copy.deepcopy(data)
        except Exception as e:
            logger.exception("Failed to deepcopy config data: %s", e)
            return data

    def _create_health_error_status(self, endpoint: str, error_type: str, message: str) -> HealthStatus:
        """Create HealthStatus for error conditions."""
        return HealthStatus(
            ready=False,
            status="unreachable",
            details={
                "endpoint": endpoint,
                "error": error_type,
                "message": message,
                "base_url": self.base_url,
            },
            http_status=0,
            latency_ms=None,
        )


__all__ = ["MonitorClient"]
