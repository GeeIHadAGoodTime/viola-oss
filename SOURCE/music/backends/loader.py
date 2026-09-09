"""Backend loader strategies shared by the music player."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

from core.logging_config import StructuredLogger
from music.backends.base import BaseBackend
from music.exceptions import BackendError
from music.player_config import BackendStreamingConfig

LoggerLike: TypeAlias = logging.Logger | StructuredLogger


def _as_stdlib_logger(logger: LoggerLike) -> logging.Logger:
    if isinstance(logger, StructuredLogger):
        return logger._logger
    return logger


def _load_vlc_backend(logger: LoggerLike) -> BaseBackend | None:
    try:
        from .vlc import VLCBackend
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("VLC backend unavailable, falling back to simple: %r", exc)
        return None
    try:
        return VLCBackend(logger=_as_stdlib_logger(logger))
    except Exception as exc:
        logger.warning("VLC backend init failed, falling back to simple: %r", exc)
        return None


def _load_embedded_backend(
    logger: LoggerLike,
    video_widget: object | None = None,
    headless: bool = True,
) -> BaseBackend | None:
    """Load EmbeddedPlayerBackend for YouTube and embedded content."""
    try:
        from .embedded_backend import EmbeddedPlayerBackend
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("EmbeddedPlayerBackend unavailable: %r", exc)
        return None
    return EmbeddedPlayerBackend(
        logger=_as_stdlib_logger(logger),
        video_widget=video_widget,
        headless=headless,
    )


def _load_qt_media_backend(logger: LoggerLike) -> BaseBackend | None:
    """Load QtMediaBackend (PyQt6 QMediaPlayer).  Returns None when unavailable."""
    try:
        from .qt_media import _QT_AVAILABLE, QtMediaBackend
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("QtMediaBackend import failed: %r", exc)
        return None
    if not _QT_AVAILABLE:
        logger.warning("QtMediaBackend unavailable — PyQt6.QtMultimedia not installed")
        return None
    return QtMediaBackend(logger=_as_stdlib_logger(logger))


def _load_null_backend(logger: LoggerLike) -> BaseBackend:
    from .null import NullBackend

    return NullBackend()


def _load_simple_backend(logger: LoggerLike, backend_config: BackendStreamingConfig | None = None) -> BaseBackend:
    from .simple import SimpleBackend

    return SimpleBackend(logger=_as_stdlib_logger(logger), config=backend_config)


@dataclass(frozen=True)
class BackendLoadRequest:
    backend_name: str
    backend_factory: Callable[[], BaseBackend] | None = None
    embedded_only: bool = False
    video_widget: object | None = None
    headless: bool = True


@dataclass(frozen=True)
class BackendLoadResult:
    backend: BaseBackend
    backend_name: str


class BackendStrategyLoader:
    """Encapsulates backend selection + fallback rules."""

    def __init__(
        self,
        *,
        logger: LoggerLike,
        backend_settings: BackendStreamingConfig | None = None,
    ) -> None:
        self._logger = logger
        self._backend_settings = backend_settings

    def load(self, request: BackendLoadRequest) -> BackendLoadResult:
        backend_name = request.backend_name
        backend: BaseBackend | None = None

        # Explicit null backend takes priority over all other flags
        if backend_name == "null":
            return BackendLoadResult(
                backend=_load_null_backend(self._logger),
                backend_name="null",
            )

        if request.embedded_only:
            backend = _load_embedded_backend(
                logger=self._logger,
                video_widget=request.video_widget,
                headless=request.headless,
            )
            if backend is None:
                raise BackendError("E2E/embedded mode requires EmbeddedPlayerBackend but it is unavailable")
            backend_name = "embedded"
            return BackendLoadResult(backend=backend, backend_name=backend_name)

        if request.backend_factory is not None:
            try:
                backend = request.backend_factory()
            except Exception as exc:  # pragma: no cover - defensive
                self._logger.warning(
                    "Custom backend factory failed: %r. Falling back to configured backend.",
                    exc,
                )
                backend = None

        if backend is None:
            backend = self._select_default_backend(backend_name)

        if backend is None:
            raise BackendError("No functional backend available")

        return BackendLoadResult(backend=backend, backend_name=backend_name)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _call_simple_backend(self) -> BaseBackend:
        return _load_simple_backend(self._logger, self._backend_settings)

    def _select_default_backend(self, backend_name: str) -> BaseBackend | None:
        backend: BaseBackend | None = None

        if backend_name == "null":
            return _load_null_backend(self._logger)

        if backend_name == "qt_media":
            backend = _load_qt_media_backend(self._logger)
            if backend is not None:
                return backend
            self._logger.warning("QtMediaBackend unavailable. Falling back to VLC → Simple.")
            backend = _load_vlc_backend(self._logger)
            if backend is not None:
                return backend
            return self._call_simple_backend()

        if backend_name == "vlc":
            backend = _load_vlc_backend(self._logger)
            if backend is None:
                self._logger.warning("VLC backend unavailable (configured as default). Falling back to simple backend.")
                backend = self._call_simple_backend()
            return backend

        try:
            backend = self._call_simple_backend()
        except Exception as exc:
            self._logger.error(
                "Simple backend failed (%r); VLC is not used as automatic fallback. "
                "Configure backend explicitly if VLC is desired.",
                exc,
            )
            if backend_name == "vlc":
                backend = _load_vlc_backend(self._logger)
            else:
                raise BackendError(
                    f"No functional backend available: simple backend failed ({exc}), "
                    "and VLC is not configured as fallback"
                ) from exc
        return backend
