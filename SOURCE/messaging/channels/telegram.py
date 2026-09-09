"""Telegram channel adapter.

Uses ``python-telegram-bot`` in polling mode (no webhook needed).
Bot token obtained for free from @BotFather — no business verification.

Features:
    - /start and /help command handlers
    - Owner restriction (first user auto-registers, others rejected)
    - Voice message transcription via ASR pipeline
    - Image/document handling with LLM multimodal support
    - Inline keyboard buttons (approval, playback controls)
    - Now-playing push notifications on track change
    - Rolling conversation history (last 5 exchanges per chat)
    - Friendly error handling (no tracebacks exposed)
    - HTML formatting for rich messages

Classification: GREEN
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import threading
import time
from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger
from core.platform import get_data_dir, get_logs_dir, get_temp_dir
from messaging.utils import chunk_text

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


def _build_telegram_send_errors() -> tuple[type[BaseException], ...]:
    """Concrete (non-blind) exception set for best-effort Telegram bot calls.

    Includes ``telegram.error.TelegramError`` when the library is installed so a
    transient API/network failure is swallowed (best-effort sends/typing) while
    still naming the types instead of a blind ``except Exception``.
    """
    errors: tuple[type[BaseException], ...] = (OSError, RuntimeError, ValueError)
    try:
        from telegram.error import TelegramError
    except ImportError as exc:
        logger.debug("python-telegram-bot not installed; using base send-error set: %s", exc)
        return errors
    return (TelegramError, *errors)


_TELEGRAM_SEND_ERRORS = _build_telegram_send_errors()


def _telegram_temp_dir() -> Path:
    temp_root = get_temp_dir() / "telegram"
    temp_root.mkdir(parents=True, exist_ok=True)
    return temp_root


# Minimum gap between outgoing messages (seconds) to avoid rate-limits
_RATE_LIMIT_GAP = 1.0
_CHAR_LIMIT = 4096
_MAX_HISTORY = 5  # rolling conversation history entries per chat

# SEC-061: inbound burst limit per chat. Caps how many inbound messages a single
# chat (including a group the owner is in) can drive into the pipeline within a
# rolling window, so a flood — bursty group traffic or a spammy sender — can't
# pin the agent. Applies to every inbound path (DM and group) before processing.
_INBOUND_BURST_WINDOW = 60.0  # seconds
_INBOUND_BURST_MAX = 20  # messages per chat per window
_EXTRACTABLE_EXTENSIONS = {
    ".txt",
    ".json",
    ".csv",
    ".md",
    ".py",
    ".log",
    ".xml",
    ".yaml",
    ".yml",
}

# Default log path — can be overridden in tests
_LOG_PATH = get_logs_dir() / "structured" / "viola-qt.log"

# Persistent history (JSONL)
_HISTORY_PATH = get_data_dir() / "telegram_history.jsonl"
_MAX_PERSIST_MESSAGE_LEN = 2000
_MAX_LOG_COUNT = 500


def _telegram_document_context(file_name: str, caption: str, content: str) -> str:
    """Return neutral attachment context for the agent.

    The channel supplies what arrived on Telegram. It does not invent a task
    such as "summarize"; the model decides what to do with the attachment.
    """
    caption_text = caption.strip()
    parts = [
        "Telegram text-document attachment",
        "filename: %s" % (file_name or "unknown"),
        "caption:\n%s" % caption_text if caption_text else "caption: <none>",
        "content:\n%s" % content,
    ]
    return "\n\n".join(parts)


def _telegram_image_context(caption: str) -> str:
    """Return neutral text context for a Telegram image message."""
    caption_text = caption.strip()
    if caption_text:
        return caption_text
    return "Telegram image attachment. Caption: <none>."


async def _external_channels_allowed(action: str) -> bool:
    try:
        from services.operator_controls import require_enabled_async

        decision = await require_enabled_async("external_channels", user_id=None, action=action)
    except Exception:
        logger.exception("Telegram owner safety control check failed closed")
        return False
    if decision.allowed:
        return True
    logger.warning("Telegram send blocked by owner safety control: %s", decision.reason)
    return False


def _persist_exchange(chat_id: int, user_text: str, response_text: str) -> None:
    """Append one exchange to the persistent JSONL history file."""
    try:
        _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        truncated = response_text[:_MAX_PERSIST_MESSAGE_LEN]
        if len(response_text) > _MAX_PERSIST_MESSAGE_LEN:
            truncated += "..."
        record = {
            "chat_id": chat_id,
            "user": user_text,
            "assistant": truncated,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(_HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
    except Exception:
        logger.warning("Failed to persist Telegram exchange to %s", _HISTORY_PATH)


def load_persistent_history(limit: int = 20, max_limit: int = 200) -> list[dict[str, str]]:
    """Load exchanges from the persistent JSONL history file.

    Args:
        limit: Number of most-recent exchanges to return.
        max_limit: Hard ceiling on limit to prevent abuse.

    Returns:
        List of ``{"user": ..., "assistant": ..., "ts": ..., "chat_id": ...}`` dicts,
        most-recent last.
    """
    limit = min(max(1, limit), max_limit)
    if not _HISTORY_PATH.exists():
        return []
    exchanges: list[dict[str, str]] = []
    try:
        with open(_HISTORY_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    exchanges.append(record)
                except (json.JSONDecodeError, ValueError):
                    continue  # skip corrupted lines
    except Exception:
        logger.warning("Failed to read persistent history from %s", _HISTORY_PATH)
        return []
    # Return the last N
    return exchanges[-limit:]


def _parse_history_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(value, fmt).replace(tzinfo=UTC)
            return parsed
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _rewrite_persistent_history(records: list[dict[str, Any]]) -> None:
    _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    _HISTORY_PATH.write_text(payload, encoding="utf-8")


def cleanup_persistent_history(days: int = 90) -> int:
    """Delete persisted Telegram exchanges older than the retention window."""
    if not _HISTORY_PATH.exists():
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=days)
    kept: list[dict[str, Any]] = []
    deleted = 0
    try:
        for line in _HISTORY_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                kept.append({"corrupt": True, "raw": line})
                continue
            ts = _parse_history_timestamp(record.get("ts"))
            if ts is not None and ts < cutoff:
                deleted += 1
            else:
                kept.append(record)
        if deleted:
            _rewrite_persistent_history(kept)
            logger.info("Cleaned up %d Telegram history records older than %d days", deleted, days)
    except OSError:
        logger.exception("Failed to clean up Telegram persistent history at %s", _HISTORY_PATH)
        return 0
    return deleted


def purge_persistent_history_for_chats(chat_ids: Iterable[object]) -> int:
    """Delete persisted Telegram JSONL exchanges for the given chat IDs."""
    if not _HISTORY_PATH.exists():
        return 0
    targets = {str(chat_id) for chat_id in chat_ids}
    if not targets:
        return 0

    kept: list[dict[str, Any]] = []
    deleted = 0
    try:
        for line in _HISTORY_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                kept.append({"corrupt": True, "raw": line})
                continue
            if str(record.get("chat_id")) in targets:
                deleted += 1
            else:
                kept.append(record)
        if deleted:
            _rewrite_persistent_history(kept)
            logger.info("Purged %d Telegram history records for GDPR deletion", deleted)
    except OSError:
        logger.exception("Failed to purge Telegram persistent history at %s", _HISTORY_PATH)
        return 0
    return deleted


def format_history_exchanges(
    history: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Convert raw history entries into paired exchange dicts.

    Returns a list of ``{"user": ..., "assistant": ..., "ts": ...}`` dicts.
    """
    exchanges: list[dict[str, str]] = []
    i = 0
    while i < len(history):
        entry = history[i]
        if entry["role"] == "user":
            user_text = entry["content"][:100]
            ts = entry.get("ts", "")

            bot_text = ""
            if i + 1 < len(history) and history[i + 1]["role"] == "assistant":
                bot_text = history[i + 1]["content"][:100]
                i += 1

            exchanges.append({"user": user_text, "assistant": bot_text, "ts": ts})
        else:
            bot_text = entry["content"][:100]
            exchanges.append({"user": "", "assistant": bot_text, "ts": entry.get("ts", "")})
        i += 1
    return exchanges


