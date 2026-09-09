"""LLM response cleanup utilities.

Strips leaked JSON/template artifacts from LLM answer text.
Moved from ``intent.ai_controller`` (Q-002) so that multiple callers
(``ai_controller``, ``agent_executor``, ``pipeline_processors``) can
import from a single shared location.

CB-10: Robust post-processing to strip leaked JSON/template artifacts
---------------------------------------------------------------------
The LLM is instructed to respond in JSON (for routing).  Occasionally it
leaks its response template into the ``answer`` field in several shapes:

  1. Trailing JSON template:  ``Great song! {"type":"answer",...}``
  2. Entire response is a template:  ``{"type":"answer","answer":"Hi"}``
  3. Code-fenced JSON:  ``Muted.```json\\n{"type":"answer",...}\\n```\\n``
  4. Raw Python dict dump:  ``Volume: {'ok': True, 'error': None, ...}``
  5. Double-brace templates:  ``{{"type":"answer","answer":"Hi"}}``

Rather than pattern-matching individual shapes (whack-a-mole), this
module applies a LAYERED cleanup that handles any combination.
"""

from __future__ import annotations

import re

from utils.text_encoding import repair_mojibake

# ---------------------------------------------------------------------------
# Compiled regexes
# ---------------------------------------------------------------------------

# Regex to detect and strip leaked JSON response templates from LLM answer text.
# Handles both quoted ("type") and unquoted (type) key formats --
# LLMs sometimes emit JavaScript-style {type: answer, answer: ...}.
_JSON_TEMPLATE_RE = re.compile(
    r'\s*\{+\s*"?type"?\s*:\s*"?(?:answer|command|ignore)"?.*$',
    re.DOTALL,
)

# Regex to strip ```json ... ``` code fences (JSON-tagged only, not ```python etc.)
_CODE_FENCE_JSON_RE = re.compile(
    r"```json\s*\n?.*?```",
    re.DOTALL,
)

# Regex to strip untagged code fences that contain JSON-like content
_CODE_FENCE_UNTAGGED_JSON_RE = re.compile(
    r"```\s*\n?\s*\{.*?\}\s*\n?```",
    re.DOTALL,
)

# Regex to strip trailing ``` code fence fragment (incomplete fence at end)
_TRAILING_FENCE_RE = re.compile(
    r"\s*```(?:json)?\s*$",
)

# Regex to detect raw Python dict dumps like: {'ok': True, 'error': None, ...}
# These appear when tool execution results leak into the message.
_PYTHON_DICT_RE = re.compile(
    r"\s*\{?\s*'(?:ok|error|data|intent|message)'\s*:",
)

# Regex to detect "Label: {'key': value, ...}" patterns (tool result dumps).
# The label must be a word-like string (letters, spaces, etc.) NOT starting
# with { or ' to avoid matching inside the dict itself.
_LABELED_DICT_RE = re.compile(
    r"^([A-Za-z][A-Za-z0-9 _-]*):\s*\{['\"](?:ok|error|data|intent|message)['\"].*\}\s*$",
    re.DOTALL,
)

# Regex to detect a *complete* JSON response template occupying the entire text.
# Handles both quoted ("type") and unquoted (type) key formats.
_WHOLE_TEMPLATE_RE = re.compile(
    r'^\s*\{+\s*"?type"?\s*:\s*"?(?:answer|command|ignore)"?',
)


