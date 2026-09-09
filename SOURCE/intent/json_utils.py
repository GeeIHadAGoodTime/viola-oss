"""Robust JSON extraction from LLM responses.

Handles markdown fences, preamble/postamble text, and malformed output.
"""

from __future__ import annotations

import json
import re

from core.logging_config import get_logger

logger = get_logger(__name__)

# Pattern for markdown code fences: ```json ... ``` or ``` ... ```
_FENCE_PATTERN = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def extract_json_from_llm_response(text: str) -> dict | None:
    """Extract the first valid JSON object from LLM response text.

    Handles:
    - Clean JSON
    - Markdown code fences (```json ... ``` and ``` ... ```)
    - Preamble/postamble text around JSON
    - Multiple JSON objects (returns first)

    Returns None if no valid JSON found.
    """
    if not text or not isinstance(text, str):
        return None

    stripped = text.strip()

    # --- Step 1: Try the raw text as-is ---
    result = _try_parse(stripped)
    if result is not None:
        return result

    # --- Step 2: Strip markdown code fences ---
    fence_match = _FENCE_PATTERN.search(stripped)
    if fence_match:
        inner = fence_match.group(1).strip()
        result = _try_parse(inner)
        if result is not None:
            logger.debug("JSON extracted after stripping markdown fence")
            return result

    # --- Step 3: Scan for JSON object by matching braces ---
    result = _extract_by_brace_scan(stripped)
    if result is not None:
        logger.debug("JSON extracted via brace-scanning (preamble/postamble removed)")
        return result

    # --- Step 4: Sanitize double braces (nano model artifact) and retry ---
    if "{{" in stripped:
        sanitized = stripped.replace("{{", "{").replace("}}", "}")
        result = _try_parse(sanitized)
        if result is not None:
            logger.debug("JSON extracted after double-brace sanitization")
            return result

    return None


def _try_parse(text: str) -> dict | None:
    """Try to parse text as a JSON object. Returns dict or None."""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _extract_by_brace_scan(text: str) -> dict | None:
    """Find the first valid JSON object by scanning for '{' and matching '}'.

    Handles nested braces correctly. Skips braces inside JSON strings.
    """
    start = 0
    while start < len(text):
        idx = text.find("{", start)
        if idx == -1:
            break

        # Try to find the matching closing brace
        end = _find_matching_brace(text, idx)
        if end is not None:
            candidate = text[idx : end + 1]
            result = _try_parse(candidate)
            if result is not None:
                return result

        # Move past this opening brace and try the next one
        start = idx + 1

    return None


def _find_matching_brace(text: str, start: int) -> int | None:
    """Find the index of the closing '}' that matches the '{' at start.

    Correctly handles:
    - Nested braces
    - Strings (ignores braces inside quoted strings)
    - Escaped characters inside strings

    Returns the index of the matching '}', or None if not found.
    """
    if start >= len(text) or text[start] != "{":
        return None

    depth = 0
    in_string = False
    i = start

    while i < len(text):
        char = text[i]

        if in_string:
            if char == "\\" and i + 1 < len(text):
                # Skip escaped character
                i += 2
                continue
            if char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return i

        i += 1

    return None
