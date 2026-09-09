"""Pipecat text filter adapter for TTS normalization on phone calls.

Wraps voice.synthesis.text_normalizer.normalize_for_speech as a Pipecat
BaseTextFilter so it can be passed to KokoroTTSService's text_filters
parameter. This ensures numbers, currency, phone numbers, percentages,
and times are spoken correctly during phone calls.

It ALSO runs the defense-in-depth corruption guard
(telephony.tts_corruption_guard) on every TTS turn, BEFORE normalization, so
that non-speakable garbage from a rare model degeneration -- foreign-script
letter runs and leaked tool/control tokens -- is never voiced to a recipient.
See tts_corruption_guard for the full incident (cloud call 53344591 voicemail).

Usage in call_manager.py::

    from telephony.tts_normalizer import SpeechTextFilter

    speech_filter = SpeechTextFilter()
    tts = KokoroTTSService(..., text_filters=[speech_filter])
    speech_filter.bind_tts(tts)  # so the guard can read the active TTS language
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

try:
    from pipecat.utils.text.base_text_filter import BaseTextFilter

    PIPECAT_AVAILABLE = True
except ImportError:
    PIPECAT_AVAILABLE = False


if PIPECAT_AVAILABLE:

    class SpeechTextFilter(BaseTextFilter):
        """Pipecat text filter that normalizes LLM text for natural TTS speech.

        Converts currency ($25 → twenty-five dollars), phone numbers
        (555-123-4567 → digit-by-digit), percentages (15% → fifteen percent),
        times (2:30 PM → two thirty PM), ordinals (1st → first), and bare
        numbers to English words.

        Set ``summarize=False`` for non-Viola speakers (e.g. the receptionist
        role-play in tools/devbench/phone_receptionist_pipeline.py) whose dialogue
        should NOT be capped at three sentences with Viola's
        "I'll send the full details in chat." suffix.

        Reuses the existing voice.synthesis.text_normalizer.normalize_for_speech
        engine — no duplicate code.
        """

        def __init__(self, *, summarize: bool = True) -> None:
            super().__init__()
            self._summarize = summarize
            # The TTS service this filter is attached to, set via bind_tts().
            # Used only to read the ACTIVE TTS language so the corruption guard
            # scopes its foreign-script stripping to English (and never strips a
            # legitimate language-switch to Chinese/Japanese/Spanish/...).
            self._tts: Any | None = None
            # The call_id of the call this filter serves, set via bind_tts().
            # Logged on every strip so a post-deploy garbage instance is
            # correlatable back to its decrypted trace -- which holds the
            # turn-type (is it the last/goodbye turn?) and whether end_call
            # emitted as a tool vs leaked as text. Without it each strip is an
            # anonymous warning and the goodbye/end_call-correlation watch-item
            # (call 53344591) stays unanswerable. Empty for non-call binds
            # (desktop voice session / loopback dev harness).
            self._call_id: str = ""

        def bind_tts(self, tts: Any, *, call_id: str = "") -> None:
            """Attach the owning TTS service so the guard can read its language.

            Called right after the TTS service is constructed in
            call_manager._create_tts. The guard reads ``tts._settings.language``
            at filter time; if no TTS is bound the guard defaults to English
            (the safe default -- the phone default language is English).

            ``call_id`` (the owning phone call's id) is stored so every strip is
            logged with its call_id -- the correlation key back to the trace.
            """
            self._tts = tts
            if call_id:
                self._call_id = call_id

        def _active_language(self) -> str:
            """Return the active TTS language code (defaults to English)."""
            tts = self._tts
            if tts is None:
                return "en"
            settings = getattr(tts, "_settings", None)
            lang = getattr(settings, "language", None) if settings is not None else None
            if lang is None:
                return "en"
            # Pipecat Language enum -> its value; plain strings pass through.
            return str(getattr(lang, "value", lang))

        async def filter(self, text: str) -> str:
            """Sanitize corruption, then apply speech normalization, for TTS.

            Order matters: the corruption guard runs FIRST so foreign-script
            garbage and leaked tool tokens are removed before normalization. If
            nothing speakable remains, returns "" -- the shared Pipecat
            TTSService drops empty/whitespace text, so the recipient hears
            silence rather than voiced junk.
            """
            from telephony.tts_corruption_guard import sanitize_tts_text
            from voice.synthesis.text_normalizer import normalize_for_speech

            language = self._active_language()
            cleaned, report = sanitize_tts_text(text, language=language)

            if report.stripped_anything:
                # Observe every strip so the model-degeneration rate is monitored
                # going forward (clustering on goodbye/end_call turns would
                # re-implicate the hang-up prompt). The call_id makes each strip
                # correlatable to its trace (turn-type + whether end_call emitted
                # as a tool); end_call_leaked surfaces the direct signal -- the
                # model wrote an end_call token as TEXT this turn -- without
                # needing the trace. Sample is log-safe + bounded.
                end_call_leaked = any("end_call" in tok for tok in report.tool_tokens)
                logger.warning(
                    "phone TTS corruption guard stripped %d garbage run(s) "
                    "[call=%s lang=%s foreign=%d tool_tokens=%d end_call_leaked=%s]: %s",
                    report.strip_count,
                    self._call_id or "unknown",
                    report.language,
                    len(report.foreign_runs),
                    len(report.tool_tokens),
                    end_call_leaked,
                    report.sample(),
                )
                # The warning above dies in `docker logs viola-api` where no
                # operator ever sees it. Page the operator (debounced to <=1/hour)
                # so a recurrence of this rare model degeneration is actually
                # noticed instead of staying a permanently-open question.
                # Best-effort: notify_phone_tts_corruption swallows every failure,
                # so an alerting problem can never break the live call.
                try:
                    from telephony.tts_corruption_alert import notify_phone_tts_corruption
                except ModuleNotFoundError as exc:
                    if exc.name != "telephony.tts_corruption_alert":
                        raise
                    # Public installations retain the local warning above.
                    # Company paging is not required to sanitize spoken output.
                else:
                    await notify_phone_tts_corruption(
                        call_id=self._call_id,
                        report=report,
                        end_call_leaked=end_call_leaked,
                    )

            if not cleaned.strip():
                # Garbage-only turn -> speak nothing.
                return ""

            return normalize_for_speech(cleaned, summarize=self._summarize)
