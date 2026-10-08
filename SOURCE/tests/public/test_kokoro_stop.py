"""Actual Kokoro cancellation with synthetic model/audio boundaries only.

These controls do not load a model, contact providers, or establish audibility.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import numpy as np

from tests.public.test_speech_volume_wiring import _isolated_modules, _isolated_network
from voice.synthesis.kokoro_engine import KokoroTTSEngine
from voice.synthesizer import Synthesizer


class KokoroStopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(_isolated_network())
        self.config = types.SimpleNamespace(
            tts_enabled=True,
            tts_opener_cache_enabled=False,
            tts_post_fx_enabled=False,
            tts_speed_jitter_pct=0,
            tts_intersentence_gap_ms=0,
            tts_voice_blend=None,
        )
        self.engine = KokoroTTSEngine(config=self.config, volume=1.0)
        self.model = types.SimpleNamespace(create=Mock(return_value=(np.ones(24, dtype=np.float32), 24000)))
        self.engine._kokoro = self.model
        self.facade = Synthesizer(self.config, self.engine)
        self.plays = []
        self.release_audio = threading.Event()
        self.playing = threading.Event()
        self.stopped = threading.Event()
        self.sd = types.ModuleType("sounddevice")
        self.sd.play = Mock(side_effect=self.play)
        self.sd.wait = Mock(side_effect=lambda: self.release_audio.wait(3))
        self.sd.stop = Mock(side_effect=AssertionError("Never stop a process-global stream"))
        self.sd.CallbackAbort = type("CallbackAbort", (Exception,), {})
        self.sd.CallbackStop = type("CallbackStop", (Exception,), {})
        self.streams = []
        self.sd.OutputStream = Mock(side_effect=self.output_stream)
        self.monitor_states = []
        monitor = types.ModuleType("diagnostics.wake_state_sync")
        monitor.get_state_sync_monitor = lambda: types.SimpleNamespace(
            update_tts_state=lambda **kw: self.monitor_states.append(kw["is_speaking"])
        )
        duck = types.ModuleType("utils.audio_ducking")
        duck.duck_context = contextlib.nullcontext
        telemetry = types.ModuleType("admin.instrumentation")
        telemetry.record_feature_used = telemetry.record_tts_latency = lambda *a: None
        self.enterContext(
            _isolated_modules(
                {
                    "sounddevice": self.sd,
                    monitor.__name__: monitor,
                    duck.__name__: duck,
                    telemetry.__name__: telemetry,
                }
            )
        )
        self.enterContext(patch("audio_core.device_validation.resolve_output_device", return_value=7))
        self.enterContext(patch.object(KokoroTTSEngine, "_broadcast_pcm_to_spokes", return_value=False))
        self.notice = self.enterContext(patch.object(KokoroTTSEngine, "_report_inaudible_reply"))
        self.addAsyncCleanup(self.cleanup_audio)

    def play(self, pcm, **kw):
        self.plays.append((pcm.copy(), kw))
        self.playing.set()

    def output_stream(self, **kwargs):
        test = self

        class Stream:
            def __init__(self):
                self.kwargs = kwargs
                self.aborted = threading.Event()
                self.worker = None
                self.closed = False
                self.rendered = []
                self.native_threads = [threading.get_ident()]

            def pump(self):
                try:
                    while not self.aborted.is_set():
                        frames = np.zeros((8, 1), dtype=np.float32)
                        try:
                            kwargs["callback"](frames, len(frames), None, None)
                        finally:
                            self.rendered.extend(frames[:, 0])
                        test.playing.set()
                        while not test.release_audio.wait(0.002):
                            if self.aborted.is_set():
                                return
                except test.sd.CallbackStop:
                    pass
                except test.sd.CallbackAbort:
                    test.stopped.set()
                finally:
                    kwargs["finished_callback"]()

            def start(self):
                self.native_threads.append(threading.get_ident())
                test.plays.append(self)
                self.worker = threading.Thread(target=self.pump, daemon=True)
                self.worker.start()

            def abort(self):
                self.native_threads.append(threading.get_ident())
                self.aborted.set()
                test.stopped.set()
                self.worker.join(1)
                test.assertFalse(self.worker.is_alive())

            def close(self):
                self.native_threads.append(threading.get_ident())
                if self.worker is not None:
                    self.worker.join(1)
                    test.assertFalse(self.worker.is_alive(), "Close raced live audio work")
                self.closed = True

        stream = Stream()
        self.streams.append(stream)
        return stream

    async def cleanup_audio(self):
        self.release_audio.set()
        for stream in self.streams:
            if stream.worker is not None:
                await asyncio.to_thread(stream.worker.join, 1)
        await asyncio.sleep(0)

    async def until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0)

        await asyncio.wait_for(wait(), 2)

    async def test_stop_does_not_wait_for_native_work_and_retires_its_result(self):
        entered, release = threading.Event(), threading.Event()
        original = self.model.create.side_effect

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Synthetic native worker was not released")
            return np.ones(24, dtype=np.float32), 24000

        self.model.create.side_effect = blocked
        task = asyncio.create_task(self.engine.synthesize("First."))
        await self.until(entered.is_set)
        stopping = asyncio.create_task(asyncio.to_thread(self.engine.stop))
        try:
            done, _ = await asyncio.wait({stopping}, timeout=0.1)
            self.assertIn(stopping, done, "stop() blocks on the running native inference")
            self.assertFalse(self.engine._synthesis_work["done"])
        finally:
            release.set()
            await stopping
            result = await task
            self.model.create.side_effect = original
        self.assertEqual(result, b"", "Stopped native synthesis must never return stale PCM")
        self.assertIs(self.engine._kokoro, self.model, "Stop must not replace or unload a live native owner")
        self.assertTrue(await self.engine.synthesize("Fresh."))

    async def test_stop_ceases_active_audio(self):
        task = asyncio.create_task(self.facade.speak("First."))
        try:
            await self.until(self.playing.is_set)
            self.facade.stop()
            await self.until(self.stopped.is_set)
            await asyncio.wait_for(task, 1)
            self.assertTrue(self.streams[0].closed)
            self.assertEqual(len(set(self.streams[0].native_threads)), 1)
            self.assertNotIn(threading.get_ident(), self.streams[0].native_threads)
            self.sd.stop.assert_not_called()
            self.notice.assert_not_called()
            self.release_audio.set()
            await self.facade.speak("Fresh.")
            self.assertEqual(len(self.plays), 2)
            self.assertEqual(self.monitor_states, [True, False, True, False])
        finally:
            self.release_audio.set()
            await task

    async def test_cancelled_speak_stops_its_worker_and_recovers(self):
        task = asyncio.create_task(self.facade.speak("First."))
        try:
            await self.until(self.playing.is_set)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await self.until(self.stopped.is_set)
            self.release_audio.set()
            await self.facade.speak("Fresh.")
            self.assertEqual(len(self.plays), 2)
            self.sd.stop.assert_not_called()
        finally:
            self.release_audio.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_stop_waiting_engine_never_stops_another_engine(self):
        other = KokoroTTSEngine(config=self.config, volume=1.0)
        other._kokoro = self.model
        first = asyncio.create_task(self.engine.speak("First."))
        second = None
        try:
            await self.until(self.playing.is_set)
            second = asyncio.create_task(other.speak("Queued."))
            await self.until(lambda: self.model.create.call_count == 2)
            other.stop()
            self.assertFalse(self.stopped.is_set())
            self.assertFalse(first.done())
            self.release_audio.set()
            await asyncio.gather(first, second)
            self.assertEqual(len(self.plays), 1, "Stopped queued PCM reached an output")
            await other.speak("Fresh.")
            self.assertEqual(len(self.plays), 2)
            self.sd.stop.assert_not_called()
        finally:
            self.release_audio.set()
            await asyncio.gather(*(t for t in (first, second) if t), return_exceptions=True)

    async def test_muting_active_audio_aborts_only_owned_stream_and_recovers(self):
        task = asyncio.create_task(self.engine.speak("First."))
        try:
            await self.until(self.playing.is_set)
            self.config.tts_enabled = False
            await self.until(self.stopped.is_set)
            await task
            self.config.tts_enabled = True
            self.release_audio.set()
            await self.engine.speak("Fresh.")
            self.assertEqual(len(self.plays), 2)
            self.sd.stop.assert_not_called()
        finally:
            self.release_audio.set()
            await task

    async def test_stop_retires_all_remaining_stream_tokens_but_keeps_history(self):
        self.release_audio.set()

        async def chunks():
            yield "First."
            self.engine.stop()
            yield "Retired."
            yield "Still retired."

        self.assertEqual(await self.facade.speak_streaming(chunks()), "First.Retired.Still retired.")
        self.assertEqual(self.model.create.call_count, 1)
        self.assertEqual(len(self.plays), 1)
        await self.facade.speak("Fresh.")
        self.assertEqual(len(self.plays), 2)

    async def test_stop_during_stream_policy_observation_cannot_adopt_new_generation(self):
        self.release_audio.set()
        enabled = self.engine._tts_is_enabled
        stopped = False

        def stop_during_policy_check():
            nonlocal stopped
            if not stopped:
                stopped = True
                self.engine.stop()
            return enabled()

        async def chunks():
            yield "Retired."
            yield "Also retired."

        with patch.object(self.engine, "_tts_is_enabled", side_effect=stop_during_policy_check):
            self.assertEqual(await self.facade.speak_streaming(chunks()), "Retired.Also retired.")
        self.assertEqual(self.plays, [])
        self.model.create.assert_not_called()
        await self.facade.speak("Fresh.")
        self.assertEqual(len(self.plays), 1)

    async def test_stop_discards_earlier_chunked_pcm(self):
        count = 0

        def synthesize(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                self.engine.stop()
            return np.ones(24, dtype=np.float32), 24000

        self.model.create.side_effect = synthesize
        text = "Hello music welcome friend " * 5 + ". " + "Hello music welcome friend " * 5 + "."
        self.assertEqual(await self.engine.synthesize(text), b"")
        self.assertEqual(count, 2)

    async def check_path_stop(self, path):
        if path == "cached":
            self.engine._lookup_opener_cache = lambda _: np.ones(24, dtype=np.int16).tobytes()
            operation = self.facade.speak("Okay!")
        elif path == "long":
            text = "Hello music welcome friend " * 5 + ". " + "Hello music welcome friend " * 5 + "."
            operation = self.facade.speak(text)
        else:

            async def chunks():
                yield "First. Second. "
                yield "Retired."

            operation = self.facade.speak_streaming(chunks())
        task = asyncio.create_task(operation)
        try:
            await self.until(self.playing.is_set)
            self.facade.stop()
            await self.until(self.stopped.is_set)
            await asyncio.wait_for(task, 1)
            self.assertEqual(len(self.plays), 1)
            self.release_audio.set()
            await self.facade.speak("Fresh.")
            self.assertEqual(len(self.plays), 2)
            self.sd.stop.assert_not_called()
            self.notice.assert_not_called()
        finally:
            self.release_audio.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_cached_opener_stop_and_recovery(self):
        await self.check_path_stop("cached")

    async def test_long_prefetched_speech_stop_and_recovery(self):
        await self.check_path_stop("long")

    async def test_llm_prefetched_speech_stop_and_recovery(self):
        await self.check_path_stop("llm")

    async def test_cancelled_waiting_utterance_does_not_stop_current_owner(self):
        first = asyncio.create_task(self.facade.speak("First."))
        queued = None
        try:
            await self.until(self.playing.is_set)
            queued = asyncio.create_task(self.facade.speak("Queued."))
            await asyncio.sleep(0.02)
            queued.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued
            self.assertFalse(self.stopped.is_set())
            self.assertFalse(first.done())
            self.release_audio.set()
            await first
            self.assertEqual(len(self.plays), 1)
        finally:
            self.release_audio.set()
            await asyncio.gather(*(task for task in (first, queued) if task), return_exceptions=True)

    async def test_output_open_failure_recovers_on_next_utterance_without_fallback(self):
        self.sd.OutputStream.side_effect = OSError("synthetic output unplugged")
        await self.facade.speak("Unplugged.")
        self.assertEqual(self.sd.OutputStream.call_count, 1)
        self.assertEqual(self.plays, [])
        self.assertIsNone(self.engine._playback_cancel)
        self.notice.assert_called_once_with()
        self.sd.play.assert_not_called()
        self.sd.stop.assert_not_called()
        self.sd.OutputStream.side_effect = self.output_stream
        self.release_audio.set()
        await self.facade.speak("Fresh.")
        self.assertEqual(len(self.plays), 1)
        self.assertEqual(self.sd.OutputStream.call_count, 2)
        self.assertEqual(self.streams[0].kwargs["device"], 7)

    async def test_stop_during_device_open_prevents_start_and_spoke_retry(self):
        opening, release = threading.Event(), threading.Event()
        broadcast = self.engine._broadcast_pcm_to_spokes

        def slow_open(**kwargs):
            opening.set()
            if not release.wait(2):
                raise AssertionError("Synthetic device open was not released")
            return self.output_stream(**kwargs)

        self.sd.OutputStream.side_effect = slow_open
        task = asyncio.create_task(self.facade.speak("First."))
        try:
            await self.until(opening.is_set)
            self.engine.stop()
            release.set()
            await asyncio.wait_for(task, 1)
            self.assertEqual(self.plays, [])
            self.assertTrue(self.streams[0].closed)
            broadcast.assert_not_called()
            self.notice.assert_not_called()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_disable_during_device_open_prevents_start_and_broadcast(self):
        self.release_audio.set()

        def disabled_open(**kwargs):
            self.config.tts_enabled = False
            return self.output_stream(**kwargs)

        self.sd.OutputStream.side_effect = disabled_open
        await self.facade.speak("Retired.")
        self.assertEqual(self.plays, [])
        self.assertTrue(self.streams[0].closed)
        self.engine._broadcast_pcm_to_spokes.assert_not_called()
        self.notice.assert_not_called()
        self.sd.stop.assert_not_called()
        self.config.tts_enabled = True
        self.sd.OutputStream.side_effect = self.output_stream
        await self.facade.speak("Fresh.")
        self.assertEqual(len(self.plays), 1)

    async def test_disable_inside_native_start_aborts_callback_before_pcm(self):
        self.release_audio.set()

        def disabling_stream(**kwargs):
            stream = self.output_stream(**kwargs)
            start = stream.start

            def disabled_start():
                self.config.tts_enabled = False
                start()

            stream.start = disabled_start
            return stream

        self.sd.OutputStream.side_effect = disabling_stream
        await self.facade.speak("Retired.")
        self.assertTrue(self.streams[0].closed)
        self.assertFalse(np.any(self.streams[0].rendered), "Callback rendered PCM after native start disabled speech")
        self.engine._broadcast_pcm_to_spokes.assert_not_called()
        self.notice.assert_not_called()
        self.sd.stop.assert_not_called()

    async def test_disable_between_audio_callbacks_zeros_all_remaining_output(self):
        self.release_audio.set()

        def disabling_stream(**kwargs):
            callback = kwargs["callback"]
            count = 0

            def disabled_callback(*args):
                nonlocal count
                count += 1
                if count == 2:
                    self.config.tts_enabled = False
                return callback(*args)

            kwargs["callback"] = disabled_callback
            stream = self.output_stream(**kwargs)
            start = stream.start

            def synchronous_start():
                start()
                stream.worker.join(1)
                self.assertFalse(stream.worker.is_alive())

            stream.start = synchronous_start
            return stream

        self.sd.OutputStream.side_effect = disabling_stream
        await self.facade.speak("First partial output.")
        self.assertTrue(np.any(self.streams[0].rendered[:8]))
        self.assertFalse(np.any(self.streams[0].rendered[8:]), "A post-disable callback leaked remaining PCM")
        self.engine._broadcast_pcm_to_spokes.assert_not_called()
        self.notice.assert_not_called()

    async def test_disable_at_immediate_completion_prevents_spoke_delivery(self):
        self.release_audio.set()

        def completing_stream(**kwargs):
            stream = self.output_stream(**kwargs)
            start = stream.start

            def synchronous_start():
                start()
                stream.worker.join(1)
                self.assertFalse(stream.worker.is_alive())
                self.config.tts_enabled = False

            stream.start = synchronous_start
            return stream

        self.sd.OutputStream.side_effect = completing_stream
        await self.facade.speak("Completed locally before disable.")
        self.assertTrue(np.any(self.streams[0].rendered))
        self.engine._broadcast_pcm_to_spokes.assert_not_called()
        self.notice.assert_not_called()

    async def test_disable_during_unsuccessful_broadcast_prevents_final_retry(self):
        self.release_audio.set()
        broadcast = self.engine._broadcast_pcm_to_spokes

        def disabled_broadcast(*args):
            self.config.tts_enabled = False
            return False

        broadcast.side_effect = disabled_broadcast
        await self.facade.speak("First.")
        self.assertEqual(broadcast.call_count, 1, "Retired PCM was retried on spokes")
        self.assertTrue(self.streams[0].closed)
        self.notice.assert_not_called()
        self.sd.stop.assert_not_called()

    async def test_disable_with_device_open_failure_has_no_spoke_fallback_or_warning(self):
        def failed_open(**kwargs):
            self.config.tts_enabled = False
            raise OSError("synthetic device open failed after speech was disabled")

        self.sd.OutputStream.side_effect = failed_open
        await self.facade.speak("Retired.")
        self.engine._broadcast_pcm_to_spokes.assert_not_called()
        self.notice.assert_not_called()
        self.assertIsNone(self.engine._playback_cancel)
        self.sd.stop.assert_not_called()

    async def test_capture_thread_barge_in_reaches_facade_and_owned_audio(self):
        from core.voice_command_handler import VoiceCommandHandler

        class Capture:
            mode = "paused"
            recording = b"synthetic recording"

            def set_mode(self, mode, on_interrupt=None):
                self.mode = mode
                if on_interrupt is not None:
                    self.interrupt = on_interrupt

        capture = Capture()
        handler = VoiceCommandHandler.__new__(VoiceCommandHandler)
        handler.tts = self.facade
        handler._record_voice_event = AsyncMock()
        task = asyncio.create_task(handler._speak_response_interruptible("First.", capture, user_id="synthetic-user"))
        try:
            await self.until(self.playing.is_set)
            await asyncio.to_thread(capture.interrupt)
            self.assertTrue(await asyncio.wait_for(task, 1))
            await self.until(self.stopped.is_set)
            self.assertEqual(capture.mode, "paused")
            self.assertEqual(capture.recording, b"synthetic recording")
            handler._record_voice_event.assert_awaited_once()
            self.sd.stop.assert_not_called()
            self.release_audio.set()
            await self.facade.speak("Fresh.")
            self.assertEqual(len(self.plays), 2)
        finally:
            self.engine.stop()
            self.release_audio.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def test_facade_forwards_stop(self):
        with patch.object(self.engine, "stop") as stop:
            self.facade.stop()
            stop.assert_called_once_with()

    def test_stop_without_active_speech_does_not_touch_music(self):
        self.engine.stop()
        self.engine.stop()
        self.sd.stop.assert_not_called()
        self.sd.OutputStream.assert_not_called()
        self.assertIs(self.engine._kokoro, self.model)

    def test_facade_missing_optional_stop_is_safe(self):
        self.facade._impl = None
        self.facade.stop()
        self.facade._impl = object()
        self.facade.stop()


if __name__ == "__main__":
    unittest.main()
