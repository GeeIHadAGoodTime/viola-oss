"""Request-schema execution checks with an in-memory synthetic tool only."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from intent.tool_types import ToolResult


class RequestToolSurfaceBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def run_synthetic_batch(self, exposed):
        from intent.agent_executor import AgentExecutor

        executor = object.__new__(AgentExecutor)
        discovered = [{"name": "synthetic_counter", "parameters": {"type": "object"}}]
        executor._mcp_hub = SimpleNamespace(list_tools=lambda: discovered)
        # Use the production request-surface resolver. The constructor keeps
        # None distinct from an explicit list, including an empty list.
        executor._native_tools = exposed
        executor._depth = 0
        executor._llm = SimpleNamespace(_native_tools=None)
        executor._get_runtime_tool_surface = lambda: None
        effects = []

        async def synthetic_dispatch(name, args, **_kwargs):
            effects.append((name, args))
            return ToolResult(ok=True, data={"synthetic": True})

        executor._execute_tool = synthetic_dispatch
        with patch("intent.agent_executor._dispatch_hook"):
            result = await executor._execute_parallel_tool_batch(
                [{"tool": "synthetic_counter", "args": {}}],
                spin_detector=SimpleNamespace(is_call_blocked=lambda *_args, **_kwargs: None),
                taint_tracker=SimpleNamespace(check_tool=lambda _name: None),
                page_url=None,
            )
        return next(iter(result.values()))["tool_result"], effects

    async def test_discovered_tool_is_blocked_when_request_exposes_no_tools(self):
        result, effects = await self.run_synthetic_batch([])
        self.assertFalse(result.ok)
        self.assertIn("does not exist", result.error)
        self.assertEqual(effects, [])

    async def test_discovered_tool_is_blocked_when_another_tool_is_exposed(self):
        result, effects = await self.run_synthetic_batch([{"name": "different_tool"}])
        self.assertFalse(result.ok)
        self.assertEqual(effects, [])

    async def test_exposed_tool_executes_once(self):
        result, effects = await self.run_synthetic_batch([{"name": "synthetic_counter"}])
        self.assertTrue(result.ok)
        self.assertEqual(effects, [("synthetic_counter", {})])

    async def test_unspecified_surface_preserves_legacy_discovery_fallback(self):
        result, effects = await self.run_synthetic_batch(None)
        self.assertTrue(result.ok)
        self.assertEqual(effects, [("synthetic_counter", {})])


if __name__ == "__main__":
    unittest.main()
