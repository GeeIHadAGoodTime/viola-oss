"""
Viola Qt UI Package
React-based Smart Display UI with Qt WebView shell

This package provides the Qt application infrastructure for hosting
the React UI in a native window via QWebEngineView.
"""

from __future__ import annotations

from .api_client import ViolaAPIClient
from .webview_window import ViolaWebViewWindow

__all__ = ["ViolaAPIClient", "ViolaWebViewWindow"]

__version__ = "2.0.0"
