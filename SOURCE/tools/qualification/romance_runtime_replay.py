"""Explicit Romance application composition with an inert inference boundary."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.abc
import importlib.util
import json
import logging
import os
import sys
import tempfile
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2]
VENDOR = SOURCE / "third_party/kokoro_onnx/src"
sys.path[:0] = [str(SOURCE), str(VENDOR)]
EVENTS = []


class RecordingSession:
    def __init__(self, path, providers):
        EVENTS.append(("session", path, tuple(providers)))
        self._model_path = path


class InertRuntime(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "onnxruntime":
            return importlib.util.spec_from_loader(fullname, self)
        if fullname.split(".")[0] in {
            "torch",
            "spacy",
            "en_core_web_sm",
            "fugashi",
            "sounddevice",
            "phonemizer",
            "misaki",
            "viola_cjk",
        }:
            raise AssertionError("Unrequested native/NLP/CJK/frontend import: " + fullname)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        EVENTS.append(("inert-ort-import",))
        module.InferenceSession = RecordingSession


sys.meta_path.insert(0, InertRuntime())
for name, values in {
    "kokoro_onnx.log": {"log": logging.getLogger("inert-romance")},
    "kokoro_onnx.trim": {"trim": lambda audio: (audio, None)},
    "core.logging_config": {"get_logger": logging.getLogger},
}.items():
    module = types.ModuleType(name)
    module.__dict__.update(values)
    sys.modules[name] = module

from voice.customer_composition import ComposedCustomerTokenizer, compose_customer_tokenizer
from voice.customer_pronunciation import CustomerTokenizer, PronunciationError
from voice.customer_romance.coverage import CoverageError, CoveredRomance

INITIAL_VENDOR_MODULES = [name for name in sys.modules if name.startswith("voice.customer_romance._romance_vendor")]

import numpy as np
from kokoro_onnx import Kokoro
from kokoro_onnx.tokenizer import Tokenizer

from voice.synthesis.text_normalizer import SpeechFormatter

VOCAB = json.loads((VENDOR / "kokoro_onnx/config.json").read_text(encoding="utf-8"))["vocab"]
LOCALES = ("es", "fr", "pt-br")
CASES = (
    ("es", "pero", "ef_dora"),
    ("es", "perro", "ef_dora"),
    ("fr", "tu", "ff_siwis"),
    ("fr", "tout", "ff_siwis"),
    ("pt-br", "bom dia", "pf_dora"),
    ("pt-br", "noite", "pf_dora"),
)


def english():
    value = CustomerTokenizer.__new__(CustomerTokenizer)
    value.vocab = dict(VOCAB)
    value._lock = threading.Lock()
    value.calls = []

    def g2p(text):
        value.calls.append(text)
        return "həlˈO", [types.SimpleNamespace(text=text, phonemes="həlˈO")]

    value._g2p = {"en-us": g2p, "en-gb": g2p}
    return value


async def collect_stream(engine, text, **kwargs):
    return [chunk async for chunk in engine.create_stream(text, **kwargs)]


class RomanceCompositionTests(unittest.TestCase):
    def setUp(self):
        self.owner = english()
        self.component = compose_customer_tokenizer(self.owner, romance_locales=LOCALES)
        self.calls = []
        self.engine = Kokoro.__new__(Kokoro)
        self.engine.tokenizer = Tokenizer(customer_tokenizer=self.component)
        self.engine.voices = {voice: object() for voice in ("af_heart", "ef_dora", "ff_siwis", "pf_dora")}

        def audio(phones, voice, speed):
            tokens = self.engine.tokenizer.tokenize(phones)
            self.calls.append((phones, tokens, voice, speed))
            return np.ones(3, dtype=np.float32), 24000

        self.engine._create_audio = audio
        EVENTS.clear()

    def test_import_and_construction_are_inactive_and_need_no_cjk(self):
        self.assertEqual(INITIAL_VENDOR_MODULES, [])
        before = dict(os.environ)
        with patch("importlib.metadata.version", side_effect=AssertionError("Unrequested distribution lookup")):
            empty = compose_customer_tokenizer(self.owner)
            component = compose_customer_tokenizer(self.owner, romance_locales=("es",))
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(set(empty._routes), set())
        self.assertEqual(set(component._routes), {"es"})
        self.assertNotIn("viola_cjk", sys.modules)
        self.assertNotIn("misaki", sys.modules)
        self.assertNotIn("_romance_vendor", sys.modules)
        self.assertEqual(EVENTS, [])

    def test_only_exact_requested_locales_are_available(self):
        for selected in ((), ("es",), ("fr",), ("pt-br",), LOCALES):
            component = compose_customer_tokenizer(self.owner, romance_locales=selected)
            for locale in (*LOCALES, "en-us", "en-gb", "pt", "pt-pt", "es-mx", "it", "hi", "ja", "zh"):
                self.assertEqual(component.supports_locale(locale), locale in (*selected, "en-us", "en-gb"))
            for invalid in (None, True, {}, " pt-br", "pt-br ", "javascript"):
                self.assertFalse(component.supports_locale(invalid))
        self.assertTrue(self.component.supports_locale("PT_BR"))
        self.assertEqual(self.component.phonemize("bom dia", "PT_BR"), self.component.phonemize("bom dia", "pt-br"))

    def test_invalid_factory_requests_reject_without_frontend_construction(self):
        for locales in ("es", ["es"], {"es"}, ("es", "es"), ("pt",), ("pt-pt",), ("ES",), ("it",), (None,)):
            with self.subTest(locales=locales), patch.object(CoveredRomance, "__init__") as initialize:
                with self.assertRaises(ValueError):
                    compose_customer_tokenizer(self.owner, romance_locales=locales)
                initialize.assert_not_called()
        with self.assertRaises(ValueError):
            compose_customer_tokenizer(object(), romance_locales=("es",))
        with self.assertRaises(ValueError):
            compose_customer_tokenizer(self.owner, mandarin="yes", romance_locales=("es",))

    def test_exact_route_type_locale_vocabulary_and_owner_admission(self):
        class Subclass(CoveredRomance):
            pass

        class EnglishSubclass(CustomerTokenizer):
            pass

        for route in (
            object(),
            Subclass("es", VOCAB),
            CoveredRomance("fr", VOCAB),
            CoveredRomance("es", dict(VOCAB, a=-123)),
        ):
            with self.subTest(route=type(route).__name__), self.assertRaises(ValueError):
                ComposedCustomerTokenizer(self.owner, {"es": route})
        with self.assertRaises(ValueError):
            ComposedCustomerTokenizer(EnglishSubclass.__new__(EnglishSubclass), {})
        with self.assertRaises(ValueError):
            ComposedCustomerTokenizer(self.owner, {"pt-pt": CoveredRomance("pt-pt", VOCAB)})
        with self.assertRaises(TypeError):
            self.component._routes["es"] = object()
        self.assertIs(self.component._english, self.owner)

    def test_english_dispatch_and_tokenization_remain_the_existing_methods(self):
        for locale in ("en-us", "EN_GB"):
            self.assertEqual(self.component.phonemize("Hello", locale), self.owner.phonemize("Hello", locale))
        self.assertEqual(self.component.tokenize("həlˈO"), self.owner.tokenize("həlˈO"))
        with self.assertRaises(PronunciationError):
            self.component.tokenize("a❓")

    def test_original_text_create_and_stream_reach_same_semantic_phones_and_voice(self):
        outputs = {}
        for locale, text, voice in CASES:
            expected = self.component._routes[locale].phonemize(text)["phonemes"]
            outputs[text] = expected
            for streaming in (False, True):
                self.calls.clear()
                with self.subTest(locale=locale, text=text, streaming=streaming):
                    if streaming:
                        result = asyncio.run(collect_stream(self.engine, text, voice=voice, lang=locale, trim=False))
                        self.assertEqual(len(result), 1)
                    else:
                        result = self.engine.create(text, voice=voice, lang=locale, trim=False)
                        self.assertEqual(result[1], 24000)
                    self.assertEqual(
                        self.calls, [(expected, [VOCAB[p] for p in expected], self.engine.voices[voice], 1.0)]
                    )
        self.assertNotEqual(outputs["pero"], outputs["perro"])
        self.assertNotEqual(outputs["tu"], outputs["tout"])
        self.assertEqual(self.owner.calls, [])

    def test_whole_phrase_french_context_and_original_elision_spans_survive(self):
        route = self.component._routes["fr"]
        for text in ("les amis", "les, amis", "l’ami", "l'ami"):
            with self.subTest(text=text), patch.object(route, "_call", wraps=route._call) as frontend:
                self.component.phonemize(text, "fr", norm=False)
                frontend.assert_called_once_with(text.replace("’", "'"))
            trace = route.phonemize(text)
            self.assertEqual("".join(span["source"] for span in trace["source_spans"]), text)
        self.assertIn("z", route.phonemize("les amis")["upstream_tokens"])
        self.assertNotIn("z", route.phonemize("les, amis")["upstream_tokens"])
        self.assertEqual(self.component.phonemize("l’ami", "fr"), self.component.phonemize("l'ami", "fr"))

    def test_normalizer_preserves_unsupported_values_for_whole_input_rejection(self):
        formatter = SpeechFormatter(summarize=False, config=types.SimpleNamespace())
        voices = {locale: voice for locale, _, voice in CASES}
        for locale in LOCALES:
            for text in ("12.50", "hola €12.50", "hola $25", "hello 世界", "hello नमस्ते"):
                normalized = formatter.format(text, language=locale)
                self.assertEqual(normalized, text)
                for streaming in (False, True):
                    with self.subTest(locale=locale, text=text, streaming=streaming):
                        with self.assertRaises(PronunciationError) as error:
                            if streaming:
                                asyncio.run(collect_stream(self.engine, normalized, voice=voices[locale], lang=locale))
                            else:
                                self.engine.create(normalized, voice=voices[locale], lang=locale)
                        self.assertNotIn(text, str(error.exception))
                        self.assertIsNone(error.exception.__cause__)
        self.assertEqual(self.calls, [])

    def test_unsupported_words_controls_and_locales_never_reach_inference(self):
        voices = {locale: voice for locale, _, voice in CASES}
        for locale, text in (
            ("fr", "ami qa"),
            ("pt-br", "piñata"),
            ("es", "hola\nworld"),
            ("es", "hola\x00world"),
            ("pt", "bom dia"),
            ("pt-pt", "bom dia"),
            ("it", "pèsca"),
            ("hi", "किताब"),
            ("es", "a" * 5001),
        ):
            with self.subTest(locale=locale, text=text[:20]), self.assertRaises(PronunciationError):
                if locale in voices:
                    self.engine.create(text, voice=voices[locale], lang=locale)
                else:
                    # Exercise unsupported pronunciation capability directly;
                    # named-voice admission has separate earlier-boundary tests.
                    self.component.phonemize(text, lang=locale)
        self.assertEqual(self.calls, [])

    def test_empty_unknown_and_nonstring_component_output_rejects(self):
        for phones in ("", "a❓", None, ["a"]):
            with (
                self.subTest(phones=phones),
                patch.object(CoveredRomance, "phonemize", return_value={"phonemes": phones}),
                self.assertRaises(PronunciationError),
            ):
                self.engine.create("hola", voice="ef_dora", lang="es")
        with patch.object(CoveredRomance, "phonemize", side_effect=CoverageError("private utterance")):
            with self.assertRaises(PronunciationError) as caught:
                self.component.phonemize("hola", "es")
            self.assertNotIn("private utterance", str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_long_original_text_keeps_all_phones_across_create_and_stream_batches(self):
        text = " ".join(["hola"] * 180)
        expected = self.component.phonemize(text, "es")
        self.assertGreater(len(expected), 510)
        for streaming in (False, True):
            self.calls.clear()
            if streaming:
                asyncio.run(collect_stream(self.engine, text, voice="ef_dora", lang="es", trim=False))
            else:
                self.engine.create(text, voice="ef_dora", lang="es", trim=False)
            self.assertGreater(len(self.calls), 1)
            self.assertEqual("".join(row[0] for row in self.calls), expected)
            self.assertEqual([token for row in self.calls for token in row[1]], [VOCAB[p] for p in expected])
            self.assertTrue(
                all(0 < len(row[0]) <= 510 and row[2] is self.engine.voices["ef_dora"] for row in self.calls)
            )

    def test_constructor_and_from_session_preserve_owner_before_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            model, voices = Path(directory) / "model", Path(directory) / "voices"
            model.write_bytes(b"inert")
            voices.write_bytes(b"inert")
            with patch("importlib.metadata.version", return_value="inert"), patch("numpy.load", return_value={}):
                one = Kokoro(str(model), str(voices), customer_tokenizer=self.component)
                two = Kokoro.from_session(one.sess, str(voices), customer_tokenizer=self.component)
            self.assertIs(one.tokenizer._customer, self.component)
            self.assertIs(two.tokenizer._customer, self.component)
            self.assertEqual(len(EVENTS), 1)
            EVENTS.clear()
            with patch("importlib.metadata.version", return_value="inert"), patch("numpy.load") as load:
                with self.assertRaises(ValueError):
                    Kokoro(str(model), str(voices), customer_tokenizer=self.component, vocab_config={"vocab": {"a": 1}})
                load.assert_not_called()
            self.assertEqual(EVENTS, [])

    def test_profile_is_still_required_and_never_activated_by_construction(self):
        with patch.dict(os.environ, {"VIOLA_KOKORO_PHONEMIZER": "espeak"}):
            component = compose_customer_tokenizer(self.owner, romance_locales=LOCALES)
            self.assertEqual(os.environ["VIOLA_KOKORO_PHONEMIZER"], "espeak")
            with self.assertRaises(ValueError):
                Tokenizer(customer_tokenizer=component)

    def test_parallel_instances_keep_requested_routes_and_dialects_isolated(self):
        components = {locale: compose_customer_tokenizer(english(), romance_locales=(locale,)) for locale in LOCALES}
        inputs = [(locale, text) for locale, text, _ in CASES] * 8
        expected = [components[locale].phonemize(text, locale) for locale, text in inputs]
        with ThreadPoolExecutor(max_workers=4) as pool:
            actual = list(pool.map(lambda row: components[row[0]].phonemize(row[1], row[0]), inputs))
        self.assertEqual(actual, expected)
        for locale, component in components.items():
            self.assertEqual(set(component._routes), {locale})


class AdoptionProvenanceTests(unittest.TestCase):
    def test_adopted_files_and_immutable_packet_keep_exact_identity(self):
        package = SOURCE / "voice/customer_romance"
        packet = SOURCE / "qualification/romance_semantic_lowering"
        manifest = json.loads((package / "provenance.json").read_text(encoding="utf-8"))
        expected = set()
        for row in manifest["files"]:
            expected.add(row["path"])
            source = (SOURCE / row["source_path"]).read_bytes()
            adopted = (package / row["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(source).hexdigest(), row["source_sha256"])
            self.assertEqual(hashlib.sha256(adopted).hexdigest(), row["sha256"])
            if row["path"] == "coverage.py":
                source = source.replace(
                    b"from _romance_vendor.base import ProsodyInfo", b"from ._romance_vendor.base import ProsodyInfo"
                )
                source = source.replace(
                    b'importlib.import_module("_romance_vendor." + name)',
                    b'importlib.import_module("._romance_vendor." + name, __package__)',
                )
                source = source.replace(
                    b'importlib.import_module("_romance_vendor.base")',
                    b'importlib.import_module("._romance_vendor.base", __package__)',
                )
            self.assertEqual(adopted, source)
            if "upstream" in row:
                original = (packet / "upstream" / Path(row["upstream"]["path"]).name).read_bytes()
                self.assertEqual(hashlib.sha256(original).hexdigest(), row["upstream"]["sha256"])
                self.assertEqual(
                    hashlib.sha1(
                        b"blob " + str(len(original)).encode() + b"\0" + original, usedforsecurity=False
                    ).hexdigest(),
                    row["upstream"]["git_blob"],
                )
        actual = {
            path.relative_to(package).as_posix()
            for path in package.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        self.assertEqual(actual, expected | {"__init__.py", "README.md", "provenance.json"})
        integrity = json.loads((packet / "integrity.json").read_text(encoding="utf-8"))
        for row in integrity["payloads"]:
            self.assertEqual(hashlib.sha256((packet / row["path"]).read_bytes()).hexdigest(), row["sha256"])

    def test_missing_or_modified_frontend_fails_before_import(self):
        target = SOURCE / "voice/customer_romance/_romance_vendor/spanish.py"
        read_bytes = Path.read_bytes

        def changed(path):
            return b"unreviewed" if path == target else read_bytes(path)

        with patch.object(Path, "read_bytes", changed), self.assertRaisesRegex(CoverageError, "identity differs"):
            CoveredRomance("es", VOCAB)

        def missing(path):
            if path == target:
                raise FileNotFoundError(path)
            return read_bytes(path)

        with patch.object(Path, "read_bytes", missing), self.assertRaises(FileNotFoundError):
            CoveredRomance("es", VOCAB)


if __name__ == "__main__":
    unittest.main()
