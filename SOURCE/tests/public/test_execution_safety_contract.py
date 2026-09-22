"""Synthetic checks for public execution boundaries.

These tests use sentinel values only.  They never start an external MCP
process, execute a shell command, or open a browser/payment page.
"""

from __future__ import annotations

import asyncio
import json
import os
import contextvars
import importlib.util
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


SOURCE_ROOT = Path(__file__).resolve().parents[2]

_TEST_PROFILE = None
_TEST_PROFILE_ROOT = None
_TEST_BOUNDARY_ROOT = None
_FAKE_HOME = None
_FAKE_DATA = None
_PROFILE_ENV = None
_HOME_LOOKUP = None
_ORIGINAL_ROOT_HANDLERS = None
_ORIGINAL_ROOT_LEVEL = None
_ORIGINAL_LOGGING_READY = False
_ORIGINAL_OBSERVABILITY_CONFIGURED = False
_ORIGINAL_OBSERVABILITY_CONFIG = None
_ORIGINAL_TEMPDIR = None


def setUpModule():
    """Put every test-time persistence boundary in a disposable profile."""

    global _FAKE_DATA
    global _FAKE_HOME
    global _HOME_LOOKUP
    global _ORIGINAL_LOGGING_READY
    global _ORIGINAL_OBSERVABILITY_CONFIG
    global _ORIGINAL_OBSERVABILITY_CONFIGURED
    global _ORIGINAL_ROOT_HANDLERS
    global _ORIGINAL_ROOT_LEVEL
    global _ORIGINAL_TEMPDIR
    global _PROFILE_ENV
    global _TEST_PROFILE
    global _TEST_BOUNDARY_ROOT
    global _TEST_PROFILE_ROOT

    root_logger = logging.getLogger()
    _ORIGINAL_ROOT_HANDLERS = list(root_logger.handlers)
    _ORIGINAL_ROOT_LEVEL = root_logger.level
    _ORIGINAL_TEMPDIR = tempfile.tempdir
    prior_logging_module = sys.modules.get("core.logging_config")
    prior_observability_module = sys.modules.get("diagnostics.observability_logging")
    _ORIGINAL_LOGGING_READY = getattr(prior_logging_module, "_LOGGING_READY", False)
    _ORIGINAL_OBSERVABILITY_CONFIGURED = getattr(prior_observability_module, "_CONFIGURED", False)
    _ORIGINAL_OBSERVABILITY_CONFIG = getattr(prior_observability_module, "_CONFIG", None)

    base_dir = Path(
        os.environ.get("VIOLA_EXECUTION_SAFETY_TEST_ROOT")
        or os.environ.get("VIOLA_DATA_DIR")
        or tempfile.gettempdir()
    )
    base_dir.mkdir(parents=True, exist_ok=True)
    _TEST_BOUNDARY_ROOT = base_dir.resolve()
    _TEST_PROFILE = tempfile.TemporaryDirectory(prefix="viola-execution-safety-", dir=base_dir)
    _TEST_PROFILE_ROOT = Path(_TEST_PROFILE.name)
    _FAKE_HOME = _TEST_PROFILE_ROOT / "home"
    _FAKE_DATA = _TEST_PROFILE_ROOT / "data"
    fake_cache = _TEST_PROFILE_ROOT / "cache"
    fake_logs = _TEST_PROFILE_ROOT / "logs"
    for directory in (_FAKE_HOME, _FAKE_DATA, fake_cache, fake_logs):
        directory.mkdir(parents=True, exist_ok=True)

    _PROFILE_ENV = patch.dict(
        os.environ,
        {
            "HOME": str(_FAKE_HOME),
            "USERPROFILE": str(_FAKE_HOME),
            "VIOLA_DATA_DIR": str(_FAKE_DATA),
            "VIOLA_CACHE_DIR": str(fake_cache),
            "VIOLA_LOG_DIR": str(fake_logs),
        },
        clear=False,
    )
    _HOME_LOOKUP = patch.object(Path, "home", return_value=_FAKE_HOME)
    _PROFILE_ENV.start()
    _HOME_LOOKUP.start()


