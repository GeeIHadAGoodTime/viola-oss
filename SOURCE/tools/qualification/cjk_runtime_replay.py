"""Fresh post-reset inert qualification; no historical receipt is reused."""

import ast
import atexit
import shutil
import asyncio
import importlib.abc
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2]
VENDOR = SOURCE / "third_party/kokoro_onnx/src"
COMPANION = SOURCE / "third_party/misaki_cjk_prototype"
# Model the ordinary merged installed package layout. Separate roots cannot
# extend the retained English regular package, and no research alias is used.
_NAMESPACE_TEMP = tempfile.TemporaryDirectory(prefix="cjk-package-layout-")
atexit.register(_NAMESPACE_TEMP.cleanup)
_LAYOUT = Path(_NAMESPACE_TEMP.name)
for distribution in (SOURCE / "third_party/misaki_en", COMPANION):
    package = distribution / "misaki"
    if not package.is_dir():
        raise RuntimeError("The retained English/CJK source package is missing")
    for payload in package.rglob("*"):
        if not payload.is_file() or "__pycache__" in payload.parts:
            continue
        target = _LAYOUT / payload.relative_to(distribution)
        if target.exists():
            raise RuntimeError("The CJK companion overlaps retained English payloads")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(payload, target)
sys.path[:0] = [str(SOURCE), str(_LAYOUT), str(VENDOR), str(COMPANION)]

NATIVE_EVENTS = []


class RecordingSession:
    def __init__(self, path, providers):
        NATIVE_EVENTS.append(("session", path, tuple(providers)))
        self._model_path = path
        self.providers = providers

    def get_providers(self):
        return self.providers


