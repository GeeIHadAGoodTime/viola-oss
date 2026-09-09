"""Pre-TTS AI identity watchdog for phone calls."""

from __future__ import annotations

import re
from typing import Any

from core.logging_config import get_logger
from telephony.call_context import truthful_identity_answer

logger = get_logger(__name__)

try:
    from pipecat.frames.frames import (
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
        TextFrame,
    )
    from pipecat.processors.frame_processor import FrameProcessor

    PIPECAT_AVAILABLE = True
except ImportError:  # pragma: no cover - Pipecat is optional in some test envs
    PIPECAT_AVAILABLE = False
    FrameProcessor = object  # type: ignore[misc, assignment] # PHONE-01: Pipecat is optional locally.
    LLMFullResponseEndFrame = None  # type: ignore[assignment] # PHONE-01: Pipecat is optional locally.
    LLMFullResponseStartFrame = None  # type: ignore[assignment] # PHONE-01: Pipecat is optional locally.
    LLMTextFrame = None  # type: ignore[assignment] # PHONE-01: Pipecat is optional locally.
    TextFrame = None  # type: ignore[assignment] # PHONE-01: Pipecat is optional locally.


_SPACE_RE = re.compile(r"\s+")
_PUNCTUATION_RE = re.compile(r"[^a-z0-9]+")

# Contraction repair MUST run before punctuation stripping. `_PUNCTUATION_RE`
# turns every apostrophe into a space, so "isn't" would become "isn t" and the
# word `not` -- which every denial pattern below requires -- disappears
# entirely. That silently blinded the whole n't family ("isn't a bot", "ain't a
# robot", "that isn't automated"). Rewriting the suffix to a free-standing
# "not" first makes a contraction normalize exactly like its expanded spelling.
_NT_CONTRACTION_RE = re.compile(r"n['’ʼ´`]t\b")

# Shared vocabulary so every identity phrasing recognises the same nouns.
# "human being" must precede "human" -- alternation is first-match, and a bare
# "human" would consume the head of "human being" and then fail the trailing
# word boundary.
_IDENTITY_NOUNS = (
    r"(?:robot|bot|ai|a i|artificial intelligence|machine|recording|"
    r"automated assistant|automated system|automated|human being|human|person)"
)
# Adjectives a recipient stacks in front of the noun: "a real live person",
# "an actual human". Previously only a handful of these were spelled out as
# whole nouns ("real person", "real live person", "live person"), so "an actual
# human" and "a real live human being" fell through.
_IDENTITY_ADJECTIVES = r"(?:real |live |actual |real live )?"
_IDENTITY_NOUN_PHRASE = _IDENTITY_ADJECTIVES + _IDENTITY_NOUNS

# The tag-question shape drops `machine` and `recording` for the same reason
# the subject-less denials below do: statement-first word order puts the noun
# next to ordinary business speech, so "that's a machine wash only, correct?"
# reads as an identity question and arms a disclosure nobody asked for. Both
# words keep full coverage through the interrogative-first shapes above, which
# are anchored on "are you" / "is this" and cannot collide that way.
_TAG_IDENTITY_NOUNS = (
    r"(?:robot|bot|ai|a i|artificial intelligence|"
    r"automated assistant|automated system|automated|human being|human|person)"
)
_TAG_IDENTITY_NOUN_PHRASE = _IDENTITY_ADJECTIVES + _TAG_IDENTITY_NOUNS

