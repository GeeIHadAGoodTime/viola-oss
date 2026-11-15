"""Durable post-turn memory extraction."""

from __future__ import annotations

import asyncio
import contextvars
import copy
import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from dataclasses import replace as dataclasses_replace
from pathlib import Path
from typing import Any

from config.settings import settings
from core.constants import TIMEOUT_MINUTE
from core.logging_config import get_logger
from core.secrets_mask import mask_secrets_in_text, scan_secrets_in_text
from services.memory.dir import MemoryDir, get_memory_dir
from services.memory.hygiene import is_junk_memory_line, mask_labelled_zip
from services.memory.store import (
    _PII_CONTENT_RE,
    _PROMPT_SENSITIVE_RE,
    SENSITIVE_CONTENT_RE,
    contains_street_address,
    redact_pii_output,
    redact_sensitive_prompt_text,
)

logger = get_logger(__name__)

_CURSOR_FILE = "extract_memories_cursor.json"
_MEMORY_WRITE_TOOLS = {"memory_store", "memory_forget"}

# Upper bound on the transcript characters handed to redaction + the background
# extraction call. The extractor only surfaces a handful of durable user facts
# through a background LLM call bounded at max_output_tokens=500 / timeout_s=15.0
# (services/openai_background.run_background_openai_response); it never needs
# megabytes of page dumps, so ~6k tokens of context is already generous.
# Truncating BEFORE redaction also bounds the redaction cost. Keep the LEADING
# window: model_visible_messages_to_transcript builds the transcript
# chronologically, so the user's own request and early context lead it while
# large machine-generated tool-result / page-dump lines pile up toward the tail.
_MAX_EXTRACTION_TRANSCRIPT_CHARS = 24000


@dataclass(frozen=True)
class ExtractedMemory:
    content: str
    category: str = "fact"


@dataclass(frozen=True)
class _ExtractionJob:
    user_id: str
    transcript: str
    task_id: str
    tools_called: tuple[str, ...]
    model_visible_messages: tuple[Mapping[str, Any], ...] | None = None
    # The address check (see "Addresses the user gave in a turn" below). What the
    # user wrote or said in this turn, and the user's words of earlier turns whose
    # job was replaced while waiting (coalesced) and so never checked.
    user_text: str = ""
    carried_user_texts: tuple[str, ...] = ()
    # False when memory extraction is skipped for this turn (a memory tool ran):
    # the job then only checks for addresses.
    extract_memory: bool = True


_IN_FLIGHT_EXTRACTIONS: dict[str, asyncio.Task[None]] = {}
_PENDING_EXTRACTIONS: dict[str, _ExtractionJob] = {}


MemoryExtractor = Callable[[str], Sequence[ExtractedMemory | dict[str, Any] | str] | Awaitable[Sequence[Any]]]


def turn_used_memory_write(tools_called: Sequence[str]) -> bool:
    for tool in tools_called:
        normalized = str(tool or "").strip()
        if normalized in _MEMORY_WRITE_TOOLS:
            return True
        if normalized == "memory":
            return True
    return False


def _transcript_hash(transcript: str) -> str:
    return hashlib.sha256(transcript.encode("utf-8", errors="replace")).hexdigest()


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        return "\n".join(part for item in content if (part := _content_to_text(item)).strip())
    if isinstance(content, Mapping):
        if "text" in content:
            return _content_to_text(content.get("text"))
        if "content" in content:
            return _content_to_text(content.get("content"))
        try:
            return json.dumps(dict(content), ensure_ascii=False, sort_keys=True)
        except TypeError:
            return str(content)
    return str(content)


def model_visible_messages_to_transcript(messages: Sequence[Mapping[str, Any]]) -> str:
    lines: list[str] = []
    visible_index = 0
    for message in messages:
        role = (str(message.get("role") or message.get("type") or "message").strip() or "message").lower()
        if role not in {"user", "assistant"}:
            continue
        parts = [_content_to_text(message.get("content"))]
        for key in (
            "tool_calls",
            "tool_call",
            "tool_results",
            "tool_result",
            "model_visible_result",
        ):
            if key in message:
                parts.append("%s: %s" % (key, _content_to_text(message.get(key))))
        body = "\n".join(part.strip() for part in parts if part and part.strip())
        if body:
            visible_index += 1
            cursor = message.get("_memory_cursor")
            cursor_bits: list[str] = ["index=%d" % visible_index, "role=%s" % role]
            if isinstance(cursor, Mapping):
                if cursor.get("task_id"):
                    cursor_bits.append("task_id=%s" % cursor["task_id"])
                if cursor.get("visible_index"):
                    cursor_bits.append("visible_index=%s" % cursor["visible_index"])
            elif message.get("uuid"):
                cursor_bits.append("uuid=%s" % message["uuid"])
            lines.append("[model_visible_message %s]" % " ".join(cursor_bits))
            lines.append("%s: %s" % (role, body))
    return "\n".join(lines).strip()


