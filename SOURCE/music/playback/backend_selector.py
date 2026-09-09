"""
Backend Selection Service

Selects and initializes appropriate playback backends based on routing decisions.
Handles:
- EmbeddedPlayerBackend selection and initialization
- VLC backend selection and initialization
- Engine manager integration
- Backend availability checks
- E2E mode handling
"""

from __future__ import annotations

import logging
from typing import Any

from config.settings import settings
from core.logging_config import get_logger
from models.player import QueueItem
from music.backends.base import BaseBackend
from music.exceptions import BackendError
from music.playback.routing import PlaybackRoute

logger = get_logger(__name__)

# Import backends lazily to avoid import errors in environments without dependencies
_EmbeddedPlayerBackend: type[BaseBackend] | None = None
_VLCBackend: type[BaseBackend] | None = None
_QtMediaBackend: type[BaseBackend] | None = None


def _load_embedded_backend_class() -> type[BaseBackend] | None:
    """Lazily load EmbeddedPlayerBackend class."""
    global _EmbeddedPlayerBackend
    if _EmbeddedPlayerBackend is None:
        try:
            from music.backends.embedded_backend import EmbeddedPlayerBackend

            _EmbeddedPlayerBackend = EmbeddedPlayerBackend
        except Exception as e:
            logger.exception("Failed to import EmbeddedPlayerBackend: %s", e)
            pass  # Silent OK: optional import fallback
    return _EmbeddedPlayerBackend


def _load_vlc_backend_class() -> type[BaseBackend] | None:
    """Lazily load VLCBackend class."""
    global _VLCBackend
    if _VLCBackend is None:
        try:
            from music.backends.vlc import VLCBackend

            _VLCBackend = VLCBackend
        except Exception as e:
            logger.exception("Failed to import VLCBackend: %s", e)
            pass  # Silent OK: optional import fallback
    return _VLCBackend


def _load_qt_media_backend_class() -> type[BaseBackend] | None:
    """Lazily load QtMediaBackend class."""
    global _QtMediaBackend
    if _QtMediaBackend is None:
        try:
            from music.backends.qt_media import _QT_AVAILABLE, QtMediaBackend

            if _QT_AVAILABLE:
                _QtMediaBackend = QtMediaBackend
            else:
                logger.debug("QtMediaBackend skipped: PyQt6.QtMultimedia unavailable")
        except Exception as e:
            logger.debug("Failed to import QtMediaBackend: %s", e)
    return _QtMediaBackend