# Function words that can sit between an identity noun and a clause boundary
# without turning that noun into a compound MODIFIER. A real denial/question
# ends its clause on the identity noun ("no robot, I promise", "a bot, right?");
# a compound keeps going into the head noun it modifies ("no bot FEES", "a bot
# FEE thing, right?"). None of these words can be a compound head, so a run of
# them still marks the clause end. Shared by the tag-question tail here and by
# the subject-less denial clause-end guard below, so both refuse a content-word
# head while allowing the genuine forms.
_CLAUSE_TAIL_WORD_ALTERNATION = (
    r"i|we|you|he|she|they|it|this|that|there|and|but|so|or|"
    r"here|now|really|honestly|truly|ok|okay|sir|maam|ma|just|please|"
    r"no|yes|either|though|anyway|at all"
)
# Tag-question clause-end guard. The identity noun must sit at a clause end
# right before the tag token ("a bot, right?"), reached across nothing but the
# clause-tail function words above ("a bot though, right?"). A CONTENT word after
# the noun means it modifies a head noun ("a bot FEE thing, right?", "a person's
# NAME, right?", "a human resources CONTACT, right?", "a person OF interest,
# correct?") -- the tag can never be reached across it, so the benign utterance
# is refused. This is the same compound-modifier hazard the denial side already
# closed; the original tag tail `\b.{0,24}?\b(?:right|...)` allowed arbitrary
# text before the tag and left the hazard open on the tag side (#4217).
_TAG_CLAUSE_END = r"(?:\s+(?:" + _CLAUSE_TAIL_WORD_ALTERNATION + r"))*\s+"

_IDENTITY_QUESTION_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bare you (?:really |actually |just )?(?:a |an )?" + _IDENTITY_NOUN_PHRASE + r"\b",
        r"\bis this (?:really |actually |just )?(?:a |an )?" + _IDENTITY_NOUN_PHRASE + r"\b",
        r"\bam i (?:talking|speaking) (?:to|with) (?:a |an )?" + _IDENTITY_NOUN_PHRASE + r"\b",
        r"\bare you (?:really |actually )?real\b",
        r"\bare you live\b",
        r"\bis this (?:really |actually )?real\b",
        r"\bis this live\b",
        r"\bwho (?:are you|am i (?:talking|speaking) (?:to|with))\b",
        r"\bwhat are you\b",
        # Tag questions ask the same thing with the statement first: "this is a
        # bot, right?", "you're not a bot, are you?". Only the interrogative-
        # first word order was recognised before, so the most casual phrasing a
        # recipient actually uses never armed the reactive disclosure.
        # "that s" / "it s" are what the normalizer leaves of "that's" / "it's";
        # the apostrophe is stripped, so the expanded spellings alone miss them.
        r"\b(?:this is|that is|that s|it s|you (?:are|re)) (?:not )?(?:a |an )?"
        + _TAG_IDENTITY_NOUN_PHRASE
        + r"\b"
        + _TAG_CLAUSE_END
        + r"(?:right|correct|are you|are not you|is it|is not it|yes or no)\b",
    )
)

_AI_AFFIRMATION_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bautomated assistant\b",
        r"\bautomated system\b",
        r"\bai assistant\b",
        r"\ba i assistant\b",
        r"\bartificial intelligence\b",
        r"\bi (?:am|m) (?:an? )?(?:ai|a i|bot|robot|automated|machine|virtual assistant|digital assistant)\b",
        r"\bthis is (?:an? )?(?:ai|a i|bot|robot|automated assistant|automated system)\b",
    )
)

# Nouns a human-claim reaches for. "human being" leads for the same
# first-match reason as `_IDENTITY_NOUNS` above.
_HUMAN_NOUNS = r"(?:person|human being|human)"
_DENIED_AI_NOUNS = r"(?:bot|robot|ai|a i|automated assistant|automated system|machine|recording)"
# Subject-less denials ("not a bot", "no robot here") deliberately drop
# `machine` and `recording` from the noun set. Those two words carry ordinary
# business meanings, so an unanchored match reads "that's not a recording I
# have access to" as a human claim and makes Viola blurt an unprompted AI
# disclosure mid-call. Both words stay in `_DENIED_AI_NOUNS`, which is only
# ever reached through an explicit "I am not ..." subject.
_DENIED_AI_NOUNS_UNANCHORED = r"(?:bot|robot|ai|a i|artificial intelligence|automated assistant|automated system)"

