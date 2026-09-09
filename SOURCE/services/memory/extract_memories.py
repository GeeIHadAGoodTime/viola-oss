"""Durable post-turn memory extraction."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import settings
from core.constants import TIMEOUT_MINUTE
from core.logging_config import get_logger
from core.secrets_mask import mask_secrets_in_text, scan_secrets_in_text
from services.memory.dir import MemoryDir, get_memory_dir
from services.memory.store import (
    SENSITIVE_CONTENT_RE,
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


def _coerce_extraction_transcript(
    *,
    transcript: str,
    model_visible_messages: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    if model_visible_messages is not None:
        history = model_visible_messages_to_transcript(model_visible_messages).strip()
        if history:
            return history
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
    redacted = redact_pii_output(clean).strip()
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


def _parse_extractor_response(text: str) -> list[ExtractedMemory]:
    clean = (text or "").strip()
    if clean.startswith("```"):
        parts = clean.split("```")
        clean = parts[1] if len(parts) > 1 else clean
        if clean.lstrip().startswith("json"):
            clean = clean.lstrip()[4:]
    payload = json.loads(clean)
    if not isinstance(payload, list):
        return []
    parsed: list[ExtractedMemory] = []
    for item in payload:
        normalized = _normalize_extracted_memory(item)
        if normalized is not None:
            parsed.append(normalized)
    return parsed


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


async def _default_extractor(transcript: str, *, user_id: str) -> list[ExtractedMemory]:
    if not settings.auto_memory_extraction_enabled or not settings.openai_api_key:
        return []
    from services.openai_background import run_background_openai_response

    capped_transcript = (transcript or "")[:_MAX_EXTRACTION_TRANSCRIPT_CHARS]
    # Redaction runs the PII / sensitive-prompt regexes, which historically
    # backtracked super-linearly on pathological input (2026-07-05 prod
    # event-loop wedge). Run it off the event loop so a bad transcript can never
    # freeze the API again; the cap above already bounds the work handed to it.
    safe_transcript = await asyncio.to_thread(redact_sensitive_prompt_text, capped_transcript)
    response = await run_background_openai_response(
        system_prompt=(
            "Extract durable user memories from a completed Viola turn. "
            "Return only JSON. Use categories preference, fact, correction, routine, context, or note."
        ),
        user_content=(
            "Conversation transcript:\n%s\n\n"
            "Return a JSON array of objects like "
            '[{"content":"User prefers jazz music","category":"preference"}]. '
            "Only include stable user facts, preferences, corrections, routines, or project context. "
            "Exclude secrets, payment data, one-off task details, greetings, and obvious filler."
        )
        % safe_transcript,
        max_output_tokens=500,
        user_id=user_id,
        timeout_s=15.0,
    )
    return _parse_extractor_response(response)


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
        cursor = self._read_cursor()
        if cursor.get("last_hash") == digest:
            return {"ok": True, "skipped": "already_processed", "written": 0}

        try:
            extracted = await self._extract(clean_transcript)
        except _background_extraction_error_types() as exc:
            logger.debug("Post-turn memory extraction failed: %s", exc)
            return {"ok": False, "error": str(exc), "written": 0}

        written = 0
        for memory in extracted:
            try:
                result = self.memory_dir.store_semantic_memory(memory.content, category=memory.category)
            except (OSError, ValueError) as exc:
                logger.debug("Extracted memory write skipped: %s", exc)
                continue
            if not result.get("deduped"):
                written += 1

        self._write_cursor({"last_hash": digest, "last_task_id": task_id, "written": written})
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
    try:
        await asyncio.wait_for(
            ExtractMemoriesService(user_id=job.user_id).extract_after_turn(
                transcript=job.transcript,
                task_id=job.task_id,
                tools_called=job.tools_called,
                model_visible_messages=job.model_visible_messages,
            ),
            timeout=TIMEOUT_MINUTE,
        )
    except _background_extraction_error_types() as exc:
        logger.debug("Post-turn memory extraction job failed: %s", exc)


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


async def extract_memories_after_turn(
    *,
    user_id: str,
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

    model_messages = tuple(copy.deepcopy(list(model_visible_messages))) if model_visible_messages is not None else None
    job = _ExtractionJob(
        user_id=user_id,
        transcript=clean_transcript,
        task_id=task_id,
        tools_called=tuple(str(tool) for tool in tools_called),
        model_visible_messages=model_messages,
    )
    key = _extraction_key(user_id)
    existing = _IN_FLIGHT_EXTRACTIONS.get(key)
    if existing is not None and not existing.done():
        _PENDING_EXTRACTIONS[key] = job
        return {"ok": True, "scheduled": True, "coalesced": True, "written": 0}
    _IN_FLIGHT_EXTRACTIONS[key] = asyncio.create_task(_extraction_worker(key, job))
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
    "drain_pending_extractions",
    "extract_memories_after_turn",
    "model_visible_messages_to_transcript",
    "sanitize_memory_candidate_text",
    "turn_used_memory_write",
]
