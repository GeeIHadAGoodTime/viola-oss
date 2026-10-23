"""Offline English Misaki adapter for the explicit customer Kokoro profile.

Never guesses an unsupported locale, drops unknown words/phonemes, invokes
an eSpeak fallback or downloads assets during speech. An out-of-lexicon Latin
word is read from Misaki's own lexicon (case variants, then a two-word
compound), then by small offline English letter-to-sound rules, and only
acronym-like tokens letter by letter, so a reply is still spoken; anything else
still fails closed and names the offending tokens on the error (never in its
message) so the reply layer can retry without them. Packaging qualification
must separately verify the installed graph, bundled files and actual audio.
"""

from __future__ import annotations

import importlib.metadata
import re
import threading
import unicodedata
from pathlib import Path

from voice.english_numbers import num2words
from voice.pronunciation_tables import _ACRONYM_INITIALISMS, _ACRONYM_WORDS, _BRAND_PRONUNCIATIONS

_FORK_VERSION = "0.9.4+viola.1"
_MODEL_VERSION = "3.8.0"
_MAX_PHONEMES = 510
_MAX_TEXT = 5000
# Misaki 0.9.4 spoken phones, including special-case a/er and the v1 flap.
# Stress/tone/punctuation symbols alone cannot constitute a spoken word.
_SPOKEN_PHONEMES = frozenset("AIOQWYaiuæɑɐɒɔəɚɛɜɪʊʌᵻᵊʔbdfhjklmnpstvwzðŋɡɹɾʃʒʤʧθT")
_EXPLICIT_OVERRIDE = re.compile(r"\[([^\]]+)\]\(([^)]*)\)")
# Exact respelling fragments already emitted by Viola's retained brand/acronym
# and default-name tables. These small additions are not a general OOV fallback.
_RESPELLINGS = {
    "luh": "lə",
    "Ann": "ˈæn",
    "stuh": "stə",
    "roh": "ɹˈO",
    "koo": "kˈu",
    "bohz": "bˈOz",
    "muh": "mə",
    "ih": "ɪ",
    "nuh": "nə",
    "trell": "tɹˈɛl",
    "giff": "ɡˈɪf",
    "nassa": "nˈæsə",
    "tock": "tˈɑk",
    "vid": "vˈɪd",
    "ee": "i",
    "ven": "vˈɛn",
    "moe": "mˈO",
    "oo": "ˈu",
    "ber": "bɚ",
}
# Music/app words likely in replies that the reviewed lexicon lacks. Each was
# checked against viola-misaki-en 0.9.4+viola.1; words it already reads well
# (EDM, R&B, K-pop, lo-fi, YouTube, Viola, playlist) are deliberately absent.
_REPLY_WORDS = {
    "lofi": "lˈOfˌI",
    "LoFi": "lˈOfˌI",
    "Spotify": "spˈɑTəfˌI",
    "Deezer": "dˈizəɹ",
    "chillhop": "ʧˈɪlhˌɑp",
    "synthwave": "sˈɪnθwˌAv",
}
_SPELLABLE_WORD = re.compile(r"[A-Za-zÀ-ÖØ-öø-ɏ]+(?:['’][A-Za-zÀ-ÖØ-öø-ɏ]+)*")
_MIN_COMPOUND_PART = 2
# Misaki 0.9.4 phoneme inventories; a rule reading outside them is discarded.
_US_PHONES = frozenset("AIOWYbdfhijklmnpstuvwzæðŋɑɔəɛɜɡɪɹɾʃʊʌʒʤʧˈˌθᵊᵻʔ")
_GB_PHONES = frozenset("AIQWYabdfhijklmnpstuvwzðŋɑɒɔəɛɜɡɪɹʃʊʌʒʤʧˈˌːθᵊ")

