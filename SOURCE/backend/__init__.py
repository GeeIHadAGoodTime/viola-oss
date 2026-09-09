"""
Backend runtime utilities and facades for the Viola application.

This package replaces the legacy ``viola_main`` entry point by exposing
stable building blocks that are shared across the Qt desktop client and
the web server.

Imports are lazy (PEP 562 ``__getattr__``) to avoid pulling in heavy
transitive dependencies (torch, openai, ctranslate2) at package-import
time.  This saves ~2-3 s on cold start.
"""

from __future__ import annotations

__all__ = [
    "AppState",
    "Bootstrap",
    "IntentBridge",
    "MusicControllerAdapter",
    "PortSelectionError",
    "UvicornServer",
    "bootstrap",
    "build_fastapi_app",
    "resolve_listen_port",
    "run_uvicorn",
    "start_backend_server",
]

# Mapping: attribute name  →  (module_path, object_name)
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "Bootstrap": ("bootstrap.factory", "Bootstrap"),
    "UvicornServer": ("core.server_factory", "UvicornServer"),
    "AppState": ("backend.app_state", "AppState"),
    "build_fastapi_app": ("backend.fastapi_app", "build_fastapi_app"),
    "IntentBridge": ("backend.intent_bridge", "IntentBridge"),
    "MusicControllerAdapter": ("backend.music_adapter", "MusicControllerAdapter"),
    "PortSelectionError": ("backend.ports", "PortSelectionError"),
    "resolve_listen_port": ("backend.ports", "resolve_listen_port"),
    "bootstrap": ("backend.runtime", "bootstrap"),
    "run_uvicorn": ("backend.runtime", "run_uvicorn"),
    "start_backend_server": ("backend.runtime", "start_backend_server"),
}


def __getattr__(name: str) -> object:
    if name in _LAZY_IMPORTS:
        module_path, obj_name = _LAZY_IMPORTS[name]
        import importlib

        mod = importlib.import_module(module_path)
        val = getattr(mod, obj_name)
        # Cache on the module so __getattr__ is called only once per name
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
