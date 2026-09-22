"""Synthetic contracts for the optional durable carrier-event adapter seam.

The production call-manager module has a large optional runtime dependency
graph. These checks compile its unchanged carrier-boundary declarations and
methods with only synthetic carrier/history modules, so no model, provider,
phone, cache, or home-directory state is initialized.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import json
import sys
import types
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch


SOURCE_ROOT = Path(__file__).resolve().parents[2]
_NAMES = {
    "CallStatus",
    "CallRecord",
    "CarrierCallIdentity",
    "CarrierEvent",
    "CarrierEventProjection",
    "CarrierEventAdapter",
}
_METHODS = {
    "get_record",
    "get_record_by_call_control_id",
    "_carrier_identity_for_record",
    "_projection_matches_identity",
    "_apply_carrier_projection",
    "_resolve_carrier_event_record",
    "_apply_durable_carrier_event",
    "_bind_carrier_call_control_id",
    "handle_call_answered",
    "handle_call_cost",
}


def _carrier_boundary_namespace() -> dict[str, object]:
    tree = ast.parse((SOURCE_ROOT / "telephony/call_manager.py").read_text(encoding="utf-8"))
    body: list[ast.stmt] = [
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        ast.Import(names=[ast.alias(name="asyncio")]),
        ast.ImportFrom(module="dataclasses", names=[ast.alias(name="dataclass"), ast.alias(name="field")], level=0),
        ast.ImportFrom(module="datetime", names=[ast.alias(name="UTC"), ast.alias(name="datetime")], level=0),
        ast.ImportFrom(module="enum", names=[ast.alias(name="Enum")], level=0),
        ast.ImportFrom(module="typing", names=[ast.alias(name="Any"), ast.alias(name="Literal"), ast.alias(name="Protocol")], level=0),
    ]
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in _NAMES:
            body.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "CallManager":
            selected = [
                member
                for member in node.body
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name in _METHODS
            ]
            body.append(ast.ClassDef(name="CallManager", bases=[], keywords=[], body=selected, decorator_list=[]))
    namespace: dict[str, object] = {
        "logger": types.SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), "carrier_boundary.py", "exec"), namespace)
    return namespace


def _local_webhook_handler() -> tuple[object, dict[str, object]]:
    tree = ast.parse((SOURCE_ROOT / "telephony/local_webhook.py").read_text(encoding="utf-8"))
    handler = next(
        node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "local_telnyx_webhook"
    )
    handler = copy.deepcopy(handler)
    handler.decorator_list = []

    class FakeHttpException(Exception):
        def __init__(self, status_code: int, detail: str) -> None:
            self.status_code = status_code
            self.detail = detail
            super().__init__(detail)

    namespace: dict[str, object] = {
        "asyncio": asyncio,
        "json": json,
        "time": __import__("time"),
        "settings": types.SimpleNamespace(telnyx_webhook_public_key="public-key"),
        "HTTPException": FakeHttpException,
        "TELNYX_TIMESTAMP_TOLERANCE_SECONDS": 300,
        "telnyx_timestamp_is_fresh": lambda _value: True,
        "verify_telnyx_signature": lambda *_args: True,
        "_event_lock": asyncio.Lock(),
        "_completed_events": {},
        "_AMD_EVENTS": frozenset(),
    }
    body: list[ast.stmt] = [
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        handler,
    ]
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), "local_webhook.py", "exec"), namespace)
    return namespace["local_telnyx_webhook"], namespace


class CarrierEventAdapterContract(unittest.TestCase):
    def test_public_webhook_keeps_false_manager_result_retryable(self) -> None:
        class Manager:
            async def handle_call_answered(self, _call_control_id: str) -> bool:
                return False

        handler, namespace = _local_webhook_handler()
        intent_package = types.ModuleType("intent")
        intent_package.__path__ = []  # type: ignore[attr-defined]
        tools_package = types.ModuleType("intent.tools")
        tools_package.__path__ = []  # type: ignore[attr-defined]
        phone_call = types.ModuleType("intent.tools.phone_call")
        phone_call._get_manager = lambda: Manager()
        phone_call._uses_independent_local_phone = lambda: True

        class Request:
            headers = {"telnyx-timestamp": "synthetic", "telnyx-signature-ed25519": "synthetic"}

            async def body(self) -> bytes:
                return json.dumps(
                    {
                        "data": {
                            "id": "event-synthetic",
                            "event_type": "call.answered",
                            "payload": {"call_control_id": "control-synthetic"},
                        }
                    }
                ).encode("utf-8")

        with patch.dict(
            sys.modules,
            {"intent": intent_package, "intent.tools": tools_package, "intent.tools.phone_call": phone_call},
        ):
            with self.assertRaises(Exception) as raised:
                asyncio.run(handler(Request()))

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(namespace["_completed_events"], {})

    def test_durable_event_requires_exact_identity_and_updates_history_after_acceptance(self) -> None:
        ns = _carrier_boundary_namespace()
        identity_type = ns["CarrierCallIdentity"]
        projection_type = ns["CarrierEventProjection"]
        manager_type = ns["CallManager"]
        identity = identity_type("owner-a", "call-synthetic", "control-synthetic")

        class Adapter:
            def __init__(self) -> None:
                self.events: list[object] = []

            async def bind_call_control_id(self, _identity: object) -> None:
                return None

            async def resolve_call(self, call_control_id: str):
                self.assertEqual(call_control_id, identity.call_control_id)
                return projection_type(identity=identity, status="active", carrier_revision=1)

            async def apply_event(self, event: object, expected_identity: object):
                self.events.append((event, expected_identity))
                return projection_type(
                    identity=identity,
                    status="completed",
                    duration_seconds=48,
                    estimated_cost_usd=0.08,
                    carrier_billed_duration_seconds=48,
                    carrier_total_cost_usd=0.08,
                    carrier_cost_at=datetime.now(tz=UTC),
                    carrier_revision=2,
                )

            def assertEqual(self, left: object, right: object) -> None:
                if left != right:
                    raise AssertionError((left, right))

        adapter = Adapter()
        manager = object.__new__(manager_type)
        manager._active_calls = {}
        manager._call_records = {}
        manager._carrier_event_adapter = adapter
        history_updates: list[dict[str, object]] = []
        telephony = types.ModuleType("telephony")
        history = types.ModuleType("telephony.call_history")
        history.update_call_history_billing_fields = lambda *args, **kwargs: history_updates.append(
            {"args": args, **kwargs}
        ) or True

        with patch.dict(sys.modules, {"telephony": telephony, "telephony.call_history": history}):
            handled = asyncio.run(
                manager.handle_call_cost(
                    "control-synthetic",
                    billed_duration_seconds=48,
                    total_cost_usd=0.08,
                    occurred_at=datetime.now(tz=UTC),
                )
            )

        self.assertTrue(handled)
        event, expected_identity = adapter.events[0]
        self.assertEqual(event.call_control_id, "control-synthetic")
        self.assertEqual(event.kind, "cost")
        self.assertEqual(expected_identity, identity)
        self.assertEqual(history_updates[0]["args"], ("call-synthetic", "owner-a"))
        self.assertEqual(history_updates[0]["carrier_revision"], 2)

    def test_mismatch_or_adapter_failure_never_writes_public_history(self) -> None:
        ns = _carrier_boundary_namespace()
        identity_type = ns["CarrierCallIdentity"]
        projection_type = ns["CarrierEventProjection"]
        record_type = ns["CallRecord"]
        status_type = ns["CallStatus"]
        manager_type = ns["CallManager"]
        identity = identity_type("owner-a", "call-synthetic", "control-synthetic")
        record = record_type(
            call_id=identity.call_id,
            phone_number="",
            task="",
            caller_name="",
            user_id=identity.user_id,
            telnyx_call_control_id=identity.call_control_id,
            status=status_type.ACTIVE,
        )

        class MismatchedAdapter:
            async def apply_event(self, _event: object, _expected_identity: object):
                return projection_type(
                    identity=identity_type("owner-b", "call-synthetic", "control-synthetic"),
                    status="completed",
                    carrier_revision=1,
                )

        manager = object.__new__(manager_type)
        manager._active_calls = {record.call_id: record}
        manager._call_records = {}
        manager._carrier_event_adapter = MismatchedAdapter()
        history_updates: list[object] = []
        telephony = types.ModuleType("telephony")
        history = types.ModuleType("telephony.call_history")
        history.update_call_history_billing_fields = lambda *_a, **_k: history_updates.append(True)

        with patch.dict(sys.modules, {"telephony": telephony, "telephony.call_history": history}):
            self.assertFalse(asyncio.run(manager.handle_call_answered(identity.call_control_id)))

        self.assertEqual(history_updates, [])

    def test_restore_cannot_replace_an_existing_differently_bound_call_id(self) -> None:
        ns = _carrier_boundary_namespace()
        identity_type = ns["CarrierCallIdentity"]
        projection_type = ns["CarrierEventProjection"]
        record_type = ns["CallRecord"]
        status_type = ns["CallStatus"]
        manager_type = ns["CallManager"]
        existing = record_type(
            call_id="call-synthetic",
            phone_number="",
            task="",
            caller_name="",
            user_id="owner-a",
            telnyx_call_control_id="control-a",
            status=status_type.ACTIVE,
        )
        projection_identity = identity_type("owner-b", "call-synthetic", "control-b")

        class Adapter:
            async def resolve_call(self, _call_control_id: str):
                return projection_type(identity=projection_identity, status="completed", carrier_revision=1)

        manager = object.__new__(manager_type)
        manager._active_calls = {existing.call_id: existing}
        manager._call_records = {}
        manager._carrier_event_adapter = Adapter()

        self.assertIsNone(asyncio.run(manager._resolve_carrier_event_record("control-b")))
        self.assertIs(manager._active_calls[existing.call_id], existing)
        self.assertEqual(manager._call_records, {})


if __name__ == "__main__":
    unittest.main()
