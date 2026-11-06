"""Which language each part of a reply is in, so it reaches the right voice.

The speech language is a setting, and it used to decide the whole reply. An
English-setting Viola asked to "say good morning in Spanish" read "Buenos días"
through the English pronunciation route (it sounded wrong), and "早上好" could
not be read at all, so the words were dropped and she went silent (1.0.5 build-9
laptop report, J10). The Spanish and Mandarin routes and voices were installed
the whole time; nothing sent text to them.

:func:`speech_spans` cuts a sentence into spans by language:

- Han text goes to the Mandarin route. Other scripts with no route (kana,
  Hangul, Cyrillic, Arabic...) are reported as ``UNSUPPORTED`` so the engine can
  say it cannot read them, instead of going quiet.
- Under an English setting, Spanish is found from a sentence's own words. An
  English sentence only gives up words to Spanish on strong evidence: names like
  "Los Angeles", "Buenos Aires" or "José" stay English.

Pure text work: no model, no settings, no I/O. This decides how a reply is
pronounced, never what happens with a command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BASE = "base"
SPANISH = "es"
MANDARIN = "zh"
UNSUPPORTED = "unsupported"

# Said once per reply, in the reply's own language, in place of text no route can read.
UNSPOKEN_NOTICE = {
    "en": "I can't say that part out loud, but it's on the screen.",
    "es": "No puedo decir esa parte en voz alta, pero está en la pantalla.",
    "zh": "这部分我读不出来，请看屏幕。",
}

_HAN = r"㐀-䶿一-鿿豈-﫿"
_KANA = r"぀-ヿㇰ-ㇿ"
# CJK punctuation and the full-width forms of ASCII punctuation.
_CJK_MARKS = r"　-〿！-／：-＠～"
# A run of Chinese: Han with its own punctuation, plus any digits written inside it.
_CJK_RUN_RE = re.compile(
    "[%s%s](?:[%s%s%s]|[\\d.,%%]+(?=[%s%s]))*" % (_HAN, _KANA, _HAN, _KANA, _CJK_MARKS, _HAN, _KANA)
)
_KANA_RE = re.compile("[%s]" % _KANA)
# Two letters or more: one Greek letter in a formula is not a language.
_OTHER_SCRIPT_RE = re.compile("(?:[^\\W\\d_](?<![\\u0000-\\u024f]))(?:[^\\W\\d_](?<![\\u0000-\\u024f])|[\\s'’-])*")

# Tone marks only pinyin uses. A romanization beside the Chinese it spells is
# not read twice.
_PINYIN_MARKS = frozenset("āēīōūǎěǐǒǔǖǘǚǜ")
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")
_QUOTED_RE = re.compile(r'"([^"]+)"|“([^”]+)”|«([^»]+)»|(?<![\w])[\'‘]([^\'’]+)[\'’](?!\w)')

# Words that are Spanish and not English. A short list on purpose: greetings and
# everyday words. Spanish words English also uses as names are left out.
_ES_STRONG = frozenset(
    """
    hola adiós adios gracias buenos buenas días dias noches tardes señor señora señorita perdón perdon disculpa
    cómo qué quién quien cuándo cuando dónde donde cuál cual porqué porque pero también tambien muy está están
    estás estoy estamos eres somos tengo tienes tiene tenemos quiero quieres quiere puedo puedes puede hacer
    hoy ayer mañana ahora aquí aqui allí alli siempre nunca mucho mucha muchos muchas gusto encantado encantada
    hasta luego vemos feliz cumpleaños navidad bienvenido bienvenida siento favor esto esta eso
    bien bueno buena estar nosotros ustedes ellos ellas usted tú él
    """.split()
)
# Spanish function words. Not evidence alone: "La La Land", "Rio de la Plata".
_ES_NEUTRAL = frozenset(
    "de la el los las un una unos unas y o en es son con para por que del al lo se su sus mi tu te le nos más sin "
    "sobre".split()
)
# Words that show a sentence is English whatever else is in it.
_EN_COMMON = frozenset(
    """
    the is are was were be been of to and in on at for with from by you your i my we our they their it its this that
    these those he she his her have has had do does did will would can could should say says said means mean word
    called how what when where why which who good morning hello hi not but or if as an
    """.split()
)
_ACCENTED_RE = re.compile(r"[áéíóúüñ]", re.IGNORECASE)


@dataclass(frozen=True)
class SpeechSpan:
    """A piece of a sentence and the route that should read it."""

    language: str
    text: str


def _is_english_base(base: str) -> bool:
    return base.lower().replace("_", "-").split("-", 1)[0] == "en"


def _base_language(base: str) -> str:
    return base.lower().replace("_", "-").split("-", 1)[0]


def _classify(word: str) -> str:
    lowered = word.lower()
    if lowered in _ES_STRONG:
        return "strong"
    if lowered in _ES_NEUTRAL:
        return "neutral"
    if lowered in _EN_COMMON:
        return "english"
    return "accented" if _ACCENTED_RE.search(lowered) else "other"


def _reads_as_spanish(text: str) -> bool:
    """Whether a whole sentence (or quoted phrase) is Spanish."""
    if "¿" in text or "¡" in text:
        return True
    words = [match.group(0) for match in _WORD_RE.finditer(text)]
    if not words:
        return False
    kinds = [_classify(word) for word in words]
    strong = kinds.count("strong")
    neutral = kinds.count("neutral")
    accented = kinds.count("accented")
    if "english" in kinds:
        return False
    if strong:
        return (strong + neutral + accented) * 3 >= len(words)
    if accented and neutral and len(words) >= 3:
        return (accented + neutral) * 2 >= len(words)
    # Function words alone need a real sentence: "Son las tres y media", never
    # a place name like "Rio de la Plata".
    return len(words) >= 5 and neutral * 2 > len(words)


def _spanish_ranges(text: str) -> list[tuple[int, int]]:
    """Character ranges of Spanish phrases inside an English sentence."""
    ranges: list[tuple[int, int]] = []
    for match in _QUOTED_RE.finditer(text):
        group = next(index for index in range(1, 5) if match.group(index) is not None)
        if _reads_as_spanish(match.group(group)):
            ranges.append((match.start(group), match.end(group)))
    words = list(_WORD_RE.finditer(text))
    index = 0
    while index < len(words):
        if _classify(words[index].group(0)) not in {"strong", "neutral"}:
            index += 1
            continue
        end = index
        strong = 0
        last_strong = -1
        first_strong = -1
        while end < len(words):
            kind = _classify(words[end].group(0))
            if kind not in {"strong", "neutral"}:
                break
            if end > index and text[words[end - 1].end() : words[end].start()].strip(" ,") != "":
                break
            if kind == "strong":
                strong += 1
                last_strong = end
                first_strong = end if first_strong < 0 else first_strong
            end += 1
        # Two Spanish-only words side by side: "buenos días", "por favor" counts
        # "favor" alone, so "por" is what makes that pair.
        if strong >= 2 or (strong == 1 and last_strong > index and words[index].group(0).lower() == "por"):
            first = index if words[index].group(0).lower() == "por" else first_strong
            ranges.append((words[first].start(), words[last_strong].end()))
        index = max(end, index + 1)
    return _merged(ranges)


def _merged(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _drop_pinyin(text: str) -> str:
    """Remove a romanization that sits beside its own Chinese text."""

    def keep(match: re.Match[str]) -> str:
        return "" if _PINYIN_MARKS & set(match.group(0).lower()) else match.group(0)

    return re.sub(r"\(\s*\)|\[\s*\]", " ", _WORD_RE.sub(keep, text))


def _speakable(text: str) -> bool:
    return any(character.isalnum() for character in text)


def _script_spans(text: str, base_language: str) -> list[SpeechSpan]:
    """Split on script: Chinese and scripts with no route, everything else stays BASE."""
    spans: list[SpeechSpan] = []
    cursor = 0
    pieces: list[tuple[int, int, str]] = []
    for match in _CJK_RUN_RE.finditer(text):
        language = UNSUPPORTED if _KANA_RE.search(match.group(0)) else MANDARIN
        pieces.append((match.start(), match.end(), language))
    taken = list(pieces)
    for match in _OTHER_SCRIPT_RE.finditer(text):
        run = match.group(0)
        if sum(character.isalpha() for character in run) < 2:
            continue
        if any(start < match.end() and match.start() < end for start, end, _ in taken):
            continue
        pieces.append((match.start(), match.end(), UNSUPPORTED))
    for start, end, language in sorted(pieces):
        spans.append(SpeechSpan(BASE, text[cursor:start]))
        spans.append(SpeechSpan(language, text[start:end]))
        cursor = end
    spans.append(SpeechSpan(BASE, text[cursor:]))
    if base_language == "zh":
        # The Mandarin route reads Chinese and English itself; only a script
        # with no route has to come out.
        return _join([SpeechSpan(BASE, span.text) if span.language == MANDARIN else span for span in spans])
    return spans


def _join(spans: list[SpeechSpan]) -> list[SpeechSpan]:
    joined: list[SpeechSpan] = []
    for span in spans:
        if joined and joined[-1].language == span.language:
            joined[-1] = SpeechSpan(span.language, joined[-1].text + span.text)
        else:
            joined.append(span)
    return joined


def speech_spans(text: str, base: str = "en-us") -> list[SpeechSpan]:
    """Cut ``text`` into spans in speaking order; ``BASE`` is the selected route."""
    if not text or not text.strip():
        return []
    base_language = _base_language(base)
    english = _is_english_base(base)
    if text.isascii() and not english:
        return [SpeechSpan(BASE, text)]
    spans = _script_spans(text, base_language)
    has_chinese = any(span.language == MANDARIN for span in spans)
    result: list[SpeechSpan] = []
    for span in spans:
        if span.language != BASE:
            result.append(span)
            continue
        piece = _drop_pinyin(span.text) if has_chinese else span.text
        if not english or not _speakable(piece):
            result.append(SpeechSpan(BASE, piece))
        elif len(spans) == 1 and _reads_as_spanish(piece):
            result.append(SpeechSpan(SPANISH, piece))
        else:
            cursor = 0
            for start, end in _spanish_ranges(piece):
                result.append(SpeechSpan(BASE, piece[cursor:start]))
                result.append(SpeechSpan(SPANISH, piece[start:end]))
                cursor = end
            result.append(SpeechSpan(BASE, piece[cursor:]))
    # Punctuation left between two spans has nothing to say on its own, and a
    # quote mark cut from its other half has nothing to close.
    kept = _join([span for span in result if _speakable(span.text)])
    edge = " " if len(kept) == 1 else " \"'“”‘’«»"
    return [SpeechSpan(span.language, span.text.strip(edge)) for span in kept]


def spanish_route_text(text: str) -> str:
    """Only what the Spanish route accepts: letters and its own punctuation.

    The route refuses a whole phrase over one quote mark, dash or inverted
    question mark, which read as silence.
    """
    cleaned = re.sub(r"[¿¡\"“”«»‘’`()\[\]{}*_~]", "", text)
    cleaned = re.sub(r"\s*[—–-]\s*|\s*…\s*|'", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def unspoken_notice(base: str) -> str:
    return UNSPOKEN_NOTICE.get(_base_language(base), UNSPOKEN_NOTICE["en"])


__all__ = [
    "BASE",
    "MANDARIN",
    "SPANISH",
    "UNSUPPORTED",
    "SpeechSpan",
    "spanish_route_text",
    "speech_spans",
    "unspoken_notice",
]
