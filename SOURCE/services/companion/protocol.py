from __future__ import annotations

import json
import struct
import time
from dataclasses import dataclass, field
from typing import Any

_HEADER_STRUCT = struct.Struct("!I")

# F-060 (NEEDS-CLARIFY -> decided 2026-05-23):
# Claude remote-worker session resume is intentionally OUT OF SCOPE for the
# Viola companion bridge. Claude's `remote/RemoteSessionManager` resumes SDK
# sessions across reconnects with preserved environment/session IDs and a
# message-adapter replay. Viola's companion bridge is a *device* bridge --
# it relays one user's command to one of their own desktops at a time, with
# per-command request IDs and a registry that already handles retries and
# timeouts. Adding Claude-style resumable session state would duplicate the
# scope of the companion bridge and introduce a session-reconnect surface
# we don't actually need; the product divergence is intentional.
#
# We therefore:
#   * keep the `remote_worker_resume` and `session_resume` aliases pointed
#     at the normalized `remote.session_resume` type so the wire shape is
#     stable;
#   * have `is_unsupported_remote_worker_message` return True for it so
#     `CompanionBridge.send_command` rejects it with the canonical
#     `CompanionUnsupportedProtocolError` carrying
#     `REMOTE_WORKER_RESUME_UNSUPPORTED_REASON`. This is fail-closed by
#     design, not a TODO.
_COMMAND_TYPE_ALIASES: dict[str, str] = {
    "window_list": "desktop.window_list",
    "window_focus": "desktop.window_focus",
    "click": "desktop.click",
    "type_text": "desktop.type_text",
    "hotkey": "desktop.hotkey",
    "screenshot": "desktop.screenshot",
    "accessibility_tree": "desktop.accessibility_tree",
    "system_audio_start": "audio.system_audio_start",
    "system_audio_stop": "audio.system_audio_stop",
    "audio_chunk": "audio.audio_chunk",
    "file_list": "files.file_list",
    "file_read": "files.file_read",
    "file_stream": "files.file_stream",
    "directory_browse": "files.directory_browse",
    "ha_entity_list": "smart_home.ha_entity_list",
    "ha_control": "smart_home.ha_control",
    "ha_state": "smart_home.ha_state",
    "cookie_export": "browser.cookie_export",
    "page_screenshot": "browser.page_screenshot",
    "active_tab_info": "browser.active_tab_info",
    # multiroom.* -- LAN speaker control, executed by the desktop hub.
    #
    # These exist because per-room volume on a LAN speaker CANNOT be executed
    # by the cloud. The durable room list is Tier-3 desktop-install state
    # (`services/multiroom/room_registry.resolve_room_registry_owner`, which
    # documents that the module "is not reachable from backend/cloud_app.py at
    # all"), and moving a speaker's level means talking to a spoke on the
    # user's LAN, which the cloud has no route to. So the cloud relays a
    # STRUCTURED command to the one machine that can do it, and reports
    # honestly when no such machine is online -- rather than offering a direct
    # endpoint that would have to lie.
    #
    # Structured, not `agent.dispatch`: these are bounded actions with typed
    # arguments and typed results, so they belong with `smart_home.ha_control`
    # rather than with the whole-turn natural-language relay.
    "room_list": "multiroom.room_list",
    "room_set_volume": "multiroom.room_set_volume",
    "room_set_mute": "multiroom.room_set_mute",
    "room_group_list": "multiroom.room_group_list",
    "room_group_create": "multiroom.room_group_create",
    "room_group_update": "multiroom.room_group_update",
    "room_group_delete": "multiroom.room_group_delete",
    "room_group_set_master_volume": "multiroom.room_group_set_master_volume",
    "room_group_set_room_settings": "multiroom.room_group_set_room_settings",
    "capabilities_report": "system.capabilities_report",
    "health_check": "system.health_check",
    "version_info": "system.version_info",
    "remote_worker_resume": "remote.session_resume",
    "session_resume": "remote.session_resume",
    # agent.dispatch relays an ENTIRE natural-language turn to the device so
    # the device runs it through its own intent pipeline (its machine, files,
    # and LLM key). Distinct from the fine-grained capability requests above:
    # those ask the device to perform one bounded action, this hands over the
    # whole turn. Backs the cloud->desktop auto-link relay.
    "agent_dispatch": "agent.dispatch",
}

