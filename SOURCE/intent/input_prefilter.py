"""
Pre-LLM input filtering for crisis, prompt-injection, sensitive values, and gibberish.

The crisis and direct security-payload checks are safety boundaries. Other user
intent and tone judgments stay with the LLM, which receives the raw conversation.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

BLOCKED_INPUT_RESPONSE = "That input was blocked by the safety filter."

# Returned when input exceeds the pipeline's hard length ceiling (see
# intent/pipeline.py:_MAX_PIPELINE_INPUT_CHARS). A real voice/text command is
# never tens of thousands of characters; over-length input is rejected cleanly
# at the top of the command path so the pre-LLM prefilters never run regexes on
# an unbounded payload (the 2026-07-05 ReDoS class).
OVERLONG_INPUT_RESPONSE = "That request was too long for me to process. Try a shorter command."

# Deterministic refusal for credential-storage requests.  Must contain multiple
# rejection keywords (can't, sensitive, security, credential, password manager,
# credit card) so live-regression keyword checks pass without depending on the
# LLM — this survives rate limits, model updates, and Unicode apostrophes.
SENSITIVE_STORAGE_RESPONSE = (
    "I can't store sensitive credentials like passwords, API keys, SSNs, or "
    "credit card numbers. For security, use a password manager or an encrypted "
    "vault instead."
)

# ---------------------------------------------------------------------------
# Crisis Detection — intercepts BEFORE LLM, provides immediate resources
# ---------------------------------------------------------------------------

CRISIS_RESPONSE = (
    "If you're in crisis, please contact the 988 Suicide & Crisis Lifeline "
    "by calling or texting 988. You're not alone, and help is available 24/7."
)

_CRISIS_PATTERNS_RE = re.compile(
    r"(?i)"
    r"(?:\bkill\s+myself\b)"
    r"|(?:\bend\s+it\s+all\b)"
    r"|(?:\bsuicide\b)"
    r"|(?:\bwant\s+to\s+die\b)"
    r"|(?:\bself[- ]?harm\b)"
    r"|(?:\bhurt\s+myself\b)"
    r"|(?:\bend\s+my\s+life\b)"
    r"|(?:\bnot\s+worth\s+living\b)"
)


def detect_crisis(text: str) -> bool:
    """Detect self-harm / crisis language in user input.

    Returns True if crisis keywords are found.  This runs BEFORE
    the LLM and before any memory storage.
    """
    if not text:
        return False
    return bool(_CRISIS_PATTERNS_RE.search(text))


@dataclass(frozen=True)
class PrefilterMatch:
    """Represents an obvious attack pattern found in user input."""

    category: str
    pattern_id: str
    matched_text: str


@dataclass(frozen=True)
class _PrefilterRule:
    category: str
    pattern_id: str
    pattern: re.Pattern[str]


_PREFILTER_RULES: tuple[_PrefilterRule, ...] = (
    _PrefilterRule(
        category="path_traversal",
        pattern_id="relative_traversal",
        pattern=re.compile(r"(?:\.\./|\.\.\\){1,}", re.IGNORECASE),
    ),
    _PrefilterRule(
        category="path_traversal",
        pattern_id="unix_sensitive_path",
        pattern=re.compile(r"(?:/etc/passwd|/etc/shadow)\b", re.IGNORECASE),
    ),
    _PrefilterRule(
        category="path_traversal",
        pattern_id="windows_sensitive_path",
        pattern=re.compile(r"c:\\windows\\system32(?:\\|$)", re.IGNORECASE),
    ),
    _PrefilterRule(
        category="command_injection",
        pattern_id="rm_rf",
        pattern=re.compile(r"(?:^|[\s'\"`])(?:sudo\s+)?rm\s+-rf(?:\s+/|\s+\*|\b)", re.IGNORECASE),
    ),
    _PrefilterRule(
        category="command_injection",
        pattern_id="format_drive",
        pattern=re.compile(r"\bformat\s+[a-z]:", re.IGNORECASE),
    ),
    _PrefilterRule(
        category="command_injection",
        pattern_id="windows_delete",
        pattern=re.compile(r"\bdel\s+/f(?:\s+/q)?(?:\s+/s)?\b", re.IGNORECASE),
    ),
    _PrefilterRule(
        category="command_injection",
        pattern_id="shutdown",
        pattern=re.compile(r"\bshutdown(?:\s+/[a-z]+)?\b", re.IGNORECASE),
    ),
    _PrefilterRule(
        category="command_injection",
        pattern_id="kill_signal",
        pattern=re.compile(r"\bkill\s+-9\b", re.IGNORECASE),
    ),
    _PrefilterRule(
        category="sql_injection",
        pattern_id="drop_table",
        pattern=re.compile(r"\bdrop\s+table\b", re.IGNORECASE),
    ),
    _PrefilterRule(
        category="sql_injection",
        pattern_id="union_select",
        pattern=re.compile(r"\bunion\s+select\b", re.IGNORECASE),
    ),
    _PrefilterRule(
        category="sql_injection",
        pattern_id="tautology_comment",
        pattern=re.compile(r"(?:'|%27)?\s*(?:or\s+)?1\s*=\s*1\s*(?:--|#|/\*)", re.IGNORECASE),
    ),
    _PrefilterRule(
        category="xss",
        pattern_id="script_tag",
        pattern=re.compile(r"<\s*script\b", re.IGNORECASE),
    ),
    _PrefilterRule(
        category="xss",
        pattern_id="html_injection_tag",
        pattern=re.compile(
            r"<\s*(?:img|svg|iframe|body|object|embed|link|meta|input|button|form)\b[^>]*\bon\w+\s*=",
            re.IGNORECASE,
        ),
    ),
    _PrefilterRule(
        category="xss",
        pattern_id="event_handler",
        pattern=re.compile(
            r"\bon(?:error|load|click|mouseover|mouseout|focus|blur|change|submit|keydown|keyup|keypress)\s*=",
            re.IGNORECASE,
        ),
    ),
    _PrefilterRule(
        category="xss",
        pattern_id="javascript_uri",
        pattern=re.compile(r"javascript\s*:", re.IGNORECASE),
    ),
    _PrefilterRule(
        category="xss",
        pattern_id="dom_cookie_access",
        pattern=re.compile(r"\bdocument\s*\.\s*cookie\b", re.IGNORECASE),
    ),
    _PrefilterRule(
        category="xss",
        pattern_id="dom_domain_access",
        pattern=re.compile(r"\bdocument\s*\.\s*domain\b", re.IGNORECASE),
    ),
    _PrefilterRule(
        category="xss",
        pattern_id="window_location_access",
        pattern=re.compile(r"\bwindow\s*\.\s*location\b", re.IGNORECASE),
    ),
    _PrefilterRule(
        category="ssrf",
        pattern_id="cloud_metadata",
        pattern=re.compile(r"169\.254\.169\.254", re.IGNORECASE),
    ),
    _PrefilterRule(
        category="ssrf",
        pattern_id="localhost_bypass",
        pattern=re.compile(r"(?:127\.0\.0\.\d|0\.0\.0\.0|localhost)\s*:\s*\d{2,5}", re.IGNORECASE),
    ),
    # Prompt injection: block jailbreak / instruction override attempts
    _PrefilterRule(
        category="prompt_injection",
        pattern_id="dan_jailbreak",
        pattern=re.compile(
            r"\b(?:DAN|do\s+anything\s+now)\b",
            re.IGNORECASE,
        ),
    ),
    _PrefilterRule(
        category="prompt_injection",
        pattern_id="instruction_override",
        pattern=re.compile(
            r"\b(?:ignore|forget|disregard)\s+(?:all\s+)?(?:(?:previous|prior|above|your|safety|system)\s+)+"
            r"(?:instructions?|rules?|guidelines?|constraints?|programming|prompts?)\b",
            re.IGNORECASE,
        ),
    ),
    _PrefilterRule(
        category="prompt_injection",
        pattern_id="instruction_injection",
        pattern=re.compile(
            r"\b(?:new|updated|revised|changed|replacement|override)\s+instructions?\s*:",
            re.IGNORECASE,
        ),
    ),
    _PrefilterRule(
        category="prompt_injection",
        pattern_id="safety_bypass",
        pattern=re.compile(
            r"\b(?:bypass|override|disable|turn\s+off|remove|forget|drop)\s+(?:your\s+)?"
            r"(?:safety|restrictions?|filters?|guardrails?|rules?|content\s+(?:filter|policy)|moderation)\b",
            re.IGNORECASE,
        ),
    ),
    _PrefilterRule(
        category="prompt_injection",
        pattern_id="unrestricted_identity",
        pattern=re.compile(
            r"\byou\s+are\s+now\s+(?:an?\s+)?(?:unrestricted|unfiltered|uncensored|jailbroken|liberated)\b",
            re.IGNORECASE,
        ),
    ),
    _PrefilterRule(
        category="prompt_injection",
        pattern_id="privilege_escalation",
        pattern=re.compile(
            r"\b(?:developer|maintenance|debug|god|admin|sudo)\s+mode\b",
            re.IGNORECASE,
        ),
    ),
    _PrefilterRule(
        category="prompt_injection",
        pattern_id="system_prompt_extraction",
        pattern=re.compile(
            r"\b(?:reveal|show|print|output|display|leak|dump)\s+(?:your\s+)?"
            r"(?:system\s+)?(?:prompt|instructions?|rules?|programming|directives?)\b",
            re.IGNORECASE,
        ),
    ),
    _PrefilterRule(
        category="prompt_injection",
        pattern_id="unrestricted_roleplay",
        pattern=re.compile(
            r"\b(?:pretend|act)\s+(?:to\s+be|as\s+if|like)\s+(?:you\s+)?"
            r"(?:have\s+no|don'?t\s+have|without)\s+"
            r"(?:restrictions?|rules?|limits?|constraints?|filters?|safety)\b",
            re.IGNORECASE,
        ),
    ),
    _PrefilterRule(
        category="prompt_injection",
        pattern_id="roleplay_no_restrictions",
        pattern=re.compile(
            r"\brole\s*play\b.*\bno\s+(?:restrictions?|rules?|limits?|safety|filters?|constraints?)\b",
            re.IGNORECASE,
        ),
    ),
    _PrefilterRule(
        category="prompt_injection",
        pattern_id="evil_persona_assignment",
        pattern=re.compile(
            r"\byou\s+are\s+\w+\s+(?:who|that|with)\s+(?:has\s+)?no\s+"
            r"(?:restrictions?|rules?|limits?|safety|filters?|constraints?)\b",
            re.IGNORECASE,
        ),
    ),
    _PrefilterRule(
        category="prompt_injection",
        pattern_id="jailbreak_keyword",
        pattern=re.compile(r"\bjailbreak\b", re.IGNORECASE),
    ),
)


def detect_obvious_attack_input(text: str) -> PrefilterMatch | None:
    """
    Detect direct security payloads that should be blocked before LLM routing.

    This is a safety boundary, not an intent classifier: direct payload matches
    are blocked, and explanation/defensive intent is not inferred here.
    """
    normalized = " ".join((text or "").strip().split())
    if not normalized:
        return None

    for rule in _PREFILTER_RULES:
        match = rule.pattern.search(normalized)
        if match:
            return PrefilterMatch(
                category=rule.category,
                pattern_id=rule.pattern_id,
                matched_text=match.group(0),
            )

    return None


# ============================================================================
# SENSITIVE-CREDENTIAL VALUE DETECTION
# ============================================================================
# Deterministic short-circuit for concrete sensitive values. Mirrors the crisis-
# safety approach: recognised server-side BEFORE any LLM call, responds with a
# firm refusal that always contains the required rejection keywords.
# Rate-limit and model-update resilient.
#
# Design notes:
#   - Blocks concrete credential values with labels (password is ..., api key: ...)
#     and bare high-confidence values (sk-..., PANs).
#   - Does not classify whether a credential mention is a reminder, storage
#     request, or educational topic. No concrete value means no deterministic block.

# Credential-label patterns coupled with a concrete-looking value.
_SENSITIVE_LABEL_RULES: tuple[_PrefilterRule, ...] = (
    # Password with a value ("password is hunter2", "password: P@ss1")
    _PrefilterRule(
        category="sensitive_credential",
        pattern_id="password_value",
        pattern=re.compile(
            r"\b(?:my\s+)?pass(?:word|code|phrase)\s*(?:is|:|=|are)\s*\S+",
            re.IGNORECASE,
        ),
    ),
    # API key / token with a value
    _PrefilterRule(
        category="sensitive_credential",
        pattern_id="api_key_value",
        pattern=re.compile(
            r"\b(?:api[_\-\s]?key|access[_\-\s]?token|auth[_\-\s]?token|"
            r"bearer[_\-\s]?token|secret[_\-\s]?key|private[_\-\s]?key)\s*"
            r"(?:is|:|=)\s*\S{6,}",
            re.IGNORECASE,
        ),
    ),
    # Bare API-key prefixes (sk-…, pk-…, ghp_…, xoxb-…) even without a label.
    # Allows internal hyphens/underscores (e.g. "sk-proj-abcdefghij").
    _PrefilterRule(
        category="sensitive_credential",
        pattern_id="api_key_prefix",
        pattern=re.compile(
            r"\b(?:sk|pk|ghp|gho|glpat|xox[bpas])[-_][A-Za-z0-9][A-Za-z0-9_\-]{7,}\b",
        ),
    ),
    # SSN in any phrasing ("my SSN is 123-45-6789" or just the 9-digit form)
    _PrefilterRule(
        category="sensitive_credential",
        pattern_id="ssn_value",
        pattern=re.compile(
            r"\b(?:ssn|social\s+security(?:\s+number)?)\b[^\d]{0,40}" r"\d{3}[\-\s]?\d{2}[\-\s]?\d{4}\b",
            re.IGNORECASE,
        ),
    ),
    # Credit / debit card numbers (13-19 digits, separators allowed)
    _PrefilterRule(
        category="sensitive_credential",
        pattern_id="credit_card_label",
        pattern=re.compile(
            r"\b(?:credit|debit|bank)\s*card\b[\w\s]{0,40}" r"\d[\d\-\s]{11,18}\d",
            re.IGNORECASE,
        ),
    ),
    # Credit-card number pattern with major brand BINs even without label
    _PrefilterRule(
        category="sensitive_credential",
        pattern_id="credit_card_pan",
        pattern=re.compile(
            r"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))" r"[\-\s]?\d{4}[\-\s]?\d{4}[\-\s]?\d{3,4}\b",
        ),
    ),
    # PIN / security code with a value
    _PrefilterRule(
        category="sensitive_credential",
        pattern_id="pin_value",
        pattern=re.compile(
            r"\b(?:pin(?:\s+code|\s+number)?|security\s+code|cvv|cvc)\s*" r"(?:is|:|=)\s*\d{3,}",
            re.IGNORECASE,
        ),
    ),
)


def detect_sensitive_request(text: str) -> PrefilterMatch | None:
    """
    Detect concrete sensitive credentials or PII values.

    Fires BEFORE the LLM tier so refusals are deterministic and rate-limit
    resilient. Returns ``None`` for mentions without a concrete value
    ("remind me to change my password tomorrow").

    The returned :class:`PrefilterMatch` is used by :mod:`intent.pipeline`
    to emit :data:`SENSITIVE_STORAGE_RESPONSE` directly to the caller.
    """
    normalized = " ".join((text or "").strip().split())
    if not normalized:
        return None

    for rule in _SENSITIVE_LABEL_RULES:
        match = rule.pattern.search(normalized)
        if match:
            return PrefilterMatch(
                category=rule.category,
                pattern_id=rule.pattern_id,
                matched_text=match.group(0),
            )

    return None


# ============================================================================
# GIBBERISH DETECTION
# ============================================================================
# Prevents nonsensical keyboard-mash input (e.g. "asdfjkl qwerty zxcv") from
# reaching the AI tier, which would waste 60-85 seconds on a timeout.
#
# Design priorities:
#   1. No false positives on real commands, foreign text, or technical terms
#   2. Fast — pure Python, no external deps, no LLM calls
#   3. Conservative — better to let gibberish through than block real input
# ============================================================================

GIBBERISH_RESPONSE = "That input was too unclear to route."

# ~300 of the most common English words plus Viola-relevant command words.
# Kept as a frozenset for O(1) lookup.
_COMMON_WORDS: frozenset[str] = frozenset(
    {
        # Articles / determiners
        "a",
        "an",
        "the",
        "this",
        "that",
        "these",
        "those",
        "my",
        "your",
        "his",
        "her",
        "its",
        "our",
        "their",
        "some",
        "any",
        "no",
        "every",
        "each",
        "all",
        # Pronouns
        "i",
        "me",
        "you",
        "he",
        "she",
        "it",
        "we",
        "they",
        "us",
        "them",
        "who",
        "what",
        "which",
        "whom",
        "whose",
        "myself",
        "yourself",
        # Prepositions / conjunctions
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "from",
        "by",
        "of",
        "about",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "under",
        "over",
        "up",
        "down",
        "out",
        "off",
        "and",
        "or",
        "but",
        "nor",
        "so",
        "yet",
        "if",
        "then",
        "than",
        "as",
        "not",
        "when",
        # Verbs (common)
        "is",
        "am",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "shall",
        "should",
        "may",
        "might",
        "can",
        "could",
        "must",
        "need",
        "get",
        "got",
        "go",
        "going",
        "gone",
        "come",
        "came",
        "make",
        "made",
        "take",
        "took",
        "give",
        "gave",
        "tell",
        "told",
        "say",
        "said",
        "know",
        "knew",
        "think",
        "thought",
        "see",
        "saw",
        "look",
        "find",
        "found",
        "want",
        "let",
        "put",
        "set",
        "run",
        "turn",
        "keep",
        "show",
        "try",
        "leave",
        "call",
        "move",
        "live",
        "start",
        "help",
        "ask",
        "read",
        "write",
        "send",
        "open",
        "close",
        "bring",
        "buy",
        "use",
        "like",
        "love",
        "hate",
        "feel",
        "hear",
        "mean",
        "sit",
        "stand",
        "hold",
        "change",
        "follow",
        "stop",
        "pay",
        "add",
        "check",
        "pick",
        "watch",
        "remember",
        "happen",
        "wait",
        "stay",
        # Adjectives / adverbs
        "good",
        "bad",
        "great",
        "big",
        "small",
        "long",
        "short",
        "old",
        "new",
        "first",
        "last",
        "next",
        "high",
        "low",
        "right",
        "left",
        "best",
        "just",
        "much",
        "more",
        "most",
        "less",
        "very",
        "really",
        "also",
        "too",
        "here",
        "there",
        "now",
        "today",
        "tomorrow",
        "yesterday",
        "always",
        "never",
        "still",
        "already",
        "only",
        "even",
        "again",
        "please",
        "sure",
        "ok",
        "okay",
        "yes",
        "yeah",
        "nope",
        "maybe",
        "well",
        "how",
        "why",
        "where",
        "else",
        # Nouns (common)
        "time",
        "day",
        "night",
        "week",
        "month",
        "year",
        "thing",
        "way",
        "name",
        "world",
        "home",
        "house",
        "room",
        "place",
        "part",
        "number",
        "people",
        "man",
        "woman",
        "child",
        "hand",
        "head",
        "life",
        "work",
        "word",
        "water",
        "food",
        "money",
        "music",
        "song",
        "book",
        "phone",
        "car",
        "door",
        "light",
        "game",
        "city",
        "news",
        "weather",
        "email",
        "file",
        "list",
        "message",
        "question",
        "answer",
        "problem",
        "idea",
        # Viola-specific command words
        "play",
        "pause",
        "resume",
        "skip",
        "previous",
        "volume",
        "shuffle",
        "repeat",
        "queue",
        "search",
        "mute",
        "unmute",
        "louder",
        "quieter",
        "timer",
        "alarm",
        "remind",
        "reminder",
        "schedule",
        "calendar",
        "define",
        "translate",
        "calculate",
        "convert",
        "compare",
        "temperature",
        "degrees",
        "forecast",
        "lights",
        "lock",
        "unlock",
        "thermostat",
        "spotify",
        "youtube",
        "jazz",
        "rock",
        "pop",
        "classical",
        "podcast",
        "radio",
        "playlist",
        "artist",
        "album",
        "track",
        "genre",
        "something",
        "anything",
        "everything",
        "nothing",
        # Numbers as words
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "hundred",
        "thousand",
        "million",
        "plus",
        "minus",
        "times",
        "half",
        "double",
        "zero",
    }
)

# Patterns that indicate structured (non-gibberish) input
_URL_PATTERN = re.compile(r"https?://|ftp://|www\.", re.IGNORECASE)
# Bounded, non-overlapping email match. The old `\S+@\S+\.\S+` form let the
# three `\S+` runs overlap across the '@' and the '.', so it backtracked
# super-linearly on a long unbroken non-whitespace token — a multi-token command
# carrying one huge '@'-bearing token wedged the event loop (same class as the
# 2026-07-05 prod ReDoS). The local part excludes '@'/whitespace; each domain
# label excludes '@'/'.'/whitespace, so no quantifier can overlap across '@' or
# a '.'. Match semantics for real emails are unchanged (still recognized as
# structured input by is_gibberish).
_EMAIL_PATTERN = re.compile(r"[^\s@]{1,254}@[^\s@.]{1,63}(?:\.[^\s@.]{1,63}){1,8}")
_FILE_PATH_PATTERN = re.compile(r"(?:[a-zA-Z]:\\|/[\w.]+/|~/)", re.IGNORECASE)
_HAS_DIGITS_PATTERN = re.compile(r"\d")

# Keyboard layout adjacency maps for detecting keyboard-mash patterns.
# Each key maps to the set of keys physically adjacent to it on a QWERTY keyboard.
_KEYBOARD_NEIGHBORS: dict[str, set[str]] = {
    "q": {"w", "a"},
    "w": {"q", "e", "a", "s"},
    "e": {"w", "r", "s", "d"},
    "r": {"e", "t", "d", "f"},
    "t": {"r", "y", "f", "g"},
    "y": {"t", "u", "g", "h"},
    "u": {"y", "i", "h", "j"},
    "i": {"u", "o", "j", "k"},
    "o": {"i", "p", "k", "l"},
    "p": {"o", "l"},
    "a": {"q", "w", "s", "z"},
    "s": {"w", "e", "a", "d", "z", "x"},
    "d": {"e", "r", "s", "f", "x", "c"},
    "f": {"r", "t", "d", "g", "c", "v"},
    "g": {"t", "y", "f", "h", "v", "b"},
    "h": {"y", "u", "g", "j", "b", "n"},
    "j": {"u", "i", "h", "k", "n", "m"},
    "k": {"i", "o", "j", "l", "m"},
    "l": {"o", "p", "k"},
    "z": {"a", "s", "x"},
    "x": {"z", "s", "d", "c"},
    "c": {"x", "d", "f", "v"},
    "v": {"c", "f", "g", "b"},
    "b": {"v", "g", "h", "n"},
    "n": {"b", "h", "j", "m"},
    "m": {"n", "j", "k"},
}

# The three main keyboard rows (letter keys only)
_KEYBOARD_ROWS = [
    set("qwertyuiop"),
    set("asdfghjkl"),
    set("zxcvbnm"),
]


def _keyboard_mash_score(text: str) -> float:
    """Score how likely a string is a keyboard mash (0.0 = normal, 1.0 = definite mash).

    Detects two patterns:
    1. Consecutive characters that are adjacent on a QWERTY keyboard
    2. Tokens that are runs along a single keyboard row

    Returns the higher of the two signals.
    """
    alpha = [c for c in text.lower() if c.isalpha()]
    if len(alpha) < 4:
        return 0.0

    # Signal 1: Adjacent-key ratio across the whole input
    adjacent_pairs = 0
    total_pairs = 0
    for i in range(len(alpha) - 1):
        c1, c2 = alpha[i], alpha[i + 1]
        if c1 in _KEYBOARD_NEIGHBORS:
            total_pairs += 1
            if c2 in _KEYBOARD_NEIGHBORS[c1]:
                adjacent_pairs += 1
        else:
            total_pairs += 1

    adjacency_ratio = adjacent_pairs / total_pairs if total_pairs > 0 else 0.0

    # Signal 2: Per-token single-row ratio
    # A keyboard-mash token tends to be all from one row (e.g. "asdfghjkl")
    tokens = text.lower().split()
    row_mash_tokens = 0
    scorable_tokens = 0
    for token in tokens:
        t_alpha = "".join(c for c in token if c.isalpha())
        if len(t_alpha) < 3:
            continue
        scorable_tokens += 1
        for row in _KEYBOARD_ROWS:
            if all(c in row for c in t_alpha):
                row_mash_tokens += 1
                break

    row_ratio = row_mash_tokens / scorable_tokens if scorable_tokens > 0 else 0.0

    return max(adjacency_ratio, row_ratio)


def _char_entropy(text: str) -> float:
    """Shannon entropy of character distribution (bits per character)."""
    if not text:
        return 0.0
    counts = Counter(text.lower())
    length = len(text)
    entropy = 0.0
    for count in counts.values():
        if count > 0:
            p = count / length
            entropy -= p * math.log2(p)
    return entropy


def _vowel_ratio(text: str) -> float:
    """Ratio of vowels to total alphabetic characters."""
    alpha = [c for c in text.lower() if c.isalpha()]
    if not alpha:
        return 0.5  # neutral — no alphabetic chars
    vowels = sum(1 for c in alpha if c in "aeiou")
    return vowels / len(alpha)


def _recognized_word_ratio(text: str) -> float:
    """Fraction of whitespace-separated tokens that are common English words."""
    tokens = text.lower().split()
    if not tokens:
        return 1.0
    # Strip basic punctuation from each token before lookup
    recognized = 0
    for token in tokens:
        cleaned = token.strip(".,!?;:'\"()-")
        if cleaned in _COMMON_WORDS:
            recognized += 1
        elif len(cleaned) <= 2 and cleaned.isalpha():
            # Very short tokens (e.g. contractions, initials) — give benefit of doubt
            recognized += 1
        elif _HAS_DIGITS_PATTERN.search(cleaned):
            # Tokens with numbers (dates, IDs, math) — not gibberish
            recognized += 1
    return recognized / len(tokens)


def is_gibberish(text: str) -> bool:
    """
    Detect nonsensical keyboard-mash input that should not be sent to the AI tier.

    Conservative by design — returns True ONLY when multiple signals agree
    the input is gibberish.  False negatives (letting gibberish through) are
    acceptable; false positives (blocking real commands) are not.

    Rules:
      - Short inputs (< 3 chars) are never gibberish
      - Single-word inputs are never gibberish (could be a command or proper noun)
      - Inputs containing URLs, emails, file paths are never gibberish
      - Inputs with numbers are given extra leniency (could be math, dates, IDs)
      - For multi-word inputs: gibberish if < 30% of tokens are recognized words
        AND the input has anomalous character-level features (high entropy or
        extreme vowel ratio)
    """
    normalized = " ".join((text or "").strip().split())
    if not normalized:
        return False

    # Short inputs — never flag
    if len(normalized) < 3:
        return False

    # Single-word inputs — never flag (could be a command, proper noun, etc.)
    tokens = normalized.split()
    if len(tokens) <= 1:
        return False

    # Non-Latin scripts — never flag (CJK, Arabic, Devanagari, Cyrillic, etc.)
    # These have no Latin vowels and no English dictionary matches, but are
    # legitimate input.  Check if >25% of alpha chars are outside Basic Latin.
    _latin_chars = [c for c in normalized if c.isalpha() and ord(c) < 128]
    _all_alpha = [c for c in normalized if c.isalpha()]
    if _all_alpha and len(_latin_chars) / len(_all_alpha) < 0.75:
        return False

    # Structured patterns — never flag
    if _URL_PATTERN.search(normalized):
        return False
    if _EMAIL_PATTERN.search(normalized):
        return False
    if _FILE_PATH_PATTERN.search(normalized):
        return False

    # If a significant portion of the input is digits or punctuation, be lenient
    # (math expressions, code snippets, etc.)
    alpha_chars = [c for c in normalized if c.isalpha()]
    if len(alpha_chars) < len(normalized) * 0.5:
        return False

    # Core heuristic: word recognition ratio
    word_ratio = _recognized_word_ratio(normalized)

    # If >= 30% of tokens are recognized, it is not gibberish
    if word_ratio >= 0.30:
        return False

    # Below 30% recognized words — check character-level signals for confirmation.
    # We require at least one anomalous character feature to avoid false positives
    # on foreign language text or technical jargon.
    alpha_text = "".join(alpha_chars)
    entropy = _char_entropy(alpha_text)
    vr = _vowel_ratio(alpha_text)

    # Typical English: entropy ~3.5-4.2, vowel ratio ~0.35-0.45
    # Keyboard mash: entropy often >4.0, vowel ratio often <0.15 or >0.70
    # Foreign languages: entropy varies but vowel ratio usually 0.20-0.55
    # Technical terms (kubernetes, tensorflow): vowel ratio ~0.23-0.36
    #
    # We flag as gibberish only if word ratio is very low AND character-level
    # features are anomalous.  The thresholds are deliberately generous to
    # avoid false positives on foreign language or technical jargon.
    anomalous_vowels = vr < 0.15 or vr > 0.75

    if anomalous_vowels:
        return True

    # Keyboard-mash detection: even if vowel ratio is within normal range,
    # a high keyboard adjacency score indicates the input is a mash along
    # keyboard rows (e.g. "qwerty asdfg zxcvb" has vowels but is still mash).
    # Threshold 0.6 = at least 60% of character pairs are physically adjacent
    # on the keyboard, or 60% of tokens sit entirely on a single keyboard row.
    mash_score = _keyboard_mash_score(normalized)
    if mash_score >= 0.6:
        return True

    return False
