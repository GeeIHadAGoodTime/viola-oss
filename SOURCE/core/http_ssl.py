"""One shared TLS (SSL) context per process for all httpx clients.

httpx builds a fresh ``ssl.SSLContext`` for every ``Client`` it constructs
(``create_ssl_context`` -> ``ssl.create_default_context``, ~150-280ms, GIL-held).
This codebase constructs httpx clients per call across 50+ sites, so a single user
action builds several fresh contexts back-to-back -- each a GIL hold that freezes
the desktop UI, because the backend and the Qt GUI share one Python interpreter
lock. Measured live (py-spy dump, 2026-06-30): after the memory-encryption fix
removed the louder cause, ``create_default_context`` (ssl.py:770) became the
dominant sustained GIL hold during a music play (123-276ms holds).

The TLS context is a pure function of ``(verify, cert, trust_env)``, so memoizing
httpx's context factory yields one context per distinct config per process, reused
by every client -- no behavior change, only the expensive build is shared. Same
library-shim pattern this codebase already uses for codex-auth.

Install once, early, before any httpx client is created (wired in
``backend.runtime.run_uvicorn``). Idempotent.
"""

from __future__ import annotations

import importlib
import threading

_lock = threading.Lock()
_installed = False

# httpx imports create_ssl_context by name into its transport module
# (``from .._config import create_ssl_context``), so that binding -- not just
# ``httpx._config`` -- is what actually gets called. Patch every module that holds
# a reference to the original.
_TARGET_MODULES = ("httpx._config", "httpx._transports.default")


def install_shared_ssl_context() -> bool:
    """Memoize httpx's per-client TLS context creation. Idempotent.

    Returns True if the shim is active (installed now or already installed),
    False if httpx isn't importable or its factory could not be located.
    """
    global _installed
    with _lock:
        if _installed:
            return True
        try:
            modules = [importlib.import_module(name) for name in _TARGET_MODULES]
        except ImportError:
            return False
        cfg = modules[0]
        orig = getattr(cfg, "create_ssl_context", None)
        if orig is None:
            return False
        if getattr(orig, "_viola_shared", False):
            _installed = True
            return True

        cache: dict = {}
        cache_lock = threading.Lock()

        def shared_create_ssl_context(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            try:
                hash(key)
            except TypeError:
                # An unhashable arg (e.g. verify is already an SSLContext) -> passthrough.
                return orig(*args, **kwargs)
            with cache_lock:
                ctx = cache.get(key)
                if ctx is None:
                    ctx = orig(*args, **kwargs)
                    cache[key] = ctx
                return ctx

        shared_create_ssl_context._viola_shared = True  # type: ignore[attr-defined]

        patched = 0
        for mod in modules:
            if getattr(mod, "create_ssl_context", None) is orig:
                mod.create_ssl_context = shared_create_ssl_context  # type: ignore[attr-defined]
                patched += 1

        _installed = patched > 0
        return _installed
