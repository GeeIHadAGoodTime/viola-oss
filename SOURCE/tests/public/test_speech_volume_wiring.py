"""Settings-to-speech gain contracts, with synthetic PCM and no audio hardware.

Runs the actual settings API, JSON/SQLite persistence, quiet-hours policy, Kokoro
post-processing and fallback worker. Only models, devices and unrelated services
are replaced. Waveform evidence is not physical-audibility acceptance.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import numpy as np

from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[2]
_SOCKET_CONNECT = socket.socket.connect
_SOCKETPAIR_CODES = frozenset(
    function.__code__
    for function in (socket.socketpair, getattr(socket, "_fallback_socketpair", None))
    if function is not None
)


def _connect_without_network(sock, address):
    # Windows implements asyncio's self-pipe using the stdlib TCP socketpair.
    # Permit only that implementation's own client-to-listener connection, never
    # arbitrary application loopback traffic or a process/thread-wide bypass.
    caller = sys._getframe(1)
    if caller.f_code in _SOCKETPAIR_CODES:
        listener = caller.f_locals.get("lsock")
        if (
            caller.f_locals.get("csock") is sock
            and isinstance(listener, socket.socket)
            and sock.family in (socket.AF_INET, socket.AF_INET6)
            and sock.type == listener.type == socket.SOCK_STREAM
            and sock.family == listener.family
            and address == listener.getsockname()[:2]
            and address[0] in ("127.0.0.1", "::1")
        ):
            return _SOCKET_CONNECT(sock, address)
    raise AssertionError("No network in gain tests")


@contextlib.contextmanager
def _isolated_network():
    with (
        patch.object(socket.socket, "connect", new=_connect_without_network),
        patch.object(socket.socket, "connect_ex", side_effect=AssertionError("No network in gain tests")),
        patch.object(socket, "create_connection", side_effect=AssertionError("No network in gain tests")),
    ):
        yield


class SocketPairNetworkGuard(unittest.TestCase):
    def test_socketpair_is_bidirectional_under_guard(self):
        with _isolated_network():
            left, right = socket.socketpair()
            with left, right:
                left.settimeout(1)
                right.settimeout(1)
                left.sendall(b"left")
                self.assertEqual(right.recv(4), b"left")
                right.sendall(b"right")
                self.assertEqual(left.recv(5), b"right")

    def test_direct_external_and_loopback_connections_stay_blocked(self):
        with _isolated_network():
            for family, host in (
                (socket.AF_INET, "127.0.0.1"),
                (socket.AF_INET, "192.0.2.1"),
                (socket.AF_INET6, "::1"),
                (socket.AF_INET6, "2001:db8::1"),
            ):
                with self.subTest(host=host), socket.socket(family, socket.SOCK_STREAM) as client:
                    for connect in (client.connect, client.connect_ex):
                        with self.assertRaisesRegex(AssertionError, "No network in gain tests"):
                            connect((host, 9))
            with self.assertRaisesRegex(AssertionError, "No network in gain tests"):
                socket.create_connection(("localhost", 9))

    def test_event_loop_and_worker_thread_remain_usable(self):
        async def value():
            return "self-pipe ready"

        with _isolated_network():
            self.assertEqual(asyncio.run(value()), "self-pipe ready")
            results = []
            worker = threading.Thread(target=lambda: results.append(asyncio.run(value())))
            worker.start()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(results, ["self-pipe ready"])

    def test_guard_restores_socket_methods_after_error(self):
        original = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection)
        with self.assertRaisesRegex(RuntimeError, "synthetic guard failure"):
            with _isolated_network():
                raise RuntimeError("synthetic guard failure")
        self.assertEqual((socket.socket.connect, socket.socket.connect_ex, socket.create_connection), original)


class SpeechVolumeWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager_module = importlib.import_module("ui.settings_manager")
        cls.quiet = importlib.import_module("core.quiet_hours")
        cls.kokoro = importlib.import_module("voice.synthesis.kokoro_engine")
        cls.fallback = importlib.import_module("voice.synthesis.engine")
        cls.cache_module = importlib.import_module("voice.synthesis.opener_cache")
        cls.identity = importlib.import_module("core.user_context")
        # Numeric conversion is an unrelated optional dependency; these fixtures
        # contain no digits. Never silently replace the real text normalizer.
        if "voice.synthesis.text_normalizer" not in sys.modules:
            numbers = types.ModuleType("num2words")
            numbers.num2words = Mock(side_effect=AssertionError("No number conversion in gain fixtures"))
            with patch.dict(sys.modules, {"num2words": numbers}):
                cls.normalizer = importlib.import_module("voice.synthesis.text_normalizer")
        else:
            cls.normalizer = sys.modules["voice.synthesis.text_normalizer"]
        # The settings router imports unrelated provider discovery eagerly.
        playlist = types.ModuleType("music.playlist_manager")
        playlist.get_playlist_manager = lambda: types.SimpleNamespace()
        local = types.ModuleType("services.llm.local_models")
        local.detect_local_ai_servers = Mock(side_effect=AssertionError("No provider discovery in gain tests"))
        with patch.dict(sys.modules, {playlist.__name__: playlist, local.__name__: local}):
            cls.api = importlib.import_module("ui.settings_api")
        for module in (cls.manager_module, cls.quiet, cls.kokoro, cls.fallback, cls.cache_module, cls.api):
            assert Path(module.__file__).resolve().is_relative_to(ROOT), module.__file__

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(_isolated_network())
        self.patches.enter_context(patch.object(self.manager_module, "SECURE_SETTINGS_AVAILABLE", False))
        self.patches.enter_context(
            patch.object(
                self.manager_module.SettingsManager,
                "_default_settings_base",
                side_effect=lambda: self.manager_module.SettingsManager.DEFAULT_SETTINGS.copy(),
            )
        )
        self.manager = self.new_manager()
        self.patches.enter_context(
            patch.object(self.manager_module, "get_settings_manager", side_effect=lambda: self.manager)
        )
        self.patches.enter_context(patch.object(self.api, "get_settings_manager", side_effect=lambda: self.manager))
        self.patches.enter_context(patch.object(self.api, "_cloud_sync_consent_api", return_value=None))
        # Use the actual named override, which is intentionally a deterministic test hook.
        self.patches.enter_context(patch.dict("os.environ", {self.quiet.QUIET_HOURS_TIME_OVERRIDE_ENV: "13:00"}))
        telemetry = types.ModuleType("admin.instrumentation")
        telemetry.record_tts_latency = Mock()
        telemetry.record_feature_used = Mock()
        diagnostics = types.ModuleType("diagnostics.wake_state_sync")
        diagnostics.get_state_sync_monitor = Mock(return_value=Mock())
        status = types.ModuleType("diagnostics.voice_status")
        status.get_voice_status = lambda: {"state": "synthetic"}
        ducking = types.ModuleType("utils.audio_ducking")
        ducking.duck_context = contextlib.nullcontext
        auth = types.ModuleType("auth.dependencies")
        auth.require_auth_or_api_key = lambda: None
        self.patches.enter_context(
            patch.dict(
                sys.modules,
                {
                    "voice.synthesis.text_normalizer": self.normalizer,
                    telemetry.__name__: telemetry,
                    diagnostics.__name__: diagnostics,
                    ducking.__name__: ducking,
                    auth.__name__: auth,
                    status.__name__: status,
                },
            )
        )
        self.connection = sqlite3.connect(self.root / "preferences.sqlite", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE user_preferences (user_id TEXT, key TEXT, value TEXT, updated_at TEXT, PRIMARY KEY(user_id, key))"
        )
        self.connection.commit()
        self.addCleanup(self.connection.close)
        database = types.SimpleNamespace(connection=self.connection, transaction=lambda: self.connection)
        self.patches.enter_context(
            patch.object(self.manager_module.SettingsManager, "_get_auth_db", return_value=database)
        )
        # A migration marker avoids any legacy account import or real auth database.
        for user in ("volume-user-a", "volume-user-b"):
            self.connection.execute(
                "INSERT INTO user_preferences VALUES (?, ?, ?, ?)", (user, "__settings_migrated__", "true", "fixture")
            )
        self.connection.commit()
        self.config = types.SimpleNamespace(
            tts_volume=80,
            tts_rate=150,
            tts_enabled=True,
            tts_opener_cache_enabled=False,
            tts_speed_jitter_pct=0,
            tts_post_fx_enabled=True,
            test_mode=False,
        )
        self.engine = self.new_engine()
        self.model_calls = []
        # Deterministic speech-like waveform exercises the real post-FX/declick chain.
        times = np.arange(2400, dtype=np.float32) / 24000
        self.wave = (0.25 * np.sin(2 * np.pi * 220 * times) + 0.05 * np.sin(2 * np.pi * 660 * times)).astype(np.float32)
        self.engine._kokoro = types.SimpleNamespace(create=self.create_audio)
        self.app = FastAPI()

        @self.app.middleware("http")
        async def synthetic_authenticated_user(request, call_next):
            user = request.headers.get("x-fixture-user")
            if user:
                request.state.user_context = types.SimpleNamespace(user_id=user)
                with self.identity.user_scope(user):
                    return await call_next(request)
            return await call_next(request)

        self.app.include_router(self.api.create_settings_router())

    def new_manager(self):
        return self.manager_module.SettingsManager(self.root / "settings.json")

    def new_engine(self, **kwargs):
        return self.kokoro.KokoroTTSEngine(
            config=self.config, model_path=self.root / "model.onnx", voices_path=self.root / "voices.bin", **kwargs
        )

    def create_audio(self, text, **kwargs):
        self.model_calls.append((text, kwargs))
        return self.wave.copy(), 24000

    def post_values(self, values, user=None, method="POST"):
        async def save():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://speech.test"
            ) as client:
                return await client.request(
                    method,
                    "/v1/settings",
                    json={"settings": values},
                    headers={"x-fixture-user": user} if user else {},
                )

        return asyncio.run(save())

    def post_settings(self, value, user=None):
        return self.post_values({"tts_volume": value}, user=user)

    def save_via_api(self, value, user=None):
        response = self.post_settings(value, user)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"], response.text)
        return response

    def render(self):
        return np.frombuffer(asyncio.run(self.engine.synthesize("A synthetic fixture.")), dtype=np.int16)

    def expected(self, unity, volume):
        return (unity.astype(np.float32) * volume).clip(-32767, 32767).astype(np.int16)

    def test_ui_api_saved_volume_reaches_same_and_new_engine(self):
        self.save_via_api(1.0)
        unity = self.render()
        self.assertGreater(np.max(np.abs(unity)), 0)
        for volume in (0.25, 0.01, 0.0, 1.0):
            self.save_via_api(volume)
            self.assertEqual(json.loads(self.manager.settings_file.read_text())["tts_volume"], volume)
            np.testing.assert_array_equal(self.render(), self.expected(unity, volume))
        self.save_via_api(0.5)
        self.manager = self.new_manager()
        self.engine = self.new_engine()
        self.engine._kokoro = types.SimpleNamespace(create=self.create_audio)
        np.testing.assert_array_equal(self.render(), self.expected(unity, 0.5))
        self.assertTrue(
            all(call[1] == {"voice": "af_heart", "speed": 1.0, "lang": "en-us"} for call in self.model_calls)
        )

    def test_quiet_hours_change_is_live_and_applied_exactly_once(self):
        self.manager.update({"tts_volume": 1.0, "quiet_hours_enabled": True, "quiet_hours_timezone": "UTC"})
        unity = self.render()
        with patch.dict("os.environ", {self.quiet.QUIET_HOURS_TIME_OVERRIDE_ENV: "23:30"}):
            self.manager.set("tts_volume", 0.5)
            np.testing.assert_array_equal(self.render(), self.expected(unity, 0.15))
            self.manager.set("quiet_hours_enabled", False)
            np.testing.assert_array_equal(self.render(), self.expected(unity, 0.5))
        self.assertTrue(self.manager_module.SettingsManager.DEFAULT_SETTINGS["quiet_hours_enabled"])

    def test_explicit_override_including_zero_wins_but_keeps_quiet_hours(self):
        self.manager.update({"tts_volume": 0.9, "quiet_hours_enabled": False})
        for explicit, normalized in ((0.0, 0.0), (0.5, 0.5), (50, 0.5), (1.0, 1.0)):
            engine = self.new_engine(volume=explicit)
            self.assertEqual(engine._current_volume(), normalized)
        self.manager.set("quiet_hours_enabled", True)
        with patch.dict("os.environ", {self.quiet.QUIET_HOURS_TIME_OVERRIDE_ENV: "23:30"}):
            self.assertAlmostEqual(self.new_engine(volume=0.5)._current_volume(), 0.15)

    def test_cached_openers_are_unity_on_disk_and_live_at_playback(self):
        self.config.tts_opener_cache_enabled = True
        cache = self.cache_module.OpenerCache(
            voice_id="af_heart",
            voice_blend="",
            speed_default=1.0,
            variants=2,
            cache_dir=self.root / "cache",
            openers=("okay",),
        )
        self.manager.update({"tts_volume": 0.0, "quiet_hours_enabled": True})
        with patch.dict("os.environ", {self.quiet.QUIET_HOURS_TIME_OVERRIDE_ENV: "23:30"}):
            self.assertTrue(cache.build(self.engine._synthesize_opener_cache_variant))
        original = cache.cache_path.read_bytes()
        self.engine._opener_cache = cache
        played = []
        with patch.object(self.engine, "_play_pcm_locally", side_effect=played.append):
            for volume in (1.0, 0.5, 0.0):
                self.manager.set("tts_volume", volume)
                asyncio.run(self.engine.speak("Okay!"))
        unity = np.frombuffer(played[0], dtype=np.int16)
        self.assertGreater(np.max(np.abs(unity)), 0)
        np.testing.assert_array_equal(np.frombuffer(played[1], dtype=np.int16), self.expected(unity, 0.5))
        self.assertFalse(np.any(np.frombuffer(played[2], dtype=np.int16)))
        self.assertEqual(original, cache.cache_path.read_bytes())
        self.assertEqual(len(self.model_calls), 2)
        self.assertTrue(cache.load())
        with patch.dict("os.environ", {self.quiet.QUIET_HOURS_TIME_OVERRIDE_ENV: "23:30"}):
            self.manager.set("tts_volume", 0.5)
            with patch.object(self.engine, "_play_pcm_locally", side_effect=played.append):
                asyncio.run(self.engine.speak("Okay!"))
        np.testing.assert_array_equal(np.frombuffer(played[-1], dtype=np.int16), self.expected(unity, 0.15))
        self.assertEqual(original, cache.cache_path.read_bytes())

    def test_legacy_cache_identity_is_retired(self):
        identity = {"voice_id": "af_heart", "voice_blend": "", "speed_default": 1.0}
        self.assertNotEqual(
            self.cache_module.opener_cache_hash(**identity, schema_version=1),
            self.cache_module.opener_cache_hash(**identity),
        )

    def test_disabled_engine_does_not_play_cached_pcm(self):
        self.config.tts_enabled = False
        self.config.tts_opener_cache_enabled = True
        self.engine._opener_cache = Mock()
        self.engine._opener_cache.lookup.return_value = np.ones(8, dtype=np.int16).tobytes()
        with patch.object(self.engine, "_play_pcm_locally") as play:
            asyncio.run(self.engine.speak("okay"))
        play.assert_not_called()
        self.assertEqual(self.model_calls, [])

    def test_per_user_values_reach_kokoro_and_do_not_leak(self):
        self.manager.update({"tts_volume": 0.9, "quiet_hours_enabled": False})
        for user, volume in (("volume-user-a", 0.25), ("volume-user-b", 0.75)):
            self.assertTrue(self.manager.update({"tts_volume": volume, "quiet_hours_enabled": False}, user_id=user))
        for user, expected in (("volume-user-a", 0.25), ("volume-user-b", 0.75), ("volume-user-a", 0.25)):
            with self.identity.user_scope(user):
                self.assertEqual(self.engine._current_volume(), expected)
                self.assertGreater(self.render().size, 0)
        self.assertEqual(self.engine._current_volume(), 0.9)

    def test_fallback_worker_preserves_request_scope_and_live_updates(self):
        properties = {}
        spoken = []
        fake = types.SimpleNamespace(
            getProperty=lambda name: [],
            setProperty=lambda name, value: properties.update({name: value}),
            say=lambda text: spoken.append((text, properties["volume"])),
            runAndWait=lambda: None,
            stop=lambda: None,
        )
        native = types.ModuleType("pyttsx3")
        initialized = threading.Event()

        def initialize():
            initialized.set()
            return fake

        native.init = initialize
        self.manager.update({"tts_volume": 0.9, "quiet_hours_enabled": False})
        with patch.dict(sys.modules, {"pyttsx3": native}):
            worker = self.fallback.TTSWorker(self.config)
            worker.start()
            try:
                self.assertTrue(initialized.wait(timeout=3))
                for user, volume in (("volume-user-a", 0.25), ("volume-user-b", 0.75), ("volume-user-a", 0.0)):
                    self.manager.update({"tts_volume": volume, "quiet_hours_enabled": False}, user_id=user)
                    with self.identity.user_scope(user):
                        self.assertTrue(worker.queue_speak("fixture", timeout=3))
                self.assertTrue(worker.queue_speak("global", timeout=3))
            finally:
                worker.stop()
                worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(spoken, [("fixture", 0.25), ("fixture", 0.75), ("fixture", 0.0), ("global", 0.9)])

    def test_legacy_global_percentage_is_durable_and_idempotent(self):
        self.manager.settings_file.write_text(json.dumps({"tts_volume": 45, "quiet_hours_enabled": False}))
        for _ in range(2):
            self.manager = self.new_manager()
            self.assertEqual(self.manager.get("tts_volume"), 0.45)
            self.assertEqual(json.loads(self.manager.settings_file.read_text())["tts_volume"], 0.45)

    def test_legacy_sqlite_percentage_is_durable_and_user_isolated(self):
        self.connection.execute(
            "INSERT INTO user_preferences VALUES (?, ?, ?, ?)", ("volume-user-a", "tts_volume", "45", "fixture")
        )
        self.connection.execute(
            "INSERT INTO user_preferences VALUES (?, ?, ?, ?)", ("volume-user-b", "tts_volume", "0.8", "fixture")
        )
        self.connection.commit()
        for _ in range(2):
            self.manager = self.new_manager()
            self.assertEqual(self.manager.get("tts_volume", user_id="volume-user-a"), 0.45)
            self.assertEqual(self.manager.get("tts_volume", user_id="volume-user-b"), 0.8)
            saved = self.connection.execute(
                "SELECT value FROM user_preferences WHERE user_id=? AND key=?", ("volume-user-a", "tts_volume")
            ).fetchone()[0]
            self.assertEqual(json.loads(saved), 0.45)

    def test_legacy_postgres_repo_percentage_preserves_other_fields(self):
        saved = []

        async def get_settings(user):
            return {"tts_volume": 45, "theme": "dark"}, 8

        async def save_settings(user, data, version):
            saved.append((user, json.loads(data), version))

        db = types.SimpleNamespace(
            connection=lambda: None,
            user_settings=types.SimpleNamespace(get_settings=get_settings, save_settings=save_settings),
        )
        with patch.object(self.manager, "_get_auth_db", return_value=db):
            result = self.manager._load_user_settings_blob("volume-user-a")
        self.assertEqual(result, {"tts_volume": 0.45, "theme": "dark"})
        self.assertEqual(saved, [("volume-user-a", result, 8)])

    def test_normalization_preserves_normalized_and_invalid_values(self):
        migrate = self.manager_module.SettingsManager._migrate_tts_volume_percent_scale
        for value in (0, 0.01, 0.5, 1, 1.0, True, "loud", None, -1, 101, float("inf")):
            blob = {"tts_volume": value, "theme": "dark"}
            self.assertFalse(migrate(blob))
            self.assertEqual(blob, {"tts_volume": value, "theme": "dark"})
        for value in (2, 45.0, 100):
            blob = {"tts_volume": value}
            self.assertTrue(migrate(blob))
            self.assertEqual(blob["tts_volume"], value / 100)
            self.assertFalse(migrate(blob))

    def test_per_user_http_save_is_durable_and_reaches_synthesis(self):
        self.manager.update({"tts_volume": 1.0, "quiet_hours_enabled": False})
        unity = self.render()
        for user, volume in (("volume-user-a", 0.25), ("volume-user-b", 0.75), ("volume-user-a", 0.0)):
            self.manager.update({"quiet_hours_enabled": False}, user_id=user)
            self.save_via_api(volume, user=user)
            saved = self.connection.execute(
                "SELECT value FROM user_preferences WHERE user_id=? AND key='tts_volume'", (user,)
            ).fetchone()[0]
            self.assertEqual(json.loads(saved), volume)
            self.manager._user_settings_cache.invalidate(user)
            with self.identity.user_scope(user):
                np.testing.assert_array_equal(self.render(), self.expected(unity, volume))
        self.assertEqual(self.manager.get("tts_volume"), 1.0)

    def test_settings_api_write_failure_stays_an_error(self):
        original = self.connection.execute("SELECT * FROM user_preferences").fetchall()
        with patch.object(self.manager, "_save_user_settings_blob", return_value=False):
            response = self.post_settings(0.25, user="volume-user-a")
        self.assertEqual(response.status_code, 500)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(self.connection.execute("SELECT * FROM user_preferences").fetchall(), original)

    def test_postgres_load_failure_never_migrates_or_overwrites(self):
        writes = []

        async def get_settings(user):
            raise OSError("synthetic unavailable database")

        async def save_settings(*args):
            writes.append(args)

        db = types.SimpleNamespace(
            connection=lambda: None,
            user_settings=types.SimpleNamespace(
                get_settings=get_settings,
                save_settings=save_settings,
            ),
        )
        with patch.object(self.manager, "_get_auth_db", return_value=db):
            with self.assertRaises(self.manager_module.UserSettingsLoadError):
                self.manager._load_user_settings_blob("volume-user-a")
            self.assertFalse(self.manager.update({"tts_volume": 0.25}, user_id="volume-user-a"))
        self.assertEqual(writes, [])

    def test_fallback_reinitialization_and_quiet_hours_resolve_live(self):
        properties = {}
        fake = types.SimpleNamespace(
            getProperty=lambda name: [], setProperty=lambda name, value: properties.update({name: value})
        )
        native = types.ModuleType("pyttsx3")
        native.init = lambda: fake
        worker = self.fallback.TTSWorker(self.config)
        self.manager.update({"tts_volume": 0.5, "quiet_hours_enabled": True})
        with patch.dict(sys.modules, {"pyttsx3": native}):
            worker._init_engine()
            self.assertEqual(properties["volume"], 0.5)
            with patch.dict("os.environ", {self.quiet.QUIET_HOURS_TIME_OVERRIDE_ENV: "23:30"}):
                worker._init_engine()
                self.assertAlmostEqual(properties["volume"], 0.15)

    def test_failed_global_save_never_activates_proposed_gain(self):
        self.manager.update({"tts_volume": 0.75, "quiet_hours_enabled": False})
        for entry in ("api", "set", "update", "pseudo_user", "system"):
            with self.subTest(entry=entry):
                with patch.object(self.manager, "save", return_value=False):
                    if entry == "api":
                        self.assertEqual(self.post_settings(0.25).status_code, 500)
                    elif entry == "set":
                        self.assertFalse(self.manager.set("tts_volume", 0.25))
                    elif entry == "pseudo_user":
                        self.assertFalse(self.manager.set_user_setting("device-fixture", "tts_volume", 0.25))
                    elif entry == "system":
                        self.assertFalse(self.manager.set_system_value("tts_volume", 0.25))
                    else:
                        self.assertFalse(self.manager.update({"tts_volume": 0.25}))
                self.assertEqual(self.manager.get("tts_volume"), 0.75)
                self.assertEqual(self.engine._current_volume(), 0.75)
                self.assertEqual(json.loads(self.manager.settings_file.read_text())["tts_volume"], 0.75)
        with patch.object(self.manager_module, "_atomic_write_json", side_effect=OSError("synthetic full disk")):
            self.assertEqual(self.post_settings(0.25).status_code, 500)
        self.assertEqual(self.manager.get("tts_volume"), 0.75)
        self.assertEqual(self.new_manager().get("tts_volume"), 0.75)

    def test_failed_user_save_reloads_durable_gain_for_every_entry(self):
        user = "volume-user-a"
        self.manager.update({"tts_volume": 0.75, "quiet_hours_enabled": False}, user_id=user)
        for entry in ("api", "set", "update", "user", "system"):
            with self.subTest(entry=entry), self.identity.user_scope(user):
                with patch.object(self.manager, "_save_user_settings_blob", return_value=False):
                    if entry == "api":
                        self.assertEqual(self.post_settings(0.25, user=user).status_code, 500)
                    elif entry == "set":
                        self.assertFalse(self.manager.set("tts_volume", 0.25))
                    elif entry == "user":
                        self.assertFalse(self.manager.set_user_setting(user, "tts_volume", 0.25))
                    elif entry == "system":
                        self.assertFalse(self.manager.set_system_value("tts_volume", 0.25, user_id=user))
                    else:
                        self.assertFalse(self.manager.update({"tts_volume": 0.25}))
                self.assertIsNone(self.manager._user_settings_cache.get(user))
                self.assertEqual(self.engine._current_volume(), 0.75)
                self.assertEqual(self.manager.get("tts_volume"), 0.75)

    def test_uncertain_user_write_reloads_committed_value_instead_of_old_snapshot(self):
        user = "volume-user-a"
        self.manager.update({"tts_volume": 0.75, "quiet_hours_enabled": False}, user_id=user)
        original = self.manager._save_user_settings_blob

        def commit_then_lose_ack(user_id, blob):
            self.assertTrue(original(user_id, blob))
            return False

        with patch.object(self.manager, "_save_user_settings_blob", side_effect=commit_then_lose_ack):
            self.assertFalse(self.manager.update({"tts_volume": 0.25}, user_id=user))
        self.assertIsNone(self.manager._user_settings_cache.get(user))
        with self.identity.user_scope(user):
            self.assertEqual(self.engine._current_volume(), 0.25)

    def test_persistence_exception_propagates_without_publishing_proposal(self):
        self.manager.update({"tts_volume": 0.75, "quiet_hours_enabled": False})
        with patch.object(self.manager, "save", side_effect=RuntimeError("synthetic persistence error")):
            with self.assertRaisesRegex(RuntimeError, "synthetic persistence error"):
                self.manager.set("tts_volume", 0.25)
        self.assertEqual(self.engine._current_volume(), 0.75)
        user = "volume-user-a"
        self.manager.update({"tts_volume": 0.75, "quiet_hours_enabled": False}, user_id=user)
        with patch.object(self.manager, "_save_user_settings_blob", side_effect=RuntimeError("synthetic DB error")):
            with self.assertRaisesRegex(RuntimeError, "synthetic DB error"):
                self.manager.update({"tts_volume": 0.25}, user_id=user)
        self.assertIsNone(self.manager._user_settings_cache.get(user))
        self.assertEqual(self.manager.get("tts_volume", user_id=user), 0.75)

    def assert_pending_write_is_private(self, user):
        self.manager.update({"tts_volume": 0.75, "quiet_hours_enabled": False}, user_id=user)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        results, errors = [], []

        def persist(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=3):
                raise AssertionError("timed out waiting for pending-save fixture")
            return False

        def update():
            try:
                results.append(self.manager.update({"tts_volume": 0.25}, user_id=user))
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        target = "_save_user_settings_blob" if user else "save"
        with patch.object(self.manager, target, side_effect=persist):
            thread = threading.Thread(target=update)
            thread.start()
            try:
                self.assertTrue(entered.wait(timeout=3))
                self.assertFalse(finished.is_set())
                self.assertEqual(self.manager.get("tts_volume", user_id=user), 0.75)
                if user:
                    self.assertTrue(
                        self.manager.update({"tts_volume": 0.4}, user_id="volume-user-b", save_immediately=False)
                    )
                    self.assertEqual(self.manager.get("tts_volume", user_id="volume-user-b"), 0.4)
                else:
                    self.assertEqual(self.manager.settings["tts_volume"], 0.75)
            finally:
                release.set()
                thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [False])
        self.assertEqual(self.manager.get("tts_volume", user_id=user), 0.75)

    def test_pending_global_write_is_invisible_to_live_readers(self):
        self.assert_pending_write_is_private(None)

    def test_pending_user_write_is_private_and_other_user_is_not_blocked(self):
        self.assert_pending_write_is_private("volume-user-a")

    def assert_failed_writer_cannot_clobber_next(self, user):
        self.manager.update({"tts_volume": 0.75, "quiet_hours_enabled": False}, user_id=user)
        entered, release = threading.Event(), threading.Event()
        results, errors = {}, []
        target = "_save_user_settings_blob" if user else "save"
        original = getattr(self.manager, target)

        def persist(*args, **kwargs):
            if threading.current_thread().name == "failed-volume-writer":
                entered.set()
                if not release.wait(timeout=3):
                    raise AssertionError("timed out waiting for writer fixture")
                return False
            return original(*args, **kwargs)

        def update(label, values):
            try:
                results[label] = self.manager.update(values, user_id=user)
            except BaseException as exc:
                errors.append(exc)

        with patch.object(self.manager, target, side_effect=persist):
            first = threading.Thread(target=update, args=("failed", {"tts_volume": 0.25}), name="failed-volume-writer")
            second = threading.Thread(target=update, args=("success", {"theme": "dark"}))
            first.start()
            try:
                self.assertTrue(entered.wait(timeout=3))
                second.start()
            finally:
                release.set()
                first.join(timeout=3)
                if second.ident is not None:
                    second.join(timeout=3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, {"failed": False, "success": True})
        self.assertEqual(self.manager.get("tts_volume", user_id=user), 0.75)
        self.assertEqual(self.manager.get("theme", user_id=user), "dark")
        if user:
            self.manager._user_settings_cache.invalidate(user)
            self.assertEqual(self.manager.get("tts_volume", user_id=user), 0.75)
            self.assertEqual(self.manager.get("theme", user_id=user), "dark")
        else:
            loaded = self.new_manager()
            self.assertEqual(loaded.get("tts_volume"), 0.75)
            self.assertEqual(loaded.get("theme"), "dark")

    def test_failed_global_writer_does_not_poison_next_successful_write(self):
        self.assert_failed_writer_cannot_clobber_next(None)

    def test_failed_user_writer_does_not_poison_next_successful_write(self):
        self.assert_failed_writer_cannot_clobber_next("volume-user-a")

    def test_explicit_deferred_writes_keep_existing_live_staging(self):
        for user in (None, "volume-user-a"):
            with self.subTest(user=user):
                self.manager.update({"tts_volume": 0.75}, user_id=user)
                self.assertTrue(self.manager.update({"tts_volume": 0.25}, user_id=user, save_immediately=False))
                self.assertEqual(self.manager.get("tts_volume", user_id=user), 0.25)
                if user:
                    stored = self.connection.execute(
                        "SELECT value FROM user_preferences WHERE user_id=? AND key='tts_volume'", (user,)
                    ).fetchone()[0]
                    self.assertEqual(json.loads(stored), 0.75)
                else:
                    self.assertEqual(json.loads(self.manager.settings_file.read_text())["tts_volume"], 0.75)

    def test_backup_only_save_with_valid_old_primary_is_rejected_for_speech(self):
        self.manager.update({"tts_volume": 0.75, "quiet_hours_enabled": False})
        original = self.manager_module._atomic_write_json

        def fail_primary(path, payload, **kwargs):
            if path == self.manager.settings_file:
                raise OSError("synthetic primary write failure")
            return original(path, payload, **kwargs)

        with patch.object(self.manager_module, "_atomic_write_json", side_effect=fail_primary):
            self.assertEqual(self.post_settings(0.25).status_code, 500)
        self.assertEqual(self.engine._current_volume(), 0.75)
        self.assertEqual(self.new_manager().get("tts_volume"), 0.75)
        backup = json.loads(self.manager.settings_file.with_suffix(".json.bak").read_text())
        self.assertEqual(backup["tts_volume"], 0.25)

    def test_quiet_hours_changes_require_reloadable_persistence(self):
        self.manager.update({"tts_volume": 0.75, "quiet_hours_enabled": False})
        original = self.manager_module._atomic_write_json

        def fail_primary(path, payload, **kwargs):
            if path == self.manager.settings_file:
                raise OSError("synthetic primary write failure")
            return original(path, payload, **kwargs)

        for key, proposed in (
            ("quiet_hours_enabled", True),
            ("quiet_hours_start", "21:00"),
            ("quiet_hours_end", "08:00"),
            ("quiet_hours_timezone", "UTC"),
        ):
            previous = self.manager.get(key)
            with (
                self.subTest(key=key),
                patch.object(self.manager_module, "_atomic_write_json", side_effect=fail_primary),
            ):
                self.assertFalse(self.manager.set(key, proposed))
            self.assertEqual(self.manager.get(key), previous)
            self.assertEqual(self.new_manager().get(key), previous)

    def test_backup_selected_by_loader_can_commit_speech_after_corrupt_primary(self):
        self.manager.update({"tts_volume": 0.75, "quiet_hours_enabled": False})
        self.manager.settings_file.write_text("{corrupt synthetic fixture")
        original = self.manager_module._atomic_write_json

        def fail_primary(path, payload, **kwargs):
            if path == self.manager.settings_file:
                raise OSError("synthetic primary write failure")
            return original(path, payload, **kwargs)

        with patch.object(self.manager_module, "_atomic_write_json", side_effect=fail_primary):
            self.assertTrue(self.manager.set("tts_volume", 0.25))
        self.assertEqual(self.engine._current_volume(), 0.25)
        self.assertEqual(self.new_manager().get("tts_volume"), 0.25)

    def test_backup_with_missing_primary_is_not_a_reloadable_speech_commit(self):
        self.manager.update({"tts_volume": 0.75, "quiet_hours_enabled": False})
        self.manager.settings_file.unlink()
        original = self.manager_module._atomic_write_json

        def fail_primary(path, payload, **kwargs):
            if path == self.manager.settings_file:
                raise OSError("synthetic missing primary write failure")
            return original(path, payload, **kwargs)

        with patch.object(self.manager_module, "_atomic_write_json", side_effect=fail_primary):
            self.assertFalse(self.manager.set("tts_volume", 0.25))
        self.assertEqual(self.engine._current_volume(), 0.75)
        self.assertTrue(self.manager.settings_file.with_suffix(".json.bak").exists())

    def test_encrypted_only_recovery_cannot_claim_changed_speech_is_saved(self):
        self.manager.update({"tts_volume": 0.75, "quiet_hours_enabled": False})
        recovered = {}
        self.manager._secure_manager = types.SimpleNamespace(
            set_secret=lambda key, value: recovered.update({key: value}),
            get_secret=lambda key: recovered.get(key),
        )
        # Exercise the supported legacy plaintext-secret recovery branch with a
        # synthetic value; ordinary credential setters still use the vault.
        self.manager.settings["llm_api_key"] = "synthetic-recovery-only"
        with patch.object(self.manager_module, "_atomic_write_json", side_effect=OSError("synthetic full disk")):
            self.assertEqual(self.post_settings(0.25).status_code, 500)
        self.assertEqual(recovered["llm_api_key"], "synthetic-recovery-only")
        self.assertEqual(self.engine._current_volume(), 0.75)
        self.assertEqual(json.loads(self.manager.settings_file.read_text())["tts_volume"], 0.75)
        self.manager._secure_manager = None
        self.assertEqual(self.new_manager().get("tts_volume"), 0.75)

    def test_secret_only_recovery_does_not_claim_mixed_nonsecret_values_saved(self):
        self.manager.update({"tts_volume": 0.75, "quiet_hours_enabled": False, "theme": "light"})
        recovered = {}
        self.manager._secure_manager = types.SimpleNamespace(
            set_secret=lambda key, value: recovered.update({key: value}),
            get_secret=lambda key: recovered.get(key),
        )
        with patch.object(self.manager_module, "_atomic_write_json", side_effect=OSError("synthetic full disk")):
            self.assertTrue(self.manager.save({"llm_api_key": "synthetic-secret-only"}))
        self.assertEqual(recovered["llm_api_key"], "synthetic-secret-only")
        # Restore the normal full snapshot before exercising a mixed save.
        self.manager._secure_manager = None
        self.manager = self.new_manager()
        self.manager._secure_manager = types.SimpleNamespace(
            set_secret=lambda key, value: recovered.update({key: value}),
            get_secret=lambda key: recovered.get(key),
        )
        # A recovered secret is a partial side effect, not proof that the
        # non-secret values from the same request will survive a restart.
        pending = {"llm_api_key": "synthetic-mixed", "theme": "dark", "tts_volume": 0.75}
        with patch.object(self.manager_module, "_atomic_write_json", side_effect=OSError("synthetic full disk")):
            self.assertFalse(self.manager.save(pending))
        self.assertEqual(recovered["llm_api_key"], "synthetic-mixed")
        self.assertEqual(self.manager.get("theme"), "light")
        self.assertEqual(json.loads(self.manager.settings_file.read_text())["theme"], "light")
        self.assertEqual(self.new_manager().get("theme"), "light")

    def fail_primary_write(self, path, payload, **kwargs):
        if path == self.manager.settings_file:
            raise OSError("synthetic primary write failure")
        return self.original_atomic_write(path, payload, **kwargs)

    def test_non_speech_api_fallback_failure_matches_active_and_restarted_values(self):
        for method in ("POST", "PATCH"):
            for recovery in ("backup", "secret"):
                with self.subTest(method=method, recovery=recovery):
                    self.manager._secure_manager = None
                    self.manager.settings.pop("llm_api_key", None)
                    self.assertTrue(self.manager.update({"theme": "light"}))
                    recovered = {}
                    self.manager._secure_manager = types.SimpleNamespace(
                        set_secret=lambda key, value: recovered.update({key: value}),
                        get_secret=lambda key: recovered.get(key),
                    )
                    # The public API refuses secret writes. Simulate an existing
                    # legacy credential recovered while an ordinary UI save fails.
                    self.manager.settings["llm_api_key"] = "synthetic-recovery-only"
                    self.original_atomic_write = self.manager_module._atomic_write_json
                    fail = self.fail_primary_write if recovery == "backup" else OSError("synthetic full disk")
                    with patch.object(self.manager_module, "_atomic_write_json", side_effect=fail):
                        response = self.post_values({"theme": "dark"}, method=method)
                    self.assertEqual(response.status_code, 500, response.text)
                    self.assertFalse(response.json()["ok"])
                    self.assertEqual(self.manager.get("theme"), "light")
                    self.assertEqual(json.loads(self.manager.settings_file.read_text())["theme"], "light")
                    self.assertEqual(self.new_manager().get("theme"), "light")
                    if recovery == "secret":
                        self.assertEqual(recovered["llm_api_key"], "synthetic-recovery-only")
                    else:
                        backup = json.loads(self.manager.settings_file.with_suffix(".json.bak").read_text())
                        self.assertEqual(backup["theme"], "dark")

    def test_all_non_speech_global_entry_points_require_reloadable_values(self):
        self.assertTrue(self.manager.update({"theme": "light"}))
        self.original_atomic_write = self.manager_module._atomic_write_json
        writes = {
            "set": lambda: self.manager.set("theme", "dark"),
            "update": lambda: self.manager.update({"theme": "dark"}),
            "pseudo_user": lambda: self.manager.set_user_setting("device-fixture", "theme", "dark"),
            "trusted": lambda: self.manager.set_system_value("theme", "dark"),
            "direct_save": lambda: self.manager.save(dict(self.manager.settings, theme="dark")),
        }
        for label, write in writes.items():
            with (
                self.subTest(entry=label),
                patch.object(self.manager_module, "_atomic_write_json", side_effect=self.fail_primary_write),
            ):
                self.assertFalse(write())
            self.assertEqual(self.manager.get("theme"), "light")
            self.assertEqual(self.new_manager().get("theme"), "light")

    def test_non_speech_corrupt_primary_backup_keeps_normal_restart(self):
        self.assertTrue(self.manager.update({"theme": "light", "show_notifications": True}))
        self.manager.settings_file.write_text("{corrupt synthetic fixture")
        self.original_atomic_write = self.manager_module._atomic_write_json
        with patch.object(self.manager_module, "_atomic_write_json", side_effect=self.fail_primary_write):
            response = self.post_values({"theme": "dark", "show_notifications": False})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["ok"])
        self.assertEqual(self.manager.get("theme"), "dark")
        loaded = self.new_manager()
        self.assertEqual(loaded.get("theme"), "dark")
        self.assertFalse(loaded.get("show_notifications"))
        self.assertEqual(loaded.get("tts_volume"), self.manager.get("tts_volume"))

    def test_non_speech_missing_primary_backup_is_not_a_saved_request(self):
        self.assertTrue(self.manager.update({"theme": "light"}))
        self.manager.settings_file.unlink()
        self.original_atomic_write = self.manager_module._atomic_write_json
        with patch.object(self.manager_module, "_atomic_write_json", side_effect=self.fail_primary_write):
            self.assertFalse(self.manager.update({"theme": "dark"}))
        self.assertEqual(self.manager.get("theme"), "light")
        self.assertTrue(self.manager.settings_file.with_suffix(".json.bak").exists())

    def test_existing_requested_value_can_succeed_via_backup_without_new_primary(self):
        self.assertTrue(self.manager.update({"theme": "light"}))
        self.original_atomic_write = self.manager_module._atomic_write_json
        with patch.object(self.manager_module, "_atomic_write_json", side_effect=self.fail_primary_write):
            self.assertTrue(self.manager.update({"theme": "light"}))
        self.assertEqual(self.manager.get("theme"), "light")
        self.assertEqual(self.new_manager().get("theme"), "light")

    def test_non_speech_boolean_nested_and_alias_updates_all_require_persistence(self):
        self.assertTrue(self.manager.update({"show_notifications": True, "agent_autonomy": "solo"}))
        self.original_atomic_write = self.manager_module._atomic_write_json
        for proposed in (
            {"show_notifications": False},
            {"delivery_address": {"city": "Synthetic fixture"}},
            {"capability_tier": "symphony"},
        ):
            previous = dict(self.manager.settings)
            with (
                self.subTest(keys=sorted(proposed)),
                patch.object(self.manager_module, "_atomic_write_json", side_effect=self.fail_primary_write),
            ):
                self.assertFalse(self.manager.update(proposed))
            self.assertEqual(dict(self.manager.settings), previous)
        loaded = self.new_manager()
        self.assertTrue(loaded.get("show_notifications"))
        self.assertEqual(loaded.get("agent_autonomy"), "solo")

    def test_direct_save_aliases_and_removed_keys_preserve_recovery_rules(self):
        self.assertTrue(self.manager.update({"agent_autonomy": "solo"}))
        self.original_atomic_write = self.manager_module._atomic_write_json
        with patch.object(self.manager_module, "_atomic_write_json", side_effect=self.fail_primary_write):
            self.assertFalse(self.manager.save({"capability_tier": "symphony"}))
        self.assertEqual(self.manager.get("agent_autonomy"), "solo")
        self.manager.settings_file.write_text("{corrupt synthetic fixture")
        with patch.object(self.manager_module, "_atomic_write_json", side_effect=self.fail_primary_write):
            self.assertTrue(self.manager.save({"capability_tier": "symphony"}))
        self.assertEqual(self.manager.get("agent_autonomy"), "symphony")

    def test_user_non_speech_save_failure_invalidates_cache_and_keeps_durable_values(self):
        user = "volume-user-a"
        self.assertTrue(self.manager.update({"theme": "light"}, user_id=user))
        for method in ("POST", "PATCH"):
            with self.subTest(method=method):
                with patch.object(self.manager, "_save_user_settings_blob", return_value=False):
                    response = self.post_values({"theme": "dark"}, user=user, method=method)
                self.assertEqual(response.status_code, 500, response.text)
                self.assertIsNone(self.manager._user_settings_cache.get(user))
                self.assertEqual(self.manager.get("theme", user_id=user), "light")
                self.assertEqual(self.new_manager().get("theme", user_id=user), "light")

    def test_user_mixed_secret_failure_does_not_claim_nonsecret_saved(self):
        user = "volume-user-a"
        self.assertTrue(self.manager.update({"theme": "light"}, user_id=user))
        recovered = {}
        with (
            patch.object(
                self.manager, "_write_user_credential", side_effect=lambda uid, key, val: recovered.update({key: val})
            ),
            patch.object(self.manager, "_save_user_settings_blob", return_value=False),
        ):
            self.assertFalse(
                self.manager.update({"theme": "dark", "llm_api_key": "synthetic-user-secret"}, user_id=user)
            )
        self.assertEqual(recovered["llm_api_key"], "synthetic-user-secret")
        self.assertIsNone(self.manager._user_settings_cache.get(user))
        self.assertEqual(self.manager.get("theme", user_id=user), "light")
        self.assertEqual(self.new_manager().get("theme", user_id=user), "light")

    def test_non_speech_normal_api_save_survives_global_and_user_restart(self):
        for user in (None, "volume-user-a"):
            with self.subTest(user=user):
                response = self.post_values({"theme": "dark", "show_notifications": False}, user=user)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["ok"])
                self.assertEqual(self.manager.get("theme", user_id=user), "dark")
                loaded = self.new_manager()
                self.assertEqual(loaded.get("theme", user_id=user), "dark")
                self.assertFalse(loaded.get("show_notifications", user_id=user))

    def test_non_speech_fallback_failure_is_private_and_next_writer_merges_durable_state(self):
        self.assertTrue(self.manager.update({"theme": "light", "show_notifications": True}))
        entered, release = threading.Event(), threading.Event()
        results, errors = {}, []
        original = self.manager_module._atomic_write_json

        def persist(path, payload, **kwargs):
            if threading.current_thread().name == "failed-theme-writer" and path == self.manager.settings_file:
                entered.set()
                if not release.wait(timeout=3):
                    raise AssertionError("timed out waiting for theme writer fixture")
                raise OSError("synthetic primary write failure")
            return original(path, payload, **kwargs)

        def write(label, values):
            try:
                results[label] = self.manager.update(values)
            except BaseException as exc:
                errors.append(exc)

        with patch.object(self.manager_module, "_atomic_write_json", side_effect=persist):
            first = threading.Thread(target=write, args=("failed", {"theme": "dark"}), name="failed-theme-writer")
            second = threading.Thread(target=write, args=("success", {"show_notifications": False}))
            first.start()
            try:
                self.assertTrue(entered.wait(timeout=3))
                self.assertEqual(self.manager.get("theme"), "light")
                second.start()
            finally:
                release.set()
                first.join(timeout=3)
                if second.ident is not None:
                    second.join(timeout=3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, {"failed": False, "success": True})
        self.assertEqual(self.manager.get("theme"), "light")
        self.assertFalse(self.manager.get("show_notifications"))
        loaded = self.new_manager()
        self.assertEqual(loaded.get("theme"), "light")
        self.assertFalse(loaded.get("show_notifications"))

    def test_user_non_speech_lost_ack_reloads_actual_commit(self):
        user = "volume-user-a"
        self.assertTrue(self.manager.update({"theme": "light"}, user_id=user))
        original = self.manager._save_user_settings_blob

        def persist(user_id, blob):
            self.assertTrue(original(user_id, blob))
            return False

        with patch.object(self.manager, "_save_user_settings_blob", side_effect=persist):
            self.assertFalse(self.manager.update({"theme": "dark"}, user_id=user))
        self.assertIsNone(self.manager._user_settings_cache.get(user))
        self.assertEqual(self.manager.get("theme", user_id=user), "dark")
        self.assertEqual(self.new_manager().get("theme", user_id=user), "dark")

    def test_mixed_global_user_request_reports_failure_without_cross_store_rollback(self):
        user = "volume-user-a"
        self.assertTrue(self.manager.update({"theme": "light"}, user_id=user))
        self.assertTrue(self.manager.set("wake_enabled", False))
        self.original_atomic_write = self.manager_module._atomic_write_json
        with patch.object(self.manager_module, "_atomic_write_json", side_effect=self.fail_primary_write):
            self.assertFalse(self.manager.update({"theme": "dark", "wake_enabled": True}, user_id=user))
        # The already-committed user store remains authoritative even though the
        # whole request failed at the global store. This is not a transaction.
        self.assertEqual(self.manager.get("theme", user_id=user), "dark")
        self.assertFalse(self.manager.get("wake_enabled"))
        loaded = self.new_manager()
        self.assertEqual(loaded.get("theme", user_id=user), "dark")
        self.assertFalse(loaded.get("wake_enabled"))

    def test_failed_global_reset_keeps_committed_active_and_restarted_values(self):
        self.assertTrue(self.manager.update({"theme": "light", "tts_volume": 0.43}))
        previous = dict(self.manager.settings)
        self.original_atomic_write = self.manager_module._atomic_write_json
        for fail in (self.fail_primary_write, OSError("synthetic full disk")):
            with (
                self.subTest(failure=str(fail)),
                patch.object(self.manager_module, "_atomic_write_json", side_effect=fail),
            ):
                self.assertFalse(self.manager.reset())
            self.assertEqual(dict(self.manager.settings), previous)
            loaded = self.new_manager()
            self.assertEqual(loaded.get("theme"), "light")
            self.assertEqual(loaded.get("tts_volume"), 0.43)

    def test_user_reset_failure_keeps_old_snapshot_until_ack_and_reloads_after_error(self):
        user = "volume-user-a"
        self.assertTrue(self.manager.update({"theme": "light", "tts_volume": 0.43}, user_id=user))
        for outcome in (False, RuntimeError("synthetic reset DB failure")):

            def persist(*args):
                self.assertEqual(self.manager.get("theme", user_id=user), "light")
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

            with (
                self.subTest(outcome=str(outcome)),
                patch.object(self.manager, "_save_user_settings_blob", side_effect=persist),
            ):
                if isinstance(outcome, Exception):
                    with self.assertRaisesRegex(RuntimeError, "synthetic reset DB failure"):
                        self.manager.reset_user_settings(user)
                else:
                    self.assertFalse(self.manager.reset_user_settings(user))
            self.assertIsNone(self.manager._user_settings_cache.get(user))
            self.assertEqual(self.manager.get("theme", user_id=user), "light")
            self.assertEqual(self.new_manager().get("tts_volume", user_id=user), 0.43)

    def test_successful_and_deferred_resets_preserve_defaults_and_user_isolation(self):
        for user in (None, "volume-user-a", "device-fixture"):
            with self.subTest(user=user):
                self.assertTrue(self.manager.update({"theme": "light", "tts_volume": 0.43}, user_id=user))
                self.assertTrue(self.manager.update({"theme": "dark"}, user_id="volume-user-b"))
                reset = (
                    self.manager.reset
                    if user is None
                    else lambda **kwargs: self.manager.reset_user_settings(user, **kwargs)
                )
                self.assertTrue(reset(save_immediately=False))
                self.assertEqual(
                    self.manager.get("tts_volume", self.manager.DEFAULT_SETTINGS["tts_volume"], user_id=user),
                    self.manager.DEFAULT_SETTINGS["tts_volume"],
                )
                self.assertEqual(
                    self.new_manager().get("tts_volume", self.manager.DEFAULT_SETTINGS["tts_volume"], user_id=user),
                    0.43,
                )
                self.assertTrue(reset())
                self.assertEqual(
                    self.new_manager().get("tts_volume", self.manager.DEFAULT_SETTINGS["tts_volume"], user_id=user),
                    self.manager.DEFAULT_SETTINGS["tts_volume"],
                )
                self.assertEqual(self.manager.get("theme", user_id="volume-user-b"), "dark")

    def write_import_fixture(self, values):
        path = self.root / "synthetic-import.json"
        path.write_text(json.dumps(values))
        return path

    def test_failed_import_returns_false_and_does_not_publish_defaults_or_values(self):
        self.assertTrue(self.manager.update({"theme": "light", "tts_volume": 0.43}))
        previous = dict(self.manager.settings)
        path = self.write_import_fixture({"theme": "dark"})
        self.original_atomic_write = self.manager_module._atomic_write_json
        for fail in (self.fail_primary_write, OSError("synthetic full disk")):
            with (
                self.subTest(failure=str(fail)),
                patch.object(self.manager_module, "_atomic_write_json", side_effect=fail),
            ):
                self.assertFalse(self.manager.import_settings(path))
            self.assertEqual(dict(self.manager.settings), previous)
            loaded = self.new_manager()
            self.assertEqual(loaded.get("theme"), "light")
            self.assertEqual(loaded.get("tts_volume"), 0.43)

    def test_import_preserves_guards_and_reports_invalid_input_without_state_changes(self):
        self.assertTrue(self.manager.update({"theme": "light", "tts_volume": 0.43}))
        previous = dict(self.manager.settings)
        for values in (
            {"require_account_for_paid_actions": False},
            ["not an object"],
            {"llm_api_key": "synthetic-secret"},
        ):
            with self.subTest(values=list(values)):
                self.assertFalse(self.manager.import_settings(self.write_import_fixture(values)))
            self.assertEqual(dict(self.manager.settings), previous)
            self.assertEqual(self.new_manager().get("tts_volume"), 0.43)

    def test_successful_import_replaces_with_defaults_and_canonical_requested_values(self):
        self.assertTrue(self.manager.update({"theme": "light", "tts_volume": 0.43, "weather_location": "old fixture"}))
        path = self.write_import_fixture({"theme": "dark", "capability_tier": "symphony"})
        self.assertTrue(self.manager.import_settings(path))
        loaded = self.new_manager()
        for manager in (self.manager, loaded):
            self.assertEqual(manager.get("theme"), "dark")
            self.assertEqual(manager.get("agent_autonomy"), "symphony")
            self.assertEqual(manager.get("weather_location"), self.manager.DEFAULT_SETTINGS["weather_location"])
            self.assertEqual(manager.get("tts_volume"), self.manager.DEFAULT_SETTINGS["tts_volume"])

    def test_empty_import_persists_default_snapshot_and_fails_honestly(self):
        path = self.write_import_fixture({})
        self.assertTrue(self.manager.update({"theme": "light", "tts_volume": 0.43}))
        with patch.object(self.manager_module, "_atomic_write_json", side_effect=OSError("synthetic full disk")):
            self.assertFalse(self.manager.import_settings(path))
        self.assertEqual(self.manager.get("tts_volume"), 0.43)
        self.assertTrue(self.manager.import_settings(path))
        self.assertEqual(self.new_manager().get("tts_volume"), self.manager.DEFAULT_SETTINGS["tts_volume"])

    def test_import_keeps_active_global_values_private_during_update(self):
        self.assertTrue(self.manager.update({"theme": "light", "tts_volume": 0.43}))
        path = self.write_import_fixture({"theme": "dark"})
        original = self.manager_module._atomic_write_json

        def persist(*args, **kwargs):
            self.assertEqual(self.manager.get("theme"), "light")
            self.assertEqual(self.manager.get("tts_volume"), 0.43)
            return original(*args, **kwargs)

        with patch.object(self.manager_module, "_atomic_write_json", side_effect=persist):
            self.assertTrue(self.manager.import_settings(path))
        self.assertEqual(self.manager.get("theme"), "dark")

    def test_user_scoped_import_failure_and_success_preserve_other_users(self):
        user = "volume-user-a"
        self.assertTrue(self.manager.update({"theme": "light"}, user_id=user))
        self.assertTrue(self.manager.update({"theme": "dark"}, user_id="volume-user-b"))
        self.assertTrue(self.manager.update({"tts_volume": 0.43}))
        path = self.write_import_fixture({"theme": "dark"})
        with self.identity.user_scope(user):
            with patch.object(self.manager, "_save_user_settings_blob", return_value=False):
                self.assertFalse(self.manager.import_settings(path))
            self.assertEqual(self.manager.get("theme"), "light")
            self.assertTrue(self.manager.import_settings(path))
        self.assertEqual(self.manager.get("theme", user_id=user), "dark")
        loaded = self.new_manager()
        self.assertEqual(loaded.get("theme", user_id=user), "dark")
        self.assertEqual(loaded.get("theme", user_id="volume-user-b"), "dark")
        self.assertEqual(loaded.get("tts_volume"), 0.43)

    def test_import_requires_omitted_default_values_to_be_saved_too(self):
        self.assertTrue(self.manager.update({"theme": "light", "tts_volume": 0.43}))
        path = self.write_import_fixture({"theme": "light"})
        self.original_atomic_write = self.manager_module._atomic_write_json
        with patch.object(self.manager_module, "_atomic_write_json", side_effect=self.fail_primary_write):
            self.assertFalse(self.manager.import_settings(path))
        self.assertEqual(self.manager.get("tts_volume"), 0.43)
        self.assertEqual(self.new_manager().get("tts_volume"), 0.43)

    def test_user_reset_lost_ack_reloads_actual_committed_empty_blob(self):
        user = "volume-user-a"
        self.assertTrue(self.manager.update({"theme": "light"}, user_id=user))
        original = self.manager._save_user_settings_blob

        def persist(user_id, blob):
            self.assertEqual(self.manager.get("theme", user_id=user), "light")
            self.assertTrue(original(user_id, blob))
            return False

        with patch.object(self.manager, "_save_user_settings_blob", side_effect=persist):
            self.assertFalse(self.manager.reset_user_settings(user))
        self.assertIsNone(self.manager._user_settings_cache.get(user))
        self.assertEqual(self.manager.get("theme", "default fixture", user_id=user), "default fixture")
        self.assertEqual(self.new_manager().get("theme", "default fixture", user_id=user), "default fixture")

    def test_global_reset_does_not_publish_before_successful_write(self):
        self.assertTrue(self.manager.update({"theme": "light", "tts_volume": 0.43}))
        original = self.manager_module._atomic_write_json

        def persist(*args, **kwargs):
            self.assertEqual(self.manager.get("theme"), "light")
            self.assertEqual(self.manager.get("tts_volume"), 0.43)
            return original(*args, **kwargs)

        with patch.object(self.manager_module, "_atomic_write_json", side_effect=persist):
            self.assertTrue(self.manager.reset())
        self.assertEqual(self.manager.get("tts_volume"), self.manager.DEFAULT_SETTINGS["tts_volume"])
        self.assertEqual(self.new_manager().get("tts_volume"), self.manager.DEFAULT_SETTINGS["tts_volume"])

    def test_reset_and_import_fallback_must_persist_removed_nonsecret_keys(self):
        self.original_atomic_write = self.manager_module._atomic_write_json
        for entry in ("reset", "import", "direct_save"):
            self.assertTrue(self.manager.reset())
            self.assertTrue(self.manager.update({"synthetic_extension_config": {"obsolete": True}}))
            path = self.write_import_fixture({})
            with (
                self.subTest(entry=entry),
                patch.object(self.manager_module, "_atomic_write_json", side_effect=self.fail_primary_write),
            ):
                if entry == "reset":
                    saved = self.manager.reset()
                elif entry == "import":
                    saved = self.manager.import_settings(path)
                else:
                    saved = self.manager.save(self.manager._default_settings_base())
                self.assertFalse(saved)
            self.assertEqual(self.manager.get("synthetic_extension_config"), {"obsolete": True})
            self.assertEqual(self.new_manager().get("synthetic_extension_config"), {"obsolete": True})

    def test_authenticated_import_resets_only_user_defaults_and_refuses_global_fields(self):
        user = "volume-user-a"
        self.assertTrue(self.manager.update({"theme": "light", "tts_volume": 0.43}, user_id=user))
        self.assertTrue(self.manager.update({"theme": "light", "tts_volume": 0.37}))
        previous = dict(self.manager.settings)
        with self.identity.user_scope(user):
            with patch.object(self.manager_module, "_atomic_write_json", side_effect=AssertionError("No global write")):
                self.assertFalse(self.manager.import_settings(self.write_import_fixture({"wake_enabled": True})))
                self.assertEqual(self.manager.get("tts_volume"), 0.43)
                self.assertTrue(self.manager.import_settings(self.write_import_fixture({"theme": "dark"})))
                self.assertEqual(self.manager.get("tts_volume"), self.manager.DEFAULT_SETTINGS["tts_volume"])
                self.assertTrue(self.manager.import_settings(self.write_import_fixture({})))
        self.assertEqual(dict(self.manager.settings), previous)
        loaded = self.new_manager()
        self.assertEqual(loaded.get("tts_volume"), 0.37)
        self.assertEqual(loaded.get("tts_volume", user_id=user), self.manager.DEFAULT_SETTINGS["tts_volume"])

    def test_user_import_pending_and_uncertain_save_keeps_durable_readback(self):
        user = "volume-user-a"
        path = self.write_import_fixture({"theme": "dark"})
        original = self.manager._save_user_settings_blob
        for commit, acknowledge in ((False, False), (True, False), (True, True)):
            self.assertTrue(self.manager.update({"theme": "light"}, user_id=user))

            def persist(user_id, blob):
                self.assertEqual(self.manager.get("theme", user_id=user), "light")
                if commit:
                    self.assertTrue(original(user_id, blob))
                return acknowledge

            with self.subTest(commit=commit, acknowledge=acknowledge), self.identity.user_scope(user):
                with patch.object(self.manager, "_save_user_settings_blob", side_effect=persist):
                    self.assertEqual(self.manager.import_settings(path), acknowledge)
                if not acknowledge:
                    self.assertIsNone(self.manager._user_settings_cache.get(user))
                expected = "dark" if commit else "light"
                self.assertEqual(self.manager.get("theme"), expected)
                self.assertEqual(self.new_manager().get("theme"), expected)

    def test_mixed_import_secret_recovery_cannot_claim_nonsecret_defaults_saved(self):
        self.assertTrue(self.manager.update({"theme": "light", "tts_volume": 0.43}))
        recovered = {}
        self.manager._secure_manager = types.SimpleNamespace(
            set_secret=lambda key, value: recovered.update({key: value}),
            get_secret=lambda key: recovered.get(key),
        )
        path = self.write_import_fixture({"theme": "dark", "llm_api_key": "synthetic-import-secret"})
        with patch.object(self.manager_module, "_atomic_write_json", side_effect=OSError("synthetic full disk")):
            self.assertFalse(self.manager.import_settings(path))
        self.assertEqual(recovered["llm_api_key"], "synthetic-import-secret")
        self.assertEqual(self.manager.get("theme"), "light")
        self.assertEqual(self.new_manager().get("tts_volume"), 0.43)

    def test_reset_and_import_corrupt_primary_backup_can_persist_full_replacements(self):
        self.original_atomic_write = self.manager_module._atomic_write_json
        for entry in ("reset", "import"):
            self.assertTrue(self.manager.update({"theme": "light", "synthetic_extension_config": {"obsolete": True}}))
            self.manager.settings_file.write_text("{corrupt synthetic fixture")
            path = self.write_import_fixture({"theme": "dark"})
            with (
                self.subTest(entry=entry),
                patch.object(self.manager_module, "_atomic_write_json", side_effect=self.fail_primary_write),
            ):
                self.assertTrue(self.manager.reset() if entry == "reset" else self.manager.import_settings(path))
            loaded = self.new_manager()
            expected = self.manager.DEFAULT_SETTINGS["theme"] if entry == "reset" else "dark"
            self.assertEqual(loaded.get("theme"), expected)
            self.assertNotIn("synthetic_extension_config", loaded.settings)

    def test_user_import_cache_expiry_during_successful_write_cannot_publish_empty_blob(self):
        user = "volume-user-a"
        self.assertTrue(self.manager.update({"theme": "light"}, user_id=user))
        path = self.write_import_fixture({"theme": "dark"})
        original = self.manager._save_user_settings_blob
        cache_class = self.manager_module._UserSettingsCache
        original_ttl = cache_class._TTL

        def persist(user_id, blob):
            saved = original(user_id, blob)
            # A slow real DB write can outlive the private staging cache TTL.
            # Expire it deterministically without delaying the test five minutes.
            cache_class._TTL = 0
            return saved

        try:
            with (
                self.identity.user_scope(user),
                patch.object(self.manager, "_save_user_settings_blob", side_effect=persist),
            ):
                self.assertTrue(self.manager.import_settings(path))
        finally:
            cache_class._TTL = original_ttl
        self.assertEqual(self.manager.get("theme", user_id=user), "dark")
        self.assertEqual(self.new_manager().get("theme", user_id=user), "dark")

    def test_copy_on_write_keeps_system_guards_and_cloud_user_boundary(self):
        key = "require_account_for_paid_actions"
        self.assertTrue(self.manager.set_system_value(key, True))
        self.assertFalse(self.manager.set(key, False))
        self.assertFalse(self.manager.update({key: False}))
        self.assertFalse(self.manager.set_user_setting("volume-user-a", key, False))
        self.assertTrue(self.manager.get(key))
        self.assertTrue(self.new_manager().get(key))
        with patch.object(self.manager_module, "_is_cloud_deployment", return_value=True):
            with self.assertRaises(ValueError):
                self.manager.set("tts_volume", 0.25)
            with self.assertRaises(ValueError):
                self.manager.update({"tts_volume": 0.25})

    def test_fallback_failure_still_signals_completion_and_records_failure(self):
        worker = self.fallback.TTSWorker(self.config)
        worker._engine = Mock()
        worker._engine.say.side_effect = RuntimeError("synthetic failure")
        request = self.fallback.TTSRequest("fixture", threading.Event())
        worker._process_request(request)
        self.assertTrue(request.completion_event.is_set())
        self.assertFalse(request.success)
        self.assertEqual(worker._engine_failure_count, 1)

    def test_unavailable_and_failed_model_keep_empty_result(self):
        self.engine._kokoro = None
        self.assertEqual(asyncio.run(self.engine.synthesize("fixture")), b"")
        self.engine._kokoro = types.SimpleNamespace(create=Mock(side_effect=RuntimeError("synthetic model failure")))
        self.assertEqual(asyncio.run(self.engine.synthesize("fixture")), b"")


if __name__ == "__main__":
    unittest.main()