_VOWEL_LETTERS = "aeiouy"
# Ordered English spelling rules for words the lexicon lacks: (pattern, kind,
# payload). "V" is a fixed nucleus (stressed, unstressed), "v" a single vowel
# letter whose quality depends on syllable shape, "C" consonant phones (None:
# the letter itself), "" silent. The first rule matching at the cursor wins.
_LETTER_RULES = tuple(
    (re.compile(pattern), kind, payload)
    for pattern, kind, payload in (
        (r"eau", "V", ("O", "O")),
        (r"aa", "V", ("ɑ", "ə")),
        (r"a[iy]$", "V", ("I", "I")),
        (r"ai|ay|ae", "V", ("A", "A")),
        (r"au|aw", "V", ("ɔ", "ɔ")),
        (r"eer|ear(?![aeiouy])", "V", ("ɪɹ", "ɪɹ")),
        (r"ee|ea", "V", ("i", "i")),
        (r"^ei", "V", ("I", "I")),
        (r"ey$", "V", ("i", "i")),
        (r"e(?=y[aeiou])", "v", "e"),
        (r"ei|ey", "V", ("A", "A")),
        (r"eu|ew", "V", ("ju", "ju")),
        (r"ie(?!r)", "V", ("i", "i")),
        (r"oa|oe", "V", ("O", "O")),
        (r"oor|oar", "V", ("ɔɹ", "ɔɹ")),
        (r"oo", "V", ("u", "u")),
        (r"ou$", "V", ("u", "u")),
        (r"ou", "V", ("W", "W")),
        (r"o(?=w[aeiouy])", "v", "o"),
        (r"ow$", "V", ("O", "O")),
        (r"ow", "V", ("W", "W")),
        (r"oi|oy", "V", ("Y", "Y")),
        (r"ue$|ui", "V", ("u", "u")),
        (r"uy", "V", ("I", "I")),
        (r"o(?=l(?:[^aeiouyl]|$))", "V", ("O", "O")),
        (r"air|are$", "V", ("ɛɹ", "ɛɹ")),
        (r"arr(?=[aeiouy])", "V", ("æɹ", "əɹ")),
        (r"ar", "V", ("ɑɹ", "əɹ")),
        (r"err|er(?=[aeiouy])", "V", ("ɛɹ", "əɹ")),
        (r"er", "V", ("ɜɹ", "əɹ")),
        (r"irr|ir(?=[aeiouy])", "V", ("ɪɹ", "əɹ")),
        (r"ir|ur|yr", "V", ("ɜɹ", "əɹ")),
        (r"orr|or", "V", ("ɔɹ", "əɹ")),
        (r"(?<=[^aeiouy])le$", "V", ("əl", "əl")),
        (r"ii$", "V", ("i", "i")),
        (r"(?<=if)y$", "V", ("I", "I")),
        (r"y(?=[aeiou])", "C", "j"),
        (r"[aeiouy]", "v", None),
        (r"^mc", "C", "mək"),
        (r"tch", "C", "ʧ"),
        (r"sch", "C", "ʃ"),
        (r"ch(?=[lr])|(?<=y)ch|kh", "C", "k"),
        (r"ch", "C", "ʧ"),
        (r"ck", "CC", "k"),
        (r"cc(?=[eiy])", "CC", "ks"),
        (r"c(?=ii$)", "C", "ʧ"),
        (r"c(?=[eiy])", "C", "s"),
        (r"dg", "CC", "ʤ"),
        (r"gh(?=t)|(?<!^)gh", "", ""),
        (r"gh", "C", "ɡ"),
        (r"^gn|gn$", "C", "n"),
        (r"g(?=[eiy])", "C", "ʤ"),
        (r"ng(?=[aeiouy])", "CC", "ŋɡ"),
        (r"ng", "C", "ŋ"),
        (r"n(?=k)", "C", "ŋ"),
        (r"ph", "C", "f"),
        (r"qu", "C", "kw"),
        (r"sh", "C", "ʃ"),
        (r"sz", "C", "s"),
        (r"(?<=[aeiouy])s(?=[aeiouy])", "C", "z"),
        (r"th", "C", "θ"),
        (r"wh|^wr", "C", "w"),
        (r"^kn", "C", "n"),
        (r"mb$", "C", "m"),
        (r"rh", "C", "ɹ"),
        (r"(?<=[^aeiouy])h|h(?![aeiouy])", "", ""),
        (r"^x", "C", "z"),
        (r"x", "CC", "ks"),
        (r"([bcdfgjklmnpqrstvwz])\1", "CC", None),
        (r"[bcdfghjklmnpqrstvwz]", "C", None),
    )
)
_LETTER_PHONES = {"c": "k", "j": "ʤ", "q": "k", "r": "ɹ", "g": "ɡ"}
# Single vowel letter: closed stressed, open stressed, word-final, magic-e, reduced.
_SINGLE_VOWELS = {
    "a": ("æ", "ɑ", "ə", "A", "ə"),
    "e": ("ɛ", "ɛ", "i", "i", "ə"),
    "i": ("ɪ", "i", "i", "I", "ɪ"),
    "o": ("ɒ", "O", "O", "O", "ə"),
    "u": ("ʌ", "u", "u", "u", "ə"),
    "y": ("ɪ", "I", "i", "I", "ɪ"),
}
_NOT_RHOTIC = "(?![ˈˌ]?[AIOQWYaiuæɑɒɔəɛɜɪʊʌ])"
_BRITISH = (
    ("ɑɹ" + _NOT_RHOTIC, "ɑː"),
    ("ɔɹ" + _NOT_RHOTIC, "ɔː"),
    ("ɜɹ" + _NOT_RHOTIC, "ɜː"),
    ("ɪɹ" + _NOT_RHOTIC, "ɪə"),
    ("ɛɹ" + _NOT_RHOTIC, "ɛː"),
    ("əɹ" + _NOT_RHOTIC, "ə"),
    ("ɑ(?!ː)", "ɑː"),
    ("ɔ(?!ː)", "ɔː"),
    ("æ", "a"),
    ("O", "Q"),
    ("u", "uː"),
    ("(?<=[ˈˌ])i", "iː"),
    ("i(?=[ˈˌ]?[AIQWYaiuæɑɒɔəɛɜɪʊʌ])", "ɪ"),
)
# French-style endings take the stress on their own short e (Adele, Annette).
_STRESSED_ENDINGS = ("ele", "elle", "ette", "esse")


