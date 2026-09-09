"""Messaging utilities -- text chunking and shared helpers."""

from __future__ import annotations


def chunk_text(text: str, limit: int) -> list[str]:
    """Split text into chunks respecting platform character limits.

    Split priority: paragraph breaks > sentence boundaries > word boundaries > hard limit.
    Each chunk is guaranteed to be <= limit characters.

    Args:
        text: The text to chunk.
        limit: Maximum characters per chunk.

    Returns:
        List of text chunks. Empty text returns a single-element list with empty string.
    """
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        # Try paragraph break (double newline)
        split_pos = _find_split_point(remaining, limit, "\n\n")
        if split_pos == -1:
            # Try single newline
            split_pos = _find_split_point(remaining, limit, "\n")
        if split_pos == -1:
            # Try sentence boundary (". " or "! " or "? ")
            split_pos = _find_sentence_break(remaining, limit)
        if split_pos == -1:
            # Try word boundary (space)
            split_pos = _find_split_point(remaining, limit, " ")
        if split_pos == -1:
            # Hard split at limit
            split_pos = limit

        chunks.append(remaining[:split_pos].rstrip())
        remaining = remaining[split_pos:].lstrip()

    return chunks


def _find_split_point(text: str, limit: int, separator: str) -> int:
    """Find the last occurrence of separator within limit chars."""
    search_area = text[:limit]
    pos = search_area.rfind(separator)
    if pos > 0:
        return pos + len(separator)
    return -1


def _find_sentence_break(text: str, limit: int) -> int:
    """Find the last sentence boundary within limit chars."""
    search_area = text[:limit]
    best = -1
    for ending in (". ", "! ", "? "):
        pos = search_area.rfind(ending)
        if pos > best:
            best = pos
    if best > 0:
        return best + 2  # include the punctuation and space
    return -1
