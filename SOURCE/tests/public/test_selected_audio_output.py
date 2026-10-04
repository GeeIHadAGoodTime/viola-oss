"""Selected-output contracts using synthetic PCM, fake devices, and real consumers.

No model, provider, hardware, or physical-audibility claim is made here.
"""

from __future__ import annotations

import contextlib
import io
import runpy
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from audio_core import device_validation as routing
from music.backends.simple_audio import SimpleBackendAudioOutput
from utils.singleton import SingletonManager
from voice.synthesis.kokoro_engine import KokoroTTSEngine


def fake_sounddevice():
    sd = types.ModuleType("sounddevice")
    sd.query_devices = Mock(
        return_value=[
            {"name": "Built-in", "max_output_channels": 2, "hostapi": 0},
            {"name": "USB Speaker", "max_output_channels": 2, "hostapi": 1},
            {"name": "USB Speaker", "max_output_channels": 2, "hostapi": 0},
            {"name": "Microphone", "max_output_channels": 0, "hostapi": 0},
        ]
    )
    sd.query_hostapis = Mock(return_value=[{"name": "Host A"}, {"name": "Host B"}])
    sd.default = types.SimpleNamespace(device=[3, 0])
    sd.play = Mock()
    sd.wait = Mock()
    sd.OutputStream = Mock()
    return sd


