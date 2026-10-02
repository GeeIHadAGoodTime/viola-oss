import ast
import builtins
from pathlib import Path
from types import SimpleNamespace
import pytest

ENTRY = Path(__file__).resolve().parents[2] / "viola_qt.py"


def load(platform="win32", name="__main__", argv=None, hook=False, missing=False):
    tree = ast.parse(ENTRY.read_text())
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


def test_preloads_native_ui_before_background_imports():
    assert load() == (
        ["win32ui"],
        ["03a-win32ui-preload-start", "03b-win32ui-preload-complete"],
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"platform": "linux"},
        {"platform": "darwin"},
        {"name": "__mp_main__"},
        {"name": "viola_qt"},
        {"argv": ["ViolaApp.exe", "--multiprocessing-fork"]},
        {"hook": True},
    ],
)
def test_does_not_initialize_windows_ui_for_other_platforms_or_helper_invocations(
    kwargs,
):
    assert load(**kwargs) == ([], [])


def test_missing_optional_module_is_observable_without_disabling_other_product_features():
    imports, checkpoints = load(missing=True)
    assert imports == ["win32ui"]
    assert checkpoints == [
        "03a-win32ui-preload-start",
        "03b-win32ui-preload-unavailable",
    ]


def test_call_precedes_telemetry_import_and_follows_velopack():
    tree = ast.parse(ENTRY.read_text())
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
    assert "_preload_windows_ui_runtime" in calls
    assert ENTRY.read_text().index("_VELOPACK_STARTUP_HOOK_RAN =") < ENTRY.read_text().index(
        "\n_preload_windows_ui_runtime()"
    )
    assert calls["_preload_windows_ui_runtime"] < min(imports)