# ---------------------------------------------------------------------------
# Markdown -> plain text for the voice/response display channel
# ---------------------------------------------------------------------------
# The model may emit markdown (bold, headers, bullet lists, links). The
# voice/response channel -- the desktop + web chat surfaces served by
# ``/v1/command`` and ChatMode -- shows the reply text verbatim and speaks it
# via TTS, so raw markdown *syntax* (``**``, ``#``, ``- ``, ``[label](url)``)
# is displayed/spoken literally ("asterisk asterisk symphony ...", issue
# #1406). Rendering that syntax to plain text is a presentation-layer
# *rendering* concern for a plain-text channel -- NOT a runtime classifier of
# the user's query, NOT a parse of the model's intent to branch behaviour, and
# NOT a jargon/token denylist. It preserves all CONTENT (words and URLs) and
# only removes markup. Rich channels (Telegram/Discord/email) keep the
# markdown and render it in their own per-channel formatters, so only
# plain-text channels call this.
_MD_CODE_FENCE_RE = re.compile(r"```[^\n]*\n?(.*?)```", re.DOTALL)
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)[^)]*\)", re.IGNORECASE)
_MD_HEADER_RE = re.compile(r"(?m)^\s{0,3}#{1,6}[ \t]+")
_MD_BLOCKQUOTE_RE = re.compile(r"(?m)^[ \t]*>[ \t]?")
_MD_BULLET_LINE_RE = re.compile(r"(?m)^[ \t]*(?:[-*+])[ \t]+(?=\S)")
_MD_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def _strip_markdown_link(match: re.Match[str]) -> str:
    label = " ".join(match.group(1).split()).strip()
    url = match.group(2).strip()
    if label and label != url:
        return "%s (%s)" % (label, url)
    return url or label


def _strip_markdown_emphasis(text: str) -> str:
    # Order matters: strip the double-marker forms before the single-marker
    # forms. Word-boundary guards leave compound tokens ("file_name", "2 * 3",
    # "C#") untouched -- these mirror the battle-tested TTS normaliser.
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"~~(.+?)~~", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"\1", text)
    return text


def strip_markdown_for_display(text: str) -> str:
    """Render markdown syntax to plain text for a plain-text/voice channel.

    Removes markdown *markup* while preserving every word and URL: code
    fences/inline code keep their inner text, ``[label](url)`` becomes
    ``label (url)``, headers/blockquote markers and ``**``/``*``/``__``/``~~``
    emphasis are dropped, and ``- ``/``* ``/``+ `` list markers are removed so
    each item stays on its own line. Markers are stripped rather than replaced
    with a bullet glyph because the same text can be handed to TTS, and a
    stray glyph would be voiced. This is a rendering transform for channels
    that show text verbatim, not a semantic filter -- see the module note
    above (issue #1406).
    """
    if not text:
        return text
    result = text
    result = _MD_CODE_FENCE_RE.sub(lambda m: m.group(1), result)
    result = _MD_INLINE_CODE_RE.sub(r"\1", result)
    result = _MD_LINK_RE.sub(_strip_markdown_link, result)
    result = _MD_HEADER_RE.sub("", result)
    result = _MD_BLOCKQUOTE_RE.sub("", result)
    result = _strip_markdown_emphasis(result)
    result = _MD_BULLET_LINE_RE.sub("", result)
    result = _MD_MULTI_BLANK_RE.sub("\n\n", result)
    # Drop trailing spaces a marker strip can leave, per line.
    result = "\n".join(line.rstrip() for line in result.split("\n"))
    return result.strip()


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _extract_answer_from_json_template(text: str) -> str | None:
    """Try to extract the ``answer`` value from a JSON response template.

    Handles doubled-brace templates (``{{ ... }}``) by normalising to
    single braces before parsing.  Returns ``None`` if the text is not
    a parseable template or contains no ``answer`` field.

    Note: when the text is a *command* template (``type=command``) this
    function returns ``None`` -- the caller is responsible for detecting
    that case and routing the command for execution instead of treating
    it as a displayable answer.
    """
    import json

    # Normalise doubled braces -> single braces for JSON parsing.
    # The LLM prompt uses {{ }} as literal brace escapes in f-strings,
    # and sometimes the LLM echoes them back.  Replace ALL occurrences.
    normalised = text.strip().replace("{{", "{").replace("}}", "}")

    try:
        parsed = json.loads(normalised)
        if isinstance(parsed, dict):
            answer = parsed.get("answer")
            if isinstance(answer, str) and answer.strip():
                return repair_mojibake(answer.strip())
            # Command template leaked into the answer path -- return None so
            # the caller can detect and execute it instead of generating a
            # misleading "I tried to X but had a processing issue" message.
            if isinstance(parsed.get("command"), str) and parsed.get("type") == "command":
                return None
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: regex extraction for ``"answer": "..."`` or ``answer: "..."``
    m = re.search(
        r'"?answer"?\s*:\s*"((?:[^"\\]|\\.)*)"',
        text,
    )
    if m:
        extracted = m.group(1)
        # Unescape common JSON escapes
        extracted = extracted.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        if extracted.strip():
            return repair_mojibake(extracted.strip())

    return None


