from __future__ import annotations

import re
from typing import Any

# ==============================
# Helpers
# ==============================


_URL_RE = re.compile(r"^(https?|file)://", re.IGNORECASE)
_DRIVE_PATH_RE = re.compile(r"^[a-zA-Z]:[\\/]", re.IGNORECASE)  # Windows drive path C:\
_HMS_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")  # h:mm:ss or mm:ss

# Voice keywords that indicate local library playback.
# Order matters: longer phrases must come before shorter ones to avoid
# partial matches (e.g. "from my library" before "from local").
_LOCAL_KEYWORD_RE = re.compile(
    r"\b(?:from\s+my\s+local(?:\s+music|\s+files)?|from\s+my\s+library|"
    r"from\s+local(?:\s+library)?|"
    r"my\s+library|local\s+music|local\s+files|local)\b",
    re.IGNORECASE,
)


def _looks_like_path(s: str) -> bool:
    return bool(_DRIVE_PATH_RE.match(s)) or s.startswith(("/", "./", "../"))


def _detect_source(q: str) -> tuple[str, str]:
    """Detect playback source from a query string.

    Returns ``(source, cleaned_query)`` where *cleaned_query* has any
    local-library keywords stripped out.
    """
    if _URL_RE.match(q):
        return "url", q
    if _looks_like_path(q):
        return "local", q
    if _LOCAL_KEYWORD_RE.search(q):
        cleaned = _LOCAL_KEYWORD_RE.sub("", q).strip()
        cleaned = " ".join(cleaned.split())  # collapse whitespace
        return "local", cleaned
    return "ytsearch1", q


def _clamp(n: int, lo: int, hi: int) -> int:
    return lo if n < lo else hi if n > hi else n


def _parse_hms(s: str) -> int | None:
    """
    Parse 'hh:mm:ss' or 'mm:ss' into total seconds.
    Returns None if not a valid HMS string.
    """
    m = _HMS_RE.match(s)
    if not m:
        return None
    h = int(m.group(1) or 0)
    m_ = int(m.group(2))
    s_ = int(m.group(3))
    return h * 3600 + m_ * 60 + s_


def _read_volume(state: Any) -> int:
    # tolerant: dict-like or object-like, default 50
    if isinstance(state, dict):
        return int(state.get("volume", 50))
    vol = getattr(state, "volume", None)
    return int(vol if vol is not None else 50)


def _state_now_and_vol(st: Any) -> tuple[str | None, int | None]:
    def _maybe(obj, key):
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    now = _maybe(st, "now_playing") or _maybe(st, "track") or None
    vol = _maybe(st, "volume")
    return now, vol


def _truncate(s: str | None, n: int = 128) -> str:
    s = (s or "").strip()
    return s[:n] + ("…" if len(s) > n else "")
