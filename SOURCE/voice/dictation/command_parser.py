"""
Parse dictated speech into structured commands for the dictation controller.

Spoken utterances are classified as one of:
- **Text**: verbatim text to type at the cursor
- **Punctuation**: a named punctuation mark (e.g. "period" -> ".")
- **Control**: editing and navigation commands (new line, undo, stop, etc.)

The parser also handles automatic capitalisation after sentence-ending
punctuation so the dictation flow reads naturally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto

from core.logging_config import get_logger

logger = get_logger(__name__)


# ======================================================================== #
# Data types                                                               #
# ======================================================================== #


class DictationAction(Enum):
    """Action types that the dictation controller can execute."""

    TEXT = auto()
    PUNCTUATION = auto()
    NEW_LINE = auto()
    NEW_PARAGRAPH = auto()
    UNDO = auto()
    STOP = auto()
    SELECT_ALL = auto()
    COPY = auto()
    PASTE = auto()
    CAPITALIZE = auto()
    TAB = auto()


@dataclass(frozen=True, slots=True)
class DictationCommand:
    """Parsed dictation instruction ready for execution.

    Attributes:
        action: The kind of action to perform.
        text: Payload text (e.g. the string to type or the punctuation character).
        repeat: How many times to repeat the action (default 1).
    """

    action: DictationAction
    text: str = ""
    repeat: int = 1


# ======================================================================== #
# Punctuation map                                                          #
# ======================================================================== #

# Keys are lower-cased spoken phrases; values are the literal characters to
# inject.  Order does not matter -- lookup is via dict.

PUNCTUATION_MAP: dict[str, str] = {
    "period": ".",
    "full stop": ".",
    "dot": ".",
    "comma": ",",
    "question mark": "?",
    "exclamation mark": "!",
    "exclamation point": "!",
    "colon": ":",
    "semicolon": ";",
    "semi colon": ";",
    "dash": " \u2014 ",  # em-dash with spaces
    "em dash": " \u2014 ",
    "hyphen": "-",
    "open parenthesis": "(",
    "open paren": "(",
    "left parenthesis": "(",
    "close parenthesis": ")",
    "close paren": ")",
    "right parenthesis": ")",
    "open quote": "\u201c",  # left double quotation mark
    "open quotes": "\u201c",
    "close quote": "\u201d",  # right double quotation mark
    "close quotes": "\u201d",
    "quote": '"',
    "double quote": '"',
    "single quote": "'",
    "apostrophe": "'",
    "at sign": "@",
    "at symbol": "@",
    "hashtag": "#",
    "hash": "#",
    "pound sign": "#",
    "ampersand": "&",
    "and sign": "&",
    "dollar sign": "$",
    "percent": "%",
    "percent sign": "%",
    "slash": "/",
    "forward slash": "/",
    "backslash": "\\",
    "back slash": "\\",
    "ellipsis": "\u2026",
    "three dots": "\u2026",
    "underscore": "_",
    "pipe": "|",
    "tilde": "~",
    "asterisk": "*",
    "star": "*",
    "plus sign": "+",
    "equals sign": "=",
    "less than": "<",
    "greater than": ">",
}

# Sentence-ending punctuation that triggers auto-capitalisation of the next word
_SENTENCE_ENDERS = frozenset({".", "?", "!"})


# ======================================================================== #
# Control command patterns                                                 #
# ======================================================================== #

# Each entry is ``(compiled_regex, DictationAction)``.  Patterns are tried
# in order; the first match wins.

_CONTROL_PATTERNS: list[tuple[re.Pattern[str], DictationAction]] = [
    (re.compile(r"^stop\s+dictation$|^stop\s+dictating$", re.IGNORECASE), DictationAction.STOP),
    (re.compile(r"^new\s+line$|^newline$", re.IGNORECASE), DictationAction.NEW_LINE),
    (re.compile(r"^new\s+paragraph$", re.IGNORECASE), DictationAction.NEW_PARAGRAPH),
    (re.compile(r"^scratch\s+that$|^undo$|^undo\s+that$", re.IGNORECASE), DictationAction.UNDO),
    (re.compile(r"^select\s+all$", re.IGNORECASE), DictationAction.SELECT_ALL),
    (re.compile(r"^copy\s+that$|^copy$", re.IGNORECASE), DictationAction.COPY),
    (re.compile(r"^paste$|^paste\s+that$", re.IGNORECASE), DictationAction.PASTE),
    (re.compile(r"^capitalize$|^caps$|^cap\s+that$", re.IGNORECASE), DictationAction.CAPITALIZE),
    (re.compile(r"^tab$|^press\s+tab$", re.IGNORECASE), DictationAction.TAB),
]


# ======================================================================== #
# Parser state (module-level, reset per dictation session)                 #
# ======================================================================== #

_capitalize_next: bool = True  # Start of dictation -> first word capitalised


def reset_parser_state() -> None:
    """Reset the parser's auto-capitalisation state.

    Call this when starting a new dictation session so the first word
    is capitalised.
    """
    global _capitalize_next
    _capitalize_next = True


def set_capitalize_next(value: bool) -> None:
    """Explicitly set the auto-capitalisation flag.

    Args:
        value: If ``True`` the next text chunk will be capitalised.
    """
    global _capitalize_next
    _capitalize_next = value


# ======================================================================== #
# Public API                                                               #
# ======================================================================== #


def parse_dictation(text: str) -> DictationCommand:
    """Parse a spoken utterance into a structured :class:`DictationCommand`.

    Processing order:

    1. **Control commands** -- checked first via regex patterns.
    2. **Punctuation** -- exact-match against the punctuation map.
    3. **Verbatim text** -- treated as text to type, with a trailing space
       appended for natural word separation.

    Auto-capitalisation is applied after sentence-ending punctuation
    (``"."``, ``"?"``, ``"!"``).

    Args:
        text: Raw transcription text from the STT engine.

    Returns:
        A :class:`DictationCommand` describing the action to perform.
    """
    global _capitalize_next

    stripped = text.strip()
    if not stripped:
        return DictationCommand(action=DictationAction.TEXT, text="")

    # --- 1. Control commands ---
    for pattern, action in _CONTROL_PATTERNS:
        if pattern.match(stripped):
            logger.debug("Dictation control command length=%d action=%s", len(stripped), action.name)
            # Some controls affect capitalisation
            if action in (DictationAction.NEW_LINE, DictationAction.NEW_PARAGRAPH):
                _capitalize_next = True
            return DictationCommand(action=action)

    # --- 2. Punctuation ---
    lower = stripped.lower()
    punct_char = PUNCTUATION_MAP.get(lower)
    if punct_char is not None:
        logger.debug("Dictation punctuation command_length=%d output=%r", len(stripped), punct_char)
        # Sentence-ending punctuation triggers capitalisation of next word
        if punct_char.strip() in _SENTENCE_ENDERS:
            _capitalize_next = True
        return DictationCommand(
            action=DictationAction.PUNCTUATION,
            text=punct_char,
        )

    # --- 3. Verbatim text ---
    output = stripped
    if _capitalize_next:
        output = output[0].upper() + output[1:] if len(output) > 1 else output.upper()
        _capitalize_next = False

    # Append trailing space for natural word separation
    output += " "

    logger.debug("Dictation text length=%d", len(output))
    return DictationCommand(action=DictationAction.TEXT, text=output)


__all__ = [
    "PUNCTUATION_MAP",
    "DictationAction",
    "DictationCommand",
    "parse_dictation",
    "reset_parser_state",
    "set_capitalize_next",
]