class BackendSelectionResult:
    """
    Result of backend selection.

    Attributes:
        backend: Selected backend instance (None if selection failed)
        backend_name: Name of the selected backend
        selection_method: How the backend was selected ('embedded', 'vlc', 'engine_manager', 'reuse')
        error: Error message if selection failed
        metadata: Additional metadata about the selection
    """

    def __init__(
        self,
        backend: BaseBackend | None = None,
        backend_name: str | None = None,
        selection_method: str = "unknown",
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        self.backend = backend
        self.backend_name = backend_name
        self.selection_method = selection_method
        self.error = error
        self.metadata = metadata or {}

    @property
    def success(self) -> bool:
        """Check if backend selection was successful."""
        return self.backend is not None and self.error is None


class BackendSelector:
    """
    Selects and initializes appropriate playback backends.

    Handles:
    - EmbeddedPlayerBackend selection
    - VLC backend selection
    - Engine manager integration
    - Backend availability checks
    - E2E mode handling
    - Backend reuse optimization

    This class centralizes all backend selection logic to make it easier to
    reason about and maintain. It handles complex fallback chains and error
    recovery.
    """

    def __init__(
        self,
        logger: logging.Logger | None = None,
        engine_manager: Any | None = None,
        existing_backend: BaseBackend | None = None,
    ):
        """
        Initialize the backend selector.

        Args:
            logger: Optional logger instance. If not provided, creates a default logger.
            engine_manager: Optional PlaybackEngineManager instance for provider-based backends.
            existing_backend: Optional existing backend instance to reuse if compatible.
        """
        self._logger = logger or get_logger("viola.playback.backend_selector")
        self._engine_manager = engine_manager
        self._existing_backend = existing_backend

    def select_backend(
        self,
        item: QueueItem,
        route: PlaybackRoute,
        upcoming_items: list[QueueItem] | None = None,
    ) -> BackendSelectionResult:
        """
        Select backend for item based on routing decision.

        This is the main entry point for backend selection. It considers:
        1. Routing decision (embedded, vlc_stream, external_browser)
        2. Engine manager availability
        3. Existing backend reuse
        4. E2E mode requirements
        5. Backend availability

        Args:
            item: QueueItem to select backend for
            route: PlaybackRoute from routing decision
            upcoming_items: Optional list of upcoming queue items for context

        Returns:
            BackendSelectionResult with selected backend or error
        """
        # Check E2E mode first (forces embedded backend)
        embedded_only = self._is_embedded_only_mode()
        if embedded_only and route.route_type != "external_browser":
            return self._select_embedded_backend_for_e2e(item, route)

        # Route to appropriate selection method
        if route.route_type in ("embedded_webview", "embedded_iframe_webview"):
            return self._select_embedded_backend(item, route, upcoming_items)

        if route.route_type == "external_browser":
            # External browser doesn't need a backend instance
            return BackendSelectionResult(
                backend=None,
                backend_name="browser",
                selection_method="external_browser",
                metadata={"playback_mode": "external_browser"},
            )

        if route.route_type == "qt_media":
            return self._select_qt_media_backend(item, route)

        if route.route_type == "vlc_stream":
            return self._select_vlc_backend(item, route)

        # Unknown route type
        return BackendSelectionResult(
            backend=None,
            backend_name=None,
            selection_method="unknown",
            error=f"Unknown route type: {route.route_type}",
            metadata={"route_type": route.route_type},
        )

    def _select_embedded_backend(
        self,
        item: QueueItem,
        route: PlaybackRoute,
        upcoming_items: list[QueueItem] | None = None,
    ) -> BackendSelectionResult:
        """
        Select embedded backend for item.

        Tries in order:
        1. Engine manager (if available and can handle item)
        2. Reuse existing EmbeddedPlayerBackend (if compatible)
        3. Create new EmbeddedPlayerBackend
        4. Fallback to engine manager (if EmbeddedPlayerBackend unavailable)

        Args:
            item: QueueItem to select backend for
            route: PlaybackRoute with routing decision
            upcoming_items: Optional list of upcoming queue items

        Returns:
            BackendSelectionResult with embedded backend or error
        """
        # Try engine manager first (preferred for provider-based playback)
        if self._engine_manager is not None:
            try:
                provider_id = self._engine_manager.identify_provider(item)
                if provider_id:
                    engine = self._engine_manager.get_engine(provider_id)
                    if engine and engine.is_available():
                        selected_backend = self._engine_manager.attach_backend(
                            item,
                            upcoming=upcoming_items or [],
                            default_backend=None,  # Explicitly reject VLC for embedded items
                        )
                        if selected_backend is not None:
                            return BackendSelectionResult(
                                backend=selected_backend,
                                backend_name=type(selected_backend).__name__,
                                selection_method="engine_manager",
                                metadata={
                                    "provider": provider_id,
                                    "route_type": route.route_type,
                                },
                            )
            except Exception as exc:
                self._logger.warning("Engine manager backend selection failed, falling back: %r", exc)

        # Try to reuse existing embedded backend (E2E mode optimization)
        embedded_only = self._is_embedded_only_mode()
        if embedded_only and self._existing_backend is not None:
            EmbeddedPlayerBackend = _load_embedded_backend_class()
            if EmbeddedPlayerBackend is not None and isinstance(self._existing_backend, EmbeddedPlayerBackend):
                return BackendSelectionResult(
                    backend=self._existing_backend,
                    backend_name="EmbeddedPlayerBackend",
                    selection_method="reuse_e2e",
                    metadata={"route_type": route.route_type},
                )

        # Try to create new EmbeddedPlayerBackend
        try:
            backend = self._load_embedded_backend()
            if backend is not None:
                return BackendSelectionResult(
                    backend=backend,
                    backend_name="EmbeddedPlayerBackend",
                    selection_method="embedded_direct",
                    metadata={"route_type": route.route_type},
                )
        except Exception as exc:
            self._logger.warning("Failed to load embedded backend: %r", exc)

        # Fallback: Try engine manager again (if EmbeddedPlayerBackend failed)
        if self._engine_manager is not None:
            try:
                selected_backend = self._engine_manager.attach_backend(
                    item,
                    upcoming=upcoming_items or [],
                    default_backend=None,  # Explicitly reject VLC for embedded items
                )
                if selected_backend is not None:
                    return BackendSelectionResult(
                        backend=selected_backend,
                        backend_name=type(selected_backend).__name__,
                        selection_method="engine_manager_fallback",
                        metadata={"route_type": route.route_type},
                    )
            except Exception as exc:
                self._logger.exception("Engine manager fallback failed: %r", exc)

        # All selection methods failed
        return BackendSelectionResult(
            backend=None,
            backend_name=None,
            selection_method="embedded_failed",
            error="Embedded backend unavailable and engine manager fallback failed",
            metadata={
                "route_type": route.route_type,
                "provider": route.provider_id,
            },
        )

    def _select_qt_media_backend(self, item: QueueItem, route: PlaybackRoute) -> BackendSelectionResult:
        """Select QtMediaBackend for local file playback.

        Falls back to VLC if QtMediaBackend is unavailable.
        """
        QtMediaBackend = _load_qt_media_backend_class()

        # Try to reuse existing QtMediaBackend
        if (
            QtMediaBackend is not None
            and self._existing_backend is not None
            and isinstance(self._existing_backend, QtMediaBackend)
        ):
            return BackendSelectionResult(
                backend=self._existing_backend,
                backend_name="QtMediaBackend",
                selection_method="reuse_qt_media",
                metadata={"route_type": route.route_type},
            )

        # Try to create new QtMediaBackend
        if QtMediaBackend is not None:
            try:
                backend = QtMediaBackend()
                return BackendSelectionResult(
                    backend=backend,
                    backend_name="QtMediaBackend",
                    selection_method="qt_media_direct",
                    metadata={"route_type": route.route_type},
                )
            except Exception as exc:
                self._logger.warning("QtMediaBackend creation failed: %r", exc)

        # Fallback to VLC
        self._logger.warning("QtMediaBackend unavailable, falling back to VLC for local file")
        return self._select_vlc_backend(item, route)

    def _select_vlc_backend(self, item: QueueItem, route: PlaybackRoute) -> BackendSelectionResult:
        """
        Select VLC backend for item.

        Tries in order:
        1. Reuse existing VLCBackend (if compatible)
        2. Create new VLCBackend

        Args:
            item: QueueItem to select backend for
            route: PlaybackRoute with routing decision

        Returns:
            BackendSelectionResult with VLC backend or error
        """
        # Try to reuse existing VLC backend
        VLCBackend = _load_vlc_backend_class()
        if (
            VLCBackend is not None
            and self._existing_backend is not None
            and isinstance(self._existing_backend, VLCBackend)
        ):
            return BackendSelectionResult(
                backend=self._existing_backend,
                backend_name="VLCBackend",
                selection_method="reuse_vlc",
                metadata={"route_type": route.route_type},
            )

        # Try to create new VLC backend
        try:
            if VLCBackend is None:
                raise BackendError("VLCBackend class not available")

            backend = VLCBackend()
            return BackendSelectionResult(
                backend=backend,
                backend_name="VLCBackend",
                selection_method="vlc_direct",
                metadata={"route_type": route.route_type},
            )
        except Exception as exc:
            self._logger.warning(
                "VLCBackend unavailable for item (provider=%s, mode=%s): %r",
                route.provider_id or "unknown",
                route.route_type,
                exc,
            )

            # If we have an existing backend, use it as fallback
            if self._existing_backend is not None:
                return BackendSelectionResult(
                    backend=self._existing_backend,
                    backend_name=type(self._existing_backend).__name__,
                    selection_method="reuse_fallback",
                    metadata={
                        "route_type": route.route_type,
                        "vlc_error": str(exc),
                    },
                )

            # No backend available
            return BackendSelectionResult(
                backend=None,
                backend_name=None,
                selection_method="vlc_failed",
                error=f"VLC backend unavailable: {exc}",
                metadata={
                    "route_type": route.route_type,
                    "provider": route.provider_id,
                },
            )

    def _select_embedded_backend_for_e2e(self, item: QueueItem, route: PlaybackRoute) -> BackendSelectionResult:
        """
        Select embedded backend for E2E mode.

        E2E mode forces embedded backend even for items that might normally
        use VLC. This is used for testing and development.

        Args:
            item: QueueItem to select backend for
            route: PlaybackRoute with routing decision

        Returns:
            BackendSelectionResult with embedded backend or error
        """
        # Try to reuse existing embedded backend
        EmbeddedPlayerBackend = _load_embedded_backend_class()
        if (
            EmbeddedPlayerBackend is not None
            and self._existing_backend is not None
            and isinstance(self._existing_backend, EmbeddedPlayerBackend)
        ):
            return BackendSelectionResult(
                backend=self._existing_backend,
                backend_name="EmbeddedPlayerBackend",
                selection_method="reuse_e2e",
                metadata={"route_type": route.route_type, "e2e_mode": True},
            )

        # Try to create new embedded backend
        try:
            backend = self._load_embedded_backend()
            if backend is None:
                raise BackendError("E2E mode requires EmbeddedPlayerBackend but it is unavailable")
            return BackendSelectionResult(
                backend=backend,
                backend_name="EmbeddedPlayerBackend",
                selection_method="embedded_e2e",
                metadata={"route_type": route.route_type, "e2e_mode": True},
            )
        except Exception as exc:
            self._logger.error("E2E mode: failed to load embedded backend: %r", exc)
            return BackendSelectionResult(
                backend=None,
                backend_name=None,
                selection_method="embedded_e2e_failed",
                error=f"E2E mode requires embedded backend but it failed: {exc}",
                metadata={"route_type": route.route_type, "e2e_mode": True},
            )

    def _load_embedded_backend(
        self,
        video_widget: Any | None = None,
        headless: bool = True,
    ) -> BaseBackend | None:
        """
        Load EmbeddedPlayerBackend instance.

        Args:
            video_widget: Optional video widget for embedded playback
            headless: Whether to run in headless mode

        Returns:
            EmbeddedPlayerBackend instance or None if unavailable
        """
        EmbeddedPlayerBackend = _load_embedded_backend_class()
        if EmbeddedPlayerBackend is None:
            return None

        try:
            # Create backend with available parameters
            return EmbeddedPlayerBackend()
        except Exception as exc:
            self._logger.warning("Failed to create EmbeddedPlayerBackend: %r", exc)
            return None

    def _is_embedded_only_mode(self) -> bool:
        """
        Check if E2E/embedded-only mode is enabled.

        Returns:
            True if embedded-only mode is enabled
        """
        return settings.embedded_only
