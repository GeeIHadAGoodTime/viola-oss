# Viola Utils Package

# Debug utilities

from __future__ import annotations

from utils.debug import DebugRingBuffer, TraceEvent, get_debug_ring_buffer

# Diagnostics utilities - these are always attempted to be imported at runtime
# Type aliases are set to the actual classes if available, else None
_DIAGNOSTICS_AVAILABLE = False
_CheckResult = None
_CheckStatus = None
_DiagnosticsCollector = None
_SelfCheck = None

try:
    from utils.diagnostics import (
        CheckResult as _CheckResultImport,
        CheckStatus as _CheckStatusImport,
        DiagnosticsCollector as _DiagnosticsCollectorImport,
        SelfCheck as _SelfCheckImport,
    )

    _CheckResult = _CheckResultImport
    _CheckStatus = _CheckStatusImport
    _DiagnosticsCollector = _DiagnosticsCollectorImport
    _SelfCheck = _SelfCheckImport
    _DIAGNOSTICS_AVAILABLE = True
except ImportError:
    # Graceful fallback if diagnostics not available
    pass


# Re-export for public API - only available if import succeeded
def get_check_result_class():
    """Get CheckResult class if available, else raise ImportError."""
    if _CheckResult is None:
        raise ImportError("Diagnostics not available")
    return _CheckResult


def get_check_status_class():
    """Get CheckStatus class if available, else raise ImportError."""
    if _CheckStatus is None:
        raise ImportError("Diagnostics not available")
    return _CheckStatus


def get_diagnostics_collector_class():
    """Get DiagnosticsCollector class if available, else raise ImportError."""
    if _DiagnosticsCollector is None:
        raise ImportError("Diagnostics not available")
    return _DiagnosticsCollector


def get_self_check_class():
    """Get SelfCheck class if available, else raise ImportError."""
    if _SelfCheck is None:
        raise ImportError("Diagnostics not available")
    return _SelfCheck


# Shared utility exports
from utils.circuit_breaker import CircuitBreaker, CircuitState
from utils.rate_limiter import RateLimiter

# Core utilities and diagnostics accessor functions
__all__ = [
    "_DIAGNOSTICS_AVAILABLE",
    "CircuitBreaker",
    "CircuitState",
    "DebugRingBuffer",
    "RateLimiter",
    "TraceEvent",
    "get_check_result_class",
    "get_check_status_class",
    "get_debug_ring_buffer",
    "get_diagnostics_collector_class",
    "get_self_check_class",
]
