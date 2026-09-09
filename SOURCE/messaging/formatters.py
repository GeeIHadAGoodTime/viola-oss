"""Format-aware outbound message rendering.

Converts Markdown responses to platform-native formatting:
    - Telegram: Markdown -> HTML (bold, italic, code, links)
    - Discord: keep Markdown (native support), add embed wrappers for structured data
    - Slack: Markdown -> Block Kit mrkdwn dialect
    - Others: strip to plain text

Each formatter handles edge cases like code blocks, URLs, special characters,
and platform-specific message length limits.
"""

from __future__ import annotations

import hashlib
import html
import re
from typing import Any
from urllib.parse import quote, quote_plus

from core.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Base formatter
# ---------------------------------------------------------------------------


class MessageFormatter:
    """Base class for platform-specific message formatters.

    Subclasses override :meth:`format` to convert Markdown to their
    platform's native format.  The base implementation returns Markdown as-is.
    """

    def format(self, text: str) -> str:
        """Convert Markdown text to platform-native format.

        Args:
            text: Markdown-formatted text from the LLM or pipeline.

        Returns:
            Platform-formatted text ready for sending.
        """
        return text

    def format_structured(self, title: str, fields: dict[str, str]) -> str:
        """Format structured data (key-value pairs) for the platform.

        Args:
            title: A heading for the structured data.
            fields: Key-value pairs to display.

        Returns:
            Platform-formatted structured text.
        """
        lines = [title]
        for key, value in fields.items():
            lines.append("  %s: %s" % (key, value))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram: Markdown -> HTML
# ---------------------------------------------------------------------------

# Escape characters that are special in Telegram HTML
_TG_HTML_ESCAPES = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
}


def _tg_escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    for char, replacement in _TG_HTML_ESCAPES.items():
        text = text.replace(char, replacement)
    return text


class TelegramFormatter(MessageFormatter):
    """Convert Markdown to Telegram HTML format.

    Telegram supports a subset of HTML: <b>, <i>, <code>, <pre>, <a>.
    """

    def format(self, text: str) -> str:
        """Convert Markdown to Telegram HTML."""
        if not text:
            return text

        result = text

        # Protect code blocks first (``` ... ```)
        code_blocks: list[str] = []

        def _save_code_block(m: re.Match) -> str:
            lang = m.group(1) or ""
            code = m.group(2)
            idx = len(code_blocks)
            escaped = _tg_escape_html(code)
            if lang:
                code_blocks.append('<pre><code class="language-%s">%s</code></pre>' % (lang, escaped))
            else:
                code_blocks.append("<pre>%s</pre>" % escaped)
            return "\x00CODEBLOCK_%d\x00" % idx

        result = re.sub(
            r"```(\w*)\n?(.*?)```",
            _save_code_block,
            result,
            flags=re.DOTALL,
        )

        # Protect inline code (`...`)
        inline_codes: list[str] = []

        def _save_inline_code(m: re.Match) -> str:
            idx = len(inline_codes)
            escaped = _tg_escape_html(m.group(1))
            inline_codes.append("<code>%s</code>" % escaped)
            return "\x00INLINE_%d\x00" % idx

        result = re.sub(r"`([^`]+)`", _save_inline_code, result)

        # Escape HTML in remaining text
        result = _tg_escape_html(result)

        # Bold: **text** or __text__
        result = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", result)
        result = re.sub(r"__(.+?)__", r"<b>\1</b>", result)

        # Italic: *text* or _text_ (but not inside words with underscores)
        result = re.sub(r"(?<!\w)\*([^*]+?)\*(?!\w)", r"<i>\1</i>", result)
        result = re.sub(r"(?<!\w)_([^_]+?)_(?!\w)", r"<i>\1</i>", result)

        # Strikethrough: ~~text~~
        result = re.sub(r"~~(.+?)~~", r"<s>\1</s>", result)

        # Links: [text](url)
        result = re.sub(
            r"\[([^\]]+)\]\(([^)]+)\)",
            r'<a href="\2">\1</a>',
            result,
        )

        # Restore code blocks and inline code
        for idx, block in enumerate(code_blocks):
            result = result.replace("\x00CODEBLOCK_%d\x00" % idx, block)
        for idx, code in enumerate(inline_codes):
            result = result.replace("\x00INLINE_%d\x00" % idx, code)

        return result

    def format_structured(self, title: str, fields: dict[str, str]) -> str:
        """Format structured data as Telegram HTML."""
        lines = ["<b>%s</b>" % _tg_escape_html(title)]
        for key, value in fields.items():
            lines.append("  <b>%s:</b> %s" % (_tg_escape_html(key), _tg_escape_html(value)))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Discord: Keep Markdown, add embed support
