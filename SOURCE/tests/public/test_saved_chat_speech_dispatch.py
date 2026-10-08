"""Saved-chat completion reaches the existing typed-reply speech policy.

Execute the actual generation and speech functions; provider, storage and audio
boundaries are inert. This verifies dispatch, not native synthesis or audibility.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import sys
import time
import types
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]


def source_functions(path, names, namespace, constants=()):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    selected = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert {n.name for n in selected} == set(names)
    selected += [
        n
        for n in tree.body
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in n.targets)
    ]
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(ROOT / path), "exec"), namespace)
    return namespace


class SavedChatSpeechDispatch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.audio = []
        self.tasks = []
        self.preference_reads = []
        self.preferences = {"speak_all_replies": True, "tts_enabled": True, "voice_muted": False}
        self.desktop = True
        self.service_error = None
        self.store_error = None
        self.block_generation = None
        self.entered_generation = asyncio.Event()
        self.envelope = {"ok": True, "data": {"message": "Surface QA speech check"}}
        self.log = Mock()

        class Tracker:
            def create_task(inner, coroutine):
                task = asyncio.create_task(coroutine)
                self.tasks.append(task)
                return task

        async def speak(text):
            self.events.append("speech")
            self.audio.append(text)

        self.engine = types.SimpleNamespace(speak=speak)
        self.context = types.SimpleNamespace(
            bindings=types.SimpleNamespace(intent=types.SimpleNamespace(tts=self.engine))
        )

        def get_preference(key, default, *, user_id):
            self.preference_reads.append((key, user_id))
            return self.preferences.get(key, default)

        defaults = types.ModuleType("config.defaults")
        for n in ast.parse((ROOT / "config/defaults.py").read_text(encoding="utf-8")).body:
            if isinstance(n, ast.Assign):
                for target in n.targets:
                    if isinstance(target, ast.Name) and target.id in {
                        "SPEAK_ALL_REPLIES_DEFAULT",
                        "TTS_ENABLED_DEFAULT",
                        "VOICE_MUTED_DEFAULT",
                    }:
                        setattr(defaults, target.id, ast.literal_eval(n.value))
        settings = types.ModuleType("ui.settings_manager")
        settings.get_settings_manager = lambda: types.SimpleNamespace(get=get_preference)
        instrumentation = types.ModuleType("admin.instrumentation")
        instrumentation.record_feature_used = lambda *args: None
        command_types = types.ModuleType("services.command")
        command_types.CommandRequest = lambda **values: types.SimpleNamespace(**values)
        conversation = types.ModuleType("services.conversation.state_manager")
        conversation.ConversationStateManager = lambda **kw: types.SimpleNamespace(
            record_exchange=lambda *args, **kwargs: None
        )
        stream = types.ModuleType("services.llm.stream_bus")
        stream.command_stream_context = lambda *args, **kwargs: contextlib.nullcontext()
        stream.finalize_stream = lambda *args, **kwargs: self.events.append(
            "finalized-error" if kwargs.get("error") else "finalized"
        )
        stream.get_stream_token_count = lambda *args: 0
        stream.get_stream_tool_events = lambda *args: []
        self.speech_module = types.ModuleType("ui.api.routes.command")
        ns = vars(self.speech_module)
        ns.update(
            asyncio=asyncio,
            log=self.log,
            is_desktop_surface=lambda: self.desktop,
            _reply_speech_tasks=Tracker(),
            Mapping=Mapping,
        )
        source_functions("services/command/command_service_core.py", {"channel_key"}, ns)
        source_functions(
            "ui/api/routes/command.py",
            {"_command_stream_message", "_reply_tts_engine", "_should_speak_reply", "_speak_reply_now", "_speak_reply"},
            ns,
            {"_LOCALLY_SPOKEN_CHANNELS", "_SPEAK_REPLY_TIMEOUT_SECONDS"},
        )
        self.enterContext(
            patch.dict(
                sys.modules,
                {
                    "config.defaults": defaults,
                    "ui.settings_manager": settings,
                    "admin.instrumentation": instrumentation,
                    "services.command": command_types,
                    "services.conversation.state_manager": conversation,
                    "services.llm.stream_bus": stream,
                    "ui.api.routes.command": self.speech_module,
                },
            )
        )

        async def execute(request):
            self.request = request
            self.entered_generation.set()
            if self.block_generation is not None:
                await self.block_generation.wait()
            if self.service_error:
                raise self.service_error
            return types.SimpleNamespace(
                to_envelope=lambda: self.envelope, error=None, intent="answer", ok=True, policy_flags=[]
            )

        async def persist(user_id, thread_id, *args, **kwargs):
            if self.store_error:
                raise self.store_error
            self.persisted = (user_id, thread_id, args, kwargs)
            self.events.append("stored")
            return types.SimpleNamespace(id="assistant-fixture")

        self.store = types.SimpleNamespace(append_message=persist, update_message=persist)
        scope = dict(
            asyncio=asyncio,
            contextlib=contextlib,
            time=time,
            logger=self.log,
            _get_command_service=lambda context: types.SimpleNamespace(execute=execute),
            _history_from_messages=lambda messages: [],
            _sanitize_chat_tool_events=lambda events: events,
            _ACTIVE_CHAT_TASKS={},
            _command_stream_message=ns["_command_stream_message"],
        )
        source_functions("ui/api/routes/chat_mode.py", {"_run_chat_command"}, scope)
        self.run_generation = scope["_run_chat_command"]
        self.args = dict(
            context=self.context,
            store=self.store,
            user_id="qa-user-a",
            thread_id="thread-a",
            stream_id="stream-a",
            text="Please speak the check",
            model=None,
            history_messages=[],
        )

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def complete(self, **overrides):
        await self.run_generation(**{**self.args, **overrides})
        if self.tasks:
            await asyncio.gather(*self.tasks)

    async def test_completed_saved_chat_speaks_once_after_storage_and_stream_completion(self):
        await self.complete()
        self.assertEqual(self.audio, ["Surface QA speech check"])
        self.assertEqual(self.events, ["stored", "finalized", "speech"])
        self.assertEqual(self.request.channel["type"], "chat_mode")
        self.assertEqual(self.request.origin, "typed")

    async def test_policy_reads_the_same_explicit_signed_in_user(self):
        await self.complete(user_id="qa-user-b")
        self.assertEqual(self.audio, ["Surface QA speech check"])
        self.assertEqual({uid for _, uid in self.preference_reads}, {"qa-user-b"})
        self.assertEqual(self.persisted[0], "qa-user-b")

    async def test_each_existing_user_opt_out_prevents_speech(self):
        for key, value in (("speak_all_replies", False), ("tts_enabled", False), ("voice_muted", True)):
            with self.subTest(key=key), patch.dict(self.preferences, {key: value}):
                await self.complete()
                self.assertEqual(self.audio, [])
                self.assertEqual(self.tasks, [])

    async def test_cloud_completion_does_not_touch_local_engine(self):
        class ForbiddenBindings:
            @property
            def intent(self):
                raise AssertionError("cloud attempted to access local speech")

        self.desktop = False
        self.context.bindings = ForbiddenBindings()
        await self.complete()
        self.assertEqual(self.events, ["stored", "finalized"])
        self.assertEqual(self.tasks, [])

    async def test_remote_and_voice_channels_stay_outside_the_local_typed_policy(self):
        for channel in ("phone", "sms", "telegram", "email", "voice", "voice-stream", "unknown"):
            with self.subTest(channel=channel):
                self.assertFalse(self.speech_module._should_speak_reply(channel, "qa-user-a"))
        for channel in ("http", "web", "chat_mode"):
            with self.subTest(channel=channel):
                self.assertTrue(self.speech_module._should_speak_reply(channel, "qa-user-a"))

    async def test_upstream_spoken_marker_prevents_duplicate_speech(self):
        self.envelope["data"]["spoken"] = True
        await self.complete()
        self.assertEqual(self.audio, [])
        self.assertEqual(self.tasks, [])

    async def test_cancellation_before_completion_never_schedules_speech(self):
        self.block_generation = asyncio.Event()
        task = asyncio.create_task(self.run_generation(**self.args))
        await self.entered_generation.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.tasks, [])
        self.assertEqual(self.audio, [])
        self.assertEqual(self.events, ["stored", "finalized-error"])

    async def test_generation_failure_never_schedules_speech(self):
        self.service_error = RuntimeError("synthetic provider failure")
        await self.complete()
        self.assertEqual(self.tasks, [])
        self.assertEqual(self.events, ["stored", "finalized-error"])

    async def test_failed_message_persistence_never_schedules_speech(self):
        self.store_error = OSError("synthetic store failure")
        await self.complete()
        self.assertEqual(self.tasks, [])
        self.assertEqual(self.events, ["finalized-error"])

    async def test_audio_failure_does_not_rewrite_a_completed_text_response(self):
        async def fail(text):
            raise RuntimeError("synthetic audio failure")

        self.engine.speak = fail
        await self.complete()
        self.assertEqual(len(self.tasks), 1)
        self.assertEqual(self.events, ["stored", "finalized"])
        self.assertEqual(self.persisted[3]["content"], "Surface QA speech check")
        self.log.warning.assert_called()

    async def test_deferred_sync_wrapper_returning_a_coroutine_is_awaited(self):
        async def actual(text):
            self.audio.append(text)

        self.engine.speak = lambda text: actual(text)
        await self.complete()
        self.assertEqual(self.audio, ["Surface QA speech check"])

    async def test_regeneration_uses_the_same_single_completion_hook(self):
        await self.complete(target_assistant_message_id="old-assistant")
        self.assertEqual(self.audio, ["Surface QA speech check"])
        self.assertEqual(self.persisted[2], ("old-assistant",))
        self.assertEqual(self.events, ["stored", "finalized", "speech"])

    async def test_absent_engine_does_not_break_completed_chat(self):
        self.context.bindings.intent.tts = None
        await self.complete()
        self.assertEqual(self.events, ["stored", "finalized"])
        self.assertEqual(self.tasks, [])


if __name__ == "__main__":
    unittest.main()