def _fold_latin(word):
    """Lowercase ASCII letters of a Latin word plus the acute-accented index."""
    letters, accent = [], None
    for character in word.lower():
        if character in "'’":
            continue
        decomposed = unicodedata.normalize("NFKD", character)
        plain = {"æ": "ae", "ç": "s", "ø": "o", "ß": "ss", "œ": "oe"}.get(
            character, "".join(c for c in decomposed if not unicodedata.combining(c))
        )
        if not plain.isascii() or not plain.isalpha():
            return None, None
        if "́" in decomposed and plain in _VOWEL_LETTERS:
            accent = len(letters)
        letters.append(plain)
    return "".join(letters), accent


def _parse(letters, accent):
    units, i = [], 0
    while i < len(letters):
        if (
            i == len(letters) - 1
            and letters[i] == "e"
            and accent != i
            and any(c in _VOWEL_LETTERS for c in letters[:-1])
        ):
            break  # silent final e
        for pattern, kind, payload in _LETTER_RULES:
            match = pattern.match(letters, i)
            if match and match.end() > i:
                break
        else:
            return None
        text = match.group(0)
        if kind == "v":
            units.append(["v", payload or text, i, False])
        elif kind == "V":
            units.append(["V", payload, i, False])
        elif kind:
            units.append(["C", payload or _LETTER_PHONES.get(text[0], text[0]), i, kind == "CC"])
        i = match.end()
    return units