def get_telegram_log_lines(count: int = 10) -> list[str]:
    """Return the last *count* Telegram-related log lines, sanitized.

    File paths and bot tokens are stripped to prevent leaking sensitive data.
    Count is capped at ``_MAX_LOG_COUNT`` (500).
    """
    count = min(max(1, count), _MAX_LOG_COUNT)
    if not _LOG_PATH.exists():
        return []

    raw = _LOG_PATH.read_text(encoding="utf-8", errors="replace")
    all_lines = raw.splitlines()

    tg_lines: list[str] = []
    for line in reversed(all_lines):
        if "telegram" in line.lower():
            tg_lines.append(line)
            if len(tg_lines) >= count:
                break
    tg_lines.reverse()

    sanitized: list[str] = []
    for line in tg_lines:
        line = re.sub(r'[A-Za-z]:\\[^\s"\']+', "<path>", line)
        line = re.sub(r'/(?:home|usr|tmp|var|opt)[^\s"\']*', "<path>", line)
        line = re.sub(r"\b[0-9]{7,}:AA[A-Za-z0-9_-]{30,}\b", "<token>", line)
        sanitized.append(line)
    return sanitized


class TelegramChannel:
    """MessageChannel backed by a Telegram bot conversation.

    Each instance corresponds to a single ``chat_id``.  The adapter
    is created by the TelegramListener on incoming messages and handed
    to the pipeline.
    """

    channel_type = "telegram"

    def __init__(self, bot: Any, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._pending_reply: asyncio.Future[str] | None = None
        self._last_send_time: float = 0.0

    async def _rate_limit(self) -> None:
        """Ensure minimum gap between sends."""
        import time

        now = time.monotonic()
        elapsed = now - self._last_send_time
        if elapsed < _RATE_LIMIT_GAP:
            await asyncio.sleep(_RATE_LIMIT_GAP - elapsed)
        self._last_send_time = time.monotonic()

    async def send(self, text: str) -> None:
        """Send a text message to the user with Telegram HTML formatting."""
        if not await _external_channels_allowed("telegram_channel_send"):
            return
        from messaging.formatters import get_formatter

        formatter = get_formatter("telegram")
        html = formatter.format(text)

        # Try HTML first, fall back to plain text
        chunks = chunk_text(html, _CHAR_LIMIT)
        for part in chunks:
            await self._rate_limit()
            try:
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=part,
                    parse_mode="HTML",
                )
                logger.info("Telegram message sent to chat %s (%d chars)", self._chat_id, len(part))
            except Exception:
                # Fallback to plain text if HTML parsing fails
                await self._send_raw(part)

    async def send_html(self, html: str) -> None:
        """Send a message with HTML formatting (pre-formatted)."""
        if not await _external_channels_allowed("telegram_channel_send_html"):
            return
        chunks = chunk_text(html, _CHAR_LIMIT)
        for part in chunks:
            await self._rate_limit()
            try:
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=part,
                    parse_mode="HTML",
                )
            except Exception:
                # Fallback to plain text if HTML parsing fails
                await self._send_raw(part)

    async def send_gate_card(self, card: dict[str, Any]) -> None:
        """Send a gate-review card with Telegram HTML and inline buttons."""
        if not await _external_channels_allowed("telegram_channel_send_gate_card"):
            return
        from messaging.formatters import (
            format_card_as_text,
            format_gate_review_as_telegram_html,
            gate_button_specs,
        )

        html_body = format_gate_review_as_telegram_html(card)
        fallback_text = format_card_as_text(card, channel_type="telegram")
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        except ImportError:
            await self.send(fallback_text)
            return

        row = []
        for spec in gate_button_specs(card):
            label = spec.get("label", "Action")
            if spec.get("action") == "open_url":
                row.append(InlineKeyboardButton(label, url=spec.get("url", "")))
            elif spec.get("action") == "send_chat":
                row.append(InlineKeyboardButton(label, callback_data=spec.get("callback_data", "")))
        reply_markup = InlineKeyboardMarkup([row]) if row else None

        chunks = chunk_text(html_body, _CHAR_LIMIT)
        for index, part in enumerate(chunks):
            await self._rate_limit()
            try:
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=part,
                    parse_mode="HTML",
                    reply_markup=reply_markup if index == 0 else None,
                )
            except Exception:
                await self.send(fallback_text)
                return

    async def _send_raw(self, text: str) -> None:
        """Send a single text chunk to the user."""
        if not await _external_channels_allowed("telegram_channel_send_raw"):
            return
        try:
            await self._bot.send_message(chat_id=self._chat_id, text=text)
        except Exception as exc:
            logger.warning("Telegram send failed (chat=%s): %s", self._chat_id, exc)

    async def send_image(self, path: str, caption: str = "") -> None:
        """Send an image via Telegram's photo API."""
        if not await _external_channels_allowed("telegram_channel_send_image"):
            return
        await self._rate_limit()
        try:
            with open(path, "rb") as fp:
                await self._bot.send_photo(chat_id=self._chat_id, photo=fp, caption=caption)
        except Exception as exc:
            logger.warning("Telegram send_image failed (chat=%s): %s", self._chat_id, exc)

    async def ask(self, prompt: str, timeout: float = 60.0) -> str | None:
        """Send *prompt* and wait for the next message in this chat.

        The TelegramListener must call ``_deliver_reply(text)`` when the
        user's next message arrives.
        """
        await self.send(prompt)
        loop = asyncio.get_running_loop()
        self._pending_reply = loop.create_future()
        try:
            return await asyncio.wait_for(self._pending_reply, timeout=timeout)
        except (TimeoutError, asyncio.CancelledError):
            return None
        finally:
            self._pending_reply = None

    async def send_typing(self) -> None:
        """Send Telegram "typing" chat action."""
        try:
            await self._bot.send_chat_action(chat_id=self._chat_id, action="typing")
        except _TELEGRAM_SEND_ERRORS as exc:
            logger.debug("Telegram send_typing failed (chat=%s): %s", self._chat_id, exc)

    # -- Button extension (mirrors Discord pattern) ------------------------------

    @property
    def supports_buttons(self) -> bool:
        """Telegram supports inline keyboard buttons."""
        try:
            from telegram import InlineKeyboardButton

            return InlineKeyboardButton is not None
        except ImportError:
            return False

    async def ask_with_buttons(
        self,
        prompt: str,
        buttons: list[str],
        timeout: float = 60.0,
    ) -> str | None:
        """Send *prompt* with inline keyboard buttons and wait for a click.

        Falls back to text-based :meth:`ask` if Telegram library issues arise.
        """
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        except ImportError:
            return await self.ask(prompt, timeout=timeout)

        keyboard = [
            [InlineKeyboardButton(label, callback_data=f"btn_{i}_{label[:20]}")] for i, label in enumerate(buttons)
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await self._rate_limit()
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=prompt,
                reply_markup=reply_markup,
            )
        except Exception as exc:
            logger.warning("Telegram button send failed, falling back to text: %s", exc)
            return await self.ask(prompt, timeout=timeout)

        # Wait for the button click to be delivered via _deliver_reply
        loop = asyncio.get_running_loop()
        self._pending_reply = loop.create_future()
        try:
            return await asyncio.wait_for(self._pending_reply, timeout=timeout)
        except (TimeoutError, asyncio.CancelledError):
            return None
        finally:
            self._pending_reply = None

    # -- internal callback -------------------------------------------------------

    def _deliver_reply(self, text: str) -> bool:
        """Called by TelegramListener when a user reply arrives.

        Returns True if the reply was consumed by a pending ``ask()``.
        """
        if self._pending_reply is not None and not self._pending_reply.done():
            self._pending_reply.set_result(text)
            return True
        return False