class SelectedAudioOutput(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("No network")))
        self.stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("No network")))
        self.sd = fake_sounddevice()
        self.selection = routing.output_device_selection("USB Speaker", "Host B")
        self.manager = types.SimpleNamespace(get=lambda key, default=None: self.selection)
        self.stack.enter_context(patch.object(SingletonManager, "get", return_value=self.manager))
        self.stack.enter_context(patch.dict(sys.modules, {"sounddevice": self.sd}))
        self.broadcast = self.stack.enter_context(
            patch.object(KokoroTTSEngine, "_broadcast_pcm_to_spokes", return_value=False)
        )
        self.notice = self.stack.enter_context(patch.object(KokoroTTSEngine, "_report_inaudible_reply"))
        self.pcm = np.array([0, 1000, -1000, 3000], dtype=np.int16).tobytes()

    def test_stable_token_matches_name_and_host(self):
        self.assertEqual(routing.resolve_output_device(self.sd), 1)
        self.assertEqual(self.sd.default.device, [3, 0])

    def test_exact_legacy_name_precedes_substring(self):
        self.sd.query_devices.return_value.append({"name": "Built-in Plus", "max_output_channels": 2})
        self.assertEqual(routing.resolve_output_device(self.sd, selection="Built-in"), 0)

    def test_unique_legacy_substring_is_supported(self):
        self.assertEqual(routing.resolve_output_device(self.sd, selection="built"), 0)

    def test_ambiguous_legacy_name_falls_back(self):
        self.assertIsNone(routing.resolve_output_device(self.sd, selection="USB Speaker"))

    def test_duplicate_stable_identity_falls_back(self):
        self.sd.query_devices.return_value.append(self.sd.query_devices.return_value[1].copy())
        self.assertIsNone(routing.resolve_output_device(self.sd))

    def test_missing_token_cannot_match_different_host_or_substring(self):
        for name, host in [("USB", "Host B"), ("USB Speaker", "Host C")]:
            with self.subTest(name=name, host=host):
                self.assertIsNone(
                    routing.resolve_output_device(self.sd, selection=routing.output_device_selection(name, host))
                )

    def test_default_does_not_enumerate(self):
        for value in (None, "", "  ", -1, "-1"):
            with self.subTest(value=value):
                self.assertIsNone(routing.resolve_output_device(self.sd, selection=value))
        self.sd.query_devices.assert_not_called()

    def test_invalid_selection_falls_back(self):
        for value in (True, [], {}, -2, "portaudio:[]", "portaudio:bad", 'portaudio:{"name":"x"}'):
            with self.subTest(value=value):
                self.assertIsNone(routing.resolve_output_device(self.sd, selection=value))

    def test_input_only_is_not_an_output(self):
        self.assertIsNone(routing.resolve_output_device(self.sd, selection="Microphone"))

    def test_device_query_error_falls_back(self):
        self.sd.query_devices.side_effect = OSError("unplugged")
        self.assertIsNone(routing.resolve_output_device(self.sd))

    def test_host_query_error_falls_back(self):
        self.sd.query_hostapis.side_effect = OSError("missing host")
        self.assertIsNone(routing.resolve_output_device(self.sd))

    def test_legacy_index_resolves_via_pyaudio_not_sounddevice(self):
        pa = types.SimpleNamespace(
            get_device_info_by_index=Mock(return_value={"name": "USB Speaker", "maxOutputChannels": 2, "hostApi": 7}),
            get_host_api_info_by_index=Mock(return_value={"name": "Host B"}),
        )
        with patch.object(routing, "portaudio_instance", return_value=contextlib.nullcontext(pa)):
            self.assertEqual(routing.resolve_output_device(self.sd, selection="9"), 1)
        pa.get_device_info_by_index.assert_called_once_with(9)
        pa.get_host_api_info_by_index.assert_called_once_with(7)

    def test_missing_legacy_index_falls_back_without_using_same_sd_index(self):
        with patch.object(routing, "portaudio_instance", side_effect=OSError("no device")):
            self.assertIsNone(routing.resolve_output_device(self.sd, selection="1"))

    def test_cold_settings_uses_appconfig_without_constructing_manager(self):
        with (
            patch.object(SingletonManager, "get", return_value=None),
            patch.object(routing.settings, "output_device", self.selection),
            patch.object(SingletonManager, "get_or_create", side_effect=AssertionError("cold construction")),
        ):
            self.assertEqual(routing.resolve_output_device(self.sd), 1)

    def test_live_explicit_default_overrides_old_appconfig(self):
        self.selection = ""
        with patch.object(routing.settings, "output_device", "Built-in"):
            self.assertIsNone(routing.resolve_output_device(self.sd))

    def test_settings_read_failure_is_logged_and_defaults(self):
        self.manager.get = Mock(side_effect=RuntimeError("unavailable settings"))
        with patch.object(routing, "logger") as logger:
            self.assertIsNone(routing.resolve_output_device(self.sd))
        logger.warning.assert_called_once()

    def test_reordering_and_disappearance_are_resolved_at_each_open(self):
        self.assertEqual(routing.resolve_output_device(self.sd), 1)
        self.sd.query_devices.return_value.reverse()
        self.assertEqual(routing.resolve_output_device(self.sd), 2)
        self.sd.query_devices.return_value.pop(2)
        self.assertIsNone(routing.resolve_output_device(self.sd))

    def test_kokoro_routes_pcm_without_changing_gain_or_spoke_payload(self):
        self.broadcast.return_value = True
        self.assertTrue(KokoroTTSEngine._play_pcm_raw(self.pcm, 16000))
        args, kwargs = self.sd.play.call_args
        np.testing.assert_array_equal(args[0], np.frombuffer(self.pcm, dtype=np.int16).astype(np.float32) / 32767)
        self.assertEqual(kwargs, {"samplerate": 16000, "device": 1})
        self.broadcast.assert_called_once_with(self.pcm, 16000)
        self.sd.wait.assert_called_once_with()
        self.notice.assert_not_called()

    def test_kokoro_missing_selected_device_uses_default(self):
        self.selection = routing.output_device_selection("Disconnected", "Host B")
        self.assertTrue(KokoroTTSEngine._play_pcm_raw(self.pcm))
        self.assertIsNone(self.sd.play.call_args.kwargs["device"])

    def test_kokoro_selected_open_failure_is_not_replayed_on_default(self):
        self.sd.play.side_effect = OSError("device removed after lookup")
        self.assertFalse(KokoroTTSEngine._play_pcm_raw(self.pcm))
        self.sd.play.assert_called_once()
        self.sd.wait.assert_not_called()
        self.broadcast.assert_called_once()
        self.notice.assert_called_once()

    def test_kokoro_midplay_failure_is_not_replayed(self):
        self.sd.wait.side_effect = OSError("unplugged mid-play")
        self.assertFalse(KokoroTTSEngine._play_pcm_raw(self.pcm))
        self.sd.play.assert_called_once()
        # Existing broadcaster retries only when it reported no successful delivery.
        self.assertEqual(self.broadcast.call_count, 2)
        self.notice.assert_called_once()

    def test_kokoro_spoke_success_survives_local_failure(self):
        self.sd.play.side_effect = OSError("no local sink")
        self.broadcast.return_value = True
        self.assertTrue(KokoroTTSEngine._play_pcm_raw(self.pcm))
        self.notice.assert_not_called()

    def test_kokoro_guard_spans_play_and_wait(self):
        from audio_core import portaudio_guard

        inside = []

        @contextlib.contextmanager
        def guard():
            inside.append(True)
            try:
                yield
            finally:
                inside.pop()

        self.sd.play.side_effect = lambda *a, **kw: self.assertTrue(inside)
        self.sd.wait.side_effect = lambda: self.assertTrue(inside)
        with patch.object(portaudio_guard, "sounddevice_playback_guard", guard):
            self.assertTrue(KokoroTTSEngine._play_pcm_raw(self.pcm))
        self.assertFalse(inside)

    def backend(self, stream=None):
        backend = types.SimpleNamespace(
            _config=types.SimpleNamespace(progress_interval_sec=0.1),
            _sample_rate=48000,
            _channels=2,
            _stream=None,
            _paused=False,
            _stop_event=threading.Event(),
            _play_generation=1,
            _is_playing=False,
            _pos_lock=threading.Lock(),
            _position_frames=0,
            _duration_frames=None,
            _emit_progress=Mock(),
            _audio_tee=Mock(),
            _playback_error=None,
            _proc=None,
        )
        self.stream = stream or types.SimpleNamespace(start=Mock(), write=Mock(), abort=Mock(), close=Mock())
        self.sd.OutputStream.return_value = self.stream
        pipeline = types.ModuleType("audio_core.streaming.pipeline_wiring")
        self.stamper = types.SimpleNamespace(_bit_depth=16, on_capture_data=Mock())
        pipeline.get_active_chunk_stamper = lambda: self.stamper
        self.injection = Mock()
        pipeline.set_direct_injection = self.injection
        self.stack.enter_context(patch.dict(sys.modules, {pipeline.__name__: pipeline}))
        self.audio = SimpleBackendAudioOutput(backend, sounddevice_module=self.sd)
        self.track = np.arange(1920, dtype=np.int16).tobytes()
        return backend

    def test_music_routes_selected_and_preserves_pcm_progress_and_multiroom(self):
        backend = self.backend()
        self.audio.playback_loop(io.BytesIO(self.track), generation=1)
        self.assertEqual(self.sd.OutputStream.call_args.kwargs["device"], 1)
        np.testing.assert_array_equal(
            self.stream.write.call_args.args[0], np.frombuffer(self.track, dtype=np.int16).reshape(-1, 2)
        )
        self.assertEqual(backend._position_frames, 960)
        backend._audio_tee.write.assert_called_once_with(self.track)
        self.stamper.on_capture_data.assert_called_once_with(self.track, 48000, 2, 2)
        self.assertEqual(self.injection.call_args_list[0].args, (True,))
        self.assertEqual(self.injection.call_args_list[-1].args, (False,))
        self.stream.close.assert_called_once()
        self.assertIsNone(backend._stream)

    def test_music_selected_open_failure_is_honest_and_not_retried(self):
        backend = self.backend()
        self.sd.OutputStream.side_effect = OSError("unplugged")
        self.audio.playback_loop(io.BytesIO(self.track), generation=1)
        self.sd.OutputStream.assert_called_once()
        self.assertFalse(backend._is_playing)
        self.assertIn("Audio playback failed", backend._playback_error)
        self.assertEqual(backend._position_frames, 0)
        backend._audio_tee.write.assert_not_called()

    def test_music_start_failure_closes_owned_stream(self):
        backend = self.backend()
        self.stream.start.side_effect = OSError("unplugged")
        self.audio.playback_loop(io.BytesIO(self.track), generation=1)
        self.stream.abort.assert_called_once()
        self.stream.close.assert_called_once()
        self.assertIsNone(backend._stream)
        self.assertFalse(backend._is_playing)

    def test_music_midwrite_unplug_does_not_replay_or_advance(self):
        backend = self.backend()
        self.stream.write.side_effect = OSError("unplugged")
        self.audio.playback_loop(io.BytesIO(self.track), generation=1)
        self.sd.OutputStream.assert_called_once()
        self.stream.write.assert_called_once()
        self.assertEqual(backend._position_frames, 0)
        self.assertFalse(backend._is_playing)
        self.assertIsNotNone(backend._playback_error)
        self.stream.close.assert_called_once()

    def test_music_stale_generation_cannot_clear_new_stream_or_progress(self):
        backend = self.backend()
        newer = object()

        def supersede(*args):
            backend._play_generation = 2
            backend._stream = newer
            backend._is_playing = True

        self.stream.write.side_effect = supersede
        self.audio.playback_loop(io.BytesIO(self.track), generation=1)
        self.assertIs(backend._stream, newer)
        self.assertTrue(backend._is_playing)
        self.assertEqual(backend._position_frames, 0)
        self.stream.close.assert_called_once()
        self.injection.assert_called_once_with(True)

    def test_music_pause_preserves_decoded_pcm(self):
        backend = self.backend()
        backend._paused = True
        calls = []

        def resume_on_silence(samples):
            calls.append(samples.copy())
            backend._paused = False

        self.stream.write.side_effect = resume_on_silence
        self.audio.playback_loop(io.BytesIO(self.track), generation=1)
        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[0].any())
        np.testing.assert_array_equal(calls[1], np.frombuffer(self.track, dtype=np.int16).reshape(-1, 2))
        self.assertEqual(backend._position_frames, 960)

    def test_music_new_stream_observes_selection_change(self):
        self.backend()
        self.audio.playback_loop(io.BytesIO(self.track), generation=1)
        self.selection = "Built-in"
        self.audio.playback_loop(io.BytesIO(self.track), generation=1)
        self.assertEqual([call.kwargs["device"] for call in self.sd.OutputStream.call_args_list], [1, 0])


