"""Customer locale declaration and real phone controls; no native speech execution."""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import os
import subprocess
import sys
import threading
import time
from contextlib import suppress
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CustomerPhoneRouting(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = _load("_routing_real_phone_fixture", ROOT / "tests/public/test_phone_language_switch.py")
        cls.case = cls.fixture.PhoneLanguageSwitch
        cls.case.setUpClass()
        cls.addClassCleanup(cls.case.doClassCleanups)
        cls.customer = _load("_routing_customer_contract", ROOT / "voice/customer_pronunciation.py")

    def setUp(self):
        self.environment = patch.dict(os.environ, {"VIOLA_KOKORO_PHONEMIZER": "espeak"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.t = self.new_call()

    def new_call(self):
        call = self.case("runTest")
        call.setUp()
        self.addCleanup(call.doCleanups)
        return call

    def bind_customer(self, call=None, customer=None):
        call = call or self.t
        if customer is None:
            customer = self.customer.CustomerTokenizer.__new__(self.customer.CustomerTokenizer)
        call.tts._kokoro = types.SimpleNamespace(
            tokenizer=types.SimpleNamespace(_customer=customer), get_voices=lambda: list(REFERENCE_VOICES)
        )
        return customer

    def last_context(self, call=None):
        call = call or self.t
        return call.context.messages[-1]["content"]

    def test_customer_declaration_is_exact_and_does_not_construct_nlp(self):
        supports = self.customer.CustomerTokenizer.supports_locale
        for lang in ("en-us", "en-gb", "EN_US", "en_GB"):
            self.assertTrue(supports(lang))
        for lang in (None, True, 1, {}, "", "en", "en-ca", " en-us", "en-us ", "ja", "zh", "fr", "javascript"):
            with self.subTest(lang=lang):
                self.assertFalse(supports(lang))

    def test_complete_pinned_registry_tags_and_enum_values_are_accepted(self):
        parse = self.t.handler_module._parse_phone_language
        cases = {
            "es-MX": "es-MX", "ES_mx": "es-MX", "Language.ES": "es", "Language.PT_BR": "pt-BR",
            "fr-CA": "fr-CA", "hi-IN": "hi-IN", "it-CH": "it-CH", "ja-JP": "ja-JP",
            "zh-CN": "zh-CN", "zh-TW": "zh-TW", "en-GB": "en-GB", "es-419": "es-419",
        }
        for raw, value in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse(raw).value, value)
        self.assertEqual(parse(self.t.language.EN_GB), self.t.language.EN_GB)
        for name, member in self.t.language.__members__.items():
            with self.subTest(enum_member=name):
                self.assertEqual(parse("Language." + name), member)
                self.assertEqual(parse(member), member)
                self.assertEqual(parse(member.value), member)

    def test_capability_lookup_imports_no_nlp_or_native_runtime(self):
        source = ROOT / "voice/customer_pronunciation.py"
        code = "\n".join([
            "import sys, importlib.util",
            "blocked = ('onnxruntime', 'spacy', 'en_core_web_sm', 'misaki')",
            "sys.modules.update({name: None for name in blocked})",
            f"spec = importlib.util.spec_from_file_location('pure_customer', {str(source)!r})",
            "module = importlib.util.module_from_spec(spec)",
            "spec.loader.exec_module(module)",
            "assert module.CustomerTokenizer.supports_locale('EN_US')",
            "assert not module.CustomerTokenizer.supports_locale('zh')",
            "assert all(sys.modules[name] is None for name in blocked)",
        ])
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    async def test_malformed_asr_tags_never_become_language_prefixes(self):
        for raw in ("javascript", "frankenstein", "es-not-a-real-tag", "Language.JAgarbage", "x.Language.ES", "es\nJA", "", None, 3, {}):
            with self.subTest(raw=raw):
                self.t = self.new_call()
                for _ in range(3):
                    await self.t.handler.on_transcription_with_language("private fixture", raw, 0.99)
                self.t.assert_unchanged()

    async def test_invalid_confidence_does_not_acknowledge_a_switch(self):
        for confidence in (True, False, float("nan"), float("inf"), -1, 1.01, "0.99", None):
            with self.subTest(confidence=confidence):
                self.t = self.new_call()
                for _ in range(3):
                    await self.t.handler.on_transcription_with_language("private fixture", "es", confidence)
                self.t.assert_unchanged()

    async def test_invalid_tag_resets_the_existing_detection_threshold(self):
        await self.t.handler.on_transcription_with_language("fixture", "es-MX", 0.9)
        await self.t.handler.on_transcription_with_language("fixture", "javascript", 0.9)
        await self.t.handler.on_transcription_with_language("fixture", "es-MX", 0.9)
        self.t.assert_unchanged()
        await self.t.handler.on_transcription_with_language("fixture", "es-MX", 0.9)
        self.assertEqual(self.t.tts._settings.language, "es")

    async def test_regional_tags_keep_all_eight_legacy_families_and_exact_voice(self):
        self.assertEqual(self.t.handler_module.KOKORO_SUPPORTED, {"en", "es", "fr", "hi", "it", "pt", "ja", "zh"})
        for raw, expected in (("en-US", "en-us"), ("en-GB", "en-gb"), ("es-MX", "es"), ("fr-CA", "fr"), ("hi-IN", "hi"), ("it-CH", "it"), ("pt-BR", "pt"), ("ja-JP", "ja"), ("zh-CN", "zh")):
            with self.subTest(raw=raw):
                self.t = self.new_call()
                self.t.tts._settings.voice = "bf_emma"
                await self.t.handler._switch_language(raw)
                self.assertEqual(self.t.tts._settings.language, expected)
                self.assertEqual(self.t.tts._settings.voice, "bf_emma")
                self.assertIn("tts_language_supported: true", self.last_context())

    async def test_customer_backend_rejects_seven_unqualified_locales_before_settings(self):
        for code in ("es", "fr", "hi", "it", "pt", "ja", "zh"):
            with self.subTest(code=code):
                self.t = self.new_call()
                self.bind_customer()
                self.t.tts._settings.language = "en-gb"
                self.t.tts._settings.voice = "bf_emma"
                with patch.object(self.t.tts, "process_frame", wraps=self.t.tts.process_frame) as process:
                    await self.t.handler._switch_language(code)
                    process.assert_not_awaited()
                self.assertEqual(self.t.tts._settings.language, "en-gb")
                self.assertEqual(self.t.tts._settings.voice, "bf_emma")
                self.assertEqual(self.t.handler.current_language, "en")
                context = self.last_context()
                self.assertIn("tts_language_supported: false", context)
                self.assertIn("tts_language_scope: selected_pronunciation_backend", context)
                self.assertNotIn("tts_language_active:", context)
                self.assertNotIn("private fixture", context)

    async def test_customer_canonical_english_mapping_is_checked_before_lookup(self):
        for raw, locale in (("en", "en-us"), ("en-US", "en-us"), ("en-GB", "en-gb")):
            with self.subTest(raw=raw):
                self.t = self.new_call()
                observed = []
                self.bind_customer(customer=types.SimpleNamespace(supports_locale=lambda value: observed.append(value) or self.customer.CustomerTokenizer.supports_locale(value)))
                await self.t.handler._switch_language(raw)
                self.assertEqual(observed, [locale])
                self.assertEqual(self.t.tts._settings.language, locale)
                self.assertEqual(self.t.tts._settings.voice, "bf_emma" if locale == "en-gb" else "af_heart")

    async def test_unknown_or_throwing_customer_capability_fails_closed(self):
        def fail(locale):
            raise RuntimeError("private fixture detail")
        for customer in (object(), types.SimpleNamespace(supports_locale=fail), types.SimpleNamespace(supports_locale=lambda value: "yes")):
            with self.subTest(customer=type(customer).__name__):
                self.t = self.new_call()
                self.bind_customer(customer=customer)
                await self.t.handler._switch_language("es")
                self.assertEqual(self.t.tts._settings.language, "en-us")
                self.assertNotIn("tts_language_active:", self.last_context())
                self.assertNotIn("private fixture", self.last_context())

    async def test_selected_customer_without_bound_capability_is_not_legacy_readiness(self):
        with patch.dict(os.environ, {"VIOLA_KOKORO_PHONEMIZER": "misaki-en"}):
            await self.t.handler._switch_language("ja")
        self.assertEqual(self.t.tts._settings.language, "en-us")
        self.assertIn("tts_language_supported: false", self.last_context())
        self.assertNotIn("tts_language_active:", self.last_context())

    async def test_bound_customer_is_authoritative_despite_default_selector(self):
        self.bind_customer()
        await self.t.handler._switch_language("zh")
        self.assertEqual(self.t.tts._settings.language, "en-us")
        self.assertNotIn("tts_language_active:", self.last_context())

    def test_remote_first_lookup_uses_local_fallback_capability_without_remote_calls(self):
        source = ROOT / "telephony/remote_voice.py"
        tree = ast.parse(source.read_text())
        proxy = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "RemoteFirstKokoro")
        methods = [node for node in proxy.body if isinstance(node, ast.FunctionDef) and node.name in {"__init__", "__getattr__"}]
        module = ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            ast.ClassDef(name="CapabilityProxy", bases=[], keywords=[], body=methods, decorator_list=[]),
        ], type_ignores=[])
        namespace = {}
        exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
        self.bind_customer()
        local = self.t.tts._kokoro
        self.t.tts._kokoro = namespace["CapabilityProxy"](local)
        supports = self.t.handler_module._selected_backend_supports_locale
        self.assertTrue(supports(self.t.tts, "en-us"))
        self.assertFalse(supports(self.t.tts, "ja"))
        self.assertIs(self.t.tts._kokoro._local, local)

    async def test_rejected_context_preserves_state_and_allows_retry(self):
        self.bind_customer()
        self.t.publish.side_effect = RuntimeError("controlled context failure")
        with self.assertRaises(RuntimeError):
            await self.t.handler._switch_language("ja")
        self.assertEqual(self.t.tts._settings.language, "en-us")
        self.assertFalse(self.t.handler.has_switched)
        self.assertEqual(self.t.tts._settings.voice, "af_heart")
        self.t.publish.side_effect = None
        await self.t.handler._switch_language("ja")
        self.assertIn("tts_language_supported: false", self.last_context())

    async def test_failed_valid_switch_rolls_back_exact_customer_dialect_and_voice(self):
        self.bind_customer()
        self.t.tts._settings.language = "en-gb"
        self.t.tts._settings.voice = "bf_emma"
        with patch.object(self.t.tts, "process_frame", new=AsyncMock(side_effect=RuntimeError("controlled settings failure"))):
            await self.t.handler._switch_language("en-US")
        self.assertEqual(self.t.tts._settings.language, "en-gb")
        self.assertEqual(self.t.tts._settings.voice, "bf_emma")
        self.assertFalse(self.t.handler.has_switched)
        self.t.publish.assert_not_awaited()

    async def test_separate_requests_do_not_share_backend_locale_or_voice(self):
        english = self.t
        french = self.new_call()
        self.bind_customer(english)
        english.tts._settings.voice = "bf_emma"
        french.tts._settings.voice = "af_heart"
        await asyncio.gather(english.handler._switch_language("en-GB"), french.handler._switch_language("fr-CA"))
        self.assertEqual((english.tts._settings.language, english.tts._settings.voice), ("en-gb", "bf_emma"))
        self.assertEqual((french.tts._settings.language, french.tts._settings.voice), ("fr", "af_heart"))
        self.assertIsNot(english.context.messages, french.context.messages)