def _shape(units, k):
    """Syllable shape after nucleus k: final, open or closed."""
    consonants = []
    for unit in units[k + 1 :]:
        if unit[0] != "C":
            break
        consonants.append(unit)
    if k + 1 == len(units):
        return "final"
    if not consonants:
        return "open"
    if k + 1 + len(consonants) == len(units) or consonants[0][3]:
        return "closed"
    if len(consonants) == 1:
        return "open"
    if len(consonants) == 2 and consonants[1][1] in ("l", "ɹ", "w") and consonants[0][1] not in "lɹnmŋ":
        return "open"
    return "closed"


def _stress_index(units, nuclei, letters):
    count = len(nuclei)
    if count >= 2 and letters.endswith(_STRESSED_ENDINGS):
        return count - 1
    if count == 2 and units[nuclei[0]][1] == "e" and units[nuclei[0] + 1][1] == "j":
        return 1  # Be-yon(ce)
    if count <= 2:
        return 0
    pen = nuclei[-2]
    if letters.rstrip("h")[-1] in "aiou":
        if units[pen][0] == "v" and units[pen][1] in "ie" and units[pen + 1][0] != "C":
            return count - 3  # hiatus: O-li-vi-a
        return count - 2
    heavy = units[pen][0] == "V" or _shape(units, pen) == "closed"
    return count - 2 if heavy else count - 3


def _single_vowel(units, k, stressed, letters, accent):
    letter = units[k][1]
    closed, open_, final, magic, reduced = _SINGLE_VOWELS[letter]
    shape = _shape(units, k)
    after = units[k + 1 :]
    if accent == units[k][2] and letter == "e" and shape == "final":
        return "A"
    if shape == "closed" and len(after) == 1 and not after[0][3] and letters.endswith("e"):
        return "ɛ" if letter == "e" and letters.endswith(_STRESSED_ENDINGS) else magic
    if shape == "final":
        return final
    if stressed:
        if shape == "closed":
            return closed
        if letter == "a" and letters.rstrip("h")[-1] not in "aiou":
            return "A"
        if letter == "i" and any(u[0] == "v" and u[1] == "i" for u in after[1:2]):
            return "ɪ"  # O-li-vi-a, Vi-vi-an
        return open_
    if shape == "open" and (after[0][0] != "C" or after[0][1] == "j"):
        return {"e": "i", "i": "i", "y": "i", "o": "O"}.get(letter, reduced)
    if letter == "o" and k == 0 and shape == "open":
        return "O"
    if shape == "closed" and after[0][3] and all(u[0] == "C" for u in after) and letter != "o":
        return closed  # an unstressed last syllable closed by a cluster keeps its vowel
    return reduced


def _letter_sounds(word, british=False):
    """Rule-based English reading of a Latin word, or None (no vowel/non-Latin)."""
    possessive = re.fullmatch(r"(.+)['’]s", word)
    if possessive:
        stem = _letter_sounds(possessive.group(1), british)
        if not stem:
            return None
        last = stem.rstrip("ˈˌ")[-1]
        return stem + ("ɪz" if last in "szʃʒʧʤ" else "s" if last in "ptkfθ" else "z")
    if re.match(r"[Oo]['’]\w", word):
        rest = _letter_sounds(word[2:], british)
        return rest and ("ˌQ" if british else "ˌO") + rest
    letters, accent = _fold_latin(word)
    if not letters or not any(c in _VOWEL_LETTERS for c in letters):
        return None
    units = _parse(letters, accent)
    nuclei = [k for k, unit in enumerate(units or ()) if unit[0] in "vV"]
    if not nuclei:
        return None
    # An acute accent marks the stress (Rosalía), except a final é in a longer
    # name, which only sets the vowel (Beyoncé).
    primary = next((n for n, k in enumerate(nuclei) if units[k][2] == accent), None)
    if primary == len(nuclei) - 1 and len(nuclei) > 2:
        primary = None
    if primary is None:
        primary = _stress_index(units, nuclei, letters)
    secondary = set()
    first, last = nuclei[0], nuclei[-1]
    if primary >= 2 or (primary == 1 and (units[first][0] == "V" or _shape(units, first) == "closed")):
        secondary.add(0)
    if primary < len(nuclei) - 1 and (
        (
            units[last][0] == "V"
            and (last + 1 < len(units) or (primary < len(nuclei) - 2 and units[last][1][0] in "AIOWY"))
        )
        or (
            units[last][0] == "v"
            and letters.endswith("e")
            and _shape(units, last) == "closed"
            and len(units) - last == 2
            and not units[-1][3]
        )
    ):
        secondary.add(len(nuclei) - 1)
    out = []
    for k, unit in enumerate(units):
        if unit[0] == "C":
            out.append(unit[1])
            continue
        n = nuclei.index(k)
        mark = "ˈ" if n == primary else ("ˌ" if n in secondary else "")
        if unit[0] == "V":
            phones = unit[1][0] if mark else unit[1][1]
        else:
            phones = _single_vowel(units, k, bool(mark), letters, accent)
        if mark:
            at = next((p for p, c in enumerate(phones) if c not in "jw"), 0)
            phones = phones[:at] + mark + phones[at:]
        out.append(phones)
    phonemes = "".join(out)
    if not british:
        return phonemes.replace("ɒ", "ɑ")
    for pattern, replacement in _BRITISH:
        phonemes = re.sub(pattern, replacement, phonemes)
    return phonemes


