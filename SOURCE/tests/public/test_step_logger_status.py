"""Live step verdicts from the real middleware, with inert persistence and IO."""

from __future__ import annotations

import asyncio
import copy
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from intent import agent_middleware
from intent.tool_types import ToolResult


class StepLoggerStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Import the production middleware and ToolResult normally. Only the
        # application/persistence/telemetry boundaries are inert substitutes.
        executor_module = types.ModuleType("intent.agent_executor")
        executor_module.AgentStepRecord = SimpleNamespace
        self.strip_image = Mock(return_value='{"ok": false, "data": {"title": "synthetic"}}')
        executor_module._strip_image_from_result = self.strip_image
        checkpoint_module = types.ModuleType("intent.task_checkpoint")
        checkpoint_module.CheckpointStep = SimpleNamespace
        self.append_step = Mock()
        checkpoint_module.append_step = self.append_step
        checkpoint_module.compress_messages = copy.deepcopy
        telemetry_module = types.ModuleType("telemetry")
        self.accumulator = Mock()
        telemetry_module.get_accumulator = Mock(return_value=self.accumulator)
        self.modules = patch.dict(
            sys.modules,
            {
                "intent.agent_executor": executor_module,
                "intent.task_checkpoint": checkpoint_module,
                "telemetry": telemetry_module,
            },
        )
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.executor = SimpleNamespace(
            _last_usage={},
            _use_native=False,
            _emit_step_jsonl=Mock(),
            _broadcast_agent_step=AsyncMock(),
        )
        self.task_log = SimpleNamespace(add_step=Mock())
        self.checkpoint = SimpleNamespace(step_index=3)
        self.context = agent_middleware.LoopContext(
            iteration=3, llm_reasoning="synthetic step", tool_use_id="synthetic-call"
        )
        self.middleware = agent_middleware.StepLoggerMiddleware(
            self.executor, self.task_log, self.checkpoint, task_id="synthetic-task"
        )
        self.addAsyncCleanup(self.middleware.drain_background_tasks)

    async def run_step(self, result, *, error=None, args=None):
        text = result.to_llm_text()
        returned = await self.middleware.process(
            self.context, "synthetic_tool", result, args or {"target": "synthetic"}, error, text, 17
        )
        self.assertEqual(returned, text)
        # The durable record remains synchronous; broadcasting/checkpointing
        # retain their existing background path.
        self.task_log.add_step.assert_called_once()
        self.executor._emit_step_jsonl.assert_called_once()
        await self.middleware.drain_background_tasks()
        self.executor._broadcast_agent_step.assert_awaited_once()
        self.append_step.assert_called_once()
        return self.executor._broadcast_agent_step.await_args.args[3]

    async def test_verified_success_is_ok(self):
        self.assertEqual(await self.run_step(ToolResult(ok=True)), "ok")

    async def test_returned_failure_with_error_text_is_error(self):
        self.assertEqual(await self.run_step(ToolResult(ok=False, error="synthetic failure")), "error")
        self.assertEqual(self.task_log.add_step.call_args.args[0].error, "synthetic failure")
        self.accumulator.increment_agent_tool.assert_called_once_with("synthetic_tool", latency_ms=17, success=False)

    async def test_returned_failure_without_error_text_is_error(self):
        self.assertEqual(await self.run_step(ToolResult(ok=False)), "error")

    async def test_structured_payload_failure_is_error(self):
        self.assertEqual(await self.run_step(ToolResult(ok=True, data={"success": False})), "error")

    async def test_explicit_error_overrides_success(self):
        self.assertEqual(await self.run_step(ToolResult(ok=True), error="synthetic exception"), "error")
        self.assertEqual(self.task_log.add_step.call_args.args[0].error, "synthetic exception")

    async def test_unverified_success_is_unknown(self):
        self.assertEqual(await self.run_step(ToolResult(ok=True, unverified=True)), "unknown")

    async def test_unverified_failure_is_unknown(self):
        self.assertEqual(await self.run_step(ToolResult(ok=False, unverified=True)), "unknown")

    async def test_unverified_failure_with_explanation_is_unknown(self):
        self.assertEqual(
            await self.run_step(ToolResult(ok=False, error="synthetic result not verified", unverified=True)), "unknown"
        )

    async def test_explicit_error_overrides_unverified(self):
        self.assertEqual(
            await self.run_step(ToolResult(ok=False, unverified=True), error="synthetic exception"), "error"
        )

    async def test_image_stripping_and_payload_snapshot_are_preserved(self):
        result = ToolResult(ok=False, data={"image_base64": "SYNTHETIC_IMAGE", "title": "synthetic"})
        args = {"target": {"name": "before"}}
        text = result.to_llm_text()
        await self.middleware.process(self.context, "synthetic_tool", result, args, None, text, 17)
        args["target"]["name"] = "after"
        await self.middleware.drain_background_tasks()
        self.strip_image.assert_called_once_with(text)
        event = self.executor._broadcast_agent_step.await_args
        self.assertEqual(event.args[3], "error")
        self.assertEqual(event.kwargs["tool_input"], {"target": {"name": "before"}})
        self.assertEqual(event.kwargs["tool_output"], self.strip_image.return_value)
        self.assertNotIn("SYNTHETIC_IMAGE", self.task_log.add_step.call_args.args[0].tool_result_summary)

    async def test_slow_broadcast_is_bounded_without_blocking_bookkeeping(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow_broadcast(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.executor._broadcast_agent_step.side_effect = slow_broadcast
        with patch.object(agent_middleware, "POST_STEP_BROADCAST_TIMEOUT_SECONDS", 0.01):
            self.assertEqual(await self.run_step(ToolResult(ok=False)), "error")
        self.assertTrue(started.is_set())
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.middleware._background_tasks)

    async def test_failed_broadcast_and_telemetry_do_not_change_result_or_checkpoint(self):
        self.executor._broadcast_agent_step.side_effect = RuntimeError("synthetic broadcast failure")
        self.accumulator.increment_agent_tool.side_effect = RuntimeError("synthetic telemetry failure")
        self.assertEqual(await self.run_step(ToolResult(ok=False)), "error")
        self.assertFalse(self.middleware._background_tasks)

    async def test_process_cancellation_still_propagates(self):
        self.executor._emit_step_jsonl.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.middleware.process(self.context, "synthetic_tool", ToolResult(ok=True), {}, None, "result", 17)
        self.executor._broadcast_agent_step.assert_not_called()
        self.append_step.assert_not_called()


if __name__ == "__main__":
    unittest.main()
