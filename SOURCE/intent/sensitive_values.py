"""Private values the user gave, kept out of every log by value, not by shape.

A delivery address leaked into the logs three builds running (1.0.5 builds 9, 10
and 11) because every mask before this one recognised an address by its shape:
a street-type word, a whole house number, a field name. A street called
"Quillmoor Drift" has no street-type word, a house number heard as "48.12" is
not a whole number, and the address travels in the user's own message, the
model's reasoning and the final answer, none of which has an address field.

So once Viola knows the address (the saved setting, or the arguments of a
``save_delivery_address`` call, even one the gate refuses), the literal values
are held here, in memory only, and :func:`mask` takes them out of any text.
Every log writer passes its text through :func:`mask` (the structured logger,
the file handlers' filter and formatters, the agent-steps writer and the other
writers under the logs folder). Lines written before the value was known (the
user's message is logged before any tool runs) are overwritten in place by
:func:`scrub_recent_logs`.

What is held for one address:

- the street line as given and as stored, and the street name without the
  house number, as a phrase (case-insensitive, spaces, ``+`` or ``%20``);
- each word of the street name that says which street it is (not "Lane",
  "North"), on its own;
- the house number and the ZIP code by their digits, whatever is written
  between them ("48.12", "4812", "48 12", "4 8 1 2"; "77.5" for 775), as a
  whole token. Numbers under three digits are masked only next to the street
  name: on their own they say nothing and are everywhere in a log;
- the city, in any text that also holds the street name, the ZIP code or the
  state (a city alone is a weather or music line, not an address).

Nothing here is written to disk, and nothing is ever dropped while the process
runs: a corrected or replaced address stays masked too, because the old one is
already in the logs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MASK = "[private]"

# Between the words of a phrase: a space, a URL "+" or "%20", an underscore.
_GAP = r"(?:[\s+_]|%20)+"
# Between the digits of a number as speech-to-text or a person writes it.
_DIGIT_SEP = r"(?:[\s,.+\-]|%20|%2[cC]){0,2}"
# A number stands alone: no letter or digit before it (a URL "%20" or "%2C"
# counts as a gap), and no letter or further digit after it.
_NUMBER_BEFORE = r"(?:(?<=%20)|(?<=%2[cC])|(?<![A-Za-z0-9_]))(?<![0-9][.,])"
_NUMBER_AFTER = r"(?![.,]?[0-9])(?![A-Za-z])"
_WORD_BEFORE = r"(?<![A-Za-z])"
_WORD_AFTER = r"(?![A-Za-z])"
# What the other masks leave in place of an address piece (this module's own,
# the address-turn number mask, the street pattern, the in-place clean-up).
_EARLIER_MASK = r"\[(?:private|redacted|REDACTED:ADDRESS)\]|\*{4,}"
_MIN_NUMBER_DIGITS = 3
_MIN_NAME_WORD = 3
# Lower-case text every _EARLIER_MASK match holds.
_EARLIER_MASK_ANCHORS = ("[private]", "[redacted]", "[redacted:address]", "****")
# Words that say nothing about which street it is.
_STREET_FILLER_WORDS = frozenset(
    "n s e w ne nw se sw north south east west northeast northwest southeast southwest "
    "st street ave avenue rd road blvd boulevard dr drive ln lane ct court cir circle pl place way "
    "pkwy parkway hwy highway ter terrace trl trail sq square apt apartment unit suite ste the of and".split()
)
_WORD_RE = re.compile(r"[^\W\d_][\w'.-]*", re.UNICODE)
_NUMBER_RUN_RE = re.compile(r"\d+(?:[^\w\n]{1,3}\d+){0,6}")
_DIGITS_RE = re.compile(r"\d")
# A server process holds many accounts: past this many, a line is masked with
# the values of the account it belongs to (the request's user) only, so one
# line is never checked against thousands of addresses.
_UNION_MAX_USERS = 16
_MAX_ADDRESSES_PER_USER = 32
_PROCESS_USER = "_process"

logger = logging.getLogger("viola.intent.sensitive_values")


@dataclass(frozen=True)
class _AddressRules:
    """The compiled patterns for one address."""

    key: tuple[str, ...]
    always: tuple[str, ...]
    city: str | None
    context: str | None
    # For the log clean-up only (see _scan). A lower-case literal every match of
    # some pattern holds, and a held number's digits with what may sit between
    # them: a stretch of log holding none of these holds no match at all.
    anchors: tuple[str, ...] = ()
    number_anchors: tuple[str, ...] = ()
    # Where whitespace (so a line break) can sit inside a match: for each gap, the
    # characters that can come just before it and just after it.
    joins: tuple[tuple[str, str], ...] = ()
    # The same for the city rule's context: lower-case words, words that count only
    # as written (a two-letter state), the ZIP code's digits.
    context_anchors: tuple[str, ...] = ()
    context_exact: tuple[str, ...] = ()
    context_numbers: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Compiled:
    always: re.Pattern[str] | None
    city_rules: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...]
    always_bytes: re.Pattern[bytes] | None
    city_rules_bytes: tuple[tuple[re.Pattern[bytes], re.Pattern[bytes]], ...]
    anchor_words: tuple[bytes, ...] = ()
    anchor_numbers: tuple[re.Pattern[bytes], ...] = ()
    joins: frozenset[tuple[int, int]] = frozenset()
    # For each of city_rules_bytes: its context's anchors (lower-case, as written, numbers).
    city_contexts: tuple[tuple[tuple[bytes, ...], tuple[bytes, ...], tuple[re.Pattern[bytes], ...]], ...] = ()


_LOCK = threading.RLock()
_RULES: dict[str, list[_AddressRules]] = {}
_COMPILED_BY_USER: dict[str, _Compiled] = {}
_UNION: _Compiled | None = None
_LOADED_USERS: set[str] = set()
_LAST_REGISTERED_AT = 0.0
# Bumped whenever the held values change: a log file scanned with older values
# is scanned again from its tail.
_RULES_VERSION = 0


# --------------------------------------------------------------------------- building


def _phrase(words: list[str]) -> str:
    return _WORD_BEFORE + _GAP.join(re.escape(word) for word in words) + _WORD_AFTER


# What can stand right before / right after the whitespace inside a gap other
# than the words themselves: the end of "+", "_" or "%20", the start of "+", "_" or "%20".
_GAP_LEFT = "+_0"
_GAP_RIGHT = "+_%"
# Between the digits of a number (_DIGIT_SEP): a digit, or the end / start of a separator.
_DIGIT_JOIN = ("0123456789,.+-cC", "0123456789,.+-%")
_ASCII_LETTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _phrase_joins(words: list[str]) -> list[tuple[str, str]]:
    return [(word[-1] + _GAP_LEFT, after[0] + _GAP_RIGHT) for word, after in zip(words, words[1:]) if word and after]


def _longest(words: list[str]) -> str:
    return max(words, key=lambda word: len(word.encode("utf-8")))


def _digits_pattern(digits: str) -> str:
    return _NUMBER_BEFORE + _DIGIT_SEP.join(re.escape(digit) for digit in digits) + _NUMBER_AFTER


def _house_number_and_name(street: str) -> tuple[str, list[str]]:
    """The house number's digits and the street name's words: "48.12 Quillmoor Drift" -> ("4812", [...])."""
    text = street.strip()
    match = re.match(r"^\s*#?(\d+(?:[\s.,-]{1,2}\d+)*)[A-Za-z]?\b", text)
    number = ""
    if match:
        number = "".join(_DIGITS_RE.findall(match.group(1)))
        text = text[match.end() :]
    else:
        # "Quillmoor Drift 4812": a number after the name is the house number too.
        tail = re.search(r"\b(\d+(?:[\s.,-]{1,2}\d+)*)\s*$", text)
        if tail:
            number = "".join(_DIGITS_RE.findall(tail.group(1)))
            text = text[: tail.start()]
    words = [word.strip(".'-") for word in _WORD_RE.findall(text)]
    return number, [word for word in words if word]


def _own_number_forms(own_words: str, digit_values: Iterable[str]) -> list[str]:
    """Numbers the user wrote whose digits are the house number or ZIP code, exactly as written."""
    wanted = {digits for digits in digit_values if len(digits) >= _MIN_NUMBER_DIGITS}
    if not wanted or not own_words:
        return []
    forms: list[str] = []
    for run in _NUMBER_RUN_RE.findall(own_words):
        if "".join(_DIGITS_RE.findall(run)) in wanted and run not in forms:
            forms.append(run)
    return forms


def _field(address: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = address.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return ""


def _address_rules(address: Mapping[str, Any], own_words: str = "", extra_streets: Iterable[str] = ()) -> _AddressRules:
    streets = [
        street for street in (_field(address, "street", "street_line", "address_line1"), *extra_streets) if street
    ]
    city = _field(address, "city", "locality")
    state = _field(address, "state", "region")
    zip_code = _field(address, "zip", "zip_code", "postal_code")
    full_text = _field(address, "full_text")

    always: list[str] = []
    name_word_patterns: list[str] = []
    numbers: list[str] = []
    # Every pattern added to ``always`` (and the city's) adds what it must contain
    # to ``anchors``/``number_anchors`` and its gaps to ``joins``: the log clean-up
    # relies on that to skip text and to cut a file between lines (_scan).
    anchors: list[str] = []
    number_anchors: list[str] = []
    joins: list[tuple[str, str]] = []
    name_words: list[str] = []
    context_anchors: list[str] = []
    context_exact: list[str] = []
    context_numbers: list[str] = []
    for street in streets:
        number, words = _house_number_and_name(street)
        if number:
            numbers.append(number)
        literal_words = street.split()
        if literal_words:
            always.append(_WORD_BEFORE + _GAP.join(re.escape(w) for w in literal_words) + _WORD_AFTER)
            anchors.append(_longest(literal_words))
            joins.extend(_phrase_joins(literal_words))
        if words:
            name = _phrase(words)
            if number:
                number_form = _DIGIT_SEP.join(re.escape(digit) for digit in number)
                always.append(_NUMBER_BEFORE + "#?" + number_form + "[A-Za-z]?" + _GAP + name[len(_WORD_BEFORE) :])
                joins.append(("0123456789" + _ASCII_LETTERS + _GAP_LEFT, words[0][0] + _GAP_RIGHT))
                if len(number) > 1:
                    joins.append(_DIGIT_JOIN)
            always.append(name)
            anchors.append(_longest(words))
            joins.extend(_phrase_joins(words))
            for word in words:
                if len(word) >= _MIN_NAME_WORD and word.lower() not in _STREET_FILLER_WORDS:
                    pattern = _WORD_BEFORE + re.escape(word) + _WORD_AFTER
                    always.append(pattern)
                    name_word_patterns.append(pattern)
                    name_words.append(word)
                    anchors.append(word)
    zip_digits = "".join(_DIGITS_RE.findall(zip_code))[:5]
    digit_values = [number for number in numbers if len(number) >= _MIN_NUMBER_DIGITS]
    if len(zip_digits) >= _MIN_NUMBER_DIGITS:
        digit_values.append(zip_digits)
    for digits in dict.fromkeys(digit_values):
        always.append(_digits_pattern(digits))
        number_anchors.append(_DIGIT_SEP.join(re.escape(digit) for digit in digits))
        joins.append(_DIGIT_JOIN)
    for form in _own_number_forms(own_words, digit_values):
        always.append(_NUMBER_BEFORE + re.escape(form) + _NUMBER_AFTER)
        anchors.append(form)
    if full_text:
        full_words = full_text.split()
        always.append(_WORD_BEFORE + _GAP.join(re.escape(w) for w in full_words) + r"(?![A-Za-z0-9])")
        anchors.append(_longest(full_words))
        joins.extend(_phrase_joins(full_words))

    city_pattern: str | None = None
    context_pattern: str | None = None
    city_words = [word for word in re.split(r"[\s,]+", city) if word]
    if city_words:
        city_pattern = _phrase(city_words)
        anchors.append(_longest(city_words))
        joins.extend(_phrase_joins(city_words))
        # A piece an earlier mask already took out still counts: "Is it Ostbrook,
        # [redacted]?" held the ZIP code before the number mask ran.
        context: list[str] = [_EARLIER_MASK, *name_word_patterns]
        context_anchors.extend([*_EARLIER_MASK_ANCHORS, *name_words])
        if zip_digits and len(zip_digits) >= _MIN_NUMBER_DIGITS:
            context.append(_digits_pattern(zip_digits))
            context_numbers.append(_DIGIT_SEP.join(re.escape(digit) for digit in zip_digits))
        if state:
            if len(state) == 2:
                # "IN", "OR", "ME": a two-letter state counts only in capitals.
                context.append("(?-i:" + _WORD_BEFORE + re.escape(state.upper()) + _WORD_AFTER + ")")
                context_exact.append(state.upper())
            else:
                context.append(_phrase(state.split()))
                context_anchors.append(_longest(state.split()))
                joins.extend(_phrase_joins(state.split()))
        context_pattern = "|".join(context) if context else None

    key = tuple(
        sorted(
            {
                *(street.lower() for street in streets),
                city.lower(),
                state.lower(),
                zip_code,
                full_text.lower(),
                *(form for form in _own_number_forms(own_words, digit_values)),
            }
        )
    )
    return _AddressRules(
        key=key,
        always=tuple(dict.fromkeys(always)),
        city=city_pattern,
        context=context_pattern,
        anchors=tuple(dict.fromkeys(anchor for anchor in anchors if anchor)),
        number_anchors=tuple(dict.fromkeys(number_anchors)),
        joins=tuple(dict.fromkeys(joins)),
        context_anchors=tuple(dict.fromkeys(context_anchors)),
        context_exact=tuple(dict.fromkeys(context_exact)),
        context_numbers=tuple(dict.fromkeys(context_numbers)),
    )


def _edge_bytes(chars: str, *, last: bool) -> set[int]:
    """The byte of each character that touches a gap (its last byte before it, its first after it), both cases."""
    found: set[int] = set()
    for char in chars:
        # A bytes pattern's IGNORECASE folds ASCII letters only.
        for variant in {char, char.lower(), char.upper()} if char.isascii() else {char}:
            encoded = variant.encode("utf-8")
            found.add(encoded[-1] if last else encoded[0])
    return found


def _compile(rules: Iterable[_AddressRules]) -> _Compiled:
    always: list[str] = []
    city_rules: list[tuple[str, str]] = []
    city_contexts: list[tuple[tuple[bytes, ...], tuple[bytes, ...], tuple[re.Pattern[bytes], ...]]] = []
    anchor_words: list[bytes] = []
    anchor_numbers: list[str] = []
    joins: set[tuple[int, int]] = set()
    for rule in rules:
        always.extend(rule.always)
        if rule.city and rule.context:
            city_rules.append((rule.city, rule.context))
            city_contexts.append(
                (
                    tuple(anchor.encode("utf-8").lower() for anchor in rule.context_anchors),
                    tuple(anchor.encode("utf-8") for anchor in rule.context_exact),
                    tuple(re.compile(source.encode("utf-8")) for source in rule.context_numbers),
                )
            )
        # bytes.lower() folds ASCII only, like a bytes pattern's IGNORECASE.
        anchor_words.extend(anchor.encode("utf-8").lower() for anchor in rule.anchors)
        anchor_numbers.extend(rule.number_anchors)
        for left, right in rule.joins:
            joins.update((a, b) for a in _edge_bytes(left, last=True) for b in _edge_bytes(right, last=False))
    # Longer patterns first, so a whole street line is one mask, not three.
    ordered = sorted(dict.fromkeys(always), key=len, reverse=True)
    union = "|".join("(?:%s)" % pattern for pattern in ordered)
    return _Compiled(
        always=re.compile(union, re.IGNORECASE) if ordered else None,
        city_rules=tuple((re.compile(c, re.IGNORECASE), re.compile(x, re.IGNORECASE)) for c, x in city_rules),
        always_bytes=re.compile(union.encode("utf-8"), re.IGNORECASE) if ordered else None,
        city_rules_bytes=tuple(
            (re.compile(c.encode("utf-8"), re.IGNORECASE), re.compile(x.encode("utf-8"), re.IGNORECASE))
            for c, x in city_rules
        ),
        anchor_words=tuple(dict.fromkeys(anchor_words)),
        anchor_numbers=tuple(re.compile(source.encode("utf-8")) for source in dict.fromkeys(anchor_numbers)),
        joins=frozenset(joins),
        city_contexts=tuple(city_contexts),
    )


def _rebuild_locked(user_id: str) -> None:
    global _UNION, _RULES_VERSION
    _RULES_VERSION += 1
    _COMPILED_BY_USER[user_id] = _compile(_RULES.get(user_id, []))
    _UNION = _compile(rule for rules in _RULES.values() for rule in rules) if len(_RULES) <= _UNION_MAX_USERS else None


# --------------------------------------------------------------------------- registering


def _normal_user(user_id: str | None) -> str:
    text = str(user_id or "").strip()
    return text or _PROCESS_USER


def register_address(
    user_id: str | None,
    address: Mapping[str, Any] | None,
    *,
    own_words: str = "",
    extra_streets: Iterable[str] = (),
    scrub: bool = True,
) -> bool:
    """Hold the literal values of one address for *user_id*; True when something new was added.

    ``address`` takes the setting's keys (street, city, state, zip) or the tool's
    (zip_code). ``own_words`` is what the user said in this conversation: a number
    in it whose digits are the house number or ZIP code is held as written too.
    With ``scrub`` (the default) earlier lines in recently changed log files are
    overwritten in the background.
    """
    global _LAST_REGISTERED_AT
    if not isinstance(address, Mapping):
        return False
    try:
        rules = _address_rules(address, own_words=own_words, extra_streets=extra_streets)
    except (re.error, TypeError, ValueError):
        logger.debug("Could not build the private-value patterns for an address", exc_info=True)
        return False
    if not rules.always and not rules.city:
        return False
    user = _normal_user(user_id)
    with _LOCK:
        held = _RULES.setdefault(user, [])
        if any(existing.key == rules.key for existing in held):
            return False
        held.append(rules)
        if len(held) > _MAX_ADDRESSES_PER_USER:
            del held[0]
        _rebuild_locked(user)
        _LAST_REGISTERED_AT = time.time()
    if scrub:
        schedule_scrub()
    return True


def register_tool_call(user_id: str | None, tool_args: Mapping[str, Any] | None, *, own_words: str = "") -> bool:
    """Hold a ``save_delivery_address`` call's raw arguments before the tool runs (also when it is refused)."""
    if not isinstance(tool_args, Mapping):
        return False
    return register_address(user_id, tool_args, own_words=own_words)


