"""Final-answer streaming: what a provider reports while it generates, and what may be spoken.

A streaming provider describes each model call (an "attempt") on the command
stream (``services.llm.stream_bus``) with ``{"answer": {...}}`` events:

- ``{"op": "start", "attempt": id}``
- ``{"op": "item", "attempt": id, "item": item_id, "kind": "message" | "function_call", "phase": phase}``
- ``{"op": "text", "attempt": id, "item": item_id, "text": delta}``
- ``{"op": "end", "attempt": id, "status": "completed" | "incomplete" | "failed", "tool_calls": bool}``

The same events cross the managed cloud forward unchanged (the cloud route
relays them, the desktop republishes them), so every provider path feeds one
speech decision: :class:`FinalAnswerSpeechGate`.

What may be spoken while the model is still writing (design, 2026-10-31):

- Only text that belongs to the final answer. A model response can carry text
  AND a function call; the agent loop never shows or speaks that leading text
  (it is a preamble). On the Responses API a message item says which it is:
  ``phase == "final_answer"`` streams live, ``"commentary"`` is held. Text with
  no phase (other providers) is held too. Held text is spoken only when its
  model call ends with no function call (then it was the answer: the
  "commentary-only" and "no phase" shapes), and dropped otherwise.
- Sentence by sentence. A sentence is released only when it is complete and
  the answer so far still passes every check that could later change the
  answer: JSON/template clean-up, the sensitive-data block, the self-diagnosis
  override (any "error" wording), and the spoken-length cap. The first sentence
  that would trip one of them stops live speech for the rest of the turn; the
  final answer, after every guard ran, decides what is still said.
- Never twice. :meth:`FinalAnswerSpeechGate.finish` compares what was spoken
  with the final answer and returns only the part not yet said. When a model
  call is abandoned (stream failure, truncation retry) its spoken sentences are
  skipped if the next call repeats them; if it says something else, live speech
  stops and the final answer's unspoken part is used.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from core.logging_config import get_logger
from services.llm.stream_bus import get_current_command_stream_id, publish_stream_event

logger = get_logger(__name__)

ANSWER_EVENT_KEY = "answer"
FINAL_ANSWER_PHASE = "final_answer"

# Mirrors voice/synthesis/text_normalizer.py: a spoken reply longer than three
# sentences or 500 characters is cut to three sentences (at most 360
# characters) plus this suffix. Live speech keeps the same budget.
MAX_LIVE_SENTENCES = 3
MAX_LIVE_CHARS = 360
MAX_SPOKEN_CHARS = 500
SUMMARY_SUFFIX = "I'll send the full details in chat."

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])[\"')\]]*\s+|\n+")
_WORD_RE = re.compile(r"[a-z0-9']+")


# --------------------------------------------------------------------------- #
# Provider side                                                               #
# --------------------------------------------------------------------------- #


class AnswerAttempt:
    """Publishes one model call's answer events to a command stream."""

    def __init__(self, stream_id: str) -> None:
        self.stream_id = stream_id
        self.attempt_id = uuid.uuid4().hex[:16]
        self.tool_calls = False
        self.ended = False

    @classmethod
    def begin(cls, stream_id: str | None = None) -> AnswerAttempt | None:
        """Start an attempt on ``stream_id`` (default: the current command stream)."""
        sid = stream_id or get_current_command_stream_id()
        if not sid:
            return None
        attempt = cls(sid)
        attempt._publish({"op": "start"})
        return attempt

    def item(self, item_id: str | None, *, kind: str, phase: str | None = None) -> None:
        if kind == "function_call":
            self.tool_calls = True
        self._publish({"op": "item", "item": str(item_id or ""), "kind": kind, "phase": phase})

    def text(self, item_id: str | None, text: str) -> None:
        if text:
            self._publish({"op": "text", "item": str(item_id or ""), "text": text})

    def end(self, *, status: str = "completed", tool_calls: bool | None = None) -> None:
        if self.ended:
            return
        self.ended = True
        if tool_calls:
            self.tool_calls = True
        self._publish({"op": "end", "status": status, "tool_calls": self.tool_calls})

    def _publish(self, payload: dict[str, Any]) -> None:
        event = {ANSWER_EVENT_KEY: {**payload, "attempt": self.attempt_id}}
        try:
            publish_stream_event(self.stream_id, event)
        except Exception:  # noqa: BLE001, RUF100 - answer events are best effort
            logger.debug("Answer event publish failed", exc_info=True)


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def observe_openai_responses_event(attempt: AnswerAttempt | None, event: Any) -> None:
    """Map one OpenAI Responses stream event onto ``attempt``."""
    if attempt is None:
        return
    event_type = str(_field(event, "type", "") or "")
    if event_type == "response.output_item.added":
        item = _field(event, "item")
        item_type = str(_field(item, "type", "") or "")
        if item_type == "message":
            phase = _field(item, "phase")
            attempt.item(_field(item, "id"), kind="message", phase=str(phase) if phase else None)
        elif item_type in {"function_call", "custom_tool_call"}:
            attempt.item(_field(item, "id"), kind="function_call")
    elif event_type == "response.output_text.delta":
        attempt.text(_field(event, "item_id"), str(_field(event, "delta", "") or ""))


