"""Provisioning + availability for the phone streaming-STT model.

Phase 1 of the streaming-STT rollout (decision report
`_diag/2026-07-05/streaming_stt_bakeoff/DECISION_REPORT.md`) runs the NeMo
cache-aware streaming FastConformer-transducer (1040 ms lookahead) via
sherpa-onnx as the phone lane's streaming first pass. This module owns the model
*artifact*: where it lives, whether it is present, and how to fetch it.

Provisioning is deliberately OPT-IN, not part of the default cloud image build:

- The extracted model is ~500 MB (encoder alone is 456 MB). Baking it into every
  cloud image for a feature that ships default-OFF (``VIOLA_PHONE_STT_STREAMING``
  defaults to ``off``) would bloat the artifact for no benefit.
- A missing model must NEVER break calls. ``streaming_stt_model_available()`` is a
  cheap presence check; the phone pipeline calls it and fails open to today's
  batch-whisper path when the model (or sherpa-onnx runtime) is absent.

To provision (once, on a box/image that will enable streaming):

    python scripts/download_models.py --streaming-stt

which calls :func:`ensure_streaming_stt_model` here. The tarball is pinned to a
SHA-256 and verified before extraction (fail-closed, same discipline as
``scripts/download_models.py``'s Kokoro/Piper pins) so an unverified artifact is
never installed.
"""

from __future__ import annotations

import hashlib
import os
import tarfile
import tempfile
import urllib.request
from pathlib import Path

# The winning bake-off candidate. Non-int8 export (matches the harness that
# produced the RTF 0.070 / zero-dead-air-hallucination evidence).
STREAMING_STT_MODEL_DIRNAME = "sherpa-onnx-nemo-streaming-fast-conformer-transducer-en-1040ms"

# k2-fsa/sherpa-onnx public release (asr-models tag). Re-downloadable indefinitely.
STREAMING_STT_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "sherpa-onnx-nemo-streaming-fast-conformer-transducer-en-1040ms.tar.bz2"
)
# SHA-256 of the .tar.bz2 (verified 2026-07-05, 450,226,958 bytes). Fail-closed:
# a download that does not match this pin is deleted, never installed.
STREAMING_STT_MODEL_TARBALL_SHA256 = "59047bcaa2aa24c60994fbab6d6974e478d2c9cecf0eabaca2e6b358cd99e717"  # pragma: allowlist secret - public model tarball SHA-256, not a credential.

# Files sherpa_onnx.OnlineRecognizer.from_transducer(...) needs. Presence of all
# four (non-empty) is the availability contract.
STREAMING_STT_MODEL_FILES = ("tokens.txt", "encoder.onnx", "decoder.onnx", "joiner.onnx")

_CHUNK = 1024 * 1024


def _repo_root() -> Path:
    # telephony/streaming_stt_model.py -> telephony/ -> repo root.
    return Path(__file__).resolve().parent.parent


def streaming_stt_model_dir() -> Path:
    """Directory that holds the extracted model files.

    Overridable with ``VIOLA_PHONE_STT_STREAMING_MODEL_DIR`` (absolute path to the
    extracted model directory) for tests and non-standard deployments. Default is
    ``<repo>/models/stt/<dirname>`` -- alongside ``models/tts`` and ``models/vad``.
    """
    override = os.environ.get("VIOLA_PHONE_STT_STREAMING_MODEL_DIR", "").strip()
    if override:
        return Path(override)
    return _repo_root() / "models" / "stt" / STREAMING_STT_MODEL_DIRNAME


def streaming_stt_model_paths() -> dict[str, Path]:
    """Absolute path to each required model file (may not exist yet)."""
    base = streaming_stt_model_dir()
    return {name: base / name for name in STREAMING_STT_MODEL_FILES}


def streaming_stt_model_available() -> bool:
    """True only when every required model file is present and non-empty.

    Cheap (stat only). The phone pipeline gates streaming-first-pass on this and
    fails open to batch whisper when it returns False -- a missing model never
    breaks a call.
    """
    for path in streaming_stt_model_paths().values():
        try:
            if not path.is_file() or path.stat().st_size == 0:
                return False
        except OSError:
            return False
    return True


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract ``tar`` under ``dest``, refusing any member that escapes it.

    Guards against path-traversal (``..`` / absolute) members in the archive
    (CVE-2007-4559 class). The k2-fsa tarball is trusted + checksum-pinned, but
    fail-closed extraction is cheap insurance.
    """
    dest_resolved = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if dest_resolved != target and dest_resolved not in target.parents:
            raise ValueError("Refusing to extract unsafe path from archive: %s" % member.name)
    tar.extractall(dest)  # nosec B202 - members validated above.


def ensure_streaming_stt_model(*, force: bool = False) -> Path:
    """Download + verify + extract the model if not already present.

    Returns the model directory. Raises on any failure (checksum mismatch,
    network error, missing files after extraction) -- callers that must fail open
    should check :func:`streaming_stt_model_available` instead of calling this.
    """
    model_dir = streaming_stt_model_dir()
    if not force and streaming_stt_model_available():
        return model_dir

    # Extract into models/stt/ so the archive's top-level dir lands as
    # models/stt/<dirname> (the archive is rooted at that dirname).
    stt_root = model_dir.parent
    stt_root.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(dir=stt_root, suffix=".tar.bz2", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with urllib.request.urlopen(STREAMING_STT_MODEL_URL) as response:  # nosec B310 - https URL pinned above.
            with tmp_path.open("wb") as handle:
                while True:
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)

        actual = _sha256_file(tmp_path)
        if actual != STREAMING_STT_MODEL_TARBALL_SHA256:
            raise ValueError(
                "Streaming STT model checksum mismatch: expected %s, got %s. "
                "Refusing to install an unverified model." % (STREAMING_STT_MODEL_TARBALL_SHA256, actual)
            )

        with tarfile.open(tmp_path, "r:bz2") as tar:
            _safe_extract(tar, stt_root)
    finally:
        tmp_path.unlink(missing_ok=True)

    if not streaming_stt_model_available():
        missing = [name for name, p in streaming_stt_model_paths().items() if not p.is_file()]
        raise RuntimeError(
            "Streaming STT model extraction did not produce the expected files "
            "(missing: %s) under %s" % (", ".join(missing) or "?", model_dir)
        )
    return model_dir