# ---------------------------------------------------------------------------


class DiscordFormatter(MessageFormatter):
    """Discord natively supports Markdown, so minimal conversion needed.

    Adds embed-style formatting for structured data.
    """

    def format(self, text: str) -> str:
        """Discord supports Markdown natively -- return as-is."""
        return text

    def format_structured(self, title: str, fields: dict[str, str]) -> str:
        """Format structured data using Discord embed-style markdown.

        Uses bold headers and indented fields for visual hierarchy.
        """
        lines = ["**%s**" % title]
        for key, value in fields.items():
            lines.append("> **%s:** %s" % (key, value))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Slack: Markdown -> mrkdwn
# ---------------------------------------------------------------------------


class SlackFormatter(MessageFormatter):
    """Convert Markdown to Slack's mrkdwn format.

    Slack mrkdwn differences from standard Markdown:
        - Bold: *text* (not **text**)
        - Italic: _text_ (same)
        - Strikethrough: ~text~ (not ~~text~~)
        - Code blocks: ```text``` (same)
        - Links: <url|text> (not [text](url))
    """

    def format(self, text: str) -> str:
        """Convert Markdown to Slack mrkdwn."""
        if not text:
            return text

        result = text

        # Protect code blocks first
        code_blocks: list[str] = []

        def _save_code_block(m: re.Match) -> str:
            idx = len(code_blocks)
            code_blocks.append(m.group(0))
            return "\x00CODEBLOCK_%d\x00" % idx

        result = re.sub(r"```.*?```", _save_code_block, result, flags=re.DOTALL)

        # Protect inline code
        inline_codes: list[str] = []

        def _save_inline_code(m: re.Match) -> str:
            idx = len(inline_codes)
            inline_codes.append(m.group(0))
            return "\x00INLINE_%d\x00" % idx

        result = re.sub(r"`[^`]+`", _save_inline_code, result)

        # Links: [text](url) -> <url|text>
        result = re.sub(
            r"\[([^\]]+)\]\(([^)]+)\)",
            r"<\2|\1>",
            result,
        )

        # Bold: **text** -> *text*
        result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)

        # Strikethrough: ~~text~~ -> ~text~
        result = re.sub(r"~~(.+?)~~", r"~\1~", result)

        # Restore code blocks and inline code
        for idx, block in enumerate(code_blocks):
            result = result.replace("\x00CODEBLOCK_%d\x00" % idx, block)
        for idx, code in enumerate(inline_codes):
            result = result.replace("\x00INLINE_%d\x00" % idx, code)

        return result

    def format_structured(self, title: str, fields: dict[str, str]) -> str:
        """Format structured data using Slack mrkdwn."""
        lines = ["*%s*" % title]
        for key, value in fields.items():
            lines.append(">*%s:* %s" % (key, value))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Plain text: strip all formatting
# ---------------------------------------------------------------------------


class PlainTextFormatter(MessageFormatter):
    """Strip all Markdown formatting to plain text.

    Used for platforms that don't support any rich text (Signal, WhatsApp
    basic mode, etc.).
    """

    def format(self, text: str) -> str:
        """Strip Markdown formatting to plain text."""
        if not text:
            return text

        result = text

        # Remove code block fences but keep content
        result = re.sub(r"```\w*\n?", "", result)

        # Remove inline code backticks
        result = re.sub(r"`([^`]+)`", r"\1", result)

        # Bold: **text** or __text__ -> text
        result = re.sub(r"\*\*(.+?)\*\*", r"\1", result)
        result = re.sub(r"__(.+?)__", r"\1", result)

        # Italic: *text* or _text_ -> text
        result = re.sub(r"(?<!\w)\*([^*]+?)\*(?!\w)", r"\1", result)
        result = re.sub(r"(?<!\w)_([^_]+?)_(?!\w)", r"\1", result)

        # Strikethrough: ~~text~~ -> text
        result = re.sub(r"~~(.+?)~~", r"\1", result)

        # Links: [text](url) -> text (url)
        result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", result)

        return result


