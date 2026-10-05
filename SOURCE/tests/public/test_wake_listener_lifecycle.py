"""Execute the real listener/registry lifecycle with inert native boundaries.

These are source-level synthetic regressions, not installed microphone/model
acceptance. The whole listener, its constructor/_init/run/cleanup, the registry,
and WorkSignal are loaded unchanged. Audio, model, AEC, policy, diagnostics and
watcher startup are isolated; no device, background watcher or provider is used.
"""

from __future__ import annotations

import faulthandler
import importlib.util
import logging
import subprocess
import sys
import threading
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[2]


def module(name, **attributes):
    result = types.ModuleType(name)
    result.__dict__.update(attributes)
    return result


def load(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


class WakeListenerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.policy = Mock()
        self.policy._config = types.SimpleNamespace(base_threshold=0.8, cooldown_ms=2000, confirmation_window=3)
        self.log = Mock(wraps=logging.getLogger(__name__))
        self.native_open = Mock(side_effect=AssertionError("Unexpected native audio initialization"))
        self.native_stream = Mock(side_effect=AssertionError("Unexpected native stream open"))
        self.native_terminate = Mock()
        modules = {
            "onnxruntime": module("onnxruntime"),
            "pyaudio": None,
            "config": module("config", AppConfig=object),
            "config.constants": module("config.constants", AUDIO_SAMPLE_RATE=16000),
            "config.settings": module("config.settings", settings=types.SimpleNamespace(force_wake=False)),
            "core.constants": module(
                "core.constants", AUDIO_INT16_SCALE=32768, SAMPLE_RATE_48K=48000, TIMEOUT_MEDIUM=0.1
            ),
            "core.logging_config": module("core.logging_config", get_logger=lambda name: self.log),
            "core.platform": module(
                "core.platform", get_logs_dir=lambda: self.directory, get_temp_dir=lambda: self.directory
            ),
            "audio_core.portaudio_guard": module(
                "audio_core.portaudio_guard",
                GuardedStream=object,
                open_portaudio=self.native_open,
                open_stream=self.native_stream,
                terminate_portaudio=self.native_terminate,
            ),
            "voice.wake_detector.aec_processor": module(
                "voice.wake_detector.aec_processor",
                AEC_FRAME_SAMPLES=160,
                AEC_SAMPLE_RATE=16000,
                AECProcessor=object,
                create_aec_processor=Mock(side_effect=AssertionError("No native AEC")),
            ),
            "voice.wake_detector.contributor_mode": module(
                "voice.wake_detector.contributor_mode",
                get_contributor_manager=lambda config: Mock(is_active=False),
            ),
            "voice.wake_detector.fp_collector": module("voice.wake_detector.fp_collector", get_fp_collector=Mock()),
            "voice.wake_detector.spoke_wake_diag": module(
                "voice.wake_detector.spoke_wake_diag",
                DIAG_ENABLED=False,
                log_hub_inference=Mock(),
            ),
            "voice.wake_detector.wake_decision_policy": module(
                "voice.wake_detector.wake_decision_policy",
                WakeContext=object,
                WakeDecisionPolicy=object,
                get_wake_policy=lambda config: self.policy,
            ),
            "voice.wake_detector.wake_types": module(
                "voice.wake_detector.wake_types",
                _default_calibration_dir=lambda: self.directory,
            ),
            "ui.qt_native.debug_events": module("ui.qt_native.debug_events", emit_debug_event=Mock()),
            "violawake": module("violawake", CLIP_SAMPLES=24000),
        }
        diagnostics = {
            "aec_effectiveness": "get_aec_diagnostics",
            "audio_pipeline_health": "get_audio_health_monitor",
            "score_history": "get_score_history",
            "wake_analytics": "get_wake_analytics",
            "wake_audio_buffer": "get_wake_audio_buffer",
            "wake_decision_trace": "get_decision_tracer",
            "wake_metrics": "get_wake_metrics",
        }
        for name, getter in diagnostics.items():
            modules[f"diagnostics.{name}"] = module(f"diagnostics.{name}", **{getter: Mock()})
        self.modules = patch.dict(sys.modules, modules)
        self.modules.start()
        self.addCleanup(self.modules.stop)
        load("services.liveness", "services/liveness.py")
        self.registry = load("audio_core.device_change", "audio_core/device_change.py")
        watcher_patch = patch.object(self.registry, "start_device_watch", Mock())
        self.watcher = watcher_patch.start()
        self.addCleanup(watcher_patch.stop)
        self.runtime = load("_wake_listener_lifecycle", "voice/wake_detector/violawake_listener.py")
        self.listener = self.make_listener()
        self.stop = threading.Event()

    def make_listener(self):
        return self.runtime.ViolaWakeListener(
            types.SimpleNamespace(wake_aec_enabled=False),
            Mock(),
            model_path=self.directory / "missing.onnx",
        )

    def assert_released(self, listener=None):
        listener = listener or self.listener
        self.assertNotIn(listener, self.registry._snapshot_owners())
        self.assertIsNone(listener._stream)
        self.assertIsNone(listener._audio)
        self.assertIsNone(listener._engine)
        self.assertFalse(listener._is_initialized)
        self.assertFalse(listener._loop_active.is_set())

    def prepare_capture(self):
        self.runtime.pyaudio = types.SimpleNamespace(paInt16=8)
        self.listener._engine = Mock()
        audio = Mock()
        audio.get_default_input_device_info.return_value = {"name": "synthetic input", "defaultSampleRate": 16000}
        stream = Mock()
        self.native_open.side_effect = None
        self.native_open.return_value = audio
        self.native_stream.side_effect = None
        self.native_stream.return_value = stream
        return audio, stream

    def stop_after_next_init(self, listener=None):
        """Exercise real init success/failure before requesting orderly stop."""
        listener = listener or self.listener
        actual_init = listener._init
        self.stop.clear()

        def initialize_then_stop():
            listener._init = actual_init
            try:
                return actual_init()
            finally:
                self.stop.set()

        listener._init = initialize_then_stop

    def test_real_failed_init_unregisters_on_stop(self):
        self.stop_after_next_init()
        self.listener.run(self.stop)
        self.assertTrue(self.listener._capture_open_failed.is_set())
        self.native_open.assert_not_called()
        self.watcher.assert_called_once_with()
        self.policy.reset.assert_called_once_with()
        self.assert_released()

    def test_repeated_supervisor_replacements_do_not_accumulate_owners(self):
        other_owner = object()
        self.registry.register_stream_owner(other_owner)
        for _ in range(3):
            listener = self.make_listener()
            self.stop_after_next_init(listener)
            listener.run(self.stop)
            self.assertTrue(listener._capture_open_failed.is_set())
            self.assert_released(listener)
            self.assertEqual(self.registry._snapshot_owners(), (other_owner,))
        self.assertEqual(self.watcher.call_count, 4)

    def test_initialization_exception_still_releases_owner(self):
        with patch.object(self.listener, "_init", side_effect=RuntimeError("injected init failure")):
            with self.assertRaisesRegex(RuntimeError, "injected init failure"):
                self.listener.run(self.stop)
        self.assert_released()
        self.policy.reset.assert_called_once_with()

    def test_failed_init_wait_exception_still_releases_owner(self):
        with patch.object(self.runtime.time, "sleep", side_effect=RuntimeError("injected wait failure")):
            with self.assertRaisesRegex(RuntimeError, "injected wait failure"):
                self.listener.run(self.stop)
        self.assertTrue(self.listener._capture_open_failed.is_set())
        self.assert_released()

    def test_registration_failure_after_append_still_releases_owner(self):
        self.watcher.side_effect = RuntimeError("injected watcher failure")
        with self.assertRaisesRegex(RuntimeError, "injected watcher failure"):
            self.listener.run(self.stop)
        self.assert_released()

    def test_successful_init_then_stop_cleans_after_unregistering(self):
        audio, stream = self.prepare_capture()
        self.stop_after_next_init()
        stream.stop_stream.side_effect = lambda: self.assertNotIn(self.listener, self.registry._snapshot_owners())
        self.listener.run(self.stop)
        self.native_open.assert_called_once_with()
        self.native_stream.assert_called_once()
        stream.stop_stream.assert_called_once_with()
        stream.close.assert_called_once_with()
        self.native_terminate.assert_called_once_with(audio)
        self.assert_released()
        self.assertFalse(self.listener._capture_open_failed.is_set())

    def test_failed_listener_can_restart_read_work_and_stop_repeatedly(self):
        self.stop_after_next_init()
        self.listener.run(self.stop)
        self.assertTrue(self.listener._capture_open_failed.is_set())
        self.assertEqual(self.listener.detection_signal.count, 0)
        for _ in range(2):
            _, stream = self.prepare_capture()
            self.stop.clear()

            def read_frame(*args, **kwargs):
                self.assertIn(self.listener, self.registry._snapshot_owners())
                self.assertTrue(self.listener._loop_active.is_set())
                return np.ones(self.listener.CHUNK_SIZE, dtype=np.int16).tobytes()

            stream.read.side_effect = read_frame
            # Stop after the real frame has been accepted, not while read is
            # returning: post-cancellation frames are intentionally discarded.
            self.listener.set_frame_tap(lambda _frame: self.stop.set())
            self.listener.run(self.stop)
            stream.read.assert_called_once_with(self.listener.CHUNK_SIZE, exception_on_overflow=False)
            self.assertFalse(self.listener._capture_open_failed.is_set())
            self.assert_released()
        self.assertEqual(self.listener.detection_signal.count, 2)
        self.assertEqual(self.watcher.call_count, 3)
        self.log.exception.assert_not_called()

    def test_state_lock_still_excludes_other_threads(self):
        acquired = []

        def try_acquire():
            owned = self.listener._state_lock.acquire(blocking=False)
            acquired.append(owned)
            if owned:
                self.listener._state_lock.release()

        with self.listener._state_lock:
            worker = threading.Thread(target=try_acquire, daemon=True)
            worker.start()
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
        self.assertEqual(acquired, [False])

    def test_setup_exception_before_read_loop_still_releases_capture(self):
        audio, stream = self.prepare_capture()
        self.listener._engine.set_callback.side_effect = RuntimeError("injected callback setup failure")
        with self.assertRaisesRegex(RuntimeError, "injected callback setup failure"):
            self.listener.run(self.stop)
        self.assert_released()
        stream.close.assert_called_once_with()
        self.native_terminate.assert_called_once_with(audio)

    def test_read_loop_exception_releases_owner_and_capture(self):
        _, stream = self.prepare_capture()
        stream.is_active.side_effect = RuntimeError("injected loop failure")
        self.listener.run(self.stop)
        self.assert_released()
        stream.close.assert_called_once_with()
        self.log.exception.assert_called_once()

    def test_real_device_open_failure_does_not_deadlock_cleanup(self):
        # A separate process bounds the known-bad non-reentrant-lock case;
        # a stuck native-failure cleanup cannot hang the public test runner.
        result = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--device-open-failure"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cleanup_exception_does_not_retain_owner(self):
        self.stop.set()
        self.policy.reset.side_effect = RuntimeError("injected cleanup failure")
        with self.assertRaisesRegex(RuntimeError, "injected cleanup failure"):
            self.listener.run(self.stop)
        self.assert_released()


class WakeRestartOwnershipTests(unittest.TestCase):
    """Actual pipeline/facade control flow, with no model or capture device."""

    def setUp(self):
        import ast
        from typing import Any

        self.calls = []
        calls = self.calls

        class Detector:
            def __init__(self, name, running=False):
                self.name, self.running = name, running
                self._intentionally_stopped = False

            def is_running(self):
                return self.running

            def is_available(self):
                return True

            def start(self, event):
                calls.append(self.name + ":start")
                self.running = True
                self._intentionally_stopped = False
                return True

            def stop(self):
                calls.append(self.name + ":stop")
                self.running = False
                self._intentionally_stopped = True

        self.Detector = Detector
        self.old, self.new = Detector("old", True), Detector("replacement")
        self.namespace = {"threading": threading, "logger": Mock(), "Path": Path, "Any": Any}
        self.future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
        path = ROOT / "voice/wake_detector/facade.py"
        tree = ast.parse(path.read_text())
        facade = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "WakeDetectorFacade")
        self.execute([facade], path)
        self.facade = self.namespace["WakeDetectorFacade"]
        self.facade.set_instance(self.old)
        overrides = patch.dict(
            sys.modules,
            {"voice.wake_detector.facade": module("voice.wake_detector.facade", WakeDetectorFacade=self.facade)},
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        path = ROOT / "voice/pipeline.py"
        tree = ast.parse(path.read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "VoicePipeline")
        self.execute(
            [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in {"_restart_wake_detector", "start"}],
            path,
        )
        self.factory = Mock(return_value=self.new)
        self.namespace["WakeDetector"] = self.factory
        self.namespace["WakeDetectorUnavailableError"] = RuntimeError
        self.pipeline = types.SimpleNamespace(
            wake_detector=self.old,
            _stop_event=threading.Event(),
            config=object(),
            _build_wake_callback=lambda: lambda: None,
            _supervisor=None,
            _rewire_aec_after_restart=Mock(),
            _try_start_continuous_capture=Mock(),
        )

    def execute(self, nodes, path):
        import ast

        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[self.future, *nodes], type_ignores=[])), str(path), "exec"
            ),
            self.namespace,
        )

    def restart(self):
        return self.namespace["_restart_wake_detector"](self.pipeline)

    def deferred_restart(self, action):
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []

        def build(*args, **kwargs):
            entered.set()
            assert release.wait(2), "synthetic replacement construction was not released"
            return self.new

        def run():
            try:
                results.append(self.restart())
            except BaseException as exc:
                errors.append(exc)

        self.factory.side_effect = build
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            assert entered.wait(1)
            action()
            self.calls.append("newer-intent-completed")
        finally:
            release.set()
            worker.join(2)
        assert not worker.is_alive()
        assert not errors
        return results

    def test_disable_during_replacement_construction_cannot_restart(self):
        results = self.deferred_restart(lambda: self.facade.sync_to_state("disabled", False))
        assert results == [False]
        assert "replacement:start" not in self.calls
        assert self.calls[-1] == "replacement:stop"
        assert self.pipeline.wake_detector is self.old
        assert self.facade.get_instance() is self.old
        assert self.old._intentionally_stopped

    def test_mute_during_replacement_construction_cannot_restart(self):
        results = self.deferred_restart(lambda: self.facade.sync_to_state("wake_word", True))
        assert results == [False]
        assert "replacement:start" not in self.calls
        assert not self.old.is_running() and not self.new.is_running()

    def test_unchanged_enabled_restart_adopts_exactly_one_replacement(self):
        assert self.restart() is True
        assert self.pipeline.wake_detector is self.new
        assert self.facade.get_instance() is self.new
        assert self.calls == ["old:stop", "replacement:start"]
        self.pipeline._rewire_aec_after_restart.assert_called_once_with()

    def test_enabled_restart_can_construct_a_missing_detector(self):
        self.pipeline.wake_detector = None
        self.facade.set_instance(None)
        assert self.restart() is True
        assert self.pipeline.wake_detector is self.new
        assert self.calls == ["replacement:start"]

    def test_rejected_cleanup_failure_does_not_activate_replacement(self):
        self.new.stop = Mock(side_effect=RuntimeError("controlled cleanup failure"))
        assert self.deferred_restart(lambda: self.facade.sync_to_state("disabled", False)) == [False]
        assert "replacement:start" not in self.calls
        self.namespace["logger"].warning.assert_called_once()

    def test_queued_restart_after_disable_does_not_construct_or_start(self):
        self.facade.sync_to_state("disabled", False)
        assert self.restart() is False
        self.factory.assert_not_called()
        assert self.calls == ["old:stop"]

    def test_newer_unmute_owns_old_instance_and_invalidates_prepared_restart(self):
        def action():
            self.facade.sync_to_state("wake_word", True)
            self.facade.sync_to_state("wake_word", False)

        assert self.deferred_restart(action) == [False]
        assert self.old.is_running()
        assert self.facade.get_instance() is self.old
        assert "replacement:start" not in self.calls

    def test_replaced_owner_invalidates_prepared_restart(self):
        other = self.Detector("new-owner", True)
        assert self.deferred_restart(lambda: self.facade.set_instance(other)) == [False]
        assert self.facade.get_instance() is other
        assert other.is_running()
        assert "replacement:start" not in self.calls

    def test_shutdown_during_construction_discards_replacement(self):
        assert self.deferred_restart(self.pipeline._stop_event.set) == [False]
        assert "replacement:start" not in self.calls

    def test_owner_aba_reregistration_retires_prepared_restart(self):
        other = self.Detector("intervening-owner")

        def action():
            self.facade.set_instance(other)
            self.facade.set_instance(self.old)

        assert self.deferred_restart(action) == [False]
        assert self.facade.get_instance() is self.old
        assert "replacement:start" not in self.calls

    def test_same_instance_reregistration_retires_prepared_restart(self):
        assert self.deferred_restart(lambda: self.facade.set_instance(self.old)) == [False]
        assert self.facade.get_instance() is self.old
        assert "replacement:start" not in self.calls

    def test_pending_pipeline_start_respects_acknowledged_disable(self):
        self.old.running = False
        self.facade.sync_to_state("disabled", False)
        assert self.namespace["start"](self.pipeline) is False
        assert "old:start" not in self.calls
        self.pipeline._try_start_continuous_capture.assert_not_called()

    def test_pending_pipeline_start_respects_acknowledged_mute(self):
        self.old.running = False
        self.facade.sync_to_state("wake_word", True)
        assert self.namespace["start"](self.pipeline) is False
        assert "old:start" not in self.calls

    def test_later_explicit_unmute_allows_current_start(self):
        self.old.running = False
        self.facade.sync_to_state("wake_word", True)
        self.facade.sync_to_state("wake_word", False)
        assert self.old.is_running()
        assert self.calls == ["old:start"]

    def test_pending_pipeline_start_does_not_duplicate_newer_unmute(self):
        self.old.running = False
        self.facade.sync_to_state("wake_word", True)
        self.facade.sync_to_state("wake_word", False)
        assert self.namespace["start"](self.pipeline) is True
        assert self.calls.count("old:start") == 1

    def test_disable_cannot_acknowledge_between_activation_check_and_start(self):
        entered, release, disabling, acknowledged = (threading.Event() for _ in range(4))
        original_start = self.old.start
        self.old.running = False
        errors = []

        def delayed_start(event):
            entered.set()
            assert release.wait(2)
            return original_start(event)

        def disable():
            disabling.set()
            try:
                self.facade.sync_to_state("wake_word", True)
                self.calls.append("disable-acknowledged")
                acknowledged.set()
            except BaseException as exc:
                errors.append(exc)

        self.old.start = delayed_start
        start_worker = threading.Thread(
            target=lambda: self.facade.start_current(self.old, self.pipeline._stop_event), daemon=True
        )
        disable_worker = threading.Thread(target=disable, daemon=True)
        start_worker.start()
        try:
            assert entered.wait(1)
            disable_worker.start()
            assert disabling.wait(1)
            assert not acknowledged.wait(0.05)
        finally:
            release.set()
            start_worker.join(2)
            if disable_worker.ident is not None:
                disable_worker.join(2)
        assert not start_worker.is_alive() and not disable_worker.is_alive()
        assert not errors and acknowledged.is_set()
        assert self.calls == ["old:start", "old:stop", "disable-acknowledged"]
        assert not self.old.is_running()

    def test_effect_before_registration_retains_disable_for_later_start(self):
        import ast

        self.facade.set_instance(None)
        self.namespace.update(
            EffectOutcome=types.SimpleNamespace(DEFERRED="deferred", FAILED="failed", APPLIED="applied"),
            EffectResult=lambda key, outcome, detail: types.SimpleNamespace(outcome=outcome),
        )
        path = ROOT / "ui/settings_effects.py"
        tree = ast.parse(path.read_text())
        self.execute([n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_apply_voice_mode"], path)
        effect = self.namespace["_apply_voice_mode"](
            "voice_mode", "disabled", settings_mgr=types.SimpleNamespace(get=lambda key, default: False)
        )
        assert effect.outcome == "deferred"
        self.old.running = False
        self.facade.set_instance(self.old)
        assert self.namespace["start"](self.pipeline) is False
        assert "old:start" not in self.calls

    def test_test_reset_does_not_leak_prior_disable_intent(self):
        self.facade.sync_to_state("disabled", False)
        self.facade.reset_for_tests()
        self.facade.set_instance(self.old)
        assert self.restart() is True


class WakeCaptureStopTests(unittest.TestCase):
    """Actual detector/listener control with native audio/model boundaries inert."""

    def setUp(self):
        import ast

        self.case = WakeListenerLifecycleTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.listener = self.case.listener
        self.parent_stop = threading.Event()
        self._workers = []
        self._releases = []
        self.addCleanup(self._finish_workers)
        path = ROOT / "voice/wake_detector/facade.py"
        tree = ast.parse(path.read_text())
        names = {"_WakeStopEvent", "WakeDetector", "WakeDetectorFacade"}
        nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in names]
        future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
        self.namespace = {
            "threading": threading,
            "time": __import__("time"),
            "Path": Path,
            "logger": Mock(),
            "TIMEOUT_MEDIUM": 0.02,
        }
        exec(
            compile(ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[])), str(path), "exec"),
            self.namespace,
        )
        self.detector = self.make_detector(self.listener)

    def _finish_workers(self):
        self.parent_stop.set()
        for release in self._releases:
            release.set()
        current = getattr(getattr(self, "detector", None), "_thread", None)
        workers = [*self._workers, *([current] if current is not None else [])]
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(2.5)
                assert not worker.is_alive(), "worker must drain before inert module cleanup"

    def make_detector(self, implementation):
        detector = object.__new__(self.namespace["WakeDetector"])
        detector._impl = implementation
        detector._raw_listener = implementation
        detector._stop_event = None
        detector._thread = None
        detector._lifecycle_lock = threading.RLock()
        detector._run_cancel = None
        detector._lifecycle_generation = 0
        detector._drain_callback = None
        detector._gain_scheduler = None
        detector._intentionally_stopped = False
        detector._detection_circuit = Mock()
        detector._detection_circuit.is_open = False
        return detector

    def prepare_native_boundary(self):
        audio, stream = self.case.prepare_capture()
        self.listener._model_path.write_bytes(b"inert model boundary, never executed")
        self.listener._load_engine = Mock(return_value=Mock())

        def finish_unexpected_read(*args, **kwargs):
            # Bound negative controls too: if a broken source reaches capture,
            # let its real loop exit before the inert module fixture is reset.
            self.parent_stop.set()
            return np.ones(self.listener.CHUNK_SIZE, dtype=np.int16).tobytes()

        stream.read.side_effect = finish_unexpected_read
        return audio, stream

    def paused_attempt(self, *, before_listener=False):
        entered, release = threading.Event(), threading.Event()
        self._releases.append(release)
        owner = self.detector if before_listener else self.listener
        name = "_run_loop" if before_listener else "_run_registered"
        actual = getattr(owner, name)

        def delayed(event):
            entered.set()
            assert release.wait(2), "controlled worker was not released"
            actual(event)

        setattr(owner, name, delayed)
        assert self.detector.start(self.parent_stop)
        worker = self.detector._thread
        self._workers.append(worker)
        assert entered.wait(1)
        return worker, release

    def test_cancelled_listener_never_initializes_native_capture(self):
        _, stream = self.prepare_native_boundary()
        self.parent_stop.set()
        self.listener.run(self.parent_stop)
        self.case.native_open.assert_not_called()
        self.case.native_stream.assert_not_called()
        stream.read.assert_not_called()
        self.case.assert_released()

    def test_cancelled_parent_does_not_create_a_worker(self):
        self.parent_stop.set()
        assert self.detector.start(self.parent_stop) is False
        assert self.detector._thread is None
        self.case.native_open.assert_not_called()

    def test_stop_before_listener_entry_cannot_open_after_return(self):
        _, stream = self.prepare_native_boundary()
        worker, release = self.paused_attempt(before_listener=True)
        try:
            self.detector.stop()
            assert not self.parent_stop.is_set()
            assert self.detector.is_running(), "draining worker must remain observable"
            assert self.detector.start(self.parent_stop) is False
        finally:
            release.set()
            worker.join(1)
        assert not worker.is_alive()
        self.case.native_open.assert_not_called()
        stream.read.assert_not_called()

    def test_stop_after_registration_cannot_open_after_return(self):
        _, stream = self.prepare_native_boundary()
        worker, release = self.paused_attempt()
        try:
            self.detector.stop()
            assert self.detector._thread is worker
            assert self.detector._run_cancel.is_set()
            assert not self.parent_stop.is_set()
            assert self.detector.start(self.parent_stop) is False
            facade = self.namespace["WakeDetectorFacade"]
            facade.set_instance(self.detector)
            # Keep disabled intent for this no-capture control. A separate
            # retained enable is now expected to resume only after drain.
            facade.sync_to_state("disabled", False)
            assert facade.start_current(self.detector, self.parent_stop) is False
        finally:
            release.set()
            worker.join(1)
        assert not worker.is_alive()
        self.case.native_open.assert_not_called()
        self.case.native_stream.assert_not_called()
        stream.read.assert_not_called()
        self.case.assert_released()

    def test_active_start_is_idempotent_without_duplicate_worker(self):
        self.prepare_native_boundary()
        worker, release = self.paused_attempt()
        try:
            assert self.detector.start(self.parent_stop) is True
            assert self.detector._thread is worker
            self.detector.stop()
        finally:
            release.set()
            worker.join(1)
        assert not worker.is_alive()
        self.case.native_stream.assert_not_called()

    def test_restart_after_drain_uses_fresh_local_cancellation(self):
        self.prepare_native_boundary()
        actual = self.listener._run_registered
        worker, release = self.paused_attempt()
        try:
            self.detector.stop()
            old_cancel = self.detector._run_cancel
        finally:
            release.set()
            worker.join(1)
        assert not worker.is_alive()
        self.listener._run_registered = actual
        _, stream = self.prepare_native_boundary()

        def read(*args, **kwargs):
            self.detector._run_cancel.set()
            return np.ones(self.listener.CHUNK_SIZE, dtype=np.int16).tobytes()

        stream.read.side_effect = read
        assert self.detector.start(self.parent_stop)
        worker = self.detector._thread
        worker.join(1)
        assert not worker.is_alive()
        assert self.detector._run_cancel is not old_cancel
        assert old_cancel.is_set() and not self.parent_stop.is_set()
        self.case.native_stream.assert_called_once()
        stream.read.assert_called_once()
        self.case.assert_released()

    def test_stop_called_from_worker_does_not_join_itself(self):
        implementation = types.SimpleNamespace(cleanup=Mock())
        detector = self.make_detector(implementation)
        implementation.run = lambda event: detector.stop()
        assert detector.start(self.parent_stop)
        worker = detector._thread
        worker.join(1)
        assert not worker.is_alive()
        assert detector._intentionally_stopped
        assert detector._run_cancel.is_set()
        assert not self.parent_stop.is_set()
        implementation.cleanup.assert_called_once()
        self.namespace["logger"].exception.assert_not_called()

    def test_stop_waits_for_in_progress_init_cleanup_before_return(self):
        audio, stream = self.prepare_native_boundary()
        entered, release, stopping, returned = (threading.Event() for _ in range(4))
        order = []

        def initialize_audio():
            entered.set()
            assert release.wait(2)
            return audio

        def open_stream(*args, **kwargs):
            order.append("capture-open")
            return stream

        def stop():
            stopping.set()
            self.detector.stop()
            order.append("stop-returned")
            returned.set()

        self.case.native_open.side_effect = initialize_audio
        self.case.native_stream.side_effect = open_stream
        assert self.detector.start(self.parent_stop)
        worker = self.detector._thread
        assert entered.wait(1)
        stopper = threading.Thread(target=stop, daemon=True)
        stopper.start()
        try:
            assert stopping.wait(1)
            assert not returned.wait(0.03)
        finally:
            release.set()
            stopper.join(1)
            worker.join(1)
        assert not stopper.is_alive() and not worker.is_alive()
        assert order == ["capture-open", "stop-returned"]
        stream.close.assert_called_once()
        stream.read.assert_not_called()
        assert not self.parent_stop.is_set()
        self.case.assert_released()

    def test_stop_union_wait_observes_either_owner_without_mutating_them(self):
        cancelled = threading.Event()
        signal = self.namespace["_WakeStopEvent"](self.parent_stop, cancelled)
        assert not signal.wait(0)
        self.parent_stop.set()
        assert signal.wait(0)
        assert not cancelled.is_set()
        self.parent_stop.clear()
        cancelled.set()
        assert signal.wait(0)
        assert not self.parent_stop.is_set()

    def test_late_read_error_cannot_reopen_capture_after_stop_returns(self):
        _, stream = self.prepare_native_boundary()
        reading, release = threading.Event(), threading.Event()
        self._releases.append(release)

        def failed_read(*args, **kwargs):
            reading.set()
            assert release.wait(2)
            raise OSError("controlled read failure delivered after stop")

        stream.read.side_effect = failed_read
        assert self.detector.start(self.parent_stop)
        worker = self.detector._thread
        self._workers.append(worker)
        try:
            assert reading.wait(1)
            assert self.case.native_stream.call_count == 1
            self.detector.stop()
        finally:
            release.set()
            worker.join(1)
        assert not worker.is_alive()
        assert self.case.native_stream.call_count == 1, "late recovery must not reopen after stop"
        self.case.assert_released()

    def test_late_read_completion_is_not_forwarded_after_stop(self):
        _, stream = self.prepare_native_boundary()
        reading, release = threading.Event(), threading.Event()
        self._releases.append(release)
        tap = Mock()
        self.listener.set_frame_tap(tap)

        def late_frame(*args, **kwargs):
            reading.set()
            assert release.wait(2)
            return np.ones(self.listener.CHUNK_SIZE, dtype=np.int16).tobytes()

        stream.read.side_effect = late_frame
        assert self.detector.start(self.parent_stop)
        worker = self.detector._thread
        self._workers.append(worker)
        try:
            assert reading.wait(1)
            self.detector.stop()
        finally:
            release.set()
            worker.join(1)
        assert not worker.is_alive()
        tap.assert_not_called()
        assert self.listener.detection_signal.count == 0
        self.case.assert_released()

    def test_shared_recovery_and_command_open_stay_cancelled_until_new_run(self):
        self.prepare_native_boundary()
        self.parent_stop.set()
        self.listener.run(self.parent_stop)
        assert self.listener._reopen_audio_stream() is False
        assert self.listener._init() is False
        assert self.listener.listen_and_record_command() is None
        self.case.native_open.assert_not_called()
        self.case.native_stream.assert_not_called()

    def test_uncancelled_stream_recovery_remains_available(self):
        _, stream = self.prepare_native_boundary()
        try:
            assert self.listener._reopen_audio_stream() is True
            self.case.native_open.assert_called_once()
            self.case.native_stream.assert_called_once()
        finally:
            self.listener.cleanup()
        stream.close.assert_called_once()

    def pending_reenable(self):
        _, stream = self.prepare_native_boundary()
        opened = threading.Event()

        def open_capture(*args, **kwargs):
            opened.set()
            return stream

        def read_frame(*args, **kwargs):
            self.parent_stop.wait(0.002)
            return np.ones(self.listener.CHUNK_SIZE, dtype=np.int16).tobytes()

        self.case.native_stream.side_effect = open_capture
        stream.read.side_effect = read_frame
        worker, release = self.paused_attempt()
        facade = self.namespace["WakeDetectorFacade"]
        facade.set_instance(self.detector)
        facade.sync_to_state("wake_word", True)
        assert self.detector.is_running() and self.detector._intentionally_stopped
        self.resume_start = Mock(wraps=self.detector.start)
        self.detector.start = self.resume_start
        facade.sync_to_state("wake_word", False)
        self.case.native_stream.assert_not_called()
        return facade, worker, release, opened

    def finish_without_resume(self, worker, release):
        release.set()
        worker.join(1)
        assert not worker.is_alive()
        assert not self.detector.is_running()
        self.resume_start.assert_not_called()
        self.case.native_stream.assert_not_called()
        self.case.assert_released()

    def test_latest_reenable_resumes_only_after_old_cleanup_drains(self):
        facade, worker, release, opened = self.pending_reenable()
        release.set()
        assert opened.wait(1)
        worker.join(1)
        assert not worker.is_alive()
        resumed = self.detector._thread
        assert resumed is not worker and resumed.is_alive()
        self._workers.append(resumed)
        assert self.detector._stop_event is self.parent_stop
        assert not self.parent_stop.is_set()
        assert self.case.native_stream.call_count == 1
        self.resume_start.assert_called_once_with(self.parent_stop)
        facade.sync_to_state("wake_word", True)
        resumed.join(1)
        assert not resumed.is_alive()
        self.case.assert_released()

    def test_repeated_enabled_intent_collapses_to_one_resume(self):
        facade, worker, release, opened = self.pending_reenable()
        facade.sync_to_state("wake_word", False)
        facade.sync_to_state("wake_word", False)
        release.set()
        assert opened.wait(1)
        worker.join(1)
        assert self.case.native_stream.call_count == 1
        assert self.detector._thread is not worker

    def test_newer_disabled_intent_cancels_queued_resume(self):
        facade, worker, release, _ = self.pending_reenable()
        facade.sync_to_state("disabled", False)
        self.finish_without_resume(worker, release)

    def test_newer_mute_cancels_queued_resume(self):
        facade, worker, release, _ = self.pending_reenable()
        facade.sync_to_state("wake_word", True)
        self.finish_without_resume(worker, release)

    def test_new_owner_cancels_queued_resume(self):
        facade, worker, release, _ = self.pending_reenable()
        replacement = self.make_detector(Mock())
        facade.set_instance(replacement)
        self.finish_without_resume(worker, release)
        replacement._impl.run.assert_not_called()

    def test_owner_aba_cancels_queued_resume(self):
        facade, worker, release, _ = self.pending_reenable()
        facade.set_instance(self.make_detector(Mock()))
        facade.set_instance(self.detector)
        self.finish_without_resume(worker, release)

    def test_parent_shutdown_cancels_queued_resume(self):
        _, worker, release, _ = self.pending_reenable()
        self.parent_stop.set()
        self.finish_without_resume(worker, release)

    def test_direct_stop_cancels_queued_resume(self):
        _, worker, release, _ = self.pending_reenable()
        self.detector.stop()
        self.finish_without_resume(worker, release)

    def test_restart_retires_callback_already_released_by_old_worker(self):
        facade, worker, release, _ = self.pending_reenable()
        callback_entered, callback_release = threading.Event(), threading.Event()
        self._releases.append(callback_release)
        actual = facade._resume_current_after_drain

        def paused_callback(cls, *args):
            callback_entered.set()
            assert callback_release.wait(2)
            actual(*args)

        facade._resume_current_after_drain = classmethod(paused_callback)
        release.set()
        assert callback_entered.wait(1)
        generation = facade._state_generation
        assert facade.begin_restart(self.detector) == generation
        # begin_restart deliberately preserves facade intent/ticket for its
        # own prepared replacement. The detector stop must retire this callback.
        callback_release.set()
        worker.join(1)
        assert not worker.is_alive()
        self.resume_start.assert_not_called()
        self.case.native_stream.assert_not_called()

    def test_newer_start_retires_callback_without_duplicating_capture(self):
        facade, worker, release, opened = self.pending_reenable()
        callback_entered, callback_release = threading.Event(), threading.Event()
        self._releases.append(callback_release)
        actual = facade._resume_current_after_drain

        def paused_callback(cls, *args):
            callback_entered.set()
            assert callback_release.wait(2)
            actual(*args)

        facade._resume_current_after_drain = classmethod(paused_callback)
        release.set()
        assert callback_entered.wait(1)
        facade.sync_to_state("wake_word", False)
        assert opened.wait(1)
        resumed = self.detector._thread
        callback_release.set()
        worker.join(1)
        assert not worker.is_alive()
        assert self.detector._thread is resumed
        assert self.case.native_stream.call_count == 1

    def test_drain_before_registration_falls_back_to_one_start(self):
        facade, worker, release, opened = self.pending_reenable()
        # Replace the queued intent with another enable whose registration
        # loses the race to a completed drain. It must neither lose nor duplicate.
        actual = self.detector.when_drained

        def finish_before_register(callback):
            release.set()
            # The old callback waits on the facade lock held by this sync;
            # retirement itself must not hold/acquire that lock.
            deadline = __import__("time").monotonic() + 1
            while self.detector.is_running() and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.001)
            assert not self.detector.is_running()
            return actual(callback)

        self.detector.when_drained = finish_before_register
        facade.sync_to_state("wake_word", False)
        assert opened.wait(1)
        worker.join(1)
        assert not worker.is_alive()
        assert self.detector._stop_event is self.parent_stop
        assert self.case.native_stream.call_count == 1

    def test_shutdown_during_drain_registration_cannot_fall_back_to_fresh_event(self):
        facade, worker, release, opened = self.pending_reenable()
        actual = self.detector.when_drained

        def finish_before_register(callback):
            self.parent_stop.set()
            release.set()
            deadline = __import__("time").monotonic() + 1
            while self.detector.is_running() and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.001)
            assert not self.detector.is_running()
            return actual(callback)

        self.detector.when_drained = finish_before_register
        facade.sync_to_state("wake_word", False)
        worker.join(1)
        assert not worker.is_alive()
        assert not self.detector.is_running()
        self.resume_start.assert_called_once_with(self.parent_stop)
        assert not opened.is_set()
        self.case.native_stream.assert_not_called()

    def test_ordinary_worker_exit_does_not_create_unrequested_restart_loop(self):
        implementation = Mock()
        detector = self.make_detector(implementation)
        self.detector = detector
        facade = self.namespace["WakeDetectorFacade"]
        facade.set_instance(detector)
        facade.sync_to_state("wake_word", False)
        worker = detector._thread
        if worker is not None:
            self._workers.append(worker)
            worker.join(1)
            assert not worker.is_alive()
        assert not detector.is_running()
        implementation.run.assert_called_once()


if __name__ == "__main__":
    if sys.argv[1:] == ["--device-open-failure"]:
        faulthandler.dump_traceback_later(2)
        case = WakeListenerLifecycleTests()
        case.setUp()
        try:
            audio, stream = case.prepare_capture()
            case.native_stream.side_effect = OSError("synthetic device unavailable")
            case.stop_after_next_init()
            case.listener.run(case.stop)
            case.assert_released()
            case.assertTrue(case.listener._capture_open_failed.is_set())
            case.native_terminate.assert_called_once_with(audio)
        finally:
            faulthandler.cancel_dump_traceback_later()
            case.doCleanups()
    else:
        unittest.main()