def tearDownModule():
    """Close test-owned log files before removing the disposable profile."""

    profile_root = _TEST_PROFILE_ROOT.resolve()
    loggers = [logging.getLogger()]
    loggers.extend(
        candidate
        for candidate in logging.Logger.manager.loggerDict.values()
        if isinstance(candidate, logging.Logger)
    )
    for active_logger in loggers:
        for handler in list(active_logger.handlers):
            filename = getattr(handler, "baseFilename", None)
            if not filename:
                continue
            try:
                Path(filename).resolve().relative_to(profile_root)
            except ValueError:
                continue
            active_logger.removeHandler(handler)
            handler.close()

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    for handler in _ORIGINAL_ROOT_HANDLERS:
        root_logger.addHandler(handler)
    root_logger.setLevel(_ORIGINAL_ROOT_LEVEL)

    logging_module = sys.modules.get("core.logging_config")
    if logging_module is not None:
        logging_module._LOGGING_READY = _ORIGINAL_LOGGING_READY
    observability_module = sys.modules.get("diagnostics.observability_logging")
    if observability_module is not None:
        observability_module._CONFIGURED = _ORIGINAL_OBSERVABILITY_CONFIGURED
        observability_module._CONFIG = _ORIGINAL_OBSERVABILITY_CONFIG

    _HOME_LOOKUP.stop()
    _PROFILE_ENV.stop()
    # Runtime startup may change this process-wide cache independently of env.
    tempfile.tempdir = _ORIGINAL_TEMPDIR
    _TEST_PROFILE.cleanup()


def _load_launcher_without_package_side_effects():
    """Load the real launcher without importing mcp_hub's eager package init."""

    package_name = "_execution_safety_mcp_hub"
    package = types.ModuleType(package_name)
    package.__path__ = [str(SOURCE_ROOT / "mcp_hub")]
    sys.modules[package_name] = package

    for module_name, path in (
        (f"{package_name}.types", SOURCE_ROOT / "mcp_hub" / "types.py"),
        (f"{package_name}.launcher", SOURCE_ROOT / "mcp_hub" / "launcher.py"),
    ):
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:  # pragma: no cover - import machinery guard
            raise RuntimeError(f"Unable to load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

    return sys.modules[f"{package_name}.launcher"], sys.modules[f"{package_name}.types"]


class ExternalMCPEnvironmentContract(unittest.IsolatedAsyncioTestCase):
    async def test_launcher_passes_only_safe_parent_env_plus_explicit_config(self):
        from mcp.client import stdio as mcp_stdio

        launcher_module, types_module = _load_launcher_without_package_side_effects()
        MCPServerLauncher = launcher_module.MCPServerLauncher
        ServerConfig = types_module.ServerConfig

        captured: dict[str, object] = {}
        synthetic_session = object()

        async def own_server(_name, params, ready, stop_event):
            captured["params"] = params
            ready.set_result(synthetic_session)
            await stop_event.wait()

        async def capture_spawn_boundary(**kwargs):
            captured["child_env"] = dict(kwargs["env"])
            raise OSError("synthetic stop before process creation")

        parent_env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", "C:\\Windows"),
            "TEMP": os.environ.get("TEMP", "C:\\Temp"),
            "SYNTHETIC_PARENT_SECRET": "must-not-cross",
            "NODE_OPTIONS": "--require=synthetic-parent-payload.js",
        }
        config = ServerConfig(
            name="synthetic",
            transport="stdio",
            command="python",
            args=["-m", "synthetic_server"],
            env={"SYNTHETIC_CONFIG_CREDENTIAL": "explicit-value"},
        )
        launcher = MCPServerLauncher()

        with patch.dict(os.environ, parent_env, clear=True), patch.object(
            launcher,
            "_own_server",
            side_effect=own_server,
        ):
            session, handle = await launcher.launch(config)
            await launcher.stop(handle, session)

            # Exercise the SDK code that performs the final environment merge at
            # the subprocess boundary, but stop before any process is created.
            with patch.object(
                mcp_stdio,
                "_create_platform_compatible_process",
                side_effect=capture_spawn_boundary,
            ):
                with self.assertRaisesRegex(OSError, "synthetic stop"):
                    async with mcp_stdio.stdio_client(captured["params"]):
                        self.fail("stdio client must not start a real process")

        self.assertIs(session, synthetic_session)
        child_env = captured["child_env"]
        self.assertIsInstance(child_env, dict)
        self.assertIn("PATH", child_env)
        self.assertIn("SYSTEMROOT", child_env)
        self.assertIn("TEMP", child_env)
        self.assertEqual(child_env["SYNTHETIC_CONFIG_CREDENTIAL"], "explicit-value")
        self.assertNotIn("SYNTHETIC_PARENT_SECRET", child_env)
        self.assertNotIn("NODE_OPTIONS", child_env)
        self.assertTrue(handle.owner_task.done())

    async def test_explicit_code_injection_environment_is_rejected(self):
        launcher_module, types_module = _load_launcher_without_package_side_effects()
        launcher = launcher_module.MCPServerLauncher()
        config = types_module.ServerConfig(
            name="synthetic",
            transport="stdio",
            command="python",
            args=["-m", "synthetic_server"],
            env={"pythonpath": "synthetic-payload"},
        )

        with self.assertRaisesRegex(ValueError, "can inject code"):
            await launcher.launch(config)