def _reads_as_letters(word):
    """Acronym-like tokens (no vowel, or short all-caps) stay letter by letter."""
    letters, _ = _fold_latin(word)
    return not letters or not any(c in _VOWEL_LETTERS for c in letters) or (word.isupper() and len(letters) <= 4)


def _spell_unknown_word(lexicon):
    """Misaki fallback hook for an unknown Latin word; never eSpeak or a model.

    Tries case variants, then the most balanced split into two gold lexicon
    words, then the English letter-to-sound rules (kept only inside Misaki's
    phoneme inventory), then letter-by-letter golds (Misaki's NNP path) for
    acronym-like tokens. Mixed tokens (digits, symbols, other scripts) return
    no phonemes and keep failing closed.
    """

    def known(word):
        ps, rating = lexicon.get_word(word, None, None, None)
        return (ps, rating) if ps and any(c in _SPOKEN_PHONEMES for c in ps) else (None, None)

    def fallback(token):
        word = token.text if token._.alias is None else token._.alias
        if not _SPELLABLE_WORD.fullmatch(word):
            return None, None
        for variant in dict.fromkeys((word.lower(), word.capitalize())):
            if variant != word:
                ps, rating = known(variant)
                if ps:
                    return ps, rating
        plain = word.lower().replace("'", "").replace("’", "")
        if not plain.isascii():
            plain = ""
        splits = sorted(
            range(_MIN_COMPOUND_PART, len(plain) - _MIN_COMPOUND_PART + 1),
            key=lambda cut: (-min(cut, len(plain) - cut), cut),
        )
        for cut in splits:
            left, right = plain[:cut], plain[cut:]
            if left[-1] in _VOWEL_LETTERS and right[0] in _VOWEL_LETTERS:
                continue  # a vowel-vowel seam is one name (bey|once), not a compound
            if left in lexicon.golds and right in lexicon.golds:
                left_ps, _ = known(left)
                right_ps, _ = known(right)
                if left_ps and right_ps:
                    return left_ps + right_ps.replace("ˈ", "ˌ"), 2
        if not _reads_as_letters(word):
            british = bool(getattr(lexicon, "british", False))
            ps = _letter_sounds(word, british)
            if ps and set(ps) <= (_GB_PHONES if british else _US_PHONES):
                return ps, 1
        if not word.isascii():
            return None, None
        return lexicon.get_NNP(word)

    return fallback