class SelectedOutputPersistence(unittest.TestCase):
    def test_api_save_restart_routes_stable_selection(self):
        # Load this exact sibling under both unittest discovery and pytest importlib mode.
        SpeechVolumeWiring = runpy.run_path(str(Path(__file__).with_name("test_speech_volume_wiring.py")))[
            "SpeechVolumeWiring"
        ]

        SpeechVolumeWiring.setUpClass()
        fixture = SpeechVolumeWiring()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        selection = routing.output_device_selection("USB Speaker", "Host B")
        response = fixture.post_values({"output_device": selection})
        self.assertEqual(response.status_code, 200, response.text)
        fixture.manager = fixture.new_manager()
        self.assertEqual(fixture.manager.get("output_device"), selection)
        with patch.object(SingletonManager, "get", return_value=fixture.manager):
            self.assertEqual(routing.resolve_output_device(fake_sounddevice()), 1)

    def test_devices_api_emits_output_identity_without_changing_input_indices(self):
        import asyncio
        import httpx
        from audio_core import portaudio_guard

        # Load this exact sibling under both unittest discovery and pytest importlib mode.
        SpeechVolumeWiring = runpy.run_path(str(Path(__file__).with_name("test_speech_volume_wiring.py")))[
            "SpeechVolumeWiring"
        ]

        SpeechVolumeWiring.setUpClass()
        fixture = SpeechVolumeWiring()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        pa = types.SimpleNamespace(
            get_device_count=Mock(return_value=2),
            get_device_info_by_index=Mock(
                side_effect=[
                    {"name": "Microphone", "maxInputChannels": 1, "maxOutputChannels": 0, "hostApi": 7},
                    {"name": "USB Speaker", "maxInputChannels": 0, "maxOutputChannels": 2, "hostApi": 7},
                ]
            ),
            get_host_api_info_by_index=Mock(return_value={"name": "Host B"}),
        )

        async def read():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=fixture.app), base_url="http://speech.test"
            ) as client:
                return await client.get("/v1/settings/devices")

        with (
            patch.object(portaudio_guard, "open_portaudio", return_value=pa),
            patch.object(portaudio_guard, "terminate_portaudio") as terminate,
        ):
            response = asyncio.run(read())
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()["data"]
        self.assertEqual(
            payload["output_devices"],
            [
                {
                    "index": 1,
                    "name": "USB Speaker",
                    "channels": 2,
                    "hostapi": "Host B",
                    "selection": routing.output_device_selection("USB Speaker", "Host B"),
                }
            ],
        )
        self.assertEqual(payload["input_devices"], [{"index": 0, "name": "Microphone", "channels": 1}])
        terminate.assert_called_once_with(pa)


