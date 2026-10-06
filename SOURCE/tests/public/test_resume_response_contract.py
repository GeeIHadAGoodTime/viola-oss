"""Run the real Resume handler/helper/serializer with inert playback boundaries."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[2]


def definitions(relative, names, namespace):
    source = ROOT / relative
    tree = ast.parse(source.read_text(encoding="utf-8"))
    selected = [
        next(
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name
        )
        for name in names
    ]
    for node in selected:
        if node.name == "post_resume":
            node.decorator_list = []
            node.args.defaults = [ast.Constant(None)]
    module = types.ModuleType("_resume_contract_" + source.stem)
    sys.modules[module.__name__] = module
    module.__dict__.update(namespace)
    compiled = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(compiled), str(source), "exec"), module.__dict__)
    return module


async def exercise(
    primary_error=None,
    backend_error=None,
    *,
    capability=True,
    missing_player=False,
    fallback_override=None,
    primary_result=None,
    managed=False,
    has_track=True,
    already_playing=False,
):
    sys.path.insert(0, str(ROOT))
    from contracts import api_response, fastapi_helpers
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse, Response

    for module, relative in (
        (api_response, "contracts/api_response.py"),
        (fastapi_helpers, "contracts/fastapi_helpers.py"),
    ):
        if Path(module.__file__).read_bytes() != (ROOT / relative).read_bytes():
            raise AssertionError("Serializer differs from selected source: " + relative)
    toolbox = definitions(
        "ui/api/routes/common.py",
        ["RouteToolbox"],
        {
            "dataclass": dataclass,
            "json": json,
            "JSONResponse": JSONResponse,
            "Response": Response,
            "SafeJSONResponse": fastapi_helpers.SafeJSONResponse,
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
    backend = types.SimpleNamespace(resume=Mock(side_effect=backend_error)) if capability else types.SimpleNamespace()
    player = types.SimpleNamespace(
        _backend=backend,
        _is_playing=False,
        _paused=True,
        _user_paused=True,
        _state=types.SimpleNamespace(is_playing=False),
    )
    if managed:
        player._backend = None
        player._backend_manager = types.SimpleNamespace(backend=backend)
    music = types.SimpleNamespace(
        resume=Mock(side_effect=primary_error, return_value=primary_result), player=None if missing_player else player
    )
    state = types.SimpleNamespace(
        now_playing={"id": "synthetic-resume-track"} if has_track else None, is_playing=already_playing
    )
    snapshot = AsyncMock(
        side_effect=lambda *_args: types.SimpleNamespace(
            now_playing=state.now_playing,
            queue=[],
            is_playing=state.is_playing,
            model_dump=lambda: {"is_playing": state.is_playing},
        )
    )
    canonical = Mock()
    broadcast = AsyncMock()
    transition = Mock()
    rescan = Mock()
    namespace = {
        "asyncio": asyncio,
        "log": Mock(),
        "music": music,
        "state": state,
        "app": types.SimpleNamespace(
            state=types.SimpleNamespace(
                hub_state_authority=types.SimpleNamespace(update_canonical_playback_state=canonical)
            )
        ),
        "hub": types.SimpleNamespace(broadcast=broadcast),
        "_safe_state_adapter": snapshot,
        "toolbox": toolbox.RouteToolbox(types.SimpleNamespace(http_requests_total=Mock())),
        "PlaybackPhase": types.SimpleNamespace(PLAYING="playing"),
        "_sm_transition": transition,
        "JSONResponse": JSONResponse,
        "failure_response": api_response.failure_response,
    }
    route = definitions(
        "ui/api/routes/control.py",
        ["_control_error_response", "_adapter_failure_error", "_direct_backend_resume", "post_resume"],
        namespace,
    )
    if fallback_override is not None:
        route._direct_backend_resume = fallback_override
    audio = types.ModuleType("audio_core")
    audio.__path__ = []
    streaming = types.ModuleType("audio_core.streaming")
    streaming.__path__ = []
    wiring = types.ModuleType("audio_core.streaming.pipeline_wiring")
    wiring.request_proctap_rescan = rescan
    with patch.dict(
        sys.modules,
        {"audio_core": audio, "audio_core.streaming": streaming, "audio_core.streaming.pipeline_wiring": wiring},
    ):
        response = await route.post_resume(user_id="synthetic-listener")
    return types.SimpleNamespace(
        status=response.status_code,
        body=json.loads(response.body.decode("utf-8")),
        player=player,
        state=state,
        music=music,
        backend=backend,
        canonical=canonical,
        broadcast=broadcast,
        transition=transition,
        rescan=rescan,
        snapshot=snapshot,
    )


class ResumeResponseContract(unittest.IsolatedAsyncioTestCase):
    def assert_refusal(self, run):
        self.assertEqual(run.status, 500)
        self.assertFalse(run.body["ok"])
        self.assertEqual(run.body["error"]["code"], "resume_failed")
        self.assertFalse(run.state.is_playing)
        self.assertFalse(run.player._is_playing)
        self.assertTrue(run.player._paused)
        self.assertTrue(run.player._user_paused)
        self.assertFalse(run.player._state.is_playing)
        run.canonical.assert_not_called()
        run.broadcast.assert_not_awaited()
        run.transition.assert_not_called()
        run.rescan.assert_not_called()
        self.assertEqual(run.snapshot.await_count, 1)

    async def test_primary_success_preserves_real_serialized_acknowledgement(self):
        run = await exercise()
        self.assertEqual(run.status, 200)
        self.assertEqual(run.body, {"ok": True, "error": None, "data": {}})
        self.assertTrue(run.state.is_playing)
        run.backend.resume.assert_not_called()
        run.canonical.assert_called_once_with(True)
        run.broadcast.assert_awaited_once()
        run.rescan.assert_called_once()

    async def test_successful_timeout_fallback_updates_state_once(self):
        run = await exercise(primary_error=TimeoutError("synthetic primary deadline"))
        self.assertEqual(run.status, 200)
        self.assertTrue(run.body["ok"])
        self.assertTrue(run.player._is_playing)
        self.assertFalse(run.player._paused)
        run.backend.resume.assert_called_once()
        run.canonical.assert_called_once_with(True)
        run.transition.assert_called_once_with(run.player, "playing")
        run.broadcast.assert_awaited_once()

    async def test_real_device_refusal_after_timeout_never_claims_playing(self):
        run = await exercise(
            primary_error=TimeoutError("synthetic primary deadline"),
            backend_error=RuntimeError("synthetic device refusal"),
        )
        self.assert_refusal(run)
        run.backend.resume.assert_called_once()

    async def test_real_device_refusal_after_primary_exception_never_claims_playing(self):
        run = await exercise(
            primary_error=RuntimeError("synthetic adapter failure"),
            backend_error=RuntimeError("synthetic device refusal"),
        )
        self.assert_refusal(run)

    async def test_missing_player_is_not_success(self):
        self.assert_refusal(
            await exercise(primary_error=TimeoutError("synthetic primary deadline"), missing_player=True)
        )

    async def test_missing_resume_capability_is_not_success(self):
        self.assert_refusal(await exercise(primary_error=TimeoutError("synthetic primary deadline"), capability=False))

    async def test_timeout_fallback_exception_preserves_state_and_serializes_failure(self):
        self.assert_refusal(
            await exercise(
                primary_error=TimeoutError("synthetic primary deadline"),
                fallback_override=Mock(side_effect=RuntimeError("synthetic fallback failure")),
            )
        )

    async def test_timeout_fallback_deadline_preserves_state_and_serializes_failure(self):
        self.assert_refusal(
            await exercise(
                primary_error=TimeoutError("synthetic primary deadline"),
                fallback_override=Mock(side_effect=TimeoutError("synthetic fallback deadline")),
            )
        )

    async def test_managed_backend_refusal_does_not_publish_playing(self):
        self.assert_refusal(
            await exercise(
                primary_error=TimeoutError("synthetic primary deadline"),
                backend_error=RuntimeError("synthetic managed failure"),
                managed=True,
            )
        )

    async def test_managed_backend_success_preserves_normal_resume(self):
        run = await exercise(primary_error=RuntimeError("synthetic adapter failure"), managed=True)
        self.assertEqual(run.status, 200)
        self.assertTrue(run.body["ok"])
        self.assertTrue(run.player._is_playing)
        self.assertFalse(run.player._paused)
        run.backend.resume.assert_called_once()
        run.canonical.assert_called_once_with(True)
        run.transition.assert_called_once_with(run.player, "playing")
        run.broadcast.assert_awaited_once()

    async def test_nothing_to_resume_guard_is_preserved(self):
        run = await exercise(has_track=False)
        self.assertEqual(run.status, 409)
        self.assertFalse(run.body["ok"])
        self.assertEqual(run.body["error"]["code"], "nothing_to_resume")
        run.music.resume.assert_not_called()
        run.backend.resume.assert_not_called()
        run.canonical.assert_not_called()
        run.broadcast.assert_not_awaited()
        run.rescan.assert_not_called()

    async def test_already_playing_guard_does_not_repeat_resume(self):
        run = await exercise(already_playing=True)
        self.assertEqual(run.status, 200)
        self.assertTrue(run.body["ok"])
        run.music.resume.assert_not_called()
        run.backend.resume.assert_not_called()
        run.canonical.assert_not_called()
        run.broadcast.assert_not_awaited()
        run.rescan.assert_not_called()

    async def test_explicit_primary_refusal_does_not_try_fallback(self):
        run = await exercise(
            primary_result={"ok": False, "error": {"code": "resume_failed", "message": "synthetic refusal"}}
        )
        self.assert_refusal(run)
        run.backend.resume.assert_not_called()


if __name__ == "__main__":
    unittest.main()
