"""
Plugin Sandboxing

Exception isolation and resource limits for third-party plugins.
Prevents plugin crashes from affecting the core system.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.constants import PLUGIN_MAX_CRASHES
from core.logging_config import get_logger

from .api import ThirdPartyPlugin

logger = get_logger(__name__)


@dataclass
class ResourceLimits:
    """Resource limits for plugins"""

    max_cpu_percent: float = 50.0
    max_memory_mb: float = 100.0
    max_execution_time_sec: float = 30.0


class PluginSandbox:
    """Sandbox plugin execution with crash tracking and auto-disable."""

    def __init__(
        self,
        plugin: ThirdPartyPlugin,
        limits: ResourceLimits | None = None,
        tts_callback: Callable[[str], None] | None = None,
    ):
        self.plugin = plugin
        self.limits = limits or ResourceLimits()
        self._tts_callback = tts_callback
        self._crashed = False
        self._crash_count = 0
        self._max_crashes = PLUGIN_MAX_CRASHES
        self._disabled = False

    def execute_with_isolation(
        self,
        func: Callable[..., Any],
        *args: Any,
        allow_disabled: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Execute plugin function with exception isolation.

        Returns function result or None if crashed.
        Disables plugin after max_crashes consecutive failures.
        """
        if self._disabled and not allow_disabled:
            return None

        try:
            result = func(*args, **kwargs)
            # Success resets crash counter (consecutive failures only)
            self._crash_count = 0
            self._crashed = False
            return result
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            self._crashed = True
            self._crash_count += 1
            logger.exception("Plugin %s crashed", self.plugin.name)

            if self._crash_count >= self._max_crashes:
                self._disabled = True
                logger.error("Disabling plugin %s (too many crashes)", self.plugin.name)
                try:
                    self.plugin.on_stop()
                except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
                    logger.debug("Plugin %s stop hook failed after sandbox crash: %s", self.plugin.name, exc)
                if self._tts_callback:
                    try:
                        self._tts_callback(
                            "I've temporarily disabled the %s plugin due to repeated errors. "
                            "Say 'reload plugins' to try again." % self.plugin.name
                        )
                    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
                        logger.debug("Plugin crash TTS notification failed: %s", exc)
            elif self._tts_callback:
                try:
                    self._tts_callback("The %s plugin encountered an error and could not complete." % self.plugin.name)
                except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
                    logger.debug("Plugin crash TTS notification failed: %s", exc)

            return None

    async def async_execute_with_isolation(
        self,
        coro_func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute async plugin function with exception isolation."""
        if self._disabled:
            return None

        try:
            result = await coro_func(*args, **kwargs)
            self._crash_count = 0
            self._crashed = False
            return result
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            self._crashed = True
            self._crash_count += 1
            logger.exception("Plugin %s async crashed", self.plugin.name)

            if self._crash_count >= self._max_crashes:
                self._disabled = True
                logger.error("Disabling plugin %s (too many crashes)", self.plugin.name)
                try:
                    self.plugin.on_stop()
                except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
                    logger.debug("Plugin %s stop hook failed after async sandbox crash: %s", self.plugin.name, exc)

            return None

    def reset(self) -> None:
        """Re-enable a disabled plugin."""
        self._disabled = False
        self._crash_count = 0
        self._crashed = False
        logger.info("Plugin %s sandbox reset", self.plugin.name)

    @property
    def has_crashed(self) -> bool:
        """Check if plugin has crashed."""
        return self._crashed

    @property
    def crash_count(self) -> int:
        """Get crash count."""
        return self._crash_count

    @property
    def is_disabled(self) -> bool:
        """Check if plugin is disabled due to crashes."""
        return self._disabled
