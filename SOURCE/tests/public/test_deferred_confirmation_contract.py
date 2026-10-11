"""Real executor approval/failure controls for public unittest discovery."""

from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

agent_executor_module = None
AgentExecutor = None
ApprovalManager = None
ToolCall = None
ToolResult = None
_calendar_add_failure_user_message = None


def setUpModule():
    """Keep incidental app initialization in a disposable synthetic profile."""
    global agent_executor_module, AgentExecutor, ApprovalManager, ToolCall, ToolResult
    global _calendar_add_failure_user_message
    profile = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory(prefix="viola-calendar-confirmation-")))
    unittest.enterModuleContext(
        patch.dict(
            os.environ,
            {
                "VIOLA_DATA_DIR": str(profile / "data"),
                "VIOLA_CACHE_DIR": str(profile / "cache"),
                "VIOLA_LOG_DIR": str(profile / "logs"),
                "VIOLA_AI_SOURCE_OVERRIDE": "local",
            },
        )
    )
    unittest.enterModuleContext(patch.object(Path, "home", return_value=profile / "home"))
    unittest.addModuleCleanup(_close_profile_handlers, profile)
    import intent.agent_executor as executor_module
    from intent.approval import ApprovalManager as Manager
    from intent.tool_types import ToolCall as Call, ToolResult as Result

    agent_executor_module = executor_module
    AgentExecutor = executor_module.AgentExecutor
    _calendar_add_failure_user_message = executor_module._calendar_add_failure_user_message
    ApprovalManager, ToolCall, ToolResult = Manager, Call, Result


def _close_profile_handlers(profile):
    profile = profile.resolve()
    loggers = [
        logging.getLogger(),
        *[logger for logger in logging.Logger.manager.loggerDict.values() if isinstance(logger, logging.Logger)],
    ]
    for logger in loggers:
        for handler in list(logger.handlers):
            filename = getattr(handler, "baseFilename", None)
            if filename and Path(filename).resolve().is_relative_to(profile):
                logger.removeHandler(handler)
                handler.close()


class CalendarConfirmationContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from config.settings import settings
        from services.idempotency import IdempotencyStore

        # This is a synthetic local-provider task, independent of private billing.
        self.enterContext(patch.object(settings, "ai_source_override", "local"))
        store = IdempotencyStore()
        self.enterContext(patch.object(agent_executor_module, "get_idempotency_store", lambda: store))

    def test_calendar_add_parse_failure_surfaces_error_not_done(self) -> None:
        message = _calendar_add_failure_user_message(
            "calendar",
            {"action": "add", "title": "Dentist", "start_time": "bad-date"},
            ToolResult(ok=False, error="Could not parse start_time. Use ISO-8601 format."),
        )

        assert message is not None
        assert "couldn't add the event" in message.lower()
        assert "start time format" in message.lower()
        assert "tomorrow at 3pm" in message
        assert "done" not in message.lower()
        assert "queued" not in message.lower()

    def _calendar_executor(self, approval=None):
        provider = MagicMock()
        provider.native_tools_supported = True
        provider.route_command_native = AsyncMock(return_value={"type": "answer", "answer": "Done."})
        provider.config = SimpleNamespace(model="gpt-5.4-mini")
        provider.ensure_servers_ready = AsyncMock()
        provider.update_tool_filters = AsyncMock()
        provider.effective_model = "gpt-5.4-mini"
        provider.mcp_server_status = {"core": True}
        provider.sdk_requires_mcp_preconnect = True

        hub = MagicMock()
        hub._tool_routes = {"calendar": object()}
        hub._tool_schemas = {"calendar": {"name": "calendar"}}
        hub.call_tool = AsyncMock(return_value={"success": False, "error": "Could not parse start_time."})
        hub.get_tool_schemas = MagicMock(return_value="")
        hub.list_tools = MagicMock(return_value=[{"name": "calendar"}])
        hub.find_required_tier = MagicMock(return_value=None)
        hub.shutdown = AsyncMock()
        hub.last_approval_path = None

        self.enterContext(patch.object(agent_executor_module, "save_checkpoint", lambda *args, **kwargs: None))
        self.enterContext(patch.object(agent_executor_module, "append_step", lambda *args, **kwargs: None))
        self.enterContext(patch.object(agent_executor_module, "_dispatch_hook", lambda *args, **kwargs: None))

        executor = AgentExecutor(
            llm_caller=provider,
            approval_manager=approval or ApprovalManager(),
            mcp_hub=hub,
            native_tools=[{"name": "calendar"}],
            user_id="test-user",
        )
        executor._finalize_log = MagicMock()

        return executor, provider, hub

    def _calendar_call(self):
        return ToolCall(
            tool="calendar",
            args={"action": "add", "title": "Dentist", "start_time": "bad-date"},
            tool_use_id="call-calendar-1",
            raw_content=[
                {
                    "type": "tool_use",
                    "id": "call-calendar-1",
                    "name": "calendar",
                    "input": {"action": "add", "title": "Dentist", "start_time": "bad-date"},
                }
            ],
        )

    def _approval_channel(self, decision="strong_affirm"):
        channel = SimpleNamespace(
            supports_buttons=False, ask=AsyncMock(return_value="Yes, I am sure"), send=AsyncMock()
        )
        approval = ApprovalManager(channel=channel)
        self.enterContext(patch.object(approval, "_classify_response", AsyncMock(return_value=decision)))
        return approval, channel

    async def test_calendar_tool_failure_terminates_agent_without_done(self) -> None:
        approval, channel = self._approval_channel()
        executor, provider, hub = self._calendar_executor(approval)
        result = await executor.run("Add dentist tomorrow", self._calendar_call())

        hub.call_tool.assert_awaited_once()
        channel.ask.assert_awaited_once()
        assert result.error == "Could not parse start_time."
        assert result.ok is False
        assert "couldn't add the event" in result.answer.lower()
        assert "done" not in result.answer.lower()
        assert "queued" not in result.answer.lower()
        provider.route_command_native.assert_not_awaited()

    async def _pending_confirmation_cannot_become_done(self, deferred_channel) -> None:
        channel = (
            SimpleNamespace(
                supports_buttons=False,
                defer_confirmation_on_ask_none=True,
                ask=AsyncMock(return_value=None),
                send=AsyncMock(),
            )
            if deferred_channel
            else None
        )
        executor, provider, hub = self._calendar_executor(ApprovalManager(channel=channel))
        result = await executor.run("Add dentist tomorrow", self._calendar_call())

        assert result.ok is False
        assert result.error == "confirmation_required"
        assert "confirm" in result.answer.lower()
        assert "Dentist" in result.answer
        assert "done" not in result.answer.lower()
        assert "queued" not in result.answer.lower()
        assert result.continue_listening is True
        assert result.params["confirmation_required"] is True
        assert result.params["tool_args"] == self._calendar_call().args
        assert result.params["confirmation_id"]
        hub.call_tool.assert_not_awaited()
        provider.route_command_native.assert_not_awaited()

    async def test_stop_hook_cannot_reopen_calendar_confirmation(self) -> None:
        from intent.hooks.lifecycle import HookRegistry
        from intent.hooks.schema import HookResult

        executor, provider, hub = self._calendar_executor()
        registry = HookRegistry(default_user_id="test-user")
        stop = MagicMock(return_value=HookResult(decision="deny", reason="Keep working"))
        registry.register_hook("Stop", stop)
        executor._hook_registry = registry
        result = await executor.run("Add dentist tomorrow", self._calendar_call())

        assert result.ok is False
        assert result.error == "confirmation_required"
        assert "confirm" in result.answer.lower()
        assert "done" not in result.answer.lower()
        stop.assert_called_once()
        hub.call_tool.assert_not_awaited()
        provider.route_command_native.assert_not_awaited()

    async def test_calendar_denied_confirmation_does_not_call_tool(self) -> None:
        approval, channel = self._approval_channel(decision="deny")
        executor, provider, hub = self._calendar_executor(approval)
        result = await executor.run("Add dentist tomorrow", self._calendar_call())

        assert result.ok is False
        assert "not confirmed" in result.error
        assert "done" not in result.answer.lower()
        channel.ask.assert_awaited_once()
        hub.call_tool.assert_not_awaited()
        provider.route_command_native.assert_not_awaited()

    async def test_calendar_success_after_confirmation_can_finish(self) -> None:
        from services.idempotency import IdempotencyStore

        store = IdempotencyStore()
        self.enterContext(patch.object(agent_executor_module, "get_idempotency_store", lambda: store))
        approval, channel = self._approval_channel()
        executor, provider, hub = self._calendar_executor(approval)
        hub.call_tool.return_value = {"success": True, "data": {"event_id": "synthetic-event"}}
        result = await executor.run("Add dentist tomorrow", self._calendar_call())

        assert result.ok is True
        assert result.error is None
        assert result.answer == "Done."
        channel.ask.assert_awaited_once()
        hub.call_tool.assert_awaited_once()
        provider.route_command_native.assert_awaited_once()

    async def test_calendar_deferred_confirmation_requires_a_later_task(self) -> None:
        from services.idempotency import IdempotencyStore

        store = IdempotencyStore()
        self.enterContext(patch.object(agent_executor_module, "get_idempotency_store", lambda: store))
        approval = ApprovalManager()
        executor, provider, hub = self._calendar_executor(approval)
        pending = await executor.run("Add dentist tomorrow", self._calendar_call())
        assert pending.ok is False
        assert pending.error == "confirmation_required"
        original_task = executor.task_id
        confirmation_args = dict(self._calendar_call().args, **pending.params["confirmation_args"])

        same_task = await executor._execute_tool("calendar", dict(confirmation_args))
        assert same_task.ok is False
        hub.call_tool.assert_not_awaited()
        provider.route_command_native.assert_not_awaited()

        next_executor, next_provider, next_hub = self._calendar_executor(approval)
        next_hub.call_tool.return_value = {"success": True, "data": {"event_id": "synthetic-event"}}
        next_call = self._calendar_call()
        next_call.args.update(pending.params["confirmation_args"])
        result = await next_executor.run("Yes, I confirm", next_call)

        assert next_executor.task_id != original_task
        assert result.ok is True
        assert result.answer == "Done."
        next_hub.call_tool.assert_awaited_once()
        next_provider.route_command_native.assert_awaited_once()

    async def test_pending_confirmation_cannot_become_done(self):
        for deferred_channel in (False, True):
            with self.subTest(deferred_channel=deferred_channel):
                await self._pending_confirmation_cannot_become_done(deferred_channel)


if __name__ == "__main__":
    unittest.main()