def drop_unspeakable(text: str, tokens=(), *, latin_script: bool = True) -> str:
    """Remove only the named unspeakable tokens (and, for Latin-script locales,
    runs of other scripts) so the rest of a reply can still be spoken."""
    result = text
    for token in sorted({t for t in tokens if isinstance(t, str) and t.strip()}, key=len, reverse=True):
        bounded = re.sub(r"(?<!\w)" + re.escape(token) + r"(?!\w)", " ", result)
        result = bounded if bounded != result else result.replace(token, " ")
    if latin_script:
        result = _NON_LATIN_RUN.sub(" ", result)
    return re.sub(r"\s+", " ", result).strip()


# Anything outside ASCII, Latin-1/Extended-A/B, general punctuation and currency.
_NON_LATIN_RUN = re.compile(r"[^\s\x00-\x7f -ɏ -⁯₠-₿]+")


# Derive only complete spaced-letter phrases from the same retained tables
# used by the formatter. Open A I contributes A I; ordinary article contexts
# are not guessed. Source changes to a table are covered by complete-table tests.
_A_INITIALISM_PHRASES = frozenset(
    match.group(0)
    for table in (_ACRONYM_INITIALISMS, _ACRONYM_WORDS, _BRAND_PRONUNCIATIONS)
    for expansion in table.values()
    for match in re.finditer(r"(?<!\S)(?:[A-Z] +)+[A-Z](?=\s|$)", expansion)
    if "A" in match.group(0).split()
)
_A_INITIALISM_PATTERN = "|".join(
    re.escape(phrase).replace(r"\ ", " +")
    for phrase in sorted(_A_INITIALISM_PHRASES, key=lambda phrase: (-len(phrase), phrase))
)
_INITIALISM_RUN = re.compile(r'(?<![^\s(\["“‘])(?:' + _A_INITIALISM_PATTERN + r')(?=[.!?,;:)\]"”’]*(?:\s|$))')
_SINGLE_QUOTED_INITIALISM = re.compile(
    r"(?<![^\s(\[\"“‘])'(?P<letters>" + _A_INITIALISM_PATTERN + r")(?P<punct>[.!?,;:]*)'(?=[.!?,;:)\]\"”’]*(?:\s|$))"
)


def _protect_initialism_a(text: str) -> str:
    """Disambiguate A only inside the retained A-bearing initialism phrases.

    The recognized phrases are derived from the retained formatter tables.
    An article before another initialism (A U S citizen) must stay an article.
    Misaki otherwise reads their A as an article. Ordinary articles and caller-supplied
    pronunciation markup remain byte-identical; other pronunciation backends
    never receive this customer-only syntax.
    """

    def replace_run(match: re.Match) -> str:
        return re.sub(r"\bA\b", "[A](/ˈA/)", match.group(0))

    def protect_segment(segment: str) -> str:
        # Paired ASCII quotes are explicit delimiters, not an apostrophe inside
        # a name/word. Misaki already handles quoted AI, but not quoted API.
        segment = _SINGLE_QUOTED_INITIALISM.sub(
            lambda match: "'" + re.sub(r"\bA\b", "[A](/ˈA/)", match.group("letters")) + match.group("punct") + "'",
            segment,
        )
        return _INITIALISM_RUN.sub(replace_run, segment)

    parts, cursor = [], 0
    for override in _EXPLICIT_OVERRIDE.finditer(text):
        parts.append(protect_segment(text[cursor : override.start()]))
        parts.append(override.group(0))
        cursor = override.end()
    parts.append(protect_segment(text[cursor:]))
    return "".join(parts)


class PronunciationError(ValueError):
    """Input cannot be spoken completely by the qualified English route.

    ``tokens`` names the offending tokens for a reply-layer retry; they are
    never part of the message, which stays free of user text.
    """

    def __init__(self, *args: object, tokens=()) -> None:
        super().__init__(*args)
        self.tokens = tuple(tokens)


