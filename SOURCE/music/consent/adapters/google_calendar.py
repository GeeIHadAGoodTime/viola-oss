"""
Google account OAuth provider adapter (Calendar + Gmail).

This is the desktop "Connect Google" flow in Settings > Connections. It is an
installed-app OAuth flow (RFC 8252 + RFC 7636):

* The authorization request carries a PKCE S256 code challenge; the code
  verifier never leaves this process, and a code is only exchanged together
  with the verifier from its own authorization request.
* The redirect is a one-shot HTTP listener on ``127.0.0.1`` with a random
  port, started for that sign-in and stopped after it. This needs a Google
  OAuth client of type "Desktop app" (Google accepts any loopback port for
  that type). Setting ``VIOLA_GOOGLE_REDIRECT_URI`` (a dev-only override for a
  "Web application" client with registered redirect URIs) uses that fixed URI
  and the main backend's ``/v1/consent/callback`` instead.
* Tokens are stored only in the desktop's encrypted consent vault (storage
  tier 3). Nothing here talks to Viola's cloud.

The provider id stays ``google_calendar`` so existing linked tokens and
settings keep working.
"""

from __future__ import annotations

import base64
import hashlib
import html as html_module
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from config.settings import get_runtime_base_url
from core.constants import TIMEOUT_EXTENDED
from core.logging_config import get_logger
from music.consent import providers as consent_providers
from music.consent.models import ProviderCapability, ProviderMetadata, TokenBundle
from music.consent.provider_config import OAuthClientConfig, get_provider_config_loader
from services.oauth.google import (
    CALENDAR_EVENTS_SCOPE,
    CALENDAR_READONLY_SCOPE,  # noqa: F401, RUF100 - re-exported for callers and tests
    GMAIL_MAILBOX_SCOPES,
    GMAIL_MODIFY_SCOPE,
    GMAIL_SEND_SCOPE,  # noqa: F401, RUF100 - re-exported for callers and tests
    GOOGLE_CONNECTION_SCOPES,
)

logger = get_logger(__name__)

# Google OAuth 2.0 endpoints
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Scopes for the desktop Google connection come from services.oauth.google so
# there is one list. The first connect asks for Calendar (read + events) and
# Gmail send; Gmail mailbox access (read, search, organize: gmail.modify) is
# asked for the first time the user wants it (incremental consent, see
# GMAIL_MAILBOX_SCOPES). Every authorization request sets
# include_granted_scopes=true, so the new token keeps what was granted before.
CALENDAR_EVENTS_RW_SCOPE = CALENDAR_EVENTS_SCOPE
DEFAULT_SCOPES = tuple(GOOGLE_CONNECTION_SCOPES)

LOOPBACK_HOST = "127.0.0.1"
# Matches the consent session TTL: a sign-in left open longer than this has
# expired on Viola's side anyway.
LOOPBACK_LIFETIME_SECONDS = 20 * 60
NOT_CONFIGURED_MESSAGE = (
    "Google sign-in is not set up in this build of Viola. Update Viola, or contact support if this keeps happening."
)


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, S256 code_challenge) per RFC 7636."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _result_page(title: str, message: str, *, ok: bool) -> bytes:
    color = "#2e7d32" if ok else "#c62828"
    page = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>%s</title>"
        "<style>body{font-family:Arial,sans-serif;text-align:center;padding:50px}"
        "h1{color:%s}</style></head><body><h1>%s</h1><p>%s</p>"
        "<p>You can close this tab and go back to Viola.</p></body></html>"
    ) % (
        html_module.escape(title),
        color,
        html_module.escape(title),
        html_module.escape(message),
    )
    return page.encode("utf-8")


