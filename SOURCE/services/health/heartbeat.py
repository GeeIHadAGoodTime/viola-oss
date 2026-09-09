"""Proactive health monitoring via periodic background checks.

Runs lightweight, non-LLM diagnostics on a configurable interval
(default 30 minutes) during active hours (default 07:00-23:00).

Publishes results through the EventBus and caches the last result
for the ``GET /api/health/heartbeat`` endpoint.

Usage::

    from services.health.heartbeat import get_heartbeat_service

    svc = get_heartbeat_service()
    await svc.start()
    # ...
    await svc.stop()
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from core.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

_DEFAULT_INTERVAL_MINUTES = 30
_DEFAULT_ACTIVE_HOUR_START = 7
_DEFAULT_ACTIVE_HOUR_END = 23
_MEMORY_WARN_MB = 500

CheckStatus = Literal["pass", "warn", "fail"]


@dataclass
class HealthCheckResult:
    """Single check outcome."""

    name: str
    status: CheckStatus
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class HeartbeatReport:
    """Aggregated result of one heartbeat cycle."""

    timestamp: str
    overall: CheckStatus
    checks: list[HealthCheckResult] = field(default_factory=list)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "overall": self.overall,
            "duration_ms": round(self.duration_ms, 1),
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "message": c.message,
                    "details": c.details,
                }
                for c in self.checks
            ],
        }


# ---------------------------------------------------------------------------
# Individual health checks (lightweight, no LLM calls)
# ---------------------------------------------------------------------------


def _check_audio_device() -> HealthCheckResult:
    """Check if an audio output device is still connected."""
    try:
        import sounddevice as sd

        from audio_core.portaudio_guard import sounddevice_guard

        with sounddevice_guard():
            devices = sd.query_devices()
        output_devs = [d for d in devices if d.get("max_output_channels", 0) > 0]
        if not output_devs:
            return HealthCheckResult(
                name="audio_device",
                status="fail",
                message="No audio output device detected",
            )
        return HealthCheckResult(
            name="audio_device",
            status="pass",
            message="Audio output available",
            details={"output_count": len(output_devs)},
        )
    except ImportError:
        return HealthCheckResult(
            name="audio_device",
            status="warn",
            message="sounddevice not installed",
        )
    except Exception as exc:
        return HealthCheckResult(
            name="audio_device",
            status="warn",
            message="Audio device query failed: %s" % exc,
        )


def _check_disk_space() -> HealthCheckResult:
    """Check available disk space on the project drive."""
    try:
        project_root = Path(__file__).resolve().parent.parent.parent
        usage = shutil.disk_usage(str(project_root))
        free_gb = usage.free / (1024**3)
        if free_gb < 0.5:
            return HealthCheckResult(
                name="disk_space",
                status="fail",
                message="Critically low disk space: %.1f GB free" % free_gb,
                details={"free_gb": round(free_gb, 2)},
            )
        if free_gb < 1.0:
            return HealthCheckResult(
                name="disk_space",
                status="warn",
                message="Low disk space: %.1f GB free" % free_gb,
                details={"free_gb": round(free_gb, 2)},
            )
        return HealthCheckResult(
            name="disk_space",
            status="pass",
            message="%.1f GB free" % free_gb,
            details={"free_gb": round(free_gb, 2)},
        )
    except Exception as exc:
        return HealthCheckResult(
            name="disk_space",
            status="warn",
            message="Disk space check failed: %s" % exc,
        )


def _check_sqlite_health() -> HealthCheckResult:
    """Check SQLite databases for health (and run WAL checkpoint if needed)."""
    project_root = Path(__file__).resolve().parent.parent.parent
    search_dirs = [
        project_root / "data",
        project_root / ".viola" / "data",
    ]
    db_files: list[Path] = []
    for d in search_dirs:
        if d.exists():
            db_files.extend(d.rglob("*.sqlite3"))
            db_files.extend(d.rglob("*.db"))

    if not db_files:
        return HealthCheckResult(
            name="sqlite_health",
            status="pass",
            message="No databases found",
        )

    errors: list[str] = []
    checkpointed = 0
    for db_path in db_files:
        try:
            conn = sqlite3.connect(str(db_path))
            # Quick integrity check (limited)
            cursor = conn.execute("PRAGMA quick_check")
            result = cursor.fetchone()
            if result and result[0] != "ok":
                errors.append("%s: %s" % (db_path.name, result[0]))

            # Run WAL checkpoint if applicable
            try:
                wal_path = db_path.with_suffix(db_path.suffix + "-wal")
                if wal_path.exists() and wal_path.stat().st_size > 1024 * 1024:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    checkpointed += 1
            except Exception:
                pass  # WAL checkpoint is best-effort

            conn.close()
        except Exception as exc:
            errors.append("%s: %s" % (db_path.name, exc))

    if errors:
        return HealthCheckResult(
            name="sqlite_health",
            status="fail",
            message="Database issues: %s" % "; ".join(errors),
            details={"errors": errors, "checkpointed": checkpointed},
        )
    return HealthCheckResult(
        name="sqlite_health",
        status="pass",
        message="All %d database(s) healthy" % len(db_files),
        details={"db_count": len(db_files), "checkpointed": checkpointed},
    )


def _check_memory_usage() -> HealthCheckResult:
    """Check current process memory usage."""
    try:
        import psutil

        process = psutil.Process(os.getpid())
        mem_mb = process.memory_info().rss / (1024 * 1024)
        if mem_mb > _MEMORY_WARN_MB:
            return HealthCheckResult(
                name="memory_usage",
                status="warn",
                message="High memory usage: %.0f MB" % mem_mb,
                details={"rss_mb": round(mem_mb, 1)},
            )
        return HealthCheckResult(
            name="memory_usage",
            status="pass",
            message="%.0f MB RSS" % mem_mb,
            details={"rss_mb": round(mem_mb, 1)},
        )
    except ImportError:
        return HealthCheckResult(
            name="memory_usage",
            status="pass",
            message="psutil not installed, skipped",
        )
    except Exception as exc:
        return HealthCheckResult(
            name="memory_usage",
            status="warn",
            message="Memory check failed: %s" % exc,
        )


def _check_llm_reachability() -> HealthCheckResult:
    """Lightweight reachability check for the configured LLM provider."""
    try:
        from config.settings import settings
    except Exception:
        return HealthCheckResult(
            name="llm_reachability",
            status="warn",
            message="Cannot load settings",
        )

    if not settings.enable_gpt:
        return HealthCheckResult(
            name="llm_reachability",
            status="pass",
            message="LLM disabled",
        )

    backend = settings.llm_backend
    url_map = {
        "openai": "https://api.openai.com/",
        "anthropic": "https://api.anthropic.com/",
        "google": "https://generativelanguage.googleapis.com/",
    }

    target_url = url_map.get(backend)
    if not target_url:
        return HealthCheckResult(
            name="llm_reachability",
            status="pass",
            message="Backend '%s' — no reachability URL" % backend,
        )

    try:
        import httpx

        resp = httpx.head(target_url, timeout=5.0, follow_redirects=True)
        return HealthCheckResult(
            name="llm_reachability",
            status="pass",
            message="%s reachable (HTTP %d)" % (backend, resp.status_code),
            details={"backend": backend, "status_code": resp.status_code},
        )
    except ImportError:
        return HealthCheckResult(
            name="llm_reachability",
            status="warn",
            message="httpx not installed",
        )
    except Exception as exc:
        return HealthCheckResult(
            name="llm_reachability",
            status="fail",
            message="%s unreachable: %s" % (backend, exc),
            details={"backend": backend},
        )


def _check_api_key_cached() -> HealthCheckResult:
    """Check that the active LLM backend has an API key configured."""
    try:
        from config.settings import settings
    except Exception:
        return HealthCheckResult(
            name="api_key_status",
            status="warn",
            message="Cannot load settings",
        )

    if not settings.enable_gpt:
        return HealthCheckResult(
            name="api_key_status",
            status="pass",
            message="LLM disabled",
        )

    backend = settings.llm_backend
    key_map = {
        "openai": settings.openai_api_key,
        "anthropic": settings.anthropic_api_key,
        "google": settings.google_api_key,
    }

    key = key_map.get(backend)
    if key:
        return HealthCheckResult(
            name="api_key_status",
            status="pass",
            message="%s API key configured" % backend,
            details={"backend": backend},
        )
    return HealthCheckResult(
        name="api_key_status",
        status="fail",
        message="No API key for backend '%s'" % backend,
        details={"backend": backend},
    )


# ---------------------------------------------------------------------------
# HeartbeatService
# ---------------------------------------------------------------------------


class HeartbeatService:
    """Background asyncio service that periodically runs health checks.

    Parameters
    ----------
    interval_minutes:
        Minutes between heartbeat cycles.  Default 30.
    active_hour_start:
        Hour (0-23) to start running checks.  Default 7.
    active_hour_end:
        Hour (0-23) to stop running checks.  Default 23.
    event_bus:
        Optional EventBus instance for publishing health events.
    """

    def __init__(
        self,
        *,
        interval_minutes: int = _DEFAULT_INTERVAL_MINUTES,
        active_hour_start: int = _DEFAULT_ACTIVE_HOUR_START,
        active_hour_end: int = _DEFAULT_ACTIVE_HOUR_END,
        event_bus: Any = None,
    ) -> None:
        self._interval_seconds = max(60, interval_minutes * 60)
        self._active_start = active_hour_start
        self._active_end = active_hour_end
        self._event_bus = event_bus
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._lock = threading.Lock()
        self._last_report: HeartbeatReport | None = None

    @property
    def last_report(self) -> HeartbeatReport | None:
        """Return the most recent heartbeat report (thread-safe read)."""
        return self._last_report

    async def start(self) -> None:
        """Start the background heartbeat loop."""
        with self._lock:
            if self._running:
                logger.debug("HeartbeatService already running")
                return
            self._running = True

        logger.info(
            "HeartbeatService starting (interval=%dm, active=%d:00-%d:00)",
            self._interval_seconds // 60,
            self._active_start,
            self._active_end,
        )
        self._task = asyncio.create_task(self._loop(), name="heartbeat-service")

    async def stop(self) -> None:
        """Stop the background heartbeat loop."""
        with self._lock:
            self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("HeartbeatService stopped")

    def _is_active_hours(self) -> bool:
        """Check if current local time is within active hours."""
        now = datetime.now()
        hour = now.hour
        if self._active_start <= self._active_end:
            return self._active_start <= hour < self._active_end
        # Wraps midnight (e.g. 22:00 - 06:00)
        return hour >= self._active_start or hour < self._active_end

    async def _loop(self) -> None:
        """Main heartbeat loop."""
        while self._running:
            try:
                if self._is_active_hours():
                    await self._run_cycle()
                else:
                    logger.debug("HeartbeatService skipping cycle (outside active hours)")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("HeartbeatService cycle failed")

            try:
                await asyncio.sleep(self._interval_seconds)
            except asyncio.CancelledError:
                break

    async def _run_cycle(self) -> None:
        """Execute one heartbeat cycle."""
        t0 = time.monotonic()
        checks = await asyncio.to_thread(self._run_checks)
        duration_ms = (time.monotonic() - t0) * 1000

        # Determine overall status
        statuses = [c.status for c in checks]
        if "fail" in statuses:
            overall: CheckStatus = "fail"
        elif "warn" in statuses:
            overall = "warn"
        else:
            overall = "pass"

        report = HeartbeatReport(
            timestamp=datetime.now(UTC).isoformat(),
            overall=overall,
            checks=checks,
            duration_ms=duration_ms,
        )
        self._last_report = report

        logger.info(
            "Heartbeat: overall=%s checks=%d duration=%.0fms",
            overall,
            len(checks),
            duration_ms,
        )

        # Log warnings/failures at appropriate levels
        for check in checks:
            if check.status == "fail":
                logger.warning("Heartbeat FAIL: %s — %s", check.name, check.message)
            elif check.status == "warn":
                logger.info("Heartbeat WARN: %s — %s", check.name, check.message)

        # Publish event through EventBus if available
        self._publish_event(report)

    def _run_checks(self) -> list[HealthCheckResult]:
        """Run all health checks synchronously (called via to_thread)."""
        check_fns = [
            _check_audio_device,
            _check_api_key_cached,
            _check_disk_space,
            _check_sqlite_health,
            _check_memory_usage,
            _check_llm_reachability,
        ]
        results: list[HealthCheckResult] = []
        for fn in check_fns:
            try:
                results.append(fn())
            except Exception as exc:
                results.append(
                    HealthCheckResult(
                        name=fn.__name__.replace("_check_", ""),
                        status="fail",
                        message="Check crashed: %s" % exc,
                    )
                )
        return results

    def _publish_event(self, report: HeartbeatReport) -> None:
        """Publish a DiagnosticsEmitted event if EventBus is available."""
        if self._event_bus is None:
            return
        try:
            from core.events.types import DiagnosticsEmitted

            severity = "INFO"
            if report.overall == "fail":
                severity = "WARNING"
            elif report.overall == "warn":
                severity = "INFO"

            event = DiagnosticsEmitted(
                source="heartbeat",
                name="health_heartbeat",
                severity=severity,
                message="Heartbeat: %s" % report.overall,
                context=report.to_dict(),
            )
            self._event_bus.publish(event)
        except Exception:
            logger.debug("Failed to publish heartbeat event", exc_info=True)

    async def run_now(self) -> HeartbeatReport:
        """Run a heartbeat cycle immediately (for on-demand health checks)."""
        await self._run_cycle()
        report = self._last_report
        if report is None:
            # Should not happen, but be defensive
            return HeartbeatReport(
                timestamp=datetime.now(UTC).isoformat(),
                overall="fail",
                checks=[],
            )
        return report


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_SERVICE_LOCK = threading.Lock()
_SERVICE_INSTANCE: HeartbeatService | None = None


def get_heartbeat_service(**kwargs: Any) -> HeartbeatService:
    """Get or create the singleton HeartbeatService.

    Keyword arguments are forwarded to the constructor on first call only.
    """
    global _SERVICE_INSTANCE
    with _SERVICE_LOCK:
        if _SERVICE_INSTANCE is None:
            _SERVICE_INSTANCE = HeartbeatService(**kwargs)
        return _SERVICE_INSTANCE


__all__ = ["HealthCheckResult", "HeartbeatReport", "HeartbeatService", "get_heartbeat_service"]