class PermissionAndIrreversibilityContract(unittest.TestCase):
    @staticmethod
    def _permission_context(*, risk="dangerous", hook=None):
        from intent.permissions.policy import PermissionContext

        return PermissionContext(
            user_id="synthetic-user",
            session_id="synthetic-session",
            tool_name="synthetic_tool",
            tool_input={},
            risk_level=risk,
            hook_provenance=(() if hook is None else (hook,)),
        )

    def test_configured_deny_and_ask_rules_survive_hook_allow(self):
        from intent.permissions.policy import PermissionHookProvenance, PermissionPolicy, PermissionRule

        hook_allow = PermissionHookProvenance(event="PreToolUse", decision="allow")
        deny = PermissionPolicy(rules=(PermissionRule("synthetic_tool", "deny", "settings"),))
        ask = PermissionPolicy(rules=(PermissionRule("synthetic_tool", "ask", "settings"),))

        self.assertEqual(deny.check(self._permission_context(hook=hook_allow)).behavior, "deny")
        self.assertEqual(ask.check(self._permission_context(hook=hook_allow)).behavior, "ask")

    def test_hook_deny_wins_and_confirm_tier_is_automatic_audit_tier(self):
        from intent.permissions.policy import PermissionHookProvenance, PermissionPolicy, PermissionRule

        hook_deny = PermissionHookProvenance(event="PreToolUse", decision="deny")
        ask = PermissionPolicy(rules=(PermissionRule("synthetic_tool", "ask", "settings"),))

        self.assertEqual(ask.check(self._permission_context(hook=hook_deny)).behavior, "deny")
        confirm_decision = PermissionPolicy().check(self._permission_context(risk="confirm"))
        self.assertEqual(confirm_decision.behavior, "allow")
        self.assertEqual(confirm_decision.source, "risk_metadata")

    def test_irreversible_classifier_fails_closed_for_unknown_payment_actions(self):
        from intent.irreversible_actions import irreversible_action_class

        expected = {
            ("run_command", ""): "shell_command",
            ("mcp_servers", "register"): "mcp_register",
            ("calendar", "create"): "calendar_write",
            ("calendar", "delete"): "calendar_delete",
            ("payment", "provider_extension_charge"): "payment",
            ("fill_payment_details", ""): "payment",
        }
        for (tool_name, action), action_class in expected.items():
            with self.subTest(tool_name=tool_name, action=action):
                self.assertEqual(irreversible_action_class(tool_name, {"action": action}), action_class)

        for safe_action in ("", "list", "request_review", "open_secure_card_entry"):
            with self.subTest(safe_action=safe_action):
                self.assertIsNone(irreversible_action_class("payment", {"action": safe_action}))


