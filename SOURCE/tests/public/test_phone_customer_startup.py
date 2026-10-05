"""Source-only privacy-bootstrap contracts; never initialize a native runtime."""

from __future__ import annotations

import ast
import os
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2]
CALL_MANAGER = SOURCE / "telephony" / "call_manager.py"
GUARDED_ENTRIES = (
    "_create_phone_kokoro_session",
    "_phone_kokoro_from_session",
    "_load_phone_kokoro_tts_runtime",
    "_ensure_phone_kokoro_tts_runtime",
    "_ensure_phone_runtime_dependencies",
    "_create_tts",
)


class PhoneCustomerStartupTests(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse(CALL_MANAGER.read_text(encoding="utf-8"))

    def load_bootstrap(self):
        selected = []
        for node in self.tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "_PHONE_CUSTOMER_PROFILE_AT_IMPORT"
                for target in node.targets
            ):
                selected.append(node)
            if isinstance(node, ast.FunctionDef) and node.name == "_require_phone_customer_startup":
                selected.append(node)
        self.assertEqual(len(selected), 2, "customer bootstrap and immutable profile must exist")
        scope = {"os": os}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(CALL_MANAGER), "exec"), scope)
        return scope["_require_phone_customer_startup"]

    def test_bootstrap_precedes_first_application_import(self):
        calls = [
            index
            for index, node in enumerate(self.tree.body)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_require_phone_customer_startup"
        ]
        first_app = next(
            index
            for index, node in enumerate(self.tree.body)
            if isinstance(node, ast.ImportFrom) and node.module == "config"
        )
        self.assertEqual(len(calls), 1)
        self.assertLess(calls[0], first_app)

    def test_all_native_cache_and_dependency_entries_recheck_first(self):
        found = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name in GUARDED_ENTRIES:
                statements = node.body
                if isinstance(statements[0], ast.Expr) and isinstance(statements[0].value, ast.Constant):
                    statements = statements[1:]
                found[node.name] = ast.unparse(statements[0])
        self.assertEqual(set(found), set(GUARDED_ENTRIES))
        self.assertEqual(set(found.values()), {"_require_phone_customer_startup()"})

    def test_default_profile_does_not_import_kokoro(self):
        with patch.dict(os.environ, {}, clear=True), patch.dict("sys.modules", {"kokoro_onnx": None}):
            self.load_bootstrap()()

    def test_customer_delegates_guard_without_mutating_environment(self):
        observed = []
        module = types.ModuleType("kokoro_onnx")
        module._require_customer_telemetry_opt_out = lambda: observed.append("guard")
        env = {"VIOLA_KOKORO_PHONEMIZER": "misaki-en", "ORT_DISABLE_TELEMETRY": "1"}
        with patch.dict(os.environ, env, clear=True), patch.dict("sys.modules", {"kokoro_onnx": module}):
            before = dict(os.environ)
            self.load_bootstrap()()
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(observed, ["guard"])

    def test_guard_failure_propagates_unchanged(self):
        error = RuntimeError("recording privacy sentinel")
        module = types.ModuleType("kokoro_onnx")

        def fail():
            raise error

        module._require_customer_telemetry_opt_out = fail
        with patch.dict(os.environ, {"VIOLA_KOKORO_PHONEMIZER": "misaki-en"}, clear=True):
            with patch.dict("sys.modules", {"kokoro_onnx": module}):
                with self.assertRaises(RuntimeError) as raised:
                    self.load_bootstrap()()
                self.assertIs(raised.exception, error)
                self.assertNotIn("ORT_DISABLE_TELEMETRY", os.environ)

    def test_both_profile_transitions_fail_before_import(self):
        for initial, changed in (("espeak", "misaki-en"), ("misaki-en", "espeak")):
            with self.subTest(initial=initial, changed=changed):
                with patch.dict(os.environ, {"VIOLA_KOKORO_PHONEMIZER": initial}, clear=True):
                    helper = self.load_bootstrap()
                    os.environ["VIOLA_KOKORO_PHONEMIZER"] = changed
                    with patch.dict("sys.modules", {"kokoro_onnx": None}):
                        with self.assertRaisesRegex(RuntimeError, "cannot change after startup"):
                            helper()


if __name__ == "__main__":
    unittest.main()
