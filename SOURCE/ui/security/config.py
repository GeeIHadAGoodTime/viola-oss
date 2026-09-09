"""
Security Configuration Management

Unified configuration for all security features.
Supports environment variables, settings, and defaults.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from config import env
from config.youtube_embed import configured_youtube_embed_origin
from core.constants import LOCALHOST, LOCALHOST_NAME
from core.logging_config import get_logger
from core.secrets_mask import mask_secret

logger = get_logger(__name__)

from ui.security.bootstrap import (
    BootstrapKeyInfo,
    BootstrapSecretInfo,
    ensure_bootstrap_api_key,
    ensure_ws_token_secret,
    is_bootstrap_key_acknowledged,
)


@dataclass
class SecurityConfig:
    """Security configuration with defaults and validation."""

    # Authentication
    auth_enabled: bool = True
    auth_api_key: str | None = None
    auth_token_secret: str | None = None
    auth_required_endpoints: list[str] | None = None  # None = all endpoints
    auth_exempt_paths: list[str] | None = None  # Paths exempt from auth even if they match required prefixes
    dev_mode: bool = False
    bootstrap_key_generated: bool = field(default=False, init=False, repr=False)
    bootstrap_key_path: Path | None = field(default=None, init=False, repr=False)
    token_secret_generated: bool = field(default=False, init=False, repr=False)
    token_secret_path: Path | None = field(default=None, init=False, repr=False)

    # Rate Limiting
    rate_limiting_enabled: bool = True
    rate_limiting_mandatory: bool = True  # Fail startup if unavailable
    rate_limiting_default: str = "200/minute"
    rate_limiting_storage_url: str | None = None  # Redis URL for distributed
    trusted_proxies: list[str] | None = None  # Trusted proxy IPs/CIDRs/hostnames for forwarded-IP validation

    # Resource Limits
    max_file_size_mb: int = 50  # 50MB default
    max_request_size_mb: int = 10  # 10MB default
    max_websocket_connections: int = 10
    max_websocket_message_size_kb: int = 64  # 64KB
    max_command_history_size: int = 5000  # characters

    # Error Handling
    error_sanitization_enabled: bool = True
    show_detailed_errors: bool = False  # Debug mode only

    # WebSocket
    # SECURITY: Enable WebSocket auth by default when auth is enabled
    # This prevents unauthorized access to real-time application state
    websocket_auth_enabled: bool = True  # Changed default to True for security
    websocket_rate_limit: str | None = None  # None = no limit

    # Path Security
    allow_path_traversal: bool = False
    cache_dir_whitelist: list[str] | None = None  # Allowed cache directories

    # CORS
    cors_enabled: bool = False  # Only enable if needed
    cors_origins: list[str] | None = None  # None = ["http://127.0.0.1", "http://localhost"]
    cors_allow_all: bool = False  # Never True in production

    # Security Headers
    security_headers_enabled: bool = True
    security_headers: dict | None = None  # Custom headers

    # Debug Mode
    debug_mode: bool = False  # Disables some security features
    debug_routes_enabled: bool = False
    debug_route_allowlist: list[str] | None = None
    debug_routes_require_token: bool = True

    def __post_init__(self):
        """Set defaults for None fields."""
        if self.auth_required_endpoints is None:
            # Secure-by-default: REST and legacy debug paths require auth.
            # NOTE: /ws/ is NOT included here because WebSocket auth is handled
            # separately in ui/websocket/routes.py via verify_websocket(), which
            # supports proper token authentication for WebSocket connections.
            # The HTTP middleware can't handle WebSocket auth (different protocol).
            self.auth_required_endpoints = [
                "/v1/",
                "/api/",
                "/debug/",
            ]

        if self.auth_exempt_paths is None:
            # Paths that must be accessible without authentication even though
            # they fall under an auth-required prefix (/v1/, /api/).
            # Each entry is checked as an exact match OR as a prefix (with trailing /).
            self.auth_exempt_paths = [
                # OAuth consent callback — browsers redirect here from external
                # OAuth providers and cannot supply API key headers.
                "/v1/consent/callback",
                "/api/v1/consent/callback",
                # Versioned health check — must be accessible to load balancers.
                "/v1/health",
                "/v1/health/details",
                "/api/v1/health",
                "/api/v1/health/details",
                # Public website endpoints (waitlist, contact) — intentionally
                # unauthenticated; no user account needed.
                "/api/public/",
                # Build/version info — non-sensitive operational info.
                "/api/about",
                "/api/version/latest",
                "/v1/version",
                # First-run status check happens before the user has paired.
                # Mutating onboarding routes remain authenticated.
                "/v1/onboarding/status",
                # The desktop UI's own crash report. It must not require the
                # thing that may have just broken: a render error can happen
                # before pairing, during sign-in, or *because* the API key
                # injection failed, and an error report that needs a healthy app
                # to be delivered is exactly the reporting-that-reports-nothing
                # shape this endpoint exists to end (2026-08-08; the browser
                # POST 401'd here on the first live proof run). The handler
                # itself refuses any non-loopback client and rebuilds the body
                # through the diagnostic allowlist, so this exemption grants a
                # LAN device nothing.
                "/v1/diagnostics/ui-error",
                # --- Multi-room spoke browser paths (read-only, LAN trust) ---
                # Spoke browsers (phones/tablets) on the LAN don't have the
                # Qt-injected API key.  Same trust model as Chromecast:
                # anything on your WiFi can connect.
                # NOTE: /v1/state removed — it exposes full player state
                # (now_playing, queue, volume). Spokes must authenticate.
                "/v1/clock",
                # NOTE: /api/v1/voice/wake-trigger removed — unauthenticated
                # wake triggers allow any device on the network to activate
                # Viola. Spoke daemons should authenticate with API key or
                # a dedicated spoke auth token (to be implemented).
                # Sync calibration — spokes query latency for playback
                # alignment; read-only and LAN-only.
                "/api/v1/sync/",
                # Streaming metrics removed from exempt list — route has
                # its own require_auth + require_dev_mode dependencies.
            ]

        if self.cors_origins is None:
            # Development defaults. Production should configure CORS origins
            # explicitly via VIOLA_CORS_ORIGINS.
            self.cors_origins = [
                f"http://{LOCALHOST}",
                f"http://{LOCALHOST}:3000",
                f"http://{LOCALHOST}:5173",
                f"http://{LOCALHOST}:8080",
                f"http://{LOCALHOST_NAME}",
                f"http://{LOCALHOST_NAME}:3000",
                f"http://{LOCALHOST_NAME}:5173",
                f"http://{LOCALHOST_NAME}:8080",
            ]

        if self.cache_dir_whitelist is None:
            # Default safe directories
            # mt-ok: directory-whitelist config entry, not a per-user file path
            self.cache_dir_whitelist = [
                "data/cache",
                ".viola/cache",
            ]

        if self.security_headers is None:
            # Enhanced security headers with modern standards
            self.security_headers = {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "SAMEORIGIN",
                "X-XSS-Protection": "1; mode=block",
                # Content Security Policy (restrictive by default)
                # Allow YouTube IFrame API for embedded player (required for video playback)
                # NOTE: qrc: is required for Qt WebChannel (qwebchannel.js) - enables JS→Python bridge
                # NOTE: 'unsafe-inline' required for Qt WebChannel integration and inline styles.
                # Cannot use nonce-based CSP due to Qt WebEngine limitations.
                # NOTE: api.useviola.com is the cloud GoTrue host (see
                # ui/react-app/src/lib/gotrue_client.ts DEFAULT_GOTRUE_URL). The
                # React Supabase client calls /auth/v1/* there directly; without
                # this entry every desktop sign-in is blocked by CSP. The
                # scripts/check_csp_gotrue_url_coherence.py gate enforces that
                # the GoTrue URL host stays in this allowlist.
                "Content-Security-Policy": (
                    "default-src 'self'; "
                    "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' qrc: https://www.youtube.com https://s.ytimg.com; "
                    "style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data: blob: https:; "
                    # ``blob:`` is required so the Knowledge inbox drag-out
                    # can fetch its own Blob-URL backed file when the user
                    # drags a row to another app — Chromium gates fetch on
                    # blob URLs through connect-src.
                    "connect-src 'self' blob: ws: wss: "
                    "https://api.useviola.com wss://api.useviola.com "
                    "https://www.youtube.com https://*.youtube.com https://*.ytimg.com; "
                    "font-src 'self' data:; "
                    "frame-src 'self' https://www.youtube.com https://*.youtube.com "
                    + configured_youtube_embed_origin(env.get("VITE_YOUTUBE_EMBED_URL"))
                    + "; "
                    "frame-ancestors 'self';"
                ),
                # Permissions Policy (limit browser features)
                # microphone=self and camera=self needed for spoke PTT and wake word streaming
                "Permissions-Policy": ("geolocation=(), microphone=(self), camera=(self), payment=(), usb=()"),
                # Referrer Policy (control referrer header)
                "Referrer-Policy": "strict-origin-when-cross-origin",
                # Cross-Origin Resource Policy (restrict resource loading)
                "Cross-Origin-Resource-Policy": "same-origin",
                # Cross-Origin Opener Policy (isolate browsing context)
                "Cross-Origin-Opener-Policy": "same-origin",
            }

            # Strict-Transport-Security: only sent when SSL/TLS is active.
            # HSTS on plain HTTP is ignored by browsers and misleading.
            # SSL trust model: Viola uses self-signed certificates for local
            # HTTPS (data/secrets/viola_cert.pem). The VIOLA_SSL_VERIFY setting
            # defaults to True; it is only explicitly disabled for self-signed
            # certs in local/development environments. Production deployments
            # should use proper CA-signed certificates.
            if os.environ.get("VIOLA_SSL_ENABLED"):
                self.security_headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        if self.debug_route_allowlist is None:
            self.debug_route_allowlist = []

    @classmethod
    def from_env(cls) -> SecurityConfig:
        """Load configuration from environment variables."""
        # NOTE: Using env.get() throughout this method is acceptable per CLAUDE.md.
        # SecurityConfig is security infrastructure configuration (separate from application
        # settings in config.settings), and uses VIOLA_SECURITY_* namespace for isolation.
        # See CLAUDE.md section 2: "Direct env.get() should only be used for system-level configuration"
        config = cls()

        # Authentication
        dev_mode_env = env.get("VIOLA_SECURITY_DEV_MODE", "false").lower() == "true"
        config.dev_mode = dev_mode_env

        auth_enabled_env = env.get("VIOLA_SECURITY_AUTH_ENABLED")
        if auth_enabled_env is None:
            # Secure by default unless dev mode explicitly requested.
            config.auth_enabled = not dev_mode_env
        else:
            config.auth_enabled = auth_enabled_env.lower() == "true"

        config.auth_api_key = env.get("VIOLA_SECURITY_API_KEY")
        config.auth_token_secret = env.get("VIOLA_SECURITY_TOKEN_SECRET")

        # Generate or load bootstrap API key when required.
        if config.auth_enabled and not config.auth_api_key:
            bootstrap_info: BootstrapKeyInfo = ensure_bootstrap_api_key()
            config.auth_api_key = bootstrap_info.api_key
            config.bootstrap_key_generated = bootstrap_info.was_created
            config.bootstrap_key_path = bootstrap_info.path

            if bootstrap_info.was_created:
                masked = mask_secret(bootstrap_info.api_key)
                logger.info(
                    "🔐 Generated initial API key at %s (masked: %s)",
                    bootstrap_info.path,
                    masked,
                )
            elif not is_bootstrap_key_acknowledged():
                logger.info(
                    "🔐 Loaded existing Viola API key from %s; onboarding reminder pending.",
                    bootstrap_info.path,
                )

        if config.auth_enabled and not config.auth_token_secret:
            try:
                from config.settings import settings as _app_settings

                app_surface = str(getattr(_app_settings, "app_surface", "desktop")).lower()
            except Exception:
                app_surface = "desktop"

            if app_surface != "cloud":
                token_info: BootstrapSecretInfo = ensure_ws_token_secret()
                config.auth_token_secret = token_info.secret
                config.token_secret_generated = token_info.was_created
                config.token_secret_path = token_info.path
                logger.info(
                    "%s local WS auth token secret at %s",
                    "Generated" if token_info.was_created else "Loaded",
                    token_info.path,
                )

        # Rate Limiting
        config.rate_limiting_enabled = env.get("VIOLA_SECURITY_RATE_LIMIT_ENABLED", "true").lower() != "false"
        config.rate_limiting_mandatory = env.get("VIOLA_SECURITY_RATE_LIMIT_MANDATORY", "true").lower() != "false"
        config.rate_limiting_default = env.get("VIOLA_SECURITY_RATE_LIMIT_DEFAULT", "200/minute")
        config.rate_limiting_storage_url = env.get("VIOLA_SECURITY_RATE_LIMIT_STORAGE_URL")
        if not config.rate_limiting_storage_url and env.get("VIOLA_RATE_LIMITER_BACKEND", "").lower() == "redis":
            config.rate_limiting_storage_url = env.get("VIOLA_REDIS_URL")

        # Resource Limits
        config.max_file_size_mb = int(env.get("VIOLA_SECURITY_MAX_FILE_SIZE_MB", "50"))
        config.max_request_size_mb = int(env.get("VIOLA_SECURITY_MAX_REQUEST_SIZE_MB", "10"))
        config.max_websocket_connections = int(env.get("VIOLA_SECURITY_MAX_WS_CONNECTIONS", "10"))
        config.max_websocket_message_size_kb = int(env.get("VIOLA_SECURITY_MAX_WS_MESSAGE_SIZE_KB", "64"))

        # Error Handling
        # Error sanitization can only be disabled in explicit debug mode
        sanitize_errors_env = env.get("VIOLA_SECURITY_SANITIZE_ERRORS", "true")
        config.error_sanitization_enabled = sanitize_errors_env.lower() != "false"
        # Only allow disabling if debug mode is enabled (safety check)
        if not config.error_sanitization_enabled and not config.debug_mode:
            logger.warning("🔐 Error sanitization cannot be disabled without debug mode. Enabling sanitization.")
            config.error_sanitization_enabled = True
        config.show_detailed_errors = env.get("VIOLA_SECURITY_DETAILED_ERRORS", "false").lower() == "true"

        # WebSocket
        # SECURITY: Default to enabled when auth is enabled (secure by default)
        ws_auth_env = env.get("VIOLA_SECURITY_WS_AUTH")
        if ws_auth_env is None:
            # Default to same as auth_enabled (secure by default)
            config.websocket_auth_enabled = config.auth_enabled
        else:
            config.websocket_auth_enabled = ws_auth_env.lower() == "true"

        # Trusted proxies (exact IPs, CIDRs, or resolvable hostnames)
        trusted_proxies_env = env.get("VIOLA_SECURITY_TRUSTED_PROXIES", "")
        if trusted_proxies_env:
            config.trusted_proxies = [ip.strip() for ip in trusted_proxies_env.split(",") if ip.strip()]
        else:
            # Default: only localhost
            config.trusted_proxies = [LOCALHOST, "::1", LOCALHOST_NAME]

        # CORS
        config.cors_enabled = env.get("VIOLA_ENABLE_CORS", "false").lower() == "true"
        config.cors_allow_all = env.get("VIOLA_CORS_ALLOW_ALL", "false").lower() == "true"

        # Debug Mode
        config.debug_mode = env.get("VIOLA_DEBUG", "false").lower() == "true"
        config.debug_routes_enabled = env.get("VIOLA_DEBUG_ROUTES_ENABLED", "false").lower() == "true"
        allowlist_raw = env.get("VIOLA_DEBUG_ROUTE_ALLOWLIST")
        if allowlist_raw:
            config.debug_route_allowlist = [item.strip() for item in allowlist_raw.split(",") if item.strip()]
        config.debug_routes_require_token = env.get("VIOLA_DEBUG_ROUTES_REQUIRE_TOKEN", "true").lower() != "false"

        # Load from settings manager if available (with validation)
        try:
            from ui.settings_manager import get_settings_manager

            settings_mgr = get_settings_manager()

            # Merge security settings (with validation)
            security_settings: dict[str, object] = settings_mgr.get("security", {})
            if security_settings:
                # Critical security settings cannot be overridden by settings file
                # They must be set via environment variables
                protected_settings = {
                    "auth_enabled",
                    "auth_api_key",
                    "auth_token_secret",
                    "rate_limiting_enabled",
                    "rate_limiting_mandatory",
                    "error_sanitization_enabled",
                }

                # Update config from settings (non-protected settings only)
                for key, value in security_settings.items():
                    if hasattr(config, key):
                        # Check if setting is protected
                        if key in protected_settings:
                            # Protected settings can NEVER be set via settings file.
                            # They must be set via environment variables only.
                            env_key = f"VIOLA_SECURITY_{key.upper()}"
                            logger.warning(
                                "🔐 Security setting '%s' is protected and cannot be set via settings file. Use environment variable %s instead.",
                                key,
                                env_key,
                            )
                            continue
                        # Non-protected: allow update from settings file
                        setattr(config, key, value)
        except Exception:
            logger.debug("Settings manager not available for security config")
            pass  # Settings manager not available

        return config

    def validate(self) -> list[str]:
        """Validate configuration and return list of warnings and errors."""
        warnings = []
        errors = []

        # Critical checks (fail startup)
        if self.debug_mode and not self.error_sanitization_enabled:
            errors.append("CRITICAL: Debug mode with error sanitization disabled - may leak sensitive info")

        if self.cors_enabled and self.cors_allow_all:
            errors.append("CRITICAL: CORS enabled with allow_all=True - security risk! Never use in production.")

        if self.auth_enabled and not self.auth_api_key and not self.auth_token_secret:
            errors.append("CRITICAL: Authentication enabled but no API key or token secret configured")

        # Warnings (log but continue)
        if self.max_file_size_mb > 100:
            warnings.append(f"Large file size limit ({self.max_file_size_mb}MB) may allow DoS")

        if self.max_websocket_connections > 100:
            warnings.append(f"High WebSocket connection limit ({self.max_websocket_connections}) may allow DoS")

        # Raise errors for critical issues
        if errors:
            for error in errors:
                logger.error("Security config error: %s", error)
            raise ValueError(
                "Critical security configuration errors detected. "
                "Fix these issues before starting the server.\n" + "\n".join(errors)
            )

        return warnings


# Global config instance (thread-safe module-level singleton)
_security_config_lock = threading.Lock()
_security_config: SecurityConfig | None = None


def _build_security_config() -> SecurityConfig:
    config = SecurityConfig.from_env()
    warnings = config.validate()
    if warnings:
        for warning in warnings:
            logger.warning("Security config warning: %s", warning)
    return config


def get_security_config() -> SecurityConfig:
    """Get global security configuration (thread-safe)."""
    global _security_config
    if _security_config is not None:
        return _security_config

    with _security_config_lock:
        if _security_config is not None:
            return _security_config
        _security_config = _build_security_config()
        return _security_config


def reset_security_config() -> None:
    """Clear cached security configuration (for tests)."""
    global _security_config
    with _security_config_lock:
        _security_config = None