# IDs measured from the unchanged bca610b8 voices-v1.0.bin. These are routing
# fixtures, not bundled model arrays or acoustic suitability assertions.
REFERENCE_VOICES = """
af_alloy af_aoede af_bella af_heart af_jessica af_kore af_nicole af_nova af_river af_sarah af_sky
am_adam am_echo am_eric am_fenrir am_liam am_michael am_onyx am_puck am_santa
bf_alice bf_emma bf_isabella bf_lily bm_daniel bm_fable bm_george bm_lewis
ef_dora em_alex em_santa ff_siwis hf_alpha hf_beta hm_omega hm_psi if_sara im_nicola
jf_alpha jf_gongitsune jf_nezumi jf_tebukuro jm_kumo pf_dora pm_alex pm_santa
zf_xiaobei zf_xiaoni zf_xiaoxiao zf_xiaoyi zm_yunjian zm_yunxi zm_yunxia zm_yunyang
""".split()


class KokoroVoiceRouteContracts(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        source = ROOT / "third_party/kokoro_onnx/src/kokoro_onnx/__init__.py"
        tree = ast.parse(source.read_text())
        kokoro = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Kokoro")
        names = {"create", "create_stream", "get_voice_style", "get_voices"}
        methods = [node for node in kokoro.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        namespace = {"np": np, "time": time, "asyncio": asyncio, "suppress": suppress, "SAMPLE_RATE": 24000, "log": types.SimpleNamespace(debug=lambda *args, **kwargs: None)}
        module = ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            ast.ClassDef(name="SourceKokoro", bases=[], keywords=[], body=methods, decorator_list=[]),
        ], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
        cls.model_class = namespace["SourceKokoro"]
        cls.customer = _load("_routing_voice_customer", ROOT / "voice/customer_pronunciation.py")

    def setUp(self):
        self.engine = self.model_class()
        self.engine.voices = {name: object() for name in REFERENCE_VOICES}
        self.phonemizer_calls = []
        self.audio_calls = []
        adapter = self.customer.CustomerTokenizer.__new__(self.customer.CustomerTokenizer)
        adapter.vocab = {"a": 1}
        adapter._lock = threading.Lock()

        def record(text):
            self.phonemizer_calls.append(text)
            return "a", [types.SimpleNamespace(text="fixture", phonemes="a")]

        adapter._g2p = {"en-us": record, "en-gb": record}
        self.engine.tokenizer = adapter
        self.engine._split_phonemes = lambda phones: [phones]

        def audio(phones, style, speed):
            self.audio_calls.append(style)
            return np.zeros(1, dtype=np.float32), 24000

        self.engine._create_audio = audio  # Recording boundary; never a native session.

    def test_all_54_voice_ids_forward_without_prefix_or_dialect_coercion(self):
        self.assertEqual(len(set(REFERENCE_VOICES)), 54)
        self.assertEqual(set(name[0] for name in REFERENCE_VOICES), set("abefhijpz"))
        self.assertEqual(self.engine.get_voices(), sorted(REFERENCE_VOICES))
        for locale in ("en-us", "en-gb"):
            for voice in REFERENCE_VOICES:
                with self.subTest(locale=locale, voice=voice):
                    self.engine.create("fixture", voice, lang=locale, trim=False)
                    self.assertIs(self.audio_calls[-1], self.engine.voices[voice])
        self.assertEqual(len(self.audio_calls), 108)

    async def test_all_54_voice_ids_forward_unchanged_in_streaming_calls(self):
        for locale in ("en-us", "en-gb"):
            for voice in REFERENCE_VOICES:
                with self.subTest(locale=locale, voice=voice):
                    frames = [chunk async for chunk in self.engine.create_stream("fixture", voice, lang=locale, trim=False)]
                    self.assertEqual(len(frames), 1)
                    self.assertIs(self.audio_calls[-1], self.engine.voices[voice])
        self.assertEqual(len(self.audio_calls), 108)

    async def test_missing_voice_ids_fail_before_pronunciation_or_audio(self):
        for voice in ("", "unknown", "af_missing", "zf_missing", "AF_HEART", "af_heart "):
            for locale in ("en-us", "en-gb", "ja"):
                with self.subTest(voice=voice, locale=locale):
                    with self.assertRaises((AssertionError, KeyError)):
                        self.engine.create("private fixture", voice, lang=locale, trim=False)
                    with self.assertRaises((AssertionError, KeyError)):
                        _ = [chunk async for chunk in self.engine.create_stream("private fixture", voice, lang=locale, trim=False)]
        self.assertEqual(self.phonemizer_calls, [])
        self.assertEqual(self.audio_calls, [])

    async def test_customer_unsupported_locale_pairs_never_reach_audio(self):
        for locale in ("en", "en-ca", "es", "fr", "hi", "it", "pt", "ja", "zh", "de", "unknown"):
            with self.subTest(locale=locale):
                with self.assertRaises(self.customer.PronunciationError) as error:
                    self.engine.create("private fixture", "af_heart", lang=locale, trim=False)
                self.assertNotIn("private fixture", str(error.exception))
                with self.assertRaises(self.customer.PronunciationError):
                    _ = [chunk async for chunk in self.engine.create_stream("private fixture", "zf_xiaobei", lang=locale, trim=False)]
        self.assertEqual(self.phonemizer_calls, [])
        self.assertEqual(self.audio_calls, [])


if __name__ == "__main__":
    unittest.main()
