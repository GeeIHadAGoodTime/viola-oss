import ast
import builtins
import gc
import unittest
from pathlib import Path
from types import SimpleNamespace

ENTRY = Path(__file__).resolve().parents[2] / "viola_qt.py"


def load(
    platform="win32", name="__main__", argv=None, hook=False, missing=False,
    gc_state=None, native_error=None, gc_module=None,
):
    tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
    node = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_preload_windows_ui_runtime"),
        None,
    )
    assert node is not None, "Windows native preload missing"
    imports = []
    checkpoints = []
    original = builtins.__import__
    gc_state = gc_state if gc_state is not None else {"enabled": True, "actions": [], "during": []}

    def disable_gc():
        gc_state["actions"].append("disable")
        gc_state["enabled"] = False

    def enable_gc():
        gc_state["actions"].append("enable")
        gc_state["enabled"] = True

    def checkpoint(name):
        checkpoints.append(name)
        if gc_module is not None:
            gc_state.setdefault("checkpoint_states", []).append(gc_module.isenabled())

    def importer(module, *args, **kwargs):
        if module == "sys":
            return SimpleNamespace(platform=platform, argv=argv or ["ViolaApp.exe"])
        if module == "gc":
            if gc_module is not None:
                gc_state["gc_imports"] = gc_state.get("gc_imports", 0) + 1
                return gc_module
            return SimpleNamespace(isenabled=lambda: gc_state["enabled"], disable=disable_gc, enable=enable_gc)
        if module == "win32ui":
            imports.append(module)
            gc_state["during"].append(gc_module.isenabled() if gc_module is not None else gc_state["enabled"])
            if gc_module is not None:
                gc_state["during_thresholds"] = gc_module.get_threshold()
            if native_error is not None:
                raise native_error
            if missing:
                raise ImportError("controlled missing optional desktop runtime")
            return object()
        return original(module, *args, **kwargs)

    ns = {
        "_sys": SimpleNamespace(platform=platform, argv=argv or ["ViolaApp.exe"]),
        "__name__": name,
        "_is_velopack_hook_invocation": lambda: hook,
        "_boot_checkpoint": checkpoint,
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

    def test_gc_is_paused_only_for_the_import_and_restored(self):
        state = {"enabled": True, "actions": [], "during": []}
        load(gc_state=state)
        self.assertEqual(state, {"enabled": True, "actions": ["disable", "enable"], "during": [False]})

    def test_already_disabled_gc_is_not_enabled_by_startup(self):
        state = {"enabled": False, "actions": [], "during": []}
        load(gc_state=state)
        self.assertEqual(state, {"enabled": False, "actions": [], "during": [False]})

    def test_optional_import_failure_restores_collection(self):
        state = {"enabled": True, "actions": [], "during": []}
        _, checkpoints = load(missing=True, gc_state=state)
        self.assertTrue(state["enabled"])
        self.assertEqual(state["actions"], ["disable", "enable"])
        self.assertEqual(checkpoints[-1], "03b-win32ui-preload-unavailable")

    def test_unexpected_import_error_propagates_after_gc_restoration(self):
        state = {"enabled": True, "actions": [], "during": []}
        with self.assertRaisesRegex(RuntimeError, "controlled native failure"):
            load(native_error=RuntimeError("controlled native failure"), gc_state=state)
        self.assertTrue(state["enabled"])
        self.assertEqual(state["actions"], ["disable", "enable"])

    def test_helper_invocations_do_not_touch_collection(self):
        state = {"enabled": True, "actions": [], "during": []}
        load(hook=True, gc_state=state)
        self.assertEqual(state, {"enabled": True, "actions": [], "during": []})

    def test_real_gc_state_and_thresholds_survive_all_import_outcomes(self):
        original_enabled = gc.isenabled()
        thresholds = gc.get_threshold()
        outcomes = [
            None, ImportError("optional missing"), RuntimeError("unexpected"), SystemExit(7), KeyboardInterrupt(),
        ]
        try:
            for initially_enabled in (True, False):
                for error in outcomes:
                    with self.subTest(enabled=initially_enabled, error=type(error).__name__):
                        (gc.enable if initially_enabled else gc.disable)()
                        state = {"enabled": initially_enabled, "actions": [], "during": []}
                        if error is not None and not isinstance(error, ImportError):
                            with self.assertRaises(type(error)) as caught:
                                load(gc_state=state, native_error=error, gc_module=gc)
                            self.assertIs(caught.exception, error)
                            self.assertEqual(state["checkpoint_states"], [initially_enabled])
                        else:
                            _, checkpoints = load(gc_state=state, native_error=error, gc_module=gc)
                            expected = (
                                "03b-win32ui-preload-unavailable" if error is not None
                                else "03b-win32ui-preload-complete"
                            )
                            self.assertEqual(checkpoints[-1], expected)
                            self.assertEqual(state["checkpoint_states"], [initially_enabled, initially_enabled])
                        self.assertEqual(state["during"], [False])
                        self.assertEqual(state["during_thresholds"], thresholds)
                        self.assertEqual(gc.isenabled(), initially_enabled)
                        self.assertEqual(gc.get_threshold(), thresholds)
        finally:
            (gc.enable if original_enabled else gc.disable)()

    def test_skipped_routes_leave_real_gc_untouched(self):
        original_enabled = gc.isenabled()
        thresholds = gc.get_threshold()
        cases = [
            {"platform": "linux"}, {"platform": "darwin"}, {"name": "__mp_main__"},
            {"name": "viola_qt"}, {"argv": ["ViolaApp.exe", "--multiprocessing-fork"]}, {"hook": True},
        ]
        try:
            for initially_enabled in (True, False):
                for kwargs in cases:
                    with self.subTest(enabled=initially_enabled, **kwargs):
                        (gc.enable if initially_enabled else gc.disable)()
                        state = {"enabled": initially_enabled, "actions": [], "during": []}
                        self.assertEqual(load(gc_state=state, gc_module=gc, **kwargs), ([], []))
                        self.assertEqual(state, {"enabled": initially_enabled, "actions": [], "during": []})
                        self.assertEqual(gc.isenabled(), initially_enabled)
                        self.assertEqual(gc.get_threshold(), thresholds)
        finally:
            (gc.enable if original_enabled else gc.disable)()

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


class StartupSurfaceVisibilityTests(unittest.TestCase):
    """Exercise real startup wiring with inert window/signal seams, never Qt UI.

    QWidget starts hidden. The seam tracks explicit show/hide calls, while the
    selected entrypoint statements and loading/error/retry handlers are real.
    This proves call ordering, not Windows first-paint or hardware readiness.
    """

    def _startup(self, failure=None):
        import html
        from types import MethodType
        from unittest.mock import Mock

        events = []

        class SignalSeam:
            def __init__(self):
                self.callbacks = []

            def connect(self, callback):
                self.callbacks.append(callback)

            def emit(self, *args):
                for callback in list(self.callbacks):
                    callback(*args)

        class WindowSeam:
            def __init__(self):
                self.visible = False
                self.html = "local loading"
                self._react_ui_loaded = False
                self._react_ui_load_url = None
                self._bootstrap = None
                self._coordinator = None
                self.webview = SimpleNamespace(setHtml=self._set_html)
                self._force_quit = False
                self._tray_icon = SimpleNamespace(isVisible=lambda: True)

            def _set_html(self, value):
                self.html = value
                events.append("html")

            def show(self):
                self.visible = True
                events.append("show")

            def hide(self):
                self.visible = False
                events.append("hide")

            def showNormal(self):
                self.show()

            def activateWindow(self):
                pass

            def raise_(self):
                pass

            def _load_react_ui(self):
                self._react_ui_loaded = True
                events.append("navigate-ready-ui")

        window = WindowSeam()
        source = ENTRY.parent / "ui/qt_native/webview_window.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ViolaWebViewWindow")
        methods = [
            n
            for n in cls.body
            if isinstance(n, ast.FunctionDef)
            and n.name
            in {
                "attach_startup_coordinator",
                "_on_backend_failed",
                "_on_backend_ready",
                "_retry_startup",
                "closeEvent",
                "_restore_from_tray",
            }
        ]
        namespace = {
            "html": html,
            "logger": Mock(),
            "_STARTUP_RETRY_URL": "viola://startup-retry",
            "_LOADING_HTML": "local loading",
        }
        original_import = builtins.__import__

        def import_settings(name, *args, **kwargs):
            if name == "ui.settings_manager":
                return SimpleNamespace(get_settings_manager=lambda: SimpleNamespace(get=lambda *args: True))
            return original_import(name, *args, **kwargs)

        namespace["__builtins__"] = {**vars(builtins), "__import__": import_settings}
        exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"), namespace)
        for method in methods:
            setattr(window, method.name, MethodType(namespace[method.name], window))

        class CoordinatorSeam:
            def __init__(self):
                self.ready = SignalSeam()
                self.failed = SignalSeam()
                self.retry_calls = 0
                self.terminal = False

            def start(self):
                events.append("start")
                if failure:
                    self.terminal = True
                    self.failed.emit(failure, {"detail": "controlled <startup> failure"})

            def retry(self):
                self.retry_calls += 1
                if not self.terminal:
                    return False
                self.terminal = False
                return True

        coordinator = CoordinatorSeam()
        main_tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
        main = next(n for n in main_tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        start = next(
            i
            for i, n in enumerate(main.body)
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "early_window" for t in n.targets)
        )
        end = next(i for i in range(start + 1, len(main.body)) if isinstance(main.body[i], ast.If))
        statements = main.body[start:end]
        execution = {
            "_ensure_window": lambda: window,
            "window_holder": {"window": window},
            "_attach_window_startup": lambda w: (
                w.attach_startup_coordinator(coordinator) if w._coordinator is None else None
            ),
            "coordinator": coordinator,
            "logger": Mock(),
            "run_in_background": lambda *args: None,
            "_preload_phone_stt_at_startup": lambda: None,
        }
        for name in (
            "_wire_video_widget",
            "_wire_browser_webview",
            "_wire_browser_overlay_controller",
            "_wire_frame_streamer",
            "_wire_cdp_browser_server",
            "_attach_update_scheduler",
        ):
            execution[name] = lambda *args: None
        main_ready = next(n for n in main.body if isinstance(n, ast.FunctionDef) and n.name == "_on_backend_ready")
        exec(compile(ast.Module(body=[main_ready], type_ignores=[]), str(ENTRY), "exec"), execution)
        coordinator.ready.connect(execution["_on_backend_ready"])
        exec(compile(ast.Module(body=statements, type_ignores=[]), str(ENTRY), "exec"), execution)
        return window, coordinator, events

    def test_normal_start_shows_local_loading_without_waiting_for_backend_ready(self):
        window, coordinator, events = self._startup()
        assert window.visible, "normal startup must show its local loading surface"
        assert not window._react_ui_loaded
        assert events == ["start", "show"]
        coordinator.ready.emit(object())
        assert window._react_ui_loaded
        assert events[-1] == "navigate-ready-ui"

    def test_failed_backend_stays_visible_and_retains_a_real_retry_surface(self):
        for phase in ("backend_start", "readiness_timeout"):
            with self.subTest(phase=phase):
                window, coordinator, events = self._startup(failure=phase)
                assert window.visible, "startup failure must not leave a hidden process"
                assert not window._react_ui_loaded
                assert "Backend Failed" in window.html
                assert "controlled &lt;startup&gt; failure" in window.html
                assert 'href="viola://startup-retry"' in window.html
                assert events == ["start", "html", "show"]
                window._retry_startup()
                assert coordinator.retry_calls == 1
                assert window.html == "local loading"
                assert window.visible
                assert not window._react_ui_loaded
                coordinator.ready.emit(object())
                assert window._react_ui_loaded

    def test_later_startup_failure_remains_visible_without_navigating_unready_http(self):
        window, coordinator, events = self._startup()
        coordinator.terminal = True
        coordinator.failed.emit("readiness_timeout", {"detail": "not ready"})
        assert window.visible
        assert "Backend Failed" in window.html
        assert "navigate-ready-ui" not in events

    def test_retry_while_starting_does_not_reset_or_duplicate_the_surface(self):
        window, coordinator, events = self._startup()
        window._retry_startup()
        assert coordinator.retry_calls == 1
        assert events == ["start", "show"]
        assert not window._react_ui_loaded

    def test_close_to_tray_before_ready_stays_hidden_until_explicit_restore(self):
        from unittest.mock import Mock

        window, coordinator, events = self._startup()
        event = Mock()
        window.closeEvent(event)
        event.ignore.assert_called_once_with()
        assert not window.visible
        coordinator.ready.emit(object())
        assert not window.visible
        assert window._react_ui_loaded
        assert events.count("show") == 1
        window._restore_from_tray()
        assert window.visible
        assert events.count("show") == 2

    def test_hidden_failure_and_retry_do_not_override_user_dismissal(self):
        from unittest.mock import Mock

        window, coordinator, events = self._startup()
        window.closeEvent(Mock())
        coordinator.terminal = True
        coordinator.failed.emit("readiness_timeout", {"detail": "not ready"})
        assert not window.visible and "Backend Failed" in window.html
        window._retry_startup()
        coordinator.ready.emit(object())
        assert not window.visible and window._react_ui_loaded
        assert events.count("show") == 1

    def test_show_occurs_before_event_loop_without_processing_events_or_relaxing_readiness(self):
        tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        calls = [n for n in ast.walk(main) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        early_show = next(
            n
            for n in calls
            if isinstance(n.func.value, ast.Name) and n.func.value.id == "early_window" and n.func.attr == "show"
        )
        app_exec = next(
            n
            for n in calls
            if isinstance(n.func.value, ast.Name) and n.func.value.id == "app" and n.func.attr == "exec"
        )
        assert early_show.lineno < app_exec.lineno
        assert not any(n.func.attr == "processEvents" for n in calls)
