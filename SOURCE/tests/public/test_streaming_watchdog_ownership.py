"""Bounded synthetic workers exercise the real facade and synthesis ownership."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
import unittest

from tests.public import test_disabled_tts_streaming as policy_fixture


class StreamingWatchdogOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        policy_fixture.DisabledTTSStreamingTests.setUp(self)
        self.scope = self.engine._run_synthesize_with_watchdog.__globals__
        self.assertEqual(self.scope["STREAM_TIMEOUT_SECONDS"], 30.0)
        self.scope["STREAM_TIMEOUT_SECONDS"] = 0.04
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.started_text = []
        original = self.engine._synthesize_internal

        def blocking(text, *args, **kwargs):
            self.started_text.append(text)
            self.started.set()
            try:
                if not self.release.wait(5):
                    raise AssertionError("Synthetic test worker was not released")
                return original(text, *args, **kwargs)
            finally:
                self.finished.set()

        self.engine._synthesize_internal = blocking
        self.addAsyncCleanup(self.release_worker)

    async def release_worker(self):
        self.release.set()
        if self.started.is_set():
            self.assertTrue(await asyncio.to_thread(self.finished.wait, 2))
        await asyncio.sleep(0)

    async def until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0)

        await asyncio.wait_for(wait(), 2)

    async def timeout_path(self, path):
        async def chunks():
            yield "First."

        if path == "synthesize":
            operation = self.engine.synthesize("First.")
        elif path == "short":
            operation = self.facade.speak("First.")
        elif path == "long":
            operation = self.facade.speak(self.long_text)
        else:
            operation = self.facade.speak_streaming(chunks())
        task = asyncio.create_task(operation)
        await self.until(self.started.is_set)
        done, _ = await asyncio.wait({task}, timeout=0.5)
        if task not in done:
            task.cancel()
            await self.release_worker()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.assertIn(task, done, "Speech waiter bypassed the existing deadline")
        result = await task
        if path == "synthesize":
            self.assertEqual(result, b"")
        if path == "llm":
            self.assertEqual(result, "First.")
        self.assertFalse(self.finished.is_set(), "Waiter timeout must not be represented as worker termination")
        self.assertEqual(self.plays, [])
        self.assertEqual(len(self.started_text), 1)
        await self.release_worker()
        self.assertEqual(self.plays, [], "A late result escaped the retired waiter")

    async def test_raw_synthesis_waiter_deadline_keeps_worker_ownership(self):
        await self.timeout_path("synthesize")

    async def test_short_speech_waiter_deadline_keeps_worker_ownership(self):
        await self.timeout_path("short")

    async def test_long_speech_waiter_deadline_keeps_worker_ownership(self):
        await self.timeout_path("long")

    async def test_llm_stream_waiter_deadline_keeps_worker_ownership(self):
        await self.timeout_path("llm")

    async def test_repeated_timeouts_never_replace_a_live_worker_or_accumulate_waiters(self):
        first = asyncio.create_task(self.engine.synthesize("First."))
        await self.until(self.started.is_set)
        self.assertEqual(await asyncio.wait_for(first, 0.5), b"")
        work = self.engine._synthesis_work
        for _ in range(3):
            self.assertEqual(await asyncio.wait_for(self.engine.synthesize("Queued."), 0.5), b"")
            self.assertIs(self.engine._synthesis_work, work)
            self.assertEqual(work["waiters"], set())
            self.assertEqual(self.started_text, ["First."])
        await self.release_worker()
        self.assertTrue(await self.engine.synthesize("Fresh."))
        self.assertEqual(self.started_text, ["First.", "Fresh."])

    async def test_cancelled_owner_leaves_worker_owned_and_next_request_can_recover(self):
        task = asyncio.create_task(self.facade.speak("First."))
        await self.until(self.started.is_set)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        self.assertFalse(self.finished.is_set())
        self.assertFalse(self.engine._speak_lock.locked())
        self.assertEqual(await self.engine.synthesize("Queued."), b"")
        self.assertEqual(self.started_text, ["First."])
        await self.release_worker()
        await self.facade.speak("Fresh.")
        self.assertEqual(len(self.plays), 1)
        self.assertEqual(self.started_text, ["First.", "Fresh."])

    async def test_normal_concurrent_request_waits_then_runs_without_replacing_owner(self):
        self.scope["STREAM_TIMEOUT_SECONDS"] = 1.0
        first = asyncio.create_task(self.engine.synthesize("First."))
        await self.until(self.started.is_set)
        second = asyncio.create_task(self.engine.synthesize("Second."))
        await self.until(lambda: second.done() or len(self.engine._synthesis_work["waiters"]) == 1)
        self.assertFalse(second.done(), "Enabled concurrent synthesis was dropped rather than serialized")
        self.assertEqual(self.started_text, ["First."])
        self.release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 2)
        self.assertEqual(results, [self.pcm, self.pcm])
        self.assertEqual(self.started_text, ["First.", "Second."])

    async def test_cancelling_an_admission_waiter_does_not_cancel_the_owner(self):
        self.scope["STREAM_TIMEOUT_SECONDS"] = 1.0
        first = asyncio.create_task(self.engine.synthesize("First."))
        await self.until(self.started.is_set)
        second = asyncio.create_task(self.engine.synthesize("Second."))
        await self.until(lambda: len(self.engine._synthesis_work["waiters"]) == 1)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        self.assertFalse(self.engine._synthesis_work["cancelled"].is_set())
        self.assertEqual(self.engine._synthesis_work["waiters"], set())
        self.release.set()
        self.assertEqual(await asyncio.wait_for(first, 2), self.pcm)
        self.assertEqual(self.started_text, ["First."])

    async def test_admission_timeout_does_not_cancel_the_owner_or_extend_its_deadline(self):
        self.scope["STREAM_TIMEOUT_SECONDS"] = 1.0
        first = asyncio.create_task(self.engine.synthesize("First."))
        await self.until(self.started.is_set)
        self.scope["STREAM_TIMEOUT_SECONDS"] = 0.04
        started = time.monotonic()
        self.assertEqual(await asyncio.wait_for(self.engine.synthesize("Second."), 0.5), b"")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(self.engine._synthesis_work["cancelled"].is_set())
        self.assertEqual(self.engine._synthesis_work["waiters"], set())
        self.release.set()
        self.assertEqual(await asyncio.wait_for(first, 2), self.pcm)
        self.assertEqual(self.started_text, ["First."])

    async def test_timed_out_worker_waiting_for_model_lock_never_enters_inference(self):
        entered = threading.Event()
        lock = threading.Lock()
        lock.acquire()

        class WaitingLock:
            def __enter__(inner):
                entered.set()
                lock.acquire()

            def __exit__(inner, *args):
                lock.release()

        self.engine._lock = WaitingLock()
        task = asyncio.create_task(self.engine.synthesize("Queued."))
        try:
            await self.until(entered.is_set)
            self.assertEqual(await asyncio.wait_for(task, 0.5), b"")
            self.assertFalse(self.engine._synthesis_work["done"])
            self.assertEqual(self.started_text, [])
        finally:
            lock.release()
            self.release.set()
        await self.until(lambda: self.engine._synthesis_work["done"])
        self.assertEqual((self.started_text, self.loads), ([], []))
        self.release.set()
        self.assertTrue(await self.engine.synthesize("Fresh."))

    async def test_timed_out_model_loading_cannot_start_inference_after_loading(self):
        loading = threading.Event()
        loaded = threading.Event()
        release_load = threading.Event()

        def load():
            loading.set()
            try:
                if not release_load.wait(5):
                    raise AssertionError("Synthetic loader not released")
                return True
            finally:
                loaded.set()

        self.engine._ensure_loaded = load
        task = asyncio.create_task(self.engine.synthesize("Queued."))
        try:
            await self.until(loading.is_set)
            self.assertEqual(await asyncio.wait_for(task, 0.5), b"")
            self.assertEqual(self.started_text, [])
        finally:
            release_load.set()
            self.release.set()
        await self.until(loaded.is_set)
        await self.until(lambda: self.engine._synthesis_work["done"])
        self.assertEqual(self.started_text, [])

    async def test_executor_cancelled_before_start_retires_slot_without_late_inference(self):
        occupied = threading.Event()
        release_executor = threading.Event()
        loop = asyncio.get_running_loop()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)

        def occupy():
            occupied.set()
            return release_executor.wait(5)

        blocker = loop.run_in_executor(None, occupy)
        await self.until(occupied.is_set)
        try:
            self.assertEqual(await asyncio.wait_for(self.engine.synthesize("Queued."), 0.5), b"")
            await self.until(lambda: self.engine._synthesis_work["done"])
            self.assertEqual(self.started_text, [])
        finally:
            release_executor.set()
        await blocker
        self.release.set()
        self.assertTrue(await self.engine.synthesize("Fresh."))
        self.assertEqual(self.started_text, ["Fresh."])

    async def test_cancelled_llm_stream_retires_its_active_prefetch(self):
        self.scope["STREAM_TIMEOUT_SECONDS"] = 1.0
        second_started = threading.Event()
        release_second = threading.Event()
        playback_started = threading.Event()
        release_playback = threading.Event()
        original = self.engine._synthesize_internal

        def synthesize(text, *args, **kwargs):
            if text == "Second.":
                second_started.set()
                if not release_second.wait(5):
                    raise AssertionError("Second worker was not released")
            self.release.set()
            return original(text, *args, **kwargs)

        def playback(*args, **kwargs):
            playback_started.set()
            if not release_playback.wait(5):
                raise AssertionError("Playback fixture was not released")
            return True

        self.engine._synthesize_internal = synthesize
        self.engine._play_pcm_raw = playback

        async def chunks():
            yield "First. Second. "

        task = asyncio.create_task(self.facade.speak_streaming(chunks()))
        try:
            await self.until(lambda: second_started.is_set() and playback_started.is_set())
            active = self.engine._synthesis_work
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 0.5)
            self.assertTrue(active["cancelled"].is_set(), "Cancelled stream retained an eligible prefetched result")
            self.assertFalse(active["done"], "Active prefetched worker was reported terminated")
        finally:
            release_second.set()
            release_playback.set()
        await self.until(lambda: active["done"])

    async def test_cancelled_long_stream_retires_its_active_prefetch(self):
        self.scope["STREAM_TIMEOUT_SECONDS"] = 1.0
        second_started = threading.Event()
        release_second = threading.Event()
        playback_started = threading.Event()
        release_playback = threading.Event()
        original = self.engine._synthesize_internal

        def synthesize(text, *args, **kwargs):
            if self.started_text:
                second_started.set()
                if not release_second.wait(5):
                    raise AssertionError("Second worker was not released")
            self.release.set()
            return original(text, *args, **kwargs)

        def playback(*args, **kwargs):
            playback_started.set()
            if not release_playback.wait(5):
                raise AssertionError("Playback fixture was not released")
            return True

        self.engine._synthesize_internal = synthesize
        self.engine._play_pcm_raw = playback

        task = asyncio.create_task(self.facade.speak(self.long_text))
        try:
            await self.until(lambda: second_started.is_set() and playback_started.is_set())
            active = self.engine._synthesis_work
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 0.5)
            self.assertTrue(active["cancelled"].is_set(), "Cancelled stream retained an eligible prefetched result")
            self.assertFalse(active["done"], "Active prefetched worker was reported terminated")
        finally:
            release_second.set()
            release_playback.set()
        await self.until(lambda: active["done"])

    async def test_executor_submission_failure_retires_slot_and_preserves_the_error(self):
        loop = asyncio.get_running_loop()
        unavailable = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        unavailable.shutdown(wait=True)
        loop.set_default_executor(unavailable)
        self.release.set()
        try:
            with self.assertRaisesRegex(RuntimeError, "shutdown"):
                await self.engine.synthesize("Rejected submission.")
            await asyncio.sleep(0)
            self.assertTrue(
                self.engine._synthesis_work["done"], "Unstarted failed submission permanently owns the slot"
            )
            self.assertEqual(self.started_text, [])
        finally:
            loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=1))
        self.assertTrue(await self.engine.synthesize("Fresh."))
        self.assertEqual(self.started_text, ["Fresh."])


if __name__ == "__main__":
    unittest.main()
