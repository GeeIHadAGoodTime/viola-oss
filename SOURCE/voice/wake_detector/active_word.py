"""The word the user says to reach Viola right now.

"Viola" unless they trained their own. Everything that has to know the word asks here:
the Stage 2 check, the barge-in check on what was heard over her voice, and spoken
hints. Her name stays Viola everywhere else.

The listener publishes the word of the model it actually loaded, so a custom model that
would not load counts as "Viola". Before any listener has run (push-to-talk, start-up)
the answer is the configured word.
"""

from __future__ import annotations

import re
import threading
from difflib import SequenceMatcher

from core.logging_config import get_logger

logger = get_logger(__name__)

BUILT_IN_WAKE_WORD = "Viola"
# How close a transcript word must be to stop her mid-sentence. Tighter than the
# Stage 2 match: a wrong pass there costs one false wake, here it cuts her off.
BARGE_IN_MATCH_RATIO = 0.75

_WORD_RE = re.compile(r"[a-z]+")
_lock = threading.Lock()
_published = False
_word: str | None = None


def publish_active_wake_word(word: str | None) -> None:
    """Record the custom word now in use (None: the built-in "Viola")."""

    global _published, _word
    cleaned = " ".join((word or "").split()) or None
    with _lock:
        _published = True
        _word = cleaned


def reset_active_wake_word_for_tests() -> None:
    global _published, _word
    with _lock:
        _published = False
        _word = None


def _configured_word() -> str | None:
    try:
        from ui.core.security import is_desktop_surface

        if not is_desktop_surface():
            return None
        from ui.settings_manager import get_settings_manager

        settings = get_settings_manager()
        if not settings.get("use_custom_wake_word", False):
            return None
        return str(settings.get("custom_wake_word_name", "") or "").strip() or None
    except Exception as exc:  # noqa: BLE001, RUF100 - settings not up yet: the built-in word
        logger.debug("Could not read the configured wake word: %s", exc)
        return None


def active_wake_word() -> str | None:
    """Return the custom wake word in use, or None for the built-in "Viola"."""

    with _lock:
        if _published:
            return _word
    return _configured_word()


def wake_word_name() -> str:
    """Return the word to tell the user to say."""

    word = active_wake_word()
    return word.capitalize() if word else BUILT_IN_WAKE_WORD


def sound_key(word: str) -> str:
    """Fold spellings of the same sound together, so "kuhpree" can meet "capri"."""
    folded = word.replace("ph", "f").replace("ck", "k").replace("qu", "k").replace("x", "ks")
    folded = folded.translate(str.maketrans("zcqvwy", "skkfui"))
    return re.sub(r"(.)\1+", r"\1", folded)


def similarity(heard: str, target: str) -> float:
    """How alike two written words sound, 0 to 1."""
    if heard == target:
        return 1.0
    return SequenceMatcher(None, sound_key(heard), sound_key(target), autojunk=False).ratio()


def wake_word_keys(target: str) -> set[str]:
    """The written forms a phrase is judged by: its longest word, and all of it joined.

    "hey jarvis" is heard when "jarvis" is, and "hey" alone must not count.
    """
    words = _WORD_RE.findall(target.lower().replace("'", ""))
    return {max(words, key=len), "".join(words)} if words else set()


def is_custom_wake_token(word: str, target: str) -> bool:
    """True when one transcript word is the custom wake word, strictly enough to stop her."""
    heard = word.lower().replace("'", "")
    return any(similarity(heard, key) >= BARGE_IN_MATCH_RATIO for key in wake_word_keys(target))


__all__ = [
    "BUILT_IN_WAKE_WORD",
    "active_wake_word",
    "is_custom_wake_token",
    "publish_active_wake_word",
    "reset_active_wake_word_for_tests",
    "similarity",
    "sound_key",
    "wake_word_keys",
    "wake_word_name",
]
