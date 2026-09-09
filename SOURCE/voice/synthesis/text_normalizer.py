from __future__ import annotations

import re

from num2words import num2words

from core.logging_config import get_logger

logger = get_logger(__name__)

_CODE_BLOCK_RE = re.compile(r"(```|~~~).*?(?:\1|$)", flags=re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_HEADER_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")
_BLOCKQUOTE_RE = re.compile(r"(?m)^\s*>\s?")
_BULLET_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)[^)]*\)", flags=re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>)\]]+", flags=re.IGNORECASE)
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+")

# ---------------------------------------------------------------------------
# Currency: $25 → "twenty-five dollars", $15.99 → "fifteen dollars and ninety-nine cents"
# Must run BEFORE the generic standalone number regex.
# ---------------------------------------------------------------------------
_CURRENCY_RE = re.compile(r"\$(\d{1,}(?:,\d{3})*)(?:\.(\d{1,2}))?")

# ---------------------------------------------------------------------------
# Phone numbers: 555-123-4567 → digit-by-digit with commas for pauses
# Matches common US formats: 555-123-4567, (555) 123-4567, 555.123.4567
# ---------------------------------------------------------------------------
_PHONE_RE = re.compile(
    r"(?<!\d)"  # not preceded by digit
    r"(?:\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4})"  # 10 digits with separators
    r"(?!\d)"  # not followed by digit
)
_DIGIT_NAMES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]

# ---------------------------------------------------------------------------
# Percentages: 15% → "fifteen percent"
# ---------------------------------------------------------------------------
_PERCENT_RE = re.compile(r"\b(\d+(?:\.\d+)?)%")

# ---------------------------------------------------------------------------
# Times: 2:30 PM → "two thirty PM", 10:05 → "ten oh five"
# ---------------------------------------------------------------------------
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})(?:\s*(AM|PM|am|pm|a\.m\.|p\.m\.))?\b")