class SharedSounddeviceOutput(unittest.TestCase):
    def setUp(self):
        from audio_core.output.sounddevice_output import SounddeviceAudioOutput

        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("No network")))
        self.sd = fake_sounddevice()
        self.stream = types.SimpleNamespace(start=Mock(), write=Mock(), abort=Mock(), close=Mock())
        self.sd.RawOutputStream = Mock(return_value=self.stream)
        self.selection = routing.output_device_selection("USB Speaker", "Host B")
        manager = types.SimpleNamespace(get=lambda *a: self.selection)
        self.stack.enter_context(patch.object(SingletonManager, "get", return_value=manager))
        self.stack.enter_context(patch.dict(sys.modules, {"sounddevice": self.sd}))
        self.driver = SounddeviceAudioOutput()
        self.addCleanup(self.driver.stop)

    def test_shared_driver_routes_selected_stream_and_preserves_pcm(self):
        self.driver.start(48000, 2, 2)
        self.assertEqual(self.sd.RawOutputStream.call_args.kwargs["device"], 1)
        pcm = np.array([1, -2, 3, -4], dtype=np.int16)
        self.driver.write(pcm.tobytes())
        np.testing.assert_array_equal(self.stream.write.call_args.args[0], pcm)
        self.driver.stop()
        self.stream.close.assert_called_once()

    def test_shared_restart_observes_setting_change(self):
        self.driver.start(48000, 2, 2)
        self.selection = "Built-in"
        self.driver.start(48000, 2, 2)
        self.sd.RawOutputStream.assert_called_once()
        self.driver.stop()
        self.driver.start(48000, 2, 2)
        self.assertEqual(self.sd.RawOutputStream.call_args.kwargs["device"], 0)

    def test_shared_start_failure_closes_unpublished_stream_without_retry(self):
        self.stream.start.side_effect = OSError("device vanished")
        with self.assertRaises(OSError):
            self.driver.start(48000, 2, 2)
        self.sd.RawOutputStream.assert_called_once()
        self.stream.abort.assert_called_once()
        self.stream.close.assert_called_once()
        self.assertFalse(self.driver._running)
        self.assertIsNone(self.driver._session)

    def test_shared_open_failure_is_not_retried(self):
        self.sd.RawOutputStream.side_effect = OSError("device unavailable")
        with self.assertRaises(OSError):
            self.driver.start(48000, 2, 2)
        self.sd.RawOutputStream.assert_called_once()
        self.assertFalse(self.driver._running)

    def test_shared_wedged_old_writer_keeps_close_ownership_after_restart(self):
        from audio_core.output import sounddevice_output

        entered = threading.Event()
        release = threading.Event()

        def wait_write(*args):
            entered.set()
            release.wait(2)

        self.stream.write.side_effect = wait_write
        self.driver.start(48000, 2, 2)
        old = self.stream
        writer = threading.Thread(target=self.driver.write, args=(b"\0\0",))
        writer.start()
        self.addCleanup(lambda: (release.set(), writer.join(3)))
        self.assertTrue(entered.wait(2))
        with patch.object(sounddevice_output, "_STOP_DRAIN_TIMEOUT_SEC", 0.01):
            self.driver.stop()
        old.close.assert_not_called()
        new = types.SimpleNamespace(start=Mock(), write=Mock(), abort=Mock(), close=Mock())
        self.sd.RawOutputStream.return_value = new
        self.selection = "Built-in"
        self.driver.start(48000, 2, 2)
        release.set()
        writer.join(2)
        self.assertFalse(writer.is_alive())
        old.close.assert_called_once()
        new.close.assert_not_called()
        self.assertIs(self.driver._session.stream, new)

    def test_shared_write_failure_detaches_closes_and_propagates(self):
        self.driver.start(48000, 2, 2)
        failure = OSError("selected speaker unplugged")
        self.stream.write.side_effect = failure
        with self.assertRaises(OSError) as raised:
            self.driver.write(b"\0\0")
        self.assertIs(raised.exception, failure)
        self.assertFalse(self.driver._running)
        self.assertIsNone(self.driver._session)
        self.assertEqual(self.driver.runtime_failure["reason"], "output_write_failed")
        self.assertIsInstance(self.driver.runtime_failure["since_epoch"], float)
        exposed = self.driver.runtime_failure
        exposed["reason"] = "caller mutation"
        self.assertEqual(self.driver.runtime_failure["reason"], "output_write_failed")
        self.stream.abort.assert_called_once()
        self.stream.close.assert_called_once()
        with self.assertRaisesRegex(RuntimeError, "output write failed"):
            self.driver.write(b"\0\0")
        self.driver.stop()
        self.stream.write.assert_called_once()
        self.stream.close.assert_called_once()
        self.sd.RawOutputStream.assert_called_once()

    def test_shared_successful_restart_clears_write_failure(self):
        self.driver.start(48000, 2, 2)
        self.stream.write.side_effect = OSError("unplugged")
        with self.assertRaises(OSError):
            self.driver.write(b"\0\0")
        new = types.SimpleNamespace(start=Mock(), write=Mock(), abort=Mock(), close=Mock())
        self.sd.RawOutputStream.return_value = new
        self.driver.start(48000, 2, 2)
        self.assertIsNone(self.driver.runtime_failure)
        self.driver.write(b"\0\0")
        new.write.assert_called_once()

    def test_shared_failed_restart_does_not_clear_write_failure(self):
        self.driver.start(48000, 2, 2)
        self.stream.write.side_effect = OSError("unplugged")
        with self.assertRaises(OSError):
            self.driver.write(b"\0\0")
        failure = self.driver.runtime_failure
        new = types.SimpleNamespace(start=Mock(side_effect=OSError("still missing")), abort=Mock(), close=Mock())
        self.sd.RawOutputStream.return_value = new
        with self.assertRaises(OSError):
            self.driver.start(48000, 2, 2)
        self.assertEqual(self.driver.runtime_failure, failure)
        self.assertFalse(self.driver._running)
        new.close.assert_called_once()

    def test_shared_old_write_failure_cannot_poison_new_stream(self):
        from audio_core.output import sounddevice_output

        entered, release = threading.Event(), threading.Event()
        errors = []

        def old_write(*args):
            entered.set()
            release.wait(2)
            raise OSError("old device vanished")

        def write():
            try:
                self.driver.write(b"\0\0")
            except OSError as exc:
                errors.append(exc)

        self.stream.write.side_effect = old_write
        self.driver.start(48000, 2, 2)
        old = self.stream
        worker = threading.Thread(target=write)
        worker.start()
        self.addCleanup(lambda: (release.set(), worker.join(3)))
        self.assertTrue(entered.wait(2))
        with patch.object(sounddevice_output, "_STOP_DRAIN_TIMEOUT_SEC", 0.01):
            self.driver.stop()
        new = types.SimpleNamespace(start=Mock(), write=Mock(), abort=Mock(), close=Mock())
        self.sd.RawOutputStream.return_value = new
        self.driver.start(48000, 2, 2)
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsNone(self.driver.runtime_failure)
        self.assertTrue(self.driver._running)
        self.assertIs(self.driver._session.stream, new)
        old.close.assert_called_once()
        new.close.assert_not_called()

    def test_shared_failed_writer_waits_for_other_writer_before_close(self):
        entered, release = threading.Event(), threading.Event()

        def blocked_write(*args):
            entered.set()
            release.wait(2)

        self.stream.write.side_effect = blocked_write
        self.driver.start(48000, 2, 2)
        worker = threading.Thread(target=self.driver.write, args=(b"\0\0",))
        worker.start()
        self.addCleanup(lambda: (release.set(), worker.join(3)))
        self.assertTrue(entered.wait(2))
        self.stream.write.side_effect = OSError("second writer sees disconnect")
        with self.assertRaises(OSError):
            self.driver.write(b"\0\0")
        self.stream.close.assert_not_called()
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.stream.close.assert_called_once()

    def test_shared_two_failed_writers_close_exactly_once(self):
        ready = threading.Barrier(2)
        errors = []

        def fail_write(*args):
            ready.wait(timeout=2)
            raise OSError("device disappeared")

        def write():
            try:
                self.driver.write(b"\0\0")
            except OSError as exc:
                errors.append(exc)

        self.stream.write.side_effect = fail_write
        self.driver.start(48000, 2, 2)
        workers = [threading.Thread(target=write) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 2)
        self.assertEqual(self.driver.runtime_failure["reason"], "output_write_failed")
        self.assertFalse(self.driver._running)
        self.stream.abort.assert_called_once()
        self.stream.close.assert_called_once()

    def test_playback_loop_stops_consuming_after_output_failure(self):
        from audio_core.output.playback_loop import SpokePlaybackLoop

        self.driver.start(48000, 2, 2)
        self.stream.write.side_effect = OSError("unplugged")
        stop = threading.Event()
        calls = []

        def next_chunk():
            calls.append(1)
            if len(calls) > 1:
                stop.set()
            return types.SimpleNamespace(pcm_data=b"\0\0")

        loop = SpokePlaybackLoop(types.SimpleNamespace(get_next_chunk=next_chunk), self.driver)
        loop._run(stop)
        self.assertEqual(len(calls), 1)
        self.stream.write.assert_called_once()


