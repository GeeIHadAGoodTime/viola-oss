"""Real offline pronunciation routing checks, with ONNX explicitly unavailable."""
from __future__ import annotations

import json
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class RealCustomerRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.modules.get("onnxruntime") is not None:
            raise RuntimeError("Routing qualification requires ONNX to remain unimported")
        cls._had_onnx = "onnxruntime" in sys.modules
        sys.modules["onnxruntime"] = None
        from voice.customer_pronunciation import CustomerTokenizer, PronunciationError

        cls.error = PronunciationError
        vocab = json.loads((ROOT / "third_party/kokoro_onnx/src/kokoro_onnx/config.json").read_text())["vocab"]
        cls.g2p = CustomerTokenizer(vocab)

    @classmethod
    def tearDownClass(cls):
        if not cls._had_onnx:
            sys.modules.pop("onnxruntime", None)

    def test_declared_locales_match_actual_adapter_without_unknown_fallback(self):
        self.assertEqual(set(self.g2p._g2p), {"en-us", "en-gb"})
        for locale in ("en-us", "en-gb", "EN_US", "EN_GB"):
            self.assertTrue(self.g2p.supports_locale(locale))
            self.assertTrue(self.g2p.phonemize("Hello world.", locale))
        for locale in ("en", "en-ca", "es", "fr", "hi", "it", "pt", "ja", "zh", "de", "javascript"):
            self.assertFalse(self.g2p.supports_locale(locale))
            with self.subTest(locale=locale), self.assertRaises(self.error):
                self.g2p.phonemize("Hello world.", locale)

    def test_mixed_script_is_rejected_without_silent_dispatch_or_token_loss(self):
        for text in ("Hello 世界.", "Hello 日本語.", "Hello こんにちは.", "Hello नमस्ते.", "Hello привет."):
            for locale in ("en-us", "en-gb"):
                with self.subTest(text=text, locale=locale), self.assertRaises(self.error) as caught:
                    self.g2p.phonemize(text, locale)
                self.assertNotIn(text, str(caught.exception))
        # An explicit user override is a separate supported pronunciation choice.
        self.assertEqual(self.g2p.phonemize("Hello [世界](/həlˈO/)."), self.g2p.phonemize("Hello [word](/həlˈO/)."))

    def test_shared_adapter_keeps_parallel_dialects_isolated(self):
        text = "The car is in the park."
        expected = {locale: self.g2p.phonemize(text, locale) for locale in ("en-us", "en-gb")}
        self.assertNotEqual(expected["en-us"], expected["en-gb"])
        locales = ["en-us", "en-gb"] * 16
        with ThreadPoolExecutor(max_workers=4) as pool:
            actual = list(pool.map(lambda locale: self.g2p.phonemize(text, locale), locales))
        self.assertEqual(actual, [expected[locale] for locale in locales])
        self.assertIsNone(sys.modules["onnxruntime"])


if __name__ == "__main__":
    unittest.main()