# ============================================================================
# Helpers
# ============================================================================


# SEC-062: serialize the auto-register read-modify-write so two concurrent first
# messages from different chats can't each observe "no owner yet" and both
# register. The Telegram polling app dispatches handlers on one event loop, but
# the loop interleaves across the SettingsManager get/set await points; a single
# threading lock held across the whole check-then-act closes the TOCTOU window
# regardless of how the scheduler interleaves.
_OWNER_REGISTER_LOCK = threading.Lock()


def _get_owner_chat_id() -> str:
    """Read owner chat ID from SettingsManager."""
    try:
        from ui.settings_manager import get_settings_manager

        sm = get_settings_manager()
        return str(sm.get("telegram_owner_chat_id", "") or "")
    except Exception:
        return ""


def _set_owner_chat_id(chat_id: int) -> None:
    """Persist owner chat ID to SettingsManager."""
    try:
        from ui.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("telegram_owner_chat_id", str(chat_id))
    except Exception as exc:
        logger.warning("Failed to persist telegram_owner_chat_id: %s", exc)


def _register_and_check_owner(chat_id: int) -> bool:
    """Atomically register the first chat as owner and report ownership.

    Returns ``True`` iff *chat_id* is (or just became) the registered owner.
    The whole check-then-act runs under ``_OWNER_REGISTER_LOCK`` so a race
    between two first-time chats cannot register both (SEC-062).
    """
    with _OWNER_REGISTER_LOCK:
        owner = _get_owner_chat_id()
        if not owner:
            _set_owner_chat_id(chat_id)
            cid = str(chat_id)
            masked = cid[:3] + "***" + cid[-2:] if len(cid) > 5 else "***"
            logger.info("Auto-registered Telegram owner: %s", masked)
            return True
        return str(chat_id) == owner


