"""
Capability Resolver — Resolution Waterfall for unconfigured domains.

Intercepts requests for domains that are not connected and returns
appropriate guidance instead of letting the AI improvise.

Tier logic:
  CONNECTED   → return None (let normal pipeline handle it)
  AVAILABLE   → return setup guidance response (proactive OAuth for Google services)
  DISCOVERED  → return "I found X, want me to connect?"
  UNKNOWN     → return general guidance or None (let AI handle with constraints)
"""

from __future__ import annotations

import threading

from core.logging_config import get_logger

logger = get_logger(__name__)

# Domains that use Google OAuth and can be auto-opened in the browser.
# Maps domain_id -> human-readable service name for the OAuth message.
_GOOGLE_OAUTH_DOMAINS: dict[str, str] = {
    "email": "Gmail",
}
_RESTRICTED_GOOGLE_OAUTH_DOMAINS: frozenset[str] = frozenset({"email"})

# Domains that can be proactively connected via non-OAuth flows.
# Maps domain_id -> (human-readable name, proactive_connect function name).
_PROACTIVE_CONNECT_DOMAINS: dict[str, str] = {
    "music": "Spotify",
}

# Cooldown: avoid re-opening OAuth if we just opened it recently.
# Stores domain_id -> True while a flow is in progress.
_oauth_in_progress: dict[str, bool] = {}
_oauth_lock = threading.Lock()


def trigger_google_oauth_flow(domain_id: str | None = None) -> bool:
    """Proactively open the Google OAuth sign-in page in the system browser.

    This is the key UX improvement: instead of asking "Want me to set that up?",
    Viola opens the browser immediately and says "just log in."

    Args:
        domain_id: Optional domain that triggered the flow (for cooldown tracking).

    Returns:
        True if the browser was opened, False if skipped (cooldown, not configured, etc.).
    """
    # Cooldown: don't re-open if we already opened for this domain recently
    if domain_id:
        with _oauth_lock:
            if _oauth_in_progress.get(domain_id):
                logger.debug("OAuth flow already in progress for %s, skipping", domain_id)
                return False
            _oauth_in_progress[domain_id] = True

    try:
        from services.oauth.google import is_google_configured, is_google_restricted_features_enabled

        if domain_id in _RESTRICTED_GOOGLE_OAUTH_DOMAINS and not is_google_restricted_features_enabled():
            logger.debug("Skipping Google OAuth for gated restricted domain %s", domain_id)
            return False

        if not is_google_configured():
            logger.info("Google OAuth not configured, cannot auto-open sign-in")
            return False

        from config.settings import settings

        # Don't open browser in test mode or cloud mode
        if getattr(settings, "test_mode", False):
            logger.debug("Skipping OAuth browser open in test mode")
            return False

        cloud_url = getattr(settings, "cloud_url", None)
        if cloud_url:
            logger.debug("Skipping OAuth browser open in cloud mode")
            return False

        import webbrowser

        from config.settings import get_settings

        s = get_settings()
        port = getattr(s, "api_port", None) or 8756
        oauth_url = "http://localhost:%s/auth/oauth/google" % port

        logger.info("Proactively opening Google OAuth sign-in page on local port %s", port)
        webbrowser.open(oauth_url)
        return True

    except Exception:
        logger.debug("Failed to auto-open OAuth browser", exc_info=True)
        return False

    finally:
        # Clear the cooldown after a delay so we don't spam browser windows,
        # but allow re-triggering after 60 seconds.
        if domain_id:

            def _clear_cooldown() -> None:
                with _oauth_lock:
                    _oauth_in_progress.pop(domain_id, None)

            timer = threading.Timer(60.0, _clear_cooldown)
            timer.daemon = True
            timer.start()


def trigger_spotify_browser_auth_login() -> bool:
    """Proactively start the in-app Spotify BrowserAuth login flow.

    Opens the BrowserAuth overlay inside Viola so the user can sign in.
    Uses the same cooldown mechanism as Google OAuth to avoid spamming
    login surfaces.

    Returns:
        True if the login flow was started, False otherwise.
    """
    with _oauth_lock:
        if _oauth_in_progress.get("spotify"):
            logger.debug("Spotify BrowserAuth login already in progress, skipping")
            return False
        _oauth_in_progress["spotify"] = True

    try:
        import httpx

        from config.settings import settings
        from core.constants import DEFAULT_API_PORT

        if getattr(settings, "test_mode", False):
            return False
        if getattr(settings, "cloud_url", None):
            return False

        port = getattr(settings, "api_port", DEFAULT_API_PORT)
        url = "http://localhost:%d/v1/browser/auth/login/spotify" % port

        # Fire-and-forget: best-effort async in a thread to not block pipeline
        import concurrent.futures

        def _do_login() -> bool:
            try:
                with httpx.Client(timeout=10) as client:
                    resp = client.post(url)
                    return resp.status_code == 200
            except Exception:
                return False

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        executor.submit(_do_login)
        executor.shutdown(wait=False)

        logger.info("Proactively starting Spotify BrowserAuth login flow")
        return True

    except Exception:
        logger.debug("Failed to start Spotify BrowserAuth login", exc_info=True)
        return False

    finally:

        def _clear_spotify_cooldown() -> None:
            with _oauth_lock:
                _oauth_in_progress.pop("spotify", None)

        timer = threading.Timer(60.0, _clear_spotify_cooldown)
        timer.daemon = True
        timer.start()


