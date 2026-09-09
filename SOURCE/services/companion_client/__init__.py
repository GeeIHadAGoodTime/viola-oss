"""Desktop companion client.

The desktop-side counterpart of ``services/companion`` (the cloud bridge).

When the user is signed into their Viola cloud account, this package pairs
the desktop as a *companion device* of that account, keeps a live WebSocket
connection to the cloud bridge (``/ws/companion/{device_id}``), advertises
what the desktop can do, and services the capability requests defined by
``services/companion/protocol.py`` (local music library, LAN smart home,
system health, ...).

This is the working counterpart to ``services/companion/bridge.py``:

    cloud bridge  --send_command-->  /ws/companion/{device_id}  <--  CompanionClient
                  <--result/error--                            -->  capability handler

Entry points
------------
``setup_companion_client(app)``
    Wire the client into a desktop FastAPI app's startup/shutdown lifecycle.
``get_companion_client()``
    Return the process-wide client singleton (``None`` until configured).
"""

from __future__ import annotations

from .capabilities import CapabilityDispatcher, CapabilityHandler
from .client import CompanionClient, get_companion_client
from .config import CompanionClientConfig, load_companion_client_config
from .identity_store import CompanionIdentity, CompanionIdentityStore
from .startup import setup_companion_client

__all__ = [
    "CapabilityDispatcher",
    "CapabilityHandler",
    "CompanionClient",
    "CompanionClientConfig",
    "CompanionIdentity",
    "CompanionIdentityStore",
    "get_companion_client",
    "load_companion_client_config",
    "setup_companion_client",
]