class InertOrt(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "onnxruntime":
            return importlib.util.spec_from_loader(fullname, self)
        if fullname.split(".")[0] in {"torch", "spacy", "fugashi", "sounddevice", "phonemizer"}:
            raise AssertionError("Unexpected native/NLP/audio import: " + fullname)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        NATIVE_EVENTS.append(("inert-ort-import",))
        module.InferenceSession = RecordingSession
        module.get_available_providers = lambda: ["CPUExecutionProvider"]


sys.meta_path.insert(0, InertOrt())
for name, values in {
    "kokoro_onnx.log": {"log": logging.getLogger("inert-kokoro")},
    "kokoro_onnx.trim": {"trim": lambda audio: (audio, None)},
}.items():
    mod = types.ModuleType(name)
    mod.__dict__.update(values)
    sys.modules[name] = mod

from voice.customer_pronunciation import CustomerTokenizer  # noqa: E402 - establish inert native boundary first
from voice.customer_composition import ComposedCustomerTokenizer  # noqa: E402 - establish inert native boundary first
from viola_cjk.customer_english_span import CustomerEnglishBridge  # noqa: E402 - establish inert native boundary first
from viola_cjk.coverage import CoveredCJK  # noqa: E402 - establish inert native boundary first
from viola_cjk.locale_numbers import prepare_exact_number  # noqa: E402 - establish inert native boundary first
from kokoro_onnx import Kokoro  # noqa: E402 - establish inert native boundary first
from kokoro_onnx.tokenizer import Tokenizer  # noqa: E402 - establish inert native boundary first
import numpy as np  # noqa: E402 - establish inert native boundary first

VOCAB = json.loads((VENDOR / "kokoro_onnx/config.json").read_text(encoding="utf-8"))["vocab"]


def english():
    value = CustomerTokenizer.__new__(CustomerTokenizer)
    value.vocab = dict(VOCAB)
    value._lock = threading.Lock()
    calls = []

    def g2p(text):
        calls.append(text)
        phones = "and" if text == "and" else "həlˈO"
        return phones, [types.SimpleNamespace(text=text, phonemes=phones)]

    value._g2p = {"en-us": g2p, "en-gb": g2p}
    value.calls = calls
    return value


def composition():
    value = english()
    bridge = CustomerEnglishBridge(value, "en-gb")
    readings = []

    class Segmenter:
        def lcut(self, text, cut_all=False):
            return [text]

    class Chinese:
        @staticmethod
        def py2ipa(reading):
            readings.append(reading)
            return {"ni3": "ni↓", "hao3": "xau̯↓", "ling2": "liŋ↗", "yi1": "i→", "dian3": "tjɛn↓"}.get(reading, "a→")

    values = {"你": "ni3", "好": "hao3", "零": "ling2", "一": "yi1", "点": "dian3"}

    def pinyin(word, **kwargs):
        return [values.get(c, "a1") for c in word]

    def numbers(value, locale, unit=None):
        return prepare_exact_number(
            value, locale, unit=unit, integer_converter=lambda digits: "零" if digits == "0" else "一"
        )

    route = CoveredCJK(
        locale="zh",
        vocab=VOCAB,
        english=bridge,
        chinese=Chinese(),
        jieba=Segmenter(),
        pinyin=pinyin,
        pinyin_style="tone3",
        number_preparer=numbers,
    )
    return ComposedCustomerTokenizer(value, {"zh": route}), route, readings


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.component, self.route, self.readings = composition()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.model = Path(self.temporary.name) / "model.onnx"
        self.voices = Path(self.temporary.name) / "voices.bin"
        self.model.write_bytes(b"inert-model")
        self.voices.write_bytes(b"inert-voices")
        NATIVE_EVENTS.clear()

    def test_actual_english_and_mandarin_tokenization(self):
        tokenizer = Tokenizer(customer_tokenizer=self.component)
        self.assertIs(tokenizer._customer, self.component)
        self.assertEqual(tokenizer.phonemize("and", "en-gb"), "and")
        self.assertEqual(tokenizer.phonemize("你好", "zh"), "ni↓xau↓")
        self.assertEqual(tokenizer.tokenize("ni↓"), [VOCAB[c] for c in "ni↓"])
        self.assertEqual(self.readings, ["ni3", "hao3"])

    def test_real_constructor_and_from_session_forward_same_owner(self):
        with (
            patch("importlib.metadata.version", return_value="inert"),
            patch("numpy.load", return_value={"af_heart": object()}),
        ):
            one = Kokoro(str(self.model), str(self.voices), customer_tokenizer=self.component)
            two = Kokoro.from_session(one.sess, str(self.voices), customer_tokenizer=self.component)
        self.assertIs(one.tokenizer._customer, self.component)
        self.assertIs(two.tokenizer._customer, self.component)
        self.assertEqual(len([e for e in NATIVE_EVENTS if e[0] == "session"]), 1)
        self.assertEqual(two.tokenizer.phonemize("你好", "zh"), "ni↓xau↓")

    def test_wrong_owner_and_vocab_reject_before_session_or_voice_loading(self):
        for owner, config in [(object(), None), (self.component, {"vocab": {"a": 1}})]:
            with (
                self.subTest(owner=type(owner).__name__),
                patch("importlib.metadata.version", return_value="inert"),
                patch("numpy.load") as load,
            ):
                with self.assertRaises(ValueError):
                    Kokoro(str(self.model), str(self.voices), vocab_config=config, customer_tokenizer=owner)
                load.assert_not_called()
                self.assertFalse(NATIVE_EVENTS)

    def test_default_constructor_still_builds_english_once(self):
        calls = []

        def initialize(instance, vocab):
            calls.append(vocab)
            instance.vocab = vocab

        with (
            patch.object(CustomerTokenizer, "__init__", initialize),
            patch("importlib.metadata.version", return_value="inert"),
            patch("numpy.load", return_value={}),
        ):
            value = Kokoro(str(self.model), str(self.voices))
        self.assertIs(type(value.tokenizer._customer), CustomerTokenizer)
        self.assertEqual(len(calls), 1)

    def test_wrong_profile_and_removed_optout_fail_closed(self):
        with patch.dict(os.environ, {"VIOLA_KOKORO_PHONEMIZER": "espeak"}):
            with self.assertRaises(ValueError):
                Tokenizer(customer_tokenizer=self.component)
        with patch.dict(os.environ, {"ORT_DISABLE_TELEMETRY": "0"}):
            if sys.platform == "win32":
                # Preserve the inherited Windows exemption. This is not ETW
                # or Windows privacy qualification.
                self.assertIs(Tokenizer(customer_tokenizer=self.component)._customer, self.component)
            else:
                with self.assertRaises(RuntimeError):
                    Tokenizer(customer_tokenizer=self.component)
        self.assertFalse(NATIVE_EVENTS)

    def test_exact_model_batching_and_create_methods_use_bound_route(self):
        value = Kokoro.__new__(Kokoro)
        value.tokenizer = Tokenizer(customer_tokenizer=self.component)
        value.voices = {"af_heart": object()}
        calls = []

        def fake_audio(phones, voice, speed):
            calls.append((phones, voice, speed))
            return np.ones(5, dtype=np.float32), 24000

        value._create_audio = fake_audio
        audio, rate = value.create("你好", voice="af_heart", lang="zh", trim=False)
        self.assertEqual(calls[0][0], "ni↓xau↓")
        self.assertEqual(rate, 24000)
        self.assertEqual(len(audio), 5)
        self.assertEqual("".join(value._split_phonemes("a" * 1300)), "a" * 1300)
        self.assertTrue(all(len(x) <= 510 for x in value._split_phonemes("a" * 1300)))

    def test_dispatch_keeps_english_phrase_context_and_complete_spans(self):
        result = self.route.phonemize("你好 Hello and world 你好")
        self.assertEqual(self.component._english.calls, ["Hello and world"])
        self.assertEqual("".join(s["source"] for s in result["spans"]), result["source"])
        self.assertEqual(result["spans"][2]["kind"], "english")

    def test_mandarin_missing_or_raw_readings_reject_and_recover(self):
        original = self.route.pinyin
        for reading in (["Spotify"], ["ni3"], ["ni3", "bad"], ["ni3", "hao9"]):
            self.route.pinyin = lambda *a, **k: reading
            with self.subTest(reading=reading), self.assertRaises(ValueError):
                self.route.phonemize("你好")
        self.route.pinyin = original
        self.assertEqual(self.route.phonemize("你好")["phonemes"], "ni↓xau↓")
        self.assertNotIn("❓", self.route.vocab)
        convert = self.route.chinese.py2ipa
        try:
            # Only the established Mandarin U+032F stage may be normalized.
            for phones in ("", "̯", " ̯ ", "xau❓↓", "xau̯❓↓", "xaú↓", "xau̩↓"):
                self.route.chinese.py2ipa = lambda reading, value=phones: value
                with self.subTest(phones=phones), self.assertRaises(ValueError):
                    self.route.phonemize("好")
            self.route.chinese.py2ipa = lambda reading: "kʰa↓"
            self.assertEqual(self.route.phonemize("好")["phonemes"], "kʰa↓")
            with self.assertRaises(ValueError):
                self.route._phones("xau̯↓")
        finally:
            self.route.chinese.py2ipa = convert
        self.route.jieba.lcut = lambda *a, **k: ["你"]
        with self.assertRaises(ValueError):
            self.route.phonemize("你好")

    def test_decimals_keep_full_fraction_and_reject_fragmented_identifiers(self):
        result = self.route.phonemize("-.001 USD")
        trace = result["spans"][0]["trace"]
        self.assertEqual(trace["source_value"], "-.001")
        self.assertEqual(trace["unit"], "USD")
        self.assertEqual(trace["fraction_digit_names"], ["零", "零", "一"])
        for text in ["00.5", "1.123456789012345678901", "12:30", "v1.2", "1,234", "12USD", "1.2.3", "++1"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.route.phonemize(text)

    def test_factory_cannot_substitute_an_unverified_japanese_dictionary(self):
        from viola_cjk.japanese_factory import create_cutlet

        with self.assertRaisesRegex(ValueError, "files are incomplete"):
            create_cutlet(self.temporary.name)


# These helpers compile selected actual application bodies with only their
# external constructor/device boundaries supplied by the recording fixture.
def selected_namespace(relative, functions=(), classes=None, values=None):
    tree = ast.parse((SOURCE / relative).read_text(encoding="utf-8"))
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions:
            selected.append(node)
        elif isinstance(node, ast.ClassDef) and node.name in (classes or {}):
            node.body = [
                part
                for part in node.body
                if isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef)) and part.name in classes[node.name]
            ]
            node.bases = []
            selected.append(node)
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace = {
        "__name__": "recorded_application",
        "Path": Path,
        "threading": threading,
        "asyncio": asyncio,
        "logger": logging.getLogger("recording-app"),
        "logging": logging,
        "time": __import__("time"),
    }
    namespace.update(values or {})
    exec(compile(module, str(SOURCE / relative), "exec"), namespace)
    return namespace


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.component, self.route, self.readings = composition()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.model = Path(self.temporary.name) / "model"
        self.voices = Path(self.temporary.name) / "voices"
        self.model.write_bytes(b"inert")
        self.voices.write_bytes(b"inert")

    def test_actual_desktop_constructor_availability_load_and_output_locale(self):
        values = {
            "settings": types.SimpleNamespace(),
            "SAMPLE_RATE_24K": 24000,
            "_DEFAULT_SPEED": 1.0,
            "_DEFAULT_VOICE": "af_heart",
            "_cfg_bool": lambda cfg, key, default: default,
            "_cfg_float": lambda cfg, key, default, **kwargs: default,
            "_cfg_int": lambda cfg, key, default, **kwargs: default,
            "_KOKORO_PKG_PROBE_ERROR": "prior default failure",
        }
        ns = selected_namespace(
            "voice/synthesis/kokoro_engine.py",
            functions={"_probe_kokoro_package"},
            classes={"KokoroTTSEngine": {"__init__", "_ensure_loaded", "_synthesize_internal", "is_available"}},
            values=values,
        )
        engine = ns["KokoroTTSEngine"]
        engine._resolve_path = staticmethod(lambda explicit, cfg, **kw: Path(explicit))
        engine._normalize_voice_blend = staticmethod(lambda value: None)
        caches = []
        engine._create_opener_cache = lambda self, cfg: caches.append("default-cache") or object()
        engine._kick_off_opener_cache_build = lambda self: None
        instance = engine(self.model, self.voices, customer_tokenizer=self.component, language="ZH")
        self.assertIsNone(instance._opener_cache)
        self.assertEqual(caches, [])
        self.assertTrue(instance.is_available())
        self.assertEqual(ns["_KOKORO_PKG_PROBE_ERROR"], "prior default failure")
        with (
            patch("numpy.load", return_value={"af_heart": object()}),
            patch("importlib.metadata.version", return_value="inert"),
        ):
            self.assertTrue(instance._ensure_loaded())
        self.assertIs(instance._kokoro.tokenizer._customer, self.component)
        calls = []

        class StopBeforeAudio(Exception):
            pass

        def record_create(*args, **kwargs):
            calls.append((args, kwargs))
            raise StopBeforeAudio()

        instance._kokoro.create = record_create
        instance._resolve_voice_for_create = lambda voice: ("af_heart", "af_heart")
        instance._speed_with_jitter = lambda: 1.0
        with self.assertRaises(StopBeforeAudio):
            instance._synthesize_internal("你好", None)
        self.assertEqual(calls[0][1]["lang"], "zh")
        self.assertEqual(calls[0][1]["voice"], "af_heart")
        default = engine(self.model, self.voices)
        self.assertEqual(caches, ["default-cache"])
        self.assertFalse(default.is_available())
        with self.assertRaises(ValueError):
            engine(self.model, self.voices, language="zh")
        with self.assertRaises(ValueError):
            engine(self.model, self.voices, customer_tokenizer=self.component, language="it")

    def test_actual_desktop_speak_uses_existing_locale_normalizer(self):
        ns = selected_namespace("voice/synthesis/kokoro_engine.py", classes={"KokoroTTSEngine": {"speak"}})
        calls = []
        normalizer = types.ModuleType("voice.synthesis.text_normalizer")
        normalizer.normalize_for_speech = lambda text, **kwargs: calls.append((text, kwargs)) or ""
        for language, expected in [("en-us", {}), ("zh", {"language": "zh"})]:
            value = ns["KokoroTTSEngine"]()
            value._tts_is_enabled = lambda: True
            value._speech_language = language
            with patch.dict(sys.modules, {"voice.synthesis.text_normalizer": normalizer}):
                asyncio.run(value.speak("12.50 USD"))
            self.assertEqual(calls[-1], ("12.50 USD", expected))

    def _phone(self):
        events = []

        class Runtime:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        values = {
            "_phone_tts_runtime": None,
            "_phone_tts_preload_enabled": lambda: True,
            "_phone_tts_warm_key": None,
            "_phone_tts_provider_fallbacks": {},
            "_phone_tts_runtime_lock": threading.Lock(),
            "_phone_tts_provider_preference": lambda: "cuda",
            "_phone_kokoro_model_paths": lambda: (self.model, self.voices),
            "_require_phone_customer_startup": lambda: events.append("guard"),
            "configure_environment": lambda: events.append("configure"),
            "_PhoneKokoroTTSRuntime": Runtime,
            "_PHONE_TTS_CPU_EP": "CPUExecutionProvider",
            "_PHONE_TTS_CUDA_EP": "CUDAExecutionProvider",
            "_PHONE_TTS_PROVIDER_CPU": "cpu",
            "_PHONE_TTS_PROVIDER_CUDA": "cuda",
            "_phone_tts_provider_plans": lambda req, avail: (
                (("CUDAExecutionProvider", "CPUExecutionProvider"), ("CPUExecutionProvider",))
                if req == "cuda"
                else (("CPUExecutionProvider",),)
            ),
            "_warm_phone_kokoro_tts_runtime": lambda value, voice: 0.01,
        }
        ns = selected_namespace(
            "telephony/call_manager.py",
            functions={
                "_phone_tts_runtime_key",
                "_phone_kokoro_from_session",
                "_load_phone_kokoro_tts_runtime",
                "_ensure_phone_kokoro_tts_runtime",
                "preload_phone_tts",
            },
            classes={"CallManager": {"__init__", "_create_tts"}},
            values=values,
        )

        def create(path, providers):
            events.append(("session", providers))
            if providers[0] == "CUDAExecutionProvider":
                raise RuntimeError("controlled GPU failure")
            return RecordingSession(str(path), list(providers))

        ns["_create_phone_kokoro_session"] = create
        return ns, events

    def test_actual_phone_owner_cache_and_gpu_fallback_are_isolated(self):
        ns, events = self._phone()
        config = types.SimpleNamespace(tts_voice="af_heart")
        with patch("numpy.load", return_value={"af_heart": object()}):
            first = ns["_ensure_phone_kokoro_tts_runtime"](config, customer_tokenizer=self.component)
            same = ns["_ensure_phone_kokoro_tts_runtime"](config, customer_tokenizer=self.component)
            other, _, _ = composition()
            next_owner = ns["_ensure_phone_kokoro_tts_runtime"](config, customer_tokenizer=other)
        self.assertIs(first, same)
        self.assertIs(first.kokoro.tokenizer._customer, self.component)
        self.assertIs(next_owner.kokoro.tokenizer._customer, other)
        self.assertIsNot(first, next_owner)
        self.assertEqual(len(ns["_phone_tts_runtime_key"](config)), 3)
        self.assertEqual(ns["_phone_tts_warm_key"][-1], id(other))
        self.assertEqual(len([e for e in events if isinstance(e, tuple)]), 4)
        self.assertEqual({k[-1] for k in ns["_phone_tts_provider_fallbacks"]}, {id(self.component), id(other)})

    def test_public_phone_preload_rejects_invalid_admission_before_native_session(self):
        config = types.SimpleNamespace(tts_voice="af_heart", tts_provider="local")
        wrong_vocab, _, _ = composition()
        wrong_vocab.vocab = dict(VOCAB, a=-100)
        for profile, owner in [("misaki-en", object()), ("misaki-en", wrong_vocab), ("espeak", self.component)]:
            ns, events = self._phone()
            with (
                self.subTest(profile=profile, owner=type(owner).__name__),
                patch.dict(os.environ, VIOLA_KOKORO_PHONEMIZER=profile),
                patch("numpy.load") as voices,
            ):
                with self.assertRaises(ValueError):
                    ns["preload_phone_tts"](config, customer_tokenizer=owner, raise_on_failure=True)
                self.assertFalse(any(isinstance(event, tuple) and event[0] == "session" for event in events))
                voices.assert_not_called()
                self.assertIsNone(ns["_phone_tts_runtime"])
        ns, events = self._phone()
        with patch("numpy.load", return_value={"af_heart": object()}):
            self.assertTrue(ns["preload_phone_tts"](config, customer_tokenizer=self.component))
        self.assertIs(ns["_phone_tts_runtime"].kokoro.tokenizer._customer, self.component)

    def test_direct_phone_loader_rejects_invalid_owner_before_provider_attempts(self):
        wrong_vocab, _, _ = composition()
        wrong_vocab.vocab = dict(VOCAB, a=-100)
        for owner in [object(), wrong_vocab]:
            ns, events = self._phone()
            with self.subTest(owner=type(owner).__name__), patch("numpy.load") as voices:
                with self.assertRaises(ValueError):
                    ns["_load_phone_kokoro_tts_runtime"](
                        self.model, self.voices, voice="af_heart", requested_provider="cuda", customer_tokenizer=owner
                    )
                self.assertFalse(any(isinstance(event, tuple) for event in events))
                self.assertNotIn("configure", events)
                voices.assert_not_called()

    def test_phone_cached_owner_is_revalidated_before_return(self):
        ns, events = self._phone()
        config = types.SimpleNamespace(tts_voice="af_heart", tts_provider="local")
        with patch("numpy.load", return_value={"af_heart": object()}):
            self.assertTrue(ns["preload_phone_tts"](config, customer_tokenizer=self.component))
        runtime = ns["_phone_tts_runtime"]
        original_vocab = self.component.vocab
        self.component.vocab = dict(VOCAB, a=-100)
        events.clear()
        with patch("numpy.load") as voices:
            with self.assertRaises(ValueError):
                ns["preload_phone_tts"](config, customer_tokenizer=self.component, raise_on_failure=True)
            self.assertFalse(any(isinstance(event, tuple) for event in events))
            voices.assert_not_called()
        self.component.vocab = original_vocab
        self.assertTrue(ns["preload_phone_tts"](config, customer_tokenizer=self.component, raise_on_failure=True))
        self.assertIs(ns["_phone_tts_runtime"], runtime)

    def test_actual_phone_constructor_guard_precedes_queue_and_provider_effects(self):
        ns, events = self._phone()

        class StopAtGuard(Exception):
            pass

        def stop():
            events.append("stopped-before-queue")
            raise StopAtGuard()

        ns["_require_phone_customer_startup"] = stop
        ns["PhoneCallQueue"] = lambda: events.append("queue")

        class Config:
            tts_provider = "local"

            @property
            def is_configured(self):
                raise AssertionError("Configuration touched before startup guard")

        config = Config()
        with self.assertRaises(StopAtGuard):
            ns["CallManager"](config, customer_tokenizer=self.component)
        self.assertEqual(events, ["stopped-before-queue"])
        events.clear()
        config = types.SimpleNamespace(is_configured=False, tts_provider="local")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            ns["CallManager"](config)
        self.assertEqual(events, [])

    def test_actual_phone_tts_service_receives_same_runtime_owner_and_voice(self):
        ns, events = self._phone()
        services = types.ModuleType("pipecat.services.tts_service")
        services.TextAggregationMode = types.SimpleNamespace(SENTENCE="sentence")
        filters = types.ModuleType("telephony.tts_normalizer")
        bindings = []

        class Filter:
            def bind_tts(self, tts, call_id):
                bindings.append((tts, call_id))

        filters.SpeechTextFilter = Filter
        ns["_create_shared_phone_kokoro_tts_service"] = lambda **kwargs: types.SimpleNamespace(**kwargs)
        manager = ns["CallManager"].__new__(ns["CallManager"])
        manager.config = types.SimpleNamespace(tts_provider="local", tts_voice="af_heart")
        manager._customer_tokenizer = self.component
        with (
            patch.dict(sys.modules, {"pipecat.services.tts_service": services, "telephony.tts_normalizer": filters}),
            patch("numpy.load", return_value={"af_heart": object()}),
        ):
            service = manager._create_tts("synthetic-call")
        self.assertIs(service.kokoro.tokenizer._customer, self.component)
        self.assertEqual(service.voice_id, "af_heart")
        self.assertEqual(bindings, [(service, "synthetic-call")])


