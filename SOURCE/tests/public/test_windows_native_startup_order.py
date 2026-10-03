import ast
import builtins
import unittest
from pathlib import Path
from types import SimpleNamespace

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


class InitialWindowGeometryTests(unittest.TestCase):
    def test_initial_window_fits_available_logical_screen(self):
        path = ENTRY.parent / "ui/qt_native/webview_window.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        setup = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_setup_window")
        qt = SimpleNamespace(WindowType=SimpleNamespace(FramelessWindowHint=1), WidgetAttribute=SimpleNamespace(WA_TranslucentBackground=2))
        namespace = {"Qt": qt}
        exec(compile(ast.Module(body=[setup], type_ignores=[]), str(path), "exec"), namespace)
        for left, top, width, height in [(0, 0, 1024, 728), (0, 0, 800, 560), (0, 0, 1364, 900), (0, 40, 1920, 1040), (-1280, 20, 1280, 720)]:
            with self.subTest(screen=(left, top, width, height)):
                calls = {}
                geometry = SimpleNamespace(width=lambda: width, height=lambda: height, left=lambda: left, top=lambda: top)
                window = SimpleNamespace(screen=lambda: SimpleNamespace(availableGeometry=lambda: geometry))
                for name in ["setWindowTitle", "setWindowFlags", "setAttribute", "setStyleSheet", "setMouseTracking", "setMinimumSize", "resize", "move"]:
                    setattr(window, name, lambda *args, key=name: calls.__setitem__(key, args))
                namespace["_setup_window"](window)
                expected_size = (min(1400, width), min(800, height))
                self.assertEqual(calls["resize"], expected_size)
                self.assertEqual(calls["setMinimumSize"], (min(1024, width), min(600, height)))
                self.assertEqual(calls.get("move"), (left + (width - expected_size[0]) // 2, top + (height - expected_size[1]) // 2))


class NativeTitlebarNames(unittest.TestCase):
    def test_minimize_and_close_have_names_in_the_native_construction_path(self):
        source = ENTRY.parent / 'ui/qt_native/webview_window.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        titlebar = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CustomTitleBar')
        names = {}
        for node in ast.walk(titlebar):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'setAccessibleName':
                receiver = node.func.value
                if isinstance(receiver, ast.Attribute) and node.args and isinstance(node.args[0], ast.Constant):
                    names[receiver.attr] = node.args[0].value
        self.assertEqual(names.get('btn_minimize'), 'Minimize')
        self.assertEqual(names.get('btn_close'), 'Close')

    def test_maximize_name_tracks_restore_state(self):
        source = ENTRY.parent / 'ui/qt_native/webview_window.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CustomTitleBar')
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_set_maximize_icon')
        namespace = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), 'exec'), namespace)
        for maximized, expected in [(False, 'Maximize'), (True, 'Restore')]:
            labels = []
            button = SimpleNamespace(setIcon=lambda icon: None, setAccessibleName=labels.append, setToolTip=lambda text: None)
            target = SimpleNamespace(btn_maximize=button, _create_icon_from_svg=lambda svg: svg)
            namespace['_set_maximize_icon'](target, maximized)
            self.assertEqual(labels, [expected])


class NativeDownloadContract(unittest.TestCase):
    def _load(self, choice, path_type=Path):
        from unittest.mock import Mock
        source = ENTRY.parent / 'ui/qt_native/webview_window.py'
        tree = ast.parse(source.read_text())
        node = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_handle_download_request'), None)
        self.assertIsNotNone(node, 'Qt profile has no download handler')
        ns = {'Path': path_type, 'QFileDialog': SimpleNamespace(getSaveFileName=Mock(return_value=(choice, ''))),
              'QApplication': SimpleNamespace(activeWindow=lambda: None),
              'QStandardPaths': SimpleNamespace(StandardLocation=SimpleNamespace(DownloadLocation=1), writableLocation=lambda _: '/synthetic-downloads'),
              'QMessageBox': SimpleNamespace(warning=Mock()), 'logger': Mock()}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(source), 'exec'), ns)
        return ns

    def test_profile_wires_downloads_once(self):
        tree = ast.parse((ENTRY.parent / 'ui/qt_native/webview_window.py').read_text())
        profile = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_get_viola_profile')
        self.assertIn('profile.downloadRequested.connect(_handle_download_request)', ast.unparse(profile))

    def test_cancel_never_accepts_or_selects_a_path(self):
        from unittest.mock import Mock
        ns = self._load(''); request = Mock(); request.downloadFileName.return_value = 'chat.md'
        ns['_handle_download_request'](request)
        request.cancel.assert_called_once(); request.accept.assert_not_called(); request.setDownloadDirectory.assert_not_called()

    def test_selected_path_is_applied_before_accepting(self):
        from pathlib import PurePosixPath, PureWindowsPath
        from unittest.mock import Mock, call
        for path_type in (PurePosixPath, PureWindowsPath):
            with self.subTest(path_style=path_type.__name__):
                directory = path_type('/synthetic-downloads')
                ns = self._load(str(directory / 'renamed.md'), path_type=path_type)
                request = Mock()
                request.downloadFileName.return_value = '../../chat.md'
                ns['_handle_download_request'](request)
                self.assertEqual(ns['QFileDialog'].getSaveFileName.call_args.args[2], str(directory / 'chat.md'))
                calls = request.mock_calls
                self.assertLess(calls.index(call.setDownloadDirectory(str(directory))), calls.index(call.accept()))
                self.assertLess(calls.index(call.setDownloadFileName('renamed.md')), calls.index(call.accept()))
                request.cancel.assert_not_called()
