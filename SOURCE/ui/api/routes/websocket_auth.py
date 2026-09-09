from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from auth.middleware import AUTH_HEADER, SESSION_COOKIE_NAME, _extract_bearer_token
from core.logging_config import get_logger
from fastapi import WebSocket
from ui.core.security import reject_websocket
from ui.security.spoke_credentials import (
    VerifiedSpokeCredential,
    get_verified_spoke_credential,
)

if TYPE_CHECKING:
    from auth.models import Session, User

logger = get_logger(__name__)
_LOOPBACK_CLIENT_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


@dataclass(frozen=True)
class WebSocketSessionContext:
    user_id: str
    device_id: str | None
    session_token: str


def get_websocket_spoke_credential(ws: WebSocket) -> VerifiedSpokeCredential | None:
    """Return the verified spoke credential presented by this WebSocket."""
    return get_verified_spoke_credential(ws)


def websocket_has_valid_spoke_token(ws: WebSocket) -> bool:
    """Check whether the client presented a valid spoke credential."""
    return get_websocket_spoke_credential(ws) is not None


def _extract_websocket_token(ws: WebSocket) -> str | None:
    bearer_token = _extract_bearer_token(ws.headers.get(AUTH_HEADER))
    if bearer_token is not None:
        return bearer_token
    return ws.cookies.get(SESSION_COOKIE_NAME)


def _extract_websocket_ws_auth_token(ws: WebSocket) -> str | None:
    query_params = getattr(ws, "query_params", {})
    getter = getattr(query_params, "get", None)
    token = getter("token") if callable(getter) else None
    return str(token) if token else None


def _websocket_token_source(ws: WebSocket) -> str:
    if _extract_bearer_token(ws.headers.get(AUTH_HEADER)) is not None:
        return "bearer"
    if ws.cookies.get(SESSION_COOKIE_NAME):
        return "cookie"
    return "none"


def _is_cloud_surface() -> bool:
    try:
        from config.settings import settings

        return str(getattr(settings, "app_surface", "desktop")).lower() == "cloud"
    except Exception:
        return False


def _is_loopback_client(ws: WebSocket) -> bool:
    client_host = ws.client.host if getattr(ws, "client", None) is not None else None
    return bool(client_host) and client_host.lower() in _LOOPBACK_CLIENT_HOSTS


def _auth_disabled_local_fallback_allowed(ws: WebSocket) -> bool:
    return not _is_cloud_surface() and _is_loopback_client(ws)


async def _close_websocket_unauthorized(ws: WebSocket) -> None:
    # reject_websocket accepts-then-closes so the client's onclose actually
    # receives code=4401/"Authentication required" instead of a blanket
    # pre-accept HTTP 403 with the code/reason discarded (issue #1166).
    await reject_websocket(ws, code=4401, reason="Authentication required")


async def _get_or_create_gotrue_app_profile_for_websocket(ws: WebSocket, user_id: str) -> dict:
    from auth.gotrue_facade import get_or_create_app_profile
    from auth.middleware import _get_gotrue_profile_engine, _set_sqlalchemy_app_user_id

    app_state = getattr(getattr(ws, "app", None), "state", None)
    connection = getattr(app_state, "gotrue_profile_connection", None) if app_state is not None else None
    if connection is not None:

        def _load_from_connection() -> dict:
            _set_sqlalchemy_app_user_id(connection, user_id)
            return get_or_create_app_profile(connection, user_id)

        return await asyncio.to_thread(_load_from_connection)

    connection_factory = (
        getattr(app_state, "gotrue_profile_connection_factory", None) if app_state is not None else None
    )
    if callable(connection_factory):
        conn_or_context = connection_factory()

        def _load_from_factory_result() -> dict:
            if hasattr(conn_or_context, "__enter__"):
                with conn_or_context as conn:
                    _set_sqlalchemy_app_user_id(conn, user_id)
                    return get_or_create_app_profile(conn, user_id)
            _set_sqlalchemy_app_user_id(conn_or_context, user_id)
            return get_or_create_app_profile(conn_or_context, user_id)

        return await asyncio.to_thread(_load_from_factory_result)

    engine = getattr(app_state, "gotrue_profile_engine", None) if app_state is not None else None
    if engine is None:
        engine = _get_gotrue_profile_engine()

    def _load_from_engine() -> dict:
        with engine.begin() as conn:
            _set_sqlalchemy_app_user_id(conn, user_id)
            return get_or_create_app_profile(conn, user_id)

    return await asyncio.to_thread(_load_from_engine)


