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
- Under an English setting, Spanish, French and Brazilian Portuguese are found
  from a sentence's own words and go to their own route and voice. Italian and
  German have no pronunciation route in the customer speech stack, so they are
  reported as ``UNSUPPORTED`` too: an honest line beats English letter rules
  reading "Guten Morgen". An English sentence only gives up words to another
  language on strong evidence: names like "Los Angeles", "Moulin Rouge", "Mamma
  Mia" or "José" stay English.

Pure text work: no model, no settings, no I/O. This decides how a reply is
pronounced, never what happens with a command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BASE = "base"
SPANISH = "es"
FRENCH = "fr"
PORTUGUESE = "pt-br"
ITALIAN = "it"
GERMAN = "de"
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

# Words that show a sentence is English whatever else is in it.
_EN_COMMON = frozenset(
    """
    the is are was were be been of to and in on at for with from by you your i my we our they their it its this that
    these those he she his her have has had do does did will would can could should say says said means mean word
    called how what when where why which who good morning hello hi not but or if as an
    """.split()
)


@dataclass(frozen=True)
class _Language:
    """How to recognise one language from its own words.

    ``strong``: words of the language that are not English words. Short on
    purpose: greetings and everyday words, nothing English uses as a word or a
    name (no "boa", "mia", "die", "bene"). ``neutral``: its function words, which
    are never evidence alone ("La La Land", "Rio de la Plata"). ``alone``: words
    that are a whole sentence by themselves ("Hola!", "Merci."). ``named``: the
    English name of the language: a sentence that says it is talking about the
    language can hand over a single word ("In French, thanks is merci.").
    """

    code: str
    named: str
    strong: frozenset[str]
    neutral: frozenset[str]
    alone: frozenset[str]
    marks: re.Pattern[str]
    # English-looking words that are ordinary here ("Ich bin in Berlin").
    homographs: frozenset[str]
    # Accented words count as evidence only where the route can read them, so
    # a wrong guess is a French reading, never a refusal.
    accents_count: bool


_LANGUAGES = (
    _Language(
        code=SPANISH,
        named="spanish",
        strong=frozenset(
            """
            hola adiós adios gracias buenos buenas días dias noches tardes señor señora señorita perdón perdon disculpa
            cómo qué quién quien cuándo cuando dónde donde cuál cual porqué porque pero también tambien muy está están
            estás estoy estamos eres somos tengo tienes tiene tenemos quiero quieres quiere puedo puedes puede hacer
            hoy ayer mañana ahora aquí aqui allí alli siempre nunca mucho mucha muchos muchas gusto encantado encantada
            hasta luego vemos feliz cumpleaños navidad bienvenido bienvenida siento favor esto esta eso
            bien bueno buena estar nosotros ustedes ellos ellas usted tú él
            """.split()
        ),
        neutral=frozenset(
            "de la el los las un una unos unas y o en es son con para por que del al lo se su sus mi tu te le nos más "
            "sin sobre".split()
        ),
        alone=frozenset("hola gracias adiós adios buenos buenas".split()),
        marks=re.compile(r"[áéíóúüñ]", re.IGNORECASE),
        homographs=frozenset(),
        accents_count=True,
    ),
    _Language(
        code=FRENCH,
        named="french",
        strong=frozenset(
            """
            bonjour bonsoir merci revoir beaucoup allez êtes suis sommes sont avec dans très aujourd'hui demain
            hier maintenant toujours jamais pourquoi quand parce mais aussi vous nous bonne journée matin soir nuit
            plaît plait c'est d'accord j'ai s'il l'heure ça voici est oui
            """.split()
        ),
        neutral=frozenset(
            "le la les un une des du de et en à au aux ce cet cette qui que ne pas sur il elle je tu bon comment".split()
        ),
        alone=frozenset("bonjour bonsoir merci".split()),
        marks=re.compile(r"[àâæçéèêëîïôùûœ]", re.IGNORECASE),
        homographs=frozenset("on as or an".split()),
        accents_count=True,
    ),
    _Language(
        code=PORTUGUESE,
        named="portuguese",
        strong=frozenset(
            """
            olá obrigado obrigada você vocês não muito muita tudo hoje amanhã ontem também então ainda até
            tchau adeus prazer desculpe estou está são bom dia noite tarde boas sempre nunca porque quando onde sei
            """.split()
        ),
        neutral=frozenset("os um uma da dos das em na nas e que para com por ao à boa bem mas eu sim agora".split()),
        alone=frozenset("olá obrigado obrigada tchau".split()),
        marks=re.compile(r"[ãõáàâêéíóôúç]", re.IGNORECASE),
        homographs=frozenset("do as no for".split()),
        accents_count=True,
    ),
    _Language(
        code=ITALIAN,
        named="italian",
        strong=frozenset(
            """
            buongiorno buonasera buonanotte arrivederci grazie prego sono siamo siete molto oggi domani ieri adesso
            perché perche anche ancora questo questa quello quella voglio posso piacere scusa scusi quando sempre
            stai mille chiamo
            """.split()
        ),
        neutral=frozenset("il lo la i gli le un una di del della dei delle e che per con al alla ciao bene".split()),
        alone=frozenset("buongiorno buonasera buonanotte arrivederci grazie".split()),
        marks=re.compile(r"[àèéìòù]", re.IGNORECASE),
        homographs=frozenset("in do".split()),
        accents_count=False,
    ),
    _Language(
        code=GERMAN,
        named="german",
        strong=frozenset(
            """
            guten gute morgen abend nacht danke bitte hallo tschüss auf wiedersehen ich bin nicht und ist sind heute
            gestern sehr schön wir haben habe möchte warum wann weil aber auch geht wie
            """.split()
        ),
        neutral=frozenset("der das den dem ein eine einen zu mit von für tag gut es dir ihnen sie mein".split()),
        alone=frozenset("hallo danke tschüss".split()),
        marks=re.compile(r"[äöüß]", re.IGNORECASE),
        homographs=frozenset("in an so was will".split()),
        accents_count=False,
    ),
)
_ROUTED = frozenset({SPANISH, FRENCH, PORTUGUESE})
# French elision: l'école, d'accord, qu'il. Function words glued to the next word.
_FRENCH_ELISION_RE = re.compile(r"^(?:[cdjlmnst]|qu)['’]", re.IGNORECASE)


