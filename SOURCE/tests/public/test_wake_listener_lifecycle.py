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

    def test_real_failed_init_unregisters_on_stop(self):
        self.stop.set()
        self.listener.run(self.stop)
        self.assertTrue(self.listener._capture_open_failed.is_set())
        self.native_open.assert_not_called()
        self.watcher.assert_called_once_with()
        self.policy.reset.assert_called_once_with()
        self.assert_released()

    def test_repeated_supervisor_replacements_do_not_accumulate_owners(self):
        other_owner = object()
        self.registry.register_stream_owner(other_owner)
        self.stop.set()
        for _ in range(3):
            listener = self.make_listener()
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
        self.stop.set()
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
        self.stop.set()
        self.listener.run(self.stop)
        self.assertTrue(self.listener._capture_open_failed.is_set())
        self.assertEqual(self.listener.detection_signal.count, 0)
        for _ in range(2):
            _, stream = self.prepare_capture()
            self.stop.clear()

            def read_frame(*args, **kwargs):
                self.assertIn(self.listener, self.registry._snapshot_owners())
                self.assertTrue(self.listener._loop_active.is_set())
                self.stop.set()
                return np.ones(self.listener.CHUNK_SIZE, dtype=np.int16).tobytes()

            stream.read.side_effect = read_frame
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


if __name__ == "__main__":
    if sys.argv[1:] == ["--device-open-failure"]:
        faulthandler.dump_traceback_later(2)
        case = WakeListenerLifecycleTests()
        case.setUp()
        try:
            audio, stream = case.prepare_capture()
            case.native_stream.side_effect = OSError("synthetic device unavailable")
            case.stop.set()
            case.listener.run(case.stop)
            case.assert_released()
            case.assertTrue(case.listener._capture_open_failed.is_set())
            case.native_terminate.assert_called_once_with(audio)
        finally:
            faulthandler.cancel_dump_traceback_later()
            case.doCleanups()
    else:
        unittest.main()
