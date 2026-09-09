from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any

_MOJIBAKE_MARKERS: tuple[str, ...] = (
    "Ã",
    "Â",
    "â€",
    "â€™",
    "â€œ",
    "â€”",
    "â€“",
    "â€¦",
    "â„¢",
    "â€¢",
    "â†",
    "ðŸ",
)


def looks_like_mojibake(text: str) -> bool:
    """Return True when *text* looks like UTF-8 decoded as Latin-1/cp1252."""
    if not text:
        return False
    return any(marker in text for marker in _MOJIBAKE_MARKERS)


def _mojibake_score(text: str) -> int:
    return sum(text.count(marker) for marker in _MOJIBAKE_MARKERS)


_MOJIBAKE_REPLACEMENTS: dict[str, str] = {
    # ------------------------------------------------------------------
    # Two-byte UTF-8 sequences misread as cp1252/Latin-1 (Â + second byte)
    # ------------------------------------------------------------------
    "\u00c2\u00b0": "\u00b0",  # Â° → °  (degree)
    "\u00c2\u00b1": "\u00b1",  # Â± → ±  (plus-minus)
    "\u00c2\u00b7": "\u00b7",  # Â· → ·  (middle dot)
    "\u00c2\u00a9": "\u00a9",  # Â© → ©  (copyright)
    "\u00c2\u00ae": "\u00ae",  # Â® → ®  (registered)
    "\u00c2\u00ab": "\u00ab",  # Â« → «  (left guillemet)
    "\u00c2\u00bb": "\u00bb",  # Â» → »  (right guillemet)
    "\u00c2\u00bd": "\u00bd",  # Â½ → ½  (one-half)
    # ------------------------------------------------------------------
    # Three-byte UTF-8 sequences misread as cp1252 (â€ + third byte)
    # cp1252 byte 0x80=€, 0x93=", 0x94=", 0x99=™, 0x9C=œ
    # ------------------------------------------------------------------
    "\u00e2\u20ac\u00a2": "\u2022",  # â€¢ → •  (bullet)
    "\u00e2\u20ac\u201d": "\u2014",  # â€" → —  (em dash: e2 80 94, 0x94=")
    "\u00e2\u20ac\u201c": "\u2013",  # â€" → –  (en dash: e2 80 93, 0x93=")
    "\u00e2\u20ac\u2122": "\u2019",  # â€™ → '  (right single quote: e2 80 99, 0x99=™)
    "\u00e2\u20ac\u0153": "\u201c",  # â€œ → "  (left double quote: e2 80 9c, 0x9C=œ)
    "\u00e2\u20ac\u00a6": "\u2026",  # â€¦ → …  (ellipsis: e2 80 a6)
    # ------------------------------------------------------------------
    # Latin accented characters (Ã + second byte via cp1252)
    # ------------------------------------------------------------------
    "\u00c3\u00ad": "\u00ed",  # Ã­ → í
    "\u00c3\u00a1": "\u00e1",  # Ã¡ → á
    "\u00c3\u00a9": "\u00e9",  # Ã© → é
    "\u00c3\u00b3": "\u00f3",  # Ã³ → ó
    "\u00c3\u00ba": "\u00fa",  # Ãº → ú
    "\u00c3\u00b1": "\u00f1",  # Ã± → ñ
    "\u00c3\u00bc": "\u00fc",  # Ã¼ → ü
    "\u00c3\u00a8": "\u00e8",  # Ã¨ → è
    "\u00c3\u00a0": "\u00e0",  # Ã  → à
    "\u00c3\u00b4": "\u00f4",  # Ã´ → ô
    "\u00c3\u00aa": "\u00ea",  # Ãª → ê
    "\u00c3\u00ae": "\u00ee",  # Ã® → î
    "\u00c3\u00a7": "\u00e7",  # Ã§ → ç
    "\u00c3\u0089": "\u00c9",  # Ã‰ → É
    "\u00c3\u0096": "\u00d6",  # Ã– → Ö
    "\u00c3\u009c": "\u00dc",  # Ã\x9c → Ü
}


def _repair_once(text: str) -> str:
    # First try targeted replacements (most reliable on Windows)
    result = text
    for bad, good in _MOJIBAKE_REPLACEMENTS.items():
        result = result.replace(bad, good)
    if result != text:
        return result

    # Fall back to encode/decode round-trip
    best = text
    best_score = _mojibake_score(text)

    for source_encoding in ("cp1252", "latin-1"):
        try:
            candidate = text.encode(source_encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue

        candidate_score = _mojibake_score(candidate)
        if candidate != best and candidate_score < best_score:
            best = candidate
            best_score = candidate_score

    return best


def repair_mojibake(text: str) -> str:
    """Repair common mojibake like ``Â°F`` and ``dÃ­as`` when present."""
    if not looks_like_mojibake(text):
        return text

    repaired = text
    for _ in range(2):
        candidate = _repair_once(repaired)
        if candidate == repaired:
            break
        repaired = candidate
    return repaired


def repair_mojibake_deep(value: Any) -> Any:
    """Recursively repair mojibake in nested JSON-like values."""
    if isinstance(value, str):
        return repair_mojibake(value)
    if isinstance(value, list):
        return [repair_mojibake_deep(item) for item in value]
    if isinstance(value, tuple):
        return tuple(repair_mojibake_deep(item) for item in value)
    if isinstance(value, Mapping):
        return {key: repair_mojibake_deep(item) for key, item in value.items()}
    return value


def ensure_utf8_stdio() -> None:
    """Reconfigure sys.stdout and sys.stderr to UTF-8 on Windows.

    On Windows the default console encoding is cp1252 (or another narrow
    codepage).  Any log message or print() call that contains non-cp1252
    characters — CJK, emoji, accented Latin outside the codepage — raises
    ``UnicodeEncodeError`` and can crash the entire process.

    This function reconfigures both streams to UTF-8 with ``errors='replace'``
    so that unencodable characters are substituted with ``?`` instead of
    raising an exception.  It is a no-op on platforms where the streams are
    already UTF-8 or where ``reconfigure`` is not available (e.g. when stdout
    is redirected to DEVNULL).

    Call once at the very top of each entry-point module (``viola_qt.py``,
    ``mcp_servers/core_tools/server.py``, etc.) before any other imports.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        current_encoding = getattr(stream, "encoding", None) or ""
        if current_encoding.lower().replace("-", "") in ("utf8", "utf_8"):
            # Already UTF-8 — nothing to do
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # Stream does not support reconfigure (e.g. io.BytesIO wrapper)
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


__all__ = [
    "ensure_utf8_stdio",
    "looks_like_mojibake",
    "repair_mojibake",
    "repair_mojibake_deep",
]