def load_saved_address(user_id: str | None, *, force: bool = False) -> bool:
    """Hold the address saved in Settings for *user_id* (once per account per process, or again with ``force``)."""
    user = _normal_user(user_id)
    with _LOCK:
        if user in _LOADED_USERS and not force:
            return False
        _LOADED_USERS.add(user)
    try:
        from ui.settings_manager import get_settings_manager

        saved = get_settings_manager().get("delivery_address", {}, user_id=None if user == _PROCESS_USER else user)
    except Exception:  # noqa: BLE001 - a settings read must never break a command or start-up
        logger.debug("Saved delivery address unavailable for the log mask", exc_info=True)
        with _LOCK:
            _LOADED_USERS.discard(user)
        return False
    if not isinstance(saved, Mapping):
        return False
    return register_address(user, saved)


def note_settings_write(user_id: str | None, updates: Mapping[str, Any]) -> None:
    """Called by the settings store before a write: a new delivery address is held before anything logs it."""
    value = updates.get("delivery_address") if isinstance(updates, Mapping) else None
    if isinstance(value, Mapping):
        register_address(user_id, value)


def has_values() -> bool:
    return bool(_COMPILED_BY_USER)


def _reset_for_tests() -> None:
    global _UNION, _LAST_REGISTERED_AT
    wait_for_scrub(5.0)
    with _LOCK:
        _RULES.clear()
        _COMPILED_BY_USER.clear()
        _LOADED_USERS.clear()
        _UNION = None
        _LAST_REGISTERED_AT = 0.0
    with _SCRUB_LOCK:
        _SCANNED.clear()


