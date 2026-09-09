"""Lazy proxy for the music controller adapter."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from core.logging_config import get_logger
from diagnostics.startup_telemetry import subsystem_timer

logger = get_logger(__name__)


class LazyMusicControllerAdapter:
    """Thread-safe lazy proxy that defers music provider setup until first use."""

    __slots__ = ("_factory", "_failure", "_hub_state_authority", "_lock", "_pending_on_state_change", "_real")

    def __init__(self, factory: Callable[[], Any]) -> None:
        object.__setattr__(self, "_factory", factory)
        object.__setattr__(self, "_real", None)
        object.__setattr__(self, "_lock", threading.Lock())
        object.__setattr__(self, "_hub_state_authority", None)
        object.__setattr__(self, "_failure", None)
        object.__setattr__(self, "_pending_on_state_change", None)

    def _ensure(self) -> Any:
        real = object.__getattribute__(self, "_real")
        if real is not None:
            return real
        lock = object.__getattribute__(self, "_lock")
        with lock:
            real = object.__getattribute__(self, "_real")
            if real is not None:
                return real
            factory = object.__getattribute__(self, "_factory")
            logger.info("LazyMusicControllerAdapter: materializing music controller on first access...")
            try:
                with subsystem_timer("music"):
                    real = factory()
            except Exception as exc:
                object.__setattr__(self, "_failure", exc)
                logger.warning("Lazy music initialization failed: %s", exc)
                raise
            if real is None:
                exc = RuntimeError("music adapter unavailable")
                object.__setattr__(self, "_failure", exc)
                raise exc
            hub_state_authority = object.__getattribute__(self, "_hub_state_authority")
            if hub_state_authority is not None:
                setter = getattr(real, "set_hub_state_authority", None)
                if callable(setter):
                    setter(hub_state_authority)
            pending_on_state_change = object.__getattribute__(self, "_pending_on_state_change")
            if pending_on_state_change is not None:
                music_player = getattr(real, "player", None) or real
                if hasattr(music_player, "on_state_change"):
                    music_player.on_state_change = pending_on_state_change
            object.__setattr__(self, "_real", real)
            logger.info("LazyMusicControllerAdapter: music controller ready")
            return real

    def materialize(self) -> Any:
        """Create and return the real music adapter if it is still lazy."""
        return self._ensure()

    def is_materialized(self) -> bool:
        return object.__getattribute__(self, "_real") is not None

    def initialization_error(self) -> BaseException | None:
        return object.__getattribute__(self, "_failure")

    @property
    def player(self) -> Any | None:
        real = object.__getattribute__(self, "_real")
        if real is None:
            return self
        return getattr(real, "player", None)

    @property
    def on_state_change(self) -> Any:
        real = object.__getattribute__(self, "_real")
        if real is not None:
            music_player = getattr(real, "player", None) or real
            return getattr(music_player, "on_state_change", None)
        return object.__getattribute__(self, "_pending_on_state_change")

    @property
    def autoplay(self) -> Any | None:
        real = object.__getattribute__(self, "_real")
        if real is None:
            return None
        return getattr(real, "autoplay", None)

    @property
    def autoplay_controller(self) -> Any | None:
        real = object.__getattribute__(self, "_real")
        if real is None:
            return None
        return getattr(real, "autoplay_controller", None)

    def set_hub_state_authority(self, hub_authority: object) -> None:
        object.__setattr__(self, "_hub_state_authority", hub_authority)
        real = object.__getattribute__(self, "_real")
        if real is not None:
            setter = getattr(real, "set_hub_state_authority", None)
            if callable(setter):
                setter(hub_authority)

    def state(self) -> dict[str, Any]:
        real = object.__getattribute__(self, "_real")
        if real is None:
            return {}
        return real.state()

    def status(self) -> dict[str, Any]:
        real = object.__getattribute__(self, "_real")
        if real is None:
            return {"status": "initializing"}
        return real.status()

    def __getattr__(self, name: str) -> Any:
        real = object.__getattribute__(self, "_ensure")()
        return getattr(real, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "on_state_change":
            object.__setattr__(self, "_pending_on_state_change", value)
            real = object.__getattribute__(self, "_real")
            if real is not None:
                music_player = getattr(real, "player", None) or real
                if hasattr(music_player, "on_state_change"):
                    music_player.on_state_change = value
            return
        real = object.__getattribute__(self, "_ensure")()
        setattr(real, name, value)

    def __dir__(self) -> list[str]:
        base = set(type(self).__dict__) | {
            "autoplay",
            "autoplay_controller",
            "initialization_error",
            "is_materialized",
            "materialize",
            "on_state_change",
            "player",
            "set_hub_state_authority",
            "state",
            "status",
        }
        real = object.__getattribute__(self, "_real")
        if real is not None:
            base.update(dir(real))
        return sorted(base)

    def __repr__(self) -> str:
        real = object.__getattribute__(self, "_real")
        if real is not None:
            return repr(real)
        return "<LazyMusicControllerAdapter (not yet materialized)>"

    def __bool__(self) -> bool:
        return True


__all__ = ["LazyMusicControllerAdapter"]
