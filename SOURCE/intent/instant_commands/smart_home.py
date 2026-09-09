"""Service-connect and smart-home instant command handlers."""

from __future__ import annotations

import webbrowser

from services.capability_registry import CapabilityRegistry

from ._base import log


class SmartHomeHandlersMixin:
    """Service-connect and smart-home instant command handlers."""

    _SERVICE_MAP: dict[str, tuple[str, str, str]] = {
        "spotify": ("music", "Spotify", "spotify"),
        "youtube music": ("music", "YouTube Music", "google"),
        "youtube": ("music", "YouTube Music", "google"),
        "google calendar": ("calendar", "Google Calendar", "google"),
        "calendar": ("calendar", "Google Calendar", "google"),
        "gmail": ("email", "Gmail", "google"),
        "google mail": ("email", "Gmail", "google"),
        "email": ("email", "Gmail", "google"),
        "e-mail": ("email", "Gmail", "google"),
        "google contacts": ("contacts", "Google Contacts", "google"),
        "contacts": ("contacts", "Google Contacts", "google"),
        "google drive": ("drive", "Google Drive", "google"),
        "drive": ("drive", "Google Drive", "google"),
        "home assistant": ("smart_home", "Home Assistant", "smart_home"),
        "smart home": ("smart_home", "Smart Home", "smart_home"),
        "hass": ("smart_home", "Home Assistant", "smart_home"),
    }

    async def connect_service(self, params: dict[str, object]) -> dict[str, object]:
        """Handle explicit requests to connect/set up/link an external service.

        Proactively triggers the OAuth flow for Google services and Spotify,
        or returns setup guidance for smart home.
        """
        original = str(params.get("_original_text", ""))
        service_raw = self._extract_service_name(original)
        if not service_raw:
            return {
                "ok": True,
                "message": (
                    "Which service would you like to connect? "
                    "I support Spotify, YouTube Music, Google Calendar, "
                    "Gmail, Google Contacts, Google Drive, and Home Assistant."
                ),
                "data": {},
            }

        service_key = service_raw.lower().strip()
        mapping = self._SERVICE_MAP.get(service_key)
        if not mapping:
            return {
                "ok": True,
                "message": "I don't have a setup flow for %s yet, but I can look into it." % service_raw,
                "data": {},
            }

        domain_id, display_name, category = mapping

        if category == "google":
            return await self._connect_google_service(domain_id, display_name)
        elif category == "spotify":
            return await self._connect_spotify()
        elif category == "smart_home":
            return self._connect_smart_home(display_name)

        return {
            "ok": True,
            "message": "I don't have a built-in setup for %s yet." % display_name,
            "data": {},
        }

    async def home_appliance_query(self, params: dict[str, object]) -> dict[str, object]:
        """Bridge appliance phrases into the smart-home path.

        When smart-home is not connected, reuse the existing setup guidance.
        When it is connected, return a non-final result so the pipeline falls
        through to the later smart-home routing layers that can actually
        interpret device-specific actions.
        """
        registry = CapabilityRegistry.get_instance()
        if registry is None or not registry.is_domain_connected("smart_home"):
            return await self.connect_service(
                {
                    **params,
                    "service": "smart home",
                    "_original_text": "connect smart home",
                }
            )

        return {
            "ok": False,
            "message": "",
            "data": {
                "domain_id": "smart_home",
                "fallback_to_llm": True,
                "original_text": str(params.get("_original_text", "")),
            },
            "error": "smart_home_connected_fallthrough",
        }

    def _extract_service_name(self, text: str) -> str | None:
        """Extract the service name from the original text using the same regex."""
        import re as _re

        explicit_service = str(text).strip()
        if explicit_service and explicit_service in self._SERVICE_MAP:
            return explicit_service

        m = _re.search(
            r"(?P<service>"
            r"spotify|youtube\s+music|youtube|"
            r"google\s+calendar|calendar|"
            r"gmail|google\s+mail|email|e-mail|"
            r"google\s+contacts|contacts|"
            r"google\s+drive|drive|"
            r"home\s+assistant|smart\s+home|hass"
            r")",
            text,
            _re.I,
        )
        return m.group("service") if m else None

    async def _connect_google_service(self, domain_id: str, display_name: str) -> dict[str, object]:
        """Trigger Google OAuth and open the sign-in page in the system browser."""
        try:
            import httpx as _httpx

            from config.settings import settings
            from core.constants import DEFAULT_API_PORT

            port = getattr(settings, "api_port", DEFAULT_API_PORT)
            base = "http://localhost:%d" % port

            async with _httpx.AsyncClient(timeout=10) as client:
                resp = await client.post("%s/auth/oauth/start" % base)
                if resp.status_code == 200:
                    data = resp.json()
                    auth_url = None
                    if isinstance(data, dict):
                        inner = data.get("data", data)
                        if isinstance(inner, dict):
                            auth_url = inner.get("auth_url")

                    if auth_url:
                        webbrowser.open(auth_url)
                        log.info(
                            "Opened Google OAuth for %s: %s",
                            display_name,
                            auth_url[:80],
                        )
                        return {
                            "ok": True,
                            "message": (
                                "I'm opening the sign-in page for %s — "
                                "just log in with your Google account and "
                                "you're all set." % display_name
                            ),
                            "data": {
                                "domain_id": domain_id,
                                "service": display_name,
                                "action": "oauth_started",
                            },
                        }

                if resp.status_code == 501:
                    log.warning("Google OAuth not configured on this instance")
                    return {
                        "ok": False,
                        "message": (
                            "Google Sign-In isn't configured on this Viola instance yet. "
                            "You'll need to add your Google OAuth credentials in the "
                            ".env file first."
                        ),
                        "data": {"domain_id": domain_id, "error": "not_configured"},
                    }

                log.warning(
                    "OAuth start returned status %d for %s",
                    resp.status_code,
                    display_name,
                )
                return {
                    "ok": False,
                    "message": (
                        "I couldn't start the sign-in for %s right now. " "Try again in a moment." % display_name
                    ),
                    "data": {"domain_id": domain_id, "error": "oauth_start_failed"},
                }

        except Exception:
            log.exception("Failed to start Google OAuth for %s", display_name)
            return {
                "ok": False,
                "message": (
                    "Something went wrong starting the sign-in for %s. " "Try again in a moment." % display_name
                ),
                "data": {"domain_id": domain_id, "error": "oauth_exception"},
            }

    async def _connect_spotify(self) -> dict[str, object]:
        """Trigger the in-app Spotify BrowserAuth login flow."""
        try:
            import httpx as _httpx

            from config.settings import settings
            from core.constants import DEFAULT_API_PORT
            from ui.security.bootstrap import load_bootstrap_api_key

            port = getattr(settings, "api_port", DEFAULT_API_PORT)
            base = "http://localhost:%d" % port
            headers: dict[str, str] = {}
            api_key = load_bootstrap_api_key()
            if api_key:
                headers["X-API-Key"] = api_key

            async with _httpx.AsyncClient(timeout=15) as client:
                resp = await client.post("%s/v1/browser/auth/login/spotify" % base, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    inner = data.get("data", data) if isinstance(data, dict) else {}
                    already_logged_in = False
                    if isinstance(inner, dict):
                        already_logged_in = inner.get("logged_in", False)

                    if already_logged_in:
                        log.info("Spotify already connected")
                        return {
                            "ok": True,
                            "message": "Spotify is already connected and ready to go.",
                            "data": {
                                "domain_id": "music",
                                "service": "Spotify",
                                "action": "already_connected",
                            },
                        }

                    if isinstance(inner, dict) and inner.get("controller_attached") is False:
                        return {
                            "ok": False,
                            "message": "Open Viola on this device, then try connecting Spotify again.",
                            "data": {
                                "domain_id": "music",
                                "service": "Spotify",
                                "error": "browser_controller_unavailable",
                            },
                        }

                    log.info("Spotify BrowserAuth login flow started")
                    return {
                        "ok": True,
                        "message": (
                            "I'm opening Spotify in Viola for you - " "just log in and I'll take it from there."
                        ),
                        "data": {
                            "domain_id": "music",
                            "service": "Spotify",
                            "action": "browser_login_started",
                        },
                    }

                log.warning("Spotify BrowserAuth login returned status %d", resp.status_code)
                return {
                    "ok": False,
                    "message": "I couldn't start the Spotify login right now. Open Viola and try again.",
                    "data": {"domain_id": "music", "error": "spotify_login_failed"},
                }

        except Exception:
            log.exception("Failed to start Spotify BrowserAuth login")
            return {
                "ok": False,
                "message": "Something went wrong connecting to Spotify. Open Viola and try again.",
                "data": {"domain_id": "music", "error": "spotify_exception"},
            }

    def _connect_smart_home(self, display_name: str) -> dict[str, object]:
        """Return setup guidance for smart home (Home Assistant)."""
        registry = CapabilityRegistry.get_instance()
        if registry is not None and registry.is_domain_connected("smart_home"):
            return {
                "ok": False,
                "message": "",
                "data": {
                    "domain_id": "smart_home",
                    "service": display_name,
                    "fallback_to_llm": True,
                },
                "error": "smart_home_connected_fallthrough",
            }

        return {
            "ok": True,
            "message": (
                "To connect %s, I'll need your Home Assistant URL and a long-lived access token. "
                "You can create a token in your Home Assistant dashboard under "
                "Profile > Long-Lived Access Tokens. Once you have it, just tell me the URL "
                "and token and I'll connect." % display_name
            ),
            "data": {
                "domain_id": "smart_home",
                "service": display_name,
                "action": "setup_guidance",
            },
        }