class MatrixFormatter(PlainTextFormatter):
    """Render Matrix message bodies as plain text plus optional custom HTML.

    Matrix ``m.text`` bodies are plain text; clients do not consistently render
    Markdown markers.  The plain body strips Markdown for readability, while
    ``format_html()`` returns a native ``formatted_body`` for Matrix-compatible
    transports that support ``org.matrix.custom.html``.
    """

    def format_html(self, text: str) -> str:
        """Convert a small Markdown subset to Matrix custom HTML."""
        if not text:
            return text

        result = html.escape(text, quote=False)

        result = re.sub(
            r"```(\w*)\n?(.*?)```",
            lambda m: "<pre><code>%s</code></pre>" % m.group(2),
            result,
            flags=re.DOTALL,
        )
        result = re.sub(r"`([^`]+)`", r"<code>\1</code>", result)
        result = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", result)
        result = re.sub(r"__(.+?)__", r"<strong>\1</strong>", result)
        result = re.sub(r"(?<!\w)\*([^*]+?)\*(?!\w)", r"<em>\1</em>", result)
        result = re.sub(r"(?<!\w)_([^_]+?)_(?!\w)", r"<em>\1</em>", result)
        result = re.sub(r"~~(.+?)~~", r"<del>\1</del>", result)
        result = re.sub(
            r"\[([^\]]+)\]\(([^)]+)\)",
            lambda m: '<a href="%s">%s</a>' % (html.escape(m.group(2), quote=True), m.group(1)),
            result,
        )
        return "<br>".join(result.splitlines())


# ---------------------------------------------------------------------------
# Registry and factory
# ---------------------------------------------------------------------------

# Channel type -> formatter class
_FORMATTER_REGISTRY: dict[str, type[MessageFormatter]] = {
    "telegram": TelegramFormatter,
    "discord": DiscordFormatter,
    "slack": SlackFormatter,
    "signal": PlainTextFormatter,
    "whatsapp": PlainTextFormatter,
    "matrix": MatrixFormatter,
    "voice": PlainTextFormatter,  # TTS should get clean text
    "console": PlainTextFormatter,
}

# Cached instances
_formatter_instances: dict[str, MessageFormatter] = {}


def get_formatter(channel_type: str) -> MessageFormatter:
    """Get the message formatter for a specific channel type.

    Returns a cached singleton per channel type. Falls back to
    PlainTextFormatter for unknown channels.
    """
    if channel_type not in _formatter_instances:
        cls = _FORMATTER_REGISTRY.get(channel_type, PlainTextFormatter)
        _formatter_instances[channel_type] = cls()
    return _formatter_instances[channel_type]


def format_for_channel(channel_type: str, text: str) -> str:
    """Convenience function: format text for a specific channel.

    Args:
        channel_type: Platform identifier.
        text: Markdown-formatted text.

    Returns:
        Platform-formatted text.
    """
    return get_formatter(channel_type).format(text)


# ---------------------------------------------------------------------------
# Card -> text rendering (channel-agnostic)
# ---------------------------------------------------------------------------

_GATE_CALLBACK_PREFIX = "vio:gate"
_GATE_CALLBACK_RE = re.compile(r"^vio:gate:([^:]{1,48}):(yes|no|approve|decline|cancel|sign|continue)$")


def extract_card(result: Any) -> dict[str, Any] | None:
    """Return a structured card from a pipeline result, if present."""
    data = getattr(result, "data", None) or {}
    if not isinstance(data, dict):
        return None
    card = data.get("card")
    return card if isinstance(card, dict) and card else None


def is_gate_review_card(card: Any) -> bool:
    """Return True when *card* is a gate-review content card."""
    return isinstance(card, dict) and card.get("type") == "gate_review"


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _gate_ctas(card: dict[str, Any]) -> list[dict[str, Any]]:
    """Return CTA dictionaries from all known card shapes, preserving order."""
    ctas: list[dict[str, Any]] = []
    for key in ("cta", "primary_cta", "secondary_cta", "tertiary_cta"):
        value = card.get(key)
        if isinstance(value, dict):
            ctas.append(value)

    for key in ("ctas", "actions", "buttons"):
        values = card.get(key)
        if isinstance(values, list):
            ctas.extend(item for item in values if isinstance(item, dict))

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for cta in ctas:
        marker = (
            _as_text(cta.get("label")),
            _as_text(cta.get("action")),
            _as_text(cta.get("text")),
            _as_text(cta.get("url")),
        )
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(cta)
    return deduped