class ShellSubprocessContract(unittest.IsolatedAsyncioTestCase):
    def test_sanitized_environment_strips_secrets_and_code_injection_case_insensitively(self):
        from intent.tools.shell import sanitized_env

        synthetic = {
            "PATH": "synthetic-path",
            "SAFE_SETTING": "preserved",
            "openai_api_key": "must-not-cross",
            "Node_Options": "--require=synthetic-payload.js",
            "pythonpath": "synthetic-module-path",
            "LD_PRELOAD": "synthetic-library",
        }
        with patch.dict(os.environ, synthetic, clear=True):
            child_env = sanitized_env()

        self.assertEqual(child_env["SAFE_SETTING"], "preserved")
        for forbidden in ("openai_api_key", "Node_Options", "pythonpath", "LD_PRELOAD"):
            self.assertNotIn(forbidden, child_env)

    async def test_allowed_shell_launch_uses_sanitized_environment_at_spawn_boundary(self):
        from intent.permissions.shell_safety import ShellSafetyDecision
        from intent.tools import shell as shell_module

        captured: dict[str, object] = {}

        class SyntheticProcess:
            returncode = 0

            async def communicate(self):
                return b"synthetic output", b""

        async def capture_exec(*args, **kwargs):
            captured["args"] = args
            captured["env"] = dict(kwargs["env"])
            return SyntheticProcess()

        decision = ShellSafetyDecision(
            behavior="allow",
            normalized_command="synthetic-tool --version",
            shell="cmd",
            cwd=str(_TEST_PROFILE_ROOT),
            read_only=True,
            reason="synthetic read-only command",
        )
        parent_env = {
            "PATH": "synthetic-path",
            "SAFE_SETTING": "preserved",
            "OPENAI_API_KEY": "must-not-cross",
            "NODE_OPTIONS": "--require=synthetic-payload.js",
        }
        with (
            patch.dict(os.environ, parent_env, clear=True),
            patch.object(shell_module, "validate_shell_command", return_value=decision),
            patch.object(shell_module, "_audit_log", return_value=True),
            patch.object(asyncio, "create_subprocess_exec", side_effect=capture_exec),
            patch.object(asyncio, "create_subprocess_shell", side_effect=AssertionError("shell path must not run")),
        ):
            result = await shell_module.run_command(
                "synthetic-tool --version",
                working_directory=str(_TEST_PROFILE_ROOT),
            )

        self.assertTrue(result.ok)
        self.assertEqual(captured["args"], ("synthetic-tool", "--version"))
        child_env = captured["env"]
        self.assertEqual(child_env["SAFE_SETTING"], "preserved")
        self.assertNotIn("OPENAI_API_KEY", child_env)
        self.assertNotIn("NODE_OPTIONS", child_env)


class PaymentToolContract(unittest.IsolatedAsyncioTestCase):
    async def test_public_payment_fill_is_explicitly_unavailable_before_browser_access(self):
        from mcp_servers.browser import server as browser_module

        with (
            patch.object(browser_module, "_payment_fill_handler", None),
            patch.object(
                browser_module.manager,
                "get_page",
                new=AsyncMock(side_effect=AssertionError("unavailable tool must not access browser")),
            ),
        ):
            payload = json.loads(await browser_module.fill_payment_details("synthetic-card"))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "payment_fill_unavailable")

    async def test_generic_browser_type_rejects_card_shaped_value_before_browser_access(self):
        from mcp_servers.browser import server as browser_module

        with patch.object(
            browser_module.manager,
            "get_page",
            new=AsyncMock(side_effect=AssertionError("payment violation must not access browser")),
        ):
            payload = json.loads(await browser_module._do_type("#card", "4242 4242 4242 4242"))

        self.assertFalse(payload["ok"])
        self.assertIn("PAYMENT SAFETY VIOLATION", payload["error"])

    async def test_registered_payment_handler_is_invoked_and_cannot_be_replaced(self):
        from mcp_servers.browser import server as browser_module

        observed: dict[str, object] = {}
        synthetic_page = object()

        async def first_handler(args, *, page):
            observed["args"] = dict(args)
            observed["page"] = page
            return json.dumps({"ok": True, "synthetic": True})

        async def replacement_handler(_args, *, page):
            return str(page)

        with (
            patch.object(browser_module, "_payment_fill_handler", None),
            patch.object(
                browser_module,
                "_get_call_payment_confirmation",
                return_value={"confirmation_token": "synthetic-token"},
            ),
            patch.object(browser_module, "_require_call_user_id", return_value="synthetic-user"),
            patch.object(browser_module.manager, "get_page", new=AsyncMock(return_value=synthetic_page)),
        ):
            browser_module.register_payment_fill_handler(first_handler)
            browser_module.register_payment_fill_handler(first_handler)
            self.assertIs(browser_module._payment_fill_handler, first_handler)
            with self.assertRaisesRegex(RuntimeError, "already registered"):
                browser_module.register_payment_fill_handler(replacement_handler)
            payload = json.loads(await browser_module.fill_payment_details("synthetic-card"))

        self.assertTrue(payload["ok"])
        self.assertEqual(observed["args"]["card_label"], "synthetic-card")
        self.assertEqual(observed["args"]["confirmation_token"], "synthetic-token")
        self.assertIs(observed["page"], synthetic_page)