def _gotrue_session_exists_for_websocket_sync(ws: WebSocket, user_id: str, session_id: str) -> bool:
    from auth.gotrue_facade import gotrue_session_exists
    from auth.middleware import _get_gotrue_profile_engine

    app_state = getattr(getattr(ws, "app", None), "state", None)
    connection = getattr(app_state, "gotrue_profile_connection", None) if app_state is not None else None
    if connection is not None:
        return gotrue_session_exists(connection, user_id=user_id, session_id=session_id)

    connection_factory = (
        getattr(app_state, "gotrue_profile_connection_factory", None) if app_state is not None else None
    )
    if callable(connection_factory):
        conn_or_context = connection_factory()
        if hasattr(conn_or_context, "__enter__"):
            with conn_or_context as conn:
                return gotrue_session_exists(conn, user_id=user_id, session_id=session_id)
        return gotrue_session_exists(conn_or_context, user_id=user_id, session_id=session_id)

    engine = getattr(app_state, "gotrue_profile_engine", None) if app_state is not None else None
    if engine is None:
        engine = _get_gotrue_profile_engine()

    with engine.begin() as conn:
        return gotrue_session_exists(conn, user_id=user_id, session_id=session_id)


async def _gotrue_session_is_alive_for_websocket(ws: WebSocket, *, user_id: str, session_id: str) -> bool:
    from auth import gotrue_facade
    from services.cache.redis_backend import get_redis

    app_state = getattr(getattr(ws, "app", None), "state", None)
    redis_backend = getattr(app_state, "gotrue_session_redis", None) if app_state is not None else None
    if redis_backend is None:
        redis_backend = await get_redis()

    cached = await gotrue_facade.cached_gotrue_session_is_valid(
        redis_backend,
        user_id=user_id,
        session_id=session_id,
    )
    if cached is not None:
        return cached

    alive = await asyncio.to_thread(
        _gotrue_session_exists_for_websocket_sync,
        ws,
        user_id,
        session_id,
    )
    if alive:
        await gotrue_facade.cache_gotrue_session_validity(
            redis_backend,
            user_id=user_id,
            session_id=session_id,
        )
    return alive


async def _verify_gotrue_websocket_auth(ws: WebSocket, token: str) -> tuple[User, Session] | None:
    try:
        from auth import gotrue_facade

        claims = gotrue_facade.decode_gotrue_jwt(token)
        user_id = claims["sub"]
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("GoTrue JWT subject must be a non-empty string")

        session = gotrue_facade.gotrue_claims_to_session(claims)
        if not await _gotrue_session_is_alive_for_websocket(ws, user_id=user_id, session_id=session.id):
            raise ValueError("GoTrue session has been revoked")

        app_profile_row = await _get_or_create_gotrue_app_profile_for_websocket(ws, user_id)
        user = gotrue_facade.gotrue_claims_to_user(claims, app_profile_row)
        return user, session
    except Exception as exc:
        logger.warning("GoTrue WebSocket auth rejected token: %s", exc)
        return None


async def _verify_desktop_local_websocket_auth(ws: WebSocket, token: str) -> tuple[User, Session] | None:
    try:
        from auth.desktop_session import validate_desktop_session_token

        validation = await validate_desktop_session_token(token)
        if not validation.authenticated or validation.user is None or validation.session is None:
            raise ValueError(validation.reason or "desktop local session rejected")
        _store_verified_websocket_session(ws, validation.user, validation.session)
        state = getattr(ws, "state", None)
        if state is not None:
            state.gotrue_access_token = validation.access_token
        return validation.user, validation.session
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError) as exc:
        logger.warning("Desktop local WebSocket auth rejected token: %s", exc)
        return None


