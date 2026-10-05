"""Replay shipped toast and routing bodies with inert Windows COM boundaries.

No desktop notification, shortcut, process, network request or device is used.
The observed Windows shortcut-save exception must be an unsuccessful local
delivery, allowing the existing user-scoped web-push leg to continue.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from types import MethodType, ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[2]


class ComError(Exception):
    """The pywintypes.com_error boundary is not an OSError or RuntimeError."""


class WindowsToastDeliveryContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        source = ROOT / "services/notifications/windows_toast.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        # Execute every actual function and literal constant, replacing imports
        # only. Local pywin32 imports remain in the real helper under test.
        nodes = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            or (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant))
        ]
        self.mod = ModuleType("services.notifications.windows_toast")
        self.mod.__dict__.update(
            __file__=str(source),
            asyncio=asyncio,
            os=os,
            platform=platform,
            subprocess=subprocess,
            sys=sys,
            Path=Path,
            logger=logging.getLogger("windows-toast-contract"),
            proc_tree=SimpleNamespace(run=Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))),
        )
        unit = ast.Module(
            body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes],
            type_ignores=[],
        )
        exec(compile(ast.fix_missing_locations(unit), str(source), "exec"), self.mod.__dict__)
        self.mod._is_windows = lambda: True
        self.mod.windows_toast_enabled = lambda **kwargs: True
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"APPDATA": self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.events = []
        self.failure = None
        self.exception_type = ComError

        def stage(name, result=None):
            def invoke(*args, **kwargs):
                self.events.append(name)
                if self.failure == name:
                    raise self.exception_type("Unable to save or register Windows shortcut")
                return result

            return invoke

        self.shortcut = SimpleNamespace(save=stage("save"))
        shell = SimpleNamespace(CreateShortCut=stage("create", self.shortcut))
        self.store = SimpleNamespace(SetValue=stage("set_value"), Commit=stage("commit"))
        win32com = ModuleType("win32com")
        win32com.client = SimpleNamespace(Dispatch=stage("dispatch", shell))
        modules = {
            "pythoncom": SimpleNamespace(CoInitialize=stage("initialize"), CoUninitialize=stage("uninitialize")),
            "pywintypes": SimpleNamespace(com_error=ComError),
            "win32com": win32com,
            "win32com.client": win32com.client,
            "win32com.propsys": SimpleNamespace(
                propsys=SimpleNamespace(
                    SHGetPropertyStoreFromParsingName=stage("property_store", self.store),
                    IID_IPropertyStore="property-store-iid",
                    PROPVARIANTType=lambda value: value,
                ),
                pscon=SimpleNamespace(PKEY_AppUserModel_ID="app-id-property"),
            ),
            "win32com.shell": SimpleNamespace(shellcon=SimpleNamespace(GPS_READWRITE=2)),
            "services.notifications.windows_toast": self.mod,
        }
        imports = patch.dict(sys.modules, modules)
        imports.start()
        self.addCleanup(imports.stop)

    def test_shortcut_success_preserves_identity_and_balances_com(self):
        self.mod._ensure_app_shortcut("Viola.Desktop")
        self.assertEqual(
            self.events,
            ["initialize", "dispatch", "create", "save", "property_store", "set_value", "commit", "uninitialize"],
        )
        self.assertEqual(self.shortcut.Targetpath, sys.executable)
        self.assertEqual(self.shortcut.Description, "Viola")
        self.assertFalse(self.mod._shortcut_path().exists())

    def test_all_com_failure_stages_translate_and_preserve_cause(self):
        for stage in (
            "initialize",
            "dispatch",
            "create",
            "save",
            "property_store",
            "set_value",
            "commit",
            "uninitialize",
        ):
            with self.subTest(stage=stage):
                self.events.clear()
                self.failure = stage
                with self.assertRaisesRegex(RuntimeError, "Windows toast shortcut registration failed") as caught:
                    self.mod._ensure_app_shortcut()
                self.assertIsInstance(caught.exception.__cause__, ComError)
                self.assertEqual(self.events.count("uninitialize"), int(stage != "initialize"))

    async def test_save_failure_is_false_without_launching_powershell(self):
        self.failure = "save"
        self.assertFalse(await self.mod.send_windows_toast("Viola", "private reminder", user_id="synthetic-user"))
        self.mod.proc_tree.run.assert_not_called()
        self.assertEqual(self.events[-1], "uninitialize")

    async def test_success_runs_existing_powershell_delivery(self):
        self.assertTrue(await self.mod.send_windows_toast("Viola", "reminder", user_id="synthetic-user"))
        call = self.mod.proc_tree.run.call_args
        self.assertEqual(call.args[0][1:4], ["-NoProfile", "-NonInteractive", "-Command"])
        self.assertIn("Viola.Desktop", call.args[0][4])
        self.assertEqual(call.kwargs["timeout"], self.mod._TOAST_TIMEOUT_SECONDS)

    async def test_powershell_failure_is_not_reported_as_success(self):
        self.mod.proc_tree.run.return_value.returncode = 1
        self.assertFalse(await self.mod.send_windows_toast("Viola", "reminder"))

    async def test_disabled_toast_never_reaches_com(self):
        self.mod.windows_toast_enabled = lambda **kwargs: False
        self.assertFalse(await self.mod.send_windows_toast("Viola", "reminder"))
        self.assertEqual(self.events, [])
        self.mod.proc_tree.run.assert_not_called()

    def test_non_windows_shortcut_registration_is_inert(self):
        self.mod._is_windows = lambda: False
        self.mod._ensure_app_shortcut()
        self.assertEqual(self.events, [])

    async def test_unexpected_programming_error_is_not_swallowed(self):
        self.failure = "save"
        self.exception_type = AssertionError
        with self.assertRaises(AssertionError):
            await self.mod.send_windows_toast("Viola", "reminder")
        self.assertEqual(self.events[-1], "uninitialize")

    def routing_service(self, web_count):
        source = ROOT / "services/notifications/push_service.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        actual = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PushNotificationService"
        )
        methods = [
            node
            for node in actual.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name in {"_send_windows_toast", "_send_normal", "_send_high", "_send_urgent"}
        ]
        namespace = {
            "logger": logging.getLogger("windows-toast-routing-contract"),
            "_DELIVERY_ERRORS": (ImportError, RuntimeError, OSError, TypeError, ValueError),
        }
        unit = ast.Module(
            body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *methods],
            type_ignores=[],
        )
        exec(compile(ast.fix_missing_locations(unit), str(source), "exec"), namespace)
        service = SimpleNamespace(
            _cloud_surface_active=lambda: False,
            _send_macos_toast=AsyncMock(return_value=False),
            _send_linux_toast=AsyncMock(return_value=False),
            _send_web_push=AsyncMock(return_value=web_count),
            _record_sent=Mock(),
        )
        for method in methods:
            setattr(service, method.name, MethodType(namespace[method.name], service))
        return service

    async def test_local_com_failure_does_not_block_each_web_push_priority(self):
        self.failure = "save"
        notification = SimpleNamespace(user_id="synthetic-user", title="Viola", message="private reminder")
        for priority in ("normal", "high", "urgent"):
            with self.subTest(priority=priority):
                service = self.routing_service(1)
                self.assertTrue(await getattr(service, "_send_" + priority)(notification))
                self.assertEqual(service._send_web_push.await_args.args, (notification,))
                service._record_sent.assert_called_once_with(notification)
        self.mod.proc_tree.run.assert_not_called()

    async def test_all_legs_failed_never_records_delivery(self):
        self.failure = "save"
        notification = SimpleNamespace(user_id="synthetic-user", title="Viola", message="private reminder")
        for priority in ("normal", "high", "urgent"):
            with self.subTest(priority=priority):
                service = self.routing_service(0)
                self.assertFalse(await getattr(service, "_send_" + priority)(notification))
                service._record_sent.assert_not_called()

    async def test_cloud_surface_never_enters_local_registration(self):
        service = self.routing_service(1)
        service._cloud_surface_active = lambda: True
        notification = SimpleNamespace(user_id="synthetic-user", title="Viola", message="private reminder")
        self.assertFalse(await service._send_windows_toast(notification))
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
