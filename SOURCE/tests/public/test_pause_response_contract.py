"""Exercise the shipped pause endpoint with inert audio adapters.

No server, device or network is started. The real endpoint body is executed;
only its application/audio boundaries and response envelopes are supplied.
"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[2]


class PauseResponseContract(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, primary_error=None, fallback_error=None, primary_result=None, *, real_player=None):
        source = ROOT / "ui/api/routes/control.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        endpoint = next(
            node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "post_pause"
        )
        endpoint.decorator_list = []
        endpoint.args.defaults = [ast.Constant(None)]
        isolated = ast.Module(
            body=[
                ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                endpoint,
            ],
            type_ignores=[],
        )
        pause = Mock(side_effect=primary_error, return_value=primary_result)
        fallback = Mock(side_effect=fallback_error)
        state = SimpleNamespace(is_playing=True)
        canonical = Mock()
        broadcast = AsyncMock()
        snapshot = AsyncMock(return_value=SimpleNamespace(model_dump=lambda: {"is_playing": True}))

        async def record(callback, **kwargs):
            return await callback()

        namespace = {
            "asyncio": asyncio,
            "log": logging.getLogger("pause-response-contract"),
            "music": SimpleNamespace(pause=pause, player=real_player),
            "_direct_backend_pause": fallback,
            "state": state,
            "app": SimpleNamespace(
                state=SimpleNamespace(hub_state_authority=SimpleNamespace(update_canonical_playback_state=canonical))
            ),
            "hub": SimpleNamespace(broadcast=broadcast),
            "_safe_state_adapter": snapshot,
            "toolbox": SimpleNamespace(record_and_call=record),
            "_adapter_failure_error": lambda result, **kwargs: (
                result.get("error") if isinstance(result, dict) and result.get("ok") is False else None
            ),
            "_control_error_response": lambda status, code, message: {"ok": False, "status": status, "error": code},
            "JSONResponse": lambda status_code, content: {**content, "status": status_code},
        }
        if real_player is not None:
            helper = next(
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == "_direct_backend_pause"
            )
            isolated.body.insert(1, helper)
            namespace["PlaybackPhase"] = SimpleNamespace(PAUSED="paused")
            namespace["_sm_transition"] = Mock()
        exec(compile(ast.fix_missing_locations(isolated), str(source), "exec"), namespace)
        result = await namespace["post_pause"](user_id="synthetic-listener")
        return SimpleNamespace(
            result=result,
            pause=pause,
            fallback=fallback,
            state=state,
            canonical=canonical,
            broadcast=broadcast,
            snapshot=snapshot,
        )

    async def test_success_updates_state_and_broadcast_once(self):
        run = await self.exercise()
        self.assertTrue(run.result["ok"])
        self.assertFalse(run.state.is_playing)
        run.fallback.assert_not_called()
        run.canonical.assert_called_once_with(False)
        run.broadcast.assert_awaited_once()

    async def test_timeout_fallback_success_is_reported_as_paused(self):
        run = await self.exercise(primary_error=TimeoutError("synthetic timeout"))
        self.assertTrue(run.result["ok"])
        self.assertFalse(run.state.is_playing)
        run.fallback.assert_called_once()
        run.canonical.assert_called_once_with(False)

    async def test_failed_timeout_fallback_never_claims_paused(self):
        for fallback_error in [RuntimeError("device unavailable"), TimeoutError("fallback timeout")]:
            with self.subTest(error=type(fallback_error).__name__):
                run = await self.exercise(
                    primary_error=TimeoutError("synthetic timeout"), fallback_error=fallback_error
                )
                self.assertFalse(run.result["ok"])
                self.assertEqual(run.result["status"], 500)
                self.assertEqual(run.result["error"], "pause_failed")
                self.assertTrue(run.state.is_playing)
                run.canonical.assert_not_called()
                run.broadcast.assert_not_awaited()
                run.snapshot.assert_not_awaited()

    async def test_failed_exception_fallback_preserves_real_state(self):
        run = await self.exercise(
            primary_error=RuntimeError("primary refused"), fallback_error=RuntimeError("fallback refused")
        )
        self.assertFalse(run.result["ok"])
        self.assertTrue(run.state.is_playing)
        run.canonical.assert_not_called()
        run.broadcast.assert_not_awaited()

    async def test_adapter_rejection_is_not_promoted_to_pause_success(self):
        run = await self.exercise(primary_result={"ok": False, "error": "pause_refused"})
        self.assertFalse(run.result["ok"])
        self.assertTrue(run.state.is_playing)
        run.fallback.assert_not_called()
        run.canonical.assert_not_called()
        run.broadcast.assert_not_awaited()

    async def test_real_fallback_device_error_reaches_endpoint_without_false_pause(self):
        backend = SimpleNamespace(pause=Mock(side_effect=RuntimeError("synthetic device failure")))
        player = SimpleNamespace(
            _backend=backend,
            _is_playing=True,
            _paused=False,
            _user_paused=False,
            _state=SimpleNamespace(is_playing=True),
        )
        run = await self.exercise(primary_error=TimeoutError("synthetic adapter timeout"), real_player=player)
        self.assertFalse(run.result["ok"])
        self.assertEqual(run.result["status"], 500)
        self.assertTrue(player._is_playing)
        self.assertFalse(player._paused)
        self.assertFalse(player._user_paused)
        self.assertTrue(player._state.is_playing)
        self.assertTrue(run.state.is_playing)
        run.canonical.assert_not_called()
        run.broadcast.assert_not_awaited()
        backend.pause.assert_called_once()


class DirectPauseFallbackContract(unittest.TestCase):
    def fallback(self, player):
        source = ROOT / "ui/api/routes/control.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        helper = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_direct_backend_pause"
        )
        transition = Mock()
        namespace = {
            "music": SimpleNamespace(player=player),
            "log": logging.getLogger("direct-pause-contract"),
            "PlaybackPhase": SimpleNamespace(PAUSED="paused"),
            "_sm_transition": transition,
        }
        exec(
            compile(ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])), str(source), "exec"),
            namespace,
        )
        return namespace["_direct_backend_pause"], transition

    def player(self, backend):
        return SimpleNamespace(
            _backend=backend,
            _is_playing=True,
            _paused=False,
            _user_paused=False,
            _state=SimpleNamespace(is_playing=True),
        )

    def test_failed_backend_does_not_change_any_flags_or_state_machine(self):
        player = self.player(SimpleNamespace(pause=Mock(side_effect=RuntimeError("synthetic failure"))))
        pause, transition = self.fallback(player)
        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            pause()
        self.assertTrue(player._is_playing)
        self.assertFalse(player._paused)
        self.assertFalse(player._user_paused)
        self.assertTrue(player._state.is_playing)
        transition.assert_not_called()

    def test_absent_player_or_pause_capability_is_not_success(self):
        for player in [None, self.player(None), self.player(SimpleNamespace())]:
            with self.subTest(player=player):
                pause, transition = self.fallback(player)
                with self.assertRaises(RuntimeError):
                    pause()
                transition.assert_not_called()

    def test_successful_legacy_or_managed_backend_updates_flags_once(self):
        for managed in [False, True]:
            with self.subTest(managed=managed):
                backend = SimpleNamespace(pause=Mock())
                player = self.player(backend)
                if managed:
                    player._backend = None
                    player._backend_manager = SimpleNamespace(backend=backend)
                pause, transition = self.fallback(player)
                pause()
                backend.pause.assert_called_once()
                self.assertFalse(player._is_playing)
                self.assertTrue(player._paused)
                self.assertTrue(player._user_paused)
                self.assertFalse(player._state.is_playing)
                transition.assert_called_once_with(player, "paused", user_initiated=True)


if __name__ == "__main__":
    unittest.main()
