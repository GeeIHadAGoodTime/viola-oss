"""
WebEngine Capability Detection - Single Source of Truth

This module provides deterministic detection of PyQt6 WebEngine availability.
Used to determine if YouTube embedded playback is possible on this system.
"""

from __future__ import annotations

from core.logging_config import get_logger

_logger = get_logger("viola.webengine_capability")
_MISSING_MESSAGE = "This device cannot play YouTube content (WebView unsupported)."
_availability_cache: bool | None = None
_needs_qapp_reprobe = False


def _probe_webengine() -> tuple[bool, bool]:
    """
    Attempt to verify that Qt WebEngine is usable.

    Returns:
        tuple(available, needs_qapp_reprobe)
    """
    try:
        from PySide6.QtWebEngineCore import QWebEngineProfile

        # NOTE: QWebEngineView import removed - we no longer instantiate it in the probe
        # to avoid native crashes from async deleteLater() cleanup
    except ImportError:
        return False, False

    try:
        # NOTE: defaultProfile() is used here for READ-ONLY probing only.
        # The app's actual profile is created in webview_window.py::_get_viola_profile()
        # as a named "viola" profile with persistent storage.
        # Do NOT use defaultProfile() for any web page — it is off-the-record in Qt6.
        profile = QWebEngineProfile.defaultProfile()
        if profile is None:
            return False, False
        # Touch a cheap attribute to ensure WebEngine runtime is initialized
        _ = profile.persistentStoragePath()
    except RuntimeError as exc:  # Missing QApplication/QGuiApplication
        message = str(exc).lower()
        if "qguiapplication" in message or "qapplication" in message:
            _logger.debug(
                "WebEngine modules detected but Qt application not initialized yet; deferring full verification."
            )
            return True, True
        _logger.debug("WebEngine profile initialization failed: %s", exc)
        return False, False
    except Exception as e:
        _logger.exception("WebEngine profile probe failed: %s", e)
        return False, False

    # NOTE: We intentionally do NOT instantiate QWebEngineView() here.
    # Creating and immediately destroying a QWebEngineView with deleteLater()
    # causes native crashes (Windows STATUS_ACCESS_VIOLATION / 0xC0000005) because
    # Chromium's async cleanup doesn't complete before the next Qt operation.
    # The profile probe above is sufficient to verify WebEngine availability.
    # Actual view creation failures are handled gracefully at runtime by
    # EmbeddedPlayerManager._get_embedded_webview() which returns None on failure.

    return True, False


def is_available() -> bool:
    """
    Check if PyQt6 WebEngine components can be imported and used.
    """
    global _availability_cache, _needs_qapp_reprobe

    if _needs_qapp_reprobe:
        try:
            from PySide6.QtWidgets import QApplication

            if QApplication.instance() is not None:
                _availability_cache = None
                _needs_qapp_reprobe = False
        except Exception as e:
            # If Qt can't be imported yet, keep existing state.
            _logger.exception("Failed to check QApplication instance: %s", e)
            pass

    if _availability_cache is not None and not _needs_qapp_reprobe:
        return _availability_cache

    available, requires_qapp = _probe_webengine()
    if requires_qapp:
        _needs_qapp_reprobe = True
        # Return optimistic True so callers know the modules exist, but keep cache unset
        # so we re-probe once a QApplication is active.
        return True

    _availability_cache = available
    return available


def explain_missing() -> str:
    """
    Return a user-facing explanation for why WebEngine is unavailable.
    """
    return _MISSING_MESSAGE


from typing import Any


def assert_available_or_explain(logger: Any = None) -> bool:
    """
    Check WebEngine availability and log a warning if missing.

    Args:
        logger: Any logger-like object with a warning() method (or None for default)
    """
    log = logger if logger is not None else _logger

    if is_available():
        return True

    explanation = explain_missing()
    log.warning("WEBENGINE_CAPABILITY: %s", explanation)
    return False
