"""Keyterm boosting for Deepgram streaming STT (S10-VOICE-003).

Deepgram Nova-2/Nova-3 accept a list of "keyterms" (Nova-3 spelling) or
"keywords" (Nova-2 spelling) at WebSocket connect time. These act as a
language-model prior that biases the decoder toward the supplied words
and phrases — useful for vocabulary the generic model under-recognises:

* product / brand names (``Viola``, ``Spotify``)
* multi-syllable proper nouns (``Stradivari``, ``Anthropic``)
* technical / domain words specific to the assistant's verbs
  (``timer``, ``reminder``, ``thermostat``)

This module curates that vocabulary and exposes it as a single function
that :class:`voice.dictation.streaming_stt.DeepgramStreamingSTT` calls
at connect time when no explicit list was passed to its constructor.

Design notes
------------

* **Compact list.** Deepgram does not document a hard cap, but
  empirically more than ~50 terms degrades latency without measurable
  WER win. We keep the default list under 50 entries.
* **No PII.** This list ends up in a Deepgram URL parameter on every
  connection; it must never contain user-specific data (names from the
  address book, file paths, etc.). User-scoped boosting belongs in
  ``DeepgramStreamingSTT(keyterms=[...])`` — explicit caller intent.
* **Caller override wins.** ``DeepgramStreamingSTT.__init__(keyterms=...)``
  bypasses this module entirely. An explicit empty list disables
  enrichment without falling back to the curated default.

Mirrors the role of Claude Code's ``voiceKeyterms.ts`` for Viola's
voice-assistant vocabulary rather than Claude's coding-assistant
vocabulary.
"""

from __future__ import annotations

from core.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------- #
# Curated default keyterms                                                #
# ---------------------------------------------------------------------- #
#
# Grouped for readability — Deepgram receives one flat list.  Every term
# here must satisfy:
#   1. Used in real Viola intents / responses.
#   2. Either rare in the base ASR model, or a homophone the model is
#      known to mishear ("vee oh luh" → "viola").
#   3. ASCII only — Deepgram URL-encodes the value but unicode in query
#      params still hits CDN edge cases.
#   4. PII-free.
#
# Keep this list in sync with the dictation tests so a regression that
# strips a class of vocabulary fails loudly.

_BRAND_AND_PRODUCT_TERMS: tuple[str, ...] = (
    "Viola",
    "Spotify",
    "YouTube",
    "Anthropic",
    "Claude",
    "OpenAI",
    "ChatGPT",
    "Stradivari",
    "Kokoro",
    "Silero",
    "Deepgram",
    "Whisper",
)

_ASSISTANT_VERB_TERMS: tuple[str, ...] = (
    "play",
    "pause",
    "skip",
    "shuffle",
    "queue",
    "remind",
    "schedule",
    "timer",
    "alarm",
    "dictate",
    "transcribe",
    "translate",
)

_DOMAIN_NOUN_TERMS: tuple[str, ...] = (
    "playlist",
    "calendar",
    "reminder",
    "thermostat",
    "podcast",
    "audiobook",
    "smart home",
    "lights",
)

_HOMOPHONE_GUARD_TERMS: tuple[str, ...] = (
    # "viola" is heard as "violet" / "via la" / "violator" without a
    # prior. The brand-list entry covers capitalised; keep a lowercase
    # variant so case-sensitive decoders still bias correctly.
    "viola",
    # Stradivari → "stretch a vari" / "stress over re" without prior.
    "stradivari",
    # Kokoro → "cocoa row" / "kokoro" itself is rare.
    "kokoro",
)


def _curated_default_terms() -> list[str]:
    """Flatten the grouped lists, dedupe, and preserve insertion order."""
    seen: set[str] = set()
    out: list[str] = []
    for group in (
        _BRAND_AND_PRODUCT_TERMS,
        _ASSISTANT_VERB_TERMS,
        _DOMAIN_NOUN_TERMS,
        _HOMOPHONE_GUARD_TERMS,
    ):
        for term in group:
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(term)
    return out


def get_voice_keyterms(extra: list[str] | None = None) -> list[str]:
    """Return the keyterm list to attach to a Deepgram connection.

    Parameters
    ----------
    extra:
        Optional list of caller-supplied terms appended after the
        curated defaults. Useful for context-aware enrichment — e.g.
        the caller knows the user just opened the Calendar UI and
        wants ``"appointment"`` / ``"meeting"`` boosted for the next
        utterance. ``None`` (default) returns just the curated list.

    Returns
    -------
    list[str]
        Deduplicated, ordered list of keyterms. Safe to pass directly
        as the Deepgram ``keyterm=`` / ``keywords=`` query value.

    Notes
    -----
    The function never raises. If something in the call chain breaks
    (e.g. a settings store fails), the caller will receive ``[]`` and
    dictation continues with model defaults — keyterm boosting is an
    accuracy enhancement, not a hard requirement for STT to function.
    """
    try:
        base = _curated_default_terms()
        if not extra:
            return base
        seen = {term.lower() for term in base}
        for term in extra:
            if not isinstance(term, str) or not term.strip():
                continue
            normalized = term.strip()
            if normalized.lower() in seen:
                continue
            seen.add(normalized.lower())
            base.append(normalized)
        return base
    except Exception:
        # Truly defensive — must not break dictation.
        logger.debug("Failed to assemble voice keyterms", exc_info=True)
        return []


__all__ = ["get_voice_keyterms"]
