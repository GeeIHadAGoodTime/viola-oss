"""
Spoke Health Monitor — periodic checks for hub connectivity, mic, and speaker.

Runs as a background asyncio task and exposes a ``/health`` endpoint
with current status for monitoring and debugging.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from core.constants import DEFAULT_API_PORT, TIMEOUT_DEFAULT
from core.logging_config import get_logger

logger = get_logger(__name__)

CHECK_INTERVAL_S = 15.0


@dataclass
class SpokeHealthStatus:
    """Current health of the spoke."""

    hub_reachable: bool = False
    mic_available: bool = False
    speaker_available: bool = False
    last_check: float = 0.0
    hub_host: str = ""
    hub_port: int = DEFAULT_API_PORT
    uptime_s: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        healthy = self.hub_reachable and self.mic_available and self.speaker_available
        return {
            "status": "ok" if healthy else "degraded",
            "hub_reachable": self.hub_reachable,
            "mic_available": self.mic_available,
            "speaker_available": self.speaker_available,
            "hub_host": self.hub_host,
            "hub_port": self.hub_port,
            "last_check": self.last_check,
            "uptime_s": round(self.uptime_s, 1),
            "errors": self.errors[-5:],  # last 5 errors
        }


class SpokeHealthMonitor:
    """Background health checker for spoke connectivity and devices."""

    def __init__(self, hub_host: str, hub_port: int = DEFAULT_API_PORT) -> None:
        self._hub_host = hub_host
        self._hub_port = hub_port
        self._status = SpokeHealthStatus(hub_host=hub_host, hub_port=hub_port)
        self._start_time = time.monotonic()
        self._task: asyncio.Task[None] | None = None

    @property
    def status(self) -> SpokeHealthStatus:
        self._status.uptime_s = time.monotonic() - self._start_time
        return self._status

    def start(self) -> None:
        """Start the background health check loop."""
        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._check_loop())
            logger.info("Spoke health monitor started")
        except RuntimeError:
            logger.warning("No running event loop; health monitor not started")

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("Spoke health monitor stopped")

    async def _check_loop(self) -> None:
        # Run first check immediately
        await self._run_checks()
        while True:
            try:
                await asyncio.sleep(CHECK_INTERVAL_S)
                await self._run_checks()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Health check error")

    async def _run_checks(self) -> None:
        errors: list[str] = []

        # 1. Hub reachable
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_DEFAULT) as client:
                resp = await client.get(f"http://{self._hub_host}:{self._hub_port}/health")
                self._status.hub_reachable = resp.status_code == 200
        except Exception as e:
            self._status.hub_reachable = False
            errors.append(f"hub unreachable: {e}")

        # 2. Mic available
        try:
            import sounddevice as sd

            devices = sd.query_devices()
            has_input = any(
                d.get("max_input_channels", 0) > 0 for d in (devices if isinstance(devices, list) else [devices])
            )
            self._status.mic_available = has_input
            if not has_input:
                errors.append("no input device found")
        except Exception as e:
            self._status.mic_available = False
            errors.append(f"mic check failed: {e}")

        # 3. Speaker available
        try:
            import sounddevice as sd

            devices = sd.query_devices()
            has_output = any(
                d.get("max_output_channels", 0) > 0 for d in (devices if isinstance(devices, list) else [devices])
            )
            self._status.speaker_available = has_output
            if not has_output:
                errors.append("no output device found")
        except Exception as e:
            self._status.speaker_available = False
            errors.append(f"speaker check failed: {e}")

        self._status.last_check = time.time()
        self._status.errors = errors

        if errors:
            logger.warning("Spoke health issues: %s", "; ".join(errors))
        else:
            logger.debug("Spoke health: all OK")
