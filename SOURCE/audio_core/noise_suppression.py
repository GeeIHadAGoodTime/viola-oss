"""Optional DeepFilterNet suppression in an explicitly configured interpreter.

Apply only in the STT path, never upstream of ViolaWake: the wake model requires
raw post-AEC audio. The dedicated worker isolates DeepFilter's NumPy 1 dependency
from the application. Missing configuration or worker failure passes audio through.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from core.constants import SAMPLE_RATE_16K
from core.logging_config import get_logger

if TYPE_CHECKING:
    import numpy as np

logger = get_logger(__name__)
_global_instance: NoiseSuppressor | None = None
_global_lock = threading.Lock()
_WORKER_START_TIMEOUT = 60
_WORKER_RESPONSE_TIMEOUT = 30


class NoiseSuppressor:
    def __init__(self, *, enabled: bool = True, post_filter: bool = True) -> None:
        self._enabled = enabled
        self._post_filter = post_filter
        self._worker: subprocess.Popen | None = None
        self._responses: queue.Queue = queue.Queue()
        self._load_attempted = False
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def is_loaded(self) -> bool:
        return self._worker is not None and self._worker.poll() is None

    @staticmethod
    def _read_responses(worker: subprocess.Popen, responses: queue.Queue) -> None:
        try:
            assert worker.stdout is not None
            for line in worker.stdout:
                responses.put(json.loads(line))
        except (OSError, ValueError):
            logger.exception("Cannot read DeepFilter worker response")
        finally:
            responses.put({"error": "DeepFilter worker exited"})

    def _stop(self) -> None:
        worker, self._worker = self._worker, None
        if worker is None:
            return
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)
        for stream in (worker.stdin, worker.stdout):
            if stream is not None:
                stream.close()

    def _ensure_loaded(self) -> bool:
        if self.is_loaded:
            return True
        if self._load_attempted:
            return False
        self._load_attempted = True
        executable = os.environ.get("VIOLA_DEEPFILTER_PYTHON", "")
        if not executable or not Path(executable).is_file():
            logger.warning("DeepFilter disabled: set VIOLA_DEEPFILTER_PYTHON to its dedicated environment interpreter")
            return False
        environment = dict(os.environ)
        environment["VIOLA_DEEPFILTER_POST_FILTER"] = "1" if self._post_filter else "0"
        environment["PYTHONNOUSERSITE"] = "1"
        # A host PYTHONPATH must not pull the application's NumPy into the worker.
        environment.pop("PYTHONPATH", None)
        try:
            self._responses = queue.Queue()
            self._worker = subprocess.Popen(
                [executable, "-u", str(Path(__file__).with_name("deepfilter_worker.py"))],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            threading.Thread(target=self._read_responses, args=(self._worker, self._responses), daemon=True).start()
            ready = self._responses.get(timeout=_WORKER_START_TIMEOUT)
            if not ready.get("ready"):
                raise RuntimeError(ready.get("error", "DeepFilter worker did not initialize"))
            return True
        except (OSError, RuntimeError, queue.Empty):
            logger.exception("DeepFilter worker unavailable; preserving original audio")
            self._stop()
            return False

    def process(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE_16K) -> np.ndarray:
        """Return a cleaned 1-D chunk with the original dtype and length."""
        import numpy as np

        if not self._enabled or audio.ndim != 1 or not audio.size or sample_rate <= 0:
            return audio
        with self._lock:
            if not self._ensure_loaded():
                return audio
            try:
                values = audio.astype(np.float32)
                if audio.dtype == np.int16:
                    values /= 32768.0
                if not np.isfinite(values).all():
                    return audio
                request = {
                    "sample_rate": sample_rate,
                    "audio": base64.b64encode(values.astype("<f4").tobytes()).decode("ascii"),
                }
                assert self._worker is not None and self._worker.stdin is not None
                self._worker.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
                self._worker.stdin.flush()
                response = self._responses.get(timeout=_WORKER_RESPONSE_TIMEOUT)
                if response.get("error"):
                    raise RuntimeError(response["error"])
                result = np.frombuffer(base64.b64decode(response["audio"], validate=True), dtype="<f4")
                if result.size != audio.size or not np.isfinite(result).all():
                    raise ValueError("Invalid DeepFilter output shape or samples")
                if audio.dtype == np.int16:
                    return np.rint(np.clip(result, -1, 32767 / 32768) * 32768).astype(np.int16)
                return result.astype(audio.dtype)
            except (OSError, RuntimeError, ValueError, KeyError, queue.Empty):
                logger.exception("DeepFilter processing failed; preserving original audio")
                self._stop()
                return audio

    def release(self) -> None:
        with self._lock:
            self._stop()
            self._load_attempted = False


def get_noise_suppressor(*, enabled: bool = True) -> NoiseSuppressor:
    global _global_instance
    with _global_lock:
        if _global_instance is None:
            _global_instance = NoiseSuppressor(enabled=enabled)
        return _global_instance


__all__ = ["NoiseSuppressor", "get_noise_suppressor"]
