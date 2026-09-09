"""Single source of truth for the companion WebSocket auth handshake.

The desktop companion client (:mod:`services.companion_client.client`) and the
cloud server route (:mod:`services.companion.routes`) BOTH import the header
names and helpers here, so the two sides can never drift into a mismatched
handshake shape again.

Incident this closes (2026-07-06): the shipped desktop put its opaque *device
token* in the ``Authorization`` bearer, but the cloud route reads that same
header as a GoTrue *user-session JWT* first (via ``verify_websocket_auth``). A
device token is not a JWT, so the socket closed 4401 ``user_session_required``
before the device token was ever checked -- no shipped desktop could pair on
the cloud surface at all.

Contract (two-factor, fail-closed). A companion socket must present BOTH:

  * ``Authorization: Bearer <GoTrue user-session JWT>`` -- proves the LIVE user
    session. This is the SAME credential the client already uses for
    ``POST /api/v1/companion/register``. Verified by ``verify_websocket_auth``.
  * ``X-Companion-Device-Token: <opaque device token>`` -- proves THIS
    registered device. Verified by ``registry.verify_device_for_user`` against
    the session's ``user_id``, so the token is only ever accepted for a device
    the session's own user registered.

Why both, and not either one alone:

  * Session alone: any signed-in user could target any ``device_id`` in the URL.
    The device token binds the socket to a specific registered device secret.
  * Device token alone: a captured token would be replayable with no proof of a
    live user session, and the socket could not be tied to session revocation.
    Requiring the session means user B (with B's own valid session) still cannot
    attach as user A's device -- ``verify_device_for_user`` looks A's device up
    in B's scope, finds nothing, and closes 4403.

The device token is a long-lived credential, so it is only ever read from a
request HEADER -- never from the WebSocket URL query string, which lands in
access logs. ``x-companion-token`` remains an accepted legacy alias header.
"""

from __future__ import annotations

from collections.abc import Callable

# The user-session credential rides the standard Authorization bearer -- the
# same header/credential the register call uses.
WS_SESSION_AUTH_HEADER = "authorization"

# The device secret rides its OWN dedicated header so it is never confused with
# the session JWT in Authorization (the 2026-07-06 mismatch) and never lands in
# a URL. Header lookups on Starlette and ``websockets`` are case-insensitive.
WS_DEVICE_TOKEN_HEADER = "x-companion-device-token"
# Accepted legacy alias (pre-dedicated-header). Also a header, never the query.
WS_DEVICE_TOKEN_LEGACY_HEADER = "x-companion-token"

_BEARER_PREFIX = "Bearer "


def build_companion_ws_auth_headers(
    *,
    session_credential: str,
    device_token: str,
    origin: str | None = None,
) -> dict[str, str]:
    """Return the headers the desktop client sends on the ``/ws/companion`` upgrade.

    Fail-closed: both credentials are required. A blank session credential or a
    blank device token raises rather than opening a half-authenticated socket.

    ``origin`` MUST be sent for any non-loopback cloud. The cloud route runs
    ``check_websocket_origin`` before it looks at either credential, and that
    guard rejects an Origin-less upgrade from a non-localhost client outright
    (``ui/core/security.py``) -- so a desktop that omits it is closed 1008
    "Origin not allowed" on every attempt and can never hold a socket, which is
    exactly the state prod was in until 2026-08-02 (proved live: identical
    handshake minus the header is accepted, and a foreign origin is still
    rejected, so this restores reachability without widening the guard).
    """
    session = (session_credential or "").strip()
    device = (device_token or "").strip()
    if not session:
        raise ValueError("companion WS handshake requires a user-session credential")
    if not device:
        raise ValueError("companion WS handshake requires a device token")
    headers = {
        "Authorization": "%s%s" % (_BEARER_PREFIX, session),
        "X-Companion-Device-Token": device,
    }
    resolved_origin = (origin or "").strip().rstrip("/")
    if resolved_origin:
        headers["Origin"] = resolved_origin
    return headers


def companion_ws_origin(cloud_url: str) -> str:
    """Return the ``Origin`` a companion presents to ``cloud_url``.

    The desktop's own cloud base URL is the honest origin for its socket, and
    it is by construction an allowed origin for that deployment (it is the
    service the client was configured to talk to). Scheme + host[:port] only --
    never a path.
    """
    raw = (cloud_url or "").strip().rstrip("/")
    if not raw:
        return ""
    scheme, _, rest = raw.partition("://")
    if not rest:
        return ""
    host = rest.split("/", 1)[0]
    if not host:
        return ""
    return "%s://%s" % (scheme, host)


def extract_companion_ws_device_token(get_header: Callable[[str], str | None]) -> str | None:
    """Extract the device token from the dedicated header(s).

    ``get_header`` is a case-insensitive header getter (e.g.
    ``websocket.headers.get``). The device token is read ONLY from
    ``X-Companion-Device-Token`` (or the legacy ``x-companion-token`` alias) --
    never from ``Authorization`` (reserved for the session JWT) and never from
    the URL query string.
    """
    for header in (WS_DEVICE_TOKEN_HEADER, WS_DEVICE_TOKEN_LEGACY_HEADER):
        raw = get_header(header)
        if raw:
            token = raw.strip()
            if token:
                return token
    return None


__all__ = [
    "WS_DEVICE_TOKEN_HEADER",
    "WS_DEVICE_TOKEN_LEGACY_HEADER",
    "WS_SESSION_AUTH_HEADER",
    "build_companion_ws_auth_headers",
    "extract_companion_ws_device_token",
]
