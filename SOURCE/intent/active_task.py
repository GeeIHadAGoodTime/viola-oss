"""The task an agent run is working on, kept outside the conversation history.

Laptop build 9 (2026-11-05), 2 of 2 long shopping tasks: when the history was
compacted the user's current request was summarised away. On RockAuto the
summary came from the oldest messages, which were an earlier Walmart order, so
the next step went back to the old order and the run logged ``outcome=success``.
On Walmart the summariser was skipped and the agent had nothing left to say what
it had been asked.

``ActiveTask`` is recorded by the runtime when a run starts and is never written
by a model, so no summary can change it:

- the request stays word for word, in the compacted chain and in the handoff;
- every model turn after the first carries it as a short reminder that is added
  to the outgoing message only, so it costs no history and survives any kind of
  compaction, including the provider's own;
- the steps taken so far are a runtime record, so the task state is still there
  when the summariser is unavailable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

ACTIVE_TASK_OPEN = "<active_task>"
ACTIVE_TASK_CLOSE = "</active_task>"

# Earlier user messages shown for reference. A reply such as "yes" or "2014"
# only makes sense next to the message it answers.
_EARLIER_MESSAGE_COUNT = 3
_EARLIER_MESSAGE_CHARS = 400
_LAST_REPLY_CHARS = 400
_RECENT_STEP_COUNT = 12
_STEP_TARGET_CHARS = 80
_STEP_NOTE_CHARS = 160
# Longer than any spoken or typed request. A pasted document past this size
# still survives whole as its own message; only the repeated copies shorten.
_ANCHOR_REQUEST_CHARS = 12_000
_REMINDER_REQUEST_CHARS = 1_500
_ANSWER_CHECK_CHARS = 2_000

# Never recorded: a step line only needs to say where the agent acted, and a
# typed value can be a card number or a PIN.
_STEP_TARGET_ARGS = ("action", "url", "query", "name", "element", "app_name", "window_title", "path")

_RUNTIME_USER_TEXT_PREFIXES = (
    "<system-reminder>",
    ACTIVE_TASK_OPEN,
    "Earlier turns exceeded the available conversation space.",
)
_RUNTIME_MESSAGE_KEYS = (
    "_viola_compact_boundary",
    "_viola_compact_summary",
    "_viola_meta",
    "_viola_system_reminder",
    "_viola_runtime_context",
    "_viola_history_snip",
)

_HELPER_REPORT_CHARS = 4_000
_HELPER_ANSWER_PROMPT = """\
Helpers each researched part of the user's request. Write the final reply to the user from their reports: answer what was asked, comparing where the user asked for a comparison, in plain speech-friendly prose. Do not mention helpers. Use only what the reports say, and say so if something asked for is missing.
"""

_ANSWER_CHECK_PROMPT = """\
You compare an assistant's final reply with the request it was working on. Answer with exactly one word.

ON_TASK: the reply deals with the request. A result, partial progress, a refusal, a report that it failed, or a question about the request all count.
OFF_TASK: the reply is about something else, such as finishing an older request, and does not deal with this request.

