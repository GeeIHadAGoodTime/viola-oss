"""Synthetic spoken-goodbye recovery through unchanged production source bodies.

No model, Pipecat installation, audio device, or carrier is used. Real transcript
collection, production/loopback callbacks, tool/latch logic, output-mark ordering,
and confirmed-hangup status logic execute with inert frame/transport boundaries.
These are source contracts, not live audio/carrier qualification.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import re
import sys
import unittest
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]


class _Frame:
    def __init__(self, text="", **kwargs):
        self.text = text
        self.__dict__.update(kwargs)


class _Processor:
    def __init__(self, *args, **kwargs):
        pass

    async def process_frame(self, frame, direction):
        pass

    async def push_frame(self, frame, direction=None):
        pass


class _Direction(Enum):
    DOWNSTREAM = "downstream"
    UPSTREAM = "upstream"


def _execute_nodes(relative, names, namespace):
    path = ROOT / relative
    tree = ast.parse(path.read_text(encoding="utf-8"))
    selected = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in tree.body:
        if getattr(node, "name", None) in names:
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in names for target in node.targets
        ):
            selected.append(node)
    exec(compile(ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])), str(path), "exec"), namespace)


def _load_namespace():
    namespace = {
        "asyncio": asyncio, "re": re, "suppress": suppress, "dataclass": dataclass,
        "datetime": datetime, "UTC": UTC, "Enum": Enum, "logger": logging.getLogger(__name__),
        "PIPECAT_AVAILABLE": True, "ControlFrame": _Frame, "FrameProcessor": _Processor,
        "FrameDirection": _Direction, "get_event_bus": lambda: None,
        "detect_persistence_objection": lambda text: False,
        "detect_called_party_opt_out": lambda text: False,
    }
    for name in (
        "InterruptionFrame", "LLMFullResponseEndFrame", "LLMFullResponseStartFrame",
        "LLMTextFrame", "TextFrame", "TranscriptionFrame", "TTSSpeakFrame",
    ):
        namespace[name] = type(name, (_Frame,), {})
    _execute_nodes("telephony/call_manager.py", {
        "CallStatus", "_TERMINAL_CALL_STATUSES", "_is_terminal_call_status", "TranscriptCollector",
    }, namespace)
    _execute_nodes("telephony/end_call_hangup.py", {
        "END_CALL_MEDIA_MARK_TIMEOUT_SECONDS", "EndCallHangupFrame",
        "dispatch_telnyx_end_call_hangup", "EndCallHangupAfterOutputProcessor",
    }, namespace)
    _execute_nodes("telephony/call_tools.py", {
        "END_CALL_MIN_DURATION_SECONDS", "_LatchedEndCall", "fire_latched_end_call_if_pending",
        "cancel_latched_end_call", "make_end_call_handler", "_current_end_call_response_has_spoken_text",
        "_function_result_properties", "_continue_llm_function_result_properties", "_no_llm_function_result_properties",
        "_SPOKEN_CLOSE_COURTESY", "_SPOKEN_CLOSE_FAREWELL", "_ASSISTANT_SPOKEN_CLOSE_RE",
        "_RECIPIENT_SPOKEN_CLOSE_RE", "_is_terminal_spoken_close", "_has_adjacent_spoken_close",
        "complete_spoken_goodbye_end_call_if_omitted",
    }, namespace)
    _execute_nodes("telephony/transcription_observer.py", {"TranscriptFrameCollector"}, namespace)
    return namespace


def _load_callback(relative, name, namespace):
    path = ROOT / relative
    tree = ast.parse(path.read_text(encoding="utf-8"))
    selected = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name]
    if len(selected) != 1:
        raise AssertionError(f"Expected exactly one real callback {name}: found {len(selected)}")
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected]
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(path), "exec"), namespace)
    return namespace[name]


class SpokenGoodbyeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ns = _load_namespace()
        modules = {}
        for module_name, values in {
            "telephony.call_tools": self.ns,
            "telephony.call_manager": self.ns,
            "intent.log_redaction": {"redact_card_data": lambda text: text},
            "pipecat.frames.frames": {"FunctionCallResultProperties": SimpleNamespace},
        }.items():
            module = ModuleType(module_name)
            module.__dict__.update(values)
            modules[module_name] = module
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        self.addCleanup(module_patch.stop)
        self.status = self.ns["CallStatus"]
        self.record = SimpleNamespace(
            call_id="synthetic-call", telnyx_call_control_id="synthetic-recipient-leg",
            status=self.status.ACTIVE, outcome=None, transcript=[],
            first_assistant_turn_complete=True, human_takeover_detected=False,
            voicemail_detected=False, _end_call_committed=False, _pending_end_call=None,
            _completed_spoken_turn_recipient_end_index=None,
            _telnyx_hangup_dispatched=False, local_end_at=None,
        )
        self.events = []
        self.frames = []

        async def hangup(*, call_control_id):
            self.events.append(("hangup", call_control_id))

        async def drain(mark_name, *, timeout):
            self.events.append(("drain", self.record._end_call_committed))
            return True

        self.client = SimpleNamespace(
            calls=SimpleNamespace(actions=SimpleNamespace(hangup=AsyncMock(side_effect=hangup)))
        )
        self.transport = SimpleNamespace(wait_for_output_mark=AsyncMock(side_effect=drain))
        self.processor = self.ns["EndCallHangupAfterOutputProcessor"](
            telnyx_client=self.client,
            call_control_id_getter=lambda: self.record.telnyx_call_control_id,
            call_record=self.record, transport=self.transport,
        )

        async def push(frame):
            self.frames.append(frame)
            await self.processor.process_frame(frame, _Direction.DOWNSTREAM)

        self.push = push
        self.llm = SimpleNamespace(push_frame=push)
        self.record._llm_service = self.llm

    def pair(self, recipient="Okay, thanks, bye.", assistant="Thanks. Bye."):
        self.record.transcript = [{"role": "them", "text": recipient}, {"role": "viola", "text": assistant}]
        # Helper-only tests explicitly supply a qualified collector boundary.
        # Callback tests below obtain this marker from the real frame collector.
        self.record._completed_spoken_turn_recipient_end_index = 1

    async def recover(self, text="Thanks. Bye.", **kwargs):
        arguments = dict(assistant_text=text, telnyx_client=self.client,
                         push_frame=self.push, hangup_after_output_drain=True)
        arguments.update(kwargs)
        return await self.ns["complete_spoken_goodbye_end_call_if_omitted"](self.record, **arguments)

    def callback(self, kind):
        if kind == "production":
            self.ns.update(record=self.record, telnyx_client=self.client, _phone_text_gate_executor=None)
            return _load_callback("telephony/call_manager.py", "_on_assistant_text_complete", self.ns)
        factory = _load_callback(
            "telephony/loopback_phone_call_session.py", "_make_loopback_assistant_complete", self.ns
        )
        self.session = SimpleNamespace(_fake_telnyx=self.client, latched_end_call_fires=[])
        return factory(self.session, self.record, self.llm)

    async def drive_frames(self, kind, frames):
        callback = self.callback(kind)
        transcript = self.ns["TranscriptCollector"](self.record)
        user = self.ns["TranscriptFrameCollector"](transcript, capture_assistant=False)
        assistant = self.ns["TranscriptFrameCollector"](
            transcript, capture_user=False, on_assistant_complete=callback
        )
        for frame in frames:
            collector = user if isinstance(frame, self.ns["TranscriptionFrame"]) else assistant
            await collector.process_frame(frame, _Direction.DOWNSTREAM)

    async def drive_turn(self, kind, *, recipient="Okay, thanks, bye.", assistant="Thanks. Bye.", interrupted=False):
        frames = [self.ns["TranscriptionFrame"](recipient), self.ns["LLMFullResponseStartFrame"](),
                  self.ns["LLMTextFrame"](assistant)]
        if interrupted:
            frames.append(self.ns["InterruptionFrame"]())
        frames.append(self.ns["LLMFullResponseEndFrame"]())
        await self.drive_frames(kind, frames)

    def split_turn_frames(self, recipient_chunks):
        return [*[self.ns["TranscriptionFrame"](chunk, finalized=True) for chunk in recipient_chunks],
                self.ns["LLMFullResponseStartFrame"](), self.ns["LLMTextFrame"]("Goodbye."),
                self.ns["LLMFullResponseEndFrame"]()]

    def assert_completed_once(self):
        self.assertEqual(self.events, [("drain", True), ("hangup", "synthetic-recipient-leg")])
        self.assertEqual(len(self.frames), 1)
        self.assertEqual(self.frames[0].call_id, self.record.call_id)
        self.assertEqual(self.record.status, self.status.COMPLETED)
        self.assertTrue(self.record._telnyx_hangup_dispatched)
        self.assertIsNotNone(self.record.local_end_at)

    async def test_production_collector_wires_no_tool_recovery(self):
        await self.drive_turn("production")
        self.assert_completed_once()

    async def test_loopback_collector_wires_no_tool_recovery(self):
        await self.drive_turn("loopback")
        self.assert_completed_once()
        self.assertEqual(self.session.latched_end_call_fires, [{"text": "Thanks. Bye.", "fired": False}])

    async def test_production_split_farewell_chunks_form_one_close(self):
        await self.drive_frames("production", self.split_turn_frames(["Okay,", "thanks,", "bye."]))
        self.assert_completed_once()

    async def test_loopback_split_farewell_chunks_form_one_close(self):
        await self.drive_frames("loopback", self.split_turn_frames(["Okay,", "thanks,", "bye."]))
        self.assert_completed_once()

    async def test_split_instruction_or_negation_cannot_become_a_farewell(self):
        for kind in ("production", "loopback"):
            for chunks in (["The word I asked you to repeat is", "goodbye."], ["I am not", "all set."],
                           ["The word I asked you to repeat is goodbye."], ["Before you go,", "take care."]):
                with self.subTest(kind=kind, chunks=chunks):
                    self.record.transcript = []
                    await self.drive_frames(kind, self.split_turn_frames(chunks))
                    self.assertFalse(self.record._end_call_committed)
                    self.assertEqual(self.events, [])

    async def test_new_recipient_chunk_during_generation_cannot_authorize_older_reply(self):
        for kind in ("production", "loopback"):
            for initial, late in (("Okay,", "thanks, bye."), ("Okay, thanks, bye.", "Actually, wait.")):
                with self.subTest(kind=kind, initial=initial, late=late):
                    self.record.transcript = []
                    await self.drive_frames(kind, [
                        self.ns["TranscriptionFrame"](initial), self.ns["LLMFullResponseStartFrame"](),
                        self.ns["LLMTextFrame"]("Goodbye."), self.ns["TranscriptionFrame"](late),
                        self.ns["LLMFullResponseEndFrame"](),
                    ])
                    self.assertFalse(self.record._end_call_committed)
                    self.assertEqual(self.events, [])

    async def test_late_text_after_interruption_is_not_eligible_spoken_completion(self):
        for kind in ("production", "loopback"):
            with self.subTest(kind=kind):
                self.record.transcript = []
                await self.drive_frames(kind, [
                    self.ns["TranscriptionFrame"]("Okay, thanks, bye."),
                    self.ns["LLMFullResponseStartFrame"](), self.ns["LLMTextFrame"]("Thanks."),
                    self.ns["InterruptionFrame"](), self.ns["LLMTextFrame"]("Goodbye."),
                    self.ns["LLMFullResponseEndFrame"](),
                ])
                self.assertFalse(self.record._end_call_committed)
                self.assertIsNone(self.record._completed_spoken_turn_recipient_end_index)
                self.assertEqual(self.events, [])

    async def test_skipped_tts_or_suppressed_text_cannot_authorize_hangup(self):
        for kind in ("production", "loopback"):
            for flag in ("skip_tts", "viola_skip_transcript"):
                with self.subTest(kind=kind, flag=flag):
                    self.record.transcript = []
                    await self.drive_frames(kind, [
                        self.ns["TranscriptionFrame"]("Okay, thanks, bye."),
                        self.ns["LLMFullResponseStartFrame"](),
                        self.ns["LLMTextFrame"]("Thanks.", **{flag: True}),
                        self.ns["LLMTextFrame"]("Goodbye."), self.ns["LLMFullResponseEndFrame"](),
                    ])
                    self.assertFalse(self.record._end_call_committed)
                    self.assertIsNone(self.record._completed_spoken_turn_recipient_end_index)
                    self.assertEqual(self.events, [])

    async def test_skipped_end_flush_cannot_qualify_buffered_goodbye(self):
        for kind in ("production", "loopback"):
            with self.subTest(kind=kind):
                self.record.transcript = []
                await self.drive_frames(kind, [
                    self.ns["TranscriptionFrame"]("Okay, bye."),
                    self.ns["LLMFullResponseStartFrame"](), self.ns["LLMTextFrame"]("Goodbye"),
                    self.ns["LLMFullResponseEndFrame"](skip_tts=True),
                ])
                self.assertFalse(self.record._end_call_committed)
                self.assertIsNone(self.record._completed_spoken_turn_recipient_end_index)
                self.assertEqual(self.events, [])

    async def test_repeated_and_concurrent_callbacks_are_single_shot(self):
        self.pair()
        self.assertEqual(await asyncio.gather(self.recover(), self.recover()), [True, False])
        self.assertFalse(await self.recover())
        self.assert_completed_once()

    async def test_carrier_dispatch_waits_until_output_mark_resolves(self):
        self.pair()
        started, release = asyncio.Event(), asyncio.Event()

        async def wait_for_mark(mark_name, *, timeout):
            started.set()
            await release.wait()
            self.events.append(("drain", self.record._end_call_committed))
            return True

        self.transport.wait_for_output_mark.side_effect = wait_for_mark
        task = asyncio.create_task(self.recover())
        try:
            await asyncio.wait_for(started.wait(), 1)
            self.assertTrue(self.record._end_call_committed)
            self.assertEqual(self.record.status, self.status.ACTIVE)
            self.client.calls.actions.hangup.assert_not_awaited()
        finally:
            release.set()
            await asyncio.wait_for(task, 1)
        self.assert_completed_once()

    async def test_current_recipient_leg_selected_after_output_drain(self):
        self.pair()

        async def switch_leg(mark_name, *, timeout):
            self.client.calls.actions.hangup.assert_not_awaited()
            self.record.telnyx_call_control_id = "synthetic-current-leg"
            self.events.append(("drain", True))
            return True

        self.transport.wait_for_output_mark.side_effect = switch_leg
        self.assertTrue(await self.recover())
        self.assertEqual(self.events, [("drain", True), ("hangup", "synthetic-current-leg")])
        self.assertEqual(self.record.status, self.status.COMPLETED)

    async def test_complete_courtesy_and_farewell_grammar(self):
        match = self.ns["_is_terminal_spoken_close"]
        for text in ("Bye!", "Thanks. Goodbye.", "Okay, thank you, take care.", "Have a great day.",
                     "THANKS FOR YOUR HELP. BYE.", "Talk soon."):
            with self.subTest(text=text):
                self.assertTrue(match(text))
                self.assertTrue(match(text, recipient=True))
        for text in ("That's all.", "All set!", "You can hang up now."):
            with self.subTest(text=text):
                self.assertTrue(match(text, recipient=True))
                self.assertFalse(match(text))

    async def test_context_negation_and_quoted_speech_never_match(self):
        match = self.ns["_is_terminal_spoken_close"]
        for text in (
            "I am not all set", "Not goodbye", "Don't say goodbye", "Do not hang up",
            "Before you go, please take care", "If I say goodbye", "Never say goodbye",
            "The word I asked you to repeat is goodbye", "You asked me to repeat goodbye.",
            '"Goodbye"', "'bye'", "Bye?", "Thanks, can you help with one more thing?",
            "Do you have a good recommendation?", "Take care to enter your email correctly.",
            "PAYMENT_GATE: Goodbye.", "Thanks", "Thank you", "", None, "okay " * 17 + "bye",
        ):
            for recipient in (False, True):
                with self.subTest(text=text, recipient=recipient):
                    self.assertFalse(match(text, recipient=recipient))

    async def test_false_positive_recipient_does_not_commit(self):
        for text in ("I am not all set", "The word is goodbye", "Thanks", "Can you help?"):
            with self.subTest(text=text):
                self.pair(recipient=text)
                self.assertFalse(await self.recover())
                self.assertFalse(self.record._end_call_committed)
        self.assertEqual(self.events, [])

    async def test_immediate_adjacency_and_recorded_callback_text_required(self):
        for middle in ({"role": "viola", "text": "One more thing."},
                       {"role": "them", "text": "Actually, wait."},
                       {"role": "tool", "text": "continue"}, None):
            with self.subTest(middle=middle):
                self.pair()
                self.record.transcript.insert(-1, middle)
                self.assertFalse(await self.recover())
        self.pair()
        self.assertFalse(await self.recover("Goodbye."))
        self.record.transcript.pop()
        self.assertFalse(await self.recover())
        self.assertEqual(self.events, [])

    async def test_first_turn_takeover_pending_and_terminal_guards(self):
        for attribute, value in (
            ("first_assistant_turn_complete", False), ("human_takeover_detected", True),
            ("_pending_end_call", object()), ("_end_call_committed", True),
            ("_telnyx_hangup_dispatched", True),
            ("_completed_spoken_turn_recipient_end_index", None),
            *(("status", status) for status in self.ns["_TERMINAL_CALL_STATUSES"]),
        ):
            with self.subTest(attribute=attribute, value=value):
                self.pair()
                old = getattr(self.record, attribute)
                setattr(self.record, attribute, value)
                self.assertFalse(await self.recover())
                setattr(self.record, attribute, old)
        self.assertEqual(self.events, [])

    async def test_interrupted_generation_cannot_trigger_callback(self):
        for kind in ("production", "loopback"):
            with self.subTest(kind=kind):
                self.record.transcript = []
                await self.drive_turn(kind, interrupted=True)
                self.assertFalse(self.record._end_call_committed)
        self.assertEqual(self.events, [])

    async def test_payment_callback_still_runs_without_recovering_unspoken_text(self):
        self.pair()
        callback = self.callback("production")
        payment = AsyncMock()
        self.ns["_phone_text_gate_executor"] = object()
        self.ns["CallManager"] = SimpleNamespace(_maybe_start_phone_payment_confirmation_from_text=payment)
        await callback("PAYMENT_GATE: Goodbye.")
        payment.assert_awaited_once()
        self.assertEqual(self.events, [])
        self.assertFalse(self.record._end_call_committed)

    async def test_old_latch_precedes_no_tool_recovery(self):
        self.pair(recipient="I still need help.", assistant="Your requested closing line.")
        self.record._pending_end_call = self.ns["_LatchedEndCall"](
            reason="explicit prior end call", telnyx_client=self.client,
            call_control_id="synthetic-recipient-leg", hangup_after_output_drain=True,
        )
        await self.callback("production")("Your requested closing line.")
        self.assert_completed_once()
        self.assertIsNone(self.record._pending_end_call)

    async def test_explicit_end_call_wins_before_completed_turn(self):
        self.pair()
        handler = self.ns["make_end_call_handler"](
            telnyx_client=self.client, call_control_id="synthetic-recipient-leg",
            call_record=self.record, hangup_after_output_drain=True,
        )
        params = SimpleNamespace(arguments={"reason": "done", "_viola_response_had_spoken_text": True},
                                 llm=self.llm, result_callback=AsyncMock())
        await handler(params)
        await self.callback("production")("Thanks. Bye.")
        self.assert_completed_once()
        self.assertEqual(params.result_callback.await_args.args[0]["call_status"], "ending")

    async def test_failed_carrier_hangup_stays_nonterminal_and_can_reconcile(self):
        self.pair()
        self.client.calls.actions.hangup.side_effect = OSError("synthetic carrier failure")
        self.assertTrue(await self.recover())
        self.assertEqual(self.record.status, self.status.ACTIVE)
        self.assertFalse(self.record._telnyx_hangup_dispatched)
        self.assertIsNone(self.record.local_end_at)
        self.client.calls.actions.hangup.side_effect = None
        await self.ns["dispatch_telnyx_end_call_hangup"](
            telnyx_client=self.client, call_control_id="", call_record=self.record, reason_text="retry",
        )
        self.assertEqual(self.record.status, self.status.COMPLETED)
        self.assertEqual(self.client.calls.actions.hangup.await_count, 2)

    async def test_missing_leg_does_not_mark_completed(self):
        self.pair()
        self.record.telnyx_call_control_id = ""
        self.assertTrue(await self.recover(push_frame=None))
        self.assertEqual(self.record.status, self.status.ACTIVE)
        self.client.calls.actions.hangup.assert_not_awaited()

    async def test_push_failure_reuses_existing_direct_dispatch_fallback(self):
        self.pair()
        self.assertTrue(await self.recover(push_frame=AsyncMock(side_effect=RuntimeError("closed pipeline"))))
        self.assertEqual(self.events, [("hangup", "synthetic-recipient-leg")])
        self.assertEqual(self.record.status, self.status.COMPLETED)


if __name__ == "__main__":
    unittest.main()