class TimeoutAndChildAuthorityContract(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _cancel_executor(AgentExecutor, *, cancelled: bool):
        executor = object.__new__(AgentExecutor)
        executor._cancel_event = asyncio.Event()
        if cancelled:
            executor._cancel_event.set()
        executor._takeover_interrupt_event = asyncio.Event()
        executor._takeover_active = False
        executor._cancelled = False
        return executor

    async def test_interrupt_cancels_read_only_action_but_completes_mutation(self):
        from intent import agent_executor as agent_module
        from intent.agent_executor import AgentExecutor

        executor = self._cancel_executor(AgentExecutor, cancelled=True)

        async def complete():
            return {"success": True}

        with self.assertRaises(agent_module._ToolExecutionCancelled):
            await AgentExecutor._run_tool_with_cancel(
                executor,
                complete(),
                tool_name="calendar",
                tool_args={"action": "list"},
                timeout_seconds=1.0,
            )

        result = await AgentExecutor._run_tool_with_cancel(
            executor,
            complete(),
            tool_name="calendar",
            tool_args={"action": "create"},
            timeout_seconds=1.0,
        )
        self.assertTrue(result["success"])

    async def test_timeout_signals_abort_and_cleans_up_inflight_tool(self):
        from intent import agent_executor as agent_module
        from intent.agent_executor import AgentExecutor

        executor = self._cancel_executor(AgentExecutor, cancelled=False)
        abort_signal = asyncio.Event()
        cancelled = asyncio.Event()

        async def never_finishes():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with self.assertRaises(agent_module._ToolExecutionTimedOut):
            await AgentExecutor._run_tool_with_cancel(
                executor,
                never_finishes(),
                tool_name="web_search",
                tool_args={"query": "synthetic"},
                timeout_seconds=0.01,
                abort_signal=abort_signal,
            )

        self.assertTrue(abort_signal.is_set())
        self.assertTrue(cancelled.is_set())

    def test_parallel_child_dispatch_allowlist_excludes_all_shared_state_families(self):
        from intent.agent_executor import _restricted_child_allowed_tools, _without_shared_state_tools
        from intent.agent_loop import _tool_allowed_by_executor

        parent_tools = [
            {"name": "mcp__search__web_search"},
            {"name": "search.web_read"},
            {"name": "memory"},
            {"name": "browser.browser_click"},
            {"name": "mcp__desktop__desktop_snapshot"},
            {"name": "mcp__host__computer_control"},
            {"name": "vault__fill_payment_details"},
            {"name": "host.signature"},
        ]
        safe_tools = _without_shared_state_tools(parent_tools)
        child = SimpleNamespace(_allowed_tools=_restricted_child_allowed_tools(safe_tools, None))

        self.assertEqual(child._allowed_tools, {"mcp__search__web_search", "search.web_read", "memory"})
        self.assertTrue(_tool_allowed_by_executor(child, "mcp__search__web_search"))
        self.assertTrue(_tool_allowed_by_executor(child, "search.web_read"))
        self.assertTrue(_tool_allowed_by_executor(child, "memory"))
        for forbidden in (
            "browser.browser_click",
            "mcp__desktop__desktop_snapshot",
            "mcp__host__computer_control",
            "vault__fill_payment_details",
            "host.signature",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertFalse(_tool_allowed_by_executor(child, forbidden))

        alias_restricted = SimpleNamespace(
            _allowed_tools=_restricted_child_allowed_tools(safe_tools, {"web_search", "web_read"})
        )
        self.assertEqual(
            alias_restricted._allowed_tools,
            {"mcp__search__web_search", "search.web_read"},
        )
        self.assertTrue(_tool_allowed_by_executor(alias_restricted, "mcp__search__web_search"))
        self.assertTrue(_tool_allowed_by_executor(alias_restricted, "search.web_read"))
        self.assertFalse(_tool_allowed_by_executor(alias_restricted, "memory"))

    async def test_semantic_parallel_task_gets_restricted_child_authority(self):
        from intent.agent_executor import AgentExecutor
        from intent.tool_types import ToolResult

        executor = object.__new__(AgentExecutor)
        executor._depth = 0
        executor._current_checkpoint = object()
        observed: list[dict[str, object]] = []

        async def run_child(**kwargs):
            observed.append(kwargs)
            return ToolResult(ok=True, data="synthetic summary")

        executor._run_child_agent = run_child
        result = await AgentExecutor._handle_spawn_parallel_subtasks(
            executor,
            {"tasks": [{"task": "Compare the two choices and report the result."}]},
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0]["exclude_shared_state_tools"])

        executor._depth = 1
        recursive = await AgentExecutor._handle_spawn_parallel_subtasks(
            executor,
            {"tasks": [{"task": "Repeat the comparison."}]},
        )
        self.assertFalse(recursive.ok)
        self.assertEqual(len(observed), 1)


class BrowserGateBindingContract(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # AgentExecutor imports checkpoint persistence, whose compatibility
        # migration reads Path.home()/.viola/tasks at module import. Module
        # setup activates the disposable home before this first import.
        from intent import task_checkpoint

        cls._task_checkpoint = task_checkpoint

    @staticmethod
    def _make_executor(AgentExecutor):
        class FakeHub:
            def __init__(self):
                self.calls = 0
                self.last_approval_path = None

            async def call_tool(self, *_args, **_kwargs):
                self.calls += 1
                return {"success": True, "data": {"synthetic": True}}

        async def run_tool(coro, **_kwargs):
            return await coro

        async def no_op_async(*_args, **_kwargs):
            return None

        executor = object.__new__(AgentExecutor)
        executor._mcp_hub = FakeHub()
        executor._session_id = "synthetic-session"
        executor._user_id = "synthetic-user"
        executor._channel = "synthetic"
        executor._allowed_tools = None
        executor._tool_timeout = 5.0
        executor._irreversible_confirmation_class = lambda *_args: None
        executor._memory_exhaustion_short_circuit = lambda *_args: None
        executor._ensure_task_id = lambda: "synthetic-task"
        executor._get_origin_channel_type = lambda: "synthetic"
        executor._run_tool_with_cancel = run_tool
        executor._broadcast_paid_action_gate = no_op_async
        executor._track_page_url = lambda *_args: None
        executor._record_memory_all_scope_no_match = lambda *_args: None
        return executor

    @staticmethod
    def _dispatch_dependencies(gate_module):
        from intent import agent_executor as agent_module

        class SyntheticMcpAuthError(Exception):
            pass

        errors_module = types.ModuleType("mcp_hub.errors")
        errors_module.McpAuthError = SyntheticMcpAuthError
        surface_module = types.ModuleType("services.user_capabilities.context")
        surface_module.set_current_surface = lambda _surface: object()
        surface_module.reset_current_surface = lambda _token: None
        return (
            patch.dict(
                sys.modules,
                {
                    "mcp_servers.browser.server": gate_module,
                    "mcp_hub.errors": errors_module,
                    "services.user_capabilities.context": surface_module,
                },
            ),
            patch.object(agent_module, "_launch_tools_kill_switch_open", return_value=True),
            patch.object(agent_module, "classify_action", return_value=None),
        )

    async def test_partial_binding_rolls_back_and_browser_call_fails_closed(self):
        from intent.agent_executor import AgentExecutor
        executor = self._make_executor(AgentExecutor)

        payment_session = contextvars.ContextVar("synthetic_payment_session", default=None)
        gate_module = types.ModuleType("mcp_servers.browser.server")
        gate_module.set_payment_session = payment_session.set
        gate_module.reset_payment_session = payment_session.reset

        def fail_signature_binding(_session_id):
            raise RuntimeError("synthetic binding failure")

        gate_module.set_signature_session = fail_signature_binding
        gate_module.reset_signature_session = lambda _token: None

        original_payment_session = payment_session.get()
        dependencies = self._dispatch_dependencies(gate_module)
        with dependencies[0], dependencies[1], dependencies[2]:
            result = await AgentExecutor._execute_tool(executor, "browser_click", {"selector": "#safe"})

        self.assertFalse(result.ok)
        self.assertEqual(result.error_category, "GATE_SESSION_BINDING_FAILED")
        self.assertEqual(executor._mcp_hub.calls, 0)
        self.assertEqual(payment_session.get(), original_payment_session)

    async def test_first_binding_failure_blocks_browser_call(self):
        from intent.agent_executor import AgentExecutor
        executor = self._make_executor(AgentExecutor)

        gate_module = types.ModuleType("mcp_servers.browser.server")
        gate_module.set_payment_session = lambda _session_id: (_ for _ in ()).throw(
            RuntimeError("synthetic first binding failure")
        )
        gate_module.set_signature_session = lambda _session_id: self.fail("second setter must not run")

        dependencies = self._dispatch_dependencies(gate_module)
        with dependencies[0], dependencies[1], dependencies[2]:
            result = await AgentExecutor._execute_tool(executor, "browser_click", {"selector": "#safe"})

        self.assertFalse(result.ok)
        self.assertEqual(result.error_category, "GATE_SESSION_BINDING_FAILED")
        self.assertEqual(executor._mcp_hub.calls, 0)

    async def test_successful_browser_call_restores_both_contexts(self):
        from intent.agent_executor import AgentExecutor
        executor = self._make_executor(AgentExecutor)

        payment_session = contextvars.ContextVar("successful_payment_session", default=None)
        signature_session = contextvars.ContextVar("successful_signature_session", default=None)
        gate_module = types.ModuleType("mcp_servers.browser.server")
        gate_module.set_payment_session = payment_session.set
        gate_module.reset_payment_session = payment_session.reset
        gate_module.set_signature_session = signature_session.set
        gate_module.reset_signature_session = signature_session.reset

        dependencies = self._dispatch_dependencies(gate_module)
        with dependencies[0], dependencies[1], dependencies[2]:
            result = await AgentExecutor._execute_tool(executor, "browser_click", {"selector": "#safe"})

        self.assertTrue(result.ok)
        self.assertEqual(executor._mcp_hub.calls, 1)
        self.assertIsNone(payment_session.get())
        self.assertIsNone(signature_session.get())

    async def test_binding_scope_preserves_ordinary_tool_dispatch(self):
        from intent.agent_executor import AgentExecutor
        executor = self._make_executor(AgentExecutor)

        # Another public test module may import task_checkpoint before this
        # module's setUpModule runs.  In an aggregate process, both the
        # process-level fake profile and this module's nested profile are valid
        # as long as they remain inside the caller-provided test boundary.
        self._task_checkpoint._LEGACY_CHECKPOINT_DIR.resolve().relative_to(_TEST_BOUNDARY_ROOT)
        self._task_checkpoint.CHECKPOINT_DIR.resolve().relative_to(_TEST_BOUNDARY_ROOT)

        gate_module = types.ModuleType("mcp_servers.browser.server")
        gate_module.set_payment_session = lambda _session_id: self.fail("ordinary tool must not bind browser context")
        gate_module.set_signature_session = lambda _session_id: self.fail("ordinary tool must not bind browser context")

        dependencies = self._dispatch_dependencies(gate_module)
        with dependencies[0], dependencies[1], dependencies[2]:
            result = await AgentExecutor._execute_tool(executor, "memory", {"action": "list"})

        self.assertTrue(result.ok)
        self.assertEqual(executor._mcp_hub.calls, 1)


if __name__ == "__main__":
    unittest.main()
