"""Direct Queue Play binds the real cursor, engine and control method.

Only backend, provider consent and persistence boundaries are inert. No player,
HTTP listener, device, model, decoder or provider is started. Definitions are
loaded unchanged from the selected source, as in test_queue_wire_contract.py.
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import sys
import threading
import types
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.player import QueueItem
from models.state_manager import ConsolidatedState
from music.queue.playlist_cursor import PlaylistCursor

if (
    Path(sys.modules[PlaylistCursor.__module__].__file__).read_bytes()
    != (ROOT / "music/queue/playlist_cursor.py").read_bytes()
):
    raise AssertionError("Imported cursor differs from the selected source")


def definitions(relative, namespace, names=None):
    path = ROOT / relative
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    nodes.extend(
        node
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and (names is None or node.name in names)
    )
    module = types.ModuleType("_queue_selection_" + path.stem)
    module.__dict__.update(namespace)
    exec(  # noqa: S102 -- compile only definitions from the selected repository source
        compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(path), "exec"), module.__dict__
    )
    return module


# Import the phase module directly to avoid MusicPlayer's eager runtime imports.
spec = importlib.util.spec_from_file_location("_queue_selection_phase", ROOT / "music/player/playback_state.py")
phase_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(phase_module)
PlaybackPhase = phase_module.PlaybackPhase

engine_module = definitions("music/runtime/queue_engine.py", {"PlaylistCursor": PlaylistCursor, "deque": deque})
controller_module = definitions("music/controller/playback_controller.py", {})
pipeline_module = definitions("music/runtime/state_pipeline.py", {"deque": deque})
control_module = definitions(
    "music/runtime/control_surface.py",
    {
        "PlayerControlSurface": object,
        "PlaybackPhase": PlaybackPhase,
        "ConfigurationError": type("ConfigurationError", (Exception,), {}),
        "is_youtube_track_requiring_provider": Mock(return_value=False),
        "is_provider_linked": Mock(return_value=True),
        "get_youtube_unavailable_reason": Mock(return_value="Synthetic provider is not linked"),
        "record_operation": Mock(),
        "OperationType": types.SimpleNamespace(PLAYBACK="playback"),
    },
)
worker_module = definitions(
    "music/runtime/worker_manager.py", {"current_thread": threading.current_thread}, {"RuntimeWorkerManager"}
)


def item(name):
    return QueueItem(id=name, title="Synthetic " + name, url="C:\\Synthetic\\" + name + ".wav", source="local")


class QueuePlaySelection(unittest.TestCase):
    def setUp(self):
        control_module.is_youtube_track_requiring_provider.reset_mock(return_value=True)
        control_module.is_youtube_track_requiring_provider.return_value = False
        control_module.is_provider_linked.reset_mock(return_value=True)
        control_module.is_provider_linked.return_value = True
        self.a, self.b, self.c, self.d, self.old = map(item, ("a", "b", "c", "d", "old"))
        self.state = types.SimpleNamespace(now_playing=None, is_playing=False)
        self.state_service = types.SimpleNamespace(
            consolidated=ConsolidatedState(),
            state=self.state,
            invalidate_snapshot=Mock(),
            set_now_playing=lambda current, **_kwargs: setattr(self.state, "now_playing", current),
            set_is_playing=lambda value: setattr(self.state, "is_playing", value),
        )
        self.queue = engine_module.PlaylistQueueEngine(state_service=self.state_service, queue_config=None)
        self.controller = controller_module.MusicPlaybackController(
            logger=logging.getLogger(__name__),
            playlist=self.queue,
            provider_router=None,
            compliance_policy=None,
            backend_loader=None,
            metrics=None,
        )
        self.pipeline = pipeline_module.PlayerStatePipeline(
            state_service=self.state_service,
            playlist=self.queue,
            logger=logging.getLogger(__name__),
            test_mode=False,
        )
        self.cv = threading.Condition()
        self.stop = Mock(side_effect=self.assert_stop_outside_lock)
        self.player = types.SimpleNamespace(
            _controller=self.controller,
            _state=self.state,
            _pending_skip_tokens=2,
            _user_paused=False,
            _paused_current=None,
            _is_playing=False,
            _playback_sm=phase_module.PlaybackStateMachine(),
            _set_now_playing_locked=self.pipeline.set_now_playing,
            _promote_current_locked=self.pipeline.promote_current,
            _stop_backend_locked=self.stop,
            _playlist=self.queue,
            _cv=self.cv,
            _logger=logging.getLogger(__name__),
            _clear_pending_start_locked=Mock(),
            _emit=Mock(),
        )
        self.backend_manager = types.SimpleNamespace(
            schedule_pending_start=Mock(), clear_pending_start=Mock(), backend=None
        )
        self.emit = Mock()
        self.control = control_module.PlayerControlService(
            owner=self.player,
            play_command=None,
            queue_command=None,
            autoplay_service=None,
            backend_manager=self.backend_manager,
            queue_engine=self.queue,
            state_service=self.state_service,
            condition=self.cv,
            emit_callback=self.emit,
            logger=logging.getLogger(__name__),
            test_mode=False,
        )

    def assert_stop_outside_lock(self):
        self.assertFalse(self.cv._is_owned(), "Backend stop must not hold the player's condition")

    def arrange(self, current=True, phase=PlaybackPhase.PLAYING):
        self.queue.reset(self.a if current else None)
        for track in (self.b, self.c, self.d):
            self.queue.append(track)
        self.queue.replace_history([self.old])
        if current:
            self.queue.mark_playing()
            self.state.now_playing = self.a
        self.player._playback_sm.transition(phase, force=True, user_initiated=phase == PlaybackPhase.PAUSED)
        self.player._is_playing = self.state.is_playing = phase == PlaybackPhase.PLAYING
        self.player._user_paused = phase in (PlaybackPhase.PAUSED, PlaybackPhase.STOPPED)
        self.player._paused_current = self.a if self.player._user_paused else None
        self.state_service.invalidate_snapshot.reset_mock()

    def assert_selected(self, selected, upcoming, history):
        self.assertIs(self.queue.current(), selected)
        self.assertIs(self.state.now_playing, selected)
        self.assertTrue(self.queue.pending)
        self.assertEqual(self.queue.upcoming(), upcoming)
        self.assertEqual(list(self.queue.history_ref), history)
        self.assertEqual(self.state_service.consolidated.queue.history, history)
        self.assertIs(self.state_service.consolidated.playback.now_playing, selected)
        self.assertFalse(self.player._is_playing)
        self.assertFalse(self.state.is_playing)
        self.assertFalse(self.player._user_paused)
        self.assertIsNone(self.player._paused_current)
        self.assertEqual(self.player._pending_skip_tokens, 0)
        self.assertEqual(self.player._queue_selection_stops_pending, 0)
        self.assertEqual(self.player._playback_sm.phase, PlaybackPhase.LOADING)
        self.backend_manager.schedule_pending_start.assert_called_with(selected.id)
        self.stop.assert_called()
        self.emit.assert_called()
        self.state_service.invalidate_snapshot.assert_called()

    def test_first_upcoming_replaces_current(self):
        self.arrange()
        old_token = self.queue.snapshot()
        self.control.play_item_now(self.b.id)
        self.assert_selected(self.b, [self.c, self.d], [self.old, self.a])
        self.assertFalse(self.queue.matches(old_token, self.a.id))
        self.assertGreater(self.queue.version, old_token[0])
        self.assertIs(self.queue.mark_playing(), self.b)
        self.assertFalse(self.queue.pending)
        self.stop.assert_called_once_with()
        self.emit.assert_called_once_with()

    def test_nonfirst_upcoming_preserves_unrelated_order(self):
        self.arrange()
        self.control.play_item_now(self.c.id)
        self.assert_selected(self.c, [self.b, self.d], [self.old, self.a])

    def test_first_row_with_no_current_is_published(self):
        self.arrange(current=False, phase=PlaybackPhase.IDLE)
        self.control.play_item_now(self.b.id)
        self.assert_selected(self.b, [self.c, self.d], [self.old])

    def test_nonfirst_row_with_no_current_is_published(self):
        self.arrange(current=False, phase=PlaybackPhase.IDLE)
        self.control.play_item_now(self.c.id)
        self.assert_selected(self.c, [self.b, self.d], [self.old])

    def test_current_row_rearms_without_duplicate_history(self):
        self.arrange()
        for _ in range(2):
            self.control.play_item_now(self.a.id)
            self.assert_selected(self.a, [self.b, self.c, self.d], [self.old])
            self.queue.mark_playing()

    def test_paused_selection_enters_loading(self):
        self.arrange(phase=PlaybackPhase.PAUSED)
        self.control.play_item_now(self.c.id)
        self.assert_selected(self.c, [self.b, self.d], [self.old, self.a])
        self.assertFalse(self.player._playback_sm.user_initiated_pause)

    def test_stopped_current_row_can_retry(self):
        self.arrange(phase=PlaybackPhase.STOPPED)
        self.control.play_item_now(self.a.id)
        self.assert_selected(self.a, [self.b, self.c, self.d], [self.old])

    def snapshot(self):
        return (
            self.queue.snapshot(),
            self.queue.pending,
            self.queue.upcoming(),
            list(self.queue.history_ref),
            self.state.now_playing,
            self.state.is_playing,
            self.player._playback_sm.phase,
            self.player._pending_skip_tokens,
            self.player._user_paused,
            self.player._paused_current,
        )

    def assert_no_side_effects(self, before):
        self.assertEqual(self.snapshot(), before)
        self.stop.assert_not_called()
        self.emit.assert_not_called()
        self.backend_manager.schedule_pending_start.assert_not_called()
        self.state_service.invalidate_snapshot.assert_not_called()

    def test_missing_target_is_unchanged_then_valid_retry_succeeds(self):
        self.arrange(phase=PlaybackPhase.PAUSED)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "not found"):
            self.control.play_item_now("missing")
        self.assert_no_side_effects(before)
        self.control.play_item_now(self.b.id)
        self.assert_selected(self.b, [self.c, self.d], [self.old, self.a])

    def test_empty_queue_rejects_without_side_effects(self):
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "not found"):
            self.control.play_item_now("missing")
        self.assert_no_side_effects(before)

    def test_blocked_provider_does_not_interrupt_then_valid_retry_succeeds(self):
        self.arrange()
        before = self.snapshot()
        control_module.is_youtube_track_requiring_provider.return_value = True
        control_module.is_provider_linked.return_value = False
        with self.assertRaisesRegex(control_module.ConfigurationError, "not linked"):
            self.control.play_item_now(self.b.id)
        self.assert_no_side_effects(before)
        control_module.is_provider_linked.assert_called_once_with("youtube_music")
        control_module.is_provider_linked.return_value = True
        self.control.play_item_now(self.b.id)
        self.assert_selected(self.b, [self.c, self.d], [self.old, self.a])

    def test_backend_stop_failure_keeps_selection_retryable(self):
        self.arrange()
        self.stop.side_effect = RuntimeError("synthetic stop failure")
        self.control.play_item_now(self.b.id)
        self.assert_selected(self.b, [self.c, self.d], [self.old, self.a])
        self.stop.side_effect = self.assert_stop_outside_lock
        self.control.play_item_now(self.b.id)
        self.assert_selected(self.b, [self.c, self.d], [self.old, self.a])

    def test_direct_selection_does_not_invoke_completion_dedup(self):
        self.arrange()
        with patch.object(self.queue._cursor, "complete_current", side_effect=AssertionError("not a completion")):
            self.control.play_item_now(self.c.id)
        self.assert_selected(self.c, [self.b, self.d], [self.old, self.a])

    def test_rewind_can_return_to_interrupted_current(self):
        self.arrange()
        self.control.play_item_now(self.c.id)
        self.assertIs(self.queue.rewind(), self.a)
        self.assertEqual(self.queue.upcoming(), [self.c, self.b, self.d])
        self.assertEqual(list(self.queue.history_ref), [self.old])

    def test_play_next_still_only_reorders_upcoming(self):
        self.arrange()
        self.assertTrue(self.controller.move_to_next(self.c.id))
        self.assertIs(self.queue.current(), self.a)
        self.assertEqual(self.queue.upcoming(), [self.c, self.b, self.d])
        self.assertEqual(list(self.queue.history_ref), [self.old])

    def test_cursor_missing_target_does_not_mutate(self):
        self.arrange()
        before = self.snapshot()
        self.assertFalse(self.queue._cursor.select_current("missing"))
        self.assertEqual(self.snapshot(), before)

    def worker(self):
        worker = worker_module.RuntimeWorkerManager.__new__(worker_module.RuntimeWorkerManager)
        worker._player = self.player
        worker._logger = self.player._logger
        worker._worker_heartbeat = 0
        worker._should_log_idle_debug = lambda *_args: False
        worker._try_handle_repeat_one = Mock(return_value=False)
        worker._complete_track_locked = Mock(side_effect=lambda result: self.queue.complete_current(result.success))
        worker._record_completion_telemetry = Mock()
        worker._update_now_playing_after_completion = Mock()
        return worker

    def test_old_completion_preserves_different_selection(self):
        self.arrange()
        worker = self.worker()
        self.control.play_item_now(self.c.id)
        worker._handle_playback_completion(self.a, types.SimpleNamespace(success=False, is_embedded_webview=False))
        self.assert_selected(self.c, [self.b, self.d], [self.old, self.a])
        worker._complete_track_locked.assert_not_called()
        with self.cv:
            claimed, token, _meta = worker._maybe_take_current_locked()
        self.assertIs(claimed, self.c)
        self.assertEqual(token[1], self.c.id)
        self.assertFalse(self.queue.pending)

    def test_old_completion_preserves_same_row_replay(self):
        self.arrange()
        worker = self.worker()
        self.control.play_item_now(self.a.id)
        result = types.SimpleNamespace(success=False, is_embedded_webview=False)
        worker._handle_playback_completion(self.a, result)
        self.assert_selected(self.a, [self.b, self.c, self.d], [self.old])
        worker._complete_track_locked.assert_not_called()
        with self.cv:
            claimed, _token, _meta = worker._maybe_take_current_locked()
        self.assertIs(claimed, self.a)
        self.assertFalse(self.queue.pending)
        # Once the replay is claimed, its completion still advances normally.
        worker._handle_playback_completion(claimed, result)
        worker._complete_track_locked.assert_called_once_with(result)
        self.assertIs(self.queue.current(), self.b)

    def test_old_completion_preserves_selection_back_to_original_row(self):
        self.arrange()
        worker = self.worker()
        self.control.play_item_now(self.b.id)
        self.queue.append(self.a)
        self.control.play_item_now(self.a.id)
        before = self.snapshot()
        worker._handle_playback_completion(self.a, types.SimpleNamespace(success=True, is_embedded_webview=False))
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.queue.pending)
        worker._complete_track_locked.assert_not_called()

    def test_idle_worker_cannot_claim_until_backend_stop_finishes(self):
        self.arrange(phase=PlaybackPhase.STOPPED)
        worker = self.worker()

        def stopping():
            self.assert_stop_outside_lock()
            with self.cv:
                self.assertEqual(worker._maybe_take_current_locked(), (None, None, None))
                self.assertEqual(worker._force_pending_start_locked(), (None, None, None))
            self.assertTrue(self.queue.pending)
            self.backend_manager.schedule_pending_start.assert_not_called()

        self.stop.side_effect = stopping
        self.control.play_item_now(self.b.id)
        self.assert_selected(self.b, [self.c, self.d], [self.old, self.a])
        with self.cv:
            claimed, _token, _meta = worker._maybe_take_current_locked()
        self.assertIs(claimed, self.b)
        self.assertFalse(self.queue.pending)

    def test_overlapping_selections_start_only_latest_after_every_stop(self):
        self.arrange(phase=PlaybackPhase.STOPPED)
        worker = self.worker()
        stops = []

        def stopping():
            self.assert_stop_outside_lock()
            stops.append(self.queue.current().id)
            if len(stops) == 1:
                self.control.play_item_now(self.c.id)
            with self.cv:
                self.assertEqual(worker._maybe_take_current_locked(), (None, None, None))
                self.assertEqual(worker._force_pending_start_locked(), (None, None, None))
            self.backend_manager.schedule_pending_start.assert_not_called()

        self.stop.side_effect = stopping
        self.control.play_item_now(self.b.id)
        self.assertEqual(stops, [self.b.id, self.c.id])
        self.assert_selected(self.c, [self.d], [self.old, self.a, self.b])
        self.backend_manager.schedule_pending_start.assert_called_once_with(self.c.id)
        with self.cv:
            claimed, _token, _meta = worker._maybe_take_current_locked()
        self.assertIs(claimed, self.c)

    def test_overlapping_same_row_selections_do_not_start_during_older_stop(self):
        self.arrange(phase=PlaybackPhase.STOPPED)
        worker = self.worker()
        stops = []

        def stopping():
            self.assert_stop_outside_lock()
            stops.append(self.queue.current().id)
            if len(stops) == 1:
                self.control.play_item_now(self.b.id)
            with self.cv:
                self.assertEqual(worker._maybe_take_current_locked(), (None, None, None))
            self.backend_manager.schedule_pending_start.assert_not_called()

        self.stop.side_effect = stopping
        self.control.play_item_now(self.b.id)
        self.assertEqual(stops, [self.b.id, self.b.id])
        self.assert_selected(self.b, [self.c, self.d], [self.old, self.a])
        self.backend_manager.schedule_pending_start.assert_called_once_with(self.b.id)
        with self.cv:
            claimed, _token, _meta = worker._maybe_take_current_locked()
        self.assertIs(claimed, self.b)

    def check_cancel_during_selection(self, operation, expected_phase, *, same_row=False):
        self.arrange(phase=PlaybackPhase.STOPPED)
        worker = self.worker()
        calls = []
        selected = self.a if same_row else self.b
        backend = types.SimpleNamespace(pause=Mock(), resume=Mock())
        self.backend_manager.backend = backend

        def stopping():
            self.assert_stop_outside_lock()
            calls.append(self.queue.current().id)
            if len(calls) == 1:
                getattr(self.control, operation)()

        self.stop.side_effect = stopping
        self.control.play_item_now(selected.id)
        self.assertIs(self.queue.current(), selected)
        self.assertIs(self.state.now_playing, selected)
        self.assertEqual(self.player._playback_sm.phase, expected_phase)
        self.assertTrue(self.player._user_paused)
        self.assertFalse(self.state.is_playing)
        self.assertTrue(self.queue.pending)
        self.assertEqual(self.player._queue_selection_stops_pending, 0)
        self.backend_manager.schedule_pending_start.assert_not_called()
        with self.cv:
            self.assertEqual(worker._maybe_take_current_locked(), (None, None, None))
            self.assertEqual(worker._force_pending_start_locked(), (None, None, None))
        before = self.snapshot()
        worker._handle_playback_completion(self.a, types.SimpleNamespace(success=False, is_embedded_webview=False))
        self.assertEqual(self.snapshot(), before)
        worker._complete_track_locked.assert_not_called()
        # Ordinary Resume starts the selected identity through the worker;
        # resuming the old backend would revive A while the UI publishes B.
        self.control.resume()
        backend.resume.assert_not_called()
        self.assert_selected(
            selected,
            [self.b, self.c, self.d] if same_row else [self.c, self.d],
            [self.old] if same_row else [self.old, self.a],
        )
        with self.cv:
            claimed, _token, _meta = worker._maybe_take_current_locked()
        self.assertIs(claimed, selected)
        self.assertFalse(self.queue.pending)

    def test_pause_during_selection_does_not_rearm_playback(self):
        self.check_cancel_during_selection("pause", PlaybackPhase.PAUSED)

    def test_stop_during_selection_does_not_rearm_playback(self):
        self.check_cancel_during_selection("stop", PlaybackPhase.STOPPED)

    def test_pause_during_same_row_selection_survives_old_completion(self):
        self.check_cancel_during_selection("pause", PlaybackPhase.PAUSED, same_row=True)

    def test_stop_during_same_row_selection_survives_old_completion(self):
        self.check_cancel_during_selection("stop", PlaybackPhase.STOPPED, same_row=True)

    def test_resume_existing_started_track_uses_backend_outside_lock(self):
        self.arrange(phase=PlaybackPhase.PAUSED)
        backend = types.SimpleNamespace(resume=Mock(side_effect=self.assert_stop_outside_lock))
        self.backend_manager.backend = backend
        self.control.resume()
        backend.resume.assert_called_once_with()
        self.assertEqual(self.player._playback_sm.phase, PlaybackPhase.PLAYING)
        self.assertTrue(self.state.is_playing)
        self.assertFalse(self.queue.pending)
        self.backend_manager.schedule_pending_start.assert_not_called()

    def test_pause_then_resume_during_stop_still_waits_for_stop(self):
        self.arrange(phase=PlaybackPhase.STOPPED)
        worker = self.worker()
        backend = types.SimpleNamespace(pause=Mock(), resume=Mock())
        self.backend_manager.backend = backend

        def stopping():
            self.assert_stop_outside_lock()
            self.control.pause()
            self.control.resume()
            self.assertEqual(self.player._playback_sm.phase, PlaybackPhase.LOADING)
            self.assertFalse(self.state.is_playing)
            with self.cv:
                self.assertEqual(worker._maybe_take_current_locked(), (None, None, None))
                self.assertEqual(worker._force_pending_start_locked(), (None, None, None))

        self.stop.side_effect = stopping
        self.control.play_item_now(self.b.id)
        backend.resume.assert_not_called()
        self.assert_selected(self.b, [self.c, self.d], [self.old, self.a])
        with self.cv:
            claimed, _token, _meta = worker._maybe_take_current_locked()
        self.assertIs(claimed, self.b)

    def test_stop_after_selection_before_worker_claim_retains_recovery_identity(self):
        self.arrange()
        worker = self.worker()
        backend = types.SimpleNamespace(resume=Mock())
        self.backend_manager.backend = backend
        self.control.play_item_now(self.b.id)
        self.control.stop()
        self.assertEqual(self.player._playback_sm.phase, PlaybackPhase.STOPPED)
        with self.cv:
            self.assertEqual(worker._maybe_take_current_locked(), (None, None, None))
            self.assertEqual(worker._force_pending_start_locked(), (None, None, None))
        self.control.resume()
        backend.resume.assert_not_called()
        self.assertFalse(self.state.is_playing)
        with self.cv:
            claimed, _token, _meta = worker._maybe_take_current_locked()
        self.assertIs(claimed, self.b)


if __name__ == "__main__":
    unittest.main()
