"""Regression tests for the security backports in the vendored Pipecat runner."""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.testclient import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "third_party" / "pipecat" / "src"))

from pipecat.runner.run import (  # noqa: E402
    _generate_ws_token,
    _resolve_download_path,
    _setup_telephony_routes,
    _setup_webrtc_routes,
    _verify_and_consume_ws_token,
    main,
)


class PipecatRunnerDownloadSecurity(unittest.TestCase):
    def test_download_path_rejects_parent_escape_and_accepts_nested_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            downloads = root / "downloads"
            nested = downloads / "nested"
            nested.mkdir(parents=True)
            allowed = nested / "recording.wav"
            allowed.write_bytes(b"synthetic audio")
            outside = root / "private.txt"
            outside.write_text("synthetic secret", encoding="utf-8")

            self.assertEqual(
                _resolve_download_path(str(downloads), "nested/recording.wav"),
                allowed.resolve(),
            )
            with self.assertRaises(HTTPException) as blocked:
                _resolve_download_path(str(downloads), "../private.txt")
            self.assertEqual(blocked.exception.status_code, 403)

    def test_percent_encoded_separator_cannot_escape_download_route(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            downloads = root / "downloads"
            downloads.mkdir()
            (downloads / "allowed.txt").write_text("allowed", encoding="utf-8")
            (root / "private.txt").write_text("private", encoding="utf-8")
            app = FastAPI()

            class RequestModel(BaseModel):
                pass

            class RequestHandler:
                def __init__(self, **_kwargs):
                    pass

                async def close(self):
                    pass

            async def frontend(_scope, _receive, _send):
                pass

            frontend_package = types.ModuleType("pipecat_ai_small_webrtc_prebuilt")
            frontend_module = types.ModuleType("pipecat_ai_small_webrtc_prebuilt.frontend")
            frontend_module.SmallWebRTCPrebuiltUI = frontend
            connection_module = types.ModuleType("pipecat.transports.smallwebrtc.connection")
            connection_module.SmallWebRTCConnection = object
            request_module = types.ModuleType("pipecat.transports.smallwebrtc.request_handler")
            request_module.IceCandidate = RequestModel
            request_module.SmallWebRTCPatchRequest = RequestModel
            request_module.SmallWebRTCRequest = RequestModel
            request_module.SmallWebRTCRequestHandler = RequestHandler
            modules = {
                "pipecat_ai_small_webrtc_prebuilt": frontend_package,
                "pipecat_ai_small_webrtc_prebuilt.frontend": frontend_module,
                "pipecat.transports.smallwebrtc.connection": connection_module,
                "pipecat.transports.smallwebrtc.request_handler": request_module,
            }
            args = argparse.Namespace(folder=str(downloads), esp32=False, host="127.0.0.1")
            with patch.dict(sys.modules, modules):
                _setup_webrtc_routes(app, args)

            with TestClient(app) as client:
                valid = client.get("/files/allowed.txt")
                blocked = client.get("/files/..%2Fprivate.txt")

            self.assertEqual(valid.status_code, 200)
            self.assertEqual(valid.text, "allowed")
            self.assertEqual(blocked.status_code, 403)
            self.assertNotIn("private", blocked.text)


class PipecatRunnerWebSocketSecurity(unittest.TestCase):
    @staticmethod
    def _app(*, proxy: str | None = "telephony.example.invalid") -> FastAPI:
        app = FastAPI()
        values = {
            "transport": "twilio",
            "proxy": proxy,
            "host": "127.0.0.1",
            "port": 7860,
        }
        _setup_telephony_routes(app, argparse.Namespace(**values), set())
        return app

    def test_default_mode_rejects_unauthenticated_telephony_websocket(self):
        app = self._app()
        with self.assertRaises(WebSocketDisconnect) as blocked:
            with TestClient(app).websocket_connect("/ws"):
                pass
        self.assertEqual(blocked.exception.code, 4003)

    def test_token_mode_accepts_valid_once_and_rejects_replay(self):
        app = self._app()
        token = _generate_ws_token()
        with patch("pipecat.runner.run._run_telephony_bot", new=AsyncMock()):
            with TestClient(app).websocket_connect(f"/ws/{token}"):
                pass
            with self.assertRaises(WebSocketDisconnect) as replayed:
                with TestClient(app).websocket_connect(f"/ws/{token}"):
                    pass
        self.assertEqual(replayed.exception.code, 4003)

    def test_expired_token_is_rejected_and_parallel_tokens_are_distinct(self):
        used: set[str] = set()
        expired = _generate_ws_token(ttl=-1)
        first = _generate_ws_token()
        second = _generate_ws_token()

        self.assertFalse(_verify_and_consume_ws_token(used, expired))
        self.assertNotEqual(first, second)
        self.assertTrue(_verify_and_consume_ws_token(used, first))
        self.assertTrue(_verify_and_consume_ws_token(used, second))

    def test_provider_webhook_issues_a_tokenized_working_websocket_url(self):
        app = self._app()
        bootstrap = "synthetic-bootstrap-value"
        with patch("pipecat.runner.run._WS_AUTH_BOOTSTRAP_SECRET", bootstrap), TestClient(
            app
        ) as client:
            self.assertEqual(client.post("/").status_code, 403)
            response = client.post(f"/?auth={bootstrap}")
            self.assertEqual(response.status_code, 200)
            match = re.search(r"/ws/([^\"<]+)", response.text)
            self.assertIsNotNone(match)
            with patch("pipecat.runner.run._run_telephony_bot", new=AsyncMock()):
                with client.websocket_connect(f"/ws/{match.group(1)}"):
                    pass

    def test_token_issuance_requires_bootstrap_even_from_loopback(self):
        app = self._app(proxy=None)
        with TestClient(app) as client:
            self.assertEqual(client.post("/").status_code, 403)
            self.assertEqual(client.post("/start").status_code, 403)

    def test_missing_bootstrap_configuration_exits_nonzero(self):
        argv = ["bot.py", "-t", "twilio", "-x", "telephony.example.invalid"]
        with (
            patch.object(sys, "argv", argv),
            patch("pipecat.runner.run._WS_AUTH_BOOTSTRAP_CONFIGURED", False),
            self.assertRaises(SystemExit) as stopped,
        ):
            main()
        self.assertEqual(stopped.exception.code, 2)

if __name__ == "__main__":
    unittest.main()
