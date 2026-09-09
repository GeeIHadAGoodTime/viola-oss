"""
Unified Security Plugin System for Viola Server

Modular, plugin-friendly security architecture that can be:
- Enabled/disabled per feature
- Extended with custom plugins
- Configured via environment variables or settings
- Completely optional (graceful degradation)

Core Principles:
- Privacy-first (no data leakage)
- Fail-secure (secure by default)
- Modular (use only what you need)
- Unified (single interface for all security)
"""

from __future__ import annotations

from .auth import AuthenticationPlugin, create_auth_plugin
from .config import SecurityConfig, get_security_config
from .core import SecurityManager, SecurityPlugin, get_security_manager
from .errors import ErrorSanitizer, get_error_sanitizer, sanitize_error_response
from .limits import ResourceLimits, get_resource_limits
from .rate_limiting import RateLimiterPlugin, create_rate_limiter
from .validation import InputValidator, get_input_validator

__all__ = [
    # Plugins
    "AuthenticationPlugin",
    "ErrorSanitizer",
    "InputValidator",
    "RateLimiterPlugin",
    "ResourceLimits",
    "SecurityConfig",
    "SecurityManager",
    # Core
    "SecurityPlugin",
    "create_auth_plugin",
    "create_rate_limiter",
    "get_error_sanitizer",
    "get_input_validator",
    "get_resource_limits",
    "get_security_config",
    "get_security_manager",
    "sanitize_error_response",
]
