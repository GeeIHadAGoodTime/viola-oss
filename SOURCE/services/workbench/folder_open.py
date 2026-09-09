"""Open the user's Workbench folder in the OS file explorer."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


def get_user_folder_path(user_id: str) -> Path:
    """Return ``<data_dir>/users/<account>/workbench`` for *user_id*."""
    from services.workbench.dir import get_workbench_dir

    return get_workbench_dir(user_id).root


def _is_desktop_runtime() -> bool:
    try:
        from config.settings import settings

        runtime = (getattr(settings, "runtime_profile", "") or "").lower()
        if runtime in {"cloud", "server"}:
            return False
    except Exception:
        pass
    return sys.platform in {"win32", "darwin", "linux"}


def get_storage_mode() -> str:
    """Return the active Workbench storage mode."""
    return "folder"


def open_user_folder(user_id: str) -> dict[str, Any]:
    """Open the user's Workbench folder and return a structured payload."""
    folder = get_user_folder_path(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "storage_mode": "folder",
        "ui_action": "open_memory_panel",
    }
    if not _is_desktop_runtime():
        payload["opened"] = False
        payload["reason"] = "not_desktop"
        return payload
    try:
        if sys.platform == "win32":
            os.startfile(str(folder))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
        payload["opened"] = True
    except OSError as exc:
        logger.exception("Failed to open Workbench folder for user")
        payload["opened"] = False
        payload["reason"] = "os_error"
        payload["error"] = str(exc)
    return payload


__all__ = ["get_storage_mode", "get_user_folder_path", "open_user_folder"]
