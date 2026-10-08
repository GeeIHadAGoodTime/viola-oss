"""Real voice-stream connection/command lifecycle with inert account/audio edges.

The complete route module and PCM session run unchanged. Synthetic ASR, socket,
bootstrap and dispatcher boundaries prevent microphone, credentials or network
access. This is source cancellation evidence, not installed voice acceptance.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import importlib.util
import json
import logging
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[2]


def module(name, **attrs):
    result = types.ModuleType(name)
    result.__dict__.update(attrs)
    return result


class VoiceStreamCancellation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Load native-backed libraries before the module-isolation snapshot.
        # Restoring patched imports must not force NumPy to initialize twice.
        for dependency in ("fastapi", "numpy"):
            __import__(dependency)
        modules = {
            "backend.launch_kill_switches": module("backend.launch_kill_switches", get_store=Mock()),
            "core.constants": module("core.constants", AUDIO_INT16_SCALE=32768, SAMPLE_RATE_16K=16000),
            "core.logging_config": module("core.logging_config", get_logger=logging.getLogger),
            "core.user_context": module(
                "core.user_context", set_current_user_id=Mock(return_value=None), reset_current_user_id=Mock()
            ),
            "services.llm.managed_budget": module("services.llm.managed_budget", cap_state_from_response=lambda _: {}),
            "ui.api.routes.transcription": module("ui.api.routes.transcription", TRANSCRIBE_FILE_FIELD="audio"),
            "ui.api.routes.websocket_auth": module(
                "ui.api.routes.websocket_auth", get_websocket_spoke_credential=Mock(), websocket_is_authorized=Mock()
            ),
            "ui.core.security": module("ui.core.security", check_websocket_origin=Mock(), reject_websocket=Mock()),
            "voice.synthesis.tts_wire": module(
                "voice.synthesis.tts_wire", TTS_PREFIX=b"TTS\0", encode_tts_frame=Mock()
            ),
            "voice.wake_detector.wake.context": module(
                "voice.wake_detector.wake.context", WakeContext=object, WakeDecisionResult=object
            ),
            "voice.wake_detector.wake_decision_policy": module(
                "voice.wake_detector.wake_decision_policy", WakeDecisionPolicy=Mock, get_wake_policy=Mock()
            ),
            "voice.wake_detector.spoke_wake_diag": module(
                "voice.wake_detector.spoke_wake_diag", DIAG_ENABLED=False, SpokeDiagnostics=Mock()
            ),
            "voice.synthesis.spoke_tts_broadcast": module(
                "voice.synthesis.spoke_tts_broadcast", suppress_spoke_tts_broadcast=nullcontext
            ),
            "config.settings": module(
                "config.settings",
                settings=types.SimpleNamespace(base_url="http://synthetic.invalid", ssl_enabled=False),
            ),
            "ui.security.bootstrap": module("ui.security.bootstrap", load_bootstrap_api_key=lambda: None),
            "httpx": module("httpx", AsyncClient=Mock(side_effect=AssertionError("No network in source control"))),
        }
        patches = patch.dict(sys.modules, modules)
        patches.start()
        self.addCleanup(patches.stop)
        spec = importlib.util.spec_from_file_location(
            "_voice_stream_cancellation", ROOT / "ui/api/routes/voice_stream.py"
        )
        self.route = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.route)
        self.route._get_engine = AsyncMock(return_value=None)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.resist_cancel = False
        self.dispatcher = types.SimpleNamespace(dispatch=AsyncMock(return_value={"message": "synthetic answer"}))
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(
                asr=types.SimpleNamespace(transcribe=self.transcribe), cloud_intent_dispatcher=self.dispatcher
            )
        )
        self.queue = asyncio.Queue()
        self.ws = types.SimpleNamespace(
            accept=AsyncMock(), receive=self.receive, send_text=AsyncMock(), send_bytes=AsyncMock(), close=AsyncMock()
        )
        self.connection = None

    async def transcribe(self, path):
        self.assertTrue(Path(path).is_file())
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            if not self.resist_cancel:
                raise
        return "synthetic request"

    async def receive(self):
        message = await self.queue.get()
        if isinstance(message, Exception):
            raise message
        return message

    def send(self, kind):
        self.queue.put_nowait({"text": json.dumps({"type": kind})})

    async def begin_transcription(self, *, tap=False):
        self.connection = asyncio.create_task(
            self.route._run_voice_stream_connection(
                self.ws, app=self.app, room="synthetic-room", user_id="synthetic-user", session_token=None
            )
        )
        self.send("ptt_start")
        for _ in range(18 if tap else 1):
            self.queue.put_nowait({"bytes": bytes(4000)})
        if not tap:
            self.send("ptt_stop")
        await asyncio.wait_for(self.started.wait(), timeout=1)

    async def disconnect(self, *, exception=False):
        event = self.route.WebSocketDisconnect() if exception else {"type": "websocket.disconnect"}
        self.queue.put_nowait(event)
        await asyncio.wait_for(self.connection, timeout=1)
        self.release.set()
        for _ in range(10):
            await asyncio.sleep(0)

    async def asyncTearDown(self):
        self.release.set()
        if self.connection is not None and not self.connection.done():
            self.queue.put_nowait({"type": "websocket.disconnect"})
            await asyncio.wait_for(self.connection, timeout=1)
        # Any deliberately retained negative-control tasks finish before mocks restore.
        for _ in range(10):
            await asyncio.sleep(0)

    async def test_ptt_disconnect_cancels_pending_transcription_before_dispatch(self):
        await self.begin_transcription()
        await self.disconnect()
        self.assertTrue(self.cancelled.is_set())
        self.dispatcher.dispatch.assert_not_awaited()

    async def test_tap_endpoint_disconnect_cancels_pending_transcription(self):
        await self.begin_transcription(tap=True)
        await self.disconnect()
        self.assertTrue(self.cancelled.is_set())
        self.dispatcher.dispatch.assert_not_awaited()

    async def test_disconnect_exception_cancels_pending_transcription(self):
        await self.begin_transcription()
        await self.disconnect(exception=True)
        self.assertTrue(self.cancelled.is_set())
        self.dispatcher.dispatch.assert_not_awaited()

    async def test_cancel_resistant_transcription_cannot_dispatch_after_disconnect(self):
        self.resist_cancel = True
        await self.begin_transcription()
        await self.disconnect()
        self.dispatcher.dispatch.assert_not_awaited()
        frames = [json.loads(call.args[0])["type"] for call in self.ws.send_text.await_args_list]
        self.assertNotIn("command_result", frames)

    async def test_connection_task_cancellation_cancels_its_pending_command(self):
        await self.begin_transcription()
        self.connection.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.connection
        self.release.set()
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertTrue(self.cancelled.is_set())
        self.dispatcher.dispatch.assert_not_awaited()

    async def test_live_connection_completes_a_turn_and_remains_available(self):
        await self.begin_transcription()
        self.release.set()
        for _ in range(10):
            await asyncio.sleep(0)
        self.dispatcher.dispatch.assert_awaited_once_with(
            "synthetic request",
            user_id="synthetic-user",
            device_id="browser",
            origin_channel="voice",
            companion_online=False,
        )
        self.send("ping")
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertFalse(self.connection.done())
        frames = [json.loads(call.args[0])["type"] for call in self.ws.send_text.await_args_list]
        self.assertIn("command_result", frames)
        self.assertIn("pong", frames)


if __name__ == "__main__":
    unittest.main()
