import ast
import builtins
from pathlib import Path
from types import SimpleNamespace
import unittest

ENTRY = Path(__file__).resolve().parents[2] / "viola_qt.py"


def load(platform="win32", name="__main__", argv=None, hook=False, missing=False):
    tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
    node = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_preload_windows_ui_runtime"),
        None,
    )
    assert node is not None, "Windows native preload missing"
    imports = []
    checkpoints = []
    original = builtins.__import__

    def importer(module, *args, **kwargs):
        if module == "sys":
            return SimpleNamespace(platform=platform, argv=argv or ["ViolaApp.exe"])
        if module == "win32ui":
            imports.append(module)
            if missing:
                raise ImportError("controlled missing optional desktop runtime")
            return object()
        return original(module, *args, **kwargs)

    ns = {
        "_sys": SimpleNamespace(platform=platform, argv=argv or ["ViolaApp.exe"]),
        "__name__": name,
        "_is_velopack_hook_invocation": lambda: hook,
        "_boot_checkpoint": checkpoints.append,
        "__builtins__": {**vars(builtins), "__import__": importer},
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ENTRY), "exec"), ns)
    ns["_preload_windows_ui_runtime"]()
    return imports, checkpoints


class WindowsNativeStartupOrderTests(unittest.TestCase):
    def test_preloads_native_ui_before_background_imports(self):
        self.assertEqual(load(), (["win32ui"], ["03a-win32ui-preload-start", "03b-win32ui-preload-complete"]))

    def test_skips_other_platforms_and_helper_invocations(self):
        cases = [
            {"platform": "linux"},
            {"platform": "darwin"},
            {"name": "__mp_main__"},
            {"name": "viola_qt"},
            {"argv": ["ViolaApp.exe", "--multiprocessing-fork"]},
            {"hook": True},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(load(**kwargs), ([], []))

    def test_missing_optional_module_is_observable(self):
        imports, checkpoints = load(missing=True)
        self.assertEqual(imports, ["win32ui"])
        self.assertEqual(checkpoints, ["03a-win32ui-preload-start", "03b-win32ui-preload-unavailable"])

    def test_call_precedes_telemetry_and_follows_velopack(self):
        tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
        calls = {
            n.value.func.id: n.lineno
            for n in tree.body
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)
        }
        imports = [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module == "telemetry.release_health_session"
        ]
        self.assertIn("_preload_windows_ui_runtime", calls)
        self.assertLess(
            ENTRY.read_text(encoding="utf-8").index("_VELOPACK_STARTUP_HOOK_RAN ="),
            ENTRY.read_text(encoding="utf-8").index("\n_preload_windows_ui_runtime()"),
        )
        self.assertLess(calls["_preload_windows_ui_runtime"], min(imports))