def _get_stored_ws_auth_token_claims(ws: WebSocket):
    state = getattr(ws, "state", None)
    if state is None:
        return None
    return getattr(state, "ws_auth_token_claims", None)


def _store_verified_websocket_session(ws: WebSocket, user: User, session: Session) -> None:
    state = getattr(ws, "state", None)
    if state is None:
        return
    state.user = user
    state.session = session
    state.user_id = user.id
    state.auth_error = None
    from core.types import UserContext

    state.user_context = UserContext(
        user_id=user.id,
        session_id=session.id,
    )


def _load_gotrue_user_row_for_websocket_sync(ws: WebSocket, user_id: str) -> dict[str, object] | None:
    del ws

    from auth import gotrue_facade

    email = gotrue_facade.gotrue_user_email_via_auth_role(user_id)
    if not email:
        return None
    email_verified = gotrue_facade.gotrue_user_email_is_verified_via_auth_role(user_id)
    phone = gotrue_facade.gotrue_user_phone_via_auth_role(user_id)
    return {
        "id": user_id,
        "email": email,
        "email_confirmed_at": datetime.now(UTC) if email_verified else None,
        "phone": phone,
        "phone_confirmed_at": None,
    }


async def _verify_ws_auth_token_auth(ws: WebSocket) -> tuple[User, Session] | None:
    try:
        from ui.security.auth import AuthenticationPlugin
        from ui.security.config import get_security_config

        claims = _get_stored_ws_auth_token_claims(ws)
        if claims is None:
            token = _extract_websocket_ws_auth_token(ws)
            if not token:
                return None
            claims = AuthenticationPlugin(get_security_config()).consume_ws_auth_token(token)
            if claims is not None:
                # requal-M2: WS auth tokens are single-use. Cache the verified
                # claims on this socket IMMEDIATELY — even when the token is a
                # legacy ``ws:`` token with no bound identity — so any later
                # verifier in the SAME handshake (e.g. the desktop-loopback
                # ``AuthenticationPlugin.verify_websocket`` call in
                # ui/websocket/routes.py) sees the token as already verified
                # instead of re-consuming it and rejecting the connection as a
                # replay (the pre-fix 403 on a properly minted ``?token=``).
                state = getattr(ws, "state", None)
                if state is not None:
                    state.ws_auth_token_claims = claims

        user_id = str(getattr(claims, "user_id", "") or "")
        session_id = str(getattr(claims, "session_id", "") or "")
        if not user_id or not session_id:
            return None

        # Local transport tickets have no company identity. Cache their
        # verified claims above so the local verifier can use them without
        # consuming a single-use ticket twice or importing company services.
        from auth import gotrue_facade
        from auth.models import Session

        if not await _gotrue_session_is_alive_for_websocket(ws, user_id=user_id, session_id=session_id):
            raise ValueError("GoTrue session has been revoked")

        user_row = await asyncio.to_thread(_load_gotrue_user_row_for_websocket_sync, ws, user_id)
        email = str((user_row or {}).get("email") or "")
        if not email:
            raise ValueError("GoTrue user row missing for WebSocket auth token")

        app_profile_row = await _get_or_create_gotrue_app_profile_for_websocket(ws, user_id)
        now = datetime.now(UTC)
        token_claims = {
            "sub": user_id,
            "session_id": session_id,
            "email": email,
            "email_verified": bool((user_row or {}).get("email_confirmed_at")),
            "phone": ((user_row or {}).get("phone") if isinstance((user_row or {}).get("phone"), str) else None),
            "phone_verified": bool((user_row or {}).get("phone_confirmed_at")),
            "iat": int(now.timestamp()),
        }
        user = gotrue_facade.gotrue_claims_to_user(token_claims, app_profile_row)
        session = Session(
            id=session_id,
            user_id=user_id,
            expires_at=now + timedelta(seconds=30),
            created_at=now,
            last_used_at=now,
        )
        _store_verified_websocket_session(ws, user, session)
        state = getattr(ws, "state", None)
        if state is not None:
            state.ws_auth_ticket_authenticated = True
            state.ws_auth_token_claims = claims
        return user, session
    except (RuntimeError, SQLAlchemyError, TypeError, ValueError) as exc:
        logger.warning("WebSocket auth-token rejected: %s", exc)
        return None


