"""Run in the explicit customer dependency profile; never imports ONNX Runtime."""

from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class RealCustomerPronunciationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.modules.get("onnxruntime") is not None:
            raise RuntimeError("Pronunciation-only qualification must start without ONNX Runtime")
        cls._had_onnx_entry = "onnxruntime" in sys.modules
        sys.modules["onnxruntime"] = None
        from voice.customer_pronunciation import CustomerTokenizer, PronunciationError

        cls.error = PronunciationError
        vocab = json.loads((ROOT / "third_party/kokoro_onnx/src/kokoro_onnx/config.json").read_text())["vocab"]
        cls.g2p = CustomerTokenizer(vocab)

    @classmethod
    def tearDownClass(cls):
        if not cls._had_onnx_entry:
            sys.modules.pop("onnxruntime", None)

    def test_initialism_a_matches_explicit_letter_not_ordinary_article(self):
        for locale in ("en-us", "en-gb"):
            for raw, explicit in [
                ("A I", "[A](/ˈA/) I"),
                ("A P I", "[A](/ˈA/) P I"),
                ("Open A I", "Open [A](/ˈA/) I"),
                ("'A I'", "'[A](/ˈA/) I'"),
                ("'A P I'", "'[A](/ˈA/) P I'"),
                ("'Open A I'", "'Open [A](/ˈA/) I'"),
            ]:
                with self.subTest(locale=locale, raw=raw):
                    self.assertEqual(self.g2p.phonemize(raw, locale), self.g2p.phonemize(explicit, locale))
                    self.assertNotIn("ɐ", self.g2p.phonemize(raw, locale))
            for article in (
                "A book",
                "A U.S. citizen",
                "A U S citizen",
                "A C P U",
                "A C I A agent",
                "A A P I response",
                "A C++ developer",
                "A I/O error",
            ):
                with self.subTest(locale=locale, article=article):
                    self.assertTrue(self.g2p.phonemize(article, locale).startswith("ɐ"))
            self.assertEqual(self.g2p.phonemize("[A I](/həlˈO/)", locale), "həlˈO")

    def test_every_retained_a_bearing_expansion_matches_explicit_letter_reading(self):
        import re
        from voice.pronunciation_tables import _ACRONYM_INITIALISMS, _ACRONYM_WORDS, _BRAND_PRONUNCIATIONS

        expansions = {
            key: value
            for table in (_ACRONYM_INITIALISMS, _ACRONYM_WORDS, _BRAND_PRONUNCIATIONS)
            for key, value in table.items()
            if re.search(r"\bA\b", value)
        }
        self.assertIn("AMD", expansions)
        self.assertEqual(len(expansions), 7)
        for locale in ("en-us", "en-gb"):
            for key, value in expansions.items():
                explicit = re.sub(r"\bA\b", "[A](/ˈA/)", value)
                for template in ("{}", "Read {} please.", "'{}'", '"{}"', "A {} example."):
                    with self.subTest(locale=locale, key=key, template=template):
                        self.assertEqual(
                            self.g2p.phonemize(template.format(value), locale),
                            self.g2p.phonemize(template.format(explicit), locale),
                        )

    def test_exact_decimal_values_are_not_rounded_to_neighbours(self):
        for left, right in [
            ("Value 12345678901234567.25.", "Value 12345678901234568."),
            ("Value 99999999999999999.99.", "Value 100000000000000000."),
            ("Value 0.12345678901234567890.", "Value 0.12345678901234568."),
        ]:
            with self.subTest(input=left):
                self.assertNotEqual(self.g2p.phonemize(left), self.g2p.phonemize(right))

    def test_currency_fraction_value_and_unit_survive(self):
        for left, right in [
            ("The price is $1.5.", "The price is $1.50."),
            ("The price is $0.5.", "The price is $0.50."),
            ("The price is £2.5.", "The price is £2.50."),
        ]:
            with self.subTest(input=left):
                self.assertEqual(self.g2p.phonemize(left), self.g2p.phonemize(right))
        for left, wrong in [
            ("The price is $1.5.", "The price is $1.05."),
            ("The price is $12.345.", "The price is 12.345."),
            ("The price is $12.000.", "The price is 12.000."),
        ]:
            with self.subTest(input=left):
                self.assertNotEqual(self.g2p.phonemize(left), self.g2p.phonemize(wrong))

    def test_explicit_overrides_cannot_suppress_words(self):
        for phones in ("", " ", ".", "!", "ˈ", "ˈˌ", "\u200b"):
            for label in ("outofdictionaryxyz", "unknown word"):
                with self.subTest(phones=phones, label=label), self.assertRaises(self.error):
                    self.g2p.phonemize(f"Hello [{label}](/{phones}/) world.")
        self.assertTrue(self.g2p.phonemize("Hello [name](/həlˈO/) world."))

    def test_all_retained_brand_and_acronym_tables_remain_pronounceable(self):
        tree = ast.parse((ROOT / "voice/pronunciation_tables.py").read_text())
        tables = {"_BRAND_PRONUNCIATIONS", "_ACRONYM_WORDS", "_ACRONYM_INITIALISMS"}
        count = 0
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in tables for target in node.targets
            ):
                for name, text in ast.literal_eval(node.value).items():
                    with self.subTest(name=name):
                        self.assertTrue(self.g2p.phonemize(text))
                    count += 1
        self.assertEqual(count, 83)
        self.assertIsNone(sys.modules.get("onnxruntime"))


if __name__ == "__main__":
    unittest.main()
