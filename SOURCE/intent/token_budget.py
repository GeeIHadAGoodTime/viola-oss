"""Proactive token budget tracking for the agent executor (LA-3).

Estimates cumulative token usage across agent loop iterations and triggers
context compaction BEFORE hitting the model's context window limit.  This
prevents the reactive 400-error path (which wastes a failed API call) and
keeps the agent loop running smoothly for long tasks.

Token counts use ``tiktoken`` so proactive compaction decisions follow the same
tokenizer family as OpenAI-compatible agent calls.  ``tiktoken`` is an
*optional* dependency: it is imported lazily and, when it is missing from the
runtime environment (e.g. a cloud image whose ``requirements-cloud.txt`` did not
yet pin it), this module degrades to a chars/4 heuristic instead of raising at
import time.  That keeps an unrelated tokenizer-dependency gap from hard-crashing
the entire agent/phone runtime path (every cloud phone call failed at setup
2026-06-22 because ``import tiktoken`` was top-level here and the dep was absent
from the cloud image).  The threshold is configurable via
``VIOLA_TOKEN_BUDGET_THRESHOLD`` (default 0.80 = compact at 80% of effective
context window).

The existing 400-error compaction in ``_get_next_response`` remains as a
safety net — this tracker is a *proactive* optimisation, not a replacement.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# ``tiktoken`` is loaded lazily (see ``_load_tiktoken``) so a missing optional
# dependency degrades the token estimate to a chars/4 heuristic instead of
# crashing this module at import time. ``_TIKTOKEN_WARNED`` guards the
# fallback-warning so we log the degradation exactly once per process rather
# than on every count.
_TIKTOKEN_WARNED = False

# Effective context window limits per model family.
# These are intentionally conservative working limits for agent history, not
# the provider-advertised hard maximums. Keeping compaction around ~128k avoids
# long-context quality degradation and runaway latency even for larger-window
# GPT-5/GPT-4.1 families.
_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-5-nano": 128_000,
    "gpt-5-mini": 128_000,
    "gpt-5": 128_000,
    "gpt-5.4-mini": 128_000,
    "gpt-5.4": 128_000,
    "gemini-2.5-flash": 128_000,
    "claude-sonnet-4-20250514": 200_000,
    "claude-opus-4-20250514": 200_000,
}
_DEFAULT_CONTEXT_WINDOW = 128_000
_LOCAL_MODEL_FALLBACK_CONTEXT_WINDOW = 32_768

# Threshold: trigger compaction at this fraction of the context window.
_DEFAULT_THRESHOLD = 0.80
_THRESHOLD = float(os.environ.get("VIOLA_TOKEN_BUDGET_THRESHOLD", str(_DEFAULT_THRESHOLD)))

# Context-window SOURCE override knob (parity with Claude Code TS's
# ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` in autoCompact.ts:getEffectiveContextWindowSize).
# When unset (the default), the resolved window is whatever the model table /
# provider advertise — behaviour is unchanged. When set to a positive integer,
# it overrides the resolved window so the compaction trigger can flip to the
# provider's real advertised window via config without a code change. Read at
# resolution time (not import time) so tests and operators can flip it live.
_CONTEXT_WINDOW_OVERRIDE_ENV = "VIOLA_AGENT_CONTEXT_WINDOW"
_DEFAULT_TOKENIZER_MODEL = "gpt-5.4-mini"
_FALLBACK_ENCODING = "cl100k_base"


def context_window_override() -> int | None:
    """Return the operator-configured context-window override, or None.

    Mirrors Claude Code TS's ``CLAUDE_CODE_AUTO_COMPACT_WINDOW``: a single env
    knob that, when set to a positive integer, overrides the resolved window.
    Invalid / non-positive values are ignored (default behaviour preserved).
    """
    raw = os.environ.get(_CONTEXT_WINDOW_OVERRIDE_ENV)
    if not raw:
        return None
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r is not an integer; ignoring context-window override",
            _CONTEXT_WINDOW_OVERRIDE_ENV,
            raw,
        )
        return None
    if parsed <= 0:
        logger.warning(
            "%s=%r is not positive; ignoring context-window override",
            _CONTEXT_WINDOW_OVERRIDE_ENV,
            raw,
        )
        return None
    return parsed


def apply_context_window_override(resolved: int) -> int:
    """Apply the ``VIOLA_AGENT_CONTEXT_WINDOW`` override to a resolved window.

    Returns the override when set (positive int); otherwise returns *resolved*
    unchanged. This is the single chokepoint every window SOURCE flows through
    (model table, provider-advertised, post-response) so the knob takes effect
    regardless of how the window was resolved.
    """
    override = context_window_override()
    if override is not None:
        return override
    return resolved


def _normalize_model_name(model: str | None) -> str:
    return (model or "").strip().lower()


def _parse_context_window_from_model_name(model: str) -> int | None:
    """Infer explicit context suffixes such as 32k, 128k, or 1m."""
    normalized = _normalize_model_name(model)
    if not normalized:
        return None
    match = re.search(r"(?<![a-z0-9])(?P<count>\d+)(?P<unit>k|m)(?![a-z0-9])", normalized)
    if not match:
        match = re.search(r"(?P<count>\d+)(?P<unit>k|m)(?=[:._-]|$)", normalized)
    if not match:
        return None
    count = int(match.group("count"))
    multiplier = 1_000_000 if match.group("unit") == "m" else 1024
    return max(1024, count * multiplier)


def _looks_like_local_model(model: str) -> bool:
    normalized = _normalize_model_name(model)
    if not normalized:
        return False
    markers = (
        "llama",
        "mistral",
        "mixtral",
        "qwen",
        "phi",
        "gemma",
        "codellama",
        "deepseek",
        "ollama",
    )
    return any(marker in normalized for marker in markers)


def context_window_for_model(model: str | None, *, default: int = _DEFAULT_CONTEXT_WINDOW) -> int:
    """Return the effective working context window for token budgeting."""
    normalized = _normalize_model_name(model)
    if not normalized:
        return default
    if normalized in _MODEL_CONTEXT_WINDOWS:
        return _MODEL_CONTEXT_WINDOWS[normalized]
    explicit = _parse_context_window_from_model_name(normalized)
    if explicit is not None:
        return explicit
    if _looks_like_local_model(normalized):
        return _LOCAL_MODEL_FALLBACK_CONTEXT_WINDOW
    return default


# Characters-per-token divisor for the heuristic used when tiktoken is absent.
# Matches the legacy ``len(text) // 4`` estimate the module shipped before
# tiktoken was introduced; conservative across English + code for GPT families.
_CHARS_PER_TOKEN = 4


@lru_cache(maxsize=1)
def _load_tiktoken() -> Any | None:
    """Import tiktoken lazily; return the module or None if unavailable.

    Cached so the (potentially slow) import + ImportError handling runs once
    per process. Returning None — rather than raising — is what lets the rest
    of this module degrade to a chars/4 heuristic instead of crashing the
    agent/phone runtime path when the optional dependency is missing.
    """
    try:
        # Deliberately lazy (not module-scope) so a missing optional tokenizer
        # degrades to chars/4 instead of crashing the agent/phone import path.
        import tiktoken
    except Exception:  # noqa: BLE001, RUF100 - broken/absent tiktoken raises arbitrary errors; degrade to chars/4
        return None
    return tiktoken


@lru_cache(maxsize=32)
def _encoding_for_model(model: str) -> Any | None:
    """Return a tiktoken encoding for *model*, or None if tiktoken can't produce one.

    Never raises. Viola's own model names (e.g. "gpt-5.4-mini") are unrecognized
    by tiktoken's model table, so ``encoding_for_model`` raises KeyError on every
    call and control always falls through to the ``cl100k_base`` fallback. If the
    runtime's tiktoken install is incomplete -- most concretely a frozen desktop
    build that dropped the `tiktoken_ext` plugin package (#345) -- that fallback
    itself raises ``ValueError: Unknown encoding cl100k_base. Plugins found: []``
    because no plugin registered any encoding. Catching only KeyError here (the
    pre-#345 shape) let that ValueError escape uncaught and crash every agent
    command that reached token counting. Both tiktoken calls are wrapped so any
    failure degrades to the chars/4 heuristic in ``count_text_tokens`` instead.
    """
    tiktoken = _load_tiktoken()
    if tiktoken is None:
        return None
    normalized = model or _DEFAULT_TOKENIZER_MODEL
    try:
        return tiktoken.encoding_for_model(normalized)
    except KeyError:
        pass
    try:
        return tiktoken.get_encoding(_FALLBACK_ENCODING)
    except Exception:  # noqa: BLE001, RUF100 - incomplete tiktoken (e.g. missing tiktoken_ext) degrades, never crashes
        return None


def _heuristic_token_count(text: str) -> int:
    """chars/4 token estimate used when tiktoken is unavailable."""
    return len(text) // _CHARS_PER_TOKEN


def count_text_tokens(text: str, *, model: str = _DEFAULT_TOKENIZER_MODEL) -> int:
    """Count text tokens, preferring tiktoken and degrading to chars/4.

    When tiktoken is installed this returns an accurate token count. When it is
    not (an optional-dependency gap), it falls back to a chars/4 heuristic and
    logs the degradation once per process — it never raises, so a missing
    tokenizer cannot hard-crash the agent or phone runtime path.
    """
    if not text:
        return 0
    encoding = _encoding_for_model(model)
    if encoding is None:
        global _TIKTOKEN_WARNED
        if not _TIKTOKEN_WARNED:
            _TIKTOKEN_WARNED = True
            logger.warning(
                "tiktoken is not installed; token-budget counting is using the "
                "chars/4 heuristic. Token-budget compaction decisions will be "
                "approximate. Install tiktoken for accurate counts."
            )
        return _heuristic_token_count(text)
    return len(encoding.encode(text))


def _estimate_tokens(text: str, *, model: str = _DEFAULT_TOKENIZER_MODEL) -> int:
    """Count token usage for budget decisions, preserving a one-token floor."""
    return max(1, count_text_tokens(text, model=model))


def _estimate_message_tokens(message: dict[str, Any], *, model: str = _DEFAULT_TOKENIZER_MODEL) -> int:
    """Estimate token count for a single message dict.

    Handles both text-based messages (``{"role": ..., "content": str}``)
    and native-format messages (``{"role": ..., "content": list[block]}``)
    including tool_use / tool_result blocks.
    """
    tokens = 4  # per-message overhead (role, separators)
    content = message.get("content", "")

    if isinstance(content, str):
        tokens += _estimate_tokens(content, model=model)
    elif isinstance(content, list):
        # Native format: list of content blocks
        for block in content:
            if not isinstance(block, dict):
                tokens += _estimate_tokens(str(block), model=model)
                continue
            block_type = block.get("type", "")
            if block_type == "text":
                tokens += _estimate_tokens(block.get("text", ""), model=model)
            elif block_type == "tool_use":
                tokens += _estimate_tokens(str(block.get("input", {})), model=model)
                tokens += _estimate_tokens(block.get("name", ""), model=model)
            elif block_type == "tool_result":
                inner = block.get("content", "")
                if isinstance(inner, str):
                    tokens += _estimate_tokens(inner, model=model)
                elif isinstance(inner, list):
                    for sub in inner:
                        if isinstance(sub, dict):
                            tokens += _estimate_tokens(sub.get("text", ""), model=model)
                        else:
                            tokens += _estimate_tokens(str(sub), model=model)
                else:
                    tokens += _estimate_tokens(str(inner), model=model)
            elif block_type == "image":
                # Images are ~1000 tokens for a typical screenshot
                tokens += 1000
            else:
                tokens += _estimate_tokens(str(block), model=model)
    else:
        tokens += _estimate_tokens(str(content), model=model)

    return tokens


class TokenBudgetTracker:
    """Tracks approximate token usage and signals when compaction is needed.

    Usage::

        tracker = TokenBudgetTracker(model="gpt-5-nano")
        tracker.reset()

        # After each message is added to history:
        tracker.add_message(message_dict)

        # Before each LLM call:
        if tracker.should_compact():
            # trigger compaction
            ...
            tracker.recount(new_messages, after_compaction=True)

    The tracker is intentionally lightweight, but uses tiktoken rather than a
    character heuristic because ``should_compact`` gates real LLM calls.
    """

    def __init__(
        self,
        model: str = "",
        threshold: float = _THRESHOLD,
        context_window: int | None = None,
    ) -> None:
        self._model = model
        self._threshold = max(0.1, min(0.99, threshold))
        self._context_window = apply_context_window_override(context_window or context_window_for_model(model))
        self._total_tokens = 0
        self._message_count = 0

    # ------------------------------------------------------------------ public

    def configure(self, *, model: str | None = None, context_window: int | None = None) -> None:
        """Update model/window without disturbing counted messages."""
        if model:
            self._model = model
        self._context_window = apply_context_window_override(context_window or context_window_for_model(self._model))

    def reset(self) -> None:
        """Reset the tracker (call at start of each agent task)."""
        self._total_tokens = 0
        self._message_count = 0

    def add_message(self, message: dict[str, Any]) -> None:
        """Add a message's estimated tokens to the running total."""
        tokens = _estimate_message_tokens(message, model=self._model or _DEFAULT_TOKENIZER_MODEL)
        self._total_tokens += tokens
        self._message_count += 1

    def add_messages(self, messages: list[dict[str, Any]]) -> None:
        """Add multiple messages to the running total."""
        for msg in messages:
            self.add_message(msg)

    def should_compact(self) -> bool:
        """Return True if cumulative tokens exceed the threshold.

        Logs a warning when the threshold is crossed.
        """
        limit = int(self._context_window * self._threshold)
        if self._total_tokens >= limit:
            pct = int((self._total_tokens / self._context_window) * 100)
            logger.warning(
                "Token budget at %d%% (%d/%d tokens, %d messages), " "triggering proactive compaction",
                pct,
                self._total_tokens,
                self._context_window,
                self._message_count,
            )
            return True
        return False

    def recount(self, messages: list[dict[str, Any]], *, after_compaction: bool = False) -> None:
        """Recount tokens from scratch over *messages*.

        Two different callers reach this method and they are not the same event:

        * the **pre-check** (the default) runs before every provider call, purely
          to ask ``should_compact()``. Nothing was compacted; usually nothing
          will be.
        * the **post-compaction** recount (``after_compaction=True``) runs after
          compaction actually replaced the message list.

        Only the second one is news, so only it logs at INFO. This used to log
        ``"Token budget recounted after compaction"`` at INFO from *both*,
        which was wrong twice over. It asserted a compaction that had not
        happened, and — because #531's per-step latency measurements are derived
        from the wall-clock gaps between successive stage log lines — that INFO
        line became the "token-budget recount" stage marker. Four measurement
        rounds on #531 then attributed 421-481 ms to "the token-budget recount".
        The recount itself does not cost that: measured directly over the real
        deployed prompt and a real warm ``/v1/command`` history (5 turns, the
        server-side load limit), it is ~0.1 ms of tokenization, ~3.2 ms with
        this log line included, and still only ~12 ms at 400 turns of history —
        see ``tests/e2e/web/latency/premodel_cpu_bench.py``. The 421-481 ms
        belongs to whatever else runs inside that log-to-log window, and naming
        it after this method sent the ticket's optimization list to the wrong
        step.
        """
        self._total_tokens = 0
        self._message_count = 0
        self.add_messages(messages)
        if after_compaction:
            logger.info(
                "Token budget recounted after compaction: %d tokens, %d messages",
                self._total_tokens,
                self._message_count,
            )
        else:
            logger.debug(
                "Token budget pre-check recount (no compaction): %d tokens, %d messages",
                self._total_tokens,
                self._message_count,
            )

    @property
    def total_tokens(self) -> int:
        """Current estimated token count."""
        return self._total_tokens

    @property
    def usage_pct(self) -> float:
        """Current usage as a fraction of the context window (0.0 – 1.0)."""
        if self._context_window <= 0:
            return 0.0
        return self._total_tokens / self._context_window

    @property
    def context_window(self) -> int:
        """The effective context window size in tokens."""
        return self._context_window
