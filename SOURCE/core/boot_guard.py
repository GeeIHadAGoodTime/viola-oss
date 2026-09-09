"""Cloud deployment boot guard.

Validates that security-critical settings are correctly configured before
the server starts in cloud (SaaS) mode. Desktop mode is unrestricted.

Called once during startup, AFTER settings are loaded, BEFORE the server
binds its listen socket. Any violation causes sys.exit(1) — hard stop.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from core.logging_config import get_logger

if TYPE_CHECKING:
    from config.settings import AppConfig

logger = get_logger(__name__)

# Tokens that must never be used in production cloud deployments.
_WEAK_DEBUG_TOKENS: frozenset[str] = frozenset(
    {
        "dev-debug-token-local",
        "test",
        "debug",
        "dev",
        "local",
        "token",
    }
)


def validate_cloud_deployment(settings: AppConfig) -> None:
    """Enforce security invariants for cloud deployments.

    Desktop surface returns immediately.
    Cloud surface checks auth, dev_mode, debug routes, JWT secret strength.
    Any violation is logged as CRITICAL and the process exits.

    Boot-sequence ordering (see docs/BOOT_SEQUENCE.md):
        1. .env loaded by ``config/settings.py`` at import time
        2. ``AppConfig`` constructed (this populates ``app_surface``)
        3. ``validate_cloud_deployment(settings)`` is invoked from the
           server startup path BEFORE any listen socket is bound
        4. VIOLA_DEBUG_AUTH_TOKEN is resolved from ``config.env`` here so
           the check always sees the final post-env-load value.

    Keeping the debug-token check inside this function (rather than in
    module import order) guarantees the token is evaluated after .env has
    been merged by settings.py. The previous nested placement was fragile
    — if ``ui.security.config`` failed to import the token check was
    silently skipped.
    """
    # Accept deployment_mode (canonical) or app_surface (legacy).
    # Case-insensitive to tolerate "Cloud"/"CLOUD" env values.
    deployment = str(getattr(settings, "deployment_mode", None) or getattr(settings, "app_surface", "desktop"))
    if deployment.lower() != "cloud":
        return
    surface = deployment.lower()

    violations: list[str] = []

    # --- Auth must be enabled ---
    if not getattr(settings, "auth_enabled", False):
        msg = "Cloud deployment requires auth_enabled=True (AppConfig)"
        logger.critical(msg)
        violations.append(msg)

    # --- Debug auth token must not be weak (checked first, independent of
    # ui.security import so a broken security module can't mask it) ---
    from config import env as _env

    debug_token = _env.get("VIOLA_DEBUG_AUTH_TOKEN", "")
    if debug_token and debug_token.lower() in _WEAK_DEBUG_TOKENS:
        msg = "Cloud deployment has a weak VIOLA_DEBUG_AUTH_TOKEN — must use a strong secret"
        logger.critical(msg)
        violations.append(msg)

    # --- Security layer auth must be enabled ---
    try:
        from ui.security.config import get_security_config
    except ModuleNotFoundError as exc:
        msg = "Cloud deployment requires ui.security module to be installed " "(ModuleNotFoundError: %s)" % exc.name
        logger.critical(msg)
        violations.append(msg)
    except ImportError as exc:
        msg = (
            "Cloud deployment could not import ui.security.config "
            "(ImportError: %s) — package present but a dependency is missing" % exc
        )
        logger.critical(msg)
        violations.append(msg)
    except SyntaxError as exc:
        msg = "Cloud deployment aborted: ui.security.config has a SyntaxError " "(%s at line %s)" % (
            exc.msg,
            exc.lineno,
        )
        logger.critical(msg)
        violations.append(msg)
    else:
        sec_cfg = get_security_config()
        if not sec_cfg.auth_enabled:
            msg = "Cloud deployment requires SecurityConfig.auth_enabled=True"
            logger.critical(msg)
            violations.append(msg)

        # --- Debug routes must be off ---
        if sec_cfg.debug_routes_enabled:
            msg = "Cloud deployment must not expose debug routes (debug_routes_enabled=True)"
            logger.critical(msg)
            violations.append(msg)

    # --- dev_mode must be off ---
    if getattr(settings, "dev_mode", False):
        msg = "Cloud deployment must not run with dev_mode=True"
        logger.critical(msg)
        violations.append(msg)

    # --- JWT secret must be strong ---
    jwt_secret = getattr(settings, "jwt_secret", None) or ""
    if len(jwt_secret) < 32:
        msg = "Cloud deployment requires jwt_secret of at least 32 characters " "(current length: %d)" % len(jwt_secret)
        logger.critical(msg)
        violations.append(msg)

    # Cloud deployments require monetized builds so billing routes and plan
    # enforcement register. Optional route flags must not disable these guards.
    if not getattr(settings, "is_monetized_build", False):
        msg = (
            "Cloud deployment requires build_profile=monetized (current: %s) — "
            "billing routes silently skip registration otherwise and the entire "
            "monetization stack (Stripe, BTCPay, plan-tier gates) is offline. "
            "Set VIOLA_BUILD_PROFILE=monetized in .env.cloud." % getattr(settings, "build_profile", "<unset>")
        )
        logger.critical(msg)
        violations.append(msg)

    # --- Verdict ---
    if violations:
        logger.critical(
            "Cloud boot guard: %d violation(s) found — refusing to start",
            len(violations),
        )
        sys.exit(1)

    logger.info("Cloud boot guard: all checks passed")