# Dropping `machine`/`recording` was necessary but not sufficient: with no
# subject to anchor on, "no <noun>" and "not a <noun>" also match when the noun
# is a COMPOUND MODIFIER rather than the thing being denied -- "no bot fees",
# "no robot vacuum in the listing", "no automated system on our end", "we
# aren't a bot-friendly venue". Every one of those is ordinary errand speech,
# and every one would have made Viola blurt an unprompted AI disclosure
# mid-call, which is the exact thing reactive-only disclosure forbids.
#
# A real denial ends its clause on the noun ("no robot, I promise", "there's
# no robot here", "this isn't a bot"); a compound keeps going into the head
# noun it modifies. Normalization has already destroyed the comma, so the
# clause end is approximated by end-of-utterance or a following function word
# that cannot be a compound head. Content words after the noun mean modifier,
# so the match is refused.
# A following function word cannot be a compound head, so it marks the clause
# end no matter how much text follows. This half of the guard is safe to apply
# to a partially streamed utterance.
_DENIAL_CLAUSE_TAIL_WORDS = r"(?=\s+(?:" + _CLAUSE_TAIL_WORD_ALTERNATION + r")\b)"
# End-of-text is only a clause end once the utterance is COMPLETE. Mid-stream it
# is just where the current chunk stopped: `process_frame` re-tests the whole
# accumulated text after every frame, so "There's no robot" tests true a beat
# before " vacuum" arrives, and `_stream_guard_correction_emitted` latches the
# correction so the later text can never take it back. Detection therefore runs
# without this half while the response is still streaming, and once more with
# it at LLMFullResponseEndFrame, when the text really has ended.
_DENIAL_CLAUSE_END_COMPLETE = r"(?=\s+(?:" + _CLAUSE_TAIL_WORD_ALTERNATION + r")\b|\s*$)"


def _denial_patterns(clause_end: str) -> tuple[re.Pattern[str], ...]:
    return tuple(
        re.compile(pattern)
        for pattern in (
            # Reuse `_IDENTITY_ADJECTIVES` (the question side already does) so the
            # double-adjective "real live" idiom is caught here too: "I'm a real
            # live person.", "I'm a real live human being." The old hard-coded
            # `(?:real |live |actual )?` matched only ONE adjective, so a stock
            # human-claim phrasing slipped past the denial guard (#4217 N1).
            r"\bi (?:am|m) (?:a |an )?" + _IDENTITY_ADJECTIVES + _HUMAN_NOUNS + r"\b",
            r"\bi (?:am|m) human\b",
            r"\bi (?:am|m) (?:not )?(?:just )?(?:a |an )?(?:real |live )?person calling\b",
            r"\byou (?:are|re) (?:talking|speaking) (?:to|with) (?:a |an )?"
            + _IDENTITY_ADJECTIVES
            + _HUMAN_NOUNS
            + r"\b",
            r"\bthis is (?:a |an )?" + _IDENTITY_ADJECTIVES + _HUMAN_NOUNS + r"\b",
            r"\bno i (?:am|m) not (?:a |an )?" + _DENIED_AI_NOUNS + r"\b",
            r"\bi (?:am|m) not (?:a |an )?" + _DENIED_AI_NOUNS + r"\b",
            # "You aren't talking to a machine." -- denies the AI noun through the
            # listener rather than the speaker, so neither the "I am not ..." nor
            # the bare "not a ..." shape reaches it.
            r"\byou (?:are|re) not (?:talking|speaking) (?:to|with) (?:a |an )?" + _DENIED_AI_NOUNS + r"\b",
            r"\bnot (?:a |an )?" + _DENIED_AI_NOUNS_UNANCHORED + r"\b" + clause_end,
            # "No robot, I promise." / "There's no robot here." -- a denial that
            # never spells out "not", so every anchored pattern above missed it.
            r"\bno (?:a |an )?" + _DENIED_AI_NOUNS_UNANCHORED + r"\b" + clause_end,
        )
    )


