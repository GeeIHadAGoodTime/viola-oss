"""Exercise actual titlebar methods; Qt dispatch runs in an isolated worker."""
import ast
import copy
import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[2] / "ui/qt_native/webview_window.py"


def methods(path, class_name, names):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    return [copy.deepcopy(n) for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]


def load_titlebar(path):
    nodes = methods(path, "CustomTitleBar", {"mousePressEvent", "mouseMoveEvent", "mouseReleaseEvent"})
    namespace = {"QMouseEvent": object, "QPoint": Point, "Qt": SimpleNamespace(MouseButton=SimpleNamespace(LeftButton=1))}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class Point:
    def __init__(self, x, y): self._x, self._y = x, y
    def x(self): return self._x
    def y(self): return self._y
    def toPoint(self): return self
    def __sub__(self, other): return Point(self._x-other._x, self._y-other._y)


class Event:
    def __init__(self, x=700, y=4, button=1, buttons=1):
        self.point, self._button, self._buttons = Point(x, y), button, buttons
        self.accepted, self.ignored = False, False
    def button(self): return self._button
    def buttons(self): return self._buttons
    def globalPosition(self): return self.point
    def accept(self): self.accepted = True
    def ignore(self): self.ignored = True


class TitlebarResizeRoutingTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_titlebar(SOURCE)
        self.moves, self.icons, self.restores = [], [], []
        self.parent = SimpleNamespace(
            _resize_edge=None, isMaximized=lambda: False, isFullScreen=lambda: False,
            mapFromGlobal=lambda point: point,
            _get_resize_edge=lambda point: "top" if point.y() < 8 else None,
            frameGeometry=lambda: SimpleNamespace(topLeft=lambda: Point(0, 0)),
            width=lambda: 1400, move=lambda point: self.moves.append((point.x(), point.y())),
            showNormal=lambda: self.restores.append(True),
        )
        self.bar = SimpleNamespace(_parent=self.parent, _drag_pos=None, _set_maximize_icon=self.icons.append)

    def call(self, name, event): self.ns[name](self.bar, event)

    def test_edge_press_defers_to_existing_parent_resize(self):
        event = Event()
        self.call("mousePressEvent", event)
        self.assertTrue(event.ignored)
        self.assertFalse(event.accepted)
        self.assertIsNone(self.bar._drag_pos)

    def test_coordinates_are_mapped_from_global_before_hit_test(self):
        self.parent.mapFromGlobal = lambda point: Point(point.x()-100, point.y()-300)
        event = Event(800, 304)
        self.call("mousePressEvent", event)
        self.assertTrue(event.ignored)

    def test_resize_move_and_release_continue_parent_routing(self):
        self.parent._resize_edge = "top"
        for name in ["mouseMoveEvent", "mouseReleaseEvent"]:
            event = Event(y=80)
            self.call(name, event)
            self.assertTrue(event.ignored, name)
            self.assertFalse(event.accepted, name)
        self.assertEqual(self.moves, [])

    def test_regular_titlebar_drag_is_unchanged(self):
        self.call("mousePressEvent", Event(y=16))
        event = Event(750, 46)
        self.call("mouseMoveEvent", event)
        self.assertEqual(self.moves, [(50, 30)])
        self.assertTrue(event.accepted)
        self.call("mouseReleaseEvent", Event())
        self.assertIsNone(self.bar._drag_pos)

    def test_maximized_top_edge_keeps_restore_drag(self):
        self.parent.isMaximized = lambda: True
        self.call("mousePressEvent", Event())
        self.call("mouseMoveEvent", Event(750, 34))
        self.assertEqual(self.restores, [True])
        self.assertEqual(self.icons, [False])
        self.assertEqual(self.moves, [(50, 18)])

    def test_fullscreen_and_right_button_do_not_move_or_start_resize(self):
        self.parent.isFullScreen = lambda: True
        self.parent._get_resize_edge = lambda point: None
        self.call("mousePressEvent", Event())
        self.call("mouseMoveEvent", Event(y=80))
        self.assertEqual(self.moves, [])
        self.bar._drag_pos = None
        event = Event(button=2, buttons=2)
        self.call("mousePressEvent", event)
        self.assertIsNone(self.bar._drag_pos)
        self.assertFalse(event.ignored)

    def test_exact_margin_boundary_remains_titlebar_drag(self):
        event = Event(y=8)
        self.call("mousePressEvent", event)
        self.assertTrue(event.accepted)
        self.assertFalse(event.ignored)


class RealQtTitlebarResizeTests(unittest.TestCase):
    def test_actual_qt_event_propagation(self):
        if importlib.util.find_spec("PySide6") is None:
            if sys.platform == "win32":
                self.fail("Windows desktop contract runtime must provide PySide6")
            self.skipTest("PySide6 unavailable; source-method contracts still run")
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--qt-worker", str(SOURCE)], env=env, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("QT_RESIZE_ROUTING_OK", result.stdout)


