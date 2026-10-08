"""Actual public facade/engine control flow with model and audio boundaries inert."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import concurrent.futures
import functools
import re
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from tests.public.test_phone_locale_normalization import _formatter

ROOT = Path(__file__).resolve().parents[2]


def _bound_class(path, name, method_names, *, helpers=(), constants=(), namespace=None):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    methods = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in method_names
    ]
    declarations = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in helpers)
        or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets))
    ]
    body = [
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *declarations,
        ast.ClassDef(name=name, bases=[], keywords=[], body=methods, decorator_list=[]),
    ]
    scope = dict(namespace or {})
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(path), "exec"), scope)
    return scope[name]


class DisabledTTSStreamingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        normalizer, _ = _formatter()
        self.normalize = normalizer.normalize_for_speech
        self.config = types.SimpleNamespace(tts_enabled=True)
        self.monitor_states = []
        monitor = types.ModuleType("diagnostics.wake_state_sync")
        monitor.get_state_sync_monitor = lambda: types.SimpleNamespace(
            update_tts_state=lambda **kwargs: self.monitor_states.append(kwargs["is_speaking"])
        )
        duck = types.ModuleType("utils.audio_ducking")
        duck.duck_context = contextlib.nullcontext
        telemetry = types.ModuleType("admin.instrumentation")
        telemetry.record_feature_used = lambda *args: None
        self.enterContext(
            patch.dict(
                "sys.modules",
                {
                    "voice.synthesis.text_normalizer": normalizer,
                    monitor.__name__: monitor,
                    duck.__name__: duck,
                    telemetry.__name__: telemetry,
                },
            )
        )

        # Cached playback's numeric conversion is an inert identity leaf here;
        # gain/post-FX behavior has separate actual-array controls.
        class PCM:
            def __init__(self, value):
                self.value = value

            def tobytes(self):
                return self.value

        namespace = dict(
            asyncio=asyncio,
            threading=threading,
            re=re,
            time=time,
            logger=Mock(),
            settings=self.config,
            SAMPLE_RATE_24K=24000,
            np=types.SimpleNamespace(int16=object(), frombuffer=lambda value, **k: PCM(value)),
        )
        engine_type = _bound_class(
            ROOT / "voice/synthesis/kokoro_engine.py",
            "BoundEngine",
            {
                "synthesize",
                "_run_synthesize_with_watchdog",
                "_synthesize_chunked",
                "_tts_is_enabled",
                "stop",
                "_prepare_route_text",
                "_play_pcm_if_enabled",
                "_synthesize_locked",
                "speak",
                "_speak_single",
                "_speak_streaming",
                "speak_streaming",
                "_speak_cached_pcm",
                "_lookup_opener_cache",
            },
            helpers={"_strip_emoji", "_split_sentences"},
            constants={"_EMOJI_RE", "_SENTENCE_RE", "_CHUNK_THRESHOLD", "_MAX_TEXT_LENGTH", "STREAM_TIMEOUT_SECONDS"},
            namespace=namespace,
        )
        self.engine = engine_type()
        self.engine._config = self.config
        self.engine._lock = threading.Lock()
        self.engine._speak_lock = None
        self.engine.last_sample_rate = 24000
        self.calls, self.plays, self.events, self.loads = [], [], [], []
        self.pcm = b"\x00\x01" * 8

        def internal(text, voice, speed, *, apply_volume):
            self.calls.append(text)
            self.events.append(("synthesis", text))
            return self.pcm

        def play(pcm, rate=24000, **kwargs):
            self.plays.append((pcm, rate))
            self.events.append(("playback", rate))
            return True

        self.engine._ensure_loaded = lambda: self.loads.append(True) or True
        self.engine._synthesize_internal = internal
        self.engine._play_pcm_raw = play
        self.engine._play_pcm_locally = play
        self.engine._lookup_opener_cache = lambda _: None
        self.engine._smooth_sentence_boundary = lambda previous, pcm: pcm
        self.engine._sleep_sentence_gap = AsyncMock()
        self.engine._join_sentence_chunks = lambda chunks, sentences: b"".join(chunks)
        self.engine._scale_pcm_volume = lambda pcm, volume: pcm
        self.engine._current_volume = lambda: 1.0
        facade_type = _bound_class(
            ROOT / "voice/synthesizer.py", "BoundFacade", {"speak", "speak_streaming"}, namespace={"logger": Mock()}
        )
        self.facade = facade_type()
        self.facade._impl = self.engine
        self.long_text = (
            ("Hello music welcome friend " * 5).strip() + ". " + ("Hello music welcome friend " * 5).strip() + "."
        )
        self.assertGreater(len(self.normalize(self.long_text)), 200)

    async def test_disabled_before_start_blocks_all_public_speech_paths(self):
        self.config.tts_enabled = False
        self.assertEqual(await self.engine.synthesize("Hello."), b"")
        await self.facade.speak("Hello.")
        await self.facade.speak(self.long_text)
        consumed = []

        async def chunks():
            for value in ("First.", "Second."):
                consumed.append(value)
                yield value

        self.assertEqual(await self.facade.speak_streaming(chunks()), "First.Second.")
        self.assertEqual(consumed, ["First.", "Second."])
        self.assertEqual((self.calls, self.plays, self.loads), ([], [], []))

    async def test_disable_between_chunks_keeps_history_but_stops_next_speech(self):
        async def chunks():
            yield "First."
            self.assertEqual(len(self.plays), 1)
            self.config.tts_enabled = False
            yield "Second."

        self.assertEqual(await self.facade.speak_streaming(chunks()), "First.Second.")
        self.assertEqual(self.calls, ["First."])
        self.assertEqual(len(self.plays), 1)

    async def test_reenable_never_replays_a_disabled_fragment(self):
        self.config.tts_enabled = False

        async def chunks():
            yield "Muted incomplete fragment "
            self.config.tts_enabled = True
            yield "Fresh."

        self.assertEqual(await self.facade.speak_streaming(chunks()), "Muted incomplete fragment Fresh.")
        self.assertEqual(self.calls, ["Fresh."])
        self.assertEqual(len(self.plays), 1)

    async def test_disabling_clears_an_earlier_pending_fragment_and_final_flush(self):
        async def chunks():
            yield "Pending incomplete fragment "
            self.config.tts_enabled = False
            yield "must stay quiet"

        self.assertEqual(await self.facade.speak_streaming(chunks()), "Pending incomplete fragment must stay quiet")
        self.assertEqual((self.calls, self.plays), ([], []))
        self.assertFalse(self.engine._speak_lock.locked())

    async def test_finished_inference_is_not_played_if_disabled_before_delivery(self):
        original = self.engine._synthesize_internal

        def finish_disabled(*args, **kwargs):
            result = original(*args, **kwargs)
            self.config.tts_enabled = False
            return result

        self.engine._synthesize_internal = finish_disabled
        for text in ("Hello.", self.long_text):
            with self.subTest(text=text):
                self.calls.clear()
                self.config.tts_enabled = True
                await self.facade.speak(text)
                self.assertTrue(self.calls)
                self.assertEqual(self.plays, [])
        self.config.tts_enabled = True

        async def chunks():
            yield "First."

        await self.facade.speak_streaming(chunks())
        self.assertEqual(self.plays, [])

    async def test_cached_pcm_cannot_bypass_disable_at_playback_boundary(self):
        self.config.tts_enabled = False
        self.engine._speak_cached_pcm(self.pcm)
        self.assertEqual((self.calls, self.plays), ([], []))
        self.config.tts_enabled = True
        self.engine._speak_cached_pcm(self.pcm)
        self.assertEqual(self.plays, [(self.pcm, 24000)])

    async def test_already_disabled_work_does_not_wait_for_a_busy_model(self):
        self.config.tts_enabled = False

        class BusyModelLock:
            def __enter__(self):
                raise AssertionError("Disabled speech attempted to acquire the model lock")

            def __exit__(self, *args):
                return False

        self.engine._lock = BusyModelLock()
        self.assertEqual(await asyncio.to_thread(self.engine._synthesize_locked, "Muted.", None), b"")
        self.assertEqual((self.calls, self.loads), ([], []))

    async def test_worker_rechecks_policy_after_waiting_for_model_lock(self):
        entered = threading.Event()
        underlying = threading.Lock()
        underlying.acquire()

        class WaitingLock:
            def __enter__(self):
                entered.set()
                underlying.acquire()
                return self

            def __exit__(self, *args):
                underlying.release()

        self.engine._lock = WaitingLock()
        task = asyncio.create_task(asyncio.to_thread(self.engine._synthesize_locked, "Queued.", None))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            self.config.tts_enabled = False
        finally:
            underlying.release()
        self.assertEqual(await asyncio.wait_for(task, 5), b"")
        self.assertEqual((self.calls, self.loads), ([], []))

    async def test_enabled_streaming_preserves_immediate_sentence_delivery(self):
        async def chunks():
            yield "First."
            self.assertEqual(self.calls, ["First."])
            self.assertEqual(len(self.plays), 1)
            yield "Second."

        self.assertEqual(await self.facade.speak_streaming(chunks()), "First.Second.")
        self.assertEqual(self.calls, ["First.", "Second."])
        self.assertEqual(len(self.plays), 2)

    async def test_disabled_stream_cancellation_releases_ownership(self):
        self.config.tts_enabled = False
        entered = asyncio.Event()
        wait = asyncio.Event()

        async def chunks():
            yield "Quiet pending text"
            entered.set()
            await wait.wait()

        task = asyncio.create_task(self.facade.speak_streaming(chunks()))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((self.calls, self.plays), ([], []))
        self.assertFalse(self.engine._speak_lock.locked())
        self.assertEqual(self.monitor_states[-2:], [True, False])
        self.config.tts_enabled = True
        await self.facade.speak("Fresh.")
        self.assertEqual(self.calls, ["Fresh."])

    # Observed-disable ownership regressions, reproduced independently in review.
    async def test_disable_during_load_cannot_start_new_inference_after_load(self):

        def load_then_disable():
            self.loads.append(True)
            self.config.tts_enabled = False
            return True

        self.engine._ensure_loaded = load_then_disable
        await self.facade.speak("A fresh sentence.")
        self.assertEqual(self.loads, [True])
        self.assertEqual(self.calls, [], "Inference began after loading finished with TTS disabled")
        self.assertEqual(self.plays, [])

    async def test_observed_disable_retires_pending_fragment_before_reenable(self):
        internal = self.engine._synthesize_internal

        def finish_first_disabled(text, *args, **kwargs):
            result = internal(text, *args, **kwargs)
            if text == "First.":
                self.config.tts_enabled = False
            return result

        self.engine._synthesize_internal = finish_first_disabled

        async def chunks():
            yield "First. Pending fragment "
            self.assertFalse(self.config.tts_enabled)
            self.assertEqual(self.plays, [])
            self.config.tts_enabled = True
            yield "Fresh."

        result = await self.facade.speak_streaming(chunks())
        self.assertEqual(result, "First. Pending fragment Fresh.")
        self.assertEqual(self.calls, ["First.", "Fresh."])
        self.assertEqual(len(self.plays), 1)

    async def test_prefetched_audio_is_retired_after_observed_disable_even_if_reenabled(self):
        second_started = threading.Event()
        release_second = threading.Event()

        def internal(text, *args, **kwargs):
            self.calls.append(text)
            if text == "Second.":
                second_started.set()
                if not release_second.wait(5):
                    raise AssertionError("Controlled second inference did not drain")
            return text.encode()

        self.engine._synthesize_internal = internal
        actual_delivery = self.engine._play_pcm_if_enabled

        def delivery(pcm, rate, **kwargs):
            if pcm == b"First.":
                self.assertTrue(second_started.wait(5), "Second prefetch never started")
                self.config.tts_enabled = False
                self.assertFalse(actual_delivery(pcm, rate, **kwargs))
                self.config.tts_enabled = True
                release_second.set()
                return False
            return actual_delivery(pcm, rate, **kwargs)

        self.engine._play_pcm_if_enabled = delivery

        async def chunks():
            yield "First. Second. "

        try:
            text = await asyncio.wait_for(self.facade.speak_streaming(chunks()), 8)
            self.assertEqual(text, "First. Second. ")
            self.assertEqual(self.calls, ["First.", "Second."])
            self.assertEqual(self.plays, [], "Retired prefetched PCM was dispatched after re-enable")
        finally:
            release_second.set()

    async def test_cache_lookup_disable_race_and_legacy_callback_shape(self):

        def lookup(_):
            self.config.tts_enabled = False
            return self.pcm

        self.engine._lookup_opener_cache = lookup
        await self.facade.speak("Hello.")
        self.assertEqual((self.calls, self.plays), ([], []))
        self.config.tts_enabled = True
        self.engine._lookup_opener_cache = lambda _: self.pcm
        self.engine._play_pcm_locally = lambda pcm, **kwargs: self.plays.append(pcm) or True
        await self.facade.speak("Hello.")
        self.assertEqual(self.plays, [self.pcm])
        self.assertEqual(self.calls, [])

    async def test_suppressed_pcm_cannot_feed_the_next_enabled_crossfade(self):
        boundaries = []

        def internal(text, *args, **kwargs):
            self.calls.append(text)
            if text == "First.":
                self.config.tts_enabled = False
            return text.encode()

        def smooth(previous, current):
            boundaries.append((previous, current))
            return current

        self.engine._synthesize_internal = internal
        self.engine._smooth_sentence_boundary = smooth

        async def chunks():
            yield "First."
            self.assertEqual(self.plays, [])
            self.config.tts_enabled = True
            yield "Fresh."

        self.assertEqual(await self.facade.speak_streaming(chunks()), "First.Fresh.")
        self.assertEqual(self.calls, ["First.", "Fresh."])
        self.assertTrue(boundaries)
        self.assertEqual(boundaries[-1], (None, b"Fresh."))
        self.assertFalse(any((previous == b"First." for previous, current in boundaries)))

    async def test_cached_request_waiting_for_speech_lock_is_retired_by_observed_disable(self):
        waiting = asyncio.Event()

        class SpeechLock(asyncio.Lock):
            async def acquire(inner):
                if inner.locked():
                    waiting.set()
                return await super().acquire()

        def lookup(_):
            return self.pcm

        self.engine._lookup_opener_cache = lookup
        self.engine._speak_lock = SpeechLock()
        await self.engine._speak_lock.acquire()
        task = asyncio.create_task(self.facade.speak("Old cached request."))
        try:
            await asyncio.wait_for(waiting.wait(), 5)
            self.config.tts_enabled = False
            self.assertEqual(await self.engine.synthesize("Muted request."), b"")
            self.config.tts_enabled = True
        finally:
            self.engine._speak_lock.release()
        await asyncio.wait_for(task, 5)
        self.assertEqual(self.plays, [], "A cached request admitted before observed disable was played after re-enable")
        await self.facade.speak("Fresh cached request.")
        self.assertEqual(len(self.plays), 1)
        self.assertEqual(self.calls, [])

    async def queued_speech(self, text):
        waiting = asyncio.Event()

        class SpeechLock(asyncio.Lock):
            async def acquire(inner):
                if inner.locked():
                    waiting.set()
                return await super().acquire()

        def lookup(_):
            return None

        self.engine._lookup_opener_cache = lookup
        self.engine._speak_lock = SpeechLock()
        await self.engine._speak_lock.acquire()
        task = asyncio.create_task(self.facade.speak(text))
        try:
            await asyncio.wait_for(waiting.wait(), 5)
            self.config.tts_enabled = False
            self.assertEqual(await self.engine.synthesize("Observed mute."), b"")
            self.config.tts_enabled = True
        finally:
            self.engine._speak_lock.release()
        await asyncio.wait_for(task, 5)
        self.assertEqual(self.calls, [], "Retired speech should not start inference after the speech lock opens")
        self.assertEqual(self.plays, [])
        await self.facade.speak("Fresh request.")
        self.assertEqual(self.calls, ["Fresh request."])
        self.assertEqual(len(self.plays), 1)

    async def test_short_request_waiting_for_speech_lock_is_retired(self):
        await self.queued_speech("Old short request.")

    async def test_long_request_waiting_for_speech_lock_is_retired(self):
        await self.queued_speech(self.long_text)

    async def test_worker_waiting_for_model_lock_is_retired(self):
        entered = threading.Event()
        lock = threading.Lock()
        lock.acquire()

        class WaitingLock:

            def __enter__(self):
                entered.set()
                lock.acquire()
                return self

            def __exit__(self, *_):
                lock.release()

        self.engine._lock = WaitingLock()
        task = asyncio.create_task(asyncio.to_thread(self.engine._synthesize_locked, "Old model request.", None))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            self.config.tts_enabled = False
            self.assertEqual(await self.engine.synthesize("Observed mute."), b"")
            self.config.tts_enabled = True
        finally:
            lock.release()
        self.assertEqual(await asyncio.wait_for(task, 5), b"")
        self.assertEqual((self.calls, self.loads), ([], []))
        self.assertTrue(await self.engine.synthesize("Fresh request."))
        self.assertEqual(self.calls, ["Fresh request."])

    async def test_enabled_callback_false_or_none_does_not_retire_pending_text(self):
        for result in [False, None, True]:
            with self.subTest(result=result):
                self.calls.clear()
                self.plays.clear()

                def callback(pcm, rate, **kwargs):
                    self.plays.append((pcm, rate))
                    return result

                self.engine._play_pcm_raw = callback

                async def chunks():
                    yield "First. Pending "
                    yield "fragment."

                self.assertEqual(await self.facade.speak_streaming(chunks()), "First. Pending fragment.")
                self.assertEqual(self.calls, ["First.", "Pending fragment."])
                self.assertEqual(len(self.plays), 2)

    async def test_existing_default_config_and_global_fallback_semantics_remain(self):
        self.engine._config = types.SimpleNamespace()
        await self.facade.speak("Enabled by existing default.")
        self.assertEqual(len(self.plays), 1)
        self.calls.clear()
        self.plays.clear()
        self.engine._config = None
        self.config.tts_enabled = False
        await self.facade.speak(self.long_text)
        self.assertEqual((self.calls, self.plays), ([], []))

    async def test_executor_queued_synthesis_is_retired_after_observed_disable(self):
        occupied = threading.Event()
        release = threading.Event()
        submitted = asyncio.Event()
        loop = asyncio.get_running_loop()

        class Executor(concurrent.futures.ThreadPoolExecutor):

            def submit(inner, fn, *args, **kwargs):
                result = super().submit(fn, *args, **kwargs)
                if isinstance(fn, functools.partial) and getattr(fn.args[0], "__name__", None) in {
                    "_synthesize_locked",
                    "run_owned",
                }:
                    submitted.set()
                return result

        executor = Executor(max_workers=1)
        loop.set_default_executor(executor)

        def block():
            occupied.set()
            return release.wait(5)

        blocker = loop.run_in_executor(None, block)
        while not occupied.is_set():
            await asyncio.sleep(0)
        task = asyncio.create_task(self.engine.synthesize("Old queued request."))
        try:
            await asyncio.wait_for(submitted.wait(), 5)
            self.assertEqual(self.calls, [])
            self.config.tts_enabled = False
            self.assertEqual(await self.engine.synthesize("Observed mute."), b"")
            self.config.tts_enabled = True
        finally:
            release.set()
        await blocker
        result = await asyncio.wait_for(task, 5)
        self.assertEqual(result, b"", "Retired executor-queued synthesis returned old PCM")
        self.assertEqual(self.calls, [], "Retired executor-queued synthesis started new inference")
        self.assertTrue(await self.engine.synthesize("Fresh request."))
        self.assertEqual(self.calls, ["Fresh request."])

    async def test_cache_lookup_preserves_missing_config_global_fallback(self):
        del self.engine._config
        self.engine._opener_cache = types.SimpleNamespace(lookup=Mock(return_value=self.pcm))
        lookup = type(self.engine)._lookup_opener_cache
        self.assertEqual(lookup(self.engine, "Hello."), self.pcm)
        self.config.tts_enabled = False
        self.assertIsNone(lookup(self.engine, "Hello."))
        self.engine._opener_cache.lookup.assert_called_once_with("Hello.", synthesize_variant=None)

    async def test_observed_disable_retires_the_previous_enabled_crossfade_epoch(self):
        boundaries = []

        def internal(text, *args, **kwargs):
            self.calls.append(text)
            if text == "First.":
                self.config.tts_enabled = False
            return text.encode()

        def smooth(previous, current):
            boundaries.append((previous, current))
            return current

        self.engine._synthesize_internal = internal
        self.engine._smooth_sentence_boundary = smooth

        async def chunks():
            yield "Before."
            yield "First."
            self.assertEqual(len(self.plays), 1)
            self.config.tts_enabled = True
            yield "Fresh."

        self.assertEqual(await self.facade.speak_streaming(chunks()), "Before.First.Fresh.")
        self.assertEqual(self.calls, ["Before.", "First.", "Fresh."])
        self.assertTrue(boundaries)
        self.assertEqual(boundaries[-1], (None, b"Fresh."))
        self.assertFalse(any((previous == b"First." for previous, current in boundaries)))


if __name__ == "__main__":
    unittest.main()