# The complete-utterance set is the default everywhere; the streaming path is
# the one caller that must use the partial set, because its text is still
# arriving.
_HUMAN_OR_AI_DENIAL_PATTERNS = _denial_patterns(_DENIAL_CLAUSE_END_COMPLETE)
_HUMAN_OR_AI_DENIAL_PATTERNS_STREAMING = _denial_patterns(_DENIAL_CLAUSE_TAIL_WORDS)


def _normalize(text: str) -> str:
    normalized = str(text or "").lower()
    normalized = _NT_CONTRACTION_RE.sub(" not", normalized)
    normalized = _PUNCTUATION_RE.sub(" ", normalized)
    return _SPACE_RE.sub(" ", normalized).strip()


def is_identity_question(text: str) -> bool:
    """Return True when recipient text asks whether Viola is AI, automated, or human."""

    normalized = _normalize(text)
    return any(pattern.search(normalized) for pattern in _IDENTITY_QUESTION_PATTERNS)


def affirms_ai_identity(text: str) -> bool:
    """Return True when outbound text truthfully identifies Viola as AI or automated."""

    normalized = _normalize(text)
    return any(pattern.search(normalized) for pattern in _AI_AFFIRMATION_PATTERNS)


def denies_ai_or_claims_human(text: str, *, utterance_complete: bool = True) -> bool:
    """Return True when outbound text claims human identity or denies AI/automation.

    Set ``utterance_complete=False`` when ``text`` is a partially streamed
    response. The subject-less denial shapes accept end-of-text as a clause end,
    which is only true once the utterance has actually ended -- mid-stream it is
    just where the current chunk stopped, and "There's no robot" would be
    corrected a beat before " vacuum" arrived.
    """

    normalized = _normalize(text)
    patterns = _HUMAN_OR_AI_DENIAL_PATTERNS if utterance_complete else _HUMAN_OR_AI_DENIAL_PATTERNS_STREAMING
    return any(pattern.search(normalized) for pattern in patterns)


