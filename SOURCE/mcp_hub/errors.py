"""Typed MCP errors.

Claude parity (S6-009): the TS hub raises a dedicated ``McpAuthError``
(``services/mcp/client.ts:152-156, 3194-3208``) when a server returns
401/Unauthorized so the agent loop and UI can offer a re-auth flow
instead of surfacing a generic exception.

The typed wrapper carries the ``server_name`` so callers can decide
which server to prompt for and update the corresponding UI state
without re-parsing the message.
"""

from __future__ import annotations

from typing import Any


class McpAuthError(Exception):
    """Raised when an MCP server returns a 401/Unauthorized response.

    Attributes:
        server_name: Name of the server that rejected the call. Used by
            the agent loop / UI to scope the re-auth prompt to a single
            connection rather than every MCP server.
    """

    def __init__(self, server_name: str, message: str | None = None) -> None:
        self.server_name = str(server_name or "")
        super().__init__(message or ('MCP server "%s" requires re-authorization' % self.server_name))


def is_mcp_unauthorized(exc: BaseException | None) -> bool:
    """Strict match for an MCP 401/Unauthorized condition.

    Claude treats auth recovery as a typed/401 condition.  Permission
    denied, session-expired, and application-level "access denied" text
    are separate failures and must not be converted into reauth prompts.

    Returns False on ``None`` so the caller can chain it safely.
    """

    if exc is None:
        return False
    if exc.__class__.__name__ == "UnauthorizedError":
        return True
    # HTTP-shaped exceptions sometimes expose .status_code / .response.
    status = _coerce_status_code(exc)
    if status == 401:
        return True
    code = getattr(exc, "code", None)
    if code == 401:
        return True
    text = str(exc).lower()
    return "401" in text


def _coerce_status_code(exc: Any) -> int | None:
    """Best-effort status code extraction from an HTTP-ish exception."""

    for attr in ("status_code", "status"):
        try:
            value = getattr(exc, attr, None)
        except Exception:
            value = None
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("status_code", "status"):
            try:
                value = getattr(response, attr, None)
            except Exception:
                value = None
            if isinstance(value, int):
                return value
    return None


__all__ = ["McpAuthError", "is_mcp_unauthorized"]
