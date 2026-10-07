"""Selected customer route contracts. Native/model boundaries remain inert.

Real Pipecat service/settings integration remains in test_customer_phone_routing;
these small source controls also run without that optional dependency stack.
"""

from __future__ import annotations

import ast
import asyncio
from contextlib import suppress
from enum import StrEnum
import importlib.util
import os
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("_selected_voice_policy", ROOT / "voice/customer_voice_routing.py")
_policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_policy)
require_customer_voice = _policy.require_customer_voice
select_customer_voice = _policy.select_customer_voice
resolve_kokoro_voice_alias = _policy.resolve_kokoro_voice_alias


def setUpModule():
    saved = {name: module for name, module in sys.modules.items() if name == "voice" or name.startswith("voice.")}
    parents = [(module, dict(vars(module))) for name, module in saved.items() if hasattr(module, "__path__")]

    def restore():
        for name in tuple(sys.modules):
            if name == "voice" or name.startswith("voice."):
                sys.modules.pop(name)
        sys.modules.update(saved)
        for module, attributes in parents:
            vars(module).clear()
            vars(module).update(attributes)

    unittest.addModuleCleanup(restore)
    for name in saved:
        sys.modules.pop(name)
    paths = patch.object(sys, "path", [str(ROOT), *sys.path])
    paths.start()
    unittest.addModuleCleanup(paths.stop)


PAIRS = (("en-us", "af_heart"), ("es", "ef_dora"), ("zh", "zf_xiaobei"))
VOICES = [voice for _, voice in PAIRS]
LOG = types.SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None, warning=lambda *a, **k: None)


def source_class(path, name, methods, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    nodes = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in methods]
    assert {n.name for n in nodes} == methods
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            ast.ClassDef(name=name, bases=[], keywords=[], body=nodes, decorator_list=[]),
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


class VoicePairPolicy(unittest.TestCase):
    def test_existing_settings_names_resolve_to_real_asset_ids(self):
        self.assertEqual(
            [resolve_kokoro_voice_alias(v) for v in ("alloy", "echo", "nova", "ef_dora")],
            ["af_alloy", "am_echo", "af_nova", "ef_dora"],
        )

    def test_selected_pairs_and_available_defaults(self):
        for locale, voice in PAIRS:
            require_customer_voice(locale, voice)
            self.assertEqual(select_customer_voice(locale, "af_heart", VOICES), voice)
        require_customer_voice("EN_US", "af_heart")
        self.assertEqual(select_customer_voice("es", "em_alex", [*VOICES, "em_alex"]), "em_alex")

    def test_cross_language_unknown_locale_and_unidentified_styles_reject(self):
        for locale, valid_voice in PAIRS:
            for voice in [v for v in VOICES if v != valid_voice] + [None, "", "AF_HEART", np.zeros(3)]:
                with self.subTest(locale=locale, voice=type(voice).__name__), self.assertRaises(ValueError):
                    require_customer_voice(locale, voice)
        for locale in ("yue", "zh-HK", "fil", "tl", "vi", "en", None):
            with self.subTest(locale=locale), self.assertRaises(ValueError):
                require_customer_voice(locale, "af_heart")

    def test_missing_locale_voice_is_not_replaced_with_another_language(self):
        with self.assertRaises(ValueError):
            select_customer_voice("es", "af_heart", ["af_heart", "zf_xiaobei"])
        for inventory in (None, "ef_dora"):
            with self.assertRaises(ValueError):
                select_customer_voice("es", "af_heart", inventory)


