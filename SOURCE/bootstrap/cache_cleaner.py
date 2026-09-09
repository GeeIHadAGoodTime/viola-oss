"""
bootstrap/cache_cleaner.py
==========================

Automatic Python bytecode cache cleaner for self-healing startup.

This module ensures stale .pyc files don't cause import errors by:
1. Clearing all __pycache__ directories on startup (optional)
2. Invalidating specific modules that have known issues
3. Providing utilities for runtime cache invalidation

The AI debugger can call these functions to self-heal import issues
without requiring manual intervention.
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from core.logging_config import get_logger
from core.platform import get_project_root

logger = get_logger(__name__)

# Track whether we've already cleaned on this startup
_CACHE_CLEANED_THIS_SESSION = False


def clear_pycache_directories(
    root_dir: Path | None = None,
    exclude_dirs: list[str] | None = None,
) -> int:
    """
    Recursively delete all __pycache__ directories.

    Args:
        root_dir: Starting directory (defaults to project root)
        exclude_dirs: Directory names to skip (e.g., ['.venv', 'node_modules'])

    Returns:
        Number of __pycache__ directories removed
    """
    if root_dir is None:
        root_dir = get_project_root()

    if exclude_dirs is None:
        exclude_dirs = [".venv", ".git", "node_modules", ".mypy_cache", ".pytest_cache"]

    removed_count = 0

    for dirpath, dirnames, _ in os.walk(root_dir):
        # Skip excluded directories
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]

        for dirname in dirnames:
            if dirname == "__pycache__":
                cache_path = Path(dirpath) / dirname
                try:
                    shutil.rmtree(cache_path)
                    removed_count += 1
                    logger.debug("Removed cache: %s", cache_path)
                except Exception as e:
                    logger.warning("Failed to remove %s: %s", cache_path, e)

    return removed_count


def invalidate_module(module_name: str) -> bool:
    """
    Invalidate a specific module's cache and force reimport.

    Args:
        module_name: Fully qualified module name (e.g., 'diagnostics.ai_debugger')

    Returns:
        True if module was successfully invalidated
    """
    try:
        # Remove from sys.modules
        modules_to_remove = [name for name in sys.modules if name == module_name or name.startswith(f"{module_name}.")]

        for name in modules_to_remove:
            del sys.modules[name]
            logger.debug("Invalidated module: %s", name)

        # Invalidate import caches
        importlib.invalidate_caches()

        return True
    except Exception as e:
        logger.warning("Failed to invalidate %s: %s", module_name, e)
        return False


def invalidate_and_reimport(module_name: str) -> ModuleType | None:
    """
    Invalidate a module and reimport it fresh.

    Args:
        module_name: Fully qualified module name

    Returns:
        The freshly imported module, or None on failure
    """
    invalidate_module(module_name)

    try:
        return importlib.import_module(module_name)
    except Exception as e:
        logger.error("Failed to reimport %s: %s", module_name, e)
        return None


def clear_module_pycache(module_name: str) -> bool:
    """
    Clear the __pycache__ for a specific module's directory.

    Args:
        module_name: Module name (e.g., 'diagnostics' or 'diagnostics.ai_debugger')

    Returns:
        True if cache was cleared
    """
    try:
        # Convert module name to path
        parts = module_name.split(".")
        module_dir = get_project_root() / Path(*parts[:-1]) if len(parts) > 1 else get_project_root() / parts[0]

        # Handle both package and module cases
        if not module_dir.is_dir():
            module_dir = module_dir.parent

        cache_dir = module_dir / "__pycache__"
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
            logger.info("Cleared cache for %s: %s", module_name, cache_dir)
            return True
        return False
    except Exception as e:
        logger.warning("Failed to clear cache for %s: %s", module_name, e)
        return False


def startup_cache_clean(force: bool = False) -> dict[str, Any]:
    """
    Perform startup cache cleaning.

    This is called automatically on application startup to prevent
    stale bytecode issues. It only runs once per session unless forced.

    Args:
        force: Force cleaning even if already done this session

    Returns:
        Dict with cleaning results
    """
    global _CACHE_CLEANED_THIS_SESSION

    if _CACHE_CLEANED_THIS_SESSION and not force:
        return {"status": "skipped", "reason": "already_cleaned_this_session"}

    results: dict[str, Any] = {
        "status": "completed",
        "caches_removed": 0,
        "modules_invalidated": [],
    }

    # Clear all pycache directories
    results["caches_removed"] = clear_pycache_directories()

    # Invalidate known problematic modules
    problematic_modules = [
        "diagnostics.ai_debugger",
        "services.llm.provider_router",
        "services.llm.llm_router",
    ]

    for module in problematic_modules:
        if module in sys.modules:
            invalidate_module(module)
            results["modules_invalidated"].append(module)

    # Clear Python's import caches
    importlib.invalidate_caches()

    _CACHE_CLEANED_THIS_SESSION = True

    logger.info(
        "Startup cache clean complete: %s caches removed, %s modules invalidated",
        results["caches_removed"],
        len(results["modules_invalidated"]),
    )

    return results


def diagnose_import_error(module_name: str, error: Exception) -> dict[str, Any]:
    """
    Diagnose an import error and suggest fixes.

    Args:
        module_name: Module that failed to import
        error: The exception that was raised

    Returns:
        Dict with diagnosis and recommended actions
    """
    error_str = str(error)
    diagnosis = {
        "module": module_name,
        "error": error_str,
        "likely_cause": "unknown",
        "recommended_action": None,
        "auto_fixable": False,
    }

    if "cannot import name" in error_str:
        diagnosis["likely_cause"] = "stale_bytecode_cache"
        diagnosis["recommended_action"] = "clear_cache_and_reimport"
        diagnosis["auto_fixable"] = True
    elif "No module named" in error_str:
        diagnosis["likely_cause"] = "missing_module"
        diagnosis["recommended_action"] = "install_dependency"
        diagnosis["auto_fixable"] = False
    elif "circular import" in error_str.lower():
        diagnosis["likely_cause"] = "circular_import"
        diagnosis["recommended_action"] = "refactor_imports"
        diagnosis["auto_fixable"] = False

    return diagnosis


def auto_fix_import_error(module_name: str, error: Exception) -> bool:
    """
    Attempt to automatically fix an import error.

    Args:
        module_name: Module that failed to import
        error: The exception that was raised

    Returns:
        True if fix was successful
    """
    diagnosis = diagnose_import_error(module_name, error)

    if not diagnosis["auto_fixable"]:
        logger.warning("Import error not auto-fixable: %s", diagnosis["likely_cause"])
        return False

    if diagnosis["recommended_action"] == "clear_cache_and_reimport":
        logger.info("Auto-fixing stale cache for %s", module_name)

        # Clear the module's pycache
        clear_module_pycache(module_name)

        # Invalidate and reimport
        module = invalidate_and_reimport(module_name)

        if module is not None:
            logger.info("Successfully auto-fixed %s", module_name)
            return True

    return False


__all__ = [
    "auto_fix_import_error",
    "clear_module_pycache",
    "clear_pycache_directories",
    "diagnose_import_error",
    "invalidate_and_reimport",
    "invalidate_module",
    "startup_cache_clean",
]
