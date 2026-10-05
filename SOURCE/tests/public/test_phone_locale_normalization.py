"""Per-call locale text contracts; native synthesis and language quality are separate."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
LANGUAGES = ("es", "fr", "hi", "it", "pt", "ja", "zh")


def _load_source(name, path, modules=None):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules or {}):
        spec.loader.exec_module(module)
    return module


def _logger_module():
    module = types.ModuleType("core.logging_config")
    module.get_logger = lambda name: types.SimpleNamespace(debug=lambda *a, **kw: None, warning=lambda *a, **kw: None)
    return module


def _formatter():
    module = _load_source(
        "_locale_formatter_contract",
        ROOT / "voice/synthesis/text_normalizer.py",
        {"core.logging_config": _logger_module()},
    )
    # Explicit inert configuration keeps these source contracts independent of
    # user settings. Actual phone-pipeline integration is exercised separately.
    config = types.SimpleNamespace(
        tts_pronunciation_overrides={},
        tts_brand_dict_enabled=True,
        tts_acronym_dict_enabled=True,
        tts_prosody_hints_enabled=True,
    )
    module._get_runtime_config = lambda value: config if value is None else value
    return module, config


class FormatterLocaleTests(unittest.TestCase):
    def setUp(self):
        self.module, self.config = _formatter()

    def test_default_and_explicit_english_preserve_spoken_forms(self):
        source = "The price is $1.5 at 3:30 on the 21st."
        expected = "The price is one dollar and fifty cents at three thirty on the twenty-first."
        self.assertEqual(self.module.normalize_for_speech(source), expected)
        for language in (None, "en", "en-us", "en-gb", "EN_us", types.SimpleNamespace(value="en-GB")):
            with self.subTest(language=language):
                self.assertEqual(self.module.normalize_for_speech(source, language=language), expected)

    def test_all_seven_non_english_families_preserve_exact_values_and_names(self):
        samples = {
            "es": "El precio es $1.5 a las 3:30. Spotify y OpenAI: 0.001, 15% y 21st.",
            "fr": "Le prix est $1.5 à 3:30. Spotify et OpenAI : 0.001, 15% et 21st.",
            "hi": "कीमत $1.5 है। Spotify OpenAI 3:30 0.001 15% 21st।",
            "it": "Il prezzo è $1.5 alle 3:30. Spotify e OpenAI: 0.001, 15% e 21st.",
            "pt": "O preço é $1.5 às 3:30. Spotify e OpenAI: 0.001, 15% e 21st.",
            "ja": "価格は$1.5です。Spotify OpenAI 3:30 0.001 15% 21st。",
            "zh": "价格是$1.5。Spotify OpenAI 3:30 0.001 15% 21st。",
        }
        for language, source in samples.items():
            with self.subTest(language=language):
                self.assertEqual(self.module.normalize_for_speech(source, language=language), source)

    def test_regions_enums_and_unknown_explicit_locales_do_not_guess_english(self):
        source = "$1.50 3:30 2026 AI MP3 Spotify 0.00000000000000000001"
        for language in ("es-MX", "pt_BR", "ZH-cn", "", "unknown", "Language.ES", types.SimpleNamespace(value="fr-FR")):
            with self.subTest(language=language):
                self.assertEqual(self.module.normalize_for_speech(source, language=language), source)

    def test_markup_url_redaction_and_script_survive_without_english_claims(self):
        source = "# 详情\n[查看](https://example.com/?secret=fixture)\n```\nhidden_fixture_secret\n```\n价格 **$1.50**，地址 https://example.com/?secret=another"
        for language in LANGUAGES:
            with self.subTest(language=language):
                result = self.module.normalize_for_speech(source, language=language, summarize=False)
                self.assertEqual(result, "详情 查看 价格 $1.50，地址 …")
                self.assertNotIn("secret", result)
                self.assertNotIn("hidden_fixture", result)
                self.assertNotIn("link sent in chat", result)

    def test_english_link_and_summary_messages_remain_unchanged(self):
        self.assertEqual(self.module.normalize_for_speech("[View](https://example.com)"), "View (link sent in chat)")
        self.assertEqual(
            self.module.normalize_for_speech("First. Second. Third. Fourth."),
            "First. Second. Third. I'll send the full details in chat.",
        )

    def test_non_english_summary_is_bounded_and_has_no_english_suffix(self):
        for language, text, expected in (
            ("fr", "Premier. Deuxième. Troisième. Quatrième.", "Premier. Deuxième. Troisième…"),
            ("ja", "一。二。三。四。", "一。 二。 三…"),
            ("hi", "एक। दो। तीन। चार।", "एक। दो। तीन…"),
        ):
            with self.subTest(language=language):
                self.assertEqual(self.module.normalize_for_speech(text, language=language), expected)
                self.assertEqual(self.module.normalize_for_speech(text, language=language, summarize=False), text)
        for language in LANGUAGES:
            result = self.module.normalize_for_speech("長" * 700, language=language)
            self.assertLessEqual(len(result), self.module._SUMMARY_CHARS + 1)
            self.assertTrue(result.endswith("…"))
            self.assertNotIn(self.module._SUMMARY_SUFFIX, result)

    def test_explicit_user_overrides_keep_existing_scope(self):
        self.config.tts_pronunciation_overrides = {"Name": "nom"}
        for language in LANGUAGES:
            self.assertEqual(self.module.normalize_for_speech("Name $1.5", language=language), "nom $1.5")
        self.assertEqual(self.module.normalize_for_speech("Name $1.5"), "nom one dollar and fifty cents")

    def test_shared_formatter_never_stores_a_call_locale(self):
        source = "$1.5 AI"
        for language in ("en", "ja", "fr", "en-GB", "zh", "en", "es") * 4:
            expected = "one dollar and fifty cents A I" if language.startswith("en") else source
            self.assertEqual(self.module.normalize_for_speech(source, language=language), expected)
        self.assertFalse(hasattr(self.module._DEFAULT_FORMATTER, "language"))
        self.assertFalse(hasattr(self.module._DEFAULT_FORMATTER, "_language"))


class PhoneFilterLocaleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.formatter, self.config = _formatter()
        base = types.ModuleType("pipecat.utils.text.base_text_filter")
        base.BaseTextFilter = object
        self.adapter = _load_source(
            "_phone_locale_filter_contract",
            ROOT / "telephony/tts_normalizer.py",
            {"core.logging_config": _logger_module(), base.__name__: base},
        )
        self.filter = self.adapter.SpeechTextFilter(summarize=False)
        self.alert = types.ModuleType("telephony.tts_corruption_alert")
        self.alert.notify_phone_tts_corruption = AsyncMock()
        self.modules = patch.dict(sys.modules, {
            "voice.synthesis.text_normalizer": self.formatter,
            "telephony.tts_corruption_alert": self.alert,
            "onnxruntime": None,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def bind(self, language):
        tts = types.SimpleNamespace(_settings=types.SimpleNamespace(language=language, voice="af_heart"))
        self.filter.bind_tts(tts)
        return tts

    async def test_unbound_and_missing_language_retain_english_default(self):
        source = "$1.5 at 3:30"
        expected = "one dollar and fifty cents at three thirty"
        self.assertEqual(await self.filter.filter(source), expected)
        self.bind(None)
        self.assertEqual(await self.filter.filter(source), expected)

    async def test_seven_language_switches_preserve_values_and_selected_voice(self):
        source = "$1.50 3:30 OpenAI Spotify 0.001"
        tts = self.bind("en-us")
        for language in LANGUAGES:
            with self.subTest(language=language):
                tts._settings.language = language
                self.assertEqual(await self.filter.filter(source), source)
                self.assertEqual(tts._settings.voice, "af_heart")
        tts._settings.language = "en-gb"
        self.assertEqual(await self.filter.filter("$1.5"), "one dollar and fifty cents")
        self.alert.notify_phone_tts_corruption.assert_not_awaited()

    async def test_separate_calls_do_not_share_locale(self):
        self.bind("ja")
        english = self.adapter.SpeechTextFilter(summarize=False)
        english.bind_tts(types.SimpleNamespace(_settings=types.SimpleNamespace(language="en-us")))
        result = await asyncio.gather(self.filter.filter("$1.5"), english.filter("$1.5"))
        self.assertEqual(result, ["$1.5", "one dollar and fifty cents"])

    async def test_corruption_guard_order_and_empty_turn_behavior_are_preserved(self):
        self.bind("zh")
        result = await self.filter.filter("价格是$1.5。 to=end_call")
        self.assertEqual(result, "价格是$1.5。")
        self.alert.notify_phone_tts_corruption.assert_awaited_once()
        self.assertEqual(await self.filter.filter("to=end_call"), "")
        self.bind("en-us")
        self.assertEqual(await self.filter.filter("hello 中文"), "hello")
        self.bind("ja")
        self.assertEqual(await self.filter.filter("日本語 $1.5"), "日本語 $1.5")
        self.assertIsNone(sys.modules["onnxruntime"])


if __name__ == "__main__":
    unittest.main()
