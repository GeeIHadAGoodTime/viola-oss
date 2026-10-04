"""Real vendored Kokoro batching/control paths with a recording model boundary.

No native phonemizer or model is required by the public source test profile.
The actual tokenizer vocabulary/filtering and wrapper methods run unchanged.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import random
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[2]


class KokoroChunkingTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        name = "_kokoro_chunking_contract"
        package = ROOT / "third_party/kokoro_onnx/src/kokoro_onnx"
        native = types.ModuleType("onnxruntime")
        native.InferenceSession = object
        phonemizer = types.ModuleType("phonemizer")
        phonemizer.phonemize = Mock(side_effect=AssertionError("No native pronunciation in source contracts"))
        wrapper = types.ModuleType("phonemizer.backend.espeak.wrapper")
        wrapper.EspeakWrapper = object
        log = types.ModuleType(name + ".log")
        log.log = logging.getLogger(name)
        spec = importlib.util.spec_from_file_location(
            name, package / "__init__.py", submodule_search_locations=[str(package)]
        )
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(
            sys.modules,
            {
                "onnxruntime": native,
                "phonemizer": phonemizer,
                "phonemizer.backend.espeak.wrapper": wrapper,
                name: cls.module,
                name + ".log": log,
            },
        ):
            spec.loader.exec_module(cls.module)
            cls.tokenizer_class = cls.module.Tokenizer
            cls.config = sys.modules[name + ".config"]
        # Keep references without leaking any synthetic import into other tests.
        for imported in tuple(sys.modules):
            if imported == name or imported.startswith(name + "."):
                sys.modules.pop(imported)
        cls.phonemizer = phonemizer

    def make_model(self, modern=False):
        class RecordingSession:
            def __init__(self):
                self.calls = []

            def get_inputs(self):
                return [types.SimpleNamespace(name="input_ids" if modern else "tokens")]

            def run(self, outputs, inputs):
                self.calls.append({key: np.asarray(value).copy() for key, value in inputs.items()})
                return [np.full(24, len(self.calls), dtype=np.float32)]

        model = self.module.Kokoro.__new__(self.module.Kokoro)
        model.tokenizer = self.tokenizer_class.__new__(self.tokenizer_class)
        model.tokenizer.vocab = self.config.DEFAULT_VOCAB
        model.sess = RecordingSession()
        model.voices = {"fixture": np.arange(511, dtype=np.float32).reshape(511, 1, 1)}
        return model

    def assert_lossless(self, model, phones):
        parts = model._split_phonemes(phones)
        self.assertEqual("".join(parts), phones)
        self.assertTrue(all(0 < len(part) <= self.config.MAX_PHONEME_LENGTH for part in parts))
        return parts

    def assert_model_inputs(self, model, phones, speed=1.0):
        observed = []
        parts = self.assert_lossless(model, phones)
        self.assertEqual(len(model.sess.calls), len(parts))
        for call, part in zip(model.sess.calls, parts):
            tokens = call.get("tokens", call.get("input_ids"))[0].tolist()
            expected = model.tokenizer.tokenize(part)
            self.assertEqual(tokens, [0, *expected, 0])
            self.assertEqual(call["style"].shape, (1, 1))
            self.assertEqual(call["style"].item(), len(expected))
            self.assertEqual(call["speed"].shape, (1,))
            # Preserve the existing export-specific speed dtype and conversion.
            modern = "input_ids" in call
            self.assertEqual(call["speed"].dtype, np.int32 if modern else np.float32)
            self.assertEqual(call["speed"].item(), int(speed) if modern else speed)
            observed.extend(tokens[1:-1])
        self.assertEqual(observed, [model.tokenizer.vocab[c] for c in phones if c in model.tokenizer.vocab])

    def test_exact_edge_lengths_and_unbroken_words(self):
        model = self.make_model()
        for length in (0, 1, 509, 510, 511, 1019, 1020, 1021, 1049, 1530, 1531):
            with self.subTest(length=length):
                parts = self.assert_lossless(model, "a" * length)
                self.assertEqual(
                    [len(p) for p in parts], [510] * (length // 510) + ([length % 510] if length % 510 else [])
                )

    def test_punctuation_then_whitespace_then_hard_boundary(self):
        model = self.make_model()
        cases = [
            ("a" * 200 + "." + "b" * 300 + " " + "c" * 200, 201),
            ("a" * 200 + " " + "b" * 400, 201),
            ("a" * 509 + ".b", 510),
            ("a" * 510 + ".b", 510),
            ("a" * 510 + " b", 510),
            ("a" * 200 + "\t" + "b" * 400, 201),
        ]
        for phones, first_length in cases:
            with self.subTest(first_length=first_length, length=len(phones)):
                self.assertEqual(len(self.assert_lossless(model, phones)[0]), first_length)

    def test_whitespace_punctuation_and_unicode_are_never_rewritten(self):
        model = self.make_model()
        for phones in ("  a, b!  ", "a,b;;c!?d", " " * 1021, "\t\n" * 400, "ˈhəlˌoʊ\t世界、。" * 100, "a." * 900):
            with self.subTest(length=len(phones)):
                self.assert_lossless(model, phones)

    def test_deterministic_mixed_sequence_coverage(self):
        rng = random.Random(20261004)
        model = self.make_model()
        for _ in range(100):
            phones = "".join(rng.choices("abɜːˈ .,!?;\t\n世界", k=rng.randrange(3000)))
            self.assert_lossless(model, phones)

    def test_sync_all_tokens_once_both_model_exports(self):
        for modern in (False, True):
            model = self.make_model(modern)
            phones = "həlˈoʊ " * 149 + "həlˈoʊ"
            audio, rate = model.create(phones, "fixture", speed=1.25, is_phonemes=True, trim=False)
            self.assert_model_inputs(model, phones, speed=1.25)
            self.assertEqual(rate, 24000)
            self.assertEqual(audio.dtype, np.float32)
            self.assertEqual(audio.shape, (24 * len(model.sess.calls),))

    async def test_stream_all_tokens_once_and_same_audio_as_sync(self):
        phones = "həlˈoʊ " * 149 + "həlˈoʊ"
        model = self.make_model()
        expected, _ = model.create(phones, "fixture", is_phonemes=True, trim=False)
        model.sess.calls.clear()
        chunks = [chunk async for chunk in model.create_stream(phones, "fixture", is_phonemes=True, trim=False)]
        self.assert_model_inputs(model, phones)
        self.assertTrue(all(rate == 24000 and audio.shape == (24,) for audio, rate in chunks))
        np.testing.assert_array_equal(np.concatenate([audio for audio, _ in chunks]), expected)

    async def test_language_forwarding_keeps_existing_phonemizer_options(self):
        for lang in ("en-us", "en-gb", "es", "fr", "it", "pt", "ja", "zh", "hi"):
            with self.subTest(lang=lang):
                for stream in (False, True):
                    model = self.make_model()
                    with patch.object(self.phonemizer, "phonemize", return_value=" a" * 600 + " 世界 ") as native:
                        if stream:
                            result = [
                                part async for part in model.create_stream(" text ", "fixture", lang=lang, trim=False)
                            ]
                            self.assertTrue(result)
                        else:
                            model.create(" text ", "fixture", lang=lang, trim=False)
                        native.assert_called_once_with("text", lang, preserve_punctuation=True, with_stress=True)
                    self.assert_model_inputs(model, (" a" * 600).strip())

    async def test_empty_sync_and_stream_skip_model(self):
        model = self.make_model()
        audio, rate = model.create("", "fixture", is_phonemes=True)
        self.assertEqual(audio.shape, (0,))
        self.assertEqual(audio.dtype, np.float32)
        self.assertEqual(rate, 24000)
        self.assertEqual([c async for c in model.create_stream("", "fixture", is_phonemes=True)], [])
        self.assertEqual(model.sess.calls, [])

    def test_overlength_internal_call_fails_instead_of_losing_tail(self):
        model = self.make_model()
        with self.assertRaises(ValueError):
            model._create_audio("a" * 511, model.voices["fixture"], 1.0)
        self.assertEqual(model.sess.calls, [])

    def test_missing_phonemes_still_use_existing_filtering(self):
        model = self.make_model()
        for phones in ("a世界b", "世界", "a" * 509 + "世界b" * 100):
            model.sess.calls.clear()
            model.create(phones, "fixture", is_phonemes=True, trim=False)
            self.assert_model_inputs(model, phones)
        self.assertEqual(model.tokenizer.tokenize("世界"), [])

    def test_blended_style_and_trim_delegate_per_chunk(self):
        model = self.make_model()
        blend = model.voices["fixture"] + 0.5
        phones = "a" * 1021
        with patch.object(self.module, "trim_audio", side_effect=lambda audio: (audio[1:], None)) as trim:
            audio, rate = model.create(phones, blend, is_phonemes=True)
        self.assertEqual(trim.call_count, 3)
        self.assertEqual(audio.shape, (69,))
        self.assertEqual(rate, 24000)
        self.assertEqual([call["style"].item() for call in model.sess.calls], [510.5, 510.5, 1.5])

    async def test_stream_error_propagates_instead_of_waiting_forever(self):
        model = self.make_model()
        with patch.object(model, "_create_audio", side_effect=ValueError("model failed")):
            with self.assertRaisesRegex(ValueError, "model failed"):
                await asyncio.wait_for(model.create_stream("a", "fixture", is_phonemes=True).__anext__(), timeout=1)

    async def assert_stream_cleanup(self, close_after_first):
        model = self.make_model()
        loop = asyncio.get_running_loop()
        pending = []
        submitted = asyncio.Event()
        original_tasks = asyncio.all_tasks()

        def submit(executor, function, *args):
            future = loop.create_future()
            pending.append(future)
            submitted.set()
            return future

        consumer = None
        stream = model.create_stream("a" * 1600, "fixture", is_phonemes=True, trim=False)
        try:
            with patch.object(loop, "run_in_executor", side_effect=submit):
                consumer = asyncio.create_task(stream.__anext__())
                await asyncio.wait_for(submitted.wait(), timeout=1)
                if close_after_first:
                    pending[0].set_result((np.ones(24, dtype=np.float32), 24000))
                    await consumer
                    await stream.aclose()
                else:
                    consumer.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await consumer
                await asyncio.sleep(0)
                self.assertTrue(all(future.done() for future in pending))
                self.assertFalse(asyncio.all_tasks() - original_tasks)
        finally:
            # Clean even the original negative control's orphan producer.
            if consumer is not None and not consumer.done():
                consumer.cancel()
            extra = asyncio.all_tasks() - original_tasks
            for task in extra:
                task.cancel()
            if extra:
                await asyncio.gather(*extra, return_exceptions=True)
            await stream.aclose()

    async def test_stream_cancellation_stops_background_producer(self):
        await self.assert_stream_cleanup(close_after_first=False)

    async def test_stream_close_stops_remaining_batches(self):
        await self.assert_stream_cleanup(close_after_first=True)


if __name__ == "__main__":
    unittest.main()
