from __future__ import annotations

import inspect
import threading
from collections.abc import Callable
from dataclasses import dataclass

from core.logging_config import get_logger

logger = get_logger(__name__)

_CHECK_INTERVAL_S = 30.0
_MISSING = object()


@dataclass
class _TTSBinding:
    health_target: object | None
    restart_target: object | None
    replace_target: Callable[[object], None] | None
    label: str
    state: str


class HealthWatchdog:
    """Background watchdog that checks subsystem health and attempts recovery."""

    def __init__(
        self,
        *,
        intent: object | None = None,
        tts_engine: object | None = None,
        interval_seconds: float = _CHECK_INTERVAL_S,
    ) -> None:
        self._intent = intent
        self._tts_engine = tts_engine
        self._interval_seconds = max(1.0, interval_seconds)
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._running = False

    def start(self) -> None:
        """Start the periodic health check loop."""
        with self._lock:
            if self._running:
                logger.debug("HealthWatchdog already running, skipping start")
                return
            self._running = True
            logger.info(
                "HealthWatchdog started (interval=%.1fs)",
                self._interval_seconds,
            )
            self._schedule_next_locked()

    def stop(self) -> None:
        """Stop the health check loop."""
        with self._lock:
            self._running = False
            timer = self._timer
            self._timer = None
        if timer is not None:
            timer.cancel()
        logger.info("HealthWatchdog stopped")

    def _schedule_next_locked(self) -> None:
        if not self._running:
            return
        timer = threading.Timer(self._interval_seconds, self._run_cycle)
        timer.daemon = True
        timer.name = "health-watchdog"
        self._timer = timer
        timer.start()

    def _run_cycle(self) -> None:
        try:
            self._check_and_heal()
        except Exception as exc:
            logger.exception("HealthWatchdog cycle failed: %s", exc)
        finally:
            with self._lock:
                if not self._running:
                    self._timer = None
                    return
                self._schedule_next_locked()

    def _check_and_heal(self) -> None:
        """Run one round of health checks and attempt restarts for failing subsystems."""
        self._check_tts()

    def _check_tts(self) -> None:
        """Run the TTS health check and attempt recovery when needed."""
        binding = self._resolve_tts_binding()
        logger.info(
            "HealthWatchdog check: subsystem=%s state=%s",
            binding.label,
            binding.state,
        )

        if binding.health_target is None:
            return

        if self._is_tts_healthy(binding.health_target):
            return

        logger.warning(
            "HealthWatchdog detected unhealthy subsystem: %s",
            binding.label,
        )
        if self._heal_tts(binding):
            logger.info(
                "HealthWatchdog recovered subsystem: %s",
                binding.label,
            )
            return

        logger.warning(
            "HealthWatchdog could not recover subsystem: %s",
            binding.label,
        )

    def _resolve_tts_binding(self) -> _TTSBinding:
        if self._tts_engine is not None:
            return self._resolve_tts_target(self._tts_engine)

        intent = self._resolve_intent()
        if intent is None:
            return _TTSBinding(None, None, None, "tts", "intent_unavailable")

        tts_engine = self._get_real_attr(intent, "tts_engine", None)
        if tts_engine is None:
            tts_engine = self._get_real_attr(intent, "tts", None)
        if tts_engine is None:
            return _TTSBinding(None, None, None, "tts", "missing")

        return self._resolve_tts_target(tts_engine)

    def _resolve_intent(self) -> object | None:
        intent = self._intent
        if intent is None:
            return None

        real_intent = self._get_real_attr(intent, "_real", None)
        ensure = self._get_real_attr(intent, "_ensure", None)
        if callable(ensure):
            if real_intent is None:
                return None
            return real_intent

        return intent

    def _resolve_tts_target(self, candidate: object) -> _TTSBinding:
        instance = self._get_real_attr(candidate, "_instance", None)
        materialize = self._get_real_attr(candidate, "_materialize", None)
        if callable(materialize):
            if instance is None:
                return _TTSBinding(
                    None,
                    None,
                    None,
                    type(candidate).__name__,
                    "idle_lazy_proxy",
                )
            return self._resolve_tts_target(instance)

        ensure_loaded = self._get_real_attr(candidate, "_ensure_loaded", None)
        engine = self._get_real_attr(candidate, "_engine", _MISSING)
        if callable(ensure_loaded) and engine is not _MISSING:
            load_error = self._get_real_attr(candidate, "_load_error", None)
            if engine is None and load_error is None:
                return _TTSBinding(
                    None,
                    None,
                    None,
                    type(candidate).__name__,
                    "idle_lazy_engine",
                )
            return _TTSBinding(
                candidate,
                candidate,
                None,
                type(candidate).__name__,
                "active",
            )

        wrapped_engine = self._get_real_attr(candidate, "tts_engine", _MISSING)
        if wrapped_engine is not _MISSING and wrapped_engine is not None:
            return _TTSBinding(
                wrapped_engine,
                wrapped_engine,
                lambda new_engine: setattr(candidate, "tts_engine", new_engine),
                type(wrapped_engine).__name__,
                "wrapped",
            )

        return _TTSBinding(
            candidate,
            candidate,
            None,
            type(candidate).__name__,
            "direct",
        )

    def _is_tts_healthy(self, tts_engine: object) -> bool:
        is_available = getattr(tts_engine, "is_available", None)
        if callable(is_available):
            try:
                return bool(is_available())
            except Exception as exc:
                logger.exception(
                    "HealthWatchdog TTS health check failed for %s: %s",
                    type(tts_engine).__name__,
                    exc,
                )
                return False

        worker = getattr(tts_engine, "_worker", None)
        if worker is not None and hasattr(worker, "is_alive"):
            try:
                return bool(worker.is_alive())
            except Exception as exc:
                logger.exception(
                    "HealthWatchdog TTS worker check failed for %s: %s",
                    type(tts_engine).__name__,
                    exc,
                )
                return False

        return True

    def _heal_tts(self, binding: _TTSBinding) -> bool:
        target = binding.restart_target
        if target is None:
            return False

        ensure_loaded = self._get_real_attr(target, "_ensure_loaded", None)
        if callable(ensure_loaded):
            logger.warning(
                "HealthWatchdog attempting TTS self-heal via _ensure_loaded for %s",
                binding.label,
            )
            try:
                return bool(ensure_loaded())
            except Exception as exc:
                logger.exception(
                    "HealthWatchdog TTS _ensure_loaded failed for %s: %s",
                    binding.label,
                    exc,
                )
                return False

        if binding.replace_target is not None and type(target).__name__ == "TTSEngine":
            logger.warning(
                "HealthWatchdog attempting TTS engine recreation for %s",
                binding.label,
            )
            try:
                config = getattr(target, "config", None)
                new_engine = type(target)(config=config) if config is not None else type(target)()
                binding.replace_target(new_engine)
                return self._is_tts_healthy(new_engine)
            except Exception as exc:
                logger.exception(
                    "HealthWatchdog TTS recreation failed for %s: %s",
                    binding.label,
                    exc,
                )
                return False

        logger.warning(
            "HealthWatchdog found no restart hook for %s",
            binding.label,
        )
        return False

    @staticmethod
    def _get_real_attr(
        target: object,
        name: str,
        default: object | None = None,
    ) -> object | None:
        try:
            static_attr = inspect.getattr_static(target, name)
        except AttributeError:
            return default

        if static_attr is _MISSING:
            return default

        try:
            return object.__getattribute__(target, name)
        except AttributeError:
            return default


__all__ = ["HealthWatchdog"]
