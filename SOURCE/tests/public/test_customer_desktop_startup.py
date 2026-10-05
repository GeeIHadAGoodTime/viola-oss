"""Desktop customer bootstrap source controls; native imports stay inert."""

from __future__ import annotations

import ast
import builtins
from pathlib import Path
from types import SimpleNamespace
import unittest

ENTRY = Path(__file__).resolve().parents[2] / "viola_qt.py"


class FatalBoundary(Exception):
    pass


def load(profile="espeak", import_error=None, guard_error=None):
    tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
    names = {"_require_desktop_customer_startup", "_fatal_boot_unless_clean_exit"}
    nodes = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in names)
        or (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_DESKTOP_CUSTOMER_PROFILE_AT_IMPORT" for t in node.targets)
        )
    ]
    assert len(nodes) == 3
    environment = {"VIOLA_KOKORO_PHONEMIZER": profile, "ORT_DISABLE_TELEMETRY": "unchanged"}
    calls = []
    original = builtins.__import__

    def guard():
        calls.append("guard")
        if guard_error is not None:
            raise guard_error

    def importer(name, *args, **kwargs):
        if name == "kokoro_onnx":
            calls.append("kokoro")
            if import_error is not None:
                raise import_error
            return SimpleNamespace(_require_customer_telemetry_opt_out=guard)
        return original(name, *args, **kwargs)

    def fatal(stage, error):
        calls.append((stage, error))
        raise FatalBoundary(stage) from error

    namespace = {
        "os": SimpleNamespace(getenv=environment.get),
        "_fatal_boot": fatal,
        "__builtins__": {**vars(builtins), "__import__": importer},
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ENTRY), "exec"), namespace)
    return namespace["_require_desktop_customer_startup"], environment, calls


class CustomerDesktopStartup(unittest.TestCase):
    def test_source_fixtures_do_not_depend_on_windows_default_encoding(self):
        for name in ("test_customer_desktop_startup.py", "test_customer_pronunciation.py"):
            fixture = Path(__file__).with_name(name)
            tree = ast.parse(fixture.read_text(encoding="utf-8"))
            reads = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "read_text"
            ]
            self.assertTrue(reads, name)
            for call in reads:
                with self.subTest(fixture=name, line=call.lineno):
                    self.assertTrue(
                        any(
                            keyword.arg == "encoding"
                            and isinstance(keyword.value, ast.Constant)
                            and keyword.value.value == "utf-8"
                            for keyword in call.keywords
                        ),
                        "Repository source is UTF-8 on every platform",
                    )

    def test_default_route_does_not_import_customer_runtime_or_change_flags(self):
        check, environment, calls = load()
        before = dict(environment)
        check()
        self.assertEqual(calls, [])
        self.assertEqual(environment, before)

    def test_selected_customer_invokes_guard_and_rechecks_every_entry(self):
        check, environment, calls = load("misaki-en")
        before = dict(environment)
        check()
        check()
        self.assertEqual(calls, ["kokoro", "guard", "kokoro", "guard"])
        self.assertEqual(environment, before)

    def test_both_profile_transitions_fail_before_runtime_import(self):
        for original, changed in [("espeak", "misaki-en"), ("misaki-en", "espeak")]:
            with self.subTest(original=original):
                check, environment, calls = load(original)
                environment["VIOLA_KOKORO_PHONEMIZER"] = changed
                with self.assertRaises(FatalBoundary):
                    check()
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], "customer speech bootstrap")
                self.assertIsInstance(calls[0][1], RuntimeError)

    def test_missing_customer_component_is_fatal_instead_of_optional_ort_fallback(self):
        failure = ImportError("controlled absent customer component")
        check, _, calls = load("misaki-en", import_error=failure)
        with self.assertRaises(FatalBoundary):
            check()
        self.assertEqual(calls[0], "kokoro")
        self.assertIs(calls[-1][1], failure)
        self.assertNotIn("guard", calls)

    def test_guard_failure_retains_existing_boot_diagnostics(self):
        for failure in [RuntimeError("pre-init guard"), ImportError("dependency"), KeyboardInterrupt(), SystemExit(7)]:
            with self.subTest(failure=type(failure).__name__):
                check, environment, calls = load("misaki-en", guard_error=failure)
                before = dict(environment)
                with self.assertRaises(FatalBoundary):
                    check()
                self.assertIs(calls[-1][1], failure)
                self.assertEqual(environment, before)

    def test_clean_exit_semantics_remain_unchanged(self):
        for code in [0, None]:
            check, _, calls = load("misaki-en", guard_error=SystemExit(code))
            with self.assertRaises(SystemExit) as caught:
                check()
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(calls, ["kokoro", "guard"])

    def test_capture_precedes_helpers_and_guard_precedes_direct_ort(self):
        tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
        capture = next(
            n.lineno
            for n in tree.body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_DESKTOP_CUSTOMER_PROFILE_AT_IMPORT" for t in n.targets)
        )

        def calls(name):
            return [
                n.lineno
                for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
            ]

        top_check = next(
            n.lineno
            for n in tree.body
            if isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name)
            and n.value.func.id == "_require_desktop_customer_startup"
        )
        ort = next(
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Import) and any(a.name == "onnxruntime" for a in n.names)
        )
        self.assertLess(capture, min(calls("_run_frozen_dash_c_and_exit")))
        self.assertLess(min(calls("_run_velopack_startup_hook_first")), top_check)
        self.assertLess(min(calls("_preload_windows_ui_runtime")), top_check)
        self.assertLess(min(calls("_configure_viola_environment")), top_check)
        self.assertLess(top_check, ort)

    def test_gui_and_headless_recheck_before_any_application_import(self):
        tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
        for name in ("main", "_run_headless_daemon"):
            node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
            check = next(
                n.lineno
                for n in node.body
                if isinstance(n, ast.Expr)
                and isinstance(n.value, ast.Call)
                and isinstance(n.value.func, ast.Name)
                and n.value.func.id == "_require_desktop_customer_startup"
            )
            imports = [n.lineno for n in ast.walk(node) if isinstance(n, (ast.Import, ast.ImportFrom))]
            self.assertLess(check, min(imports))
            # Execute just the actual entry boundary with a sentinel; no GUI,
            # daemon, model or other application import may run first.
            prefix = [n for n in node.body if n.lineno <= check]
            small = ast.FunctionDef(name=node.name, args=node.args, body=prefix, decorator_list=[])

            def stop():
                raise FatalBoundary("entry recheck")

            ns = {"_require_desktop_customer_startup": stop}
            exec(compile(ast.fix_missing_locations(ast.Module(body=[small], type_ignores=[])), str(ENTRY), "exec"), ns)
            with self.assertRaisesRegex(FatalBoundary, "entry recheck"):
                ns[name]()