def qt_worker(path):
    # Extract unchanged production methods into real Qt subclasses, avoiding the
    # application's network, WebEngine, tray and audio startup side effects.
    from PySide6.QtCore import QPoint, QPointF, QRect, QEvent, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout
    namespace = dict(QPoint=QPoint, QRect=QRect, QMouseEvent=QMouseEvent, Qt=Qt, QMainWindow=QMainWindow, QWidget=QWidget)
    for original, name, base, selected in [
        ("ViolaWebViewWindow", "ResizeWindow", "QMainWindow", {"_get_resize_edge", "_update_cursor", "mousePressEvent", "mouseMoveEvent", "mouseReleaseEvent"}),
        ("CustomTitleBar", "Titlebar", "QWidget", {"mousePressEvent", "mouseMoveEvent", "mouseReleaseEvent", "mouseDoubleClickEvent", "_toggle_maximize"}),
    ]:
        node = ast.ClassDef(name=name, bases=[ast.Name(id=base, ctx=ast.Load())], keywords=[], body=methods(path, original, selected), decorator_list=[])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(path), "exec"), namespace)
    app = QApplication([])
    window = namespace["ResizeWindow"]()
    window.setWindowFlags(Qt.WindowType.FramelessWindowHint)
    window.RESIZE_MARGIN = 8
    window._resize_edge = window._resize_start_pos = window._resize_start_geometry = None
    window.setMinimumSize(1024, 600)
    window.setGeometry(100, 100, 1400, 800)
    central = QWidget(window)
    window.setCentralWidget(central)
    layout = QVBoxLayout(central)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    bar = namespace["Titlebar"](central)
    bar._parent, bar._drag_pos = window, None
    bar._set_maximize_icon = lambda value: None
    bar.setFixedHeight(32)
    layout.addWidget(bar)
    layout.addWidget(QWidget(central))
    window.show()
    app.processEvents()

    def send(kind, local, button, buttons):
        global_pos = bar.mapToGlobal(local)
        event = QMouseEvent(kind, QPointF(local), QPointF(global_pos), button, buttons, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(bar, event)

    left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
    before = QRect(window.geometry())
    send(QEvent.Type.MouseButtonPress, QPoint(700, 4), left, left)
    assert window._resize_edge == "top", "Qt did not propagate the titlebar edge press"
    send(QEvent.Type.MouseMove, QPoint(700, 54), none, left)
    assert window.height() == before.height()-50 and window.geometry().bottom() == before.bottom(), (before, window.geometry())
    send(QEvent.Type.MouseButtonRelease, QPoint(700, 4), left, none)
    assert window._resize_edge is None and bar._drag_pos is None
    before = QRect(window.geometry())
    send(QEvent.Type.MouseButtonPress, QPoint(700, 4), left, left)
    send(QEvent.Type.MouseMove, QPoint(700, 1000), none, left)
    assert window.height() >= 600 and window.width() >= 1024
    send(QEvent.Type.MouseButtonRelease, QPoint(700, 4), left, none)
    assert window._resize_edge is None
    before = QRect(window.geometry())
    send(QEvent.Type.MouseButtonPress, QPoint(700, 16), left, left)
    assert window._resize_edge is None and bar._drag_pos is not None
    send(QEvent.Type.MouseMove, QPoint(730, 36), none, left)
    assert window.size() == before.size() and window.pos() == before.topLeft()+QPoint(30, 20)
    send(QEvent.Type.MouseButtonRelease, QPoint(700, 16), left, none)
    assert bar._drag_pos is None
    send(QEvent.Type.MouseButtonPress, QPoint(700, 4), Qt.MouseButton.RightButton, Qt.MouseButton.RightButton)
    assert window._resize_edge is None and bar._drag_pos is None
    send(QEvent.Type.MouseButtonRelease, QPoint(700, 4), Qt.MouseButton.RightButton, none)
    window.showMaximized()
    app.processEvents()
    send(QEvent.Type.MouseButtonPress, QPoint(700, 4), left, left)
    assert window._resize_edge is None and bar._drag_pos is not None
    send(QEvent.Type.MouseMove, QPoint(720, 24), none, left)
    assert not window.isMaximized() and window._resize_edge is None
    send(QEvent.Type.MouseButtonRelease, QPoint(700, 16), left, none)
    window.showFullScreen()
    app.processEvents()
    before = QRect(window.geometry())
    send(QEvent.Type.MouseButtonPress, QPoint(700, 4), left, left)
    send(QEvent.Type.MouseMove, QPoint(730, 54), none, left)
    assert window.isFullScreen() and window.geometry() == before and window._resize_edge is None
    send(QEvent.Type.MouseButtonRelease, QPoint(700, 4), left, none)
    window.showNormal()
    app.processEvents()
    send(QEvent.Type.MouseButtonDblClick, QPoint(700, 16), left, left)
    assert window.isMaximized()
    send(QEvent.Type.MouseButtonDblClick, QPoint(700, 16), left, left)
    assert not window.isMaximized()
    window.close()
    app.processEvents()
    print("QT_RESIZE_ROUTING_OK: actual child-to-parent press, move, release, minimum-size and ordinary-drag paths")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--qt-worker":
        qt_worker(Path(sys.argv[2]))
    else:
        unittest.main()