class CompanionTests(unittest.TestCase):
    def test_combined_physical_namespace_keeps_every_english_payload(self):
        import misaki

        self.assertTrue(Path(misaki.__file__).is_relative_to(_LAYOUT))
        original = SOURCE / "third_party/misaki_en"
        for payload in (original / "misaki").rglob("*"):
            if payload.is_file() and "__pycache__" not in payload.parts:
                self.assertEqual((_LAYOUT / payload.relative_to(original)).read_bytes(), payload.read_bytes())
        self.assertTrue((_LAYOUT / "misaki/data/ja_words.txt").is_file())

    def setUp(self):
        self.owner = english()
        self.bridge = CustomerEnglishBridge(self.owner, "en-gb")

    def test_bridge_preserves_identity_word_and_rejects_changed_origin(self):
        self.assertEqual(self.bridge.phonemize("and")["phonemes"], "and")
        with patch.object(CustomerTokenizer, "phonemize", lambda *a, **k: "and"):
            with self.assertRaises(ValueError):
                self.bridge.phonemize("and")
            with self.assertRaises(ValueError):
                CustomerEnglishBridge(self.owner)
        self.owner.vocab = dict(self.owner.vocab, a=-100)
        with self.assertRaises(ValueError):
            self.bridge.phonemize("and")
        with self.assertRaises(ValueError):
            CustomerEnglishBridge(object())

    def test_factory_pins_precede_any_missing_optional_import(self):
        from voice.customer_composition import compose_customer_tokenizer, _CJK_DEPENDENCIES, _COMPANION_VERSION

        versions = {"viola-misaki-cjk-prototype": _COMPANION_VERSION, **_CJK_DEPENDENCIES}
        for dependency in versions:
            wrong = dict(versions, **{dependency: "wrong"})
            with (
                self.subTest(dependency=dependency),
                patch("importlib.metadata.version", side_effect=lambda name: wrong[name]),
                self.assertRaises(RuntimeError),
            ):
                compose_customer_tokenizer(self.owner, mandarin=True)
        old_companion = dict(versions, **{"viola-misaki-cjk-prototype": "0.9.4+viola.cjk.2"})
        with (
            patch("importlib.metadata.version", side_effect=lambda name: old_companion[name]),
            self.assertRaises(RuntimeError),
        ):
            compose_customer_tokenizer(self.owner, mandarin=True)
        with self.assertRaises(ValueError):
            compose_customer_tokenizer(self.owner, mandarin="yes")
        with self.assertRaises(ValueError):
            compose_customer_tokenizer(object())

    def test_factory_never_creates_japanese_after_missing_mandarin_data(self):
        from voice.customer_composition import compose_customer_tokenizer, _CJK_DEPENDENCIES, _COMPANION_VERSION

        versions = {"viola-misaki-cjk-prototype": _COMPANION_VERSION, **_CJK_DEPENDENCIES}
        cn2an = types.ModuleType("cn2an")
        jieba = types.ModuleType("jieba")
        jieba.__file__ = "/definitely-missing/jieba/__init__.py"
        pypinyin = types.ModuleType("pypinyin")
        zh = types.ModuleType("misaki.zh")
        zh.ZHG2P = object
        with (
            patch.dict(sys.modules, {"cn2an": cn2an, "jieba": jieba, "pypinyin": pypinyin, "misaki.zh": zh}),
            patch("importlib.metadata.version", side_effect=lambda name: versions[name]),
            patch("viola_cjk.japanese_factory.create_cutlet") as create,
        ):
            with self.assertRaisesRegex(RuntimeError, "dictionary is missing"):
                compose_customer_tokenizer(self.owner, mandarin=True, japanese_dictionary_dir="/missing")
            create.assert_not_called()

    def test_actual_japanese_integer_rules_and_exact_fraction_preparation(self):
        from misaki.num2kana import do_convert, hiragana_dict

        prepared = prepare_exact_number(
            "-.005", "ja", unit="CNY", integer_converter=lambda digits: do_convert(digits, hiragana_dict)
        )
        self.assertEqual(prepared["source_value"], "-.005")
        self.assertEqual(prepared["fraction_digit_names"], ["ゼロ", "ゼロ", "ご"])
        self.assertEqual(prepared["spoken_tokens"], ["マイナス", "ゼロ", "てん", "ゼロ", "ゼロ", "ご", "じんみんげん"])
        self.assertEqual(
            prepare_exact_number("125", "ja", integer_converter=lambda digits: do_convert(digits, hiragana_dict))[
                "spoken_text"
            ],
            "ひゃく に じゅう ご",
        )
        for value in ["00.5", "1e3", "NaN", "+1", "1.234567890123456789012", "1000000000"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                prepare_exact_number(value, "ja", integer_converter=lambda digits: do_convert(digits, hiragana_dict))

    def _cutlet(self, reading="かな"):
        fugashi = types.ModuleType("fugashi")
        fugashi.Tagger = lambda *a, **k: (_ for _ in ()).throw(AssertionError("No native tagger"))
        jaconv = types.ModuleType("jaconv")
        jaconv.kata2hira = lambda text: text
        mojimoji = types.ModuleType("mojimoji")
        mojimoji.zen_to_han = lambda text, **kw: text
        mojimoji.han_to_zen = lambda text, **kw: text
        with patch.dict(sys.modules, {"fugashi": fugashi, "jaconv": jaconv, "mojimoji": mojimoji}):
            from misaki.cutlet import Cutlet, HEPBURN
        value = Cutlet.__new__(Cutlet)
        value.table = dict(HEPBURN)
        value.exceptions = {}
        value.tagger = lambda text: [
            types.SimpleNamespace(
                surface=text, feature=types.SimpleNamespace(pron=reading, kana=reading), char_type=7, is_unk=False
            )
        ]
        return value

    def test_actual_cutlet_algorithm_records_consumption_and_restores_class_method(self):
        cutlet = self._cutlet()
        self.assertNotIn("_romaji_word", vars(cutlet))
        route = CoveredCJK(locale="ja", vocab=VOCAB, english=self.bridge, cutlet=cutlet)
        result = route.phonemize("かな")
        self.assertEqual(result["phonemes"], "kana")
        self.assertEqual(result["spans"][0]["trace"][0]["source"], "かな")
        self.assertNotIn("_romaji_word", vars(cutlet))

        def old(word):
            return "kana"

        cutlet._romaji_word = old
        self.assertEqual(route.phonemize("かな")["phonemes"], "kana")
        self.assertIs(cutlet._romaji_word, old)

    def test_japanese_incomplete_consumption_and_dropped_readings_reject(self):
        for reading in ["漢字", "ゃ", "ひゃゅ", "゙", "ー", "かな🙂"]:
            with self.subTest(reading=reading):
                cutlet = self._cutlet(reading)
                route = CoveredCJK(locale="ja", vocab=VOCAB, english=self.bridge, cutlet=cutlet)
                with self.assertRaises(ValueError):
                    route.phonemize("かな")
                self.assertNotIn("_romaji_word", vars(cutlet))
        cutlet = self._cutlet()
        cutlet.tagger = lambda text: [
            types.SimpleNamespace(
                surface="か", feature=types.SimpleNamespace(pron="か", kana="か"), char_type=7, is_unk=False
            )
        ]
        with self.assertRaises(ValueError):
            CoveredCJK(locale="ja", vocab=VOCAB, english=self.bridge, cutlet=cutlet).phonemize("かな")
        self.assertNotIn("_romaji_word", vars(cutlet))

    def test_numeric_japanese_tokens_are_not_joined_for_retokenization(self):
        cutlet = self._cutlet("かな")
        calls = []
        old = cutlet.tagger
        cutlet.tagger = lambda text: calls.append(text) or old(text)

        def numbers(value, locale, unit=None):
            return prepare_exact_number(value, locale, unit=unit, integer_converter=lambda value: "ゼロ")

        route = CoveredCJK(locale="ja", vocab=VOCAB, english=self.bridge, cutlet=cutlet, number_preparer=numbers)
        result = route.phonemize("-.05 CNY")
        self.assertEqual(calls, ["マイナス", "ゼロ", "てん", "ゼロ", "ご", "じんみんげん"])
        self.assertEqual(result["spans"][0]["trace"]["source_value"], "-.05")
        self.assertEqual(result["spans"][0]["source"], "-.05 CNY")


class DictionaryFactoryTests(unittest.TestCase):
    def setUp(self):
        import hashlib
        from viola_cjk import japanese_factory as factory

        self.factory = factory
        self.temporary = tempfile.TemporaryDirectory(prefix="CJK 'dictionary' 日本 ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.a = self.root / "dictionary-a"
        self.b = self.root / "dictionary-b"
        real = factory._manifest()
        self.manifest = dict(real, files=[])
        for index, row in enumerate(real["files"]):
            raw = ("synthetic " + row["path"]).encode()
            for root in [self.a, self.b]:
                path = root / row["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw if root == self.a else raw + b" different")
            self.manifest["files"].append(
                {"path": row["path"], "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            )
        self.events = []
        expected = self.manifest["system_dictionary"]

        class InertCutlet:
            def __init__(self):
                raise AssertionError("Default Cutlet constructor must not run")

        self.cutlet_module = types.ModuleType("misaki.cutlet")
        self.cutlet_module.Cutlet = InertCutlet
        self.cutlet_module.HEPBURN = {"か": "ka"}
        self.fugashi_module = types.ModuleType("fugashi")

        def generic(arguments, wrapper):
            import shlex

            parsed = shlex.split(arguments)
            self.events.append((parsed, wrapper))
            return types.SimpleNamespace(
                dictionary_info=[
                    {
                        "filename": str(Path(parsed[3]) / "sys.dic"),
                        "charset": "UTF-8",
                        "version": expected["version"],
                        "size": expected["lexsize"],
                    }
                ]
            )

        self.fugashi_module.GenericTagger = generic

    def build(self, path):
        with (
            patch.object(self.factory, "_manifest", return_value=self.manifest),
            patch("importlib.metadata.version", return_value="1.5.2"),
            patch.dict(sys.modules, {"fugashi": self.fugashi_module, "misaki.cutlet": self.cutlet_module}),
        ):
            return self.factory.create_cutlet(path)

    def test_explicit_paths_fields_and_native_selection_are_bound(self):
        result = self.build(self.a)
        arguments, wrapper = self.events[0]
        self.assertEqual(arguments[0::2], ["-r", "-d"])
        self.assertEqual(arguments[3], str(self.a.resolve()))
        self.assertEqual(arguments[1], str(Path(self.factory.__file__).with_name("mecabrc").resolve()))
        self.assertEqual(wrapper(*map(str, range(29))).pron, "9")
        self.assertEqual(wrapper(*map(str, range(29))).kana, "20")
        unknown = wrapper(*map(str, range(6)))
        self.assertIsNone(unknown.pron)
        self.assertIsNone(unknown.kana)
        self.assertEqual(result.table, {"か": "ka"})
        self.assertEqual(result.exceptions, {})

    def test_movable_root_alias_cannot_change_verified_dictionary(self):
        alias = self.root / "alias"
        alias.symlink_to(self.a, target_is_directory=True)
        original = self.fugashi_module.GenericTagger

        def swap(arguments, wrapper):
            alias.unlink()
            alias.symlink_to(self.b, target_is_directory=True)
            return original(arguments, wrapper)

        self.fugashi_module.GenericTagger = swap
        try:
            result = self.build(alias)
        except ValueError as exc:
            self.fail("Verified canonical root did not survive an alias move: " + str(exc))
        self.assertEqual(self.events[0][0][3], str(self.a.resolve()))
        self.assertEqual(Path(result.tagger.dictionary_info[0]["filename"]).parent, self.a.resolve())
        self.assertEqual(alias.resolve(), self.b.resolve())

    def test_every_missing_or_changed_payload_precedes_native_construction(self):
        for row in self.manifest["files"]:
            path = self.a / row["path"]
            raw = path.read_bytes()
            try:
                path.unlink()
                with self.subTest(path=row["path"], case="missing"), self.assertRaises(ValueError):
                    self.build(self.a)
                path.write_bytes(raw + b"x")
                with self.subTest(path=row["path"], case="size"), self.assertRaises(ValueError):
                    self.build(self.a)
                path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
                with self.subTest(path=row["path"], case="digest"), self.assertRaises(ValueError):
                    self.build(self.a)
            finally:
                path.write_bytes(raw)
        self.assertEqual(self.events, [])

    def test_wrong_native_metadata_or_user_dictionary_rejects(self):
        base = {
            "filename": str(self.a / "sys.dic"),
            "charset": "utf8",
            **{
                "version": self.manifest["system_dictionary"]["version"],
                "size": self.manifest["system_dictionary"]["lexsize"],
            },
        }
        cases = [
            [dict(base, filename=str(self.b / "sys.dic"))],
            [dict(base, charset="shift-jis")],
            [dict(base, version=0)],
            [dict(base, size=1)],
            [base, dict(base)],
            [],
        ]
        for info in cases:
            self.fugashi_module.GenericTagger = lambda *a, **k: types.SimpleNamespace(dictionary_info=info)
            with self.subTest(info=info), self.assertRaises(ValueError):
                self.build(self.a)

    def test_manifest_or_explicit_rc_change_rejects_before_import(self):
        file = Path(self.factory.__file__)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "unidic.json").write_text("{}")
            with (
                patch.object(self.factory, "__file__", str(root / "japanese_factory.py")),
                self.assertRaisesRegex(ValueError, "manifest differs"),
            ):
                self.factory.validate_dictionary(self.a)
            (root / "unidic.json").write_bytes(file.with_name("unidic.json").read_bytes())
            (root / "mecabrc").write_text("userdic = unexpected")
            with (
                patch.object(self.factory, "__file__", str(root / "japanese_factory.py")),
                self.assertRaisesRegex(ValueError, "configuration differs"),
            ):
                self.build(self.a)
        self.assertEqual(self.events, [])

    def test_repeated_word_conversion_does_not_rehash_dictionary(self):
        seed = CompanionTests._cutlet(self, "かな")
        self.cutlet_module.Cutlet = type(seed)
        self.cutlet_module.HEPBURN = dict(seed.table)
        original = self.fugashi_module.GenericTagger
        count = []

        def generic(arguments, wrapper):
            metadata = original(arguments, wrapper).dictionary_info

            class Tagger:
                dictionary_info = metadata

                def __call__(self, text):
                    count.append(text)
                    features = [None] * 29
                    features[9] = features[20] = "かな"
                    return [types.SimpleNamespace(surface=text, feature=wrapper(*features), char_type=7, is_unk=False)]

            return Tagger()

        self.fugashi_module.GenericTagger = generic
        with patch.object(self.factory, "validate_dictionary", wraps=self.factory.validate_dictionary) as validate:
            cutlet = self.build(self.a)
            route = CoveredCJK(locale="ja", vocab=VOCAB, english=CustomerEnglishBridge(english()), cutlet=cutlet)
            for _ in range(5):
                self.assertEqual(route.phonemize("かな")["phonemes"], "kana")
            self.assertEqual(validate.call_count, 1)
            self.assertEqual(count, ["かな"] * 5)

    def test_dictionary_hash_reads_are_bounded_streams(self):
        original_open = Path.open
        reads = []
        case = self

        class Reader:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def read(self, size=-1):
                case.assertGreater(size, 0, "Dictionary payload must not be read whole")
                case.assertLessEqual(size, 4 * 1024 * 1024)
                reads.append(size)
                return self.stream.read(size)

        def check(dictionary_dir):
            canonical_root = dictionary_dir.resolve(strict=True)
            opened = set()

            def tracked(path, *args, **kwargs):
                stream = original_open(path, *args, **kwargs)
                if path.is_relative_to(canonical_root):
                    opened.add(path.relative_to(canonical_root).as_posix())
                    return Reader(stream)
                return stream

            reads.clear()
            with patch.object(Path, "open", tracked):
                self.build(dictionary_dir)
            self.assertGreaterEqual(len(reads), 18)
            self.assertEqual(opened, {row["path"] for row in self.manifest["files"]})

        alias = self.root / "bounded-read-alias"
        alias.symlink_to(self.a, target_is_directory=True)
        for label, directory in (("original", self.a), ("alias", alias)):
            with self.subTest(directory=label):
                check(directory)


class SharedCutletTests(unittest.TestCase):
    def test_two_routes_share_one_observation_lock_and_restore_class_attribute(self):
        from concurrent.futures import ThreadPoolExecutor

        seed = CompanionTests._cutlet(self, "かな")
        first = threading.Event()
        release = threading.Event()
        attempted = threading.Event()
        second = threading.Event()

        def tagger(text):
            if text == "かな":
                first.set()
                if not release.wait(2):
                    raise AssertionError("Fixture owner was not released")
            else:
                second.set()
            return [
                types.SimpleNamespace(
                    surface=text, feature=types.SimpleNamespace(pron=text, kana=text), char_type=7, is_unk=False
                )
            ]

        seed.tagger = tagger
        owner = english()
        bridge = CustomerEnglishBridge(owner)
        left = CoveredCJK(locale="ja", vocab=VOCAB, english=bridge, cutlet=seed)
        right = CoveredCJK(locale="ja", vocab=VOCAB, english=bridge, cutlet=seed)

        def run_second():
            attempted.set()
            return right.phonemize("なか")

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(left.phonemize, "かな")
            self.assertTrue(first.wait(1))
            b = pool.submit(run_second)
            self.assertTrue(attempted.wait(1))
            try:
                self.assertFalse(second.wait(0.05))
            finally:
                release.set()
            self.assertEqual(a.result(timeout=2)["phonemes"], "kana")
            self.assertEqual(b.result(timeout=2)["phonemes"], "naka")
        self.assertNotIn("_romaji_word", vars(seed))


if __name__ == "__main__":
    unittest.main()
