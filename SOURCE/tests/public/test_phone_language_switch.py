"""Language control against the real pinned Pipecat services, without a model."""

from __future__ import annotations

import ast
import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]


class PhoneLanguageSwitch(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # Kokoro is optional. Replace only its model/native boundary, never the
        # service, frames, settings, or language resolver being qualified here.
        model = types.ModuleType("kokoro_onnx")
        model.Kokoro = lambda *args: None
        transformers = types.ModuleType("transformers")

        class ForbiddenModel:
            def __init__(self, *args, **kwargs):
                raise AssertionError("Language control must not construct a turn model")

        transformers.WhisperFeatureExtractor = ForbiddenModel
        # Earlier public tests may cache the installed Pipecat wheel. Isolate
        # only these runtime/model families and adapters; restore their exact
        # objects and parent attributes without clearing unrelated imports.
        prefixes = ("pipecat", "kokoro_onnx", "onnxruntime", "transformers")
        adapters = {
            "telephony.language_handler",
            "telephony.tts_normalizer",
            "telephony.transcription_observer",
            "telephony.continuous_stream_resampler",
        }

        def scoped(name):
            return name in adapters or any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)

        saved_modules = {name: module for name, module in sys.modules.items() if scoped(name)}
        missing = object()
        parent = sys.modules.get("telephony")
        saved_attributes = {
            name.rsplit(".", 1)[1]: getattr(parent, name.rsplit(".", 1)[1], missing) for name in adapters
        }

        def restore_modules():
            for name in tuple(sys.modules):
                if scoped(name):
                    sys.modules.pop(name)
            sys.modules.update(saved_modules)
            parent = sys.modules.get("telephony")
            if parent is not None:
                for name, value in saved_attributes.items():
                    if value is missing:
                        parent.__dict__.pop(name, None)
                    else:
                        setattr(parent, name, value)

        cls.addClassCleanup(restore_modules)
        for name in saved_modules:
            sys.modules.pop(name)
        sys.modules.update(
            {"kokoro_onnx": model, "onnxruntime": types.ModuleType("onnxruntime"), "transformers": transformers}
        )
        paths = patch.object(sys, "path", [str(ROOT / "third_party/pipecat/src"), str(ROOT), *sys.path])
        paths.start()
        cls.addClassCleanup(paths.stop)
        cls.kokoro = importlib.import_module("pipecat.services.kokoro.tts")
        model_constructor = patch.object(cls.kokoro, "Kokoro", model.Kokoro)
        model_constructor.start()
        cls.addClassCleanup(model_constructor.stop)
        cls.handler_module = importlib.import_module("telephony.language_handler")
        cls.language = importlib.import_module("pipecat.transcriptions.language").Language
        cls.frames = importlib.import_module("pipecat.frames.frames")
        cls.settings = importlib.import_module("pipecat.services.settings")
        base_tts_module = importlib.import_module("pipecat.services.tts_service")
        ai_module = importlib.import_module("pipecat.services.ai_service")
        cls.base_tts = base_tts_module.TTSService
        cls.filter_class = importlib.import_module("telephony.tts_normalizer").SpeechTextFilter
        cls.context_class = importlib.import_module("pipecat.processors.aggregators.llm_context").LLMContext
        cls.aggregators = importlib.import_module("pipecat.processors.aggregators.llm_response_universal")
        cls.strategies = importlib.import_module("pipecat.turns.user_turn_strategies")
        cls.processors = importlib.import_module("pipecat.processors.frame_processor")
        for module in (cls.kokoro, cls.handler_module, base_tts_module, ai_module, cls.frames, cls.settings):
            assert Path(module.__file__).resolve().is_relative_to(ROOT), (module.__name__, module.__file__)

    def setUp(self):
        self.llm = types.SimpleNamespace(push_frame=AsyncMock())
        with patch.object(self.kokoro, "_ensure_model_files") as files:
            self.tts = self.kokoro.KokoroTTSService(settings=self.kokoro.KokoroTTSService.Settings(voice="af_heart"))
            files.assert_called_once()
        self.handler = self.handler_module.LanguageHandler(self.llm, self.tts)
        self.context = self.context_class([])
        self.target = self.aggregators.LLMUserAggregator(
            self.context,
            params=self.aggregators.LLMUserAggregatorParams(
                user_turn_strategies=self.strategies.ExternalUserTurnStrategies(), user_idle_timeout=0
            ),
        )
        self.publish = AsyncMock(wraps=self.target.process_frame)
        self.target.process_frame = self.publish
        self.handler.set_context_frame_target(self.target)
        self.speech_filter = self.filter_class()
        self.speech_filter.bind_tts(self.tts)
        self.assertFalse(hasattr(self.tts, "set_language"))
        self.assertIsInstance(self.tts, self.base_tts)

    def assert_unchanged(self):
        self.assertEqual(self.tts._settings.language, "en-us")
        self.assertEqual(self.tts._settings.voice, "af_heart")
        self.assertEqual(self.handler.current_language, "en")
        self.assertFalse(self.handler.has_switched)
        self.publish.assert_not_awaited()

    async def test_all_eight_shipped_locales_reach_real_kokoro_stream(self):
        expected = {"en": "en-us", "es": "es", "fr": "fr", "it": "it", "pt": "pt", "ja": "ja", "zh": "zh", "hi": "hi"}
        self.assertEqual(self.handler_module.KOKORO_SUPPORTED, set(expected))
        for code, locale in expected.items():
            with self.subTest(code=code):
                self.setUp()
                observed = []

                async def create_stream(text, **kwargs):
                    observed.append((text, kwargs))
                    if False:
                        yield  # No model execution or audio-quality claim.

                self.tts._kokoro = types.SimpleNamespace(create_stream=create_stream)
                await self.handler._switch_language(code)
                self.assertEqual(self.tts._settings.language, locale)
                self.assertEqual(self.speech_filter._active_language(), locale)
                self.assertEqual(self.handler.current_language, code)
                self.assertTrue(self.handler.has_switched)
                self.assertEqual(self.tts._settings.voice, "af_heart")
                self.publish.assert_awaited_once()
                frame = self.publish.await_args.args[0]
                self.assertIsInstance(frame, self.frames.LLMMessagesAppendFrame)
                self.assertFalse(frame.run_llm)
                self.llm.push_frame.assert_not_awaited()
                self.assertIn("tts_language_active: " + code, frame.messages[0]["content"])
                self.assertEqual([frame async for frame in self.tts.run_tts("fixture", "context")], [])
                self.assertEqual(observed, [("fixture", {"voice": "af_heart", "lang": locale, "speed": 1.0})])

    async def test_transcription_threshold_normalization_and_sticky_success(self):
        await self.handler.on_transcription_with_language("hola", "Language.ES", 0.95)
        self.assert_unchanged()
        await self.handler.on_transcription_with_language("hola", "es-MX", 0.95)
        self.assertEqual(self.tts._settings.language, "es")
        self.assertEqual(self.handler.current_language, "es")
        await self.handler.on_transcription_with_language("bonjour", "fr", 0.99)
        await self.handler.on_transcription_with_language("bonjour", "fr", 0.99)
        self.assertEqual(self.tts._settings.language, "es")
        self.publish.assert_awaited_once()

    async def test_english_and_low_confidence_reset_detection_threshold(self):
        for language, confidence in [("en-US", 0.99), ("es", 0.69)]:
            with self.subTest(language=language):
                self.setUp()
                await self.handler.on_transcription_with_language("hola", "es", 0.95)
                await self.handler.on_transcription_with_language("fixture", language, confidence)
                await self.handler.on_transcription_with_language("hola", "es", 0.95)
                self.assert_unchanged()

    async def test_unsupported_and_invalid_codes_never_update_tts_or_claim_active(self):
        for code in ("de", "ko", "xx", "", "not-a-language"):
            with self.subTest(code=code):
                self.setUp()
                with patch.object(self.tts, "process_frame", wraps=self.tts.process_frame) as process:
                    await self.handler._switch_language(code)
                    process.assert_not_awaited()
                self.assertEqual(self.tts._settings.language, "en-us")
                self.assertEqual(self.handler.current_language, "en")
                self.assertTrue(self.handler.has_switched)  # Existing graceful-exit decision.
                context = self.publish.await_args.args[0].messages[0]["content"]
                self.assertIn("tts_language_supported: false", context)
                self.assertNotIn("tts_language_active:", context)

    async def test_missing_and_non_kokoro_adapters_do_not_claim_success(self):
        for tts in (None, object(), types.SimpleNamespace(_settings=self.settings.TTSSettings(language="en-us"))):
            with self.subTest(tts=type(tts).__name__):
                self.handler = self.handler_module.LanguageHandler(self.llm, tts)
                self.handler.set_context_frame_target(self.target)
                await self.handler._switch_language("es")
                self.assert_unchanged()

    async def test_failed_or_ignored_frame_retains_state_and_allows_retry(self):
        async def late_failure(*args):
            self.tts._settings.language = "es"
            raise RuntimeError("synthetic failure after settings mutation")

        for effect in (RuntimeError("synthetic update failure"), late_failure, None):
            with self.subTest(effect=str(effect)):
                self.setUp()
                with patch.object(self.tts, "process_frame", new=AsyncMock(side_effect=effect)):
                    await self.handler._switch_language("es")
                self.assert_unchanged()
                await self.handler._switch_language("es")
                self.assertTrue(self.handler.has_switched)
                self.assertEqual(self.tts._settings.language, "es")

    async def test_context_failure_rolls_back_exact_locale_and_preserves_voice(self):
        self.tts._settings.language = "en-gb"
        self.tts._settings.voice = "bf_emma"
        self.publish.side_effect = RuntimeError("synthetic context failure")
        await self.handler._switch_language("ja")
        self.assertEqual(self.tts._settings.language, "en-gb")
        self.assertEqual(self.tts._settings.voice, "bf_emma")
        self.assertEqual(self.handler.current_language, "en")
        self.assertFalse(self.handler.has_switched)
        self.publish.side_effect = None
        await self.handler._switch_language("ja")
        self.assertEqual(self.tts._settings.language, "ja")
        self.assertTrue(self.handler.has_switched)

    async def test_active_context_waits_for_settings_acknowledgment(self):
        async def observe(frame, direction):
            self.assertEqual(self.tts._settings.language, "zh")
            self.assertFalse(self.handler.has_switched)
            self.assertEqual(self.handler.current_language, "en")
            await self.aggregators.LLMUserAggregator.process_frame(self.target, frame, direction)
            self.assertIs(self.context.messages[-1], frame.messages[0])
            self.assertFalse(self.handler.has_switched)

        self.publish.side_effect = observe
        await self.handler._switch_language("zh")
        self.assertTrue(self.handler.has_switched)

    async def test_cancellation_restores_language_without_consuming_the_switch(self):
        self.publish.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.handler._switch_language("zh")
        self.assertEqual(self.tts._settings.language, "en-us")
        self.assertFalse(self.handler.has_switched)
        self.assertEqual(self.handler.current_language, "en")

    async def test_real_call_and_loopback_factory_services_accept_the_update(self):
        # Execute the two exact factory functions without importing the carrier
        # manager or constructing an LLM. Their service classes remain real.
        factories = [
            ("telephony/call_manager.py", "_create_shared_phone_kokoro_tts_service"),
            ("telephony/loopback_phone_call_session.py", "_create_kokoro_tts"),
        ]
        for filename, name in factories:
            with self.subTest(factory=name):
                tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
                node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
                module = ast.Module(
                    body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node],
                    type_ignores=[],
                )
                namespace = {"maybe_remote_first_kokoro": lambda runtime: runtime, "LOOPBACK_SAMPLE_RATE": 16000}
                exec(compile(ast.fix_missing_locations(module), str(ROOT / filename), "exec"), namespace)
                with patch.object(self.kokoro, "_ensure_model_files"):
                    service = (
                        namespace[name](kokoro=object(), voice_id="af_heart")
                        if "shared" in name
                        else namespace[name](voice_id="af_heart", normalize_text=True)
                    )
                self.assertIsInstance(service, self.kokoro.KokoroTTSService)
                self.handler = self.handler_module.LanguageHandler(self.llm, service)
                self.handler.set_context_frame_target(self.target)
                await self.handler._switch_language("hi")
                self.assertEqual(service._settings.language, "hi")
                self.assertTrue(self.handler.has_switched)

    async def test_missing_or_noop_context_target_cannot_commit_a_switch(self):
        for target in (None, types.SimpleNamespace(messages=[], process_frame=AsyncMock())):
            with self.subTest(target=type(target).__name__):
                self.handler.set_context_frame_target(target)
                await self.handler._switch_language("es")
                self.assert_unchanged()
                self.llm.push_frame.assert_not_awaited()

    async def test_failure_after_context_append_retains_the_committed_switch(self):
        for failure in (RuntimeError("late failure"), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__):
                self.setUp()

                async def append_then_fail(frame, direction):
                    await self.aggregators.LLMUserAggregator.process_frame(self.target, frame, direction)
                    raise failure

                self.publish.side_effect = append_then_fail
                if isinstance(failure, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await self.handler._switch_language("ja")
                else:
                    await self.handler._switch_language("ja")
                self.assertEqual(self.tts._settings.language, "ja")
                self.assertEqual(self.handler.current_language, "ja")
                self.assertTrue(self.handler.has_switched)
                self.assertEqual(len(self.context.messages), 1)
                self.assertIn("tts_language_active: ja", self.context.messages[0]["content"])
                await self.handler.on_transcription_with_language("fixture", "ja", 0.99)
                self.assertEqual(len(self.context.messages), 1)

    def test_both_phone_pipeline_builders_bind_the_direct_context_target(self):
        for filename in ("telephony/call_manager.py", "telephony/loopback_phone_call_session.py"):
            with self.subTest(filename=filename):
                tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
                bindings = [
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "language_handler"
                    and node.func.attr == "set_context_frame_target"
                ]
                self.assertEqual(len(bindings), 1)
                binding = bindings[0]
                self.assertEqual([ast.unparse(arg) for arg in binding.args], ["user_aggregator"])
                owner = next(
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.AsyncFunctionDef) and binding in list(ast.walk(node))
                )
                pipeline = next(
                    node
                    for node in ast.walk(owner)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Pipeline"
                )
                self.assertLess(binding.lineno, pipeline.lineno)

    async def test_queued_barge_in_keeps_context_and_next_speech_in_the_same_language(self):
        from pipecat.clocks.system_clock import SystemClock
        from pipecat.pipeline.task_observer import TaskObserver
        from pipecat.utils.asyncio.task_manager import TaskManager, TaskManagerParams
        from telephony.transcription_observer import TranscriptionObserver

        manager = TaskManager()
        manager.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
        task_observer = TaskObserver(observers=[], task_manager=manager)
        await task_observer.start()
        setup = self.processors.FrameProcessorSetup(clock=SystemClock(), task_manager=manager, observer=task_observer)
        entered = asyncio.Event()
        calls = []

        async def stream(text, **kwargs):
            calls.append({"text": text, **kwargs})
            if len(calls) == 1:
                entered.set()
                await asyncio.Event().wait()
            if False:
                yield

        self.tts._kokoro = types.SimpleNamespace(create_stream=stream)
        assistant = self.aggregators.LLMAssistantAggregator(self.context)
        llm = self.processors.FrameProcessor()
        llm.link(self.tts)
        self.tts.link(assistant)
        self.target.process_frame = self.aggregators.LLMUserAggregator.process_frame.__get__(self.target)
        handler = self.handler_module.LanguageHandler(llm, self.tts)
        handler.set_context_frame_target(self.target)
        observer = TranscriptionObserver(language_handler=handler)
        processors = [llm, self.tts, assistant, self.target, observer]
        direction = self.processors.FrameDirection.DOWNSTREAM

        async def queue_and_wait(processor, frame):
            done = asyncio.Event()

            async def after(*args):
                done.set()

            await processor.queue_frame(frame, callback=after)
            await asyncio.wait_for(done.wait(), 2)

        try:
            for processor in processors:
                await processor.setup(setup)
            ready = asyncio.Event()

            async def on_start(processor, frame):
                if isinstance(frame, self.frames.StartFrame):
                    ready.set()

            assistant.add_event_handler("on_after_process_frame", on_start)
            for processor in [llm, self.tts, self.target, observer]:
                await processor.process_frame(self.frames.StartFrame(), direction)
            await asyncio.wait_for(ready.wait(), 2)
            await self.tts.queue_frame(self.frames.TTSSpeakFrame("English introduction."))
            await asyncio.wait_for(entered.wait(), 2)
            for index, text in enumerate(("hola", "hola de nuevo")):
                await queue_and_wait(
                    observer, self.frames.TranscriptionFrame(text, "recipient", str(index), self.language.ES)
                )
            self.assertEqual(self.tts._settings.language, "es")
            self.assertTrue(handler.has_switched)
            self.assertEqual(len(self.context.messages), 1)
            self.assertIn("tts_language_active: es", self.context.messages[0]["content"])
            await queue_and_wait(self.tts, self.frames.InterruptionFrame())
            await queue_and_wait(
                observer, self.frames.TranscriptionFrame("continuar", "recipient", "3", self.language.ES)
            )
            await queue_and_wait(self.tts, self.frames.TTSSpeakFrame("Following response."))
            self.assertEqual([call["lang"] for call in calls], ["en-us", "es"])
            self.assertEqual(len(self.context.messages), 1)
            self.assertIn("tts_language_active: es", self.context.messages[0]["content"])
            self.assertEqual(handler.current_language, "es")
        finally:
            for processor in processors:
                await processor.process_frame(self.frames.CancelFrame(), direction)
            for processor in processors:
                await processor.cleanup()
            await task_observer.stop()
            await task_observer.cleanup()

    async def test_unsupported_context_failure_remains_retryable(self):
        self.publish.side_effect = RuntimeError("synthetic context failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic context failure"):
            await self.handler._switch_language("de")
        self.assertFalse(self.handler.has_switched)
        self.assertEqual(self.handler.current_language, "en")


if __name__ == "__main__":
    unittest.main()
