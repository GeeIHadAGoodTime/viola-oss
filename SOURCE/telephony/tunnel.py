"""Cloudflare quick-tunnel fallback for Telnyx phone media WebSocket."""

from __future__ import annotations

import contextlib
import os
import queue
import re
import shutil
import subprocess
import threading
import time

from core.constants import TIMEOUT_SHUTDOWN, TIMEOUT_VERY_LONG
from core.logging_config import get_logger
from core.subprocess_utils import popen_silent

logger = get_logger(__name__)

_tunnel: _PhoneTunnel | None = None


class _PhoneTunnel:
    """Manage a single Cloudflare quick tunnel for emergency phone fallback."""

    def __init__(self, port: int = 8770) -> None:
        self.port = port
        self._process: subprocess.Popen[str] | None = None
        self._wss_url = ""

    @property
    def wss_url(self) -> str:
        return self._wss_url

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def ensure_running(self) -> str:
        """Start the quick tunnel if needed and return its public wss:// URL."""
        if self.is_running and self._wss_url:
            return self._wss_url

        self.stop()

        cloudflared = shutil.which("cloudflared")
        if not cloudflared:
            raise RuntimeError("cloudflared not found on PATH")

        logger.info("Starting Cloudflare quick tunnel for phone media on localhost:%d", self.port)

        null_device = "nul" if os.name == "nt" else "/dev/null"
        self._process = popen_silent(
            [
                cloudflared,
                "tunnel",
                "--config",
                null_device,
                "--url",
                "http://localhost:%d" % self.port,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        output_queue: queue.Queue[str] = queue.Queue()
        if self._process.stdout is None:
            self.stop()
            raise RuntimeError("cloudflared stdout was not available")

        reader = threading.Thread(
            target=self._enqueue_output,
            args=(self._process.stdout, output_queue),
            name="phone-cloudflared-output",
            daemon=True,
        )
        reader.start()

        url_pattern = re.compile(r"https://([a-z0-9-]+\.trycloudflare\.com)")
        deadline = time.monotonic() + TIMEOUT_VERY_LONG

        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("cloudflared exited with code %d" % self._process.returncode)

            remaining = max(0.0, deadline - time.monotonic())
            try:
                line = output_queue.get(timeout=min(TIMEOUT_SHUTDOWN, remaining))
            except queue.Empty:
                continue

            match = url_pattern.search(line)
            if match:
                self._wss_url = "wss://" + match.group(1)
                logger.info("Phone quick tunnel ready: %s", self._wss_url)
                return self._wss_url

        self.stop()
        raise RuntimeError("Failed to get quick tunnel URL after %.0fs" % TIMEOUT_VERY_LONG)

    @staticmethod
    def _enqueue_output(pipe, output_queue: queue.Queue[str]) -> None:
        for line in pipe:
            output_queue.put(line)

    def stop(self) -> None:
        """Stop the tunnel process."""
        if self._process is None:
            return

        try:
            self._process.terminate()
            self._process.wait(timeout=TIMEOUT_SHUTDOWN)
        except Exception:
            with contextlib.suppress(Exception):
                self._process.kill()
        self._process = None
        self._wss_url = ""
        logger.info("Phone quick tunnel stopped")


def get_phone_tunnel(port: int = 8770) -> _PhoneTunnel:
    """Get or create the phone tunnel singleton."""
    global _tunnel
    if _tunnel is None or _tunnel.port != port:
        if _tunnel is not None:
            _tunnel.stop()
        _tunnel = _PhoneTunnel(port)
    return _tunnel
