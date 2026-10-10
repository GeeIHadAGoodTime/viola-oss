"""Synthetic consultation producer-to-HTTP-reply contracts.

Execute unchanged production function/class bodies, real asyncio futures and a
local ASGI route. Only issuer delivery, authentication identity and Pipecat
result boundaries are inert fixtures. No account, call, device or provider runs.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]


def _execute_source(relative, names, namespace):
    path = ROOT / relative
    selected = []
    found = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        # Use the production stdlib imports, including UUID generation.
        if isinstance(node, ast.ImportFrom) and node.module in {"__future__", "dataclasses", "uuid", "typing"}:
            selected.append(node)
        elif getattr(node, "name", None) in names:
            selected.append(node)
            found.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            matches = {target.id for target in targets if isinstance(target, ast.Name)} & names
            if matches:
                selected.append(node)
                found.update(matches)
    if found != names:
        raise AssertionError(f"Missing production nodes: {names - found}")
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)


class _SyntheticUser(BaseModel):
    id: str


class ConsultationCorrelationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = asyncio.Queue()
        self.tasks = []
        self.hub = SimpleNamespace(_user_clients={"synthetic-owner": [object()]})

        async def broadcast(kind, payload, **kwargs):
            await self.events.put((kind, payload, kwargs))

        self.hub.broadcast = AsyncMock(side_effect=broadcast)
        event_module = ModuleType("ui.websocket.event_hub")
        event_module.get_event_hub = lambda: self.hub
        self.tools = ModuleType("telephony.call_tools")
        frames_module = ModuleType("pipecat.frames.frames")
        frames_module.FunctionCallResultProperties = SimpleNamespace
        modules = {"telephony.call_tools": self.tools, "ui.websocket.event_hub": event_module,
                   "pipecat.frames.frames": frames_module}
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        self.addCleanup(module_patch.stop)
        self.ns = self.tools.__dict__
        self.ns.update(asyncio=asyncio, logger=logging.getLogger(__name__))
        _execute_source("telephony/call_tools.py", {
            "_PendingConsultation", "_PENDING_CONSULTATIONS", "CONSULT_USER_FALLBACK_ANSWER",
            "_VOICE_CONSULT_TIMEOUT", "_MESSAGING_CONSULT_TIMEOUT", "PIPELINE_FUNCTION_TIMEOUT_MARGIN_SECS",
            "_WEB_CONSULT_CHANNEL_TYPES", "_call_record_id", "_call_record_user_id", "_event_hub_has_consult_target",
            "_broadcast_call_consultation", "_web_consult_timeout", "_ask_call_issuer_via_web_consult",
            "submit_consult_user_reply", "_consult_timeout_for_channel", "_issuer_channel_description",
            "_issuer_channel_needs_web_consult", "_ask_call_issuer", "consult_user_handler",
            "_consult_handler_wait_budget_secs", "_function_result_properties",
            "_continue_llm_function_result_properties",
        }, self.ns)
        self.record = SimpleNamespace(call_id="synthetic-call", user_id="synthetic-owner", issuer_channel=None,
                                      issuer_channel_info={"channel_type": "web"})

        async def current_user(request: Request):
            owner = request.headers.get("x-synthetic-owner")
            return _SyntheticUser(id=owner) if owner else None

        async def verify_request(request):
            return request.headers.get("x-synthetic-auth") == "yes"

        router = APIRouter()
        route_ns = {
            "APIRouter": APIRouter, "Depends": Depends, "Request": Request, "User": _SyntheticUser,
            "get_current_user_optional": current_user, "HTTPException": HTTPException,
            "JSONResponse": JSONResponse, "tos_router": router, "logger": logging.getLogger(__name__),
            "failure_response": lambda code, message: {"ok": False, "error": message, "error_code": code},
        }
        _execute_source("telephony/routes.py", {
            "_require_call_auth", "_route_user_id", "reply_to_phone_call_consultation",
        }, route_ns)
        app = FastAPI()
        app.state.auth_plugin = SimpleNamespace(verify_request=verify_request)
        app.include_router(router, prefix="/v1/phone")
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://synthetic.invalid")
        self.addAsyncCleanup(self.client.aclose)

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.assertEqual(self.ns["_PENDING_CONSULTATIONS"], {})

    async def start_question(self, text="Synthetic question?", record=None):
        task = asyncio.create_task(self.ns["_ask_call_issuer_via_web_consult"](text, record or self.record, "medium"))
        self.tasks.append(task)
        kind, event, routing = await asyncio.wait_for(self.events.get(), timeout=1)
        self.assertEqual(kind, "call_consultation")
        self.assertEqual(routing, {"user_id": "synthetic-owner", "force": True})
        self.assertTrue(event["pending"])
        self.assertRegex(event["consultation_id"], r"^[0-9a-f]{32}$")
        return task, event

    async def reply(self, body, *, owner="synthetic-owner", authenticated=True, call_id="synthetic-call"):
        headers = {}
        if owner is not None:
            headers["x-synthetic-owner"] = owner
        if authenticated:
            headers["x-synthetic-auth"] = "yes"
        return await self.client.post(f"/v1/phone/call/{call_id}/reply", json=body, headers=headers)

    async def test_generated_id_round_trips_to_matching_future(self):
        task, event = await self.start_question()
        pending = self.ns["_PENDING_CONSULTATIONS"][self.record.call_id]
        self.assertEqual(event["consultation_id"], pending.consultation_id)
        response = await self.reply({"answer": "  Synthetic answer  ", "consultation_id": event["consultation_id"]})
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True}))
        self.assertEqual(await task, "Synthetic answer")

    async def test_replaced_question_rejects_delayed_answer_even_with_identical_text(self):
        first, old_event = await self.start_question()
        second, event = await self.start_question()
        self.assertNotEqual(old_event["consultation_id"], event["consultation_id"])
        self.assertEqual(await first, self.ns["CONSULT_USER_FALLBACK_ANSWER"])
        pending = self.ns["_PENDING_CONSULTATIONS"][self.record.call_id]
        response = await self.reply({"answer": "Old answer", "consultation_id": old_event["consultation_id"]})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["ok"])
        self.assertFalse(pending.future.done())
        response = await self.reply({"answer": "New answer", "consultation_id": event["consultation_id"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(await second, "New answer")

    async def test_retry_after_accepted_old_answer_cannot_answer_next_question(self):
        first, old_event = await self.start_question()
        body = {"answer": "First answer", "consultation_id": old_event["consultation_id"]}
        self.assertEqual((await self.reply(body)).status_code, 200)
        self.assertEqual(await first, "First answer")
        second, event = await self.start_question("Next question?")
        self.assertEqual((await self.reply(body)).status_code, 409)
        self.assertFalse(second.done())
        self.assertEqual((await self.reply({"answer": "Second answer", "consultation_id": event["consultation_id"]})).status_code, 200)
        self.assertEqual(await second, "Second answer")

    async def test_timed_out_question_id_cannot_answer_later_question(self):
        self.ns["_web_consult_timeout"] = lambda record: 0
        first, old_event = await self.start_question()
        self.assertEqual(await first, self.ns["CONSULT_USER_FALLBACK_ANSWER"])
        self.ns["_web_consult_timeout"] = lambda record: 20
        second, event = await self.start_question()
        response = await self.reply({"answer": "Late", "consultation_id": old_event["consultation_id"]})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.ns["_PENDING_CONSULTATIONS"][self.record.call_id].future.done())
        self.assertEqual((await self.reply({"answer": "Current", "consultation_id": event["consultation_id"]})).status_code, 200)
        self.assertEqual(await second, "Current")

    async def test_duplicate_after_completion_is_not_acknowledged_again(self):
        task, event = await self.start_question()
        body = {"answer": "Synthetic answer", "consultation_id": event["consultation_id"]}
        self.assertEqual((await self.reply(body)).status_code, 200)
        self.assertEqual(await task, "Synthetic answer")
        self.assertEqual((await self.reply(body)).status_code, 404)

    async def test_owner_authorization_precedes_correlation_check(self):
        task, event = await self.start_question()
        for identity in (event["consultation_id"], "unrelated-id"):
            with self.subTest(identity=identity):
                response = await self.reply({"answer": "No", "consultation_id": identity}, owner="other-owner")
                self.assertEqual(response.status_code, 403)
                self.assertFalse(self.ns["_PENDING_CONSULTATIONS"][self.record.call_id].future.done())
        self.assertFalse(task.done())

    async def test_question_id_does_not_replace_route_authentication(self):
        task, event = await self.start_question()
        response = await self.reply({"answer": "No", "consultation_id": event["consultation_id"]}, authenticated=False)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(task.done())

    async def test_missing_owner_identity_is_rejected(self):
        task, event = await self.start_question()
        response = await self.reply({"answer": "No", "consultation_id": event["consultation_id"]}, owner=None)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(task.done())

    async def test_unowned_pending_consultation_is_never_authorized(self):
        task, event = await self.start_question()
        self.ns["_PENDING_CONSULTATIONS"][self.record.call_id].user_id = ""
        response = await self.reply({"answer": "No", "consultation_id": event["consultation_id"]})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(task.done())

    async def test_id_for_another_call_cannot_resolve_this_call(self):
        first, old_event = await self.start_question()
        other_record = SimpleNamespace(**{**vars(self.record), "call_id": "other-call"})
        second, event = await self.start_question(record=other_record)
        response = await self.reply({"answer": "No", "consultation_id": old_event["consultation_id"]}, call_id="other-call")
        self.assertEqual(response.status_code, 409)
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        self.assertNotEqual(old_event["consultation_id"], event["consultation_id"])

    async def test_explicit_malformed_ids_never_downgrade_to_legacy(self):
        task, event = await self.start_question()
        for identity in (None, "", " ", 0, 1, False, True, [], {}, [event["consultation_id"]]):
            with self.subTest(identity=identity):
                response = await self.reply({"answer": "No", "consultation_id": identity})
                self.assertEqual(response.status_code, 400)
                self.assertFalse(self.ns["_PENDING_CONSULTATIONS"][self.record.call_id].future.done())
        self.assertFalse(task.done())

    async def test_nonmatching_string_id_is_compared_exactly(self):
        task, event = await self.start_question()
        response = await self.reply({"answer": "No", "consultation_id": f" {event['consultation_id']} "})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(task.done())

    async def test_legacy_http_body_still_targets_current_question_without_stale_protection(self):
        first, _ = await self.start_question()
        second, _ = await self.start_question()
        self.assertEqual(await first, self.ns["CONSULT_USER_FALLBACK_ANSWER"])
        # Deliberately document the compatibility limitation, rather than claim
        # that an uncorrelated legacy answer gains stale-question protection.
        response = await self.reply({"answer": "Legacy call-level answer"})
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True}))
        self.assertEqual(await second, "Legacy call-level answer")

    async def test_legacy_internal_takeover_signature_is_unchanged(self):
        task, _ = await self.start_question()
        result = self.ns["submit_consult_user_reply"](self.record.call_id, "I am joining the call now.", self.record.user_id)
        self.assertEqual(result, (True, "accepted"))
        self.assertEqual(await task, "I am joining the call now.")

    async def test_internal_reply_still_requires_owner(self):
        task, event = await self.start_question()
        submit = self.ns["submit_consult_user_reply"]
        self.assertEqual(submit(self.record.call_id, "No", consultation_id=event["consultation_id"]), (False, "auth_required"))
        self.assertEqual(submit(self.record.call_id, "No", "other-owner", consultation_id=event["consultation_id"]), (False, "forbidden"))
        self.assertFalse(task.done())

    async def test_invalid_internal_ids_do_not_resolve_future(self):
        task, _ = await self.start_question()
        for identity in ("", " ", False, 1, []):
            self.assertEqual(self.ns["submit_consult_user_reply"](
                self.record.call_id, "No", self.record.user_id, consultation_id=identity,
            ), (False, "invalid_consultation_id"))
        self.assertFalse(task.done())

    async def test_legacy_errors_and_missing_call_behavior_remain(self):
        for body in ({}, {"answer": " "}, [], None):
            self.assertEqual((await self.reply(body)).status_code, 400)
        for body in ({"answer": "Answer"}, {"answer": "Answer", "consultation_id": "unknown"}):
            self.assertEqual((await self.reply(body)).status_code, 404)

    async def test_no_delivery_cleans_up_pending_question(self):
        self.hub._user_clients = {}
        task, _ = await self.start_question()
        self.assertEqual(await task, self.ns["CONSULT_USER_FALLBACK_ANSWER"])
        self.assertEqual(self.ns["_PENDING_CONSULTATIONS"], {})

    async def test_broadcast_failure_cleans_up_pending_question(self):
        self.hub.broadcast.side_effect = RuntimeError("synthetic delivery unavailable")
        result = await self.ns["_ask_call_issuer_via_web_consult"]("Question?", self.record, "low")
        self.assertEqual(result, self.ns["CONSULT_USER_FALLBACK_ANSWER"])
        self.assertEqual(self.ns["_PENDING_CONSULTATIONS"], {})

    async def test_cancelled_question_cleans_up_only_its_own_wait(self):
        first, _ = await self.start_question()
        second, event = await self.start_question()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        self.assertEqual(self.ns["_PENDING_CONSULTATIONS"][self.record.call_id].consultation_id, event["consultation_id"])
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
        self.assertEqual(self.ns["_PENDING_CONSULTATIONS"], {})

    async def test_voice_handler_and_answer_notification_stay_uncorrelated(self):
        channel = SimpleNamespace(channel_type="voice", ask=AsyncMock(return_value="Voice answer"))
        self.record.issuer_channel = channel
        params = SimpleNamespace(arguments={"question": "Voice question?", "urgency": "low"}, result_callback=AsyncMock())
        await self.ns["consult_user_handler"](params, self.record)
        channel.ask.assert_awaited_once_with("Voice question?", timeout=self.ns["_VOICE_CONSULT_TIMEOUT"])
        kind, event, _ = await self.events.get()
        self.assertEqual(kind, "call_consultation")
        self.assertEqual(event["answer"], "Voice answer")
        self.assertNotIn("consultation_id", event)
        self.assertNotIn("pending", event)
        self.assertEqual(params.result_callback.await_args.args, ({"user_response": "Voice answer"},))
        self.assertTrue(params.result_callback.await_args.kwargs["properties"].run_llm)
        self.assertEqual(self.ns["_PENDING_CONSULTATIONS"], {})


if __name__ == "__main__":
    unittest.main()
