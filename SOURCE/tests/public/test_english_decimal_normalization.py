"""Exact plain-decimal normalization; native audio and broader identifiers are separate."""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from decimal import localcontext
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def _formatter():
    log = types.ModuleType("core.logging_config")
    log.get_logger = lambda name: types.SimpleNamespace(debug=lambda *args, **kwargs: None)
    spec = importlib.util.spec_from_file_location("_decimal_formatter_contract", ROOT / "voice/synthesis/text_normalizer.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"core.logging_config": log}):
        spec.loader.exec_module(module)
    config = types.SimpleNamespace(
        tts_pronunciation_overrides={},
        tts_brand_dict_enabled=True,
        tts_acronym_dict_enabled=True,
        tts_prosody_hints_enabled=False,
    )
    return module, module.SpeechFormatter(summarize=False, config=config)


class EnglishDecimalNormalization(unittest.TestCase):
    def setUp(self):
        self.module, self.formatter = _formatter()

    def test_exact_fraction_sign_and_trailing_zeros(self):
        cases = {
            "0.5": "zero point five",
            "-1.25": "minus one point two five",
            ".5": "zero point five",
            "-.5": "minus zero point five",
            "+1.05": "plus one point zero five",
            "−0.005": "minus zero point zero zero five",
            "0.000": "zero point zero zero zero",
            "-0.00": "minus zero point zero zero",
            "001.050": "one point zero five zero",
            "2026.1999": "two thousand twenty six point one nine nine nine",
            "123.4567890": "one hundred twenty three point four five six seven eight nine zero",
            "0.1234567890": "zero point one two three four five six seven eight nine zero",
        }
        for source, expected in cases.items():
            for locale in (None, "en", "en-us", "en-gb", "EN_US"):
                with self.subTest(source=source, locale=locale):
                    self.assertEqual(self.formatter.format(f"Value {source}.", language=locale), f"Value {expected}.")

    def test_all_fraction_digits_survive_decimal_context_changes(self):
        fraction = "12345678901234567890"
        expected = " ".join(self.module._DIGIT_NAMES[int(digit)] for digit in fraction)
        for precision in (1, 3, 6, 28, 80):
            with self.subTest(precision=precision), localcontext() as context:
                context.prec = precision
                result = self.formatter.format("Value -12345678901234567." + fraction + ".")
                self.assertTrue(result.startswith("Value minus twelve quadrillion"))
                self.assertEqual(result.split(" point ", 1)[1], expected + ".")

    def test_long_fraction_has_no_implicit_twenty_digit_truncation(self):
        fraction = "1234567890" * 5
        expected = " ".join(self.module._DIGIT_NAMES[int(digit)] for digit in fraction)
        self.assertEqual(self.formatter.format("0." + fraction), "zero point " + expected)

    def test_unsupported_decimals_remain_complete_without_partial_rewrites(self):
        for source in ("1000000000000000000000.2026", "9" * 150 + ".1234567890", "0." + "5" * 150):
            with self.subTest(source=source):
                self.assertEqual(self.formatter.format("Value " + source + "."), "Value " + source + ".")

    def test_existing_currency_percent_time_phone_and_ordinal_contracts(self):
        cases = {
            "$1.5": "one dollar and fifty cents",
            "$12.345": "twelve point three four five dollars",
            "$0.01": "one cent",
            "$12.000": "twelve dollars",
            "7.5%": "seven point five percent",
            "15%": "fifteen percent",
            "2:30 PM": "two thirty PM",
            "21st in 2026": "twenty-first in twenty twenty-six",
        }
        phone = "five five five, one two three, four five six seven"
        cases.update({value: phone for value in ("555-123-4567", "555.123.4567", "(555) 123-4567", "5551234567")})
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(self.formatter.format(source), expected)
        self.assertEqual(
            self.formatter.format("Value 0.5; call 555.123.4567 at 2:30 PM for $1.5."),
            "Value zero point five; call " + phone + " at two thirty PM for one dollar and fifty cents.",
        )

    def test_decimal_matcher_abstains_from_existing_identifier_and_currency_paths(self):
        # These retain the pre-existing normalizer path; this patch does not
        # claim that every inherited identifier pronunciation is correct.
        for source in (
            "v1.25", "build_1.25", "1.25kg", "1.2.3.4", "555.123.4567", "1.25.txt",
            "abc/1.25", "abc:1.25", "1.25/file", "1,234.56", "1.25e3", "A1.25",
            "$1.5", "£1.25", "€1.25", "1.25€", "₹1.25", "7.5%", "1.25-2.5",
            "1.25@example.com", "@1.25", "#1.25", "₩1.25", "1.25₽",
        ):
            with self.subTest(source=source):
                self.assertIsNone(self.module._DECIMAL_RE.search(source))

    def test_other_locales_preserve_exact_decimals_and_units(self):
        source = "Value -1.25 +0.005 0.12345678901234567890 $1.5 7.5% 555.123.4567"
        for locale in ("es", "fr", "hi", "it", "pt", "ja", "zh", "unknown", ""):
            with self.subTest(locale=locale):
                self.assertEqual(self.formatter.format(source, language=locale), source)

    def test_decimal_conversion_never_uses_float(self):
        with patch("builtins.float", side_effect=AssertionError("Float conversion prohibited")):
            self.assertEqual(self.formatter.format("Value 1.2500."), "Value one point two five zero zero.")

    def test_markdown_header_cleans_before_plain_decimal_conversion(self):
        self.assertEqual(self.formatter.format("#1.25"), "one point two five")
        self.assertEqual(self.formatter.format("Tag #1.25"), "Tag #one.twenty five")

    def test_english_url_and_summary_controls_remain_present(self):
        self.assertEqual(self.formatter.format("See https://example.com/1.25?secret=fixture"), "See (link sent in chat)")
        self.assertEqual(
            self.module.SpeechFormatter(config=self.formatter._config).format("First. Second. Third. Fourth."),
            "First. Second. Third. I'll send the full details in chat.",
        )


if __name__ == "__main__":
    unittest.main()
