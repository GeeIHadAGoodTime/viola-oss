"""Fresh pinned-rule representation controls, not pronunciation goldens."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import unicodedata

from hindi_word import HindiRepresentableWord, UnsupportedWord


CONFIG = Path(__file__).resolve().parents[1] / "evidence" / "kokoro-config.json"
VOCAB = json.loads(CONFIG.read_text())["vocab"]
WORDS = {
    "कमल": "kəməl", "किताब": "kitaːb", "कला": "kəlaː", "राम": "raːm", "सीता": "siːtaː",
    "कुमार": "kumaːr", "लड़का": "ləɽkaː", "ज़रा": "zəraː", "क़लम": "qələm", "ख़त": "xət",
    "ग़ज़ल": "ɣəzəl", "माँ": "mãː", "चाय": "ʧaːj", "छाता": "ʧʰaːtaː", "जाम": "ʤaːm",
    "नमक": "nəmək", "का": "kaː", "क्": "k", "शक्ति": "ʃəkti", "आँ": "ãː",
    "टमाटर": "ʈəmaːʈər", "फूल": "pʰuːl", "खाना": "kʰaːnaː", "क़": "qə",
    "पंख": "pəŋkʰə", "पंच": "pəɲʧə", "ठंड": "ʈʰənɖə", "संपर्क": "səmprkə",
}


class HindiControls(unittest.TestCase):
    def setUp(self):
        self.engine = HindiRepresentableWord(VOCAB)

    def convert(self, source):
        try:
            return self.engine.phonemize(source)
        except UnsupportedWord as error:
            self.fail(f"Supported source control was rejected: {source!r}: {error}")

    def test_bound_vocabulary(self):
        self.assertEqual(hashlib.sha256(CONFIG.read_bytes()).hexdigest(),
                         "5abb01e2403b072bf03d04fde160443e209d7a0dad49a423be15196b9b43c17f")

    def test_rule_outputs_complete_source_and_forms(self):
        for word, expected in WORDS.items():
            for form in ("NFC", "NFD"):
                source = unicodedata.normalize(form, word)
                with self.subTest(source=source, form=form):
                    result = self.convert(source)
                    self.assertEqual(result.source, source)
                    self.assertEqual(result.phonemes, expected)
                    self.assertEqual(result.consumed_source_indices, tuple(range(len(source))))
                    self.assertEqual(len(result.phones), len(expected))
                    self.assertTrue(all(p.source_indices for p in result.phones))
                    self.assertTrue(all(0 <= i < len(source) for p in result.phones for i in p.source_indices))
                    self.assertEqual({i for span in result.spans for i in span.source_indices}, set(range(len(source))))

    def test_precomposed_nukta_retains_original_offsets(self):
        for source, expected, owners in (("लड़का", "ləɽkaː", (1,)), ("लड़का", "ləɽkaː", (1, 2)),
                                         ("फ़ा", "faː", (0,)), ("फ़ा", "faː", (0, 1)),
                                         ("ज़ा", "zaː", (0,)), ("ज़ा", "zaː", (0, 1))):
            with self.subTest(source=source):
                result = self.convert(source)
                self.assertEqual(result.phonemes, expected)
                wanted = "ɽ" if source.startswith("ल") else expected[0]
                self.assertEqual(next(p.source_indices for p in result.phones if p.symbol == wanted), owners)

    def test_nasal_metathesis_preserves_separate_owners(self):
        result = self.convert("माँ")
        self.assertEqual(result.phonemes, "mãː")
        self.assertEqual([p.source_indices for p in result.phones], [(0,), (1,), (2,), (1,)])
        self.assertTrue(any(r.removed == "ː̃" and r.inserted == "̃ː" for r in result.rewrites))

    def test_nasal_contexts_and_aspirates_are_represented(self):
        for source, symbol in (("पंप", "m"), ("पंख", "ŋ"), ("पंच", "ɲ"), ("ठंड", "n")):
            with self.subTest(source=source):
                result = self.convert(source)
                self.assertIn(symbol, result.phonemes)
                self.assertEqual(next(p.source_indices for p in result.phones if p.symbol == symbol), (1,))
        result = self.convert("छाता")
        self.assertEqual(result.phonemes[:2], "ʧʰ")
        self.assertEqual([p.source_indices for p in result.phones[:2]], [(0,), (0,)])

    def test_deleted_inherent_vowels_and_virama_remain_auditable(self):
        result = self.convert("क्")
        self.assertEqual(result.phonemes, "k")
        self.assertEqual(result.consumed_source_indices, (0, 1))
        self.assertTrue(any(r.removed == "ə्" and r.inserted == "" and r.source_indices == (0, 1)
                            for r in result.rewrites))
        result = self.convert("राम")
        self.assertEqual(result.phonemes, "raːm")
        self.assertTrue(any(r.removed == "ə" and r.source_indices == (2,) for r in result.rewrites))

    def test_no_lost_or_passthrough_source(self):
        for source in ("", "रामX", "रामA", "राम1", "राम१", "१राम", "राम€", "₹राम", "राम!", "राम ",
                       "राम\n", "राम\x00", "राम🙂", "राम東京", "क\u200d", "क\u200c", "क़़", "काि",
                       "िक", "ँक", "ंक", "्क", "क्ा", "क्अ", "आा", "कँँ", "क््", "क।", "क॥", "आ̃",
                       "अंक", "अंग", "सअंक",
                       "कं", "कां", "सं", "रामं", "क" * 129):
            with self.subTest(source=source):
                with self.assertRaises(UnsupportedWord):
                    self.engine.phonemize(source)

    def test_unsupported_features_cannot_be_enabled_by_extra_vocab(self):
        engine = HindiRepresentableWord(set(VOCAB) | set("ɦ\u0324\u0325\u0329"))
        for source in ("हाथ", "घर", "धन", "भाल", "झाम", "ढाल", "ऋण", "कृ", "ॠ", "ऌ", "ॡ", "ढ़"):
            with self.subTest(source=source):
                with self.assertRaises(UnsupportedWord):
                    engine.phonemize(source)

    def test_selected_vocab_must_preserve_every_feature(self):
        for absent, source in (("ʰ", "छाता"), ("ʧ", "चाय"), ("ʤ", "जाम"), ("̃", "माँ"),
                               ("ː", "कला"), ("ɽ", "लड़का"), ("ŋ", "पंख"), ("ə", "कमल")):
            with self.subTest(absent=absent):
                with self.assertRaises(UnsupportedWord):
                    HindiRepresentableWord(set(VOCAB) - {absent}).phonemize(source)
        mutable = dict(VOCAB)
        engine = HindiRepresentableWord(mutable)
        mutable.clear()
        self.assertEqual(engine.phonemize("राम").phonemes, "raːm")

    def test_modified_rules_fail_closed(self):
        source = Path(__file__).with_name("data")
        for selected in ("map", "post"):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as tmp:
                for path in source.iterdir():
                    (Path(tmp) / path.name).write_bytes(path.read_bytes() + (b"\n" if path.name == selected else b""))
                with self.assertRaises(UnsupportedWord):
                    HindiRepresentableWord(VOCAB, Path(tmp))

    def test_requests_do_not_reuse_source_or_drop_nukta(self):
        first = self.convert("फ़िल्म")
        self.assertEqual(first.phonemes, "filmə")
        self.assertEqual(self.convert("फिल्म").phonemes, "pʰilmə")
        self.assertEqual(self.convert("म्").phonemes, "m")
        self.assertEqual(self.convert("फ़िल्म"), first)
        self.assertEqual(first.source, "फ़िल्म")


if __name__ == "__main__":
    unittest.main()