def resolve(
    text: str,
    domain_id: str,
    domain_state: str,
    display_name: str,
    setup_orthodox_path: str | None = None,
    auto_setup_possible: bool = False,
    requirements: list[str] | None = None,
) -> dict[str, object] | None:
    """Run the resolution waterfall for a classified domain.

    Args:
        text: Original user input.
        domain_id: The classified domain ID.
        domain_state: The domain's current state (connected/available/discovered/unknown).
        display_name: Human-readable domain name.
        setup_orthodox_path: Setup guidance string from the domain's SetupGuide.
        auto_setup_possible: Whether Viola can set this up autonomously.
        requirements: Setup requirements carried into pending setup context.

    Returns:
        A dict matching PipelineResult's data shape if the waterfall handled
        the request, or None if the normal pipeline should proceed.
    """
    # Tier 1: Connected — let normal pipeline handle it
    if domain_state == "connected":
        return None

    # Tier 2: Available — known integration exists, not configured
    if domain_state == "available":
        logger.info(
            "Capability resolver: domain %s is available but not configured",
            domain_id,
        )
        if auto_setup_possible:
            # Check if this is a Google OAuth domain — if so, proactively open
            # the browser instead of passively asking.
            google_service = _GOOGLE_OAUTH_DOMAINS.get(domain_id)
            if google_service:
                browser_opened = trigger_google_oauth_flow(domain_id)
                if browser_opened:
                    message = (
                        "I'm opening the sign-in page for %s — "
                        "just log in and I'll take care of the rest." % google_service
                    )
                else:
                    # OAuth not configured or browser didn't open — fall back
                    # to the passive offer.
                    message = "%s is not connected yet. I can help start the setup flow." % display_name
            elif domain_id in _PROACTIVE_CONNECT_DOMAINS:
                # Proactive connect domain (e.g. Spotify) — try to auto-start
                # the login flow just like Google OAuth.
                service_name = _PROACTIVE_CONNECT_DOMAINS[domain_id]
                if domain_id == "music" and trigger_spotify_browser_auth_login():
                    message = "I'm opening %s inside Viola — " "just log in and I'll take it from there." % service_name
                else:
                    message = "%s is not connected yet. I can help start the setup flow." % display_name
            else:
                # Remaining domains (e.g. smart home) — cut to the chase.
                message = "%s is not connected yet. I can help start the setup flow." % display_name
            return {
                "message": message,
                "domain_id": domain_id,
                "resolver_tier": "available",
                "offers_setup": True,
                "setup_context": {
                    "domain_id": domain_id,
                    "display_name": display_name,
                    "orthodox_path": setup_orthodox_path or "",
                    "requirements": list(requirements) if requirements else [],
                },
            }
        else:
            message = _build_available_response(display_name, setup_orthodox_path)
            return {
                "message": message,
                "domain_id": domain_id,
                "resolver_tier": "available",
            }

    # Tier 2.5: Discovered — detected on network but not connected
    if domain_state == "discovered":
        logger.info(
            "Capability resolver: domain %s was discovered on network",
            domain_id,
        )
        message = "I found a %s service on your network. I can help connect it." % display_name
        return {
            "message": message,
            "domain_id": domain_id,
            "resolver_tier": "discovered",
        }

    # Tier 3: Unknown — no known integration
    if domain_state == "unknown":
        logger.info(
            "Capability resolver: domain %s is unknown",
            domain_id,
        )
        # Return None to let the AI handle it with the constraint context
        # already injected by Phase 2
        return None

    # Default: let pipeline continue
    return None


def _build_available_response(
    display_name: str,
    setup_orthodox_path: str | None,
) -> str:
    """Build a natural language response for an available-but-not-configured domain."""
    parts = ["%s is not connected yet." % display_name]

    if setup_orthodox_path:
        parts.append("To get this working: %s." % setup_orthodox_path.rstrip("."))
    else:
        parts.append("Setup is available through Viola's connection flow.")

    return " ".join(parts)
