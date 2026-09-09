"""Text-capture channel for the web chat and Telegram webhook routes.

Both routes are request/response (REST / webhook) rather than long-lived
streams, so they can't offer interactive approval or progress nudges.
This channel implements the ``MessageChannel`` protocol but captures any
outbound sends into an internal buffer instead of pushing them anywhere
— the REST handler reads the captured text from the pipeline result
object, not from ``channel.send``.

The key reason this exists at all is so the canonical IntentPipeline
(via ``CloudIntentDispatcher``) sees a real ``channel.channel_type``
string and the CHAN-R2 channel-aware prompt block picks the right
per-channel guidance. ``ask()`` returns ``None`` so approval gates can
defer confirmation to the next request rather than block the HTTP request
waiting for a user response that cannot arrive in a single round-trip.
"""

from __future__ import annotations

from collections.abc import Mapping


class WebChatChannel:
    """REST ``MessageChannel`` for the ``/chat/message`` web endpoint.

    ``channel_type`` is ``"web"`` so CHAN-R2's per-channel prompt block
    tells the model it's typing into the Viola web chat UI (markdown
    rendered, no artificial brevity). Approval gates requiring
    interactive confirmation short-circuit via ``ask() -> None`` and
    defer when the approval runtime supports structured follow-up.
    """

    active_delivery = False
    channel_type = "web"
    defer_confirmation_on_ask_none = True

    def __init__(self) -> None:
        self._outbound: list[str] = []

    async def send(self, text: str) -> None:
        self._outbound.append(text)

    async def send_image(self, path: str, caption: str = "") -> None:
        if caption:
            self._outbound.append(caption)

    async def ask(self, prompt: str, timeout: float = 60.0) -> str | None:
        return None

    async def send_typing(self) -> None:
        return None

    @property
    def captured_text(self) -> str:
        """Return any text captured via ``send`` during the request."""
        return "\n".join(self._outbound)


class TelegramWebhookChannel(WebChatChannel):
    """REST ``MessageChannel`` for the Telegram webhook path.

    Same single-turn semantics as ``WebChatChannel`` but with
    ``channel_type = "telegram"`` so the model and any downstream
    formatter tree Telegram-specific rules (Markdown → HTML, 4096-char
    chunks).
    """

    channel_type = "telegram"


class PhoneSmsChannel(WebChatChannel):
    """REST ``MessageChannel`` metadata for phone-originated command calls.

    ``send()`` still captures text for the HTTP response. Payment confirmation
    URL delivery is handled out-of-band by the payment dispatcher using the
    phone number on this object.
    """

    channel_type = "phone"

    def __init__(self, phone_number: str = "", caller_number: str = "") -> None:
        super().__init__()
        self.phone_number = (phone_number or caller_number or "").strip()
        self.caller_number = (caller_number or phone_number or "").strip()


class EmailWebhookChannel(WebChatChannel):
    """REST ``MessageChannel`` metadata for email-originated command calls."""

    channel_type = "email"

    def __init__(self, email: str = "") -> None:
        super().__init__()
        self.email = (email or "").strip()
        self.email_address = self.email


def resolve_rest_channel(raw_channel: object | None) -> object | None:
    """Build a REST ``MessageChannel`` (WebChatChannel family) from request metadata.

    Single source of truth shared by the desktop command service
    (``services/command/command_service_core.py``) and the cloud dispatch
    (``services/cloud_intent/dispatch.py``) so both REST surfaces resolve
    channels identically -- an already-built channel object passes through, a
    string identifier (``"http"``, ``"phone"``, ``"telegram"`` ...) or a
    metadata mapping (``{"origin_channel": "phone", "phone_number": ...}``)
    maps to the matching WebChatChannel subclass, and ``None``/empty/non-string
    input returns ``None`` (no channel).

    Every channel this returns has ``active_delivery = False`` (REST is
    single-turn request/response), so ``ask_user`` and approval gates defer via
    the pending-question shape instead of blocking the HTTP round-trip -- the
    reason a cloud/phone turn can offer ``ask_user`` at all without a socket to
    wait on.
    """
    if raw_channel is None:
        return None

    # Already a MessageChannel-like object -> pass through unchanged.
    if hasattr(raw_channel, "send") and hasattr(raw_channel, "channel_type"):
        return raw_channel

    channel_metadata: dict[str, object] = {}
    if isinstance(raw_channel, Mapping):
        channel_metadata = dict(raw_channel)
        raw_channel = (
            channel_metadata.get("origin_channel")
            or channel_metadata.get("channel")
            or channel_metadata.get("type")
            or channel_metadata.get("channel_type")
            or ""
        )

    if not isinstance(raw_channel, str):
        return None

    channel_key = raw_channel.strip().lower()
    if not channel_key:
        return None

    if channel_key == "telegram":
        return TelegramWebhookChannel()
    if channel_key in {"phone", "sms"}:
        phone_number = str(
            channel_metadata.get("phone_number")
            or channel_metadata.get("caller_number")
            or channel_metadata.get("from_number")
            or channel_metadata.get("to_number")
            or ""
        )
        caller_number = str(channel_metadata.get("caller_number") or channel_metadata.get("from_number") or "")
        return PhoneSmsChannel(phone_number=phone_number, caller_number=caller_number)
    if channel_key == "email":
        email = str(channel_metadata.get("email") or channel_metadata.get("email_address") or "")
        return EmailWebhookChannel(email=email)

    channel = WebChatChannel()
    if channel_key in {"http", "web"}:
        channel.channel_type = "web"
    elif channel_key in {"voice-stream", "voice"}:
        channel.channel_type = "voice"
    else:
        channel.channel_type = channel_key
    return channel