# agent.* scope: whole-turn relay. The device that advertises this scope is
# offering to run a cloud user's commands end to end on the user's behalf.
AGENT_DISPATCH_TYPE = "agent.dispatch"
# multiroom.* scope: LAN room + group control on the user's own desktop hub.
MULTIROOM_SCOPE = "multiroom"
MULTIROOM_COMMAND_TYPES = frozenset(value for value in _COMMAND_TYPE_ALIASES.values() if value.startswith("multiroom."))
REMOTE_WORKER_RESUME_TYPE = "remote.session_resume"
BROWSER_COOKIE_EXPORT_TYPE = "browser.cookie_export"
REMOTE_WORKER_RESUME_UNSUPPORTED_REASON = (
    "Claude remote-worker session resume is intentionally out of scope for the "
    "Viola companion bridge. Viola companion devices are stateless per-request "
    "relays; SDK clients should issue a fresh request rather than resume a "
    "previous session."
)
COOKIE_EXPORT_UNSUPPORTED_REASON = (
    "Browser cookie export is desktop-only Tier-3 credential material and is unavailable on the cloud companion."
)
# F-060: explicitly out-of-scope, NOT a temporary deficiency.
REMOTE_WORKER_RESUME_OUT_OF_SCOPE: bool = True
UNSUPPORTED_REMOTE_WORKER_TYPES = frozenset({REMOTE_WORKER_RESUME_TYPE})
CLOUD_UNSUPPORTED_COMPANION_TYPES = frozenset({REMOTE_WORKER_RESUME_TYPE, BROWSER_COOKIE_EXPORT_TYPE})

_RESPONSE_TYPES = frozenset(
    {
        "result",
        "error",
        "stream_start",
        "stream_chunk",
        "stream_end",
        "progress",
    }
)

# ---------------------------------------------------------------- relay budget
#
# ``agent.dispatch`` is not a bounded capability call. Every other companion
# command answers one small question ("list these files", "what windows are
# open") and is done in well under a second. A relayed WHOLE TURN runs the
# user's entire request through the desktop's own agent pipeline: LLM
# round-trips, tool searches, a real browser launch, a screenshot. It is a
# different order of magnitude and needs its own budget.
#
# Sizing it with the generic 30-second constant cancelled real turns
# mid-action. Measured on prod 2026-08-02, three for three: traces
# ``bdb756f9a5df`` (31.67 s, after launching Chrome and driving it to
# open.spotify.com) and ``623eb56cfeb1`` (28.37 s, after taking a real 1024-wide
# screenshot of the user's screen), both ending ``outcome='cancelled'`` with an
# empty final answer.
#
# The two halves hold DIFFERENT deadlines on purpose, and the ORDER is the
# load-bearing part: the cloud must stay patient strictly LONGER than the
# desktop's own budget, by enough for the desktop's reply to unwind and travel
# back over the WebSocket. When the two were equal (both 30.0, differing only by
# the bridge's 0.1 s guard margin) the cloud lost the race every single time --
# it gave up first, and then DISCARDED the desktop's answer as stale:
#
#     19:07:31 desktop_relay: ... did not yield a usable answer (status=timed_out)
#     19:07:32 bridge: Discarding stale companion result for terminal request_id
#
# So a change that raises one of these MUST preserve the ordering; the
# ``companion-relay-turn-budget`` gate fails the build otherwise.
RELAY_TURN_DESKTOP_BUDGET_SECONDS = 65.0
RELAY_TURN_CLOUD_PATIENCE_SECONDS = 75.0
# The margin the cloud keeps on top of the desktop's budget, so a desktop that
# hits its own deadline still gets its honest answer back before the cloud
# stops listening. Measured unwind + round-trip on the failing runs was ~1.4 s.
RELAY_TURN_CLOUD_MARGIN_SECONDS = RELAY_TURN_CLOUD_PATIENCE_SECONDS - RELAY_TURN_DESKTOP_BUDGET_SECONDS

COMMAND_TYPES = frozenset(_COMMAND_TYPE_ALIASES.values())
MESSAGE_TYPES = (
    COMMAND_TYPES
    | _RESPONSE_TYPES
    | {
        "system.capabilities_report",
        "system.health_check",
        "system.version_info",
    }
)
BINARY_COMPATIBLE_TYPES = frozenset(
    {
        "desktop.screenshot",
        "browser.page_screenshot",
        "audio.audio_chunk",
        "stream_chunk",
        "result",
    }
)


