"""
Rate Limiting Plugin

Unified rate limiting for all endpoints.
Mandatory by default (fails startup if unavailable).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from starlette.requests import Request
from starlette.responses import Response

from config import env
from core.constants import LOCALHOST, LOCALHOST_NAME
from core.logging_config import get_logger
from fastapi import FastAPI

from .config import SecurityConfig, get_security_config
from .core import SecurityPlugin

log = get_logger(__name__)


class RateLimiterPlugin(SecurityPlugin):
    """Rate limiting plugin with unified interface."""

    def __init__(self, config: SecurityConfig | None = None):
        super().__init__("rate_limiting", enabled=True)
        self.config = config or get_security_config()
        self.enabled = self.config.rate_limiting_enabled

        self.limiter: Any = None
        self.rate_limit_func: Callable | None = None

        # Try to import slowapi
        self.slowapi_available = False
        try:
            from slowapi import Limiter, _rate_limit_exceeded_handler
            from slowapi.errors import RateLimitExceeded
            from slowapi.util import get_remote_address

            self.Limiter = Limiter
            self._rate_limit_exceeded_handler = _rate_limit_exceeded_handler
            self.RateLimitExceeded = RateLimitExceeded
            self.get_remote_address = get_remote_address
            self.slowapi_available = True
        except ImportError:
            self.slowapi_available = False

    def initialize(self, app: FastAPI) -> None:
        """Initialize rate limiting."""
        if not self.enabled:
            log.info("⏭️ Rate limiting disabled")
            return

        if not self.slowapi_available:
            if self.config.rate_limiting_mandatory:
                raise RuntimeError(
                    "Rate limiting is mandatory but slowapi is not installed. Install with: pip install slowapi"
                )
            else:
                log.warning("⚠️ Rate limiting unavailable (slowapi not installed)")
                log.warning("   Install: pip install slowapi")
                self.enabled = False
                return

        # Initialize limiter
        try:
            # SECURITY: Only trust X-Forwarded-For from trusted proxies
            # Get trusted proxy IPs from config (default: localhost only)
            trusted_proxy_ips = self._get_trusted_proxy_ips()

            # Custom key function that respects X-Forwarded-For (proxy support)
            def get_client_address(request):
                # Get the direct connection IP (proxy IP if behind proxy)
                direct_ip = self.get_remote_address(request)

                # Check X-Forwarded-For header (for proxy support)
                forwarded_for = request.headers.get("X-Forwarded-For")
                if forwarded_for:
                    # SECURITY: Only trust X-Forwarded-For if request comes from trusted proxy
                    if direct_ip in trusted_proxy_ips:
                        # Get first IP (client) from forwarded chain
                        client_ip = forwarded_for.split(",")[0].strip()
                        # Validate IP format (basic check)
                        if self._is_valid_ip(client_ip):
                            return client_ip
                        else:
                            log.warning(
                                "Invalid IP format in X-Forwarded-For: %s, using direct IP: %s",
                                client_ip,
                                direct_ip,
                            )
                    else:
                        # Untrusted proxy - ignore X-Forwarded-For to prevent spoofing
                        log.warning(
                            "Ignoring X-Forwarded-For from untrusted proxy %s, using direct IP for rate limiting",
                            direct_ip,
                        )

                # Fallback to direct connection IP
                return direct_ip

            self.limiter = self.Limiter(
                key_func=get_client_address,  # Proxy-aware
                default_limits=[self.config.rate_limiting_default],
                storage_uri=self.config.rate_limiting_storage_url,
            )

            app.state.limiter = self.limiter
            if self.RateLimitExceeded is not None:
                # Cast required: FastAPI's exception handler expects Exception, but we're registering
                # a specific handler for RateLimitExceeded (which is the documented pattern)
                handler = cast(
                    Callable[[Request, Exception], Response | Awaitable[Response]],
                    self._rate_limit_exceeded_handler,
                )
                app.add_exception_handler(self.RateLimitExceeded, handler)

            # Create rate limit decorator
            def rate_limit(limit: str):
                """Create rate limit decorator."""
                if self.limiter is not None:
                    return self.limiter.limit(limit)
                return lambda func: func  # No-op if limiter unavailable

            self.rate_limit_func = rate_limit

            log.info(
                "🚦 Rate limiting enabled: %s (default)",
                self.config.rate_limiting_default,
            )
            log.info("   Storage: %s", self.config.rate_limiting_storage_url or "in-memory")

        except Exception as e:
            if self.config.rate_limiting_mandatory:
                raise RuntimeError(f"Failed to initialize rate limiting: {e}")
            else:
                log.error("Failed to initialize rate limiting: %s", e)
                self.enabled = False

    def cleanup(self) -> None:
        """Cleanup rate limiting plugin.

        No cleanup needed for rate limiting plugin (stateless).
        """
        pass  # No cleanup needed (stateless plugin)

    def limit(self, limit: str) -> Callable:
        """
        Get rate limit decorator.

        Args:
            limit: Rate limit string (e.g., "30/minute", "100/hour")

        Returns:
            Decorator function
        """
        if not self.enabled or not self.rate_limit_func:
            # Return no-op decorator
            def noop(func):
                return func

            return noop

        return self.rate_limit_func(limit)

    def get_default_limit(self) -> str:
        """Get default rate limit."""
        return self.config.rate_limiting_default

    def _get_trusted_proxy_ips(self) -> set:
        """
        Get list of trusted proxy IPs from config.

        Returns:
            Set of trusted proxy IP addresses
        """
        # Default: only trust localhost proxies (127.0.0.1, ::1)
        default_trusted = {LOCALHOST, "::1", LOCALHOST_NAME}

        # Check SecurityConfig first (preferred)
        if hasattr(self.config, "trusted_proxies") and self.config.trusted_proxies:
            config_trusted = set(self.config.trusted_proxies)
            return config_trusted | default_trusted

        # Fallback to environment variable (for backward compatibility)
        trusted_proxies_env = env.get("VIOLA_SECURITY_TRUSTED_PROXIES", "")
        if trusted_proxies_env:
            # Parse comma-separated list
            trusted_proxies = {ip.strip() for ip in trusted_proxies_env.split(",") if ip.strip()}
            return trusted_proxies | default_trusted

        return default_trusted

    def _is_valid_ip(self, ip: str) -> bool:
        """
        Basic IP address validation.

        Args:
            ip: IP address string to validate

        Returns:
            True if IP format is valid, False otherwise
        """
        import ipaddress

        try:
            ipaddress.ip_address(ip)
            return True
        except ValueError:
            return False


def create_rate_limiter(config: SecurityConfig | None = None) -> RateLimiterPlugin:
    """Create rate limiter plugin."""
    return RateLimiterPlugin(config)
