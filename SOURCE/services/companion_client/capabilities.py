"""Capability advertisement + request dispatch for the companion client.

The cloud bridge gates every command through ``services.companion.security``:
a command of type ``<scope>.<action>`` is only forwarded to a device whose
advertised capabilities allow that ``scope``/``action`` pair. So the desktop
must advertise *exactly* what it is willing to serve, and then only register
handlers for those same message types.

This module owns both halves so they cannot drift:

* :class:`CapabilityDispatcher` -- a registry of ``message_type -> handler``.
* :meth:`CapabilityDispatcher.advertised_capabilities` -- the capability map
  derived from the registered handlers, in the ``{scope: {actions: [...]}}``
  shape ``services.companion.security.normalize_capabilities`` expects.

Handlers are plain async callables ``(payload) -> dict``. They must NOT raise
for ordinary failure -- they return a dict and the client decides whether it
is a ``result`` or an ``error``. A raised exception is caught by the client
and reported as an ``error`` frame (fail safe: the cloud never hangs).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from core.logging_config import get_logger
from services.companion.protocol import message_action, message_scope, normalize_message_type

logger = get_logger(__name__)

# A handler receives the command payload and returns a JSON-serializable
# result dict. ``{"error": "..."}`` (or a raised exception) signals failure.
CapabilityHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class CapabilityDispatcher:
    """Maps companion command types to local desktop handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, CapabilityHandler] = {}

    def register(self, message_type: str, handler: CapabilityHandler) -> None:
        """Register a handler for a single (normalized) command type."""
        normalized = normalize_message_type(message_type)
        if message_scope(normalized) is None:
            raise ValueError("Cannot register handler for non-scoped type %r" % message_type)
        if normalized in self._handlers:
            raise ValueError("Duplicate companion handler for %s" % normalized)
        self._handlers[normalized] = handler
        logger.debug("Registered companion capability handler: %s", normalized)

    def handler_for(self, message_type: str) -> CapabilityHandler | None:
        """Return the handler for a command type, or ``None`` if unsupported."""
        try:
            normalized = normalize_message_type(message_type)
        except ValueError:
            return None
        return self._handlers.get(normalized)

    def supported_types(self) -> frozenset[str]:
        """Return every command type this dispatcher can service."""
        return frozenset(self._handlers)

    def advertised_capabilities(self) -> dict[str, Any]:
        """Build the capability map to send to the cloud.

        Shape: ``{scope: {"actions": ["action", ...]}}`` -- exactly what the
        cloud's :func:`normalize_capabilities` and :func:`capability_allows`
        consume. Only scopes/actions with a live handler are advertised, so
        the desktop cannot be asked to do something it has no code for.
        """
        scopes: dict[str, set[str]] = {}
        for message_type in self._handlers:
            scope = message_scope(message_type)
            if scope is None:
                continue
            scopes.setdefault(scope, set()).add(message_action(message_type))
        return {scope: {"actions": sorted(actions)} for scope, actions in scopes.items()}

    async def dispatch(self, message_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Run the handler for ``message_type``.

        Returns the handler's result dict. Raises :class:`KeyError` when no
        handler is registered -- the client maps that to an ``error`` frame.
        """
        handler = self.handler_for(message_type)
        if handler is None:
            raise KeyError(normalize_message_type(message_type))
        result = await handler(dict(payload or {}))
        if not isinstance(result, dict):
            raise TypeError("Companion handler for %s returned a non-dict result" % message_type)
        return result