@dataclass(frozen=True)
class SpeechSpan:
    """A piece of a sentence and the route that should read it."""

    language: str
    text: str


def _is_english_base(base: str) -> bool:
    return base.lower().replace("_", "-").split("-", 1)[0] == "en"


def _base_language(base: str) -> str:
    return base.lower().replace("_", "-").split("-", 1)[0]


def _classify(word: str, language: _Language) -> str:
    lowered = word.lower().replace("’", "'")
    if lowered in language.strong:
        return "strong"
    if lowered in language.neutral or lowered in language.homographs:
        return "neutral"
    if language.code == FRENCH and _FRENCH_ELISION_RE.match(lowered):
        return "neutral"
    if lowered in _EN_COMMON:
        return "english"
    return "accented" if language.accents_count and language.marks.search(lowered) else "other"


def _score(text: str, language: _Language) -> int | None:
    """How strongly a whole sentence (or quoted phrase) reads as ``language``, or None."""
    words = [match.group(0) for match in _WORD_RE.finditer(text)]
    if not words or language.named in text.lower():
        return None
    kinds = [_classify(word, language) for word in words]
    strong = kinds.count("strong")
    neutral = kinds.count("neutral")
    accented = kinds.count("accented")
    english = kinds.count("english")
    points = strong * 2 + neutral + accented
    # A foreign sentence can hold a word English also has ("Ich bin in Berlin"),
    # an English sentence about a foreign phrase cannot pass this.
    if english and (strong < 2 or strong + neutral <= english * 2):
        return None
    if len(words) == 1:
        return points if words[0].lower() in language.alone else None
    if strong and strong + neutral >= 2:
        return points if (strong + neutral + accented) * 3 >= len(words) else None
    if accented and neutral and len(words) >= 3:
        return points if (accented + neutral) * 2 >= len(words) else None
    # Function words alone need a real sentence: "Son las tres y media", never
    # a place name like "Rio de la Plata".
    return points if len(words) >= 5 and neutral * 2 > len(words) else None


