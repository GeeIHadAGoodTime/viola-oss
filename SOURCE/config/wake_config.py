"""
Wake Configuration Utilities

Model path resolution only. NO config resolution logic here.
Config is resolved ONCE in bootstrap/factory.py and written to settings.
All other code reads from settings.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


def _is_valid_model_file(path: Path) -> bool:
    """
    Check if a model file exists and is valid (non-empty).

    Args:
        path: Path to the model file

    Returns:
        True if file exists and has content, False otherwise
    """
    if not path.exists():
        return False
    # ONNX models should be at least a few KB - 1KB is a safe minimum
    MIN_MODEL_SIZE = 1024
    try:
        size = path.stat().st_size
        if size < MIN_MODEL_SIZE:
            logger.warning("Model file too small (%d bytes), likely corrupted: %s", size, path)
            return False
        return True
    except OSError as e:
        logger.warning("Failed to stat model file %s: %s", path, e)
        return False


def get_violawake_model_path(version: int | None = None) -> Path | None:
    """
    Get path to ViolaWake model, respecting version settings.

    Args:
        version: Specific version to load, or None for latest

    Returns:
        Path to model file, or None if not found
    """
    # This file is at config/wake_config.py
    # Models are at violawake_data/trained_models/
    project_root = Path(__file__).parent.parent
    models_dir = project_root / "violawake_data" / "trained_models"
    versions_file = models_dir / "versions.json"

    if versions_file.exists():
        try:
            with open(versions_file) as f:
                registry = json.load(f)

            # Determine which version to load
            target_version = version
            if target_version is None:
                target_version = registry.get("latest")

            if target_version is not None:
                # Find version entry
                for v in registry["versions"]:
                    if v["version"] == target_version:
                        model_path = models_dir / v["filename"]
                        if _is_valid_model_file(model_path):
                            logger.debug(
                                "Using ViolaWake model v%s: %s",
                                target_version,
                                model_path,
                            )
                            return model_path
                        break

                # Fallback: try direct filename
                fallback_path = models_dir / f"viola_v{target_version}.onnx"
                if _is_valid_model_file(fallback_path):
                    logger.debug("Using ViolaWake model (fallback): %s", fallback_path)
                    return fallback_path
        except Exception as e:
            logger.warning("Failed to load versions.json: %s", e)

    # Try v4 first, then v3, then v2, then v1
    for version_num in [4, 3, 2, 1]:
        model_path = models_dir / f"viola_v{version_num}.onnx"
        if _is_valid_model_file(model_path):
            logger.debug("Using ViolaWake model v%d: %s", version_num, model_path)
            return model_path

    # Final fallback: temporal CNN model (production model)
    cnn_path = models_dir / "temporal_cnn.onnx"
    if _is_valid_model_file(cnn_path):
        logger.debug("Using ViolaWake temporal_cnn model (final fallback): %s", cnn_path)
        return cnn_path

    logger.error("No valid ViolaWake model found in %s", models_dir)
    return None


def get_model_versions_info() -> dict[str, Any]:
    """
    Get information about available model versions.

    Returns:
        Dict with version info including latest, all versions, and their metrics
    """
    project_root = Path(__file__).parent.parent
    models_dir = project_root / "violawake_data" / "trained_models"
    versions_file = models_dir / "versions.json"

    if versions_file.exists():
        try:
            with open(versions_file) as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Failed to load versions.json: %s", e)

    return {"versions": [], "latest": None}


def check_pyaudio_available() -> bool:
    """Check if PyAudio is importable."""
    try:
        from importlib import import_module

        import_module("pyaudio")

        return True
    except ImportError:
        return False


__all__ = [
    "check_pyaudio_available",
    "get_model_versions_info",
    "get_violawake_model_path",
]