async def websocket_session_is_current(ws: WebSocket, *, user_id: str, session_id: str) -> bool:
    """Return whether a live WebSocket's bound GoTrue session is still valid."""
    if not user_id or not session_id:
        return False
    return await _gotrue_session_is_alive_for_websocket(ws, user_id=user_id, session_id=session_id)


async def verify_websocket_auth(
    websocket: WebSocket,
    *,
    required: bool = True,
) -> tuple[User, Session] | None:
    """Verify a WebSocket bearer JWT, ``viola_session`` cookie, or WS token."""
    token = _extract_websocket_token(websocket)
    if not token:
        result = await _verify_ws_auth_token_auth(websocket)
        if result is not None:
            return result
        if required:
            await _close_websocket_unauthorized(websocket)
        return None

    if _is_cloud_surface():
        result = await _verify_gotrue_websocket_auth(websocket, token)
    else:
        result = await _verify_desktop_local_websocket_auth(websocket, token)
    if result is None and required:
        if _websocket_token_source(websocket) == "cookie":
            logger.debug("WebSocket auth rejected session cookie")
        await _close_websocket_unauthorized(websocket)
    return result


async def get_websocket_session_context(
    ws: WebSocket,
) -> WebSocketSessionContext | None:
    """Verify the session cookie and return the authenticated principal context."""
    session_token = _extract_websocket_token(ws)
    if not session_token:
        session_token = _extract_websocket_ws_auth_token(ws)
        if not session_token:
            return None

    verified = await verify_websocket_auth(ws, required=False)
    if verified is None:
        return None

    _user, session = verified
    user_id = getattr(session, "user_id", None)
    if not user_id:
        logger.warning("WebSocket session verified without user_id; rejecting")
        return None

    return WebSocketSessionContext(
        user_id=user_id,
        device_id=getattr(session, "device_id", None),
        session_token=session_token,
    )


async def websocket_has_valid_session(ws: WebSocket) -> bool:
    """Verify the session cookie using the same session service as HTTP auth."""
    return await verify_websocket_auth(ws, required=False) is not None


async def websocket_is_authorized(
    ws: WebSocket,
    *,
    allow_spoke_token: bool,
) -> bool:
    """Apply the shared WebSocket auth policy for hub and spoke endpoints."""
    from ui.security.config import get_security_config

    security_config = get_security_config()
    # Spoke-token verification hops to a worker thread: it reads the spoke
    # secret file and, on first use in a process, hardens the secret dir with
    # an icacls subprocess — that must never run on the asyncio event loop
    # (multiroom starvation conviction, _diag/2026-07-01/
    # spoke_ios_foreground_stall_and_video_sync.md).
    if not security_config.auth_enabled or not security_config.websocket_auth_enabled:
        if allow_spoke_token and await asyncio.to_thread(websocket_has_valid_spoke_token, ws):
            return True
        if _auth_disabled_local_fallback_allowed(ws):
            return True
        logger.warning("WebSocket auth-disabled fallback rejected non-local or cloud client")
        return False

    if allow_spoke_token and await asyncio.to_thread(websocket_has_valid_spoke_token, ws):
        return True

    return await verify_websocket_auth(ws, required=False) is not None


__all__ = [
    "SESSION_COOKIE_NAME",
    "WebSocketSessionContext",
    "get_websocket_session_context",
    "get_websocket_spoke_credential",
    "verify_websocket_auth",
    "websocket_has_valid_session",
    "websocket_has_valid_spoke_token",
    "websocket_is_authorized",
    "websocket_session_is_current",
]
