"""Vision LLM analysis and multi-turn screen insight sessions.

Sends enriched screen context (screenshot + metadata) to a vision-capable
LLM and returns a natural-language analysis.  Supports both one-shot and
streaming responses, as well as multi-turn follow-up conversations about
the same screen via :class:`ScreenInsightSession`.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger

if TYPE_CHECKING:
    from vision.context_enrichment import EnrichedContext

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are Viola's screen-awareness module.  The user has shared what "
    "is currently visible on their display and asked a question about it.  "
    "Analyse the visual content together with any extracted text or "
    "metadata provided and give a concise, helpful answer.\n\n"
    "Guidelines:\n"
    "- Be direct and concise.  Prefer short paragraphs or bullet points.\n"
    "- When the answer is ambiguous, think step-by-step before concluding.\n"
    "- Never mention that you are looking at a 'screenshot' or 'image' -- "
    "speak as though you can see the user's screen naturally.\n"
    "- If you spot a potential issue (error dialog, warning, typo), "
    "proactively mention it.\n"
    "- If the content is sensitive (passwords, financial data), acknowledge "
    "the sensitivity and refuse to read it aloud."
)

# ---------------------------------------------------------------------------
# Model preference list for vision tasks
# ---------------------------------------------------------------------------

_VISION_MODEL_PREFERENCE: list[str] = [
    "claude-sonnet-4-20250514",
    "gpt-4o",
    "gemini-2.0-flash",
]

# ---------------------------------------------------------------------------
# Follow-up detection
# ---------------------------------------------------------------------------

_FOLLOWUP_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bwhat about\b", re.IGNORECASE),
    re.compile(r"\bexplain\b", re.IGNORECASE),
    re.compile(r"\btell me more\b", re.IGNORECASE),
    re.compile(r"\bcan you elaborate\b", re.IGNORECASE),
    re.compile(r"\bgo on\b", re.IGNORECASE),
    re.compile(r"\bwhat else\b", re.IGNORECASE),
    re.compile(r"\band\b.*\?$", re.IGNORECASE),
    re.compile(r"\bhow about\b", re.IGNORECASE),
    re.compile(r"\bwhat does that mean\b", re.IGNORECASE),
    re.compile(r"\bwhy\b.*\?$", re.IGNORECASE),
    re.compile(r"\bmore detail\b", re.IGNORECASE),
    re.compile(r"\bclarify\b", re.IGNORECASE),
    re.compile(r"\bwhat do you mean\b", re.IGNORECASE),
    re.compile(r"\bcontinue\b", re.IGNORECASE),
    re.compile(r"\bkeep going\b", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# VisionAnalyzer
# ---------------------------------------------------------------------------


class VisionAnalyzer:
    """Analyse enriched screen context using a vision-capable LLM.

    Uses Viola's :class:`~services.llm.provider_router.ProviderAgnosticRouter`
    for model selection and request routing.

    Attributes:
        SYSTEM_PROMPT: The system prompt sent to the vision LLM.
    """

    SYSTEM_PROMPT: str = _SYSTEM_PROMPT

    def __init__(self) -> None:
        self._router: Any | None = None

    # ------------------------------------------------------------------
    # Lazy router access
    # ------------------------------------------------------------------

    def _get_router(self) -> Any:
        """Return (or create) the :class:`ProviderAgnosticRouter` instance."""
        if self._router is None:
            try:
                from services.llm.provider_router import (
                    ProviderAgnosticRouter,
                    get_active_router,
                )

                self._router = get_active_router()
                if self._router is None:
                    self._router = ProviderAgnosticRouter()
            except Exception as exc:
                logger.error("Failed to initialise LLM router: %s", exc)
                raise RuntimeError("Vision analysis requires an LLM provider.  " "Check your AI settings.") from exc
        return self._router

    # ------------------------------------------------------------------
    # Message construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_user_message(context: EnrichedContext) -> list[dict[str, Any]]:
        """Build a multimodal user message for the vision LLM.

        The message is a list of content blocks following the OpenAI
        ``messages`` format (compatible with Anthropic / Google adapters):

        1. An ``image_url`` block with the base64 JPEG screenshot.
        2. A ``text`` block combining the user question, window title,
           URL, extracted text, and clipboard text.

        Args:
            context: Enriched screen context.

        Returns:
            A list of content-block dicts.
        """
        content_parts: list[dict[str, Any]] = []

        # Image block
        content_parts.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/jpeg;base64," + context.screenshot_b64,
                    "detail": "high",
                },
            }
        )

        # Text block -- assemble contextual information
        text_lines: list[str] = []
        text_lines.append(context.user_question)

        if context.window_title:
            text_lines.append("[Window: %s]" % context.window_title)
        if context.app_name:
            text_lines.append("[App: %s]" % context.app_name)
        if context.url:
            text_lines.append("[URL: %s]" % context.url)
        if context.extracted_text:
            # Truncate very long extracted text to avoid blowing context
            excerpt = context.extracted_text[:3000]
            text_lines.append("[Extracted text]\n%s" % excerpt)
        if context.clipboard_text:
            text_lines.append("[Clipboard]\n%s" % context.clipboard_text)

        content_parts.append({"type": "text", "text": "\n\n".join(text_lines)})

        return content_parts

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def analyze(
        self,
        context: EnrichedContext,
        stream: bool = True,
    ) -> str | AsyncIterator[str]:
        """Analyse the enriched screen context via a vision LLM.

        Args:
            context: Enriched screen capture context.
            stream: If ``True`` (default), return an async iterator that
                yields text chunks as they arrive.  If ``False``, return
                the complete response string.

        Returns:
            Complete answer string when *stream* is ``False``, or an
            :class:`AsyncIterator` of text chunks when *stream* is ``True``.

        Raises:
            PrivacyBlockedError: when the privacy filter refuses the capture.
        """
        from vision.privacy_filter import get_privacy_filter

        # Mandatory privacy gate: no pixels or extracted text leave the
        # device for the vision LLM without block/redact enforcement.
        context = get_privacy_filter().enforce_context(context)

        router = self._get_router()
        user_content = self._build_user_message(context)

        # We use the router's ask() for a simple non-streaming response.
        # For streaming we would need the underlying provider directly;
        # the current router API returns a finished string, so we
        # simulate streaming by yielding the full result in one chunk
        # when a true streaming API is not available.

        # Select the best vision-capable model from our preference list
        model_override = self._pick_vision_model(router)

        try:
            answer = await router.ask(
                question=user_content,
                system_prompt=self.SYSTEM_PROMPT,
                include_history=False,
                max_tokens=1024,
                temperature=0.5,
            )

            if stream:

                async def _stream_wrapper() -> AsyncIterator[str]:
                    yield answer

                return _stream_wrapper()
            return answer
        except Exception as exc:
            logger.exception("Vision analysis failed")
            raise RuntimeError("Vision analysis failed: %s" % exc) from exc

    # ------------------------------------------------------------------
    # Model selection helper
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_vision_model(router: Any) -> str | None:
        """Select a vision-capable model from the preference list.

        Inspects the router's active provider to see which model is
        configured and returns an override only when the current model
        is not in the preference list.

        Returns:
            A model name override, or ``None`` to use the default.
        """
        try:
            status = router.get_status()
            primary = status.get("primary") or {}
            current_model = primary.get("model", "")
            # If the current model is already vision-capable, no override
            for preferred in _VISION_MODEL_PREFERENCE:
                if preferred in current_model:
                    return None
            # Otherwise suggest the first preference (router may ignore it)
            return _VISION_MODEL_PREFERENCE[0]
        except Exception:
            return None


# ---------------------------------------------------------------------------
# ScreenInsightSession
# ---------------------------------------------------------------------------


class ScreenInsightSession:
    """Maintains conversational context for follow-up questions about a screen.

    A session is created when the user first asks about their screen and
    allows subsequent questions (within 120 seconds) to reference the same
    screenshot without re-capturing.

    Attributes:
        ttl: Session time-to-live in seconds (default 120).
    """

    ttl: float = 120.0

    def __init__(self) -> None:
        self._context: EnrichedContext | None = None
        self._last_activity: float = 0.0
        self._history: list[dict[str, str]] = []

    # ------------------------------------------------------------------
    # Context management
    # ------------------------------------------------------------------

    def update_context(self, context: EnrichedContext) -> None:
        """Store a new enriched context and reset the session timer.

        Args:
            context: The enriched screen context to store.
        """
        self._context = context
        self._last_activity = time.monotonic()
        self._history.clear()
        logger.debug("Screen insight session updated for %s", context.app_name)

    def get_context(self) -> EnrichedContext | None:
        """Return the stored context if the session has not expired.

        Returns:
            The stored :class:`EnrichedContext`, or ``None`` if the session
            has expired or no context was stored.
        """
        if self._context is None:
            return None
        if time.monotonic() - self._last_activity > self.ttl:
            logger.debug("Screen insight session expired (TTL=%s s)", self.ttl)
            self._context = None
            self._history.clear()
            return None
        return self._context

    # ------------------------------------------------------------------
    # Conversation history
    # ------------------------------------------------------------------

    def add_exchange(self, question: str, answer: str) -> None:
        """Record a question/answer exchange in the session history.

        Args:
            question: The user's question.
            answer: The assistant's answer.
        """
        self._history.append({"role": "user", "content": question})
        self._history.append({"role": "assistant", "content": answer})
        self._last_activity = time.monotonic()

    def get_history(self) -> list[dict[str, str]]:
        """Return the conversation history for this session.

        Returns:
            List of ``{"role": ..., "content": ...}`` dicts.
        """
        return list(self._history)

    # ------------------------------------------------------------------
    # Follow-up detection
    # ------------------------------------------------------------------

    @staticmethod
    def is_followup(text: str) -> bool:
        """Detect whether *text* looks like a follow-up question.

        Checks for common follow-up indicators such as "what about",
        "explain", "tell me more", etc.

        Args:
            text: User input to test.

        Returns:
            ``True`` if the text matches a follow-up pattern.
        """
        for pattern in _FOLLOWUP_PATTERNS:
            if pattern.search(text):
                return True
        return False