# --------------------------------------------------------------------------- #
# Speech side                                                                 #
# --------------------------------------------------------------------------- #


def split_sentences(text: str) -> tuple[list[str], str]:
    """Split ``text`` into complete sentences and the unfinished rest."""
    parts = _SENTENCE_SPLIT_RE.split(text or "")
    if len(parts) <= 1:
        return [], text or ""
    done = [p.strip() for p in parts[:-1] if p and p.strip()]
    return done, parts[-1]


def all_sentences(text: str) -> list[str]:
    done, rest = split_sentences(text)
    if rest.strip():
        done.append(rest.strip())
    return done


def _words(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower().replace("’", "'"))


def same_sentence(a: str, b: str) -> bool:
    return _words(a) == _words(b)


def hold_reason(text_so_far: str, sentence: str) -> str | None:
    """Why ``sentence`` (the newest part of ``text_so_far``) must not be spoken yet.

    Each check mirrors a guard that can still change the final answer after
    the model finished. ``None`` means nothing would change it.
    """
    stripped = text_so_far.lstrip()
    if stripped[:1] in {"{", "[", "`"}:
        return "structured_text"
    try:
        from intent.response_cleanup import strip_json_template

        if strip_json_template(text_so_far).strip() != text_so_far.strip():
            return "template_cleanup"
    except Exception:  # noqa: BLE001, RUF100 - unknown cleanup result means hold
        return "template_cleanup"
    try:
        from intent import response_quality_gate

        pattern = getattr(response_quality_gate, "_SENSITIVE_SOLICITATION_RE", None)
        if pattern is not None and pattern.search(text_so_far):
            return "sensitive_data_block"
    except Exception:  # noqa: BLE001, RUF100 - unknown safety result means hold
        return "sensitive_data_block"
    # intent/ai_controller.py replaces a failed turn's answer that mentions an
    # "error" with the self-diagnosis explanation.
    if "error" in sentence.lower():
        return "error_wording"
    return None


@dataclass
class _Item:
    phase: str | None = None
    kind: str = "message"
    text: str = ""
    pending: str = ""


@dataclass
class _Attempt:
    items: dict[str, _Item] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    tool_calls: bool = False
    spoke_final: bool = False
    ended: bool = False