class PipelineOutputHealth(unittest.TestCase):
    def run_pipeline(self, failure=None):
        from audio_core.output.sounddevice_output import SounddeviceAudioOutput
        from audio_core.streaming import pipeline_wiring as pipeline
        from ui.api.routes import health

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("No network")))
            modules = {}
            for name, klass in (
                ("audio_core.output.playback_loop", "SpokePlaybackLoop"),
                ("audio_core.sync_engine", "SyncEngine"),
                ("audio_core.streaming.playback_scheduler", "PlaybackScheduler"),
                ("audio_core.streaming.spoke_receiver", "SpokeAudioReceiver"),
                ("core.events.bus", "LocalEventBus"),
            ):
                module = types.ModuleType(name)
                setattr(module, klass, Mock())
                modules[name] = module
            calibration = types.ModuleType("audio_core.calibration.device_latency")
            calibration.detect_output_latency = Mock(return_value={})
            modules[calibration.__name__] = calibration
            sd = fake_sounddevice()
            stream = types.SimpleNamespace(start=Mock(), write=Mock(), abort=Mock(), close=Mock())
            sd.RawOutputStream = Mock(return_value=stream)
            if failure == "open":
                sd.RawOutputStream.side_effect = OSError("selected device open failed")
            elif failure == "start":
                stream.start.side_effect = OSError("selected device start failed")
            modules["sounddevice"] = sd
            stack.enter_context(patch.dict(sys.modules, modules))
            selection = routing.output_device_selection("USB Speaker", "Host B")
            manager = types.SimpleNamespace(get=lambda *args: selection)
            stack.enter_context(patch.object(SingletonManager, "get", return_value=manager))
            stack.enter_context(patch("audio_core.output.get_output_driver", return_value=SounddeviceAudioOutput()))
            stack.enter_context(patch.object(pipeline, "_initial_clock_sync", return_value=0))
            stack.enter_context(patch.object(pipeline, "_output_health_driver", None, create=True))
            stack.enter_context(
                patch.dict(
                    pipeline._output_health_state, {"state": "not_started", "provider": None, "fallback_reason": None}
                )
            )
            app = types.SimpleNamespace(state=types.SimpleNamespace())
            result = pipeline.setup_device_pipeline(app, "synthetic-room")
            if failure == "write":
                stream.write.side_effect = OSError("selected speaker unplugged")
                with self.assertRaises(OSError):
                    app.state.device_output_driver.write(b"\0\0")
            snapshot = pipeline.get_output_health()
            api_health = health._check_audio_output_health()
            if failure == "write":
                self.assertIsNotNone(result)
                self.assertEqual(snapshot["state"], "error")
                self.assertEqual(snapshot["fallback_reason"], "output_write_failed")
                self.assertEqual(api_health["status"], "error")
                self.assertEqual(api_health["reason"], "output_write_failed")
                self.assertIn("stopped", api_health["message"])
                stream.close.assert_called_once()
            elif failure:
                self.assertIsNone(result)
                self.assertEqual(snapshot["state"], "error")
                self.assertEqual(snapshot["fallback_reason"], "output_start_failed")
                self.assertEqual(api_health["status"], "error")
                self.assertEqual(api_health["reason"], "output_start_failed")
                modules["audio_core.sync_engine"].SyncEngine.return_value.stop.assert_called_once()
                modules["audio_core.streaming.spoke_receiver"].SpokeAudioReceiver.return_value.stop.assert_called_once()
                modules["core.events.bus"].LocalEventBus.return_value.unsubscribe.assert_called_once()
                self.assertIsNone(app.state.device_sync_engine)
                self.assertFalse(hasattr(app.state, "device_output_driver"))
                if failure == "start":
                    stream.close.assert_called_once()
            else:
                self.assertIsNotNone(result)
                self.assertEqual(snapshot["state"], "ok")
                self.assertEqual(api_health["status"], "ok")
                self.assertIsNotNone(app.state.device_output_driver)
                app.state.device_output_driver.stop()
            sd.RawOutputStream.assert_called_once()
            self.assertEqual(sd.RawOutputStream.call_args.kwargs["device"], 1)

    def test_pipeline_open_failure_and_health_api_are_truthful(self):
        self.run_pipeline("open")

    def test_pipeline_start_failure_and_health_api_are_truthful(self):
        self.run_pipeline("start")

    def test_pipeline_success_remains_healthy(self):
        self.run_pipeline()

    def test_pipeline_midplay_disconnect_and_health_api_are_truthful(self):
        self.run_pipeline("write")

    def test_replaced_driver_failure_does_not_poison_new_health(self):
        from audio_core.streaming import pipeline_wiring as pipeline

        old = types.SimpleNamespace(runtime_failure={"reason": "output_write_failed", "since_epoch": 123.0})
        new = types.SimpleNamespace(runtime_failure=None)
        with (
            patch.dict(pipeline._output_health_state),
            patch.object(pipeline, "_output_health_driver", None),
        ):
            pipeline._set_output_health("ok", provider="OldDriver", driver=old)
            self.assertEqual(pipeline.get_output_health()["state"], "error")
            self.assertEqual(pipeline.get_output_health()["since_epoch"], 123.0)
            pipeline._set_output_health("ok", provider="NewDriver", driver=new)
            snapshot = pipeline.get_output_health()
            self.assertEqual(snapshot["state"], "ok")
            self.assertEqual(snapshot["provider"], "NewDriver")
            pipeline._set_output_health("not_started")
            self.assertIsNone(pipeline._output_health_driver)
            self.assertEqual(pipeline.get_output_health()["state"], "not_started")

    def test_starting_health_is_not_a_playback_success(self):
        from audio_core.streaming import pipeline_wiring as pipeline
        from ui.api.routes import health

        with patch.object(pipeline, "get_output_health", return_value={"state": "starting"}):
            result = health._check_audio_output_health()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["reason"], "output_starting")

    def test_null_fallback_health_stays_degraded(self):
        from audio_core.streaming import pipeline_wiring as pipeline
        from ui.api.routes import health

        with patch.object(
            pipeline,
            "get_output_health",
            return_value={"state": "degraded", "fallback_reason": "sounddevice_unavailable"},
        ):
            result = health._check_audio_output_health()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["reason"], "sounddevice_unavailable")


if __name__ == "__main__":
    unittest.main()
