"""Synthetic checks for public execution boundaries.

These tests use sentinel values only.  They never start an external MCP
process, execute a shell command, or open a browser/payment page.
"""

from __future__ import annotations

import os
import contextvars
import importlib.util
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SOURCE_ROOT = Path(__file__).resolve().parents[2]

_TEST_PROFILE = None
_TEST_PROFILE_ROOT = None
_FAKE_HOME = None
_FAKE_DATA = None
_PROFILE_ENV = None
_HOME_LOOKUP = None
_ORIGINAL_ROOT_HANDLERS = None
_ORIGINAL_ROOT_LEVEL = None
_ORIGINAL_LOGGING_READY = False
_ORIGINAL_OBSERVABILITY_CONFIGURED = False
_ORIGINAL_OBSERVABILITY_CONFIG = None


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
    global _PROFILE_ENV
    global _TEST_PROFILE
    global _TEST_PROFILE_ROOT

    root_logger = logging.getLogger()
    _ORIGINAL_ROOT_HANDLERS = list(root_logger.handlers)
    _ORIGINAL_ROOT_LEVEL = root_logger.level
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

        self.assertEqual(
            self._task_checkpoint._LEGACY_CHECKPOINT_DIR,
            _FAKE_HOME / ".viola" / "tasks",
        )
        self.assertEqual(self._task_checkpoint.CHECKPOINT_DIR, _FAKE_DATA / "tasks")

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
