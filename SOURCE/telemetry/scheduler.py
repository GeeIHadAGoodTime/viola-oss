"""Automatic telemetry send scheduler - fires once per interval (default 4h).

Runs as a daemon thread so it dies with the main process.
Respects the opt-in gate: does nothing if should_send() is False.
Adds random jitter (0-60 min) to prevent thundering herd.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time

from core.constants import VIOLA_VERSION
from core.logging_config import get_logger

logger = get_logger(__name__)

# Maximum jitter in seconds (60 minutes)
_MAX_JITTER_SECONDS = 3600


class TelemetryScheduler:
    """Periodic background sender for accumulated telemetry.

    Parameters
    ----------
    accumulator:
        The TelemetryAccumulator singleton.
    interval_hours:
        Hours between send attempts (default 4).
    """

    def __init__(
        self,
        accumulator: object,
        *,
        interval_hours: int = 4,
    ) -> None:
        self._accumulator = accumulator
        self._interval_seconds = interval_hours * 3600
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background send loop.

        No-op if already running or if telemetry is disabled.
        """
        from telemetry.reporter import TelemetryReporter

        if not TelemetryReporter.should_send():
            logger.info("Telemetry scheduler not started: send conditions not met")
            return

        if self._thread is not None and self._thread.is_alive():
            logger.debug("Telemetry scheduler already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="telemetry-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Telemetry scheduler started (interval=%dh, jitter up to %dmin)",
            self._interval_seconds // 3600,
            _MAX_JITTER_SECONDS // 60,
        )

    def stop(self, *, final_send: bool = True) -> None:
        """Stop the scheduler and optionally do a final send.

        Parameters
        ----------
        final_send:
            If True and there is accumulated data, perform one last
            send before returning.  Blocks briefly for the HTTP call.
        """
        self._stop_event.set()

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                logger.warning("Telemetry scheduler thread did not exit in time")

        self._thread = None

        if final_send:
            self._try_final_send()

        logger.info("Telemetry scheduler stopped")

    @property
    def is_running(self) -> bool:
        """True if the background thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Sleep-loop that wakes up once per interval to send."""
        # One-shot first_run funnel ping (download→install bridge). Runs on
        # the scheduler thread so app startup never blocks on network; it is
        # internally gated on the full telemetry opt-in set and a sent-once
        # settings flag, so this call is a no-op on every run after the first.
        self._try_first_run_ping()

        if self._accumulator_has_priority_data():
            self._try_send()

        # Initial jitter so all installs don't send at the same time
        jitter = random.randint(0, _MAX_JITTER_SECONDS)
        first_wait = self._interval_seconds + jitter
        logger.debug(
            "Telemetry scheduler: first send in %ds (interval %ds + jitter %ds)",
            first_wait,
            self._interval_seconds,
            jitter,
        )

        # Wait for the first interval (interruptible via stop_event)
        if self._stop_event.wait(timeout=first_wait):
            return  # Stop requested

        while not self._stop_event.is_set():
            self._try_send()

            # Next cycle: interval + fresh jitter
            jitter = random.randint(0, _MAX_JITTER_SECONDS)
            wait = self._interval_seconds + jitter
            if self._stop_event.wait(timeout=wait):
                return

    def _try_first_run_ping(self) -> None:
        """Send the one-shot first_run funnel ping.  Never raises."""
        try:
            from telemetry.first_run import send_first_run_ping

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(send_first_run_ping())
            finally:
                loop.close()
        except Exception:  # noqa: BLE001, RUF100 - scheduler thread safety net; ping is retried next start
            logger.exception("Telemetry scheduler: first_run ping attempt failed")

    def _try_send(self) -> None:
        """Attempt a single send cycle.  Never raises."""
        from telemetry.reporter import TelemetryReporter

        if not TelemetryReporter.should_send():
            logger.debug("Telemetry scheduler: send conditions no longer met, skipping")
            return

        if not self._accumulator_has_data():
            logger.debug("Telemetry scheduler: no accumulated data, skipping send")
            return

        reporter = self._build_reporter()
        try:
            sent = self._run_send(reporter)
            if sent:
                logger.info("Telemetry scheduler: send succeeded")
            else:
                logger.warning("Telemetry scheduler: send returned False")
        except Exception:
            logger.exception("Telemetry scheduler: send failed")

    def _try_final_send(self) -> None:
        """Best-effort send on shutdown.  Never raises."""
        from telemetry.reporter import TelemetryReporter

        if not TelemetryReporter.should_send():
            return
        if not self._accumulator_has_data():
            return

        logger.info("Telemetry scheduler: performing final send on shutdown")
        reporter = self._build_reporter()
        try:
            sent = self._run_send(reporter)
            if sent:
                logger.info("Telemetry scheduler: final send succeeded")
            else:
                logger.debug("Telemetry scheduler: final send returned False")
        except Exception:
            logger.exception("Telemetry scheduler: final shutdown send failed")

    def _accumulator_has_data(self) -> bool:
        """Check if the accumulator has any meaningful data."""
        snap = self._accumulator.snapshot()
        cmds = snap.get("commands", {})
        if cmds.get("total", 0) > 0:
            return True
        health = snap.get("health", {})
        return any(
            int(health.get(name, 0) or 0) > 0
            for name in (
                "sessions_started",
                "sessions_clean_exits",
                "session_crashes",
                "crashes",
                "unhandled_exceptions",
            )
        )

    def _accumulator_has_priority_data(self) -> bool:
        """True when release-health data should upload before the normal interval."""
        snap = self._accumulator.snapshot()
        health = snap.get("health", {})
        return any(
            int(health.get(name, 0) or 0) > 0
            for name in (
                "session_crashes",
                "crashes",
                "unhandled_exceptions",
            )
        )

    def _build_reporter(self) -> object:
        """Build a TelemetryReporter from current state."""
        from config.settings import settings
        from telemetry.install_id import get_or_create_install_id
        from telemetry.reporter import TelemetryReporter

        return TelemetryReporter(
            self._accumulator,
            install_id=get_or_create_install_id(),
            tier="free",
            app_version=getattr(settings, "app_version", VIOLA_VERSION),
        )

    @staticmethod
    def _run_send(reporter: object) -> bool:
        """Run the async send() in a temporary event loop.

        The scheduler thread has no running asyncio loop, so we create
        a short-lived one for each send.
        """
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(reporter.send())  # type: ignore[union-attr]
        finally:
            loop.close()
