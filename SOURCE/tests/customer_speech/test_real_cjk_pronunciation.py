"""Run only with the explicit CJK companion dependencies; ONNX stays unavailable."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


class RealMandarinModelPhones(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.modules.get("onnxruntime") is not None:
            raise RuntimeError("Pronunciation-only qualification must not preload ONNX Runtime")
        cls._had_onnx = "onnxruntime" in sys.modules
        sys.modules["onnxruntime"] = None
        from voice.customer_composition import compose_customer_tokenizer
        from voice.customer_pronunciation import CustomerTokenizer

        cls.vocab = json.loads((ROOT / "third_party/kokoro_onnx/src/kokoro_onnx/config.json").read_text())["vocab"]
        cls.english = CustomerTokenizer(cls.vocab)
        cls.composed = compose_customer_tokenizer(cls.english, mandarin=True)
        cls.route = cls.composed._routes["zh"]

    @classmethod
    def tearDownClass(cls):
        if not cls._had_onnx:
            sys.modules.pop("onnxruntime", None)

    def test_real_upstream_output_gets_its_existing_model_phone_stage(self):
        from misaki.zh import ZHG2P
        from pypinyin import Style, lazy_pinyin

        self.assertEqual(lazy_pinyin("你好", style=Style.TONE3, neutral_tone_with_five=True), ["ni3", "hao3"])
        self.assertEqual(ZHG2P.py2ipa("ni3"), "ni↓")
        self.assertEqual(ZHG2P.py2ipa("hao3"), "xau̯↓")
        self.assertNotIn("̯", self.vocab)
        result = self.route.phonemize("你好")
        self.assertEqual(result["phonemes"], "ni↓xau↓")
        self.assertEqual(result["spans"][0]["trace"][0]["readings"], ["ni3", "hao3"])
        self.assertEqual(result["spans"][0]["trace"][0]["source"], "你好")
        self.assertEqual(self.composed.phonemize("你好", "zh"), "ni↓xau↓")
        self.assertTrue(self.composed.tokenize(result["phonemes"]))
        self.assertIsNone(sys.modules.get("onnxruntime"))

    def test_unknown_diacritics_and_empty_output_still_fail_closed(self):
        self.assertNotIn("❓", self.vocab)
        for phones in ("", "̯", " ̯ ", "xau❓↓", "xau̯❓↓", "xaú↓", "xau̩↓"):
            with (
                self.subTest(phones=phones),
                patch.object(self.route.chinese, "py2ipa", return_value=phones),
                self.assertRaises(ValueError),
            ):
                self.composed.phonemize("好", "zh")
        # A valid model aspiration marker remains intact; no generic mark removal.
        with patch.object(self.route.chinese, "py2ipa", return_value="kʰa↓"):
            self.assertEqual(self.composed.phonemize("好", "zh"), "kʰa↓")
        with self.assertRaises(ValueError):
            self.route._phones("xau̯↓")

    def test_native_conversion_still_consumes_each_selected_reading_once(self):
        convert = self.route.chinese.py2ipa
        with patch.object(self.route.chinese, "py2ipa", side_effect=convert) as selected:
            self.composed.phonemize("你好", "zh")
        self.assertEqual([call.args for call in selected.call_args_list], [("ni3",), ("hao3",)])
        self.assertEqual(self.composed.phonemize("Hello", "en-us"), self.english.phonemize("Hello", "en-us"))


if __name__ == "__main__":
    unittest.main()
