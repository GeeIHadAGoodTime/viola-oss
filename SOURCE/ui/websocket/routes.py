from __future__ import annotations

import asyncio
import hmac
import uuid

from core.logging_config import get_logger
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from ui.api.routes.websocket_auth import (
    get_websocket_session_context,
    get_websocket_spoke_credential,
    verify_websocket_auth,
)
from ui.core.bindings import Bindings
from ui.core.player_state import to_player_state
from ui.core.security import SecurityContext, check_websocket_origin, reject_websocket
from ui.security import ResourceLimits
from ui.websocket.event_hub import EventHub

log = get_logger(__name__)
_LOOPBACK_CLIENT_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _is_loopback_client_host(client_host: str | None) -> bool:
    return bool(client_host) and client_host.lower() in _LOOPBACK_CLIENT_HOSTS


async def _reject_ws_handshake(
    ws: WebSocket,
    reason: str,
    *,
    code: int = 1008,
    user_id: str | None = None,
) -> None:
    """Log WHY a ``/ws/events`` handshake is being rejected, then close it.

    A rejected handshake used to ``close(1008)`` silently, so a desktop tab
    that connected but was refused (expired/absent WS token, non-loopback
    origin, cloud non-UUID principal) looked identical on the server to a tab
    that never connected at all. That ambiguity is exactly what forced a
    log-archaeology pass to distinguish "never connected" from
    "connected-and-rejected" for the empty live-transcript panel (call
    49c7106f). This greppable marker (sibling of PHONE_TRANSCRIPT_SOCKET_BIND)
    names the principal + reason before the close so the split is visible live.

    The close itself goes through ``reject_websocket`` (accept-then-close):
    every caller here rejects BEFORE ``hub.connect(ws, ...)`` runs its own
    ``ws.accept()``, so a bare ``ws.close()`` would collapse into a blanket
    HTTP 403 on the real uvicorn wire protocol and discard `code`/`reason`
    exactly like issue #1166 -- the client never learns WHY it was rejected,
    which is the same ambiguity this marker was originally added to kill.
    """
    client_host = ws.client.host if getattr(ws, "client", None) is not None else None
    log.warning(
        "PHONE_TRANSCRIPT_SOCKET_REJECT reason=%s code=%s client=%s user=%s",
        reason,
        code,
        client_host or "-",
        user_id or "-",
    )
    await reject_websocket(ws, code=code, reason=reason)