class ModelVoiceBoundary(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cls = source_class(
            ROOT / "third_party/kokoro_onnx/src/kokoro_onnx/__init__.py",
            "Kokoro",
            {"create", "create_stream", "get_voice_style", "get_voices"},
            {"np": np, "asyncio": asyncio, "time": time, "suppress": suppress, "log": LOG, "SAMPLE_RATE": 24000},
        )
        self.model = cls()
        self.model.voices = {v: object() for v in VOICES}
        self.calls = []
        from voice.customer_composition import ComposedCustomerTokenizer

        component = ComposedCustomerTokenizer.__new__(ComposedCustomerTokenizer)
        self.model.tokenizer = types.SimpleNamespace(
            _customer=component, phonemize=lambda text, lang: self.calls.append(("g2p", lang)) or "a"
        )
        self.model._split_phonemes = lambda phones: [phones]
        self.model._create_audio = lambda phones, style, speed: (
            self.calls.append(("audio", style)) or np.zeros(1, dtype=np.float32),
            24000,
        )

    async def test_selected_sync_and_stream_pairs_forward_the_exact_style(self):
        for locale, voice in PAIRS:
            self.model.create("fixture", voice, lang=locale, trim=False)
            _ = [frame async for frame in self.model.create_stream("fixture", voice, lang=locale, trim=False)]
            self.assertEqual(
                self.calls[-4:],
                [
                    ("g2p", locale),
                    ("audio", self.model.voices[voice]),
                    ("g2p", locale),
                    ("audio", self.model.voices[voice]),
                ],
            )

    async def test_rejected_pairs_never_call_pronunciation_or_inference(self):
        cases = [(locale, voice) for locale, correct in PAIRS for voice in VOICES if voice != correct]
        cases += [("es", "ef_missing"), ("en-us", np.zeros(3)), ("zh-HK", "zf_xiaobei")]
        for locale, voice in cases:
            for is_phonemes in (False, True):
                with self.assertRaises(ValueError):
                    self.model.create("private fixture", voice, lang=locale, is_phonemes=is_phonemes, trim=False)
                with self.assertRaises(ValueError):
                    _ = [
                        f
                        async for f in self.model.create_stream(
                            "private fixture", voice, lang=locale, is_phonemes=is_phonemes, trim=False
                        )
                    ]
        self.assertEqual(self.calls, [])

    async def test_existing_implicit_english_style_is_unchanged(self):
        from voice.customer_pronunciation import CustomerTokenizer

        self.model.tokenizer._customer = CustomerTokenizer.__new__(CustomerTokenizer)
        style = np.zeros((511, 256), dtype=np.float32)
        self.model.create("fixture", style, lang="en-us", trim=False)
        _ = [frame async for frame in self.model.create_stream("fixture", style, lang="en-us", trim=False)]
        self.assertEqual(len(self.calls), 4)
        self.assertIs(self.calls[1][1], style)
        self.assertIs(self.calls[3][1], style)


class DesktopVoiceBoundary(unittest.TestCase):
    def test_explicit_constructor_uses_named_voice_and_rejects_mismatches(self):
        path = ROOT / "voice/synthesis/kokoro_engine.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        helpers = [
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {"_cfg_bool", "_cfg_float", "_cfg_int"}
        ]
        ns = {
            "threading": threading,
            "Path": Path,
            "_DEFAULT_VOICE": "af_heart",
            "_DEFAULT_SPEED": 1.0,
            "SAMPLE_RATE_24K": 24000,
        }
        exec(compile(ast.Module(body=helpers, type_ignores=[]), str(path), "exec"), ns)
        cls = source_class(path, "KokoroTTSEngine", {"__init__", "_resolve_path", "_normalize_voice_blend"}, ns)
        from voice.customer_composition import ComposedCustomerTokenizer

        component = ComposedCustomerTokenizer.__new__(ComposedCustomerTokenizer)
        component._routes = {"es": None, "zh": None}
        cfg = types.SimpleNamespace(tts_voice_blend=[["af_river", 0.5], ["af_alloy", 0.5]])
        with (
            patch.dict(os.environ, {"VIOLA_KOKORO_PHONEMIZER": "misaki-en"}),
            patch.dict(sys.modules, {"core.platform": types.SimpleNamespace(get_project_root=lambda: ROOT)}),
        ):
            for locale, voice in PAIRS:
                engine = cls(config=cfg, customer_tokenizer=component, language=locale, voice=voice)
                self.assertEqual((engine._speech_language, engine._voice), (locale, voice))
                self.assertIsNone(engine._voice_blend_spec)
                self.assertIsNone(engine._kokoro)
                for wrong in (v for v in VOICES if v != voice):
                    with self.assertRaises(ValueError):
                        cls(config=cfg, customer_tokenizer=component, language=locale, voice=wrong)
            with self.assertRaises(ValueError):
                cls(config=cfg, customer_tokenizer=component)

    def test_explicit_customer_voice_override_and_blend_contract(self):
        cls = source_class(
            ROOT / "voice/synthesis/kokoro_engine.py", "KokoroTTSEngine", {"_resolve_voice_for_create"}, {}
        )
        for locale, voice in PAIRS:
            engine = cls()
            engine._customer_tokenizer = object()
            engine._speech_language = locale
            engine._voice = voice
            engine._voice_blend_spec = None
            self.assertEqual(engine._resolve_voice_for_create(None), (voice, voice))
            for wrong in (v for v in VOICES if v != voice):
                with self.assertRaises(ValueError):
                    engine._resolve_voice_for_create(wrong)
            engine._voice_blend_spec = [(voice, 0.5), ("af_heart", 0.5)]
            with self.assertRaises(ValueError):
                engine._resolve_voice_for_create(None)
            self.assertEqual(engine._resolve_voice_for_create(voice), (voice, voice))


