"""Normal qualification launch and production wiring; native boundaries are inert."""
from __future__ import annotations

import ast
import asyncio
from contextlib import nullcontext
import importlib
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
LOG = types.SimpleNamespace(**{name: lambda *a, **k: None for name in ("info", "warning", "error", "debug")})


def setUpModule():
    saved = {name: value for name, value in sys.modules.items() if name == "voice" or name.startswith("voice.")}
    parents = [(value, dict(vars(value))) for value in saved.values() if hasattr(value, "__path__")]
    old_path = list(sys.path)
    for name in saved:
        sys.modules.pop(name)
    sys.path.insert(0, str(ROOT))

    def restore():
        for name in tuple(sys.modules):
            if name == "voice" or name.startswith("voice."):
                sys.modules.pop(name)
        sys.modules.update(saved)
        for value, contents in parents:
            vars(value).clear()
            vars(value).update(contents)
        sys.path[:] = old_path

    unittest.addModuleCleanup(restore)


def actual_source(path, names, namespace, *, class_name=None):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    scope = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name).body if class_name else tree.body
    nodes = [n for n in scope if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert {n.name for n in nodes} == set(names)
    if class_name:
        nodes = [ast.ClassDef(name=class_name, bases=[], keywords=[], body=nodes, decorator_list=[])]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(ROOT / path), "exec"), namespace)
    return namespace[class_name] if class_name else namespace