def _complete_via_consent_service(state: str, code: str | None, error: str | None) -> tuple[bool, str, str]:
    """Finish (or abandon) the consent step that owns *state*.

    Returns (ok, title, message) for the browser page. The consent session,
    not this listener, decides which user the token belongs to.
    """
    from music.consent import get_consent_service

    service = get_consent_service()
    resolved = service.resolve_callback_state(state)
    if not resolved:
        return False, "Sign-in expired", "This Google sign-in link has expired. In Viola, click Connect again."
    session_id = resolved["session_id"]
    if error or not code:
        service.cancel_session(session_id)
        if error == "access_denied":
            return False, "Google not connected", "You cancelled the Google sign-in. Nothing was connected."
        return False, "Google not connected", "Google did not finish the sign-in. Nothing was connected."
    try:
        service.complete_step_from_state(state, code=code)
    except Exception as exc:
        logger.warning("Google loopback consent completion failed: %s", type(exc).__name__)
        try:
            service.cancel_session(session_id)
        except Exception:
            logger.debug("Consent session cleanup after a failed Google sign-in failed", exc_info=True)
        return (
            False,
            "Google not connected",
            "Viola could not finish connecting Google. In Viola, click Connect again.",
        )
    return True, "Google connected", _connected_message(service, session_id)


def _connected_message(service: object, session_id: str) -> str:
    """Say what the new Google token lets Viola do (read from the stored token)."""
    granted: set[str] = set()
    try:
        session = service.get_session(session_id)
        bundle = service._vault.get_token("google_calendar", user_id=session.user_id)
        granted = set(getattr(bundle, "scopes", None) or ())
    except Exception:
        logger.debug("Could not read the new Google token's scopes", exc_info=True)
    if all(scope in granted for scope in GMAIL_MAILBOX_SCOPES):
        return "Viola can now use your Google Calendar, and read, organize and send your Gmail when you ask."
    return "Viola can now use your Google Calendar and send email you ask it to send."


CompletionCallback = Callable[[str, "str | None", "str | None"], "tuple[bool, str, str]"]


class LoopbackRedirectReceiver:
    """One-shot ``http://127.0.0.1:<random port>/`` OAuth redirect listener.

    Accepts exactly one redirect that carries the expected ``state``; every
    other request gets a 404. Stops after that redirect or after
    ``lifetime_seconds``, whichever comes first. Binds the IPv4 loopback
    address only, so nothing off this machine can reach it.
    """

    def __init__(
        self,
        state: str,
        *,
        on_redirect: CompletionCallback = _complete_via_consent_service,
        lifetime_seconds: float = LOOPBACK_LIFETIME_SECONDS,
        on_close: Callable[[str], None] | None = None,
    ) -> None:
        self._state = state
        self._on_redirect = on_redirect
        self._on_close = on_close
        self._done = threading.Event()
        self._closed = threading.Event()
        receiver = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                receiver._handle(self)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - http.server API
                # Never log request lines: they carry the authorization code.
                return

        self._server = ThreadingHTTPServer((LOOPBACK_HOST, 0), _Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self.redirect_uri = "http://%s:%d/" % (LOOPBACK_HOST, self.port)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.25},
            name="google-oauth-loopback",
            daemon=True,
        )
        self._thread.start()
        self._timer = threading.Timer(lifetime_seconds, self.close)
        self._timer.daemon = True
        self._timer.start()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def _handle(self, request: BaseHTTPRequestHandler) -> None:
        parts = urlsplit(request.path)
        query = parse_qs(parts.query)
        state = (query.get("state") or [""])[0]
        if parts.path != "/" or not state or not secrets.compare_digest(state, self._state) or self._done.is_set():
            request.send_response(404)
            request.send_header("Content-Type", "text/plain; charset=utf-8")
            request.end_headers()
            request.wfile.write(b"Not found")
            return
        self._done.set()
        code = (query.get("code") or [None])[0]
        error = (query.get("error") or [None])[0]
        try:
            ok, title, message = self._on_redirect(state, code, error)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Google loopback redirect handler failed: %s", type(exc).__name__)
            ok, title, message = False, "Google not connected", "Something went wrong. In Viola, click Connect again."
        body = _result_page(title, message, ok=ok)
        request.send_response(200 if ok else 400)
        request.send_header("Content-Type", "text/html; charset=utf-8")
        request.send_header("Cache-Control", "no-store")
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)
        threading.Thread(target=self.close, name="google-oauth-loopback-close", daemon=True).start()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._done.set()
        self._timer.cancel()
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:  # pragma: no cover - defensive
            logger.debug("Google loopback listener close failed", exc_info=True)
        if self._on_close is not None:
            try:
                self._on_close(self._state)
            except Exception:  # pragma: no cover - defensive
                logger.debug("Google loopback on_close callback failed", exc_info=True)