class CustomerTokenizer:
    @staticmethod
    def supports_locale(lang: object) -> bool:
        """Declare locale capability without loading NLP, assets or a runtime.

        This is not asset readiness, voice/accent suitability or permission to
        activate the customer profile. Keep normalization aligned with phonemize.
        """
        return isinstance(lang, str) and lang.lower().replace("_", "-") in {"en-us", "en-gb"}

    def __init__(self, vocab: dict) -> None:
        # Metadata checks happen before importing any NLP or native runtime.
        if importlib.metadata.version("viola-misaki-en") != _FORK_VERSION:
            raise RuntimeError("The reviewed Viola Misaki English fork is required")
        try:
            importlib.metadata.version("misaki")
        except importlib.metadata.PackageNotFoundError:
            pass
        else:
            raise RuntimeError("Upstream misaki conflicts with the customer English fork")
        if importlib.metadata.version("en-core-web-sm") != _MODEL_VERSION:
            raise RuntimeError("The pinned offline English pronunciation model is required")
        import en_core_web_sm
        import spacy
        from misaki import en

        # Importing the known model package gives an explicit local resource;
        # never call spaCy's download CLI or allow a network model identifier.
        model = Path(en_core_web_sm.__file__).resolve().parent / f"en_core_web_sm-{_MODEL_VERSION}"
        if not (model / "config.cfg").is_file():
            raise RuntimeError("The offline English pronunciation model is incomplete")
        nlp = spacy.load(model, enable=["tok2vec", "tagger"])
        self._g2p = {
            "en-us": en.G2P(nlp=nlp, number_to_words=num2words, british=False),
            "en-gb": en.G2P(nlp=nlp, number_to_words=num2words, british=True),
        }
        for g2p in self._g2p.values():
            g2p.lexicon.golds.update(en.Lexicon.grow_dictionary({**_RESPELLINGS, **_REPLY_WORDS}))
            g2p.fallback = _spell_unknown_word(g2p.lexicon)
        self.vocab = vocab
        self._lock = threading.Lock()

    def tokenize(self, phonemes: str) -> list[int]:
        if len(phonemes) > _MAX_PHONEMES:
            raise PronunciationError("Customer phoneme sequence exceeds the model limit")
        if not phonemes or any(character not in self.vocab for character in phonemes):
            raise PronunciationError("Customer phoneme sequence is empty or contains unsupported symbols")
        return [self.vocab[character] for character in phonemes]

    def phonemize(self, text: str, lang: str = "en-us", norm: bool = True) -> str:
        locale = lang.lower().replace("_", "-")
        if locale not in self._g2p:
            raise PronunciationError("Customer Misaki speech supports only en-us and en-gb")
        if not isinstance(text, str) or not text.strip() or len(text) > _MAX_TEXT:
            raise PronunciationError("Customer speech text must contain 1 to 5000 characters")
        # Misaki's explicit phoneme syntax otherwise accepts / / or // and
        # silently suppresses the labelled word. Validate before preprocessing.
        for match in _EXPLICIT_OVERRIDE.finditer(text):
            target = match.group(2)
            if len(target) > 1 and target.startswith("/") and target.endswith("/"):
                override = target.strip("/")
                if not any(character in _SPOKEN_PHONEMES for character in override):
                    raise PronunciationError("Explicit pronunciation overrides must contain spoken phonemes")
        text = _protect_initialism_a(text)
        with self._lock:
            phonemes, tokens = self._g2p[locale](text.strip() if norm else text)
        # Unknown words must not disappear when the neural wrapper filters its
        # vocabulary. Do not include potentially private utterances in errors.
        unknown = [
            token.text
            for token in tokens or ()
            if token.phonemes is None
            or (
                any(character.isalnum() for character in token.text)
                and not any(character in _SPOKEN_PHONEMES for character in token.phonemes)
            )
        ]
        if not tokens or unknown:
            raise PronunciationError(
                "Customer speech contains an unknown word; supply a pronunciation override", tokens=unknown
            )
        phonemes = phonemes.strip()
        if not phonemes or any(character not in self.vocab for character in phonemes):
            unsupported = [
                token.text for token in tokens if any(character not in self.vocab for character in token.phonemes)
            ]
            raise PronunciationError("Customer speech contains unsupported phonemes", tokens=unsupported)
        return phonemes
