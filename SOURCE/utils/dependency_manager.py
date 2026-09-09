from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from config import env
from core.logging_config import get_logger
from core.subprocess_utils import run_silent

logger = get_logger(__name__)


@dataclass(frozen=True)
class DependencySpec:
    import_name: str
    packages: Sequence[str]
    reason: str


class DependencyInstallationError(RuntimeError):
    """Raised when required dependencies could not be installed automatically."""

    def __init__(self, missing: Sequence[DependencySpec]) -> None:
        message = "; ".join(f"{spec.import_name} ({', '.join(spec.packages)})" for spec in missing)
        super().__init__(message)
        self.missing = list(missing)


def _is_installed(import_name: str) -> bool:
    try:
        spec = importlib.util.find_spec(import_name)
        return spec is not None
    except Exception as e:
        logger.debug("Failed to check if '%s' is installed: %s", import_name, e, exc_info=True)
        return False


def _auto_install_enabled() -> bool:
    return env.get("VIOLA_AUTO_INSTALL", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def _install_packages(packages: Sequence[str]) -> None:
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", *packages]
    logger.info("📦 Installing missing dependency via pip: {}", " ".join(packages))
    # Security: Use explicit argument lists, no shell=True
    from config.constants import SUBPROCESS_TIMEOUT_SECONDS

    proc = run_silent(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,  # Add timeout for security
        env=dict(os.environ, PATH=os.environ.get("PATH", "")),  # Limited env
    )
    if proc.returncode != 0:
        logger.error(
            "pip install failed for %s (exit=%s)\nstdout:\n%s\nstderr:\n%s",
            packages,
            proc.returncode,
            proc.stdout,
            proc.stderr,
        )
        raise RuntimeError(f"pip install failed for {packages} (exit code {proc.returncode})")
    logger.info("✅ Installed %s", ", ".join(packages))


def ensure_dependencies(
    specs: Iterable[DependencySpec],
    *,
    auto_install: bool = True,
    strict: bool = False,
) -> list[DependencySpec]:
    """
    Ensure dependencies are importable, optionally auto-installing via pip.

    Args:
        specs: Iterable of DependencySpec definitions.
        auto_install: Attempt pip installations for missing packages.
        strict: Raise DependencyInstallationError if anything remains missing.

    Returns:
        List of specs still missing after installation attempts.
    """

    missing: list[DependencySpec] = []
    do_auto = auto_install and _auto_install_enabled()

    for spec in specs:
        if _is_installed(spec.import_name):
            continue

        if do_auto:
            try:
                _install_packages(spec.packages)
            except Exception as exc:
                logger.warning(
                    "Auto-install failed for %s (%s): %s",
                    spec.import_name,
                    ", ".join(spec.packages),
                    exc,
                )
            else:
                if _is_installed(spec.import_name):
                    continue

        missing.append(spec)

    if missing and strict:
        raise DependencyInstallationError(missing)

    return missing


# NOTE (#4786): openwakeword belongs in this list -- it is genuinely required.
#
# Ground-truthed directly against the actual shipped model
# (violawake_data/trained_models/temporal_cnn.onnx): its ONNX graph declares
# input name "embeddings" with shape (batch, 9, 96) -- the OpenWakeWord
# embedding shape, not a raw mel-spectrogram. violawake/engine.py's
# ViolaWake._load_model() detects this via _is_embedding_input_onnx() and
# routes to _load_onnx_mlp(), which constructs openwakeword.utils.AudioFeatures
# to turn raw audio into those embeddings before the classifier ever runs --
# confirmed live: a real Build Linux (AppImage) boot logged "ViolaWake engine
# pre-loaded: ViolaWake(model=temporal_cnn.onnx, backend=onnx_mlp, ...)".
# _load_onnx_mlp() raises "openwakeword is required for OWW-based models" the
# instant the package is missing -- unconditionally, for the one model every
# install actually ships, not for some optional custom-model path.
#
# requirements_linux.txt and requirements_macos.txt previously excluded
# openwakeword on a stale premise (comments claiming the production model was
# "mel/CNN" and openwakeword-free) that predates whatever retrained/replaced
# temporal_cnn.onnx with the current OWW-embedding architecture; the comments
# were never updated after the swap. Both requirements files now pin
# openwakeword to match requirements_desktop.txt, which is why Windows never
# showed this symptom. Do not remove openwakeword from here again without
# first proving (not assuming) the actually-bundled model's ONNX input shape
# via onnxruntime -- a stale comment is not evidence.
VOICE_DEPENDENCIES: Sequence[DependencySpec] = (
    DependencySpec(
        import_name="openwakeword",
        packages=("openwakeword>=0.5.0",),
        reason="Wake word detection runtime",
    ),
    DependencySpec(
        import_name="onnxruntime",
        packages=("onnxruntime>=1.17.0",),
        reason="ONNX inference backend for wake word models",
    ),
    DependencySpec(
        import_name="pyaudio",
        packages=("PyAudio",),
        reason="Microphone capture via PyAudio",
    ),
    DependencySpec(
        import_name="sounddevice",
        packages=("sounddevice",),
        reason="Audio device enumeration",
    ),
)


def ensure_voice_dependencies(
    *,
    auto_install: bool = True,
    strict: bool = False,
) -> list[DependencySpec]:
    """
    Ensure all voice dependencies (wake word + audio stack) are present.

    Args:
        auto_install: Attempt pip installation when modules are missing.
        strict: Raise DependencyInstallationError when dependencies remain missing.

    Returns:
        List of missing DependencySpec entries, empty when fully satisfied.
    """

    missing = ensure_dependencies(VOICE_DEPENDENCIES, auto_install=auto_install, strict=False)

    if missing:
        for spec in missing:
            logger.error(
                "Missing voice dependency '%s' (%s) – %s",
                spec.import_name,
                ", ".join(spec.packages),
                spec.reason,
            )
        if strict:
            raise DependencyInstallationError(missing)

    return missing


__all__ = [
    "VOICE_DEPENDENCIES",
    "DependencyInstallationError",
    "DependencySpec",
    "ensure_dependencies",
    "ensure_voice_dependencies",
]