_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_SPOKEN_TEXT_BLOCK_TYPES = frozenset({"text", "input_text", "output_text"})


def _spoken_text(content: Any) -> str:
    """Only what was said in a message: no runtime reminders, no tool blocks."""
    if isinstance(content, str):
        return _SYSTEM_REMINDER_RE.sub("", content).strip()
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping) and block.get("type") in _SPOKEN_TEXT_BLOCK_TYPES:
                parts.append(_spoken_text(block.get("text")))
            elif isinstance(block, str):
                parts.append(_spoken_text(block))
        return "\n".join(part for part in parts if part)
    return ""


def current_turn_transcript(messages: Sequence[Mapping[str, Any]]) -> str:
    """Return only the latest turn, for the memory extractor.

    The extractor used to read every model-visible message: the runtime reminder,
    all the history in the prompt and cursor lines carrying task ids. That made
    each command's extraction grow with the session, and is where "System reminder
    indicates..." and raw ids in saved memory came from.

    Tool results are left out too. A result is a log of what a tool did ("Wrote
    notes.txt"), a page, or memory read back; build 10 kept the first 1,000
    characters of each and file-action lines were saved as facts about the user.
    A memory comes from what the user said and what Viola answered.
    """
    start = 0
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if str(message.get("role") or "").lower() == "user" and _spoken_text(message.get("content")):
            start = index
            break
    lines: list[str] = []
    for message in messages[start:]:
        role = str(message.get("role") or "").lower()
        if role not in {"user", "assistant"}:
            continue
        body = _spoken_text(message.get("content"))
        if body:
            lines.append("%s: %s" % (role, body))
    return "\n".join(lines)