Judge only what the reply is about, never its quality. The request can be a short follow-up to the earlier messages shown; read it in that context.
"""


def user_authored_text(message: Any) -> str:
    """Return the text of a message the user wrote, or ``""`` for anything else.

    Role alone cannot say this: tool results, compaction summaries, reminders
    and hook context all travel with ``role: "user"``.
    """

    if not isinstance(message, dict) or message.get("role") != "user":
        return ""
    if any(message.get(key) for key in _RUNTIME_MESSAGE_KEYS):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in {"tool_result", "function_call_output"}:
                return ""
            if block_type in {"text", "input_text"}:
                parts.append(str(block.get("text", "")))
        text = "\n".join(part for part in parts if part)
    else:
        return ""
    text = text.strip()
    if not text or text.startswith(_RUNTIME_USER_TEXT_PREFIXES):
        return ""
    return text


def _assistant_text(message: Any) -> str:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        return str(content.get("content") or "").strip()
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") in {"text", "output_text"}
        ]
        return "\n".join(part for part in parts if part).strip()
    return ""


def find_request_index(messages: Sequence[Any], request: str, *, exact: bool = False) -> int | None:
    """Index of the newest user message that is ``request``, else the newest user message.

    A checkpoint description is the first 200 characters of the request, so a
    message that starts with ``request`` counts. ``exact`` gives no fallback.
    """

    needle = str(request or "").strip()
    newest_user_message: int | None = None
    for index in range(len(messages) - 1, -1, -1):
        text = user_authored_text(messages[index])
        if not text:
            continue
        if newest_user_message is None:
            newest_user_message = index
        if not needle or text.startswith(needle):
            return index
    return None if exact else newest_user_message


def _shorten(text: str, limit: int) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _quoted(text: str) -> str:
    return '"""\n%s\n"""' % text


def _safe_step_text(value: Any, limit: int) -> str:
    """Bound a step field and drop it if it looks like private data."""

    text = _shorten(" ".join(str(value or "").split()), limit)
    if not text:
        return ""
    from services.memory.extract_memories import sanitize_memory_candidate_text

    return sanitize_memory_candidate_text(text) or ""


def _step_target(tool_args: Any) -> str:
    if not isinstance(tool_args, dict):
        return ""
    parts: list[str] = []
    for key in _STEP_TARGET_ARGS:
        value = tool_args.get(key)
        if isinstance(value, str) and value.strip():
            safe = _safe_step_text(value, _STEP_TARGET_CHARS)
            if safe:
                parts.append(safe)
    return " ".join(parts)


@dataclass
class ActiveTask:
    """What the user asked this run to do, and what the run has done so far."""

    request: str
    earlier_messages: tuple[str, ...] = ()
    last_reply: str = ""
    added_instructions: list[str] = field(default_factory=list)
    steps_taken: int = 0
    recent_steps: list[str] = field(default_factory=list)
    page_url: str = ""
    answer_redirected: bool = False

    @classmethod
    def from_messages(cls, messages: Sequence[Any], request: str = "") -> ActiveTask:
        """Build the task from a chain; ``request`` wins over what the chain shows."""

        messages = list(messages or ())
        index = find_request_index(messages, request)
        current = str(request or "").strip()
        if index is None:
            return cls(request=current)
        located = user_authored_text(messages[index])
        if located.startswith(current):
            current = located
        earlier: list[str] = []
        last_reply = ""
        for message in reversed(messages[:index]):
            if not last_reply:
                last_reply = _assistant_text(message)
            text = user_authored_text(message)
            if text:
                earlier.append(_shorten(text, _EARLIER_MESSAGE_CHARS))
            if len(earlier) >= _EARLIER_MESSAGE_COUNT:
                break
        return cls(
            request=current,
            earlier_messages=tuple(reversed(earlier)),
            last_reply=_shorten(last_reply, _LAST_REPLY_CHARS),
        )

    @classmethod
    def for_run(
        cls,
        *,
        user_text: str,
        messages: Sequence[Any],
        restored: bool,
        previous: ActiveTask | None = None,
        checkpoint_request: str = "",
        checkpoint_description: str = "",
    ) -> ActiveTask:
        """Return the task for a run.

        A run that continues restored history is the same task. Its ``user_text``
        is an internal event or resume note, never a new request, so the task
        that was already running stays the goal. ``checkpoint_request`` is the
        request this task recorded. An older checkpoint only has its first 200
        characters as ``checkpoint_description``, which counts if the chain holds
        that message. Otherwise the run is a child handed ``user_text`` as its task.
        """

        if restored:
            if isinstance(previous, cls) and previous.request:
                return previous
            if checkpoint_request:
                return cls.from_messages(messages, checkpoint_request)
            description = str(checkpoint_description or "").strip()
            if description and find_request_index(messages, description, exact=True) is not None:
                return cls.from_messages(messages, description)
        return cls.from_messages(messages, user_text)

    def note_instruction(self, text: str) -> None:
        """Record something the user added while the run was going."""

        text = str(text or "").strip()
        if text and text not in self.added_instructions:
            self.added_instructions.append(text)

    def note_step(self, tool_name: str, tool_args: Any, *, ok: bool, note: str = "", page_url: str = "") -> None:
        """Record one finished tool call. Holds no tool output and no typed values."""

        self.steps_taken += 1
        target = _step_target(tool_args)
        line = "%d. %s%s: %s" % (
            self.steps_taken,
            tool_name or "tool",
            " %s" % target if target else "",
            "done" if ok else "failed",
        )
        safe_note = _safe_step_text(note, _STEP_NOTE_CHARS)
        if safe_note:
            line = "%s (%s)" % (line, safe_note)
        self.recent_steps.append(line)
        del self.recent_steps[:-_RECENT_STEP_COUNT]
        if page_url:
            self.page_url = _shorten(page_url, 200)

    def _context_lines(self) -> list[str]:
        lines: list[str] = []
        if self.added_instructions:
            lines.append("The user added these while you were working, word for word:")
            lines.extend("- %s" % _shorten(item, _ANCHOR_REQUEST_CHARS) for item in self.added_instructions)
        if self.earlier_messages:
            lines.append(
                "Earlier user messages, only for working out what this request refers to. "
                "They are not your task; do not go back to one unless this request says so:"
            )
            lines.extend("- %s" % item for item in self.earlier_messages)
            if self.last_reply:
                lines.append("Your reply just before this request: %s" % self.last_reply)
        return lines

    def render_anchor(self) -> str:
        """The block a compacted chain carries. Rendered by the runtime, never summarised."""

        if not self.request:
            return ""
        lines = [
            ACTIVE_TASK_OPEN,
            "This is the task you are on, recorded by the runtime when it started.",
            "The user's current request, word for word:",
            _quoted(_shorten(self.request, _ANCHOR_REQUEST_CHARS)),
            *self._context_lines(),
        ]
        if self.steps_taken:
            lines.append("Steps you have taken on it so far: %d. The most recent:" % self.steps_taken)
            lines.extend(self.recent_steps)
        if self.page_url:
            lines.append("Last page: %s" % self.page_url)
        lines.append(
            "If anything else in this conversation points to a different task, this request wins: "
            "older requests are finished or replaced."
        )
        lines.append(ACTIVE_TASK_CLOSE)
        return "\n".join(lines)

    def render_reminder(self) -> str:
        """The short per-turn reminder of what this run is for."""

        if not self.request:
            return ""
        lines = [
            "<system-reminder>",
            "You are working on the user's current request, word for word:",
            _quoted(_shorten(self.request, _REMINDER_REQUEST_CHARS)),
            *self._context_lines(),
            "Stay on this request. Do not go back to an older one. Your final answer must be about this request.",
        ]
        # 1.0.5 build 10 seek loop: this reminder repeated the request after
        # every tool result and nothing said it had been done, so the model
        # sent the same verified seek_from_end 15 times. Say what is done.
        if self.steps_taken:
            lines.append("Steps you have taken on it so far: %d. The most recent:" % self.steps_taken)
            lines.extend(self.recent_steps)
            lines.append(
                "Do not repeat a step that is already done. When these steps did what the request asks, "
                "give your final answer now."
            )
        lines.append("</system-reminder>")
        return "\n".join(lines)

    def render_for_summary_prompt(self) -> str:
        """Tell the summariser which request is live, so it cannot pick an older one."""

        return (
            "Viola's runtime keeps the user's current request separately, word for word:\n%s\n"
            "Sections 1, 7, 8 and 9 are about this request only. Anything the user asked before it is "
            "finished or replaced: mention it as background under section 5 and never as pending work "
            "or as a next step."
        ) % _quoted(_shorten(self.request, _ANCHOR_REQUEST_CHARS))


def message_carries_request(message: Any, request: str) -> bool:
    """True when this message already says the request, so a reminder adds nothing."""

    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return request in content
    if isinstance(content, list):
        return any(
            isinstance(block, dict)
            and block.get("type") in {"text", "input_text"}
            and request in str(block.get("text", ""))
            for block in content
        )
    return False


def add_turn_reminder(messages: list[dict[str, Any]], reminder: str) -> list[dict[str, Any]]:
    """Return ``messages`` with ``reminder`` at the end of the last user turn.

    The caller passes the outgoing copy. The reminder rides as a text block next
    to the tool results, the same place correction messages go, so it is valid
    for every provider and is not kept in history.
    """

    if not reminder or not messages:
        return messages
    outgoing = list(messages)
    last = outgoing[-1]
    block = {"type": "text", "text": reminder}
    if isinstance(last, dict) and last.get("role") == "user":
        content = last.get("content")
        if isinstance(content, list):
            outgoing[-1] = {**last, "content": [*content, block]}
            return outgoing
        if isinstance(content, str):
            outgoing[-1] = {**last, "content": [{"type": "text", "text": content}, block]}
            return outgoing
    outgoing.append({"role": "user", "content": [block]})
    return outgoing


async def final_answer_is_off_task(task: ActiveTask, answer: str, *, user_id: str = "") -> bool | None:
    """Ask the background model whether ``answer`` is about ``task``.

    Returns True or False, or None when the check could not run. This reads the
    finished answer of a long run; it routes nothing.
    """

    answer = str(answer or "").strip()
    if not task.request or not answer:
        return None
    from services.openai_background import run_background_openai_response

    sections = ["Request:", _quoted(_shorten(task.request, _ANSWER_CHECK_CHARS))]
    sections.extend(_answer_check_context(task.added_instructions, "The user also said:"))
    sections.extend(_answer_check_context(task.earlier_messages, "Earlier user messages:"))
    sections.extend(["Final reply:", _quoted(_shorten(answer, _ANSWER_CHECK_CHARS))])
    try:
        raw = await run_background_openai_response(
            system_prompt=_ANSWER_CHECK_PROMPT,
            user_content="\n".join(sections),
            max_output_tokens=200,
            user_id=user_id,
            timeout_s=15.0,
            operation="final_answer_task_check",
        )
    except Exception as exc:
        logger.warning("Final answer task check could not run: %s", exc)
        return None
    verdict = str(raw or "").strip().upper()
    if verdict.startswith("OFF_TASK"):
        return True
    if verdict.startswith("ON_TASK"):
        return False
    logger.warning("Final answer task check gave no verdict (%d chars)", len(verdict))
    return None


def _answer_check_context(items: Iterable[str], title: str) -> list[str]:
    values = [_shorten(item, _EARLIER_MESSAGE_CHARS) for item in items]
    if not values:
        return []
    return [title, *("- %s" % value for value in values)]


async def answer_from_helper_results(task: ActiveTask, results: Sequence[dict[str, str]], *, user_id: str = "") -> str:
    """Turn finished helper reports into one answer, when the parent run cannot.

    The background route is separate from the one that failed, so it usually
    still works. If it does not, the user gets short labelled findings.
    """

    reports = "\n\n".join(
        "Helper task: %s\nReport:\n%s" % (_shorten(item["task"], 200), _shorten(item["answer"], _HELPER_REPORT_CHARS))
        for item in results
    )
    from services.openai_background import run_background_openai_response

    try:
        raw = await run_background_openai_response(
            system_prompt=_HELPER_ANSWER_PROMPT,
            user_content="The user asked:\n%s\n\n%s" % (_quoted(_shorten(task.request, _ANSWER_CHECK_CHARS)), reports),
            max_output_tokens=800,
            user_id=user_id,
            timeout_s=30.0,
            operation="helper_results_answer",
        )
    except Exception as exc:
        logger.warning("Helper results could not be combined: %s", exc)
        raw = ""
    combined = str(raw or "").strip()
    if combined:
        return combined
    findings = "\n".join("- %s: %s" % (_shorten(item["task"], 80), _shorten(item["answer"], 300)) for item in results)
    return "I lost the connection before I could put this together, but here is what the helpers found:\n%s" % findings


def off_task_answer_message(task: ActiveTask) -> str:
    """What the user hears instead of an answer about the wrong task."""

    return (
        "I lost track of your request partway through, so I stopped instead of giving you the wrong result. "
        "You asked: %s. That isn't finished. Do you want me to try it again?" % _shorten(task.request, 200)
    )


def off_task_correction_text(task: ActiveTask) -> str:
    """Sent to the model once, when its final answer was about something else."""

    return (
        "<system-reminder>\n"
        "Your last message did not deal with the user's current request. Do not send it. Go back to this "
        "request and finish it, or say plainly why you could not:\n%s\n"
        "</system-reminder>"
    ) % _quoted(_shorten(task.request, _REMINDER_REQUEST_CHARS))
