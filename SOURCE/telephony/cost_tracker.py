"""Developer cost tracking for phone calls.

Tracks actual token usage (from Pipecat's LLM metrics), TTS characters,
STT audio duration, and Telnyx minutes to produce per-call cost breakdowns.

All LLM pricing is imported from services.llm.pricing (single source of truth).
Telnyx per-minute rates live here since they're telephony-specific.

Pipecat's OpenAILLMService emits LLMTokenUsage via start_llm_usage_metrics()
with actual prompt_tokens and completion_tokens from the OpenAI streaming
response (stream_options: include_usage: true). We intercept these via
the on_metrics event handler on the LLM service.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.logging_config import get_logger
from services.llm.pricing import calculate_cost_usd

logger = get_logger(__name__)

# Telnyx per-minute pricing (telephony-specific, not in services.llm.pricing)
_TELNYX_PER_MINUTE_USD = 0.007  # US domestic PSTN outbound (verified via third-party calculator)
_TELNYX_TOLL_FREE_PER_MINUTE_USD = 0.015  # Toll-free origination ($0.01-0.03, using midpoint)
_TELNYX_REGULATORY_SURCHARGE = 0.10  # ~10% (USF, TRS, state telecom taxes)
_TELNYX_NUMBER_MONTHLY_USD = 1.00  # US local DID monthly rental

PHONE_TTS_FREE_PROVIDERS = frozenset({"local", "piper", "espeak"})
PHONE_TTS_PRICING_USD_PER_1K_CHARS = {
    "local": 0.0,
    "piper": 0.0,
    "espeak": 0.0,
    # Official ElevenLabs API pricing lists Flash/Turbo at $0.05/1K chars and
    # Multilingual v2/v3 at $0.10/1K chars. Use the higher public API TTS rate
    # until the runtime pins a cheaper model explicitly.
    "elevenlabs": 0.10,
}
_PHONE_TTS_DISPLAY_MODEL = {
    "local": "kokoro",
    "piper": "piper",
    "espeak": "espeak-ng",
    "elevenlabs": "elevenlabs",
}
_PHONE_TTS_ESTIMATED_CHARS_PER_MINUTE = 800


def _normalize_phone_tts_provider(tts_provider: str) -> str:
    provider = (tts_provider or "local").strip().lower()
    if provider not in PHONE_TTS_PRICING_USD_PER_1K_CHARS:
        raise ValueError("Unsupported phone TTS provider for spend tracking: %s" % tts_provider)
    return provider


def estimate_phone_tts_cost_usd(tts_provider: str, characters: int) -> float:
    """Return TTS spend for text that reaches the phone TTS provider."""
    provider = _normalize_phone_tts_provider(tts_provider)
    billable_characters = max(0, int(characters or 0))
    rate_per_1k = PHONE_TTS_PRICING_USD_PER_1K_CHARS[provider]
    return (billable_characters / 1000.0) * rate_per_1k


def estimate_phone_tts_reservation_cents(tts_provider: str, duration_seconds: float) -> int:
    """Conservative call-start reservation for paid phone TTS."""
    provider = _normalize_phone_tts_provider(tts_provider)
    if provider in PHONE_TTS_FREE_PROVIDERS:
        return 0
    minutes = max(0.0, float(duration_seconds or 0.0)) / 60.0
    estimated_characters = math.ceil(minutes * _PHONE_TTS_ESTIMATED_CHARS_PER_MINUTE)
    estimated_cost_usd = estimate_phone_tts_cost_usd(provider, estimated_characters)
    return max(1, math.ceil(estimated_cost_usd * 100)) if estimated_cost_usd > 0.0 else 0


def _get_telnyx_rate(phone_number: str) -> float:
    """Return per-minute Telnyx rate based on number type."""
    from telephony.number_validation import is_toll_free_number

    if is_toll_free_number(phone_number):
        return _TELNYX_TOLL_FREE_PER_MINUTE_USD
    return _TELNYX_PER_MINUTE_USD


@dataclass
class CallCostBreakdown:
    """Complete cost breakdown for a phone call."""

    call_id: str = ""
    duration_seconds: float = 0.0
    # Telnyx
    telnyx_minutes: float = 0.0
    telnyx_cost_usd: float = 0.0
    # LLM
    llm_model: str = ""
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    llm_cost_usd: float = 0.0
    # STT
    stt_model: str = "whisper-base"
    stt_audio_seconds: float = 0.0
    stt_cost_usd: float = 0.0  # $0 for local Whisper
    # TTS
    tts_model: str = "kokoro"
    tts_characters: int = 0
    tts_cost_usd: float = 0.0  # $0 for local Kokoro
    # Hold mode
    hold_duration_seconds: float = 0.0
    hold_windows_opened: int = 0
    # Summary
    total_cost_usd: float = 0.0
    cost_per_minute_usd: float = 0.0

    def to_dict(self) -> dict:
        """Serialize to dict for JSON storage."""
        return {
            "call_id": self.call_id,
            "duration_seconds": round(self.duration_seconds, 1),
            "telnyx": {
                "minutes": round(self.telnyx_minutes, 2),
                "cost_usd": round(self.telnyx_cost_usd, 6),
            },
            "llm": {
                "model": self.llm_model,
                "prompt_tokens": self.llm_prompt_tokens,
                "completion_tokens": self.llm_completion_tokens,
                "cost_usd": round(self.llm_cost_usd, 6),
            },
            "stt": {
                "model": self.stt_model,
                "audio_seconds": round(self.stt_audio_seconds, 1),
                "cost_usd": round(self.stt_cost_usd, 6),
            },
            "tts": {
                "model": self.tts_model,
                "characters": self.tts_characters,
                "cost_usd": round(self.tts_cost_usd, 6),
            },
            "total_cost_usd": round(self.total_cost_usd, 6),
            "cost_per_minute_usd": round(self.cost_per_minute_usd, 6),
        }


class CostTracker:
    """Accumulates actual costs during a phone call.

    Pipecat 0.0.106 emits LLM token usage via a ``MetricsFrame`` on the
    pipeline bus (NOT via the event-handler system — registering
    ``event_handler("on_metrics")`` only logs a warning and silently
    discards the handler). Wire this tracker via ``CostMetricsCollector``
    inserted between the LLM and TTS in the Pipecat pipeline:

        tracker = CostTracker(call_id, llm_model)
        collector = CostMetricsCollector(tracker)
        pipeline = Pipeline([..., llm, collector, tts, ...])
    """

    def __init__(
        self,
        call_id: str,
        llm_model: str = "gpt-5.4-mini",
        phone_number: str = "",
        tts_provider: str = "local",
    ) -> None:
        self._call_id = call_id
        self._llm_model = llm_model
        self._phone_number = phone_number
        self._tts_provider = _normalize_phone_tts_provider(tts_provider)
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._tts_characters = 0
        self._stt_audio_seconds = 0.0

    def add_llm_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Record LLM token usage from a single API call."""
        self._prompt_tokens += prompt_tokens
        self._completion_tokens += completion_tokens
        logger.debug(
            "CostTracker[%s] LLM: +%d prompt, +%d completion (total: %d/%d)",
            self._call_id,
            prompt_tokens,
            completion_tokens,
            self._prompt_tokens,
            self._completion_tokens,
        )

    def add_tts_usage(self, characters: int) -> None:
        """Record TTS character count."""
        self._tts_characters += max(0, int(characters or 0))

    def add_stt_usage(self, audio_seconds: float) -> None:
        """Record STT audio duration processed."""
        self._stt_audio_seconds += audio_seconds

    def build_breakdown(self, duration_seconds: float) -> CallCostBreakdown:
        """Compute a cost breakdown without settling user spend counters."""
        minutes = duration_seconds / 60.0
        rate = _get_telnyx_rate(self._phone_number)
        telnyx_cost = minutes * rate * (1 + _TELNYX_REGULATORY_SURCHARGE)

        # LLM cost from actual tokens - pricing from services.llm.pricing (single source of truth)
        llm_cost = calculate_cost_usd(self._llm_model, self._prompt_tokens, self._completion_tokens)

        # STT: $0 for local Whisper
        stt_cost = 0.0

        tts_cost = estimate_phone_tts_cost_usd(self._tts_provider, self._tts_characters)

        total = telnyx_cost + llm_cost + stt_cost + tts_cost
        cpm = total / minutes if minutes > 0 else 0.0

        breakdown = CallCostBreakdown(
            call_id=self._call_id,
            duration_seconds=duration_seconds,
            telnyx_minutes=minutes,
            telnyx_cost_usd=telnyx_cost,
            llm_model=self._llm_model,
            llm_prompt_tokens=self._prompt_tokens,
            llm_completion_tokens=self._completion_tokens,
            llm_cost_usd=llm_cost,
            stt_model="whisper-base",
            stt_audio_seconds=self._stt_audio_seconds,
            stt_cost_usd=stt_cost,
            tts_model=_PHONE_TTS_DISPLAY_MODEL.get(self._tts_provider, self._tts_provider),
            tts_characters=self._tts_characters,
            tts_cost_usd=tts_cost,
            total_cost_usd=total,
            cost_per_minute_usd=cpm,
        )

        return breakdown

    def finalize(self, duration_seconds: float, user_id: str = "") -> CallCostBreakdown:
        """Compute final cost breakdown.

        Args:
            duration_seconds: Total call duration.
            user_id: User ID for recording spend in plan_limiter.

        Returns:
            Complete CallCostBreakdown with actual token counts.
        """
        breakdown = self.build_breakdown(duration_seconds)
        total = breakdown.total_cost_usd

        logger.info(
            "CostTracker[%s] finalized: $%.4f (telnyx=$%.4f, llm=$%.4f, tts=$%.4f, "
            "prompt=%d, completion=%d, model=%s, tts_provider=%s)",
            self._call_id,
            total,
            breakdown.telnyx_cost_usd,
            breakdown.llm_cost_usd,
            breakdown.tts_cost_usd,
            self._prompt_tokens,
            self._completion_tokens,
            self._llm_model,
            self._tts_provider,
        )

        # Company-funded calls reconcile with the company spend ledger.
        from telephony.usage import company_phone_billing_available

        if user_id and company_phone_billing_available():
            try:
                from billing.plan_limiter import get_plan_limiter

                total_cents = total * 100
                get_plan_limiter().settle_spend_cents(user_id, total_cents)
                logger.debug(
                    "CostTracker[%s] recorded %.1f cents to plan_limiter for user %s",
                    self._call_id,
                    total_cents,
                    user_id,
                )
            except Exception:
                # total_cents may not be defined if get_plan_limiter() itself failed
                _lost = total_cents if "total_cents" in dir() else total * 100
                logger.exception(
                    "CostTracker[%s] plan_limiter spend recording FAILED for user %s (%.1f cents lost)",
                    self._call_id,
                    user_id,
                    _lost,
                )
                raise  # Don't silently swallow — caller must know billing failed

        return breakdown

    def format_breakdown(self, breakdown: CallCostBreakdown) -> str:
        """Format a cost breakdown as a human-readable string."""
        mins = int(breakdown.duration_seconds // 60)
        secs = int(breakdown.duration_seconds % 60)
        lines = [
            "=== COST BREAKDOWN ===",
            "Duration: %dm %ds" % (mins, secs),
            "Telnyx:   $%.4f (%.2f min x $%.3f/min)"
            % (breakdown.telnyx_cost_usd, breakdown.telnyx_minutes, _TELNYX_PER_MINUTE_USD),
            "LLM:      $%.4f (%d prompt + %d completion tokens, %s)"
            % (
                breakdown.llm_cost_usd,
                breakdown.llm_prompt_tokens,
                breakdown.llm_completion_tokens,
                breakdown.llm_model,
            ),
            "STT:      $%.4f (local Whisper base)" % breakdown.stt_cost_usd,
            "TTS:      $%.4f (%s, %d chars)" % (breakdown.tts_cost_usd, breakdown.tts_model, breakdown.tts_characters),
            "---------------------",
            "TOTAL:    $%.4f" % breakdown.total_cost_usd,
            "Cost/min: $%.4f" % breakdown.cost_per_minute_usd,
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipecat FrameProcessor — intercepts MetricsFrame, feeds CostTracker.
# ---------------------------------------------------------------------------
#
# Pipecat 0.0.106 pushes LLM token usage into the pipeline as a
# ``MetricsFrame`` containing a list of ``MetricsData`` items. The
# `event_handler("on_metrics")` approach a previous version of this
# file documented is **broken** — Pipecat's AIService only registers
# ``on_function_calls_started`` and ``on_completion_timeout``; any
# other event name is silently discarded (logger.warning in
# pipecat/utils/base_object.py:add_event_handler). This collector is
# the supported integration point.

try:
    from pipecat.frames.frames import LLMTextFrame, MetricsFrame, TextFrame, TTSSpeakFrame
    from pipecat.metrics.metrics import LLMUsageMetricsData
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    _PIPECAT_AVAILABLE = True
except ImportError:
    _PIPECAT_AVAILABLE = False


if _PIPECAT_AVAILABLE:

    class CostMetricsCollector(FrameProcessor):
        """Captures LLM token usage from MetricsFrames and forwards them.

        Insert between the LLM service and the TTS in the Pipecat
        pipeline. Reads ``LLMUsageMetricsData`` entries from every
        ``MetricsFrame`` that passes through and calls
        ``tracker.add_llm_usage`` with the reported prompt/completion
        tokens. All frames (metrics or otherwise) are forwarded
        unchanged so downstream processors see the same stream.
        """

        def __init__(self, tracker: CostTracker) -> None:
            super().__init__()
            self._tracker = tracker

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)

            if isinstance(frame, MetricsFrame):
                for entry in frame.data or ():
                    if isinstance(entry, LLMUsageMetricsData):
                        usage = entry.value
                        try:
                            self._tracker.add_llm_usage(
                                int(usage.prompt_tokens or 0),
                                int(usage.completion_tokens or 0),
                            )
                        except Exception:
                            logger.debug(
                                "CostMetricsCollector: failed to record usage",
                                exc_info=True,
                            )

            await self.push_frame(frame, direction)

    class TTSUsageCollector(FrameProcessor):
        """Counts text characters immediately before phone TTS consumes them."""

        def __init__(self, tracker: CostTracker) -> None:
            super().__init__()
            self._tracker = tracker

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)

            if direction == FrameDirection.DOWNSTREAM and isinstance(frame, (LLMTextFrame, TextFrame, TTSSpeakFrame)):
                try:
                    self._tracker.add_tts_usage(len(str(getattr(frame, "text", "") or "")))
                except Exception:
                    logger.exception("TTSUsageCollector: failed to record TTS usage")

            await self.push_frame(frame, direction)

else:  # pragma: no cover — imported only when pipecat is missing

    class CostMetricsCollector:  # type: ignore[no-redef]
        """Stub when Pipecat isn't installed; collector is a no-op."""

        def __init__(self, tracker: CostTracker) -> None:
            self._tracker = tracker

        async def process_frame(self, frame, direction):  # pragma: no cover
            return None

        async def push_frame(self, frame, direction):  # pragma: no cover
            return None

    class TTSUsageCollector:  # type: ignore[no-redef]
        """Stub when Pipecat isn't installed; collector is a no-op."""

        def __init__(self, tracker: CostTracker) -> None:
            self._tracker = tracker

        async def process_frame(self, frame, direction):  # pragma: no cover
            return None

        async def push_frame(self, frame, direction):  # pragma: no cover
            return None