class FactorySelectedRoutes(unittest.TestCase):
    def setUp(self):
        path = ROOT / "voice/synthesis/factory.py"
        self.calls = []
        self.result = types.SimpleNamespace()
        ns = {
            "logger": LOG,
            "_forward_generic_voice": lambda cfg: self.calls.append("generic"),
            "get_shared_kokoro": lambda cfg: self.calls.append("legacy") or self.result,
            "create_kokoro": lambda cfg, **kw: self.calls.append(kw) or self.result,
        }
        self.cls = source_class(
            path, "TTSFactory", {"__init__", "create_primary", "create_kokoro", "create_with_fallback"}, ns
        )
        from voice.customer_composition import ComposedCustomerTokenizer

        self.component = ComposedCustomerTokenizer.__new__(ComposedCustomerTokenizer)
        self.component._routes = {"es": None, "zh": None}
        self.env = patch.dict(os.environ, {"VIOLA_KOKORO_PHONEMIZER": "misaki-en"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def factory(self, locale="es", voice="ef_dora", backend="kokoro"):
        obj = self.cls(
            types.SimpleNamespace(tts_backend=backend),
            policy=object(),
            customer_tokenizer=self.component,
            language=locale,
            voice=voice,
        )
        obj.create_local = lambda: self.fail("An explicit customer route must not construct system TTS")
        return obj

    def test_each_selected_factory_binds_exact_route_and_named_voice(self):
        for locale, voice in PAIRS:
            factory = self.factory(locale, voice)
            self.assertIs(factory.create_primary(), self.result)
            self.assertEqual(self.calls[-1], {"customer_tokenizer": self.component, "language": locale, "voice": voice})
            self.assertIs(factory.create_with_fallback(), self.result)
        self.assertEqual(len(self.calls), 3)

    def test_missing_explicit_runtime_and_non_kokoro_do_not_fall_back(self):
        factory = self.factory()
        factory.create_kokoro = lambda: None
        with self.assertRaises(RuntimeError):
            factory.create_primary()
        with self.assertRaises(ValueError):
            self.factory(backend="pyttsx3").create_primary()

    def test_explicit_factories_never_share_a_legacy_locale_singleton(self):
        spanish = self.factory()
        mandarin = self.factory("zh", "zf_xiaobei")
        spanish.create_primary()
        mandarin.create_primary()
        self.assertEqual([call["language"] for call in self.calls], ["es", "zh"])
        self.assertNotIn("legacy", self.calls)

    def test_unsupported_locale_or_voice_rejects_before_runtime_creation(self):
        for locale, voice in (("es", "af_heart"), ("yue", "zf_xiaobei"), ("it", "if_sara"), ("zh", None)):
            with self.assertRaises(ValueError):
                self.factory(locale, voice)
        self.assertEqual(self.calls, [])


class PhoneSelectedRoutes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Keep the actual pinned language enum/resolver and actual vendor map.
        language_path = ROOT / "third_party/pipecat/src/pipecat/transcriptions/language.py"
        tree = ast.parse(language_path.read_text(encoding="utf-8"))
        nodes = [
            n
            for n in tree.body
            if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in {"Language", "resolve_language"}
        ]
        language_ns = {"StrEnum": StrEnum, "logger": LOG}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(language_path), "exec"), language_ns)
        self.Language = language_ns["Language"]
        vendor = ROOT / "third_party/pipecat/src/pipecat/services/kokoro/tts.py"
        node = next(
            n
            for n in ast.parse(vendor.read_text(encoding="utf-8")).body
            if isinstance(n, ast.FunctionDef) and n.name == "language_to_kokoro_language"
        )
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(vendor), "exec"), language_ns)
        mapper = language_ns["language_to_kokoro_language"]

        class Service:
            Settings = types.SimpleNamespace
            language_to_service_language = staticmethod(mapper)

            async def process_frame(service, frame, direction):
                self.updates.append(frame)
                service._settings.language = mapper(frame.delta.language)
                if hasattr(frame.delta, "voice"):
                    service._settings.voice = frame.delta.voice

        modules = {
            "pipecat.transcriptions.language": types.SimpleNamespace(Language=self.Language),
            "pipecat.frames.frames": types.SimpleNamespace(
                LLMMessagesAppendFrame=types.SimpleNamespace, TTSUpdateSettingsFrame=types.SimpleNamespace
            ),
            "pipecat.processors.frame_processor": types.SimpleNamespace(
                FrameDirection=types.SimpleNamespace(DOWNSTREAM="down")
            ),
            "pipecat.services.kokoro.tts": types.SimpleNamespace(KokoroTTSService=Service),
        }
        self.patch = patch.dict(sys.modules, modules)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        path = ROOT / "telephony/language_handler.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        nodes = [n for n in tree.body if not (isinstance(n, ast.ImportFrom) and n.module == "core.logging_config")]
        ns = {"get_logger": lambda name: LOG, "__name__": "_source_phone_handler"}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), ns)
        self.service = Service()
        self.service._settings = types.SimpleNamespace(language="en-us", voice="af_heart")
        self.service._kokoro = types.SimpleNamespace(
            tokenizer=types.SimpleNamespace(
                _customer=types.SimpleNamespace(supports_locale=lambda locale: locale in {"en-us", "es", "zh"})
            ),
            get_voices=lambda: VOICES,
        )
        self.updates = []
        self.handler = ns["LanguageHandler"](None, self.service)
        self.context = types.SimpleNamespace(messages=[])

        async def publish(frame, direction):
            self.context.messages.extend(frame.messages)

        self.context.process_frame = publish
        self.handler.set_context_frame_target(self.context)

    async def test_selected_regional_aliases_select_matching_named_voices(self):
        for raw, locale, voice in (
            ("en-US", "en-us", "af_heart"),
            ("ES_mx", "es", "ef_dora"),
            ("zh-CN", "zh", "zf_xiaobei"),
            ("cmn", "zh", "zf_xiaobei"),
            ("cmn-CN", "zh", "zf_xiaobei"),
        ):
            with self.subTest(raw=raw):
                await self.handler._switch_language(raw)
                self.assertEqual((self.service._settings.language, self.service._settings.voice), (locale, voice))
                self.assertIn("tts_language_supported: true", self.context.messages[-1]["content"])
        self.assertIn("recipient_language_name: Mandarin Chinese", self.context.messages[-1]["content"])

    async def test_unsupported_or_ambiguous_variants_do_not_change_settings(self):
        for raw in ("zh-HK", "zh-CN-guangxi", "yue", "yue-CN", "fil", "tl", "vi", "it", "hi"):
            await self.handler._switch_language(raw)
            self.assertEqual((self.service._settings.language, self.service._settings.voice), ("en-us", "af_heart"))
            self.assertIn("tts_language_supported: false", self.context.messages[-1]["content"])
            self.assertNotIn("tts_language_active:", self.context.messages[-1]["content"])
        self.assertEqual(self.updates, [])

    async def test_missing_selected_voice_is_unavailable_before_settings(self):
        self.service._kokoro.get_voices = lambda: ["af_heart"]
        await self.handler._switch_language("es")
        self.assertEqual(self.updates, [])
        self.assertIn("tts_voice_available: false", self.context.messages[-1]["content"])

    async def test_context_failure_rolls_back_both_voice_and_locale(self):
        async def fail(frame, direction):
            raise RuntimeError("controlled failure")

        self.context.process_frame = fail
        await self.handler._switch_language("es")
        self.assertEqual((self.service._settings.language, self.service._settings.voice), ("en-us", "af_heart"))
        self.assertFalse(self.handler.has_switched)
        self.assertEqual(self.context.messages, [])

    async def test_ignored_voice_update_never_acknowledges_a_partial_switch(self):
        async def incomplete(frame, direction):
            self.service._settings.language = "es"

        self.service.process_frame = incomplete
        await self.handler._switch_language("es")
        self.assertEqual((self.service._settings.language, self.service._settings.voice), ("en-us", "af_heart"))
        self.assertFalse(self.handler.has_switched)
        self.assertEqual(self.context.messages, [])


if __name__ == "__main__":
    unittest.main()