def normalize_message_type(message_type: str) -> str:
    value = str(message_type or "").strip()
    if not value:
        raise ValueError("message_type is required")

    lowered = value.replace(":", ".").lower()
    if lowered in _COMMAND_TYPE_ALIASES:
        return _COMMAND_TYPE_ALIASES[lowered]
    if lowered in MESSAGE_TYPES:
        return lowered

    if "." not in lowered:
        alias = _COMMAND_TYPE_ALIASES.get(lowered)
        if alias:
            return alias
    return lowered


def message_scope(message_type: str) -> str | None:
    normalized = normalize_message_type(message_type)
    if normalized in _RESPONSE_TYPES:
        return None
    if "." not in normalized:
        return None
    return normalized.split(".", 1)[0]


def message_action(message_type: str) -> str:
    normalized = normalize_message_type(message_type)
    if "." not in normalized:
        return normalized
    return normalized.split(".", 1)[1]


def is_response_type(message_type: str) -> bool:
    return normalize_message_type(message_type) in _RESPONSE_TYPES


def is_unsupported_remote_worker_message(message_type: str) -> bool:
    return normalize_message_type(message_type) in UNSUPPORTED_REMOTE_WORKER_TYPES


def unsupported_cloud_companion_message_reason(message_type: str) -> str | None:
    normalized = normalize_message_type(message_type)
    if normalized == REMOTE_WORKER_RESUME_TYPE:
        return REMOTE_WORKER_RESUME_UNSUPPORTED_REASON
    if normalized == BROWSER_COOKIE_EXPORT_TYPE:
        return COOKIE_EXPORT_UNSUPPORTED_REASON
    return None


def is_unsupported_cloud_companion_message(message_type: str) -> bool:
    return normalize_message_type(message_type) in CLOUD_UNSUPPORTED_COMPANION_TYPES


@dataclass(slots=True)
class CompanionMessage:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.type = normalize_message_type(self.type)
        if not isinstance(self.payload, dict):
            raise TypeError("payload must be a dict")
        if self.request_id is not None:
            self.request_id = str(self.request_id).strip() or None
        self.timestamp = float(self.timestamp)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "payload": self.payload,
            "request_id": self.request_id,
            "timestamp": self.timestamp,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CompanionMessage:
        return cls(
            type=str(payload.get("type", "")),
            payload=dict(payload.get("payload") or {}),
            request_id=payload.get("request_id"),
            timestamp=float(payload.get("timestamp") or time.time()),
        )

    @classmethod
    def from_json(cls, raw: str) -> CompanionMessage:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("Companion message payload must be a JSON object")
        return cls.from_dict(parsed)


@dataclass(slots=True)
class CompanionBinaryFrame:
    type: str
    payload: bytes
    request_id: str | None = None
    timestamp: float = field(default_factory=time.time)
    content_type: str = "application/octet-stream"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.type = normalize_message_type(self.type)
        if not isinstance(self.payload, (bytes, bytearray)):
            raise TypeError("payload must be bytes")
        self.payload = bytes(self.payload)
        self.timestamp = float(self.timestamp)
        if self.request_id is not None:
            self.request_id = str(self.request_id).strip() or None
        if not isinstance(self.metadata, dict):
            raise TypeError("metadata must be a dict")

    def pack(self) -> bytes:
        header = json.dumps(
            {
                "type": self.type,
                "request_id": self.request_id,
                "timestamp": self.timestamp,
                "content_type": self.content_type,
                "metadata": self.metadata,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return _HEADER_STRUCT.pack(len(header)) + header + self.payload

    @classmethod
    def unpack(cls, raw: bytes) -> CompanionBinaryFrame:
        if len(raw) < _HEADER_STRUCT.size:
            raise ValueError("Binary frame is too short")
        (header_size,) = _HEADER_STRUCT.unpack(raw[: _HEADER_STRUCT.size])
        if header_size <= 0 or len(raw) < _HEADER_STRUCT.size + header_size:
            raise ValueError("Binary frame header is invalid")
        header_bytes = raw[_HEADER_STRUCT.size : _HEADER_STRUCT.size + header_size]
        payload = raw[_HEADER_STRUCT.size + header_size :]
        parsed = json.loads(header_bytes.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("Binary frame header must be a JSON object")
        return cls(
            type=str(parsed.get("type", "")),
            payload=payload,
            request_id=parsed.get("request_id"),
            timestamp=float(parsed.get("timestamp") or time.time()),
            content_type=str(parsed.get("content_type") or "application/octet-stream"),
            metadata=dict(parsed.get("metadata") or {}),
        )