def _sentence_language(text: str) -> str | None:
    """The language a whole sentence is in, when it is not English."""
    if "¿" in text or "¡" in text:
        return SPANISH
    best: tuple[int, str] | None = None
    for language in _LANGUAGES:
        points = _score(text, language)
        if points is not None and (best is None or points > best[0]):
            best = (points, language.code)
    return best[1] if best else None


def _phrase_ranges(text: str) -> list[tuple[int, int, str]]:
    """Foreign phrases inside an English sentence, as (start, end, language)."""
    found: list[tuple[int, int, str]] = []
    for match in _QUOTED_RE.finditer(text):
        group = next(index for index in range(1, 5) if match.group(index) is not None)
        language = _sentence_language(match.group(group))
        if language is not None:
            found.append((match.start(group), match.end(group), language))
    words = list(_WORD_RE.finditer(text))
    lowered = text.lower()
    for language in _LANGUAGES:
        # "In French, thanks is merci": the sentence says what the word is.
        needed = 1 if language.named in lowered else 2
        index = 0
        while index < len(words):
            if _classify(words[index].group(0), language) not in {"strong", "neutral"}:
                index += 1
                continue
            end = index
            strong: list[int] = []
            while end < len(words):
                kind = _classify(words[end].group(0), language)
                if kind not in {"strong", "neutral"}:
                    break
                if end > index and text[words[end - 1].end() : words[end].start()].strip(" ,") != "":
                    break
                if kind == "strong":
                    strong.append(end)
                end += 1
            # "por favor": "favor" is the only Spanish-only word, "por" makes the pair.
            pair = len(strong) == 1 and strong[0] > index and words[index].group(0).lower() == "por"
            if len(strong) >= needed or pair:
                first = index if pair else strong[0]
                found.append((words[first].start(), words[strong[-1]].end(), language.code))
            index = max(end, index + 1)
    return _merged(found)


def _merged(ranges: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Overlapping finds keep the longest one: "buenos días", not its "días" alone."""
    kept: list[tuple[int, int, str]] = []
    for found in sorted(ranges, key=lambda item: (item[0], item[0] - item[1])):
        if kept and found[0] < kept[-1][1]:
            continue
        kept.append(found)
    return kept


def _spoken_as(language: str) -> str:
    return language if language in _ROUTED else UNSUPPORTED


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
        elif len(spans) == 1 and _sentence_language(piece) is not None:
            result.append(SpeechSpan(_spoken_as(_sentence_language(piece) or ""), piece))
        else:
            cursor = 0
            for start, end, language in _phrase_ranges(piece):
                result.append(SpeechSpan(BASE, piece[cursor:start]))
                result.append(SpeechSpan(_spoken_as(language), piece[start:end]))
                cursor = end
            result.append(SpeechSpan(BASE, piece[cursor:]))
    # Punctuation left between two spans has nothing to say on its own, and a
    # quote mark cut from its other half has nothing to close.
    kept = _join([span for span in result if _speakable(span.text)])
    edge = " " if len(kept) == 1 else " \"'“”‘’«»"
    return [SpeechSpan(span.language, span.text.strip(edge)) for span in kept]


def route_text(language: str, text: str) -> str:
    """Only what a Romance route accepts: letters and its own punctuation.

    A route refuses a whole phrase over one quote mark, dash or inverted
    question mark, which read as silence. French keeps its apostrophes, which
    are how it joins words.
    """
    cleaned = re.sub(r"[¿¡\"“”«»‘`()\[\]{}*_~]", "", text)
    cleaned = re.sub(r"\s*[—–-]\s*|\s*…\s*", " ", cleaned)
    if language == FRENCH:
        cleaned = cleaned.replace("’", "'")
    else:
        cleaned = re.sub(r"['’]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def spanish_route_text(text: str) -> str:
    return route_text(SPANISH, text)


def unspoken_notice(base: str) -> str:
    return UNSPOKEN_NOTICE.get(_base_language(base), UNSPOKEN_NOTICE["en"])


__all__ = [
    "BASE",
    "FRENCH",
    "GERMAN",
    "ITALIAN",
    "MANDARIN",
    "PORTUGUESE",
    "SPANISH",
    "UNSUPPORTED",
    "SpeechSpan",
    "route_text",
    "spanish_route_text",
    "speech_spans",
    "unspoken_notice",
]
