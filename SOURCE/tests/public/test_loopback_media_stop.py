"""Execute loopback teardown and the real watchdog with inert media boundaries.

The selected method bodies are compiled unchanged from source, as in the
carrier-adapter contracts. This is lifecycle source qualification, not a real
Pipecat/audio/model or carrier call. No optional model imports are needed.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import time
import unittest
from contextlib import suppress
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[2]


def _lifecycle_namespace():
    namespace = {
        "asyncio": asyncio,
        "suppress": suppress,
        "datetime": datetime,
        "UTC": UTC,
        "Enum": Enum,
        "time": time,
        "logger": logging.getLogger(__name__),
        "_PHONE_WS_DISCONNECT_HANGUP_GRACE_SECS": 0.0,
        "_record_needs_telnyx_hangup": lambda record: bool(record.telnyx_call_control_id),
        "_async_post_telnyx_hangup": AsyncMock(side_effect=AssertionError("No carrier request is allowed")),
    }
    selections = [
        (
            "telephony/call_manager.py",
            {"CallStatus"},
            {"CallManager": {"_watch_telnyx_ws_disconnect"}},
        ),
        (
            "telephony/loopback_phone_call_session.py",
            {
                "_TURN_WAIT_TIMEOUT_SECONDS",
                "_TEE_DRAIN_SECONDS",
                "_TERMINAL_CALL_STATUSES",
                "_LoopbackStoppedMediaTransport",
            },
            {
                "LoopbackCallManager": {"end_call"},
                "LoopbackPhoneCallSession": {
                    "stop",
                    "_stop_once",
                    "simulate_media_stream_stop_after_first_frame",
                    "simulate_recipient_speech",
                    "_call_has_ended",
                },
            },
        ),
    ]
    for relative, names, methods in selections:
        source = ROOT / relative
        tree = ast.parse(source.read_text(encoding="utf-8"))
        body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in names for target in node.targets
            ):
                body.append(node)
            elif isinstance(node, ast.ClassDef):
                if node.name in names:
                    body.append(node)
                elif node.name in methods:
                    node.body = [
                        member
                        for member in node.body
                        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and member.name in methods[node.name]
                    ]
                    body.append(node)
        exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(source), "exec"), namespace)
    return namespace


class LoopbackMediaStopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ns = _lifecycle_namespace()
        self.status = self.ns["CallStatus"]
        self.drained = asyncio.Event()
        self.runner = asyncio.create_task(self.drained.wait())
        self.addAsyncCleanup(self.cleanup_runner)
        self.pipeline = SimpleNamespace(stop_when_done=AsyncMock(side_effect=self.drained.set), cancel=AsyncMock())
        self.record = SimpleNamespace(
            call_id="synthetic-call",
            user_id="synthetic-owner",
            status=self.status.ACTIVE,
            error=None,
            started_at=datetime.now(tz=UTC),
            ended_at=None,
            media_stopped_at=None,
            telnyx_call_control_id=None,
            recording_enabled=False,
            disclosure_spoken=False,
            disclosure_text_confirmed=False,
            recording_started_after_disclosure=False,
            recording_paths={},
            _pipeline_task=self.pipeline,
        )
        manager = self.ns["LoopbackCallManager"]()
        manager._active_calls = {self.record.call_id: self.record}
        manager._call_records = {self.record.call_id: self.record}
        manager._call_tasks = {self.record.call_id: self.runner}
        session = self.ns["LoopbackPhoneCallSession"]()
        session.record = self.record
        session.call_manager = manager
        session._pipeline_task = self.pipeline
        session._runner_task = self.runner
        session._stop_lock = asyncio.Lock()
        session._stop_complete = False
        session._stop_trace_complete = False
        session._recording_storage_paths = []
        session._recorded_audio = bytearray(b"synthetic-pcm")
        for name in (
            "_receptionist_bot",
            "_receptionist_speech_injector",
            "_receptionist_pipeline_task",
            "_receptionist_runner_task",
            "_opening_settle_task",
            "_phone_latency_trace",
            "_hold_handler",
            "_voicemail_handler",
            "_task_trace",
        ):
            setattr(session, name, None)
        for name in (
            "_dispose_event_subscriptions",
            "_restore_event_bus",
            "_mark_loopback_disclosure_if_spoken",
            "_append_phone_trace_event",
            "_append_trace_complete",
            "_publish_lifecycle",
        ):
            setattr(session, name, Mock())
        session._subscriptions = []
        session.recorded_audio = session._recorded_audio
        self.session = session

    async def cleanup_runner(self):
        self.runner.cancel()
        with suppress(asyncio.CancelledError):
            await self.runner

    async def test_clean_stop_drains_runner_completes_and_releases_slot(self):
        await self.session.simulate_media_stream_stop_after_first_frame()
        assert self.record.status == self.status.COMPLETED
        assert self.record.error is None
        assert self.record.media_stopped_at is not None
        assert self.record.ended_at is not None
        assert self.runner.done()
        assert not self.runner.cancelled()
        assert self.record.call_id not in self.session.call_manager._active_calls
        assert self.record.call_id not in self.session.call_manager._call_tasks
        self.session._dispose_event_subscriptions.assert_called_once()
        self.session._restore_event_bus.assert_called_once()
        self.ns["_async_post_telnyx_hangup"].assert_not_awaited()

    async def test_abnormal_loss_stays_failed_and_rejects_later_speech(self):
        await self.session.simulate_media_stream_stop_after_first_frame(reason="websocket_closed")
        assert self.record.status == self.status.FAILED
        assert "websocket_closed" in self.record.error
        assert self.record.media_stopped_at is not None
        assert self.record.call_id not in self.session.call_manager._active_calls
        await self.session.simulate_recipient_speech("Must not reach a model")
        await self.session.stop()
        assert self.runner.done()
        assert self.record.status == self.status.FAILED

    async def test_repeated_clean_stop_keeps_terminal_state_and_rejects_speech(self):
        self.enable_terminal_effects()
        await self.session.simulate_media_stream_stop_after_first_frame()
        stopped_at = self.record.media_stopped_at
        ended_at = self.record.ended_at
        duration = self.record.duration_seconds
        await self.session.simulate_media_stream_stop_after_first_frame()
        await self.session.stop()  # The caller's ordinary finally block.
        assert self.record.status == self.status.COMPLETED
        await self.session.simulate_recipient_speech("Must not reach a model")
        assert self.record.media_stopped_at == stopped_at
        assert self.record.ended_at == ended_at
        assert self.record.duration_seconds == duration
        self.assert_single_terminal_effects()
        assert self.runner.done()
        self.ns["_async_post_telnyx_hangup"].assert_not_awaited()

    def enable_terminal_effects(self):
        self.record.recording_enabled = True
        self.record.disclosure_spoken = True
        self.storage = SimpleNamespace(save=AsyncMock(return_value="synthetic-recording.wav"))
        self.session.call_manager._recording_storage = self.storage
        self.session._phone_latency_trace = SimpleNamespace(complete=Mock())
        self.session._task_trace = SimpleNamespace(flush=Mock())

    def assert_single_terminal_effects(self):
        self.storage.save.assert_awaited_once()
        self.session._publish_lifecycle.assert_called_once_with("completed")
        self.session._append_trace_complete.assert_called_once()
        self.session._phone_latency_trace.complete.assert_called_once()
        self.session._task_trace.flush.assert_called_once()
        assert self.session._recording_storage_paths == ["synthetic-recording.wav"]
        assert self.record.recording_paths == {"loopback": "synthetic-recording.wav"}

    async def test_concurrent_stops_finalize_once(self):
        self.enable_terminal_effects()
        await asyncio.gather(self.session.stop(), self.session.stop(), self.session.stop())
        self.assert_single_terminal_effects()
        assert self.session._stop_complete
        assert self.runner.done()

    async def test_failed_cleanup_is_retryable_without_false_completion(self):
        self.enable_terminal_effects()
        self.session._receptionist_bot = SimpleNamespace(stop=AsyncMock(side_effect=[RuntimeError("cleanup"), None]))
        with self.assertRaisesRegex(RuntimeError, "cleanup"):  # noqa: PT027 - stdlib public contracts
            await self.session.stop()
        assert not self.session._stop_complete
        self.storage.save.assert_not_awaited()
        self.session._publish_lifecycle.assert_not_called()
        await self.session.stop()
        self.assert_single_terminal_effects()
        assert self.session._stop_complete

    async def test_failed_recording_save_retries_with_stable_end_time(self):
        self.enable_terminal_effects()
        self.storage.save.side_effect = [RuntimeError("recording"), "synthetic-recording.wav"]
        with self.assertRaisesRegex(RuntimeError, "recording"):  # noqa: PT027 - stdlib public contracts
            await self.session.stop()
        ended_at = self.record.ended_at
        duration = self.record.duration_seconds
        assert not self.session._stop_complete
        self.session._append_trace_complete.assert_not_called()
        self.session._phone_latency_trace.complete.assert_not_called()
        await self.session.stop()
        await self.session.stop()
        assert self.storage.save.await_count == 2  # One failed attempt, one successful save.
        self.session._publish_lifecycle.assert_called_once_with("completed")
        self.session._append_trace_complete.assert_called_once()
        self.session._phone_latency_trace.complete.assert_called_once()
        assert self.record.ended_at == ended_at
        assert self.record.duration_seconds == duration
        assert self.session._recording_storage_paths == ["synthetic-recording.wav"]

    async def test_late_failure_does_not_repeat_saved_recording_or_trace(self):
        self.enable_terminal_effects()
        self.session._publish_lifecycle.side_effect = [RuntimeError("publication"), None]
        with self.assertRaisesRegex(RuntimeError, "publication"):  # noqa: PT027 - stdlib public contracts
            await self.session.stop()
        ended_at = self.record.ended_at
        assert not self.session._stop_complete
        await self.session.stop()
        await self.session.stop()
        self.storage.save.assert_awaited_once()
        self.session._append_trace_complete.assert_called_once()
        self.session._phone_latency_trace.complete.assert_called_once()
        self.session._task_trace.flush.assert_called_once()
        assert self.session._publish_lifecycle.call_count == 2  # Failed attempt plus successful retry.
        assert self.record.ended_at == ended_at
        assert self.session._stop_complete

    async def test_cancelled_cleanup_remains_retryable(self):
        self.enable_terminal_effects()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_stop():
            entered.set()
            await release.wait()

        self.session._receptionist_bot = SimpleNamespace(stop=blocked_stop)
        task = asyncio.create_task(self.session.stop())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):  # noqa: PT027 - stdlib public contracts
            await task
        assert not self.session._stop_complete
        self.storage.save.assert_not_awaited()
        release.set()
        await self.session.stop()
        self.assert_single_terminal_effects()

    async def test_cancelled_runner_drain_does_not_cancel_shared_runner_or_finalize(self):
        self.enable_terminal_effects()
        draining = asyncio.Event()
        self.pipeline.stop_when_done.side_effect = draining.set
        task = asyncio.create_task(self.session.stop())
        await draining.wait()
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):  # noqa: PT027 - stdlib public contracts
            await task
        assert not self.session._stop_complete
        assert not self.runner.done()
        self.storage.save.assert_not_awaited()
        self.session._publish_lifecycle.assert_not_called()
        self.session._dispose_event_subscriptions.assert_called_once()
        self.session._restore_event_bus.assert_called_once()
        self.drained.set()
        await self.session.stop()
        self.assert_single_terminal_effects()
        assert self.runner.done()

    async def test_stop_before_start_does_not_latch_a_future_session(self):
        self.session.record = None
        await self.session.stop()
        assert not self.session._stop_complete
        self.session.record = self.record
        self.enable_terminal_effects()
        await self.session.stop()
        self.assert_single_terminal_effects()

    async def test_cancelled_owner_finally_still_finishes_cleanup(self):
        self.enable_terminal_effects()
        started = asyncio.Event()

        async def owner():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                await self.session.stop()

        task = asyncio.create_task(owner())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):  # noqa: PT027 - stdlib public contracts
            await task
        assert self.session._stop_complete
        assert self.runner.done()
        self.assert_single_terminal_effects()

    async def test_new_cancellation_interrupts_cancelled_owners_finally_and_can_retry(self):
        self.enable_terminal_effects()
        started = asyncio.Event()
        draining = asyncio.Event()
        self.pipeline.stop_when_done.side_effect = draining.set

        async def owner():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                await self.session.stop()

        task = asyncio.create_task(owner())
        await started.wait()
        task.cancel()
        await draining.wait()
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):  # noqa: PT027 - stdlib public contracts
            await task
        assert not self.session._stop_complete
        assert not self.runner.done()
        self.storage.save.assert_not_awaited()
        self.session._publish_lifecycle.assert_not_called()
        self.session._dispose_event_subscriptions.assert_called_once()
        self.session._restore_event_bus.assert_called_once()
        self.drained.set()
        await self.session.stop()
        self.assert_single_terminal_effects()

    async def test_clean_stop_does_not_overwrite_an_existing_failure(self):
        self.record.status = self.status.FAILED
        self.record.error = "original failure"
        await self.session.simulate_media_stream_stop_after_first_frame()
        assert self.record.status == self.status.FAILED
        assert self.record.error == "original failure"
        assert self.runner.done()

    async def test_no_audio_timeout_does_not_claim_a_call_end(self):
        self.session._recorded_audio.clear()
        with self.assertRaisesRegex(TimeoutError, "first outbound"):  # noqa: PT027 - stdlib public contracts
            await self.session.simulate_media_stream_stop_after_first_frame(timeout=0)
        assert self.record.status == self.status.ACTIVE
        assert self.record.media_stopped_at is None
        self.pipeline.stop_when_done.assert_not_awaited()

    async def test_cancelled_wait_does_not_claim_completion(self):
        self.session._recorded_audio.clear()
        task = asyncio.create_task(self.session.simulate_media_stream_stop_after_first_frame())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):  # noqa: PT027 - stdlib public contracts
            await task
        assert self.record.status == self.status.ACTIVE
        assert not self.runner.done()
        self.pipeline.stop_when_done.assert_not_awaited()

    async def test_start_is_required(self):
        self.session.record = None
        with self.assertRaisesRegex(RuntimeError, "start"):  # noqa: PT027 - stdlib public contracts
            await self.session.simulate_media_stream_stop_after_first_frame()


if __name__ == "__main__":
    unittest.main()