# --------------------------------------------------------------------------- masking


def _current_compiled() -> _Compiled | None:
    union = _UNION
    if union is not None:
        return union
    if not _COMPILED_BY_USER:
        return None
    try:
        from core.user_context import get_current_user_id

        return _COMPILED_BY_USER.get(_normal_user(get_current_user_id()))
    except (ImportError, LookupError):
        return None


def _mask_with(compiled: _Compiled, text: str, context_text: str | None = None) -> str:
    out = text
    context = text if context_text is None else context_text
    for city_re, context_re in compiled.city_rules:
        if city_re.search(out) and context_re.search(context):
            out = city_re.sub(MASK, out)
    if compiled.always is not None:
        out = compiled.always.sub(MASK, out)
    return out


def mask(text: Any) -> Any:
    """Return *text* with every held private value replaced by ``[private]``; anything not a str is returned as is."""
    if not isinstance(text, str) or not text:
        return text
    compiled = _current_compiled()
    if compiled is None:
        return text
    try:
        return _mask_with(compiled, text)
    except (re.error, RecursionError):  # pragma: no cover - defensive: never break a log line
        return text


def _strings(value: Any, depth: int = 0) -> Iterable[str]:
    if depth > 40:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item, depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _strings(item, depth + 1)


def _mask_tree(compiled: _Compiled, value: Any, context: str, depth: int = 0) -> Any:
    if depth > 40:
        return value
    if isinstance(value, str):
        return _mask_with(compiled, value, context) if value else value
    if isinstance(value, Mapping):
        return {key: _mask_tree(compiled, item, context, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_tree(compiled, item, context, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_mask_tree(compiled, item, context, depth + 1) for item in value)
    if isinstance(value, (set, frozenset)):
        return type(value)(_mask_tree(compiled, item, context, depth + 1) for item in value)
    return value


def mask_value(value: Any) -> Any:
    """:func:`mask` every string inside *value* (dicts, lists, tuples); the city rule sees the whole value."""
    compiled = _current_compiled()
    if compiled is None:
        return value
    if isinstance(value, str):
        return mask(value)
    try:
        context = "\n".join(_strings(value))
        return _mask_tree(compiled, value, context)
    except (re.error, RecursionError):  # pragma: no cover - defensive
        return value


def mask_json_line(line: str) -> str:
    """Mask one JSON log line and keep it valid JSON."""
    masked = mask(line)
    if masked == line:
        return line
    ending = "\n" if line.endswith("\n") else ""
    try:
        json.loads(masked)
        return masked
    except ValueError:
        pass
    try:
        return json.dumps(mask_value(json.loads(line)), default=str) + ending
    except ValueError:
        return masked


class SensitiveValueFilter(logging.Filter):
    """A file handler's filter: the record's text and its exception text, masked before it is formatted.

    Records from loggers that never pass ``core.logging_config`` (plain
    ``logging.getLogger``, third-party code) reach the file only through here.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if _current_compiled() is None:
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not break logging
            message = str(record.msg)
        masked = mask(message)
        if masked != message:
            record.msg = masked
            record.args = None
        if record.exc_info and not record.exc_text:
            try:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            except Exception:  # noqa: BLE001
                record.exc_text = None
        if record.exc_text:
            record.exc_text = mask(record.exc_text)
        if record.stack_info:
            record.stack_info = mask(record.stack_info)
        return True


def install_filter(handler: logging.Handler) -> logging.Handler:
    """Add one :class:`SensitiveValueFilter` to *handler* (idempotent)."""
    if not any(isinstance(existing, SensitiveValueFilter) for existing in handler.filters):
        handler.addFilter(SensitiveValueFilter())
    return handler


# --------------------------------------------------------------------------- lines already written

SCRUB_MAX_AGE_SECONDS = 15 * 60
SCRUB_TAIL_BYTES = 8 * 1024 * 1024
_TEXT_SUFFIXES = (".log", ".jsonl", ".json", ".txt", ".csv")
_SCRUB_LOCK = threading.Lock()
_SCRUB_STATE = threading.Condition()
_scrub_pending = False
_scrub_running = False
# The clean-up runs on a background thread, but ``re`` holds the GIL for a whole
# search: one search over an 8 MB tail stopped every other Python thread in Viola
# (audio, the wake listener, the API server) for about 3 s. So a pass reads only
# what it has not scanned with the current values, works in pieces of about this
# size cut where no value can run across, and sleeps between pieces.
_CHUNK_BYTES = 64 * 1024
_CHUNK_PAUSE_SECONDS = 0.002
# How far back a pass that resumes a file first reads, to find a line break no
# value can run across (four times further each time that is not enough).
_LOOKBACK_BYTES = 4 * 1024
# A line longer than this has the city rule's context searched around its anchors only.
_LONG_LINE_BYTES = 4096
# What ``\s`` matches in a bytes pattern.
_WHITESPACE = frozenset(b" \t\n\r\x0b\x0c")


@dataclass(frozen=True)
class _FileScan:
    """How far one log file was scanned, and with which held values."""

    version: int
    tail_bytes: int
    identity: int
    # The end of the last complete line scanned: a line still being written is scanned again.
    offset: int


_SCANNED: dict[str, _FileScan] = {}
_SCRUB_COMPILED: tuple[int, _Compiled | None] = (-1, None)


def _is_text_log(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) or (suffix + ".") in name for suffix in _TEXT_SUFFIXES)


def _log_files(roots: Iterable[Path], newer_than: float) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            candidates: Iterable[Path] = [root]
        elif root.is_dir():
            candidates = (Path(base) / name for base, _dirs, names in os.walk(root) for name in names)
        else:
            continue
        for path in candidates:
            try:
                if _is_text_log(path) and path.stat().st_mtime >= newer_than:
                    files.append(path)
            except OSError:
                continue
    return files


def _spans(
    compiled: _Compiled,
    buffer: bytes,
    start: int = 0,
    end: int | None = None,
    line_lo: int | None = None,
    line_hi: int | None = None,
) -> list[tuple[int, int]]:
    """Every held value in ``buffer[start:end]``, by one regex pass over that stretch.

    The city rule looks at the whole line around a city match, within
    ``buffer[line_lo:line_hi]`` (by default the stretch itself).
    """
    end = len(buffer) if end is None else end
    line_lo = start if line_lo is None else line_lo
    line_hi = end if line_hi is None else line_hi
    spans: list[tuple[int, int]] = []
    for index, (city_re, context_re) in enumerate(compiled.city_rules_bytes):
        for match in city_re.finditer(buffer, start, end):
            newline = buffer.rfind(b"\n", line_lo, match.start())
            line_start = newline + 1 if newline >= 0 else line_lo
            line_end = buffer.find(b"\n", match.end(), line_hi)
            if line_end < 0:
                line_end = line_hi
            if _context_in_line(compiled, index, context_re, buffer, line_start, line_end):
                spans.append(match.span())
    if compiled.always_bytes is not None:
        spans.extend(match.span() for match in compiled.always_bytes.finditer(buffer, start, end))
    return spans


def _context_in_line(
    compiled: _Compiled, index: int, context_re: re.Pattern[bytes], buffer: bytes, start: int, end: int
) -> bool:
    """``context_re.search(buffer[start:end])``, the line around a city match.

    A long line (a page of text in a tool result) is searched only around the
    context's anchors, from the nearest place no value runs across before each to
    the nearest one after it: one search over a 400 KB line held the GIL for 36 ms.
    """
    if end - start <= _LONG_LINE_BYTES or index >= len(compiled.city_contexts):
        return context_re.search(buffer[start:end]) is not None
    words, exact, numbers = compiled.city_contexts[index]
    low = buffer[start:end].lower()
    hits: list[tuple[int, int]] = []
    for word in words:
        found = low.find(word)
        while found >= 0:
            hits.append((start + found, start + found + len(word)))
            found = low.find(word, found + len(word))
    for word in exact:
        found = buffer.find(word, start, end)
        while found >= 0:
            hits.append((found, found + len(word)))
            found = buffer.find(word, found + len(word), end)
    for number in numbers:
        match = number.search(low)
        while match is not None:
            hits.append((start + match.start(), start + match.end()))
            match = number.search(low, match.end())
    searched_to = start
    for hit_start, hit_end in sorted(hits):
        if hit_end <= searched_to:
            continue
        window_start = _back_to_cut(compiled, buffer, hit_start, start, start, end)
        window_end = _forward_to_cut(compiled, buffer, hit_end, end, start, end)
        if context_re.search(buffer, window_start, window_end):
            return True
        searched_to = window_end
    return False


def _neighbours(buffer: bytes, cut: int, lo: int, hi: int) -> tuple[int, int]:
    """The last non-blank byte before ``cut`` and the first at or after it (``lo - 1`` / ``hi`` when none)."""
    before = cut - 1
    while before >= lo and buffer[before] in _WHITESPACE:
        before -= 1
    after = cut
    while after < hi and buffer[after] in _WHITESPACE:
        after += 1
    return before, after


def _can_cut(compiled: _Compiled, buffer: bytes, cut: int, lo: int, hi: int) -> bool:
    """True when no held value can run across ``cut``, a place just after whitespace in ``buffer[lo:hi]``.

    Inside a match, whitespace sits only in a gap between two words or two digits,
    and no match starts or ends with whitespace. So a match across this place
    holds the last non-blank byte before it and the first one after it, and that
    pair is one a held pattern joins (``_Compiled.joins``). The lookaheads that end
    a match refuse only a letter or a digit, which whitespace and the end of a
    stretch both satisfy; lookbehinds see the bytes before a stretch. ``lo`` and
    ``hi`` bound what this pass scans: nothing beyond them is matched in it anyway.
    """
    before, after = _neighbours(buffer, cut, lo, hi)
    if before < lo or after >= hi:
        return True
    return (buffer[before], buffer[after]) not in compiled.joins


_WHITESPACE_RUN = re.compile(rb"\s+")


def _back_to_cut(compiled: _Compiled, buffer: bytes, index: int, floor: int, lo: int, hi: int) -> int:
    """The nearest place at or before ``index`` no value runs across (``floor`` when there is none)."""
    end = index
    step = 256
    while end > floor:
        begin = max(floor, end - step)
        for run in reversed(list(_WHITESPACE_RUN.finditer(buffer, begin, end))):
            if _can_cut(compiled, buffer, run.end(), lo, hi):
                return run.end()
        end = begin
        step *= 4
    return floor


def _forward_to_cut(compiled: _Compiled, buffer: bytes, index: int, ceiling: int, lo: int, hi: int) -> int:
    """The nearest place at or after ``index`` no value runs across (``ceiling`` when there is none)."""
    pos = index
    while pos < ceiling:
        run = _WHITESPACE_RUN.search(buffer, pos, ceiling)
        if run is None:
            break
        if _can_cut(compiled, buffer, run.end(), lo, hi):
            return run.end()
        pos = run.end()
    return ceiling


def _next_cut(compiled: _Compiled, buffer: bytes, pos: int, lo: int, hi: int) -> int:
    """The end of the piece that starts at ``pos``: the last line break no value runs across within
    _CHUNK_BYTES, else the last such space (a line longer than a piece), else the first one after."""
    target = pos + _CHUNK_BYTES
    if target >= hi:
        return hi
    newline = buffer.rfind(b"\n", pos, target)
    while newline >= pos:
        if _can_cut(compiled, buffer, newline + 1, lo, hi):
            return newline + 1
        newline = buffer.rfind(b"\n", pos, newline)
    cut = _back_to_cut(compiled, buffer, target, pos, lo, hi)
    return cut if cut > pos else _forward_to_cut(compiled, buffer, target, hi, lo, hi)


def _scan_piece(compiled: _Compiled, buffer: bytes, pos: int, cut: int, lo: int, hi: int) -> list[tuple[int, int]]:
    """:func:`_spans` over ``buffer[pos:cut]``, run only where a match can be.

    Every match holds one of the held values' anchors (a word of it, lower-case, or
    a number's digits) and lies between two places no value runs across. So the
    regex runs only from the nearest such place before each anchor to the nearest
    one after it, usually a word or two; the rest of the piece costs a ``lower()``
    and a few ``find()`` calls.
    """
    low = buffer[pos:cut].lower()
    hits: list[tuple[int, int]] = []
    for word in compiled.anchor_words:
        found = low.find(word)
        while found >= 0:
            hits.append((pos + found, pos + found + len(word)))
            found = low.find(word, found + len(word))
    for number in compiled.anchor_numbers:
        match = number.search(low)
        while match is not None:
            hits.append((pos + match.start(), pos + match.end()))
            match = number.search(low, max(match.end(), match.start() + 1))
    if not hits:
        return []
    windows: list[list[int]] = []
    for start, end in sorted(hits):
        if windows and end <= windows[-1][1]:
            continue  # inside the last window already
        start = _back_to_cut(compiled, buffer, start, pos, lo, hi)
        end = _forward_to_cut(compiled, buffer, end, cut, lo, hi)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])
    spans: list[tuple[int, int]] = []
    for start, end in windows:
        spans.extend(_spans(compiled, buffer, start, end, lo, hi))
    return spans


def _scan(compiled: _Compiled, buffer: bytes, start: int, end: int) -> list[tuple[int, int]]:
    """The spans one :func:`_spans` call over ``buffer[start:end]`` finds, found piece by piece.

    ``start`` is a line start. Pieces end where no value can run across, so no
    match is split; each is about _CHUNK_BYTES, and the thread sleeps between
    pieces so no other thread waits long on the GIL.
    """
    spans: list[tuple[int, int]] = []
    pos = start
    while pos < end:
        cut = _next_cut(compiled, buffer, pos, start, end)
        spans.extend(_scan_piece(compiled, buffer, pos, cut, start, end))
        pos = cut
        if pos < end and _CHUNK_PAUSE_SECONDS > 0:
            time.sleep(_CHUNK_PAUSE_SECONDS)
    return spans


_LETTER_BYTES = re.compile(rb"[A-Za-z]")
_DIGIT_BYTES = re.compile(rb"[0-9]")


def _overwrite(found: bytes) -> bytes:
    """Same length. A bare number (a house number or ZIP code on its own) becomes 9s, so a
    JSON line that held it as a number stays valid JSON; anything with letters becomes ``*``."""
    if _LETTER_BYTES.search(found):
        return b"*" * len(found)
    return _DIGIT_BYTES.sub(b"9", found)


def _read(path: Path, start: int, size: int) -> bytes:
    with open(path, "rb") as handle:
        handle.seek(start)
        return handle.read(size - start)


def _file_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _resume_point(compiled: _Compiled, buffer: bytes, at: int, lo: int | None) -> int | None:
    """The nearest line start at or before ``at`` that no value runs across.

    ``lo`` is where this pass's scan may start at the earliest (the tail's first
    line), or None when ``buffer`` starts in the middle of the file: then None
    means "read further back".
    """
    floor = 0 if lo is None else lo
    cut = at
    while cut > floor:
        before, after = _neighbours(buffer, cut, floor, len(buffer))
        if before < floor:
            return None if lo is None else cut
        if after >= len(buffer) or (buffer[before], buffer[after]) not in compiled.joins:
            return cut
        newline = buffer.rfind(b"\n", floor, cut - 1)
        if newline < 0:
            return None if lo is None else floor
        cut = newline + 1
    return None if lo is None else floor


def _scrub_file(path: Path, compiled: _Compiled, tail_bytes: int, version: int = -1) -> int:
    """Mask held values in the part of *path* not yet scanned with these values (the last *tail_bytes* at most)."""
    key = _file_key(path)
    try:
        stat = os.stat(path)
    except OSError:
        _SCANNED.pop(key, None)
        return 0
    size = stat.st_size
    floor = max(0, size - tail_bytes)
    seen = _SCANNED.get(key)
    resume = -1
    if (
        seen is not None
        and (seen.version, seen.tail_bytes, seen.identity) == (version, tail_bytes, stat.st_ino)
        and seen.offset <= size
    ):
        if seen.offset == size:
            return 0  # nothing appended: nothing read
        if seen.offset > floor:
            resume = seen.offset
    try:
        begin = -1
        if resume >= 0:
            # Only the lines appended since, from the nearest earlier line break no value runs across.
            lookback = _LOOKBACK_BYTES
            while True:
                read_from = max(floor, resume - lookback)
                buffer = _read(path, read_from, size)
                at = resume - read_from
                if at > len(buffer) or buffer[at - 1 : at] != b"\n":
                    break  # rewritten under the same name: scan the tail
                lo: int | None = None
                if read_from == floor:
                    lo = 0 if floor == 0 else buffer.find(b"\n") + 1
                point = _resume_point(compiled, buffer, at, lo)
                if point is not None:
                    begin = point
                    break
                lookback *= 4
        if begin < 0:
            read_from = floor
            buffer = _read(path, read_from, size)
            begin = 0
            if read_from:
                # Start on a whole line.
                newline = buffer.find(b"\n")
                if newline < 0:
                    return 0
                begin = newline + 1
    except OSError:
        return 0
    end_of_lines = buffer.rfind(b"\n", begin)
    offset = read_from + (end_of_lines + 1 if end_of_lines >= 0 else begin)
    _SCANNED[key] = _FileScan(version, tail_bytes, stat.st_ino, max(offset, resume))
    if b"\x00" in buffer[begin : begin + 4096]:
        return 0
    spans = _scan(compiled, buffer, begin, len(buffer))
    if not spans:
        return 0
    written = 0
    try:
        # In place, same length: no rename and no truncate, so a handle the
        # logger holds open keeps appending where it was.
        with open(path, "r+b") as handle:
            for start, end in sorted(set(spans)):
                if end > start:
                    handle.seek(read_from + start)
                    handle.write(_overwrite(buffer[start:end]))
                    written += 1
    except OSError:
        # Scan its tail again next pass, as before.
        _SCANNED.pop(key, None)
        logger.debug("Could not overwrite private values in a log file", exc_info=True)
    return written


def _default_roots() -> list[Path]:
    roots: list[Path] = []
    try:
        from core.platform import get_logs_dir

        logs = get_logs_dir()
        roots.append(logs)
        # The AI debug session log sits beside the logs folder (observability_logging).
        roots.append(logs.parent / "scripts" / "ai_debug_session.log")
    except Exception:  # noqa: BLE001
        logger.debug("Logs folder unavailable for the private-value clean-up", exc_info=True)
    return roots


def _session_memory_files(user_id: str, memory_root: Path | None, newer_than: float) -> list[Path]:
    """One account's per-task session memory files changed since *newer_than*.

    ``users/<id>/memory/session_memory/*.md`` (``services/conversation/session_memory``)
    holds the turn's own words (the task line, tool arguments and results), written
    before an address in them may be known. Only this account's own folder.
    """
    try:
        from services.memory.dir import account_root

        folder = account_root(user_id, memory_root) / "memory" / "session_memory"
        if not folder.is_dir():
            return []
        files: list[Path] = []
        with os.scandir(folder) as entries:
            for entry in entries:
                if not entry.name.lower().endswith(".md"):
                    continue
                try:
                    if entry.is_file() and entry.stat().st_mtime >= newer_than:
                        files.append(Path(entry.path))
                except OSError:
                    continue
        return files
    except Exception:  # noqa: BLE001 - the clean-up must never take the app down
        logger.debug("Session memory folder unavailable for the private-value clean-up", exc_info=True)
        return []


def scrub_recent_logs(
    roots: Iterable[Path] | Path | None = None,
    *,
    max_age_seconds: float = SCRUB_MAX_AGE_SECONDS,
    tail_bytes: int = SCRUB_TAIL_BYTES,
    memory_root: Path | None = None,
) -> int:
    """Overwrite held private values with ``*`` (a bare number with ``9``) in log files changed recently.

    Same byte length, in place, only the last *tail_bytes* of each file. A file
    already scanned with the same values is scanned only from where the last pass
    stopped (the start of its last whole line); new values, or a file that shrank
    or was replaced, mean its tail is scanned again. Returns how many places were
    overwritten. Uses every account's values for the log files.

    The default pass (no *roots*) and a pass given *memory_root* also cover each
    account's session memory files changed recently, by the same rules, each
    with that account's own values only (never another account's folder).
    """
    global _SCRUB_COMPILED
    with _LOCK:
        version = _RULES_VERSION
        if not _RULES:
            compiled = None
        elif _SCRUB_COMPILED[0] == version:
            compiled = _SCRUB_COMPILED[1]
        else:
            compiled = _compile(rule for rules in _RULES.values() for rule in rules)
            _SCRUB_COMPILED = (version, compiled)
        accounts = [(user, user_compiled) for user, user_compiled in _COMPILED_BY_USER.items() if user != _PROCESS_USER]
    if compiled is None:
        return 0
    include_session_memory = roots is None or memory_root is not None
    if roots is None:
        root_list = _default_roots()
    elif isinstance(roots, Path):
        root_list = [roots]
    else:
        root_list = list(roots)
    newer_than = time.time() - max_age_seconds
    total = 0
    with _SCRUB_LOCK:
        jobs: list[tuple[Path, _Compiled]] = [(path, compiled) for path in _log_files(root_list, newer_than)]
        if include_session_memory:
            for user, user_compiled in accounts:
                jobs.extend((path, user_compiled) for path in _session_memory_files(user, memory_root, newer_than))
        # Forget files that are gone or no longer recent, so this cannot grow without bound.
        current = {_file_key(path) for path, _rules in jobs}
        for key in [key for key in _SCANNED if key not in current]:
            del _SCANNED[key]
        for path, rules in jobs:
            total += _scrub_file(path, rules, tail_bytes, version)
    if total:
        # A count only: the values themselves are never logged.
        logger.info("Private-value log clean-up: masked=%d in log files changed in the last 15 minutes", total)
    return total


def _scrub_worker() -> None:
    global _scrub_pending, _scrub_running
    while True:
        with _SCRUB_STATE:
            if not _scrub_pending:
                _scrub_running = False
                _SCRUB_STATE.notify_all()
                return
            _scrub_pending = False
        try:
            scrub_recent_logs()
        except Exception:  # noqa: BLE001 - the clean-up must never take the app down
            logger.debug("Private-value log clean-up failed", exc_info=True)


def schedule_scrub() -> None:
    """Run :func:`scrub_recent_logs` on a background thread (calls while one runs coalesce into one more pass)."""
    global _scrub_pending, _scrub_running
    with _SCRUB_STATE:
        _scrub_pending = True
        if _scrub_running:
            return
        _scrub_running = True
    threading.Thread(target=_scrub_worker, name="viola-private-log-scrub", daemon=True).start()


def wait_for_scrub(timeout: float = 10.0) -> bool:
    """Wait until no background clean-up is pending or running."""
    deadline = time.monotonic() + timeout
    with _SCRUB_STATE:
        while _scrub_running or _scrub_pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _SCRUB_STATE.wait(remaining)
    return True


def note_task_finished() -> None:
    """After a task: if an address was registered in the last 15 minutes, clean up again.

    Lines written during the rest of that task by any writer are covered too.
    """
    if _LAST_REGISTERED_AT and time.time() - _LAST_REGISTERED_AT < SCRUB_MAX_AGE_SECONDS:
        schedule_scrub()


__all__ = [
    "MASK",
    "SensitiveValueFilter",
    "has_values",
    "install_filter",
    "load_saved_address",
    "mask",
    "mask_json_line",
    "mask_value",
    "note_settings_write",
    "note_task_finished",
    "register_address",
    "register_tool_call",
    "scrub_recent_logs",
    "schedule_scrub",
    "wait_for_scrub",
]
