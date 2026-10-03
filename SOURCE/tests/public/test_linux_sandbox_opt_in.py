"""Exercise actual pre-Qt environment setup without starting a GUI or services."""

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace


ENTRY = Path(__file__).resolve().parents[2] / "viola_qt.py"


def load_startup_flags(platform="linux", environ=None, source=None):
    """Run the entrypoint's contiguous Chromium setup against a private mapping."""
    tree = ast.parse(source if source is not None else ENTRY.read_text(encoding="utf-8"))
    start = next(
        i
        for i, node in enumerate(tree.body)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_chromium_stability_flags" for target in node.targets)
    )
    end = next(
        i
        for i, node in enumerate(tree.body[start:], start)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_raw_cdp_debugging" for target in node.targets)
    )
    isolated = dict(environ or {})
    namespace = {"os": SimpleNamespace(environ=isolated), "sys": SimpleNamespace(platform=platform)}
    exec(compile(ast.Module(body=tree.body[start:end], type_ignores=[]), str(ENTRY), "exec"), namespace)
    return isolated


class LinuxSandboxOptInTests(unittest.TestCase):
    def test_zero_is_removed_before_qt_checks_variable_presence(self):
        result = load_startup_flags(environ={"QTWEBENGINE_DISABLE_SANDBOX": "0"})
        self.assertNotIn("QTWEBENGINE_DISABLE_SANDBOX", result)
        self.assertNotIn("--no-sandbox", result["QTWEBENGINE_CHROMIUM_FLAGS"].split())

    def test_existing_compatibility_default_is_unchanged(self):
        for original in ({}, {"QTWEBENGINE_DISABLE_SANDBOX": "1"}, {"QTWEBENGINE_DISABLE_SANDBOX": ""}):
            with self.subTest(original=original):
                result = load_startup_flags(environ=original)
                self.assertIn("QTWEBENGINE_DISABLE_SANDBOX", result)
                self.assertEqual(result["QTWEBENGINE_DISABLE_SANDBOX"], original.get("QTWEBENGINE_DISABLE_SANDBOX", "1"))
                self.assertIn("--no-sandbox", result["QTWEBENGINE_CHROMIUM_FLAGS"].split())

    def test_opt_in_keeps_caller_flags_and_other_environment(self):
        result = load_startup_flags(
            environ={
                "QTWEBENGINE_DISABLE_SANDBOX": "0",
                "QTWEBENGINE_CHROMIUM_FLAGS": "--lang=en-US --disable-gpu",
                "UNRELATED": "kept",
            }
        )
        flags = result["QTWEBENGINE_CHROMIUM_FLAGS"].split()
        self.assertIn("--lang=en-US", flags)
        self.assertIn("--autoplay-policy=no-user-gesture-required", flags)
        self.assertEqual(flags.count("--disable-gpu"), 1)
        self.assertEqual(result["UNRELATED"], "kept")

    def test_explicit_caller_flag_is_not_silently_rewritten(self):
        result = load_startup_flags(
            environ={"QTWEBENGINE_DISABLE_SANDBOX": "0", "QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox"}
        )
        self.assertNotIn("QTWEBENGINE_DISABLE_SANDBOX", result)
        self.assertEqual(result["QTWEBENGINE_CHROMIUM_FLAGS"].split().count("--no-sandbox"), 1)

    def test_other_platforms_keep_environment_semantics(self):
        for platform in ("win32", "darwin"):
            for original in ({}, {"QTWEBENGINE_DISABLE_SANDBOX": "0"}):
                with self.subTest(platform=platform, original=original):
                    result = load_startup_flags(platform=platform, environ=original)
                    self.assertEqual(result.get("QTWEBENGINE_DISABLE_SANDBOX"), original.get("QTWEBENGINE_DISABLE_SANDBOX"))
                    self.assertNotIn("--no-sandbox", result["QTWEBENGINE_CHROMIUM_FLAGS"].split())

    def test_startup_configuration_precedes_first_qt_import(self):
        tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
        flag_write = next(
            node.lineno
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(ast.unparse(target) == "os.environ['QTWEBENGINE_CHROMIUM_FLAGS']" for target in node.targets)
        )
        qt_imports = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("PySide6")
        ]
        self.assertTrue(qt_imports)
        self.assertLess(flag_write, min(qt_imports))


if __name__ == "__main__":
    unittest.main()