_ORDINAL_RE = re.compile(r"\b(\d+)(st|nd|rd|th)\b", flags=re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
# Isolated markdown symbols that survive emphasis/header/bullet cleanup.
# Matches *, #, or _ that are NOT adjacent to a word character on either side
# (e.g. "type * here" or "-- # --"), leaving compound tokens like
# "C*" or "file_name" untouched.
_ISOLATED_SYMBOL_RE = re.compile(r"(?<!\w)[*#_](?!\w)")
# Calendar years need their own spoken forms before generic number conversion:
# 1901-1999 use "nineteen oh one"/"nineteen ninety-nine"; 2000-2009 stay "two thousand ...".
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_PARENTHETICAL_RE = re.compile(r"\s*\(([^()]{3,80})\)\s*")
_STRONG_CONJUNCTION_RE = re.compile(r"(?<![,;:])\s+(but|so|because)\s+", flags=re.IGNORECASE)
_AND_CLAUSE_RE = re.compile(
    r"(?<![,;:])\s+and\s+(?=(?:I|you|we|they|he|she|it|there|that|this)\b)",
    flags=re.IGNORECASE,
)
_TRAILING_HEDGE_RE = re.compile(
    r"^(?P<prefix>.*?)(?P<comma>,?\s*)(?P<phrase>you know|I think)[.!?]?$", flags=re.IGNORECASE
)
# Number-to-words conversion for standalone integers (e.g. 1000 -> "one thousand").
# Only matches bare digit sequences bounded by non-word characters so tokens like
# "mp3", "16kHz", or already-converted ordinals ("2nd") are left untouched.
_STANDALONE_NUMBER_RE = re.compile(r"\b(\d+)\b")
_ONES = [
    "zero",
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
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_MAX_SPOKEN_CHARS = 500
_SUMMARY_CHARS = 360
_MAX_SPOKEN_SENTENCES = 3
_SUMMARY_SUFFIX = "I'll send the full details in chat."

# Initialisms: spell each letter or digit-like suffix as a spoken token.
_ACRONYM_INITIALISMS = {
    "AI": "A I",
    "API": "A P I",
    "CPU": "C P U",
    "GPU": "G P U",
    "USB": "U S B",
    "URL": "U R L",
    "FAQ": "F A Q",
    "CEO": "C E O",
    "CTO": "C T O",
    "CFO": "C F O",
    "HR": "H R",
    "NYC": "N Y C",
    "LA": "L A",
    "UK": "U K",
    "US": "U S",
    "EU": "E U",
    "UN": "U N",
    "FBI": "F B I",
    "CIA": "C I A",
    "PNG": "P N G",
    "MP3": "M P three",
    "MP4": "M P four",
    "HTML": "H T M L",
    "CSS": "C S S",
    "JS": "J S",
    "XML": "X M L",
    "PDF": "P D F",
    "DVD": "D V D",
    "GPS": "G P S",
}

# Word-style acronyms: common lexicalized pronunciations, not letter-by-letter.
_ACRONYM_WORDS = {
    "RAM": "ram",
    "ROM": "rom",
    "NATO": "nay toe",
    "NASA": "nassa",
    "JPEG": "jay peg",
    "GIF": "giff",
    "JSON": "jay sawn",
    "Wi-Fi": "why fie",
    "WiFi": "why fie",
}
_ACRONYM_PRONUNCIATIONS = {**_ACRONYM_INITIALISMS, **_ACRONYM_WORDS}

_BRAND_PRONUNCIATIONS = {
    "Spotify": "Spot if eye",
    "YouTube": "you tube",
    "Anthropic": "Ann throw pick",
    "GitHub": "git hub",
    "OpenAI": "Open A I",
    "ChatGPT": "chat G P T",
    "Claude": "clawed",
    "Slack": "slack",
    "Discord": "discord",
    "Reddit": "red it",
    "Twitter": "twitter",
    "Instagram": "in stuh gram",
    "TikTok": "tick tock",
    "Netflix": "net flicks",
    "Amazon": "amazon",
    "Microsoft": "my crow soft",
    "Google": "goo gull",
    "Apple": "apple",
    "NVIDIA": "en vid ee uh",
    "AMD": "A M D",
    "Intel": "in tell",
    "Stripe": "stripe",
    "PayPal": "pay pal",
    "Venmo": "ven moe",
    "Lyft": "lift",
    "Uber": "oo ber",
    "Airbnb": "air B N B",
    "DoorDash": "door dash",
    "GrubHub": "grub hub",
    "Roku": "roh koo",
    "Sonos": "soh nohs",
    "Bose": "bohz",
    "Yamaha": "yah muh hah",
    "Wikipedia": "wick ih pee dee uh",
    "LinkedIn": "linked in",
    "Pinterest": "pin ter est",
    "Notion": "notion",
    "Figma": "fig muh",
    "Linear": "linear",
    "Asana": "uh sah nuh",
    "Trello": "trell oh",
    "Dropbox": "drop box",
    "iCloud": "eye cloud",
    "Gmail": "G mail",
    "Outlook": "out look",
}


def _literal_dict_re(keys: object, *, flags: int = 0) -> re.Pattern[str]:
    ordered = sorted((str(key) for key in keys), key=len, reverse=True)
    return re.compile(r"(?<!\w)(" + "|".join(re.escape(key) for key in ordered) + r")(?!\w)", flags=flags)


_ACRONYM_RE = _literal_dict_re(_ACRONYM_PRONUNCIATIONS)
_BRAND_RE = _literal_dict_re(_BRAND_PRONUNCIATIONS, flags=re.IGNORECASE)


def _apply_literal_dictionary(
    text: str,
    replacements: dict[str, str],
    pattern: re.Pattern[str],
    *,
    ignore_case: bool = False,
) -> str:
    lookup = {key.lower(): value for key, value in replacements.items()} if ignore_case else replacements

    def replace(match: re.Match[str]) -> str:
        matched = match.group(0)
        key = matched.lower() if ignore_case else matched
        return lookup.get(key, matched)

    return pattern.sub(replace, text)


def _get_runtime_config(config: object | None) -> object | None:
    if config is not None:
        return config
    try:
        from config.settings import settings as runtime_settings

        return runtime_settings
    except Exception:
        return None


def _clean_pronunciation_overrides(config: object | None) -> dict[str, str]:
    raw = getattr(config, "tts_pronunciation_overrides", {}) if config is not None else {}
    if not isinstance(raw, dict):
        return {}

    cleaned: dict[str, str] = {}
    for key, value in raw.items():
        source = str(key).strip()
        replacement = str(value).strip()
        if source and replacement:
            cleaned[source] = replacement
    return cleaned


def _apply_pronunciation_overrides(text: str, config: object | None) -> str:
    overrides = _clean_pronunciation_overrides(config)
    if not overrides:
        return text
    pattern = _literal_dict_re(overrides, flags=re.IGNORECASE)
    return _apply_literal_dictionary(text, overrides, pattern, ignore_case=True)


def _replace_parenthetical(match: re.Match[str]) -> str:
    body = match.group(1).strip()
    if body.lower() == "link sent in chat":
        return match.group(0)
    return " — %s — " % body


def _replace_trailing_hedge(match: re.Match[str]) -> str:
    prefix = match.group("prefix").rstrip()
    phrase = match.group("phrase")
    if not prefix:
        return "%s…" % phrase
    if prefix.endswith((",", ";", ":", "—")):
        return "%s %s…" % (prefix, phrase)
    return "%s, %s…" % (prefix, phrase)


def _apply_prosody_hints(text: str) -> str:
    result = _PARENTHETICAL_RE.sub(_replace_parenthetical, text)
    result = _STRONG_CONJUNCTION_RE.sub(r", \1 ", result)
    result = _AND_CLAUSE_RE.sub(", and ", result)
    return _TRAILING_HEDGE_RE.sub(_replace_trailing_hedge, result)


def _clean_num2words_output(words: object) -> str:
    return str(words).replace(",", "").replace("-", " ").replace(" and ", " ")


def _clean_num2words_ordinal_output(words: object) -> str:
    return str(words).replace(",", "").replace(" and ", " ")


def _number_to_words(n: int) -> str:
    """Convert a non-negative integer to English words using num2words."""
    if n < 0:
        return str(n)
    try:
        words = num2words(n, lang="en")
    except (NotImplementedError, OverflowError, TypeError, ValueError):
        return str(n)
    return _clean_num2words_output(words)


def _two_digit_year_words(n: int) -> str:
    if n == 0:
        return "hundred"
    if n < 10:
        return "oh %s" % _ONES[n]
    if n < 20:
        return _ONES[n]
    tens = _TENS[n // 10]
    ones = _ONES[n % 10] if n % 10 else ""
    return tens + ("-" + ones if ones else "")


def _replace_year(match: re.Match[str]) -> str:
    try:
        year = int(match.group(0))
    except (ValueError, IndexError):
        return match.group(0)

    century = year // 100
    last_two = year % 100

    if century == 19:
        if last_two == 0:
            return "nineteen hundred"
        return "nineteen %s" % _two_digit_year_words(last_two)

    if year == 2000:
        return "two thousand"
    if 2001 <= year <= 2009:
        return "two thousand %s" % _ONES[last_two]
    return "twenty %s" % _two_digit_year_words(last_two)


def _replace_number(match: re.Match[str]) -> str:
    try:
        return _number_to_words(int(match.group(0)))
    except (ValueError, IndexError):
        return match.group(0)


def _replace_currency(match: re.Match[str]) -> str:
    """Convert $25 → 'twenty-five dollars', $15.99 → 'fifteen dollars and ninety-nine cents'."""
    try:
        dollars_str = match.group(1).replace(",", "")
        cents_str = match.group(2)
        dollars = int(dollars_str)
        cents = int(cents_str) if cents_str else 0

        if dollars == 0 and cents > 0:
            cent_word = "cent" if cents == 1 else "cents"
            return "%s %s" % (_number_to_words(cents), cent_word)

        dollar_word = "dollar" if dollars == 1 else "dollars"
        result = "%s %s" % (_number_to_words(dollars), dollar_word)

        if cents > 0:
            cent_word = "cent" if cents == 1 else "cents"
            result += " and %s %s" % (_number_to_words(cents), cent_word)

        return result
    except (ValueError, IndexError):
        return match.group(0)


def _replace_phone(match: re.Match[str]) -> str:
    """Convert phone numbers to digit-by-digit pronunciation with pauses."""
    digits = re.sub(r"[^\d]", "", match.group(0))
    if len(digits) != 10:
        return match.group(0)
    # Group as 3-3-4 with commas for natural pausing
    parts = []
    for group in (digits[:3], digits[3:6], digits[6:]):
        parts.append(" ".join(_DIGIT_NAMES[int(d)] for d in group))
    return ", ".join(parts)


def _replace_percent(match: re.Match[str]) -> str:
    """Convert 15% → 'fifteen percent', 7.5% → 'seven point five percent'."""
    try:
        num_str = match.group(1)
        if "." in num_str:
            whole, frac = num_str.split(".", 1)
            whole_words = _number_to_words(int(whole)) if whole else "zero"
            frac_words = " ".join(_DIGIT_NAMES[int(d)] for d in frac)
            return "%s point %s percent" % (whole_words, frac_words)
        return "%s percent" % _number_to_words(int(num_str))
    except (ValueError, IndexError):
        return match.group(0)


def _replace_time(match: re.Match[str]) -> str:
    """Convert 2:30 PM → 'two thirty PM', 10:05 → 'ten oh five'."""
    try:
        hour = int(match.group(1))
        minute = int(match.group(2))
        ampm = match.group(3) or ""

        hour_word = _number_to_words(hour)
        if minute == 0:
            minute_word = "o'clock"
        elif minute < 10:
            minute_word = "oh %s" % _number_to_words(minute)
        else:
            minute_word = _number_to_words(minute)

        result = "%s %s" % (hour_word, minute_word)
        if ampm:
            result += " %s" % ampm.upper().replace(".", "")
        return result
    except (ValueError, IndexError):
        return match.group(0)


def _replace_markdown_emphasis(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
    return text


def _replace_markdown_link(match: re.Match[str]) -> str:
    label = _WHITESPACE_RE.sub(" ", match.group(1)).strip()
    if label:
        return "%s (link sent in chat)" % label
    return "(link sent in chat)"


def _replace_url(match: re.Match[str]) -> str:
    """Replace URLs with a natural spoken reference instead of reading them aloud."""
    return "(link sent in chat)"


def _replace_ordinal(match: re.Match[str]) -> str:
    try:
        words = num2words(int(match.group(1)), to="ordinal", lang="en")
    except (NotImplementedError, OverflowError, TypeError, ValueError, IndexError):
        return match.group(0)
    return _clean_num2words_ordinal_output(words)


def _replace_bullets(text: str) -> str:
    lines = text.splitlines()
    converted: list[str] = []
    bullet_items: list[str] = []

    def flush_bullets() -> None:
        if not bullet_items:
            return
        converted.append("... ".join(bullet_items) + "...")
        bullet_items.clear()

    for line in lines:
        match = _BULLET_LINE_RE.match(line)
        if match:
            item = _replace_markdown_emphasis(match.group(1)).strip()
            item = item.rstrip(" .;:")
            if item:
                bullet_items.append(item)
            continue
        flush_bullets()
        converted.append(line)

    flush_bullets()
    return "\n".join(converted)


def _split_sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_BOUNDARY_RE.split(text) if part.strip()]


def _truncate_at_word(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text.strip()
    truncated = text[:limit].rsplit(" ", 1)[0].strip()
    return truncated or text[:limit].strip()


def _summarize_oversized(text: str) -> str:
    if not text:
        return text

    sentences = _split_sentences(text)
    too_many_sentences = len(sentences) > _MAX_SPOKEN_SENTENCES
    too_long = len(text) > _MAX_SPOKEN_CHARS
    if not too_many_sentences and not too_long:
        return text

    if sentences:
        summary = " ".join(sentences[:_MAX_SPOKEN_SENTENCES])
    else:
        summary = text

    summary = _truncate_at_word(summary, _SUMMARY_CHARS).rstrip(" ,;:")
    if summary and summary[-1] not in ".!?":
        summary += "."
    if summary:
        return "%s %s" % (summary, _SUMMARY_SUFFIX)
    return _SUMMARY_SUFFIX


class SpeechFormatter:
    """Final-boundary formatter for text that will be spoken aloud."""

    def __init__(self, *, summarize: bool = True, config: object | None = None) -> None:
        # Viola's user-facing TTS sets summarize=True so the assistant doesn't
        # drone on at length; long replies get capped at 3 sentences and end
        # with "I'll send the full details in chat." Other speakers piped
        # through this formatter — e.g. the receptionist role-play in
        # tools/phone_receptionist_pipeline.py — must opt out, otherwise
        # human-style multi-sentence prompts get truncated mid-thought and
        # the canned suffix is read aloud verbatim to the caller.
        self._summarize = summarize
        self._config = config

    def format(self, text: str) -> str:
        """Normalize raw LLM/tool output into concise TTS-friendly speech text."""
        try:
            original_text = str(text)
            result = original_text
            cfg = _get_runtime_config(self._config)

            result = _CODE_BLOCK_RE.sub(" ", result)
            result = _INLINE_CODE_RE.sub(r"\1", result)
            result = _MARKDOWN_LINK_RE.sub(_replace_markdown_link, result)
            result = _HEADER_RE.sub("", result)
            result = _BLOCKQUOTE_RE.sub("", result)
            result = _replace_markdown_emphasis(result)
            result = _replace_bullets(result)
            result = _URL_RE.sub(_replace_url, result)
            result = _ORDINAL_RE.sub(_replace_ordinal, result)
            # Specialized number patterns BEFORE generic standalone number conversion.
            # Order matters: currency/phone/percent/time consume specific patterns so
            # the generic regex doesn't mangle them (e.g. phone digits as integers).
            result = _CURRENCY_RE.sub(_replace_currency, result)
            result = _PHONE_RE.sub(_replace_phone, result)
            result = _PERCENT_RE.sub(_replace_percent, result)
            result = _TIME_RE.sub(_replace_time, result)
            result = _YEAR_RE.sub(_replace_year, result)
            result = _STANDALONE_NUMBER_RE.sub(_replace_number, result)
            result = _apply_pronunciation_overrides(result, cfg)
            if getattr(cfg, "tts_brand_dict_enabled", True):
                result = _apply_literal_dictionary(result, _BRAND_PRONUNCIATIONS, _BRAND_RE, ignore_case=True)
            if getattr(cfg, "tts_acronym_dict_enabled", True):
                result = _apply_literal_dictionary(result, _ACRONYM_PRONUNCIATIONS, _ACRONYM_RE)
            result = _ISOLATED_SYMBOL_RE.sub(" ", result)
            result = _WHITESPACE_RE.sub(" ", result).strip()
            if self._summarize:
                result = _summarize_oversized(result)
            if getattr(cfg, "tts_prosody_hints_enabled", True):
                result = _apply_prosody_hints(result)
            result = _WHITESPACE_RE.sub(" ", result).strip()

            logger.debug("TTS speech formatter: %s -> %s", len(original_text), len(result))
            return result
        except Exception as exc:
            logger.debug("TTS speech formatter failed: %s", exc)
            return str(text)


_DEFAULT_FORMATTER = SpeechFormatter(summarize=True)
_VERBATIM_FORMATTER = SpeechFormatter(summarize=False)


def normalize_for_speech(text: str, *, summarize: bool = True) -> str:
    """Normalize raw LLM output into cleaner TTS-friendly speech text.

    ``summarize=False`` keeps every sentence and never appends Viola's
    "I'll send the full details in chat." suffix. Use it for non-Viola
    speakers like the receptionist role-play whose dialogue should not
    be capped at three sentences.
    """
    formatter = _DEFAULT_FORMATTER if summarize else _VERBATIM_FORMATTER
    return formatter.format(text)