def _strip_code_fences(text: str) -> str:
    """Remove JSON-containing code fences from text.

    Only strips fences that are tagged as ``json`` or that contain
    JSON-like content (opening ``{``).  Non-JSON fences (e.g.
    ``\\`\\`\\`python``) are left intact.

    Handles:
    - Complete ``\\`\\`\\`json`` fences
    - Untagged fences with JSON-like content
    - Trailing incomplete fences: text \\`\\`\\`json
    """
    if "```" not in text:
        return text

    # Strip ```json ... ``` fences
    cleaned = _CODE_FENCE_JSON_RE.sub("", text)

    # Strip untagged ``` { ... } ``` fences (JSON content)
    cleaned = _CODE_FENCE_UNTAGGED_JSON_RE.sub("", cleaned)

    # Strip trailing incomplete fence (e.g. answer ends with ```json)
    cleaned = _TRAILING_FENCE_RE.sub("", cleaned)

    return cleaned.strip()


def _strip_python_dict_dump(text: str) -> str:
    """Remove raw Python dict dumps from the message.

    Handles patterns like:
    - ``Volume: {'ok': True, 'error': None, 'data': {...}}``
    - ``{'ok': True, 'intent': 'set_volume', ...}``

    If the text has a label before the dict (e.g. "Volume:"), the label
    is kept as a reasonable user-facing message.  Otherwise tries to
    extract a 'message' value from the dict.
    """
    if not _PYTHON_DICT_RE.search(text):
        return text

    # Check for "Label: {dict}" pattern
    m = _LABELED_DICT_RE.match(text)
    if m:
        label = m.group(1).strip()
        if label:
            return label

    # Try to extract a 'message' value from the dict (any nesting level)
    # Search for both single-quoted and double-quoted message values
    for pattern in (
        r"'message'\s*:\s*'((?:[^'\\]|\\.)*)'",
        r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"',
    ):
        msg_match = re.search(pattern, text)
        if msg_match:
            extracted = msg_match.group(1).strip()
            if extracted:
                return extracted

    # Last resort: return empty string to signal that the content is
    # not user-suitable (caller will handle fallback)
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def strip_json_template(text: str) -> str:
    """Remove any JSON response template that the LLM leaked into the answer.

    This is the SINGLE entry point for all LLM response cleanup.  It applies
    a layered strategy that handles ANY combination of leaked artifacts:

    1. Code fences (```json ... ```)
    2. Raw Python dict dumps
    3. Entire-response JSON templates
    4. Trailing JSON templates

    Args:
        text: Raw answer text from LLM.

    Returns:
        Cleaned text with all JSON/template artifacts removed.
    """
    if not text:
        return text

    # --- Layer 1: Strip code fences ---
    # Must come first because code fences can wrap JSON templates.
    cleaned = _strip_code_fences(text)

    # --- Layer 2: Strip raw Python dict dumps ---
    cleaned = _strip_python_dict_dump(cleaned)

    # If cleaning removed all content, try to extract answer from original
    if not cleaned.strip():
        extracted = _extract_answer_from_json_template(text.strip())
        if extracted:
            return extracted
        return repair_mojibake(text)  # last resort: return original

    # --- Layer 3: Handle entire-response JSON templates ---
    stripped = cleaned.strip()
    if _WHOLE_TEMPLATE_RE.match(stripped):
        # Try stripping the trailing template first -- if that still
        # leaves content it was case 1 (trailing template with prose).
        cleaned_trailing = _JSON_TEMPLATE_RE.sub("", cleaned).strip()
        if cleaned_trailing:
            return repair_mojibake(cleaned_trailing)

        # Nothing left after stripping -> the entire text *was* the
        # template.  Extract the answer field.
        extracted = _extract_answer_from_json_template(stripped)
        if extracted:
            return extracted

    # --- Layer 4: Strip trailing JSON templates ---
    cleaned_final = _JSON_TEMPLATE_RE.sub("", cleaned).strip()
    # Only return if stripping left content
    return repair_mojibake(cleaned_final if cleaned_final else cleaned)


# Backward-compatible alias (old private name used by callers).
_strip_json_template = strip_json_template
