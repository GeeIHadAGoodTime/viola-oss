"""Fresh source/representation controls; no model or third-party Python imports."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import unicodedata

from italian_stress import ItalianExplicitStress, UnsupportedWord


CONFIG = Path(__file__).resolve().parents[1] / "evidence" / "kokoro-config.json"
VOCAB = json.loads(CONFIG.read_text())["vocab"]

# These are bounded rule-stream expectations, not acoustic/native-speaker goldens.
WORDS = {
    "papà": "papˈa", "pàpa": "pˈapa", "àncora": "ˈankora", "ancóra": "ankˈora",
    "caffè": "kafːˈɛ", "perché": "perkˈe", "città": "ʧitːˈa", "virtù": "virtˈu",
    "così": "kosˈi", "però": "perˈɔ", "né": "nˈe", "è": "ˈɛ", "sé": "sˈe",
    "tè": "tˈɛ", "più": "piˈu", "già": "ʤˈa", "laggiù": "laʤːˈu", "ciò": "ʧˈɔ",
    "bambù": "bambˈu", "lunedì": "lunedˈi", "venerdì": "venerdˈi", "martedì": "martedˈi",
    "mercoledì": "merkoledˈi", "giovedì": "ʤovedˈi", "tòrta": "tˈɔrta", "tórta": "tˈorta",
    "pèsca": "pˈɛska", "pésca": "pˈeska", "scàla": "skˈala", "scòpa": "skˈɔpa",
    "scùdo": "skˈudo", "scrìvo": "skrˈivo", "schèma": "skˈɛma", "schìfo": "skˈifo",
    "scèna": "ʃˈɛna", "scì": "ʃˈi", "scìa": "ʃˈia", "scìe": "ʃˈie",
    "ùscita": "ˈuʃita", "màschera": "mˈaskera",
}


class ItalianControls(unittest.TestCase):
    def setUp(self):
        self.engine = ItalianExplicitStress(VOCAB)

    def convert(self, source):
        try:
            return self.engine.phonemize(source)
        except UnsupportedWord as error:
            self.fail(f"Supported source control was rejected: {source!r}: {error}")

    def test_bound_vocabulary(self):
        self.assertEqual(hashlib.sha256(CONFIG.read_bytes()).hexdigest(),
                         "5abb01e2403b072bf03d04fde160443e209d7a0dad49a423be15196b9b43c17f")

    def test_case_and_normalization_keep_original_offsets(self):
        for word, expected in WORDS.items():
            for cased in (word, word.upper(), word.title()):
                for form in ("NFC", "NFD"):
                    source = unicodedata.normalize(form, cased)
                    with self.subTest(source=source, form=form):
                        result = self.convert(source)
                        self.assertEqual(result.source, source)
                        self.assertEqual(result.phonemes, expected)
                        self.assertEqual(result.consumed_source_indices, tuple(range(len(source))))
                        self.assertEqual(len(result.phones), len(expected))
                        self.assertEqual(result.phones[result.stress_phone_index].symbol, "ˈ")
                        self.assertEqual(result.phones[result.stress_phone_index].source_indices,
                                         result.stress_source_indices)
                        self.assertTrue(all(p.source_indices for p in result.phones))
                        self.assertTrue(all(0 <= i < len(source) for p in result.phones for i in p.source_indices))
                        accented = next(i for i, char in enumerate(source)
                                        if any(a in unicodedata.normalize("NFD", char) for a in "\u0300\u0301"))
                        expected_span = (accented - 1, accented) if unicodedata.combining(source[accented]) else (accented,)
                        self.assertEqual(result.stress_source_indices, expected_span)

    def test_exact_digraph_and_silent_source_spans(self):
        result = self.convert("Chè")
        self.assertEqual(result.phonemes, "kˈɛ")
        self.assertEqual(result.phones[0].source_indices, (0, 1))
        result = self.convert("hà")
        self.assertEqual(result.phonemes, "ˈa")
        self.assertEqual(result.spans[0].mapped_phones, "")
        self.assertEqual(result.spans[0].source_indices, (0,))
        self.assertEqual(result.consumed_source_indices, (0, 1))

    def test_sc_context_does_not_eat_stressed_i(self):
        result = self.convert("Sci\u0300a")
        self.assertEqual(result.phonemes, "ʃˈia")
        self.assertEqual(result.phones[0].source_indices, (0, 1))
        self.assertEqual(result.stress_source_indices, (2, 3))
        self.assertEqual(result.phones[-1].source_indices, (4,))
        self.assertEqual(self.convert("pésca").phonemes, "pˈeska")
        self.assertEqual(self.convert("scèna").phonemes, "ʃˈɛna")

    def test_voicing_and_affricate_tokens_preserve_owners(self):
        self.assertEqual(self.convert("smàlto").phonemes, "zmˈalto")
        for word, symbol, owners in (("già", "ʤ", (0, 1)), ("ciò", "ʧ", (0, 1)),
                                      ("pìzza", "ʦ", (2, 3))):
            with self.subTest(word=word):
                result = self.convert(word)
                self.assertEqual(next(p.source_indices for p in result.phones if p.symbol == symbol), owners)

    def test_rejects_unsupported_whole_words(self):
        for source in ("", "casa", "àncòra", "pàPa", "Pa\u0300Pa", "\u0300a", "b\u0300", "à\u0300",
                       "á", "â", "a\u0302", "café!", "caffè ", "€caffè", "$é", "123à", "à٣", "à１",
                       "a-bè", "caffè\n", "caffè\x00", "caffè🙂", "ßà", "東京à", "àw", "àx", "ày",
                       "à" * 129, "scià", "sciò", "sciù", "sciènza", "còscienza", "lìscio"):
            with self.subTest(source=source):
                with self.assertRaises(UnsupportedWord):
                    self.engine.phonemize(source)

    def test_no_vocab_deletion_or_mutable_alias(self):
        vocab = dict(VOCAB)
        engine = ItalianExplicitStress(vocab)
        vocab.clear()
        self.assertEqual(engine.phonemize("papà").phonemes, "papˈa")
        for absent, word in (("ˈ", "papà"), ("ɛ", "caffè"), ("ʃ", "scèna"), ("i", "scìa"), ("ː", "caffè")):
            with self.subTest(absent=absent):
                with self.assertRaises(UnsupportedWord):
                    ItalianExplicitStress(set(VOCAB) - {absent}).phonemize(word)

    def test_modified_rules_fail_closed(self):
        source = Path(__file__).with_name("data")
        for selected in ("map", "pre", "post", "strip"):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as tmp:
                for path in source.iterdir():
                    (Path(tmp) / path.name).write_bytes(path.read_bytes() + (b"\n" if path.name == selected else b""))
                with self.assertRaises(UnsupportedWord):
                    ItalianExplicitStress(VOCAB, Path(tmp))


if __name__ == "__main__":
    unittest.main()