def _is_uuid_shaped(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def _is_cloud_surface() -> bool:
    from config.settings import settings

    return str(getattr(settings, "app_surface", "desktop")).lower() == "cloud"


def _cloud_websocket_user_id_allowed(user_id: str | None) -> bool:
    return not _is_cloud_surface() or _is_uuid_shaped(user_id)


def _resolve_paired_spoke_user_id() -> str | None:
    """Map paired spoke credentials onto the desktop device principal."""
    from config.settings import settings
    from core.user_context import get_current_or_device_user_id

    if str(getattr(settings, "app_surface", "desktop")).lower() == "cloud":
        return None
    return get_current_or_device_user_id()


def _resolve_desktop_loopback_user_id(ws: WebSocket, security_config) -> str | None:
    """Bind a scoped local principal for desktop-only loopback sockets.

    This is intentionally unavailable on cloud surfaces and non-loopback
    clients. It binds the desktop install's *active* principal — the
    logged-in GoTrue account when one exists, otherwise the bootstrap
    device identity — so a signed-in desktop's ``/ws/events`` socket
    registers under the SAME id that owner-scoped server-originated
    broadcasts target.

    Why the active principal and not the request/device principal: this
    handler runs OUTSIDE the auth middleware's request contextvar, so the
    older ``get_current_or_device_user_id()`` always fell back to the
    DEVICE id even while signed in. The phone cloud-event relay
    (``telephony/phone_cloud_event_relay.py``) owner-scopes its local
    transcript rebroadcast to ``get_desktop_active_user_id()`` — the
    ACCOUNT id while signed in — so a socket bound to the device id sat in
    an empty owner bucket and the live transcript panel never populated
    (link-4 owner-scope mismatch, live call 420d182b). Using the same
    ``get_desktop_active_user_id()`` here makes both sides agree for the
    same human in every desktop state: account when signed in, device when
    logged out. It never widens scope — it resolves exactly one principal,
    the one-user-per-install identity — and stays unavailable on cloud.
    """
    from config.settings import settings
    from core.user_context import get_desktop_active_user_id

    if str(getattr(settings, "app_surface", "desktop")).lower() == "cloud":
        return None

    client_host = ws.client.host if getattr(ws, "client", None) is not None else None
    if not _is_loopback_client_host(client_host):
        return None

    # Resolve the active principal once. On the desktop surface this never
    # raises (it falls back to the device identity); guard defensively so an
    # unexpected LookupError fails CLOSED (reject) rather than binding a wrong
    # or global scope.
    try:
        active_principal = get_desktop_active_user_id()
    except LookupError:
        return None

    if not security_config.auth_enabled:
        return active_principal

    api_key = ws.query_params.get("api_key")
    if api_key and security_config.auth_api_key and hmac.compare_digest(api_key, security_config.auth_api_key):
        return active_principal

    token = ws.query_params.get("token") or ws.headers.get("X-Auth-Token")
    if token:
        return active_principal

    return None


def register_event_socket(
    app: FastAPI,
    hub: EventHub,
    bindings: Bindings,
    security: SecurityContext,
    resource_limits: ResourceLimits,
) -> None:
    """Register the `/ws/events` endpoint used by the web UI."""

    music = bindings.music
    state = bindings.state
    auth_plugin = security.auth_plugin
    security_config = security.config

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket):
        user_id: str | None = None
        device_id: str | None = None
        # Pass client_host so a no-Origin connection from loopback (Qt webview,
        # local tools) is allowed, matching /ws/audio-stream and the documented
        # check_websocket_origin contract. Without it, client_host defaults to
        # None and every no-Origin handshake is rejected.
        if not check_websocket_origin(ws, client_host=ws.client.host if ws.client else None):
            await _reject_ws_handshake(ws, "Origin not allowed")
            return

        if not await resource_limits.check_websocket_limit(ws):
            await _reject_ws_handshake(ws, "Connection limit exceeded")
            return

        if auth_plugin.is_enabled() and security_config.websocket_auth_enabled:
            # SECURITY: Remote spoke access must use a paired spoke credential
            # or a real account-backed session. Loopback desktop clients can
            # continue using the existing local WS token/API-key transport.
            # Worker-thread hop: spoke verification reads the secret file and
            # can harden the secret dir (icacls subprocess) on first use —
            # never on the event loop (2026-07-01 starvation conviction).
            spoke_credential = await asyncio.to_thread(get_websocket_spoke_credential, ws)
            if spoke_credential is not None:
                user_id = _resolve_paired_spoke_user_id()
                device_id = spoke_credential.device_id
                if user_id is None:
                    await _reject_ws_handshake(ws, "Spoke credentials unavailable")
                    return
            else:
                session_context = await get_websocket_session_context(ws)
                if session_context is not None:
                    user_id = session_context.user_id
                    device_id = session_context.device_id
                    if not _cloud_websocket_user_id_allowed(user_id):
                        await _reject_ws_handshake(
                            ws,
                            "Cloud WebSocket authentication requires a valid user UUID",
                            user_id=user_id,
                        )
                        return
                else:
                    client_host = ws.client.host if getattr(ws, "client", None) is not None else None
                    if not _is_loopback_client_host(client_host):
                        await _reject_ws_handshake(ws, "Authentication required (non-loopback, no session)")
                        return
                    token = ws.headers.get(getattr(auth_plugin, "token_header_name", "X-Auth-Token"))
                    if not await auth_plugin.verify_websocket(ws, token):
                        await _reject_ws_handshake(ws, "Authentication required (WS token/api-key rejected)")
                        return
                    auth_result = await verify_websocket_auth(ws, required=False)
                    if auth_result is not None:
                        user, session = auth_result
                        user_id = user.id
                        device_id = getattr(session, "device_id", None)
                        if not _cloud_websocket_user_id_allowed(user_id):
                            await _reject_ws_handshake(
                                ws,
                                "Cloud WebSocket authentication requires a valid user UUID",
                                user_id=user_id,
                            )
                            return
                    if user_id is None:
                        user_id = _resolve_desktop_loopback_user_id(ws, security_config)
                    if user_id is None:
                        await _reject_ws_handshake(ws, "Authentication required (no desktop principal resolvable)")
                        return
        else:
            user_id = _resolve_desktop_loopback_user_id(ws, security_config)
            if user_id is None:
                await _reject_ws_handshake(ws, "Authentication required (auth disabled, no desktop principal)")
                return

        if not _cloud_websocket_user_id_allowed(user_id):
            await _reject_ws_handshake(
                ws,
                "Cloud WebSocket authentication requires a valid user UUID",
                user_id=user_id,
            )
            return

        # #1822: the loopback cookie branch above (auth_plugin.verify_websocket)
        # binds the ambient current_user_id ContextVar via
        # ui/security/auth.py's _attach_desktop_local_authenticated_session /
        # _attach_gotrue_authenticated_session, which captures the reset Token
        # onto ws.state.current_user_context_token. This socket has no ASGI
        # middleware teardown of its own (AuthMiddleware no-ops for non-http
        # scope), so this handler owns unwinding it in its own finally below —
        # for the life of this connection, never past it.
        user_context_token = getattr(getattr(ws, "state", None), "current_user_context_token", None)

        # Extract optional room from query params (used AFTER auth, not to bypass it)
        room_id = ws.query_params.get("room") or None
        await hub.connect(ws, room_id=room_id, user_id=user_id, device_id=device_id)
        # Greppable bind marker (sibling of PHONE_TRANSCRIPT_L1..L4): which principal
        # this socket registered under. A tab socket bound to the DEVICE principal
        # cannot receive the relay's ACCOUNT-scoped phone rebroadcasts
        # (local_clients=0) — pre-call probes gate dialing on seeing the account
        # uuid here after a post-login tab reload.
        log.info("PHONE_TRANSCRIPT_SOCKET_BIND user=%s room=%s", user_id, room_id or "-")
        registered = False

        try:
            await resource_limits.register_websocket(ws)
            registered = True

            hub_authority = getattr(app.state, "hub_state_authority", None)
            await hub.broadcast(
                "state",
                to_player_state(music, state, hub_authority=hub_authority).model_dump(),
                user_id=user_id,
            )

            while True:
                message = await ws.receive_text()
                resource_limits.validate_websocket_message_size(message)
                if message.strip():
                    # Dispatch to registered command handlers (e.g., youtube_state)
                    log.info("WS route: dispatching message (chars=%d)", len(message))
                    handled = await hub.dispatch_command(message, ws)
                    log.info("WS route: Message handled=%s", handled)
                    if not handled:
                        log.debug(
                            "WebSocket message not handled (chars=%d)",
                            len(message),
                        )

        except ValueError as exc:
            log.warning("WebSocket message size validation failed: %s", exc)
            # ws-established-close: hub.connect() above already accepted this socket.
            await ws.close(code=1009, reason="Message too large")
        except WebSocketDisconnect as exc:
            close_code = getattr(ws, "close_code", None) or getattr(exc, "code", None)
            log.debug("WebSocket client disconnected (code=%s)", close_code)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("WebSocket error: %s", exc)
            # ws-established-close: hub.connect() above already accepted this socket.
            await ws.close(code=1011, reason="Internal server error")
        finally:
            if registered:
                await resource_limits.unregister_websocket(ws)
            await hub.disconnect(ws)
            if user_context_token is not None:
                from core.user_context import reset_current_user_id

                ws.state.current_user_context_token = None
                reset_current_user_id(user_context_token)


async def _get_room_id_for_device(user_id: str, device_id: str) -> str | None:
    """Look up room_id from user_devices table.

    Uses user_devices.id as the canonical device identifier (server-assigned).

    Args:
        user_id: User ID
        device_id: Device ID (matches user_devices.id primary key)

    Returns:
        room_id if found, None otherwise
    """
    from auth.database import get_auth_db

    try:
        db = get_auth_db()
        conn = db.connection
        row = conn.execute(
            "SELECT room_id FROM user_devices WHERE id = ? AND user_id = ?",
            (device_id, user_id),
        ).fetchone()

        if row is None:
            return None
        return row["room_id"]
    except Exception as exc:
        log.warning("Failed to look up room_id for device %s: %s", device_id, exc)
        return None


__all__ = ["_get_room_id_for_device", "register_event_socket"]