class AIIdentityWatchdog(FrameProcessor if PIPECAT_AVAILABLE else object):  # type: ignore[misc] # PHONE-01: Pipecat-optional base class.
    """Monitor outbound phone text so AI identity claims are enforced at runtime.

    The enforcement guarantee is **language-agnostic and proactive**, not a
    word-list. The recipient may speak any language, so the runtime cannot rely
    on matching an English identity question or an English human-claim to know
    when to disclose (SEC-059). Instead the watchdog deterministically prepends
    the truthful automated-assistant sentence to Viola's FIRST outbound turn
    whenever proactive disclosure is required — the model then carries it into
    the rest of the opening in whatever language it is speaking. The English
    regex paths below remain only as best-effort defense-in-depth for an
    English-language call that somehow reaches a human-claim; they are never the
    sole guarantee, so they cannot "fail open" on a non-English call.

    Latency rule: do not hold streamed speech while a partial phrase is still
    ambiguous. Text flows to TTS immediately; if the accumulated stream later
    contains a human claim or AI denial, the watchdog immediately emits the
    truthful automated-assistant correction after the offending text.
    """

    def __init__(self, *, caller_name: str, proactive_disclosure: bool = False) -> None:
        if PIPECAT_AVAILABLE:
            super().__init__(name="ai_identity_watchdog")
        self._truthful_identity_answer = truthful_identity_answer(caller_name)
        self._identity_question_pending = False
        # Language-agnostic primary control: the first outbound turn must carry
        # the AI disclosure. This is independent of any recipient transcript and
        # of the call language, so it holds for non-English calls too.
        self._proactive_disclosure_required = bool(proactive_disclosure)
        self._stream_guard_text_parts: list[str] = []
        self._stream_guard_text_emitted = False
        self._stream_guard_correction_emitted = False

    @property
    def truthful_identity_answer(self) -> str:
        return self._truthful_identity_answer

    @property
    def identity_question_pending(self) -> bool:
        return self._identity_question_pending

    @property
    def proactive_disclosure_required(self) -> bool:
        return self._proactive_disclosure_required

    def observe_recipient_transcript(self, text: str) -> None:
        if is_identity_question(text):
            self._identity_question_pending = True
            logger.info("Phone AI identity watchdog observed recipient identity question")

    async def process_frame(self, frame: Any, direction: Any) -> None:
        if not PIPECAT_AVAILABLE:
            return

        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            self._reset_stream_guard()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseEndFrame):
            if not self._stream_guard_text_emitted and self._identity_question_pending:
                self._identity_question_pending = False
                logger.warning("Phone AI identity watchdog emitted truthful identity answer for empty outbound turn")
                await self.push_frame(LLMTextFrame(self._truthful_identity_answer), direction)
            elif self._stream_guard_text_emitted and not self._stream_guard_correction_emitted:
                # The streaming pass ran against a still-arriving utterance, so it
                # could not treat end-of-text as a clause end. Now the text really
                # has ended, so the full-strength patterns get their one look --
                # this is what catches a denial that ends the turn ("no, this
                # isn't a bot") without holding a single frame back from TTS.
                if denies_ai_or_claims_human("".join(self._stream_guard_text_parts)):
                    self._identity_question_pending = False
                    self._proactive_disclosure_required = False
                    self._stream_guard_correction_emitted = True
                    logger.warning("Phone AI identity watchdog corrected completed human/AI-denial claim after TTS")
                    await self.push_frame(LLMTextFrame(self._truthful_identity_answer), direction)
            self._reset_stream_guard()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TextFrame):
            await self._process_streaming_text_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

    async def _process_streaming_text_frame(self, frame: Any, direction: Any) -> None:
        text = str(frame.text or "")
        if not text:
            await self.push_frame(frame, direction)
            return

        if self._should_prepend_truthful_identity(text):
            self._identity_question_pending = False
            self._proactive_disclosure_required = False
            logger.warning("Phone AI identity watchdog streamed truthful identity answer before outbound text")
            await self.push_frame(LLMTextFrame(self._truthful_identity_answer), direction)

        self._stream_guard_text_parts.append(text)
        self._stream_guard_text_emitted = True
        await self.push_frame(frame, direction)

        if self._stream_guard_correction_emitted:
            return

        streamed_text = "".join(self._stream_guard_text_parts)
        if denies_ai_or_claims_human(streamed_text, utterance_complete=False):
            self._identity_question_pending = False
            self._proactive_disclosure_required = False
            self._stream_guard_correction_emitted = True
            logger.warning("Phone AI identity watchdog corrected streamed human/AI-denial claim after TTS")
            await self.push_frame(LLMTextFrame(self._truthful_identity_answer), direction)

    def _should_prepend_truthful_identity(self, first_text: str) -> bool:
        needs_disclosure = self._proactive_disclosure_required or self._identity_question_pending
        if not needs_disclosure:
            return False
        if self._already_discloses(first_text):
            self._identity_question_pending = False
            self._proactive_disclosure_required = False
            return False
        return bool(str(first_text or "").strip())

    def _already_discloses(self, outbound_text: str) -> bool:
        """Language-agnostic check that the disclosure is already present.

        Matches when the watchdog's own truthful sentence (which it controls in
        the call's language) is already in the text, OR — best-effort, English
        only — the model emitted a recognizable AI affirmation. The truthful
        sentence comparison is what makes this work without a per-language word
        list: the watchdog injected that exact sentence, so detecting it back is
        language-independent.
        """
        normalized = _normalize(outbound_text)
        if _normalize(self._truthful_identity_answer) and _normalize(self._truthful_identity_answer) in normalized:
            return True
        return affirms_ai_identity(outbound_text)

    def _reset_stream_guard(self) -> None:
        self._stream_guard_text_parts = []
        self._stream_guard_text_emitted = False
        self._stream_guard_correction_emitted = False