def _is_owner(chat_id: int) -> bool:
    """Check if chat_id matches the registered owner."""
    owner = _get_owner_chat_id()
    if not owner:
        return False  # no owner — deny (auto-registration should happen before this)
    return str(chat_id) == owner


async def _transcribe_voice(file_path: Path) -> str | None:
    """Transcribe an audio file using the ASR pipeline."""
    try:
        from voice.transcription.factory import (
            create_default_transcriber,
            get_transcriber,
        )

        transcriber = get_transcriber()
        if transcriber is None:
            transcriber = create_default_transcriber()
        return await transcriber.transcribe(file_path)
    except Exception as exc:
        logger.warning("Voice transcription failed: %s", exc)
        return None


def _format_now_playing(title: str, artist: str = "", album: str = "") -> str:
    """Format a now-playing message with HTML."""
    parts = [f"<b>{_html_escape(title)}</b>"]
    if artist:
        parts.append(f"<i>{_html_escape(artist)}</i>")
    if album:
        parts.append(_html_escape(album))
    return " — ".join(parts)


def _html_escape(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ============================================================================
# Listener
# ============================================================================


class TelegramListener:
    """Runs a Telegram bot in polling mode and routes messages to the pipeline.

    Features:
        - /start, /help commands
        - Owner authentication (first user auto-registers)
        - Voice message transcription
        - Image/document handling
        - Inline keyboard callbacks
        - Now-playing push notifications
        - Rolling conversation history

    Usage::

        listener = TelegramListener(bot_token, pipeline)
        await listener.start()   # runs until cancelled
    """

    channel_type = "telegram"

    def __init__(self, bot_token: str, pipeline: Any) -> None:
        self._token = bot_token
        self._pipeline = pipeline
        self._channels: dict[int, TelegramChannel] = {}
        self._app: Any = None
        self._bot_username: str = ""
        # Rolling conversation history per chat_id
        self._history: dict[int, deque[dict[str, str]]] = {}
        # SEC-061: inbound message timestamps per chat for burst limiting
        self._inbound_bursts: dict[int, deque[float]] = {}
        # Track last announced track to avoid duplicate pushes
        self._last_announced_track: str = ""
        self._now_playing_task: asyncio.Task[None] | None = None
        self._connected_at: float = 0.0
        self._connection_state: str = "disconnected"

    # -- MessageChannel interface (routes to owner chat) --------------------

    async def send(self, text: str) -> None:
        """Send a message to the owner via their TelegramChannel."""
        ch = self._get_owner_channel()
        if ch is None:
            logger.warning("Cannot send: no owner channel available")
            return
        await ch.send(text)

    async def send_image(self, path: str, caption: str = "") -> None:
        """Send an image to the owner."""
        ch = self._get_owner_channel()
        if ch is None:
            return
        await ch.send_image(path, caption)

    async def send_typing(self) -> None:
        """Send typing indicator to the owner."""
        ch = self._get_owner_channel()
        if ch is None:
            return
        await ch.send_typing()

    async def ask(self, prompt: str, timeout: float = 60.0) -> str | None:
        """Ask the owner a question and wait for reply."""
        ch = self._get_owner_channel()
        if ch is None:
            return None
        return await ch.ask(prompt, timeout)

    def _get_owner_channel(self) -> Any:
        """Return the TelegramChannel for the registered owner, or None."""
        owner_id_str = _get_owner_chat_id()
        if not owner_id_str:
            return None
        try:
            owner_chat_id = int(owner_id_str)
        except ValueError:
            return None
        ch = self._channels.get(owner_chat_id)
        if ch is not None:
            return ch
        # Owner hasn't messaged since boot — create a channel if bot is ready
        if self._app is not None:
            ch = TelegramChannel(self._app.bot, owner_chat_id)
            self._channels[owner_chat_id] = ch
            return ch
        return None

    def get_health(self) -> dict[str, Any]:
        """Return connection health for this adapter."""
        now = time.monotonic()
        uptime = (now - self._connected_at) if self._connected_at else 0.0
        return {
            "state": self._connection_state,
            "uptime_seconds": round(uptime, 1) if self._connection_state == "connected" else 0.0,
            "active_channels": len(self._channels),
        }

    def _get_history(self, chat_id: int) -> list[dict[str, object]]:
        """Get conversation history for a chat as a list."""
        hist = self._history.get(chat_id)
        if hist is None:
            return []
        return list(hist)

    def _record_history(self, chat_id: int, user_text: str, response_text: str) -> None:
        """Record a user/response exchange in rolling history and persist to disk."""
        if chat_id not in self._history:
            self._history[chat_id] = deque(maxlen=_MAX_HISTORY)
        ts = time.strftime("%H:%M:%S")
        self._history[chat_id].append(
            {
                "role": "user",
                "content": user_text,
                "ts": ts,
            }
        )
        self._history[chat_id].append(
            {
                "role": "assistant",
                "content": response_text,
                "ts": ts,
            }
        )
        # Persist to JSONL file (survives restarts)
        _persist_exchange(chat_id, user_text, response_text)

    def _inbound_allowed(self, chat_id: int) -> bool:
        """SEC-061: return False when *chat_id* has exceeded its inbound burst.

        Tracks inbound message timestamps in a rolling window per chat. Applies
        to every inbound path (DM and group) so a flood can't pin the pipeline.
        """
        now = time.monotonic()
        bucket = self._inbound_bursts.get(chat_id)
        if bucket is None:
            bucket = deque()
            self._inbound_bursts[chat_id] = bucket
        cutoff = now - _INBOUND_BURST_WINDOW
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= _INBOUND_BURST_MAX:
            logger.warning(
                "Telegram inbound burst limit hit for chat %s: %d msgs in %.0fs window",
                chat_id,
                len(bucket),
                _INBOUND_BURST_WINDOW,
            )
            return False
        bucket.append(now)
        return True

    def _owner_user_key(self) -> str:
        """Stable per-install owner identity for pipeline data isolation (SEC-062).

        Telegram is owner-only (one-user-per-install): every accepted message
        belongs to the registered owner. The isolation key must therefore be the
        stable owner identity — the owner chat id — not the per-message sender's
        ``from_user.id``, which differs from the owner in group chats and would
        fragment/mis-scope the owner's history and state across senders.
        """
        owner = _get_owner_chat_id()
        return owner or "owner"

    def _get_or_create_channel(self, bot: Any, chat_id: int) -> TelegramChannel:
        """Get existing channel or create a new one."""
        channel = self._channels.get(chat_id)
        if channel is None:
            channel = TelegramChannel(bot, chat_id)
            self._channels[chat_id] = channel
        return channel

    async def _process_and_respond(
        self,
        channel: TelegramChannel,
        text: str,
        chat_id: int,
        prefix: str = "",
        user_key: str = "",
    ) -> None:
        """Run text through the pipeline and send the response.

        Args:
            channel: The Telegram channel to respond on.
            text: User input text.
            chat_id: Chat identifier for history tracking.
            prefix: Optional prefix for the response (e.g. "[Voice] ...").
            user_key: User identifier for per-user history isolation.
        """
        await channel.send_typing()

        try:
            history = self._get_history(chat_id)
            result = await self._pipeline.process(text, history=history, channel=channel, user_key=user_key)

            from messaging.formatters import (
                extract_card,
                extract_message_with_card,
                format_card_as_text,
                is_gate_review_card,
            )

            card = extract_card(result)
            if is_gate_review_card(card):
                if prefix:
                    await channel.send(prefix.rstrip())
                if not card.get("confirmation_link_dispatched"):
                    await channel.send_gate_card(card)
                self._record_history(chat_id, text, format_card_as_text(card, channel_type="telegram"))
                return

            message = extract_message_with_card(result, channel_type="telegram")

            if isinstance(message, str) and message:
                response = f"{prefix}{message}" if prefix else message
                await channel.send(response)
                self._record_history(chat_id, text, message)
            elif prefix:
                # Still show the prefix even if pipeline returned nothing
                await channel.send(f"{prefix}(no response)")
            else:
                await channel.send("Done.")
                self._record_history(chat_id, text, "Done.")
        except TimeoutError:
            try:
                await channel.send("Processing timed out. Please try again or simplify your request.")
            except Exception:
                logger.warning("Failed to send timeout message to chat %s", chat_id)
        except Exception:
            logger.exception("Telegram pipeline error (chat=%s)", chat_id)
            try:
                await channel.send("An error occurred while processing your message. Please try again.")
            except Exception:
                logger.warning("Failed to send error message to chat %s", chat_id)

    async def start(self) -> None:
        """Start polling for Telegram updates."""
        try:
            from telegram import Update
            from telegram.ext import (
                ApplicationBuilder,
                CallbackQueryHandler,
                CommandHandler,
                MessageHandler,
                filters,
            )
        except ImportError:
            logger.error("python-telegram-bot not installed. Run: pip install python-telegram-bot")
            return

        self._app = ApplicationBuilder().token(self._token).build()

        # Cache bot username for group mention filtering
        try:
            bot_info = await self._app.bot.get_me()
            self._bot_username = bot_info.username or ""
            logger.info("Telegram bot username: @%s", self._bot_username)
        except Exception as exc:
            logger.warning("Failed to get Telegram bot username: %s", exc)

        # ----------------------------------------------------------------
        # /start command
        # ----------------------------------------------------------------
        async def _on_start(update: Update, context: Any) -> None:
            if update.message is None:
                return
            chat_id = update.message.chat_id

            if not _register_and_check_owner(chat_id):
                await update.message.reply_text("This is a private assistant.")
                return

            await update.message.reply_text(
                "Hi! I'm Viola, your AI music assistant.\n\n"
                "You can tell me things like:\n"
                '  - "play some jazz"\n'
                '  - "what\'s playing?"\n'
                '  - "skip" / "pause" / "volume up"\n'
                '  - "set a timer for 5 minutes"\n'
                '  - "what\'s the weather?"\n\n'
                "Send /help for more examples.\n"
                "You can also send voice messages and I'll transcribe them!"
            )

        # ----------------------------------------------------------------
        # /help command
        # ----------------------------------------------------------------
        async def _on_help(update: Update, context: Any) -> None:
            if update.message is None:
                return
            chat_id = update.message.chat_id

            if not _register_and_check_owner(chat_id):
                await update.message.reply_text("This is a private assistant.")
                return

            await update.message.reply_text(
                "Here's what I can do:\n\n"
                "Music:\n"
                '  "play [song/artist/genre]"\n'
                '  "pause" / "resume" / "stop"\n'
                '  "skip" / "previous"\n'
                '  "volume up/down" / "set volume to 50"\n'
                '  "what\'s playing?" / "queue"\n'
                '  "shuffle" / "repeat"\n\n'
                "Other:\n"
                '  "what\'s the weather in London?"\n'
                '  "set a timer for 10 minutes"\n'
                '  "remind me to call Mom at 3pm"\n\n'
                "Tips:\n"
                "  - Send a voice message and I'll transcribe + process it\n"
                "  - Send an image with a caption to ask about it\n"
                "  - I remember our last few messages for context"
            )

        # ----------------------------------------------------------------
        # /debug — dump conversation history (owner-only)
        # ----------------------------------------------------------------
        async def _on_debug(update: Update, context: Any) -> None:
            if update.message is None:
                return
            chat_id = update.message.chat_id
            if not _is_owner(chat_id):
                return  # silent for non-owners

            hist = self._history.get(chat_id)
            if not hist:
                await update.message.reply_text("No conversation history yet.")
                return

            exchanges = format_history_exchanges(list(hist))
            lines: list[str] = []
            for idx, ex in enumerate(exchanges, 1):
                ts_label = f" [{ex['ts']}]" if ex["ts"] else ""
                if ex["user"]:
                    lines.append(f"{idx}.{ts_label} You: {ex['user']}")
                if ex["assistant"]:
                    lines.append(f"   Viola: {ex['assistant']}")

            header = f"Conversation history ({len(exchanges)} exchanges, max {_MAX_HISTORY}):\n\n"
            await update.message.reply_text(header + "\n".join(lines))

        # ----------------------------------------------------------------
        # /log — last 10 Telegram-related log lines (owner-only)
        # ----------------------------------------------------------------
        async def _on_log(update: Update, context: Any) -> None:
            if update.message is None:
                return
            chat_id = update.message.chat_id
            if not _is_owner(chat_id):
                return  # silent for non-owners

            try:
                sanitized = get_telegram_log_lines(10)
                if not sanitized:
                    await update.message.reply_text("No Telegram log entries found.")
                    return
                header = f"Last {len(sanitized)} Telegram log entries:\n\n"
                await update.message.reply_text(header + "\n".join(sanitized))
            except Exception as exc:
                logger.warning("Failed to read logs for /log command: %s", exc)
                await update.message.reply_text("Could not read log file.")

        # ----------------------------------------------------------------
        # Text messages
        # ----------------------------------------------------------------
        async def _on_message(update: Update, context: Any) -> None:
            if update.message is None or update.message.text is None:
                return
            chat_id = update.message.chat_id

            if not _register_and_check_owner(chat_id):
                await update.message.reply_text("This is a private assistant.")
                return

            # -- SEC-061: inbound burst limit (DM and group) --
            if not self._inbound_allowed(chat_id):
                return

            # -- G3: Deduplication check --
            try:
                from messaging.dedup import get_dedup_cache

                msg_id = str(update.message.message_id) if update.message.message_id else ""
                if msg_id and get_dedup_cache().is_duplicate(
                    channel_type="telegram",
                    chat_id=str(chat_id),
                    message_id=msg_id,
                ):
                    logger.debug("Telegram dedup: skipping duplicate message %s", msg_id)
                    return
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                logger.debug("Telegram dedup check failed (non-fatal): %s", exc)

            text = update.message.text

            # Group mention filter: in groups, only respond when @mentioned
            chat_type = update.message.chat.type  # "private", "group", "supergroup", "channel"
            if chat_type != "private" and self._bot_username:
                mention = "@%s" % self._bot_username
                if mention.lower() not in text.lower():
                    return  # Not mentioned in group — skip
                text = text.replace(mention, "").replace(mention.lower(), "").strip()
                if not text:
                    return

            # SEC-062: scope to the stable install-owner identity, not the
            # per-message sender id (which diverges from the owner in groups).
            user_key = self._owner_user_key()

            channel = self._get_or_create_channel(context.bot, chat_id)

            # Check if a pending ask() should consume this reply
            if channel._deliver_reply(text):
                return

            await self._process_and_respond(channel, text, chat_id, user_key=user_key)

        # ----------------------------------------------------------------
        # Voice messages
        # ----------------------------------------------------------------
        async def _on_voice(update: Update, context: Any) -> None:
            if update.message is None:
                return
            chat_id = update.message.chat_id

            if not _register_and_check_owner(chat_id):
                await update.message.reply_text("This is a private assistant.")
                return

            voice = update.message.voice or update.message.audio
            if voice is None:
                return

            channel = self._get_or_create_channel(context.bot, chat_id)
            await channel.send_typing()

            # Download voice file to temp directory
            try:
                tg_file = await context.bot.get_file(voice.file_id)
                with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False, dir=str(_telegram_temp_dir())) as tmp:
                    tmp_path = Path(tmp.name)
                await tg_file.download_to_drive(str(tmp_path))
            except Exception:
                logger.exception("Failed to download voice message (chat=%s)", chat_id)
                await channel.send("I couldn't download your voice message. Please try again.")
                return

            # Transcribe
            try:
                transcribed = await _transcribe_voice(tmp_path)
            finally:
                # Clean up temp file
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.debug("Telegram temp file cleanup failed: %s", exc)

            if not transcribed or not transcribed.strip():
                await channel.send("I couldn't understand the audio. Could you try again?")
                return

            # SEC-062: scope to the stable install-owner identity (see _owner_user_key).
            user_key = self._owner_user_key()
            prefix = f'[Voice] "{transcribed}"\n\n'
            await self._process_and_respond(channel, transcribed, chat_id, prefix=prefix, user_key=user_key)

        # ----------------------------------------------------------------
        # Images, stickers, documents
        # ----------------------------------------------------------------
        async def _on_media(update: Update, context: Any) -> None:
            if update.message is None:
                return
            chat_id = update.message.chat_id

            if not _register_and_check_owner(chat_id):
                await update.message.reply_text("This is a private assistant.")
                return

            channel = self._get_or_create_channel(context.bot, chat_id)
            await channel.send_typing()

            caption = update.message.caption or ""

            # --- Photo / Sticker ---
            photo = None
            if update.message.photo:
                photo = update.message.photo[-1]  # highest resolution
            elif update.message.sticker and update.message.sticker.file_id:
                photo = update.message.sticker

            if photo is not None:
                # Download and try multimodal LLM
                try:
                    tg_file = await context.bot.get_file(photo.file_id)
                    suffix = ".webp" if update.message.sticker else ".jpg"
                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir=str(_telegram_temp_dir())) as tmp:
                        tmp_path = Path(tmp.name)
                    await tg_file.download_to_drive(str(tmp_path))
                except Exception:
                    logger.exception("Failed to download image (chat=%s)", chat_id)
                    await channel.send("I couldn't download that image.")
                    return

                try:
                    response = await self._analyze_image(tmp_path, caption)
                    await channel.send(response)
                finally:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError as exc:
                        logger.debug("Telegram temp file cleanup failed: %s", exc)
                return

            # --- Document ---
            doc = update.message.document
            if doc is not None:
                file_name = doc.file_name or "unknown"
                ext = Path(file_name).suffix.lower()

                if ext == ".pdf":
                    await channel.send(
                        "I received your PDF, but PDF parsing isn't supported yet. "
                        "Try sending the content as a text file (.txt) instead."
                    )
                    return

                if ext not in _EXTRACTABLE_EXTENSIONS:
                    await channel.send(
                        f"I can process text files ({', '.join(sorted(_EXTRACTABLE_EXTENSIONS))}). "
                        f"This file type ({ext or 'unknown'}) isn't supported yet."
                    )
                    return

                # Download and extract text
                try:
                    tg_file = await context.bot.get_file(doc.file_id)
                    with tempfile.NamedTemporaryFile(suffix=ext, delete=False, dir=str(_telegram_temp_dir())) as tmp:
                        tmp_path = Path(tmp.name)
                    await tg_file.download_to_drive(str(tmp_path))
                    content = tmp_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    logger.exception("Failed to read document (chat=%s)", chat_id)
                    await channel.send("I couldn't read that file.")
                    return
                finally:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError as exc:
                        logger.debug("Telegram temp file cleanup failed: %s", exc)

                # Truncate very large files
                if len(content) > 8000:
                    content = content[:8000] + "\n...(truncated)"

                combined = _telegram_document_context(file_name, caption, content)
                await self._process_and_respond(channel, combined, chat_id)
                return

            await channel.send(
                "Received your message, but this file type isn't supported yet. Try sending text or an image."
            )

        # ----------------------------------------------------------------
        # Callback queries (inline button presses)
        # ----------------------------------------------------------------
        async def _on_callback_query(update: Update, context: Any) -> None:
            query = update.callback_query
            if query is None:
                return

            await query.answer()  # acknowledge the button press

            chat_id = query.message.chat_id if query.message else None
            if chat_id is None:
                return

            # SEC-062: only the registered owner may drive button-triggered
            # actions (e.g. confirming a gate card). A group member must not be
            # able to press an approval button that runs in the owner's context.
            if not _is_owner(chat_id):
                return

            data = query.data or ""

            from messaging.formatters import gate_callback_reply_text, parse_gate_callback_data

            gate_callback = parse_gate_callback_data(data)
            if gate_callback is not None:
                _token, verb = gate_callback
                command_text = gate_callback_reply_text(verb)
                # Scope to the stable install-owner identity (see _owner_user_key).
                user_key = self._owner_user_key()
                channel = self._get_or_create_channel(context.bot, chat_id)
                await self._process_and_respond(channel, command_text, chat_id, user_key=user_key)
                return

            # Playback control buttons from now-playing notifications
            if data.startswith("ctrl_"):
                action = data[5:]  # e.g. "pause", "skip", "vol_up", "vol_down"
                command_map = {
                    "pause": "pause",
                    "resume": "resume",
                    "skip": "skip",
                    "vol_up": "volume up",
                    "vol_down": "volume down",
                }
                command_text = command_map.get(action)
                if command_text:
                    channel = self._get_or_create_channel(context.bot, chat_id)
                    await self._process_and_respond(channel, command_text, chat_id)
                return

            # Generic button replies (from ask_with_buttons)
            # Extract the label from callback_data "btn_{i}_{label}"
            if data.startswith("btn_"):
                parts = data.split("_", 2)
                label = parts[2] if len(parts) > 2 else data
                channel = self._channels.get(chat_id)
                if channel is not None:
                    channel._deliver_reply(label)
                return

        # ----------------------------------------------------------------
        # Register handlers
        # ----------------------------------------------------------------
        self._app.add_handler(CommandHandler("start", _on_start))
        self._app.add_handler(CommandHandler("help", _on_help))
        self._app.add_handler(CommandHandler("debug", _on_debug))
        self._app.add_handler(CommandHandler("log", _on_log))
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
        self._app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, _on_voice))
        self._app.add_handler(
            MessageHandler(
                filters.PHOTO | filters.Sticker.ALL | filters.Document.ALL,
                _on_media,
            )
        )
        self._app.add_handler(CallbackQueryHandler(_on_callback_query))

        # ----------------------------------------------------------------
        # Start polling + now-playing monitor
        # ----------------------------------------------------------------
        logger.info("Telegram bot starting (polling mode)")
        self._connection_state = "connecting"
        try:
            await self._app.initialize()
            await self._app.start()
            await self._app.updater.start_polling(drop_pending_updates=True)

            self._connection_state = "connected"
            self._connected_at = time.monotonic()

            # Start now-playing monitor as background task
            self._now_playing_task = asyncio.create_task(self._now_playing_monitor(), name="telegram-now-playing")

            # Block until cancelled
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            logger.info("Telegram bot shutting down")
        finally:
            self._connection_state = "disconnected"
            self._connected_at = 0.0
            if self._now_playing_task is not None:
                self._now_playing_task.cancel()
                try:
                    await self._now_playing_task
                except asyncio.CancelledError as exc:
                    logger.debug("Telegram now-playing task cancelled: %s", exc)
                except (OSError, RuntimeError, ValueError) as exc:
                    logger.debug("Telegram now-playing task ended with error: %s", exc)
            try:
                if self._app.updater.running:
                    await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as exc:
                logger.debug("Telegram shutdown error: %s", exc)

    async def stop(self) -> None:
        """Signal the listener to stop."""
        if self._now_playing_task is not None:
            self._now_playing_task.cancel()
        if self._app is not None:
            try:
                if self._app.updater and self._app.updater.running:
                    await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as exc:
                logger.debug("Telegram stop error: %s", exc)

    # ----------------------------------------------------------------
    # Now-playing push notifications
    # ----------------------------------------------------------------
    async def _now_playing_monitor(self) -> None:
        """Periodically check for track changes and push notifications."""
        while True:
            try:
                await asyncio.sleep(5.0)

                # Guard: need owner and bot to be connected
                owner_id = _get_owner_chat_id()
                if not owner_id or self._app is None:
                    continue

                owner_chat_id = int(owner_id)

                # Get current track info from pipeline's music player
                music = getattr(self._pipeline, "music", None)
                if music is None:
                    continue

                status_fn = getattr(music, "status", None) or getattr(music, "get_status", None)
                if not callable(status_fn):
                    continue

                try:
                    status = status_fn()
                except Exception:
                    continue

                if not isinstance(status, dict):
                    continue

                title = status.get("title") or status.get("track", "")
                if not title:
                    continue

                artist = status.get("artist", "")
                track_key = f"{title}|{artist}"

                if track_key == self._last_announced_track:
                    continue

                self._last_announced_track = track_key

                # Build now-playing message with control buttons
                np_text = f"Now playing: {_format_now_playing(title, artist)}"

                try:
                    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

                    keyboard = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton("Pause", callback_data="ctrl_pause"),
                                InlineKeyboardButton("Skip", callback_data="ctrl_skip"),
                                InlineKeyboardButton("Vol-", callback_data="ctrl_vol_down"),
                                InlineKeyboardButton("Vol+", callback_data="ctrl_vol_up"),
                            ]
                        ]
                    )
                    bot = self._app.bot
                    await bot.send_message(
                        chat_id=owner_chat_id,
                        text=np_text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                except Exception as exc:
                    # Fail silently — don't disrupt playback (Flag 2)
                    logger.debug("Now-playing push failed: %s", exc)

            except asyncio.CancelledError:
                return
            except Exception:
                # Fail silently — never let monitoring disrupt playback (Flag 2)
                logger.debug("Now-playing monitor error", exc_info=True)
                await asyncio.sleep(10.0)

    # ----------------------------------------------------------------
    # Image analysis via LLM
    # ----------------------------------------------------------------
    async def _analyze_image(self, image_path: Path, caption: str = "") -> str:
        """Try to analyze an image using a multimodal LLM provider."""
        try:
            import base64

            image_bytes = image_path.read_bytes()
            image_b64 = base64.b64encode(image_bytes).decode("utf-8")

            # Determine mime type from extension
            ext = image_path.suffix.lower()
            mime_map = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".webp": "image/webp",
                ".gif": "image/gif",
            }
            mime_type = mime_map.get(ext, "image/jpeg")

            prompt = _telegram_image_context(caption)

            # Try to get the LLM handler from the pipeline
            gpt_handler = getattr(self._pipeline, "gpt_handler", None)
            if gpt_handler is None:
                ai_controller = getattr(self._pipeline, "ai_controller", None)
                gpt_handler = getattr(ai_controller, "client", None) if ai_controller else None

            if gpt_handler is None:
                return (
                    "Image analysis requires an AI model. "
                    "Configure an LLM provider (OpenAI, Anthropic, or Google) in Settings to enable this."
                )

            # Build multimodal message (OpenAI/Anthropic compatible format)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_b64}",
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ]

            # Try the chat method on the handler
            chat_fn = getattr(gpt_handler, "chat", None) or getattr(gpt_handler, "complete", None)
            if chat_fn is None:
                return "I received your image but the configured AI model doesn't " "support image analysis."

            if asyncio.iscoroutinefunction(chat_fn):
                response = await chat_fn(messages)
            else:
                response = await asyncio.to_thread(chat_fn, messages)

            if isinstance(response, str):
                return response
            # Some providers return dicts
            if isinstance(response, dict):
                return (
                    response.get("message", "")
                    or response.get("content", "")
                    or response.get("text", "")
                    or str(response)
                )
            return str(response)

        except Exception as exc:
            logger.warning("Image analysis failed: %s", exc)
            return "I received your image but couldn't analyze it. " "The AI model may not support multimodal input."
