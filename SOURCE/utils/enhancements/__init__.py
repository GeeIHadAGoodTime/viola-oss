"""
High ROI Enhancements Framework

Modular, plugin-friendly enhancements for production-grade reliability,
security, and performance.

Usage:
    from utils.enhancements import enhance_all

    # Auto-enhance all components
    app, player, gpt = enhance_all(app, player, gpt)

    # Or enhance selectively
    from utils.enhancements.connection_pool import enhance_with_pooling
    gpt = enhance_with_pooling(gpt)
"""

from __future__ import annotations

from .async_resolution import AsyncResolutionEnhancer, enhance_with_async
from .connection_pool import ConnectionPoolEnhancer, enhance_with_pooling
from .orchestrator import enhance_all
from .request_tracing import RequestTracingEnhancer, enhance_with_tracing
from .secrets import SecureSettingsManager, enhance_with_encryption

__all__ = [
    "AsyncResolutionEnhancer",
    "ConnectionPoolEnhancer",
    "RequestTracingEnhancer",
    "SecureSettingsManager",
    "enhance_all",
    "enhance_with_async",
    "enhance_with_encryption",
    "enhance_with_pooling",
    "enhance_with_tracing",
]

__version__ = "1.0.0"
