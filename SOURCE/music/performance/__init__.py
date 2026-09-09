"""
music/performance/__init__.py

High-performance music resolution system with caching, parallelization, and freshness management.
Provides a unified integration layer for all performance improvements.
"""

from __future__ import annotations

from .integration import PerformanceConfig, PerformanceEnhancedPlayer

__all__ = ["PerformanceConfig", "PerformanceEnhancedPlayer"]
