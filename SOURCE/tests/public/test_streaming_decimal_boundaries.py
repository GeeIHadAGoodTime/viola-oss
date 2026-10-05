"""Actual desktop streaming ingestion, with synthesis and playback kept inert."""

from __future__ import annotations

import ast
import asyncio
import re
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.public.test_phone_locale_normalization import _formatter

ROOT = Path(__file__).resolve().parents[2]


def load_streaming_method(source: Path):
    tree = ast.parse(source.read_text(encoding="utf-8"))
    method = next(
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "speak_streaming"
    )
    helpers = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id in {"_SENTENCE_RE", "_EMOJI_RE"} for target in node.targets
            )
        )
        or (isinstance(node, ast.FunctionDef) and node.name == "_strip_emoji")
    ]
    logger = types.SimpleNamespace(**{name: lambda *args, **kwargs: None for name in ("debug", "info", "warning")})
    namespace = {"asyncio": asyncio, "re": re, "time": time, "logger": logger}
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            *helpers,
            ast.ClassDef(name="BoundStreaming", bases=[], keywords=[], body=[method], decorator_list=[]),
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace["BoundStreaming"]


class StreamingDecimalBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        normalizer, _ = _formatter()
        self.normalize = normalizer.normalize_for_speech
        self.monitor_states = []
        monitor_module = types.ModuleType("diagnostics.wake_state_sync")
        monitor_module.get_state_sync_monitor = lambda: types.SimpleNamespace(
            update_tts_state=lambda **kwargs: self.monitor_states.append(kwargs["is_speaking"])
        )
        duck_module = types.ModuleType("utils.audio_ducking")
        from contextlib import nullcontext

        duck_module.duck_context = nullcontext
        self.enterContext(
            patch.dict(
                "sys.modules",
                {
                    "voice.synthesis.text_normalizer": normalizer,
                    "diagnostics.wake_state_sync": monitor_module,
                    "utils.audio_ducking": duck_module,
                },
            )
        )
        self.engine = load_streaming_method(ROOT / "voice/synthesis/kokoro_engine.py")()
        self.engine._speak_lock = None
        self.engine.last_sample_rate = 24000
        self.calls = []
        self.played = []

        def synthesize(text, voice):
            self.calls.append(text)
            return b"\x00\x00" * 16

        self.engine._synthesize_locked = synthesize
        self.engine._play_pcm_raw = lambda pcm, rate: self.played.append((pcm, rate))
        self.engine._sleep_sentence_gap = AsyncMock()
        self.engine._smooth_sentence_boundary = lambda previous, current: current

    async def consume(self, chunks):
        async def stream():
            for chunk in chunks:
                yield chunk

        self.calls.clear()
        self.played.clear()
        result = await self.engine.speak_streaming(stream())
        self.assertEqual(result, "".join(chunks))
        self.assertFalse(self.engine._speak_lock.locked())
        return list(self.calls)

    async def test_native_diagnostic_decimal_preserved_at_every_two_part_split(self):
        raw = "The value is -0.01234567890123456789."
        expected = self.normalize(raw)
        self.assertIn("minus zero point zero one two", expected)
        for position in range(1, len(raw)):
            with self.subTest(position=position):
                self.assertEqual(await self.consume([raw[:position], raw[position:]]), [expected])

    async def test_multichunk_and_single_character_delivery_preserve_the_whole_value(self):
        raw = "The value is -0.01234567890123456789."
        expected = [self.normalize(raw)]
        for chunks in (
            list(raw),
            ["The value is ", "-", "0", ".", "012", "345", "678", "901", "234", "567", "89", "."],
            ["", "The value is -0.", "", "01234567890123456789.", ""],
        ):
            with self.subTest(chunks=chunks):
                self.assertEqual(await self.consume(chunks), expected)

    async def test_signed_decimal_and_currency_forms_survive_each_partition(self):
        for raw in (
            "Value +1.25.",
            "Value -12.00001.",
            "Price $1.50.",
            "Values 0.1 and 2.3.",
            "Value .5.",
            "Value -.5.",
            "Value +.50.",
            "Value −.05.",
        ):
            expected = [self.normalize(raw)]
            for position in range(1, len(raw)):
                with self.subTest(raw=raw, position=position):
                    self.assertEqual(await self.consume([raw[:position], raw[position:]]), expected)

    async def test_leading_decimals_single_character_and_explicit_boundary_delivery(self):
        for sign in ("", "+", "-", "−"):
            raw = f"Value {sign}.50."
            with self.subTest(sign=sign):
                expected = [self.normalize(raw)]
                self.assertIn("zero point five zero", expected[0])
                self.assertEqual(await self.consume(list(raw)), expected)
                self.assertEqual(await self.consume([f"Value {sign}.", "", "50", "."]), expected)
                self.assertEqual(await self.consume([f"Value {sign}.50"]), [self.normalize(f"Value {sign}.50")])

    async def test_pending_decimal_helper_tracks_the_retained_complete_token_grammar(self):
        normalizer, _ = _formatter()
        for prefix in ("", "Value ", "(", "[", '"', "'", "value:", "v", "x/", "x\\", "@", "#", "$", "..", "++", "--"):
            for sign in ("", "+", "-", "−"):
                pending = prefix + sign + "."
                probe = pending + "0"
                expected = any(m.end() == len(probe) for m in normalizer._DECIMAL_RE.finditer(probe))
                with self.subTest(pending=pending):
                    self.assertEqual(normalizer.has_pending_decimal_point(pending), expected)
        for ordinary in ("Ready.", "Dr.", "C++.", "Wait...", "3!", "3?", "", ". "):
            with self.subTest(ordinary=ordinary):
                self.assertFalse(normalizer.has_pending_decimal_point(ordinary))

    async def test_cancelled_leading_decimal_prefixes_are_not_spoken_or_reused(self):
        for prefix in ("Value .", "Value -.", "Value +.", "Value −."):
            with self.subTest(prefix=prefix):
                self.calls.clear()
                entered = asyncio.Event()
                never = asyncio.Event()

                async def stream():
                    yield prefix
                    entered.set()
                    await never.wait()

                task = asyncio.create_task(self.engine.speak_streaming(stream()))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    self.assertEqual(self.calls, [])
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                self.assertFalse(self.engine._speak_lock.locked())
                self.assertEqual(await self.consume(["Fresh reply."]), ["Fresh reply."])

    async def test_final_numeric_sentence_flushes_without_losing_the_period_or_value(self):
        for raw in ("There are 3.", "Value -0.05", "Price $1.5."):
            with self.subTest(raw=raw):
                self.assertEqual(await self.consume([raw]), [self.normalize(raw)])

    async def test_known_delimiter_flushes_numeric_sentence_before_stream_end(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def stream():
            yield "There are 3."
            yield " Next sentence. "
            entered.set()
            await release.wait()

        task = asyncio.create_task(self.engine.speak_streaming(stream()))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            self.assertEqual(self.calls, [self.normalize("There are 3."), self.normalize("Next sentence.")])
        finally:
            release.set()
            await asyncio.wait_for(task, 5)

    async def test_ordinary_punctuation_keeps_immediate_synthesis(self):
        for raw in ("Ready.", "Ready!", "Ready?", "Count 3!", "Count 3?", "Dr.", "C++.", "Wait...", "Ready . "):
            with self.subTest(raw=raw):
                self.calls.clear()
                entered = asyncio.Event()
                release = asyncio.Event()

                async def stream():
                    yield raw
                    entered.set()
                    await release.wait()

                task = asyncio.create_task(self.engine.speak_streaming(stream()))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    self.assertEqual(self.calls, [self.normalize(raw)])
                finally:
                    release.set()
                    await asyncio.wait_for(task, 5)

    async def test_cancelled_decimal_prefix_is_not_spoken_or_reused(self):
        entered = asyncio.Event()
        never = asyncio.Event()

        async def stream():
            yield "The value is -0."
            entered.set()
            await never.wait()
            yield "01234567890123456789."

        task = asyncio.create_task(self.engine.speak_streaming(stream()))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            self.assertEqual(self.calls, [])
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.engine._speak_lock.locked())
        self.assertEqual(self.monitor_states[-2:], [True, False])
        self.assertEqual(await self.consume(["Fresh reply."]), ["Fresh reply."])

    async def test_iterator_failure_does_not_flush_or_leak_a_pending_decimal(self):
        async def stream():
            yield "Value 12."
            raise RuntimeError("synthetic upstream stop")

        with self.assertRaisesRegex(RuntimeError, "synthetic upstream stop"):
            await self.engine.speak_streaming(stream())
        self.assertEqual(self.calls, [])
        self.assertFalse(self.engine._speak_lock.locked())
        self.assertEqual(self.monitor_states[-2:], [True, False])
        self.assertEqual(await self.consume(["Fresh reply."]), ["Fresh reply."])


if __name__ == "__main__":
    unittest.main()
