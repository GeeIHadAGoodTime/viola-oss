"""Writable / baked cache resolution for faster-whisper model loads.

The cloud API container runs with a read-only rootfs
(``docker-compose.cloud.yml`` ``read_only: true``); only ``/tmp`` and the
``/app/data`` volume are writable. faster-whisper's default HuggingFace cache
lives under ``~/.cache/huggingface`` which resolves to ``/app/.cache`` (HOME is
``/app``), so an unguarded ``WhisperModel("tiny.en")`` crashes at model load
with ``[Errno 30] Read-only file system: '/app/.cache'`` and, because the cloud
voice-stream startup builds the ASR engine fail-closed, the whole container
exits (2026-07-04 browser-voice enablement).

This mirrors the already-proven phone STT pattern
(``telephony.call_manager._load_phone_stt_model`` +
``_phone_stt_download_root``): pin ``download_root`` to a Viola-controlled
writable directory instead of the read-only HF default, and prefer a build-time
*baked* snapshot on the (readable) rootfs so the first turn under load never
cold-downloads. When the baked snapshot is present it is loaded with
``local_files_only=True`` so huggingface_hub performs no network calls and no
cache writes -- the read-only rootfs is never touched.

The baked root is a PROJECT-ROOT-RELATIVE path, not an absolute POSIX one.
An absolute ``/app/models/faster-whisper`` default is unreachable on Windows
(``Path("/app/...")`` resolves against the current drive), so every desktop
install failed :func:`snapshot_present`, fell through to
``local_files_only=False`` and cold-downloaded the weights from HuggingFace on
the user's first push-to-talk -- 75 MB and ~84 s on a clean VM, and an outright
failure offline or behind a TLS-inspecting proxy. Anchoring on
``core.platform.get_project_root()`` (the repo in dev, ``_internal`` in a frozen
PyInstaller install, ``/app`` in the cloud image) is the same idiom the shipped
Kokoro TTS weights already use (``KokoroTTSEngine._resolve_path``) and yields
byte-identical behavior in the container while finally resolving on Windows.

Every faster-whisper ``WhisperModel(...)`` construction in the voice pipeline
MUST route its ``download_root``/``local_files_only`` through
:func:`resolve_model_load` (enforced by the ``voice-stt-writable-model-cache``
ratchet gate).
"""

from __future__ import annotations

import os
from pathlib import Path

from core.logging_config import get_logger

logger = get_logger(__name__)

# Build-time baked faster-whisper cache, relative to the application root.
# Dockerfile.cloud pre-downloads the cloud voice model into
# ``/app/models/faster-whisper`` and viola.spec bundles the desktop model into
# ``models/faster-whisper`` inside the frozen tree; both resolve here.
# Overridable for tests and alternative image layouts.
BAKED_ROOT_ENV = "VIOLA_FASTER_WHISPER_BAKED_ROOT"
_DEFAULT_BAKED_ROOT = "models/faster-whisper"


def baked_root() -> Path:
    """Return the directory holding build-time baked faster-whisper snapshots.

    An explicit ``VIOLA_FASTER_WHISPER_BAKED_ROOT`` wins. Otherwise the default
    is resolved against the application root so it is correct on every platform
    and in every packaging mode, rather than against the process cwd (a
    Start-Menu launch's cwd is the read-only install directory).
    """
    override = os.environ.get(BAKED_ROOT_ENV)
    if override:
        return Path(override)

    from core.platform import get_project_root

    return get_project_root() / _DEFAULT_BAKED_ROOT


def writable_download_root() -> Path:
    """Return a writable faster-whisper cache under ``VIOLA_CACHE_DIR``.

    Never the read-only HF default. Also redirects HF_HOME/HF_HUB_CACHE (which
    faster-whisper/huggingface_hub also consult) onto the writable tree as
    defense-in-depth, matching the phone STT path.
    """
    from core.platform import configure_environment, get_cache_dir

    configure_environment()
    root = get_cache_dir() / "faster-whisper"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _repo_dir_name(model_name: str) -> str:
    # faster-whisper resolves a bare size (e.g. "tiny.en") to the Systran repo
    # and caches it under the HF layout ``models--Systran--faster-whisper-<size>``.
    return f"models--Systran--faster-whisper-{model_name}"


def snapshot_present(root: Path, model_name: str) -> bool:
    """True when ``root`` already contains a baked snapshot for ``model_name``."""
    # A bare filesystem path passed as the model IS the snapshot.
    if Path(model_name).is_dir():
        return True
    snapshots = root / _repo_dir_name(model_name) / "snapshots"
    if not snapshots.is_dir():
        return False
    return any(child.is_dir() for child in snapshots.iterdir())


def resolve_model_load(model_name: str) -> dict[str, object]:
    """Return read-only-safe ``WhisperModel`` cache kwargs for ``model_name``.

    - Baked snapshot present on the (readable) rootfs -> load it offline
      (``local_files_only=True``): no network, no writes, safe on a read-only
      rootfs and instant on the first turn.
    - Otherwise -> a writable ``download_root`` under ``VIOLA_CACHE_DIR`` so a
      cold download lands on the ``/app/data`` volume, never the read-only
      ``/app/.cache``.
    """
    baked = baked_root()
    if snapshot_present(baked, model_name):
        logger.info("faster-whisper: loading baked model '%s' from %s (offline)", model_name, baked)
        return {"download_root": str(baked), "local_files_only": True}
    root = writable_download_root()
    if snapshot_present(root, model_name):
        # Deliberately NOT local_files_only: unlike the baked snapshot (verified
        # at build time), a snapshot in the writable cache may be a partial
        # download, and huggingface_hub's normal path repairs that. It already
        # falls back to the cached copy when the network is unreachable, so the
        # self-healing costs nothing offline.
        logger.info("faster-whisper: model '%s' found in writable cache %s", model_name, root)
        return {"download_root": str(root), "local_files_only": False}
    logger.info(
        "faster-whisper: model '%s' is neither baked (%s) nor cached (%s); this load will "
        "download from HuggingFace and needs network",
        model_name,
        baked,
        root,
    )
    return {"download_root": str(root), "local_files_only": False}


def model_available_offline(model_name: str) -> bool:
    """True when ``model_name`` can load with no network at all.

    A load of a model that is neither baked into the build nor already in the
    writable cache must reach HuggingFace. Callers that iterate a fallback chain
    use this to avoid paying one network timeout per candidate on a machine that
    has no working network (``WhisperTranscriber._load_model``).
    """
    if snapshot_present(baked_root(), model_name):
        return True
    try:
        return snapshot_present(writable_download_root(), model_name)
    except OSError:
        # An unwritable cache dir is itself a "not available offline" answer.
        return False