def _gate_details(card: dict[str, Any]) -> list[tuple[str, str]]:
    details = card.get("details")
    rendered: list[tuple[str, str]] = []
    if isinstance(details, dict):
        for key, value in details.items():
            rendered.append((_as_text(key), _as_text(value)))
        return rendered
    if not isinstance(details, list):
        return rendered
    for item in details:
        if isinstance(item, dict):
            label = _as_text(item.get("label") or item.get("name") or item.get("key") or item.get("title"))
            value = _as_text(item.get("value") or item.get("text") or item.get("body"))
            if label or value:
                rendered.append((label or "Detail", value))
        else:
            value = _as_text(item)
            if value:
                rendered.append(("Detail", value))
    return rendered


def _gate_token(card: dict[str, Any]) -> str:
    for key in ("token", "gate_token", "confirmation_token", "task_id", "id"):
        token = _as_text(card.get(key))
        if token:
            return token

    subject_url = _as_text(card.get("subject_url"))
    if subject_url:
        match = re.search(r"/confirm/([^/?#]+)", subject_url)
        if match:
            return match.group(1)
    return _as_text(card.get("gate_kind")) or "gate"


def _safe_callback_token(token: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._~-]+", "-", token).strip("-") or "gate"
    if len(safe) <= 32:
        return safe
    digest = hashlib.sha1(token.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return "%s-%s" % (safe[:20], digest)


def _gate_verb(cta: dict[str, Any]) -> str:
    explicit = _as_text(cta.get("verb")).lower()
    if explicit in {"yes", "no", "approve", "decline", "cancel", "sign", "continue"}:
        return explicit
    text = ("%s %s" % (_as_text(cta.get("text")), _as_text(cta.get("label")))).lower()
    if any(word in text for word in ("no", "don't", "do not", "cancel", "stop", "decline", "reject")):
        return "no"
    if any(word in text for word in ("yes", "sign", "continue", "approve", "confirm", "pay", "go ahead")):
        return "yes"
    return "continue"


def gate_callback_data(card: dict[str, Any], cta: dict[str, Any]) -> str:
    """Build compact Telegram/Discord callback data for a gate CTA."""
    token = _safe_callback_token(_gate_token(card))
    verb = _gate_verb(cta)
    data = "%s:%s:%s" % (_GATE_CALLBACK_PREFIX, token, verb)
    if len(data.encode("utf-8")) <= 64:
        return data
    digest = hashlib.sha1(token.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    return "%s:%s:%s" % (_GATE_CALLBACK_PREFIX, digest, verb)


def parse_gate_callback_data(data: str) -> tuple[str, str] | None:
    """Parse ``vio:gate:<token>:<verb>`` callback data."""
    match = _GATE_CALLBACK_RE.match(data or "")
    if not match:
        return None
    return match.group(1), match.group(2)


def matrix_to_reply_link(room_id: str, reply_text: str) -> str:
    """Build a Matrix.to link that opens the room with a prefilled reply body."""
    body = quote_plus(reply_text)
    if not room_id:
        return "https://matrix.to/#/?body=%s" % body
    return "https://matrix.to/#/%s?body=%s" % (quote(room_id, safe=""), body)


def gate_callback_reply_text(verb: str) -> str:
    """Map a compact gate callback verb back to chat text for the pipeline."""
    normalized = (verb or "").strip().lower()
    if normalized in {"no", "decline", "cancel"}:
        return "no"
    if normalized == "approve":
        return "yes confirm payment"
    if normalized in {"yes", "sign", "continue"}:
        return "yes sign it and continue"
    return normalized or "yes"


def gate_button_specs(card: dict[str, Any]) -> list[dict[str, str]]:
    """Return normalized CTA button specs for rich messaging adapters."""
    specs: list[dict[str, str]] = []
    for cta in _gate_ctas(card):
        label = _as_text(cta.get("label")) or "Continue"
        action = _as_text(cta.get("action")).lower()
        if action == "open_url":
            url = _as_text(cta.get("url") or cta.get("href") or card.get("subject_url"))
            if url:
                specs.append({"label": label, "action": "open_url", "url": url})
            continue
        if action == "send_chat":
            text = _as_text(cta.get("text")) or gate_callback_reply_text(_gate_verb(cta))
            specs.append(
                {
                    "label": label,
                    "action": "send_chat",
                    "text": text,
                    "verb": _gate_verb(cta),
                    "callback_data": gate_callback_data(card, cta),
                }
            )
    return specs


def _gate_reply_hint(card: dict[str, Any]) -> str:
    gate_kind = _as_text(card.get("gate_kind")).lower()
    send_specs = [spec for spec in gate_button_specs(card) if spec.get("action") == "send_chat"]
    if gate_kind == "signature":
        return 'Reply "yes" to sign and continue, or "no" to cancel.'
    if gate_kind == "payment":
        return 'Open the confirmation link to review payment. Reply "no" to cancel if needed.'
    if send_specs:
        hints = []
        for spec in send_specs:
            text = spec.get("text", "")
            if text:
                hints.append('reply "%s"' % text)
        if hints:
            return "You can also %s." % " or ".join(hints)
    return ""


def format_gate_review_as_text(card: dict[str, Any]) -> str:
    """Render a gate-review card as Markdown-ish fallback text."""
    parts: list[str] = []
    title = _as_text(card.get("title")) or "Review required"
    if title:
        parts.append("**%s**" % title)

    body = _as_text(card.get("body"))
    if body:
        parts.append(body)

    subject_url = _as_text(card.get("subject_url"))
    if subject_url:
        parts.append("Review link: %s" % subject_url)

    details = _gate_details(card)
    if details:
        parts.append("Details:")
        for label, value in details:
            if label and value:
                parts.append("- %s: %s" % (label, value))
            elif value:
                parts.append("- %s" % value)

    specs = gate_button_specs(card)
    if specs:
        parts.append("Actions:")
        for spec in specs:
            label = spec.get("label", "Action")
            if spec.get("action") == "open_url":
                parts.append("- %s: %s" % (label, spec.get("url", "")))
            elif spec.get("action") == "send_chat":
                parts.append('- %s: reply "%s"' % (label, spec.get("text", "")))

    hint = _gate_reply_hint(card)
    if hint:
        parts.append(hint)

    return "\n".join(part for part in parts if part)


def _html_lines(text: str) -> str:
    return "<br>".join(html.escape(line, quote=False) for line in text.splitlines())


def format_gate_review_as_telegram_html(card: dict[str, Any]) -> str:
    """Render a gate-review card body as Telegram-safe HTML."""
    parts: list[str] = []
    title = _as_text(card.get("title")) or "Review required"
    parts.append("<b>%s</b>" % html.escape(title, quote=False))

    body = _as_text(card.get("body"))
    if body:
        parts.append(_html_lines(body))

    subject_url = _as_text(card.get("subject_url"))
    if subject_url:
        escaped_url = html.escape(subject_url, quote=True)
        parts.append('Review link: <a href="%s">%s</a>' % (escaped_url, html.escape(subject_url, quote=False)))

    details = _gate_details(card)
    if details:
        detail_lines = ["<b>Details</b>"]
        for label, value in details:
            if label and value:
                detail_lines.append(
                    "<b>%s:</b> %s" % (html.escape(label, quote=False), html.escape(value, quote=False))
                )
            elif value:
                detail_lines.append(html.escape(value, quote=False))
        parts.append("<br>".join(detail_lines))

    hint = _gate_reply_hint(card)
    if hint:
        parts.append(html.escape(hint, quote=False))

    return "\n\n".join(parts)


def format_gate_review_as_matrix_html(card: dict[str, Any], room_id: str = "") -> str:
    """Render a gate-review card as Matrix custom HTML."""
    parts: list[str] = []
    title = _as_text(card.get("title")) or "Review required"
    parts.append("<strong>%s</strong>" % html.escape(title, quote=False))

    body = _as_text(card.get("body"))
    if body:
        parts.append(_html_lines(body))

    subject_url = _as_text(card.get("subject_url"))
    if subject_url:
        escaped_url = html.escape(subject_url, quote=True)
        parts.append('Review link: <a href="%s">%s</a>' % (escaped_url, html.escape(subject_url, quote=False)))

    details = _gate_details(card)
    if details:
        detail_lines = ["<strong>Details</strong>"]
        for label, value in details:
            if label and value:
                detail_lines.append(
                    "<strong>%s:</strong> %s" % (html.escape(label, quote=False), html.escape(value, quote=False))
                )
            elif value:
                detail_lines.append(html.escape(value, quote=False))
        parts.append("<br>".join(detail_lines))

    specs = gate_button_specs(card)
    action_lines: list[str] = []
    reply_hints: list[str] = []
    for spec in specs:
        label = html.escape(spec.get("label", "Action"), quote=False)
        if spec.get("action") == "open_url":
            url = html.escape(spec.get("url", ""), quote=True)
            if url:
                action_lines.append('<a href="%s">%s</a>' % (url, label))
        elif spec.get("action") == "send_chat":
            reply_text = spec.get("text", "")
            href = matrix_to_reply_link(room_id, reply_text)
            action_lines.append('<a href="%s">%s</a>' % (html.escape(href, quote=True), label))
            if reply_text:
                reply_hints.append("or reply: %s" % reply_text)
    if action_lines:
        parts.append("<br>".join(action_lines))

    if reply_hints:
        parts.append("<br>".join(html.escape(hint, quote=False) for hint in reply_hints))
    else:
        hint = _gate_reply_hint(card)
        if hint:
            parts.append(html.escape(hint, quote=False))

    return "<br><br>".join(parts)


def format_card_as_text(card: dict[str, Any] | None, channel_type: str = "plain") -> str:
    """Render a content card dict as formatted text for messaging channels.

    Messaging channels (Telegram, Discord, etc.) cannot display the React
    ContentCard component. This converts the card data into readable text
    so that the full information is delivered, not just the voice summary.

    Args:
        card: Card dict with ``type``, ``title``, and type-specific fields.
        channel_type: Target platform for format hints (currently unused --
            the output is Markdown which each channel's formatter converts).

    Returns:
        Formatted text representation of the card, or empty string if the
        card is invalid/empty.
    """
    if not card or not isinstance(card, dict):
        return ""

    card_type = card.get("type", "")
    title = card.get("title", "")
    parts: list[str] = []

    if title:
        parts.append("**%s**" % title)

    subtitle = card.get("subtitle")
    if subtitle:
        parts.append("_%s_" % subtitle)

    if card_type == "list":
        items = card.get("items", [])
        for item in items:
            parts.append("\u2022 %s" % item)

    elif card_type == "info":
        value = card.get("value", "")
        unit = card.get("unit", "")
        parts.append("%s%s" % (value, (" %s" % unit) if unit else ""))

    elif card_type == "table":
        columns = card.get("columns", [])
        rows = card.get("rows", [])
        if columns:
            parts.append(" | ".join(str(c).replace("|", "\\|") for c in columns))
            parts.append(" | ".join("---" for _ in columns))
        for row in rows:
            parts.append(" | ".join(str(cell).replace("|", "\\|") for cell in row))

    elif card_type == "detail":
        body = card.get("body", "")
        if body:
            parts.append(body)
        source = card.get("source")
        if source:
            parts.append("_Source: %s_" % source)

    elif card_type == "gate_review":
        return format_gate_review_as_text(card)

    else:
        # Unknown card type — dump title only (already added above)
        pass

    return "\n".join(parts)


def extract_message_with_card(result: Any, channel_type: str = "plain") -> str:
    """Extract the response text from a pipeline result for messaging channels.

    Mirrors the web UI's card-XOR-chat behavior (UX-CARD-1): when a card is
    present, the card text IS the response — the ``message`` field is a TTS
    artifact (voice summary) that messaging users don't need.  When no card
    is present, the ``message`` field is the response.

    Args:
        result: A ``PipelineResult`` (or any object with a ``.data`` dict).

    Returns:
        Card text when a card is present, otherwise the message string.
    """
    data = getattr(result, "data", None) or {}
    if not isinstance(data, dict):
        return ""

    card = extract_card(result)
    if card:
        card_text = format_card_as_text(card, channel_type=channel_type)
        if card_text:
            return card_text

    message = data.get("message", "")
    return message if isinstance(message, str) else ""
