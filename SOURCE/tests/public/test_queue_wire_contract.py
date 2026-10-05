"""Bind the actual Queue route/service/serializer wire shape to frontend fixtures.

Playback, state/broadcast, provider and authentication boundaries are inert. The selected route,
QueueService, RouteToolbox and envelope/JSON serialization bodies run unchanged.
No HTTP listener, account, player or model is started.
"""

from __future__ import annotations
import ast
import asyncio
import json
import sys
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[2]

FIXTURE = ROOT / "ui/react-app/src/components/fixtures/queueWireContract.json"
TRACK = {"id": "synthetic-queue-track", "title": "Synthetic queued song"}


def load_definitions(relative, namespace):
    path = ROOT / relative
    module = types.ModuleType("_queue_wire_" + path.stem)
    module.__dict__.update(namespace)
    sys.modules[module.__name__] = module
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    nodes.extend(node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)))
    exec(
        compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(path), "exec"), module.__dict__
    )
    return module


class Router:
    def __init__(self):
        self.routes = {}

    def register(self, method, path, **_kwargs):
        def decorate(function):
            self.routes[method, path] = function
            return function

        return decorate

    def get(self, path, **kwargs):
        return self.register("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.register("POST", path, **kwargs)

    def delete(self, path, **kwargs):
        return self.register("DELETE", path, **kwargs)


async def serialize_case(operation, refused):
    sys.path.insert(0, str(ROOT))
    from contracts import api_response, fastapi_helpers
    from core import json_types
    from fastapi import Body, Depends, HTTPException, Path as ApiPath
    from fastapi.responses import JSONResponse, Response

    for module, relative in (
        (api_response, "contracts/api_response.py"),
        (fastapi_helpers, "contracts/fastapi_helpers.py"),
        (json_types, "core/json_types.py"),
    ):
        if Path(module.__file__).read_bytes() != (ROOT / relative).read_bytes():
            raise AssertionError("Imported serializer differs from the selected source: " + relative)
    SafeJSONResponse = fastapi_helpers.SafeJSONResponse
    to_json_value = json_types.to_json_value
    track = types.SimpleNamespace(model_dump=lambda: dict(TRACK))

    def player_state(*_args, **_kwargs):
        if refused and operation == "get":
            raise RuntimeError("synthetic unavailable read")
        return types.SimpleNamespace(now_playing=None, queue=[track], is_playing=False)

    service = load_definitions(
        "ui/api/services/queue_service.py",
        {
            "dataclass": dataclass,
            "Protocol": Protocol,
            "cast": cast,
            "JsonDict": dict,
            "to_json_value": to_json_value,
            "log": Mock(),
            "ConfigurationError": type("ConfigurationError", (Exception,), {}),
            "InvalidOperation": type("InvalidOperation", (Exception,), {}),
            "_to_player_state": player_state,
            "is_youtube_track_requiring_provider": lambda _item: False,
        },
    )
    service.QueueService._broadcast_state = AsyncMock(return_value=None)
    music = types.SimpleNamespace(clear_queue=Mock(), play_item_now=Mock(), remove_from_queue=Mock())
    if refused:
        if operation == "clear":
            del music.clear_queue
        elif operation == "play":
            music.play_item_now.side_effect = ValueError("synthetic missing item")
        elif operation == "remove":
            music.remove_from_queue.side_effect = ValueError("synthetic missing item")
    context = types.SimpleNamespace(
        router=Router(),
        bindings=types.SimpleNamespace(music=music, state=None),
        hub=types.SimpleNamespace(),
        app=None,
        http_requests_total=Mock(),
    )
    common = load_definitions(
        "ui/api/routes/common.py",
        {
            "dataclass": dataclass,
            "json": json,
            "JSONResponse": JSONResponse,
            "Response": Response,
            "SafeJSONResponse": SafeJSONResponse,
            "HTTPException": HTTPException,
            "log": Mock(),
            **{
                name: getattr(api_response, name)
                for name in (
                    "ResponseContractError",
                    "ensure_envelope",
                    "failure_response",
                    "reconcile_status",
                    "status_for_envelope",
                    "success_response",
                )
            },
        },
    )
    routes = load_definitions(
        "ui/api/routes/queue.py",
        {
            "Any": Any,
            "Body": Body,
            "Depends": Depends,
            "Path": ApiPath,
            "JSONResponse": JSONResponse,
            "failure_response": api_response.failure_response,
            "QueueService": service.QueueService,
            "QueueServiceError": service.QueueServiceError,
            "require_auth": lambda: None,
            "require_operator_auth": lambda: None,
            "log": Mock(),
        },
    )
    routes.register_queue_routes(context, common.RouteToolbox(context))
    method, path, args = {
        "get": ("GET", "/v1/queue", ()),
        "clear": ("POST", "/v1/queue/clear", ()),
        "play": ("POST", "/v1/queue/play", ({"item_id": TRACK["id"]},)),
        "remove": ("DELETE", "/v1/queue/item/{item_id}", (TRACK["id"],)),
    }[operation]
    response = await context.router.routes[method, path](*args)
    return {"status": response.status_code, "body": json.loads(response.body.decode("utf-8"))}


async def all_cases():
    return {
        operation + ("_refused" if refused else "_success"): await serialize_case(operation, refused)
        for operation in ("get", "clear", "play", "remove")
        for refused in (False, True)
    }


class QueueWireContract(unittest.IsolatedAsyncioTestCase):
    async def verify_operation(self, operation):
        expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for refused in (False, True):
            with self.subTest(refused=refused):
                actual = await serialize_case(operation, refused)
                self.assertEqual(actual, expected[operation + ("_refused" if refused else "_success")])
                self.assertEqual(actual["body"]["ok"], not refused)
                self.assertEqual(actual["status"] < 400, not refused)
                if not refused:
                    self.assertNotIn("ok", actual["body"]["data"])

    async def test_get_wire(self):
        await self.verify_operation("get")

    async def test_clear_wire(self):
        await self.verify_operation("clear")

    async def test_play_wire(self):
        await self.verify_operation("play")

    async def test_remove_wire(self):
        await self.verify_operation("remove")


if __name__ == "__main__":
    if sys.argv[1:] == ["--write-fixture"]:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(asyncio.run(all_cases()), indent=2) + "\n", encoding="utf-8")
    else:
        unittest.main()