def _coerce_extraction_transcript(
    *,
    transcript: str,
    model_visible_messages: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    if model_visible_messages is not None:
        current_turn = current_turn_transcript(model_visible_messages).strip()
        if current_turn:
            return current_turn
    return (transcript or "").strip()


def sanitize_memory_candidate_text(content: str) -> str | None:
    clean = str(content or "").strip()
    if len(clean) < 5:
        return None
    if SENSITIVE_CONTENT_RE.search(clean):
        return None
    if scan_secrets_in_text(clean):
        return None
    if mask_secrets_in_text(clean) != clean:
        return None
    # A heard house number can be a digit off, and nothing here reads it back. A
    # street address is saved only through save_delivery_address or Settings.
    if contains_street_address(clean) or is_junk_memory_line(clean):
        return None
    redacted = mask_labelled_zip(redact_pii_output(clean)).strip()
    return redacted if len(redacted) >= 5 else None


def _normalize_extracted_memory(
    item: ExtractedMemory | dict[str, Any] | str,
) -> ExtractedMemory | None:
    if isinstance(item, ExtractedMemory):
        content = item.content
        category = item.category
    elif isinstance(item, dict):
        content = str(item.get("content") or item.get("memory") or item.get("fact") or "").strip()
        category = str(item.get("category") or "fact").strip()
    else:
        content = str(item or "").strip()
        category = "fact"
    content = sanitize_memory_candidate_text(content)
    if content is None:
        return None
    return ExtractedMemory(content=content, category=category or "fact")


def _load_reply_json(text: str) -> Any:
    clean = (text or "").strip()
    if clean.startswith("```"):
        parts = clean.split("```")
        clean = parts[1] if len(parts) > 1 else clean
        if clean.lstrip().startswith("json"):
            clean = clean.lstrip()[4:]
    return json.loads(clean)


def _memories_from(payload: Any) -> list[ExtractedMemory]:
    if not isinstance(payload, list):
        return []
    parsed: list[ExtractedMemory] = []
    for item in payload:
        normalized = _normalize_extracted_memory(item)
        if normalized is not None:
            parsed.append(normalized)
    return parsed


@dataclass(frozen=True)
class ReportedAddress:
    """One address the background pass says the user gave. Held in memory only, never logged."""

    street: str
    city: str = ""
    state: str = ""
    zip_code: str = ""


_MAX_REPORTED_ADDRESSES = 8
_MAX_ADDRESS_FIELD_CHARS = 200


def _address_field(item: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = item.get(name)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            text = str(value).strip()
            if text:
                return text[:_MAX_ADDRESS_FIELD_CHARS]
    return ""


def _addresses_from(payload: Any) -> list[ReportedAddress] | None:
    """The reply's address list; None when the reply has none (an old-style array, or not a list)."""
    if not isinstance(payload, list):
        return None
    found: list[ReportedAddress] = []
    for item in payload[:_MAX_REPORTED_ADDRESSES]:
        if isinstance(item, str):
            item = {"street": item}
        if not isinstance(item, Mapping):
            continue
        found.append(
            ReportedAddress(
                street=_address_field(item, "street", "street_line", "address_line1", "address"),
                city=_address_field(item, "city", "locality", "town"),
                state=_address_field(item, "state", "region", "province"),
                zip_code=_address_field(item, "zip", "zip_code", "postal_code", "postcode"),
            )
        )
    return found


def _parse_extractor_reply(text: str) -> tuple[list[ExtractedMemory], list[ReportedAddress] | None]:
    """Memories and reported addresses from the background reply.

    The reply is a JSON object ``{"memories": [...], "addresses": [...]}``. The
    old bare array (memories only) is still accepted; it reports no addresses
    (None: the address check did not happen). Text that is not JSON raises
    ``ValueError`` as before.
    """
    payload = _load_reply_json(text)
    if isinstance(payload, list):
        return _memories_from(payload), None
    if isinstance(payload, Mapping):
        return _memories_from(payload.get("memories")), _addresses_from(payload.get("addresses"))
    return [], None


def _parse_extractor_response(text: str) -> list[ExtractedMemory]:
    return _parse_extractor_reply(text)[0]


def _extractor_error_types() -> tuple[type[BaseException], ...]:
    errors: tuple[type[BaseException], ...] = (
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
        json.JSONDecodeError,
    )
    try:
        from openai import APIConnectionError, APIError, APITimeoutError
    except ImportError:
        return errors
    return errors + (APIError, APIConnectionError, APITimeoutError)


def _background_extraction_error_types() -> tuple[type[BaseException], ...]:
    try:
        from services.openai_background import BackgroundLLMUnavailable
    except ImportError:
        return (asyncio.TimeoutError,) + _extractor_error_types()
    return (asyncio.TimeoutError, BackgroundLLMUnavailable) + _extractor_error_types()


# --------------------------------------------------------------------------- addresses the user gave in a turn
#
# The log mask (intent/sensitive_values.py) hides a delivery address by its
# value, and it learns the value from a save_delivery_address call or a Settings
# write. A street the user types and Viola only asks about ("what city and ZIP?")
# never reaches the tool, so it stayed plain in the logs. The background pass
# above already reads every turn after it ends, hidden; it now also reports any
# postal address the user gave. A reported address is held only when its street
# words and house-number digits are in the user's own text of the turn (the
# save_delivery_address gate's own word check), and holding it schedules the
# clean-up that overwrites the earlier log lines. Held in memory only; the log
# gets counts only.

_ADDRESS_ASK = (
    "List every postal address the user wrote or said in this turn, even a partial one, one whose "
    "street name looks unusual, or one Viola did not save. For each, copy the street line exactly as the "
    "user gave it, house number and unit included, and add the city, state and ZIP code only when the user "
    "gave them. Leave out addresses only Viola said, and places that are not a postal address (a city alone, "
    "a business, a country)."
)
_COMBINED_REPLY_FORMAT = (
    'Answer with one JSON object: {"memories": <the array described above>, '
    '"addresses": [{"street": "...", "city": "...", "state": "...", "zip": "..."}]}. '
    "Use [] for a list that has nothing."
)
_ADDRESS_ONLY_SYSTEM_PROMPT = "Find the postal addresses a user gave in a completed Viola turn. Return only JSON."
_ADDRESS_ONLY_REPLY_FORMAT = (
    'Answer with one JSON object: {"addresses": [{"street": "...", "city": "...", "state": "...", "zip": "..."}]}. '
    'Use {"addresses": []} when there is none.'
)
_ADDRESS_OUTPUT_TOKENS = 500
_ADDRESS_TIMEOUT_S = 15.0
_ADDRESS_RETRY_DELAY_S = 2.0
_CHECKED_TEXTS_MAX = 64


@dataclass
class _TurnAddressCheck:
    """The address check of one job: the turn's own user text and texts carried from replaced jobs."""

    user_id: str
    current: str
    carried: tuple[str, ...] = ()
    current_checked: bool = False
    combined_attempted: bool = False
    results: list[tuple[int, int, int]] = field(default_factory=list)


# Set by _run_extraction_job while the memory call runs, so _default_extractor
# can put the address ask into that same call.
_ADDRESS_CHECK: contextvars.ContextVar[_TurnAddressCheck | None] = contextvars.ContextVar(
    "viola_turn_address_check", default=None
)
# Hashes of user texts already checked in this process, per account: a repeated
# identical turn needs no new request (the values it held stay held).
_CHECKED_TEXTS: dict[str, OrderedDict[str, None]] = {}


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _was_checked(user_id: str, text: str) -> bool:
    return _text_digest(text) in _CHECKED_TEXTS.get(user_id, OrderedDict())


def _remember_checked(user_id: str, texts: Sequence[str]) -> None:
    seen = _CHECKED_TEXTS.setdefault(user_id, OrderedDict())
    for text in texts:
        seen[_text_digest(text)] = None
        seen.move_to_end(_text_digest(text))
    while len(seen) > _CHECKED_TEXTS_MAX:
        seen.popitem(last=False)


def _redact_for_address_check(text: str) -> str:
    """The user's words with secrets hidden (cards, passwords, keys, email, phone) but a street address kept.

    ``redact_sensitive_prompt_text`` replaces a house number + name + street-type
    word ("48 Maple Street") with "[redacted]", so the memory transcript never
    shows such an address and the address ask could not report it.
    """
    kept = _PII_CONTENT_RE.sub(
        lambda match: match.group(0) if match.lastgroup == "street" else "[redacted]",
        str(text or ""),
    )
    return _PROMPT_SENSITIVE_RE.sub("[redacted sensitive]", kept)


def _street_has_content(street: str) -> bool:
    from intent.content_sanitizer import _street_name_words

    return bool(re.search(r"\d", street)) or bool(_street_name_words(street))


def _hold_reported_addresses(user_id: str, addresses: Sequence[ReportedAddress], own_text: str) -> tuple[int, int, int]:
    """Hold each reported address the user's own text really holds; returns (reported, held, dropped).

    The check is the save_delivery_address gate's (``_words_not_from_user``): every
    street-name word and the house number's digits must be in the user's words of
    the turn. A city, state or ZIP code the user did not say is left out; the
    street is still held. Anything else is dropped.
    """
    from intent.content_sanitizer import _words_not_from_user
    from intent.sensitive_values import register_tool_call

    held = dropped = 0
    for address in addresses:
        street = address.street.strip()
        if not street or not _street_has_content(street):
            dropped += 1
            continue
        missing = _words_not_from_user(
            {"street": street, "city": address.city, "zip_code": address.zip_code},
            own_text,
        )
        if "street" in missing or "house number" in missing:
            dropped += 1
            continue
        args: dict[str, str] = {"street": street}
        if address.city and "city" not in missing:
            args["city"] = address.city
        if address.zip_code and "ZIP code" not in missing:
            args["zip_code"] = address.zip_code
        if address.state and "city" not in _words_not_from_user({"city": address.state}, own_text):
            args["state"] = address.state
        register_tool_call(user_id, args, own_words=own_text)
        held += 1
    # Counts only: no value, no length.
    logger.info(
        "Address check after the turn: %d reported, %d held, %d dropped",
        len(addresses),
        held,
        dropped,
    )
    return len(addresses), held, dropped


def _hold_safely(check: _TurnAddressCheck, addresses: Sequence[ReportedAddress], own_text: str) -> None:
    try:
        check.results.append(_hold_reported_addresses(check.user_id, addresses, own_text))
    except Exception:  # noqa: BLE001 - the address check must never break memory extraction or a turn
        logger.debug("Address check after the turn could not hold a value", exc_info=True)


class _AddressCheckUnavailable(RuntimeError):
    """No background AI route for this account: nothing was sent."""


def _nothing_was_sent(exc: BaseException) -> bool:
    """True for a failure raised before any request left (no route, sign-in, consent, spend limit)."""
    if isinstance(exc, _AddressCheckUnavailable):
        return True
    try:
        from services.openai_background import (
            BackgroundLLMConsentRequired,
            BackgroundLLMLimitReached,
            BackgroundLLMSignInRequired,
        )
    except ImportError:
        return False
    return isinstance(exc, (BackgroundLLMSignInRequired, BackgroundLLMConsentRequired, BackgroundLLMLimitReached))


async def _address_only_request(user_id: str, texts: Sequence[str]) -> list[ReportedAddress]:
    """One background call that asks only for addresses (memory extraction skipped or replaced)."""
    from services.openai_background import is_background_ai_available, run_background_openai_response

    if not is_background_ai_available(user_id):
        raise _AddressCheckUnavailable("Background AI is not available for the address check")
    joined = "\n".join(text.strip() for text in texts if text.strip())[:_MAX_EXTRACTION_TRANSCRIPT_CHARS]
    safe_words = await asyncio.to_thread(_redact_for_address_check, joined)
    response = await run_background_openai_response(
        system_prompt=_ADDRESS_ONLY_SYSTEM_PROMPT,
        user_content="What the user wrote or said:\n%s\n\n%s %s"
        % (safe_words, _ADDRESS_ASK, _ADDRESS_ONLY_REPLY_FORMAT),
        max_output_tokens=_ADDRESS_OUTPUT_TOKENS,
        user_id=user_id,
        timeout_s=_ADDRESS_TIMEOUT_S,
    )
    payload = _load_reply_json(response)
    addresses = _addresses_from(payload.get("addresses")) if isinstance(payload, Mapping) else None
    if addresses is None:
        raise ValueError("The address check reply has no address list")
    return addresses


async def _finish_address_check(check: _TurnAddressCheck) -> None:
    """Check every user text of this job the memory call did not check; never raises."""
    texts = [text for text in check.carried if text.strip()]
    if check.current.strip() and not check.current_checked:
        texts.append(check.current)
    texts = [text for text in texts if not _was_checked(check.user_id, text)]
    if not texts:
        return
    # Two tries in all: the memory call that also asked counts as the first.
    attempts = 1 if check.combined_attempted else 2
    sent_before = False
    for attempt in range(attempts):
        if attempt and sent_before:
            # A request that went out and failed: give the route a moment. A
            # route that was not there is asked again at once.
            await asyncio.sleep(_ADDRESS_RETRY_DELAY_S)
        try:
            reported = await _address_only_request(check.user_id, texts)
        except _background_extraction_error_types() as exc:
            sent_before = not _nothing_was_sent(exc)
            logger.debug("Address check after the turn failed (try %d): %s", attempt + 1, type(exc).__name__)
            continue
        _hold_safely(check, reported, "\n".join(texts))
        _remember_checked(check.user_id, texts)
        return
    # Counts only, so a missed check is visible in the log.
    logger.warning(
        "Address check after the turn did not run: %d user message(s) not checked after %d tries",
        len(texts),
        attempts + (1 if check.combined_attempted else 0),
    )


async def _default_extractor(transcript: str, *, user_id: str) -> list[ExtractedMemory]:
    if not settings.auto_memory_extraction_enabled:
        return []
    from services.openai_background import is_background_ai_available, run_background_openai_response

    # Keyless customer installs route managed AI through Viola's cloud; a local
    # OPENAI_API_KEY is only one of the ways background AI can be available.
    if not is_background_ai_available(user_id):
        return []

    capped_transcript = (transcript or "")[:_MAX_EXTRACTION_TRANSCRIPT_CHARS]
    # Redaction runs the PII / sensitive-prompt regexes, which historically
    # backtracked super-linearly on pathological input (2026-07-05 prod
    # event-loop wedge). Run it off the event loop so a bad transcript can never
    # freeze the API again; the cap above already bounds the work handed to it.
    safe_transcript = await asyncio.to_thread(redact_sensitive_prompt_text, capped_transcript)
    user_content = (
        "Conversation transcript:\n%s\n\n"
        "Return a JSON array of objects like "
        '[{"content":"User prefers jazz music","category":"preference"}]. '
        "Most turns hold nothing lasting: return [] for them. "
        "Only include a fact about the user that will still be true and useful next month, "
        "stated as the fact itself. Never describe the conversation: no 'User asked', "
        "no questions, requests, commands, or what was played, searched or answered. "
        "Exclude secrets, payment data, street addresses, one-off task details, greetings, "
        "system or runtime text, identifiers, and obvious filler."
    ) % safe_transcript
    # The same call also asks for the addresses the user gave (one call, not two).
    check = _ADDRESS_CHECK.get()
    ask_addresses = check is not None and bool(check.current.strip()) and not check.current_checked
    if ask_addresses:
        own_words = check.current[:_MAX_EXTRACTION_TRANSCRIPT_CHARS]
        own_block = ""
        if contains_street_address(own_words):
            # The transcript above shows such an address as "[redacted]".
            own_block = (
                "The user's own words in this turn, for the address list only (not for memories):\n%s\n\n"
                % await asyncio.to_thread(_redact_for_address_check, own_words)
            )
        user_content = "%s\n\n%sSeparately from the memories: %s %s" % (
            user_content,
            own_block,
            _ADDRESS_ASK,
            _COMBINED_REPLY_FORMAT,
        )
        check.combined_attempted = True
    response = await run_background_openai_response(
        system_prompt=(
            "Extract durable user memories from a completed Viola turn. "
            "Return only JSON. Use categories preference, fact, correction, routine, context, or note."
        ),
        user_content=user_content,
        max_output_tokens=500,
        user_id=user_id,
        timeout_s=15.0,
    )
    if not ask_addresses or check is None:
        return _parse_extractor_response(response)
    memories, addresses = _parse_extractor_reply(response)
    if addresses is not None:
        _hold_safely(check, addresses, check.current)
        check.current_checked = True
        _remember_checked(check.user_id, [check.current])
    return memories


class ExtractMemoriesService:
    """Post-turn durable memory extractor with cursor-based idempotency."""

    def __init__(
        self,
        *,
        user_id: str,
        memory_dir: MemoryDir | None = None,
        extractor: MemoryExtractor | None = None,
    ) -> None:
        self.user_id = user_id
        self.memory_dir = memory_dir or get_memory_dir(user_id)
        self.extractor = extractor
        self.cursor_path = self.memory_dir.root / _CURSOR_FILE

    def _read_cursor(self) -> dict[str, Any]:
        if not self.cursor_path.exists():
            return {}
        try:
            payload = json.loads(self.cursor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_cursor(self, payload: dict[str, Any]) -> None:
        self.cursor_path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    def _store_extracted(self, extracted: Sequence[ExtractedMemory], digest: str, task_id: str) -> int:
        written = 0
        for memory in extracted:
            try:
                result = self.memory_dir.store_semantic_memory(memory.content, category=memory.category)
            except (OSError, ValueError) as exc:
                logger.debug("Extracted memory write skipped: %s", exc)
                continue
            if not result.get("deduped"):
                written += 1
        # Counts only. Shows on the next laptop run whether repeats and junk in
        # memory come from this background extractor or from the memory tool.
        logger.info(
            "Memory extraction: %d candidates, %d written, %d already known",
            len(extracted),
            written,
            len(extracted) - written,
        )

        self._write_cursor({"last_hash": digest, "last_task_id": task_id, "written": written})
        return written

    async def _extract(self, transcript: str) -> list[ExtractedMemory]:
        if self.extractor is None:
            return await _default_extractor(transcript, user_id=self.user_id)
        result = self.extractor(transcript)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[assignment]
        parsed: list[ExtractedMemory] = []
        for item in result:
            normalized = _normalize_extracted_memory(item)
            if normalized is not None:
                parsed.append(normalized)
        return parsed

    async def extract_after_turn(
        self,
        *,
        transcript: str = "",
        task_id: str,
        tools_called: Sequence[str] = (),
        model_visible_messages: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        clean_transcript = _coerce_extraction_transcript(
            transcript=transcript,
            model_visible_messages=model_visible_messages,
        )
        if not clean_transcript:
            return {"ok": True, "skipped": "empty_transcript", "written": 0}
        if turn_used_memory_write(tools_called):
            return {"ok": True, "skipped": "memory_tool_used", "written": 0}

        digest = _transcript_hash(clean_transcript)
        # File reads and writes run off the event loop: this job runs in the
        # background right after a turn, while that turn's answer is still
        # streaming to the screen and into speech.
        cursor = await asyncio.to_thread(self._read_cursor)
        if cursor.get("last_hash") == digest:
            return {"ok": True, "skipped": "already_processed", "written": 0}

        try:
            extracted = await self._extract(clean_transcript)
        except _background_extraction_error_types() as exc:
            logger.debug("Post-turn memory extraction failed: %s", exc)
            return {"ok": False, "error": str(exc), "written": 0}

        written = await asyncio.to_thread(self._store_extracted, extracted, digest, task_id)
        dream_result: dict[str, Any] = {}
        try:
            from services.memory.auto_dream import AutoDreamService

            dream_result = await AutoDreamService(user_id=self.user_id, memory_dir=self.memory_dir).run_if_due()
        except _background_extraction_error_types() as exc:
            logger.debug("autoDream consolidation skipped: %s", exc)
            dream_result = {"ok": False, "error": str(exc), "written": 0}
        return {
            "ok": True,
            "written": written,
            "candidates": len(extracted),
            "auto_dream": dream_result,
        }


def _extraction_key(user_id: str) -> str:
    # Per CLAUDE.md Multi-Tenant Rule: never silently fall back to a shared
    # "default" key. The module-level _IN_FLIGHT_EXTRACTIONS / _PENDING_EXTRACTIONS
    # dicts use this string as their tenancy slot, so a "default" collapse
    # would cross-contaminate any future caller that arrives with an empty
    # user_id (extracted memories from user A would race against user B in
    # a shared queue). Fail loudly so the regression surfaces immediately.
    cleaned = (user_id or "").strip()
    if not cleaned:
        raise ValueError("extract_memories: user_id is required (multi-tenant scope)")
    return cleaned


async def _run_extraction_job(job: _ExtractionJob) -> None:
    check: _TurnAddressCheck | None = None
    if job.user_text.strip() or any(text.strip() for text in job.carried_user_texts):
        check = _TurnAddressCheck(user_id=job.user_id, current=job.user_text, carried=job.carried_user_texts)
    token = _ADDRESS_CHECK.set(check)
    try:
        if job.extract_memory:
            # Opening the user's memory folder creates and migrates directories
            # (and on first use sets their ACLs): keep that off the event loop.
            service = await asyncio.to_thread(ExtractMemoriesService, user_id=job.user_id)
            await asyncio.wait_for(
                service.extract_after_turn(
                    transcript=job.transcript,
                    task_id=job.task_id,
                    tools_called=job.tools_called,
                    model_visible_messages=job.model_visible_messages,
                ),
                timeout=TIMEOUT_MINUTE,
            )
    except _background_extraction_error_types() as exc:
        logger.debug("Post-turn memory extraction job failed: %s", exc)
    finally:
        _ADDRESS_CHECK.reset(token)
    if check is None:
        return
    # Whatever the memory call did not check (memory extraction off, skipped as
    # already processed, failed, or texts carried from replaced jobs).
    try:
        await asyncio.wait_for(_finish_address_check(check), timeout=TIMEOUT_MINUTE)
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("Address check after the turn did not finish in time")


async def _extraction_worker(key: str, first_job: _ExtractionJob) -> None:
    job: _ExtractionJob | None = first_job
    try:
        while job is not None:
            await _run_extraction_job(job)
            job = _PENDING_EXTRACTIONS.pop(key, None)
    finally:
        current_task = asyncio.current_task()
        if _IN_FLIGHT_EXTRACTIONS.get(key) is current_task:
            pending = _PENDING_EXTRACTIONS.pop(key, None)
            if pending is None:
                _IN_FLIGHT_EXTRACTIONS.pop(key, None)
            else:
                _IN_FLIGHT_EXTRACTIONS[key] = asyncio.create_task(_extraction_worker(key, pending))


def _schedule_job(key: str, job: _ExtractionJob) -> bool:
    """Start *job* or queue it behind the running one; True when it was queued (coalesced).

    Only one job waits per account, so a newer turn replaces the waiting one and
    that turn's memory extraction is dropped (as before). Its user text is never
    dropped: it is carried into the newer job for the address check. A job that
    only checks addresses never replaces a waiting memory job; its text is
    carried by that job instead.
    """
    existing = _IN_FLIGHT_EXTRACTIONS.get(key)
    if existing is not None and not existing.done():
        waiting = _PENDING_EXTRACTIONS.get(key)
        if waiting is not None:
            if not job.extract_memory:
                job = dataclasses_replace(
                    waiting,
                    carried_user_texts=waiting.carried_user_texts + job.carried_user_texts + (job.user_text,),
                )
            else:
                job = dataclasses_replace(
                    job,
                    carried_user_texts=waiting.carried_user_texts + (waiting.user_text,) + job.carried_user_texts,
                )
        _PENDING_EXTRACTIONS[key] = job
        return True
    _IN_FLIGHT_EXTRACTIONS[key] = asyncio.create_task(_extraction_worker(key, job))
    return False


def schedule_address_check(*, user_id: str, user_text: str, task_id: str = "") -> bool:
    """The address check alone for a turn that ended by cancellation or an error; never awaits.

    The user's words of such a turn are already in the logs (the pipeline line,
    agent-steps, agent_tasks), but the turn never reaches the post-turn step. The
    job starts as its own task on the running loop, so cancelling the turn's task
    cannot stop it and Stop waits for nothing. No memory extraction. The
    background call is accounted like every background call (its own spend
    reservation and usage record), never on the turn's task.
    """
    own_text = str(user_text or "").strip()
    if not own_text:
        return False
    try:
        _schedule_job(
            _extraction_key(user_id),
            _ExtractionJob(
                user_id=user_id,
                transcript="",
                task_id=task_id,
                tools_called=(),
                user_text=own_text,
                extract_memory=False,
            ),
        )
    except (RuntimeError, ValueError) as exc:  # no running loop, or no account
        logger.debug("Address check for an ended turn not started: %s", type(exc).__name__)
        return False
    return True


async def extract_memories_after_turn(
    *,
    user_id: str,
    transcript: str = "",
    task_id: str,
    tools_called: Sequence[str] = (),
    model_visible_messages: Sequence[Mapping[str, Any]] | None = None,
    user_text: str = "",
) -> dict[str, Any]:
    """Schedule the hidden post-turn pass: memory extraction, and the address check of *user_text*.

    *user_text* is what the user wrote or said in this turn. When it is given,
    the turn is checked for addresses also when memory extraction is skipped.
    """
    clean_transcript = _coerce_extraction_transcript(
        transcript=transcript,
        model_visible_messages=model_visible_messages,
    )
    if not clean_transcript:
        return {"ok": True, "skipped": "empty_transcript", "written": 0}
    own_text = str(user_text or "").strip()
    if turn_used_memory_write(tools_called):
        if own_text:
            # Memory extraction is skipped; the address check still runs.
            _schedule_job(
                _extraction_key(user_id),
                _ExtractionJob(
                    user_id=user_id,
                    transcript="",
                    task_id=task_id,
                    tools_called=tuple(str(tool) for tool in tools_called),
                    user_text=own_text,
                    extract_memory=False,
                ),
            )
        return {"ok": True, "skipped": "memory_tool_used", "written": 0}

    model_messages = tuple(copy.deepcopy(list(model_visible_messages))) if model_visible_messages is not None else None
    job = _ExtractionJob(
        user_id=user_id,
        transcript=clean_transcript,
        task_id=task_id,
        tools_called=tuple(str(tool) for tool in tools_called),
        model_visible_messages=model_messages,
        user_text=own_text,
    )
    key = _extraction_key(user_id)
    if _schedule_job(key, job):
        return {"ok": True, "scheduled": True, "coalesced": True, "written": 0}
    return {"ok": True, "scheduled": True, "coalesced": False, "written": 0}


async def drain_pending_extractions(*, timeout_s: float = TIMEOUT_MINUTE) -> dict[str, Any]:
    tasks = [task for task in _IN_FLIGHT_EXTRACTIONS.values() if not task.done()]
    if not tasks:
        return {"ok": True, "drained": 0}
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout_s)
    except TimeoutError:
        return {"ok": False, "error": "timeout", "drained": 0}
    return {"ok": True, "drained": len(tasks)}


__all__ = [
    "ExtractMemoriesService",
    "ExtractedMemory",
    "ReportedAddress",
    "current_turn_transcript",
    "drain_pending_extractions",
    "extract_memories_after_turn",
    "model_visible_messages_to_transcript",
    "sanitize_memory_candidate_text",
    "schedule_address_check",
    "turn_used_memory_write",
]
