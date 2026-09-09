"""Defense-in-depth corruption guard for phone TTS text.

Viola's model (gpt-5.4-mini) occasionally *degenerates* on a turn and emits
non-speakable garbage mixed into otherwise-clean prose. On cloud call 53344591
(a voicemail) the model produced::

    This is Viola, the phone system is working now, talk soon. Goodbye.
    ಅಂತ to=end_call 腾讯天天中彩票 This call may be recorded and transcribed.

i.e. a clean message + "Goodbye." then GARBAGE: Kannada letters, the literal
tool-call syntax "to=end_call", and Chinese lottery spam. Kokoro TTS then VOICED
that garbage to the recipient's voicemail ("...to equals end call, Chinese
letter, Chinese letter..."). A corpus scan of 59 calls found this is the ONLY
instance (~1/59 rare model degeneration), so the correct fix is a TTS-gate
corruption guard, NOT chasing the model's sampling.

This guard runs as close to the voice as possible -- inside the Pipecat text
filter that every phone TTS provider already shares (see
``telephony/tts_normalizer.SpeechTextFilter``) -- and removes two classes of
non-speakable garbage before any text reaches the synthesizer:

  (a) **Foreign-script letter runs.** Maximal runs of letters whose Unicode
      script is not Latin (CJK, Kannada, Hangul, Hiragana/Katakana, Cyrillic,
      Arabic, Devanagari, Thai, ...) plus the combining marks that attach to
      them. These characters have no business in an English utterance.

  (b) **Leaked tool/control tokens.** The phone function names
      (``end_call``, ``consult_user``, ``press_button``, ``save_call_result``,
      ``present_call_plan``, ``enter_hold_mode``) and the tool-call syntax that
      sometimes bleeds in front of them (``to=``, ``=``) when they appear as
      *spoken text* rather than an actual tool call.

This is a corruption guard, NOT model-intent boxing. It does not classify the
user's query, parse the model's natural-language reply to branch behavior, or
inject steering hints. It only deletes characters that are physically
un-speakable English -- the runtime stays trusting of the model's *meaning*.

**Language-switch safety.** ``telephony/language_handler.py`` legitimately
switches the phone TTS to a non-English supported language (Chinese, Japanese,
Spanish, ...) for non-English recipients. When the active TTS language is one of
those, non-Latin text is CORRECT and is NOT stripped -- only the foreign-script
rule is scoped to English. The tool-token rule always applies (a leaked
``end_call`` is garbage in any language).

If a turn is garbage-ONLY (nothing speakable remains after sanitizing) the
caller should speak NOTHING -- the shared Pipecat ``TTSService`` already drops
empty/whitespace text before synthesis, so returning ``""`` yields silence.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Canonical phone tool/function names. MUST stay a superset of every tool name
# exposed to the phone model. The check-phone-tts-corruption-guard gate verifies
# this set covers the tool names declared in telephony/call_tools.py and
# call_manager.py, so a newly-added tool can't leak its name unguarded.
PHONE_TOOL_TOKENS: frozenset[str] = frozenset(
    {
        "consult_user",
        "present_call_plan",
        "press_button",
        "save_call_result",
        "end_call",
        "enter_hold_mode",
    }
)

# Languages for which non-Latin script is legitimate (the language-switch
# feature, telephony/language_handler.KOKORO_SUPPORTED minus English).
_NON_ENGLISH_SUPPORTED = frozenset({"es", "fr", "it", "pt", "ja", "zh", "hi"})

# Tool-token matcher: an optional tool-call connector (``to=`` / ``name:`` /
# ``=``) immediately followed by one of the function names, as a whole word.
# Longest names first so e.g. ``save_call_result`` wins over any prefix.
_TOOL_TOKEN_RE = re.compile(
    r"(?:\b(?:to|name|tool|function|fn|recipient|type|args|action)\s*[=:]\s*)?"
    r"[=:]?\s*"
    r"\b(?:" + "|".join(re.escape(t) for t in sorted(PHONE_TOOL_TOKENS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

_WHITESPACE_RE = re.compile(r"\s+")

# Cap on how many sample fragments we keep for logging (avoid unbounded logs).
_MAX_SAMPLES = 8


@dataclass
class StripReport:
    """What the corruption guard removed from one TTS turn."""

    language: str = "en"
    foreign_runs: list[str] = field(default_factory=list)
    tool_tokens: list[str] = field(default_factory=list)

    @property
    def stripped_anything(self) -> bool:
        return bool(self.foreign_runs or self.tool_tokens)

    @property
    def strip_count(self) -> int:
        return len(self.foreign_runs) + len(self.tool_tokens)

    def sample(self) -> str:
        """Short log-safe sample of what was stripped."""
        parts = self.foreign_runs[:_MAX_SAMPLES] + self.tool_tokens[:_MAX_SAMPLES]
        joined = " | ".join(p.strip() for p in parts if p.strip())
        return joined[:200]


def is_english_language(language: str | None) -> bool:
    """True when the active TTS language is English (or unknown -> default English).

    Non-Latin stripping is scoped to English so the language-switch feature
    (Chinese/Japanese/Spanish/... recipients) is never corrupted. Unknown or
    missing language defaults to English, which is the correct, safe default:
    the phone default is English and English-only providers (espeak/piper) have
    no language switch.
    """
    if not language:
        return True
    code = str(language).strip().lower()
    # Strip enum-ish / locale forms: "Language.EN", "en-US", "en_us" -> "en".
    code = code.split(".")[-1].split("-")[0].split("_")[0][:2]
    if not code:
        return True
    if code in _NON_ENGLISH_SUPPORTED:
        return False
    return code == "en"


def _is_foreign_letter_or_mark(ch: str) -> bool:
    """A letter whose script is not Latin, or a combining mark.

    Combining marks (Mn/Mc/Me) attach to a base letter; inside a foreign run
    they belong to the foreign script (e.g. the Kannada anusvara in
    ``ಅಂತ``). A run is only stripped when it contains at least one
    foreign LETTER, so a stray accent next to Latin text is never removed alone.
    """
    cat = unicodedata.category(ch)
    if cat[0] == "L":
        try:
            name = unicodedata.name(ch)
        except ValueError:
            # Unnamed letter -> not Latin, treat as foreign.
            return True
        return not name.startswith("LATIN")
    return cat in ("Mn", "Mc", "Me")


def _strip_foreign_script_runs(text: str) -> tuple[str, list[str]]:
    """Remove maximal runs of non-Latin-script letters (+ their marks).

    Each removed run is replaced by a single space so adjacent English words
    are never glued together. Returns ``(cleaned, removed_runs)``.
    """
    out: list[str] = []
    removed: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if _is_foreign_letter_or_mark(text[i]):
            j = i
            has_letter = False
            while j < n and _is_foreign_letter_or_mark(text[j]):
                if unicodedata.category(text[j])[0] == "L":
                    has_letter = True
                j += 1
            run = text[i:j]
            if has_letter:
                removed.append(run)
                out.append(" ")
            else:
                # A run of only combining marks with no foreign base letter --
                # leave it; it likely decorates Latin text.
                out.append(run)
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out), removed


def _strip_tool_tokens(text: str) -> tuple[str, list[str]]:
    """Remove leaked phone tool/control tokens (and their ``to=`` syntax)."""
    removed: list[str] = []

    def _capture(match: re.Match[str]) -> str:
        removed.append(match.group(0))
        return " "

    cleaned = _TOOL_TOKEN_RE.sub(_capture, text)
    return cleaned, removed


def sanitize_tts_text(text: str, *, language: str | None = "en") -> tuple[str, StripReport]:
    """Sanitize model text before it reaches the phone TTS voice.

    Removes leaked tool/control tokens always, and non-Latin-script letter runs
    when the active TTS ``language`` is English. Collapses the whitespace left
    behind. Returns ``(clean_text, report)``. If nothing speakable remains the
    clean text is ``""`` (the caller should then speak nothing).

    Args:
        text: Raw text the model wants spoken.
        language: Active/expected TTS language code (e.g. "en", "en-us", "zh",
            or a Pipecat ``Language`` value). Determines whether foreign-script
            stripping applies. Defaults to English.
    """
    report = StripReport(language=str(language or "en"))
    if not text:
        return "", report

    cleaned = text

    # (a) Foreign-script letters -- only when speaking English.
    if is_english_language(language):
        cleaned, foreign = _strip_foreign_script_runs(cleaned)
        report.foreign_runs = foreign

    # (b) Leaked tool/control tokens -- always.
    cleaned, tokens = _strip_tool_tokens(cleaned)
    report.tool_tokens = tokens

    if report.stripped_anything:
        # Collapse the gaps the removals left behind, but only touch runs of
        # whitespace -- preserve single spaces that carry prosody.
        cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()

    return cleaned, report
