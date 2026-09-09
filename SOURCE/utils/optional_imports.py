"""
utils/optional_imports.py

Consistent handling of optional dependencies.
"""

from __future__ import annotations

import importlib
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


def try_import(module_name: str, package: str | None = None) -> Any | None:
    """
    Attempt to import a module, returning None if unavailable.

    Args:
        module_name: Name of module to import
        package: Optional package for relative imports

    Returns:
        The imported module or None if import fails
    """
    try:
        return importlib.import_module(module_name, package)
    except (ImportError, OSError) as exc:
        logger.debug("Optional dependency %s not available: %s", module_name, exc)
        return None


def require_import(module_name: str, feature: str) -> Any:
    """
    Import a module or raise a clear error about the missing dependency.

    Args:
        module_name: Name of module to import
        feature: Human-readable feature name for error message

    Returns:
        The imported module

    Raises:
        ImportError: If module is not available, with helpful message
    """
    module = try_import(module_name)
    if module is None:
        raise ImportError(f"{feature} requires '{module_name}'. " f"Install with: pip install {module_name}")
    return module


# Pre-imported optional dependencies for convenience.
#
# NOTE (#1663): importing ``sounddevice`` runs its one-time PortAudio
# ``Pa_Initialize``. It is deliberately NOT wrapped in
# ``audio_core.portaudio_guard.sounddevice_guard`` here: this module is imported
# during very early bootstrap, and pulling in ``audio_core.portaudio_guard`` would
# force the whole heavy ``audio_core`` package to initialize at that point (and risk
# an import cycle). CPython's per-module import lock already serializes this init
# against any other ``import sounddevice``. The genuine, high-churn race — repeated
# ``sounddevice.query_devices()`` full enumerations on several long-lived boot
# threads — is what gets serialized, at those call sites, under the shared
# ``PORTAUDIO_LOCK`` (see ``sounddevice_guard`` and ``check_portaudio_guarded.py``).
sounddevice = try_import("sounddevice")
pyaudio = try_import("pyaudio")
vlc = try_import("vlc")
onnxruntime = try_import("onnxruntime")