@dataclass
class _PendingAuthorization:
    code_verifier: str
    redirect_uri: str
    created: float = field(default_factory=time.monotonic)
    receiver: LoopbackRedirectReceiver | None = None


def _configured_redirect_override() -> str | None:
    """Dev-only fixed redirect (VIOLA_GOOGLE_REDIRECT_URI) for a Web-type client."""
    try:
        from config.settings import get_settings

        value = str(getattr(get_settings(), "google_redirect_uri", "") or "").strip()
    except Exception:
        return None
    return value or None


class GoogleCalendarOAuthAdapter:
    """Installed-app OAuth adapter for the desktop Google connection."""

    provider_id = "google_calendar"
    display_name = "Google (Calendar and Gmail)"

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        redirect_uri: str | None = None,
        config_loader=None,
        user_id: str | None = None,
    ):
        self._config_loader = config_loader or get_provider_config_loader()
        self._config_user_id = user_id
        self._pending: dict[str, _PendingAuthorization] = {}
        self._pending_lock = threading.Lock()
        self._explicit_redirect_uri = redirect_uri or _configured_redirect_override()
        oauth_config: OAuthClientConfig | None = None

        if not client_id and not client_secret:
            oauth_config = self._load_oauth_config()

        self._client_id = client_id or (oauth_config.client_id if oauth_config else None)
        self._client_secret = client_secret or (oauth_config.client_secret if oauth_config else None)

        if redirect_uri:
            self._default_redirect_uri = redirect_uri
        elif oauth_config and oauth_config.redirect_uri:
            self._default_redirect_uri = oauth_config.redirect_uri
        else:
            runtime_base_url = get_runtime_base_url()
            self._default_redirect_uri = f"{runtime_base_url}/v1/consent/callback"

        self._config_source = (
            "parameter" if (client_id or client_secret) else (oauth_config.source if oauth_config else "none")
        )

        logger.info(
            "Google OAuth config: client_id_present=%s, secret_present=%s, source=%s, loopback=%s",
            bool(self._client_id),
            bool(self._client_secret),
            self._config_source,
            self.uses_loopback_redirect(),
        )

    def _load_oauth_config(self) -> OAuthClientConfig | None:
        if self._config_user_id:
            return self._config_loader.load_oauth_config(
                "google_calendar",
                user_id=self._config_user_id,
            )
        try:
            from core.user_context import get_current_user_id

            user_id = get_current_user_id()
        except LookupError:
            return None
        return self._config_loader.load_oauth_config(
            "google_calendar",
            user_id=user_id,
        )

    def _ensure_oauth_config_loaded(self) -> None:
        if self._client_id and self._client_secret:
            return
        oauth_config = self._load_oauth_config()
        if oauth_config is None:
            return
        self._client_id = self._client_id or oauth_config.client_id
        self._client_secret = self._client_secret or oauth_config.client_secret
        if not getattr(self, "_default_redirect_uri", None):
            self._default_redirect_uri = oauth_config.redirect_uri
        self._config_source = oauth_config.source

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_id=self.provider_id,
            display_name=self.display_name,
            scopes=DEFAULT_SCOPES,
            default_redirect_uri=self._default_redirect_uri,
            capability=ProviderCapability(
                supports_offline=True,
                max_bitrate_kbps=None,
                supports_lyrics=False,
                extras={"calendar_provider": True, "gmail_send": True, "gmail_mailbox_scope": GMAIL_MODIFY_SCOPE},
            ),
            can_rotate=True,
            notes=(
                "Google Calendar events and Gmail (send, plus read and organize once the user allows it) "
                "via installed-app OAuth (PKCE, loopback)."
            ),
            is_music_provider=False,
        )

    def uses_loopback_redirect(self) -> bool:
        """True when sign-in uses a one-shot 127.0.0.1 listener (Desktop client)."""
        return not self._explicit_redirect_uri

    def _prune_pending(self) -> None:
        cutoff = time.monotonic() - LOOPBACK_LIFETIME_SECONDS
        stale: list[_PendingAuthorization] = []
        with self._pending_lock:
            for key in [k for k, v in self._pending.items() if v.created < cutoff]:
                stale.append(self._pending.pop(key))
        for pending in stale:
            if pending.receiver is not None:
                pending.receiver.close()

    def authorization_url(self, *, redirect_uri: str, state: str, scopes: Sequence[str] | None = None) -> str:
        self._ensure_oauth_config_loaded()
        if not self._client_id:
            raise RuntimeError(NOT_CONFIGURED_MESSAGE)

        self._prune_pending()
        effective_scopes = tuple(scopes or DEFAULT_SCOPES)
        scope_string = " ".join(effective_scopes)
        verifier, challenge = _pkce_pair()

        receiver: LoopbackRedirectReceiver | None = None
        if self.uses_loopback_redirect():
            receiver = LoopbackRedirectReceiver(state)
            effective_redirect = receiver.redirect_uri
        else:
            effective_redirect = self._explicit_redirect_uri or redirect_uri

        with self._pending_lock:
            previous = self._pending.pop(state, None)
            self._pending[state] = _PendingAuthorization(
                code_verifier=verifier,
                redirect_uri=effective_redirect,
                receiver=receiver,
            )
        if previous is not None and previous.receiver is not None:
            previous.receiver.close()

        params = {
            "client_id": self._client_id,
            "redirect_uri": effective_redirect,
            "response_type": "code",
            "scope": scope_string,
            "state": state,
            "access_type": "offline",
            # Incremental consent: the new token also carries every scope the
            # user granted earlier, so asking for Gmail mailbox access later
            # never drops Calendar or send.
            "include_granted_scopes": "true",
            "prompt": "consent",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }

        auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
        logger.info(
            "Generated Google authorization URL (loopback=%s, pkce=S256, scopes=%s)",
            receiver is not None,
            scope_string,
        )
        return auth_url

    def exchange_code(self, *, code: str, redirect_uri: str, state: str | None = None) -> TokenBundle:
        self._ensure_oauth_config_loaded()
        if not self._client_id or not self._client_secret:
            raise RuntimeError(NOT_CONFIGURED_MESSAGE)

        with self._pending_lock:
            pending = self._pending.pop(state, None) if state else None
        if pending is None:
            # PKCE is mandatory: a code without the verifier from its own
            # authorization request is refused rather than exchanged.
            raise RuntimeError("Google sign-in expired or was started elsewhere. Click Connect again.")
        if pending.receiver is not None:
            pending.receiver.close()

        response = httpx.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": pending.redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": pending.code_verifier,
            },
            timeout=TIMEOUT_EXTENDED,
        )
        response.raise_for_status()
        data = response.json()

        expires_in = data.get("expires_in", 3600)
        return TokenBundle(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=data.get("scope", "").split(),
        )

    def refresh_token(self, refresh_token_str: str) -> TokenBundle:
        self._ensure_oauth_config_loaded()
        if not self._client_id or not self._client_secret:
            raise RuntimeError(NOT_CONFIGURED_MESSAGE)

        response = httpx.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh_token_str,
                "grant_type": "refresh_token",
            },
            timeout=TIMEOUT_EXTENDED,
        )
        response.raise_for_status()
        data = response.json()

        expires_in = data.get("expires_in", 3600)
        return TokenBundle(
            access_token=data["access_token"],
            refresh_token=refresh_token_str,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=data.get("scope", "").split(),
        )

    def revoke(self, token: str) -> None:
        httpx.post(
            GOOGLE_REVOKE_URL,
            params={"token": token},
            timeout=TIMEOUT_EXTENDED,
        )

    def get_scopes(self) -> list[str]:
        return list(DEFAULT_SCOPES)

    def get_authorization_url(self) -> str | None:
        """Report whether a sign-in URL can be built (status probes only).

        The real flow goes through ``POST /v1/consent/session``. This probe
        never starts a loopback listener or creates a PKCE verifier.
        """
        self._ensure_oauth_config_loaded()
        if not self._client_id:
            return None
        return f"{GOOGLE_AUTH_URL}?{urlencode({'client_id': self._client_id, 'scope': ' '.join(DEFAULT_SCOPES)})}"


def _auto_register() -> None:
    """Auto-register the Google adapter on module import."""
    adapter = GoogleCalendarOAuthAdapter()
    consent_providers.register_provider(adapter)


_auto_register()
