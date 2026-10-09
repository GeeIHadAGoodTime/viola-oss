"""Run actual credential/navigation methods with inert Qt and clock seams.

These contracts verify token freshness/order without launching Qt, opening a
socket, loading persistent credentials, or claiming native desktop acceptance.
"""
import ast
import builtins
import hashlib
import hmac
import json
import secrets
import unittest
from pathlib import Path
from threading import RLock
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]


def methods(path, class_name, names, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    selected = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(selected) == len(names)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return {name: namespace[name] for name in names}


class StartupWebSocketCredentialTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        config = SimpleNamespace(auth_api_key="synthetic-local-key", auth_token_secret="synthetic-signing-key")
        auth_path = ROOT / "ui/security/auth.py"
        auth_tree = ast.parse(auth_path.read_text(encoding="utf-8"))
        ttl = next(n.value.value for n in auth_tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "WS_AUTH_TOKEN_TTL_SECONDS" for t in n.targets))
        namespace = {
            "hashlib": hashlib, "hmac": hmac, "secrets": secrets,
            "time": SimpleNamespace(time=lambda: self.now), "log": Mock(),
            "WS_AUTH_TOKEN_TTL_SECONDS": ttl, "WebSocketAuthTokenClaims": SimpleNamespace,
            "_ws_token_lock": RLock(), "_ws_identity_claims": {},
            "_consumed_ws_nonces": {}, "_nonce_cleanup_time": 0,
        }
        auth_methods = methods(auth_path, "AuthenticationPlugin", {
            "generate_ws_auth_token", "consume_ws_auth_token", "_consume_ws_token_once",
        }, namespace)
        plugin_type = type("RealTokenMethods", (), auth_methods)
        self.auth = plugin_type()
        self.auth.config = config

        class Script:
            InjectionPoint = SimpleNamespace(DocumentCreation=1)
            ScriptWorldId = SimpleNamespace(MainWorld=0)

            def setName(self, value): self.name = value
            def setInjectionPoint(self, value): self.point = value
            def setWorldId(self, value): self.world = value
            def setRunsOnSubFrames(self, value): self.subframes = value
            def setSourceCode(self, value): self.source = value

        self.scripts = []
        self.navigations = []
        collection = SimpleNamespace(
            find=lambda name: [s for s in self.scripts if s.name == name],
            remove=self.scripts.remove, insert=self.scripts.append,
        )
        self.window = SimpleNamespace(
            port=8756, _CREDENTIALS_SCRIPT_NAME="viola_desktop_credentials",
            _console_page=SimpleNamespace(scripts=lambda: collection),
            _react_ui_loaded=False, _react_ui_load_url=None,
            _build_shell_url=lambda: "http://localhost:8756/static/react/index.html",
            webview=SimpleNamespace(setUrl=lambda url: self.navigations.append((url, self.current_token()))),
        )
        webview_path = ROOT / "ui/qt_native/webview_window.py"
        window_methods = methods(webview_path, "ViolaWebViewWindow", {
            "_build_credentials_js", "_install_or_refresh_credentials_script",
            "_load_react_ui", "_on_backend_ready", "_refresh_credentials_after_load",
        }, {"json": json, "logger": Mock(), "LOCALHOST_NAME": "localhost", "QUrl": str})
        for name, method in window_methods.items():
            setattr(self.window, name, MethodType(method, self.window))

        original_import = builtins.__import__
        modules = {
            "config.settings": SimpleNamespace(settings=SimpleNamespace(ssl_enabled=False, cloud_url="")),
            "ui.security.auth": SimpleNamespace(AuthenticationPlugin=lambda _: self.auth),
            "ui.security.config": SimpleNamespace(get_security_config=lambda: config),
            "PySide6.QtWebEngineCore": SimpleNamespace(QWebEngineScript=Script),
        }
        self.imports = patch("builtins.__import__", side_effect=lambda name, *args, **kwargs:
                             modules[name] if name in modules else original_import(name, *args, **kwargs))
        self.imports.start()
        self.addCleanup(self.imports.stop)
        self.window._install_or_refresh_credentials_script()
        self.window._refresh_credentials_after_load(True)

    def current_token(self):
        script = self.scripts[0]
        line = next(line for line in script.source.splitlines() if line.startswith("window.__VIOLA_WS_AUTH_TOKEN__ = "))
        return json.loads(line.split(" = ", 1)[1].removesuffix(";"))

    def test_delayed_readiness_refreshes_token_before_first_shell_navigation(self):
        previous = self.current_token()
        self.now += 40
        self.assertIsNone(self.auth.consume_ws_auth_token(previous))
        self.window._on_backend_ready(object())
        self.assertEqual(len(self.navigations), 1)
        token = self.navigations[0][1]
        self.assertNotEqual(token, previous)
        self.assertIsNotNone(self.auth.consume_ws_auth_token(token))
        self.assertIsNone(self.auth.consume_ws_auth_token(token), "tickets must remain single-use")

    def test_retry_after_failed_shell_load_also_refreshes_credentials(self):
        self.window._load_react_ui()
        first = self.navigations[0][1]
        self.window._react_ui_load_url = None
        self.now += 90
        self.window._load_react_ui()
        self.assertEqual(len(self.navigations), 2)
        self.assertIsNotNone(self.auth.consume_ws_auth_token(self.navigations[1][1]))
        self.assertIsNone(self.auth.consume_ws_auth_token(first))

    def test_duplicate_readiness_does_not_replace_inflight_credentials_or_navigate(self):
        self.window._load_react_ui()
        first = self.current_token()
        self.now += 1
        self.window._on_backend_ready(object())
        self.assertEqual(len(self.navigations), 1)
        self.assertEqual(self.current_token(), first)
        self.window._react_ui_load_url = None
        self.window._react_ui_loaded = True
        self.window._load_react_ui()
        self.assertEqual(len(self.navigations), 1)
        self.assertEqual(self.current_token(), first)

    def test_refresh_keeps_one_top_frame_document_creation_script(self):
        self.now += 40
        self.window._load_react_ui()
        self.assertEqual(len(self.scripts), 1)
        script = self.scripts[0]
        self.assertEqual(script.point, 1)
        self.assertEqual(script.world, 0)
        self.assertFalse(script.subframes)
        self.assertIn('window.__VIOLA_BASE_URL__ = "http://localhost:8756";', script.source)


if __name__ == "__main__":
    unittest.main()