class QualificationLaunchTests(unittest.TestCase):
    def setUp(self):
        self.runtime = importlib.import_module("voice.customer_runtime")
        self.addCleanup(patch.stopall)
        patch.object(self.runtime, "_profile", None).start()
        patch.object(self.runtime, "_composition", None).start()
        patch.dict(os.environ, {}, clear=True).start()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patch.object(sys, "executable", str(self.root / "ViolaApp.exe")).start()
        patch.object(sys, "frozen", True, create=True).start()
        patch.object(sys, "platform", "win32").start()
        self.marker = {
            "schema_version": 1,
            "artifact_kind": "customer-speech-qualification-only",
            "qualification_runtime_selected": True,
            "release_gate_eligible": False,
            "customer_release_eligible": False,
            "selected_languages": ["English", "Mandarin", "Spanish"],
            "selected_voice_ids": ["af_heart", "ef_dora", "zf_xiaobei"],
            "app_sha256_before_signing": "historical-not-the-signed-app-digest",
        }

    def write_marker(self):
        (self.root / self.runtime.MARKER_NAME).write_text(json.dumps(self.marker), encoding="utf-8")

    def activate(self):
        self.write_marker()
        self.runtime.activate_qualification_profile()

    def test_normal_marked_artifact_selects_before_native_import_without_external_flags(self):
        self.assertNotIn("VIOLA_KOKORO_PHONEMIZER", os.environ)
        self.activate()
        self.assertEqual(os.environ["VIOLA_KOKORO_PHONEMIZER"], "misaki-en")
        for flag in self.runtime._OFFLINE:
            self.assertEqual(os.environ[flag], "1")
        selected = self.runtime.qualification_profile()
        self.assertFalse(selected["customer_release_eligible"])
        self.assertEqual(selected["locales"], ("en-us", "es", "zh"))
        with self.assertRaises(TypeError):
            selected["customer_release_eligible"] = True
        self.runtime.require_qualification_bootstrap()

    def test_source_launch_missing_marker_and_missing_hook_fail_closed(self):
        with self.assertRaises(RuntimeError):
            self.runtime.activate_qualification_profile()
        self.write_marker()
        with self.assertRaises(RuntimeError):
            self.runtime.require_qualification_bootstrap()
        with patch.object(sys, "frozen", False):
            with self.assertRaises(RuntimeError):
                self.runtime.activate_qualification_profile()
        self.assertIsNone(self.runtime.qualification_profile())

    def test_marker_cannot_grant_release_or_expand_unselected_languages(self):
        for field, value in (
            ("schema_version", True), ("artifact_kind", "customer-release"),
            ("qualification_runtime_selected", False), ("release_gate_eligible", True),
            ("customer_release_eligible", True), ("selected_languages", ["English", "Spanish", "Japanese"]),
            ("selected_voice_ids", ["af_heart", "ef_dora", "jf_alpha"]),
            ("selected_voice_ids", ["af_heart", "ef_dora", "zf_xiaobei", "af_heart"]),
        ):
            with self.subTest(field=field, value=value), patch.dict(self.marker, {field: value}):
                self.write_marker()
                with self.assertRaises((RuntimeError, ValueError)):
                    self.runtime.activate_qualification_profile()
                self.assertNotIn("VIOLA_KOKORO_PHONEMIZER", os.environ)

    def test_late_native_import_qa_conflict_and_changed_flags_refuse(self):
        self.write_marker()
        for module in ("onnxruntime", "kokoro_onnx"):
            with patch.dict(sys.modules, {module: types.ModuleType(module)}), self.assertRaises(RuntimeError):
                self.runtime.activate_qualification_profile()
        for key, value in (("VIOLA_KOKORO_PHONEMIZER", "espeak"), ("VIOLA_INTERNAL_SPEECH_CANDIDATE", "1")):
            with patch.dict(os.environ, {key: value}), self.assertRaises(RuntimeError):
                self.runtime.activate_qualification_profile()
        self.activate()
        for key in ("VIOLA_KOKORO_PHONEMIZER", *self.runtime._OFFLINE):
            with patch.dict(os.environ, {key: "wrong"}), self.assertRaises(RuntimeError):
                self.runtime.qualification_profile()

    def test_selected_pairs_and_default_are_exact_and_do_not_advertise_dormant_voices(self):
        self.activate()
        for locale, voice in (("en-us", "af_heart"), ("es", "ef_dora"), ("zh", "zf_xiaobei")):
            self.assertEqual(self.runtime.resolve_selection(types.SimpleNamespace(tts_language=locale, tts_voice=voice)), (locale, voice))
            self.assertEqual(self.runtime.resolve_phone_voice(voice), (locale, voice))
        self.assertEqual(self.runtime.resolve_selection(types.SimpleNamespace()), ("en-us", "af_heart"))
        for locale, voice in (("es", "af_heart"), ("en-us", "af_alloy"), ("auto", "af_heart"), ("zh-HK", "zf_xiaobei"), ("ja", "jf_alpha"), ("zh", "missing")):
            with self.subTest(locale=locale, voice=voice), self.assertRaises(ValueError):
                self.runtime.resolve_selection(types.SimpleNamespace(tts_language=locale, tts_voice=voice))

    def test_existing_components_construct_once_and_failed_composition_is_not_cached(self):
        self.activate()
        english = Mock()
        owner = types.SimpleNamespace(supports_locale=lambda locale: locale in {"en-us", "es", "zh"})
        compose = Mock(side_effect=[RuntimeError("missing dependency"), owner])
        modules = {
            "kokoro_onnx.config": types.SimpleNamespace(DEFAULT_VOCAB={"a": 1}),
            "voice.customer_pronunciation": types.SimpleNamespace(CustomerTokenizer=english),
            "voice.customer_composition": types.SimpleNamespace(compose_customer_tokenizer=compose),
        }
        with patch.dict(sys.modules, modules):
            with self.assertRaises(RuntimeError):
                self.runtime.get_qualification_composition()
            self.assertIsNone(self.runtime._composition)
            self.assertIs(self.runtime.get_qualification_composition(), owner)
            self.assertIs(self.runtime.get_qualification_composition(), owner)
        self.assertEqual(compose.call_count, 2)
        self.assertEqual(compose.call_args.kwargs, {"mandarin": True, "romance_locales": ("es",)})

    def factory(self, create):
        namespace = {"threading": threading, "_shared_kokoro_lock": threading.Lock(), "_shared_kokoro": None,
                     "_shared_kokoro_attempted": False, "create_kokoro": create, "_forward_generic_voice": lambda config: None}
        actual_source("voice/synthesis/factory.py", {"get_shared_kokoro"}, namespace)
        module = types.ModuleType("voice.synthesis.factory")
        module.get_shared_kokoro = namespace["get_shared_kokoro"]
        package = types.ModuleType("voice.synthesis"); package.__path__ = []
        patch.dict(sys.modules, {"voice.synthesis": package, "voice.synthesis.factory": module}).start()
        return namespace

    def test_normal_bootstrap_and_wake_callers_share_selected_owner_and_voice(self):
        self.activate()
        owner = object()
        patch.object(self.runtime, "get_qualification_composition", return_value=owner).start()
        engine = types.SimpleNamespace(set_customer_route=Mock(), _speech_language="es", _voice="ef_dora")
        create = Mock(return_value=engine)
        factory = self.factory(create)
        bootstrap = actual_source("bootstrap/factory.py", {"create_tts"}, {"cast": lambda kind, value: value, "TTSEngineType": object, "Any": object}, class_name="BootstrapFactory")
        synth = actual_source("voice/synthesizer.py", {"_create_implementation"}, {}, class_name="Synthesizer")
        config = types.SimpleNamespace(tts_backend="kokoro", tts_language="es", tts_voice="ef_dora")
        self.assertIs(bootstrap.create_tts(config), engine)
        instance = synth(); instance.config = config
        self.assertIs(instance._create_implementation(), engine)
        self.assertIs(factory["_shared_kokoro"], engine)
        create.assert_called_once_with(config, customer_tokenizer=owner, language="es", voice="ef_dora")
        engine.set_customer_route.assert_called_once_with("es", "ef_dora")

    def test_normal_creation_failure_never_enters_legacy_fallback_or_retries_failed_selection(self):
        self.activate()
        patch.object(self.runtime, "get_qualification_composition", return_value=object()).start()
        create = Mock(side_effect=RuntimeError("missing selected input"))
        factory = self.factory(create)
        bootstrap = actual_source("bootstrap/factory.py", {"create_tts"}, {"cast": lambda kind, value: value, "TTSEngineType": object, "Any": object}, class_name="BootstrapFactory")
        synth = actual_source("voice/synthesizer.py", {"_create_implementation"}, {}, class_name="Synthesizer")
        config = types.SimpleNamespace(tts_backend="kokoro", tts_language="zh", tts_voice="zf_xiaobei")
        with self.assertRaises(RuntimeError):
            bootstrap.create_tts(config)
        self.assertTrue(config._customer_speech_selection_failed)
        self.assertIsNone(factory["_shared_kokoro"])
        instance = synth(); instance.config = config
        with self.assertRaises(RuntimeError):
            instance._create_implementation()
        self.assertEqual(create.call_count, 1)

    def test_startup_sync_uses_effective_saved_pair_before_engine_creation(self):
        self.activate()
        namespace = {"logger": LOG}
        bootstrap = actual_source("bootstrap/factory.py", {"_apply_audio_settings_to_app_config"}, namespace, class_name="BootstrapFactory")
        requested = []
        def read(key, default=None, **kwargs):
            requested.append((key, kwargs))
            return {"tts_language": "zh", "tts_voice": "zf_xiaobei"}.get(key, default)
        config = types.SimpleNamespace(stt_backend="whisper_local", wake_sensitivity=.5, tts_rate=1.0)
        bootstrap._apply_audio_settings_to_app_config(settings_obj=config, settings_mgr=types.SimpleNamespace(get=read))
        self.assertEqual((config.tts_language, config.tts_voice), ("zh", "zf_xiaobei"))
        self.assertIn(("tts_voice", {"on_load_error": "raise"}), requested)

    def engine(self):
        methods = {"set_customer_route", "invalidate_customer_route", "_prepare_route_text", "_synthesize_locked"}
        cls = actual_source("voice/synthesis/kokoro_engine.py", methods, {"logger": LOG, "_MAX_TEXT_LENGTH": 5000, "_strip_emoji": lambda text: text.replace("❓", "")}, class_name="KokoroTTSEngine")
        engine = cls(); engine._lock = threading.Lock()
        engine._customer_tokenizer = types.SimpleNamespace(supports_locale=lambda locale: locale in {"en-us", "es", "zh"})
        engine._kokoro = types.SimpleNamespace(get_voices=lambda: ["af_heart", "ef_dora", "zf_xiaobei"])
        engine._speech_language, engine._voice = "en-us", "af_heart"
        engine._tts_disable_marker = object(); engine._tts_is_enabled = lambda: True
        engine._ensure_loaded = Mock(return_value=True); engine._synthesize_internal = Mock(return_value=b"audio")
        return engine

    def test_atomic_route_change_retires_old_work_and_failed_apply_blocks_stale_speech(self):
        self.activate(); engine = self.engine(); old_marker = engine._tts_disable_marker
        engine.set_customer_route("es", "ef_dora")
        self.assertEqual(engine._synthesize_locked("old", None, policy_marker=old_marker), b"")
        engine._synthesize_internal.assert_not_called()
        with self.assertRaises(ValueError):
            engine.set_customer_route("zh", "af_heart")
        self.assertEqual((engine._speech_language, engine._voice), ("es", "ef_dora"))
        with self.assertRaises(RuntimeError):
            engine._synthesize_locked("new", None)
        engine._ensure_loaded.assert_not_called()
        engine.set_customer_route("zh", "zf_xiaobei")
        self.assertIsNone(engine._customer_route_error)
        self.assertEqual(engine._synthesize_locked("你好", None), b"audio")

    def test_pre_setter_invalidation_and_missing_actual_voice_never_mutate_pair(self):
        self.activate(); engine = self.engine(); old = (engine._speech_language, engine._voice)
        engine.invalidate_customer_route()
        with self.assertRaises(RuntimeError):
            engine._synthesize_locked("hello", None)
        self.assertEqual((engine._speech_language, engine._voice), old)
        engine._kokoro.get_voices = lambda: ["af_heart", "ef_dora"]
        with self.assertRaises(ValueError):
            engine.set_customer_route("zh", "zf_xiaobei")
        self.assertEqual((engine._speech_language, engine._voice), old)

    def test_customer_text_and_pronunciation_failures_are_not_silently_dropped(self):
        self.activate(); engine = self.engine()
        self.assertEqual(engine._prepare_route_text("hola ❓"), "hola ❓")
        with self.assertRaises(ValueError):
            engine._prepare_route_text("a" * 5001)
        engine._synthesize_internal.side_effect = ValueError("unsupported pronunciation")
        with self.assertRaises(ValueError):
            engine._synthesize_locked("hola ❓", None)
        engine._customer_tokenizer = None
        self.assertEqual(engine._prepare_route_text("hello ❓"), "hello ")

    def test_phone_normal_constructor_binds_composition_before_queue_side_effects(self):
        self.activate(); owner = object()
        construct = patch.object(self.runtime, "get_qualification_composition", return_value=owner).start()
        check = Mock()
        patch.dict(sys.modules, {"voice.customer_composition": types.SimpleNamespace(require_customer_composition=check)}).start()
        manager = actual_source("telephony/call_manager.py", {"__init__"}, {"os": os, "_require_phone_customer_startup": lambda: None}, class_name="CallManager")
        instance = manager.__new__(manager)
        config = types.SimpleNamespace(tts_voice="zf_xiaobei", tts_provider="local", is_configured=False)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            instance.__init__(config)
        self.assertIs(instance._customer_tokenizer, owner)
        construct.assert_called_once_with(); check.assert_called_once_with(owner)

    def test_phone_warmup_uses_the_selected_language_and_voice(self):
        self.activate()
        namespace = actual_source("telephony/call_manager.py", {"_warm_phone_kokoro_tts_runtime"}, {"os": os, "time": time, "_PHONE_TTS_WARMUP_TEXT": "legacy"})
        model = types.SimpleNamespace(create=Mock(return_value=([.1], 24000)))
        for voice, locale, text in (("af_heart", "en-us", "Hello."), ("ef_dora", "es", "hola"), ("zf_xiaobei", "zh", "你好")):
            namespace["_warm_phone_kokoro_tts_runtime"](model, voice=voice)
            model.create.assert_called_with(text, voice=voice, lang=locale)

    def test_phone_marker_without_hook_stops_before_normal_native_imports(self):
        self.write_marker()
        namespace = actual_source(
            "telephony/call_manager.py", {"_require_phone_customer_startup"},
            {"os": os, "_PHONE_CUSTOMER_PROFILE_AT_IMPORT": False},
        )
        native = Mock()
        with patch.dict(sys.modules, {"kokoro_onnx": types.SimpleNamespace(_require_customer_telemetry_opt_out=native)}):
            with self.assertRaisesRegex(RuntimeError, "startup hook"):
                namespace["_require_phone_customer_startup"]()
        native.assert_not_called()

    def test_stream_route_change_drains_old_response_and_next_utterance_uses_new_locale(self):
        self.activate()
        engine = self.engine()
        namespace = {"asyncio": asyncio, "time": time, "logger": LOG, "_SENTENCE_RE": re.compile(r"(?<=[.!?])\s+")}
        cls = actual_source("voice/synthesis/kokoro_engine.py", {"speak_streaming"}, namespace, class_name="KokoroTTSEngine")
        engine.speak_streaming = types.MethodType(cls.speak_streaming, engine)
        engine._speak_lock = None
        engine.last_sample_rate = 24000
        normalized, synthesized = [], []

        def normalize(text, **kwargs):
            normalized.append((text, kwargs))
            return text

        async def synthesize(text, voice, **kwargs):
            synthesized.append((text, engine._speech_language, engine._voice))
            return b"\0\0"

        async def gap(*args):
            return None

        engine._run_synthesize_with_watchdog = synthesize
        engine._play_pcm_if_enabled = lambda *args, **kwargs: True
        engine._smooth_sentence_boundary = lambda old, current: current
        engine._sleep_sentence_gap = gap
        modules = {
            "voice.synthesis.text_normalizer": types.SimpleNamespace(normalize_for_speech=normalize, has_pending_decimal_point=lambda text: False),
            "diagnostics.wake_state_sync": types.SimpleNamespace(get_state_sync_monitor=lambda: types.SimpleNamespace(update_tts_state=lambda **kwargs: None)),
            "utils.audio_ducking": types.SimpleNamespace(duck_context=nullcontext),
        }
        engine.set_customer_route("es", "ef_dora")

        async def changing():
            yield "hola."
            engine.set_customer_route("zh", "zf_xiaobei")
            yield "你好."

        async def following():
            yield "你好."

        with patch.dict(sys.modules, modules):
            self.assertEqual(asyncio.run(engine.speak_streaming(changing())), "hola.你好.")
            self.assertEqual(normalized, [("hola.", {"language": "es"})])
            self.assertEqual(synthesized, [("hola.", "es", "ef_dora")])
            asyncio.run(engine.speak_streaming(following()))
        self.assertEqual(normalized[-1], ("你好.", {"language": "zh"}))
        self.assertEqual(synthesized[-1], ("你好.", "zh", "zf_xiaobei"))

    def test_default_source_and_qa_factory_keep_legacy_creation_contract(self):
        create = Mock(return_value=object()); namespace = self.factory(create)
        config = types.SimpleNamespace(tts_voice="default")
        first = namespace["get_shared_kokoro"](config)
        self.assertIs(namespace["get_shared_kokoro"](config), first)
        create.assert_called_once_with(config)

    def test_qualification_disables_remote_probe_and_tts_warmup_but_keeps_qa_flag(self):
        self.activate()
        namespace = {"os": os, "time": time, "logger": LOG, "_TRUE_VALUES": {"1", "true", "yes", "on"},
                     "_health": types.SimpleNamespace(available=Mock(return_value=True)), "_execute_op": Mock()}
        actual_source("telephony/remote_voice.py", {
            "_env", "phone_voice_remote_enabled", "phone_voice_remote_url", "phone_voice_remote_api_key",
            "_remote_configured", "remote_voice_available", "warm_remote_voice",
        }, namespace)
        with patch.dict(os.environ, {"VIOLA_PHONE_VOICE_REMOTE_ENABLED": "1", "VIOLA_PHONE_VOICE_REMOTE_URL": "https://example.invalid", "VIOLA_PHONE_VOICE_REMOTE_API_KEY": "synthetic-fixture"}):
            self.assertFalse(namespace["phone_voice_remote_enabled"]())
            self.assertFalse(namespace["remote_voice_available"]())
            self.assertFalse(namespace["warm_remote_voice"]())
            namespace["_health"].available.assert_not_called()
            namespace["_execute_op"].assert_not_called()
            with patch.object(sys, "frozen", False), patch.object(self.runtime, "_profile", None):
                self.assertTrue(namespace["phone_voice_remote_enabled"]())


if __name__ == "__main__":
    unittest.main()