class FinalAnswerSpeechGate:
    """Turns answer events into sentences that are safe to speak now.

    Not thread-safe; feed it from one thread (the speech sink hops to its loop).
    """

    def __init__(self, *, live: bool = True, reason: str | None = None) -> None:
        self._attempts: dict[str, _Attempt] = {}
        self._spoken: list[str] = []
        self._replay: list[str] = []
        self._live = live
        self.stop_reason: str | None = None if live else (reason or "disabled")
        self._final_text_so_far = ""
        self._closed = False

    # -- state ------------------------------------------------------------
    @property
    def spoken(self) -> list[str]:
        return list(self._spoken)

    @property
    def spoken_text(self) -> str:
        return " ".join(self._spoken)

    @property
    def live(self) -> bool:
        return self._live and not self._closed

    @property
    def heard_text(self) -> str:
        """Final-answer text received so far (spoken or about to be)."""
        return self._final_text_so_far

    def stop_live(self, reason: str) -> None:
        if self._live:
            self._live = False
            self.stop_reason = reason
            logger.info("Streamed speech stopped early (%s); the final answer decides the rest", reason)

    # -- events -----------------------------------------------------------
    def feed(self, event: dict[str, Any] | None) -> list[str]:
        """Consume one stream-bus event; return sentences to speak now."""
        if self._closed or not isinstance(event, dict):
            return []
        payload = event.get(ANSWER_EVENT_KEY)
        if not isinstance(payload, dict):
            return []
        op = payload.get("op")
        attempt_id = str(payload.get("attempt") or "")
        if op == "start":
            for other_id, other in self._attempts.items():
                if not other.ended and other_id != attempt_id:
                    self._abandon(other)
            self._attempts[attempt_id] = _Attempt()
            return []
        attempt = self._attempts.get(attempt_id)
        if attempt is None or attempt.ended:
            return []
        if op == "item":
            item_id = str(payload.get("item") or "")
            kind = str(payload.get("kind") or "message")
            if kind == "function_call":
                attempt.tool_calls = True
                if attempt.spoke_final:
                    logger.info("A function call followed final-answer text in one model call")
            if item_id not in attempt.items:
                attempt.order.append(item_id)
            attempt.items[item_id] = _Item(phase=payload.get("phase"), kind=kind)
            return []
        if op == "text":
            item_id = str(payload.get("item") or "")
            item = attempt.items.get(item_id)
            if item is None:
                item = _Item(phase=None)
                attempt.items[item_id] = item
                attempt.order.append(item_id)
            text = str(payload.get("text") or "")
            item.text += text
            if item.phase == FINAL_ANSWER_PHASE and not attempt.tool_calls:
                attempt.spoke_final = True
                return self._take(item, text, final=False)
            return []
        if op == "end":
            attempt.ended = True
            if str(payload.get("tool_calls")).lower() == "true" or payload.get("tool_calls") is True:
                attempt.tool_calls = True
            status = str(payload.get("status") or "completed")
            if status != "completed":
                self._abandon(attempt)
                return []
            if attempt.tool_calls:
                return []
            out: list[str] = []
            if attempt.spoke_final:
                for item_id in attempt.order:
                    item = attempt.items[item_id]
                    if item.phase == FINAL_ANSWER_PHASE:
                        out.extend(self._take(item, "", final=True))
                return out
            # No final-answer text: the held text (commentary-only or no
            # phase) was the answer.
            for item_id in attempt.order:
                item = attempt.items[item_id]
                if item.kind == "message" and item.text:
                    out.extend(self._take(item, item.text, final=True))
            return out
        return []

    def _abandon(self, attempt: _Attempt) -> None:
        attempt.ended = True
        if attempt.spoke_final and self._spoken:
            # The next model call may say the same thing again: skip it then.
            self._replay = list(self._spoken)
            self._final_text_so_far = self.spoken_text

    def _take(self, item: _Item, text: str, *, final: bool) -> list[str]:
        item.pending += text
        sentences, rest = split_sentences(item.pending)
        if final and rest.strip():
            sentences.append(rest.strip())
            rest = ""
        item.pending = rest
        out: list[str] = []
        for sentence in sentences:
            if self._offer(sentence):
                out.append(sentence)
        return out

    def _offer(self, sentence: str) -> bool:
        if self._replay:
            expected = self._replay[0]
            if same_sentence(sentence, expected):
                self._replay.pop(0)
                return False
            self._replay = []
            self.stop_live("diverged_after_retry")
            return False
        if not self._live:
            return False
        candidate = (self._final_text_so_far + " " + sentence).strip()
        reason = hold_reason(candidate, sentence)
        if reason is not None:
            self.stop_live(reason)
            return False
        spoken_chars = sum(len(s) + 1 for s in self._spoken)
        if len(self._spoken) >= MAX_LIVE_SENTENCES or spoken_chars + len(sentence) > MAX_LIVE_CHARS:
            self.stop_live("long_answer")
            return False
        self._final_text_so_far = candidate
        self._spoken.append(sentence)
        return True

    # -- end of turn ------------------------------------------------------
    def finish(self, final_message: str | None, *, already_spoken: bool = False) -> str:
        """Close the gate; return the part of ``final_message`` still to be said.

        With nothing spoken live this is ``final_message`` itself (the caller
        speaks it as before). Otherwise it is what the final answer says after
        the sentences already spoken, under the same length cap a whole reply
        gets, and never a sentence twice.
        """
        self._closed = True
        final = (final_message or "").strip()
        if already_spoken:
            return ""
        if not self._spoken:
            return final
        final_sentences = all_sentences(final)
        matched = 0
        while (
            matched < len(self._spoken)
            and matched < len(final_sentences)
            and same_sentence(self._spoken[matched], final_sentences[matched])
        ):
            matched += 1
        if matched < len(self._spoken):
            logger.warning(
                "Streamed speech differs from the final answer after %d of %d spoken sentence(s); "
                "saying only the final answer's unspoken part",
                matched,
                len(self._spoken),
            )
        rest = final_sentences[matched:]
        oversized = len(final_sentences) > MAX_LIVE_SENTENCES or len(final) > MAX_SPOKEN_CHARS
        if not oversized:
            return " ".join(rest)
        budget = max(0, MAX_LIVE_SENTENCES - len(self._spoken))
        kept = rest[:budget]
        return " ".join([*kept, SUMMARY_SUFFIX]) if kept or rest else ""


__all__ = [
    "ANSWER_EVENT_KEY",
    "AnswerAttempt",
    "FinalAnswerSpeechGate",
    "all_sentences",
    "hold_reason",
    "observe_openai_responses_event",
    "same_sentence",
    "split_sentences",
]
