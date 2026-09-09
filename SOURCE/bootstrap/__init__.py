"""Bootstrap package exports for the live startup path."""

# Cache cleaner import (actual cleaning gated by VIOLA_CLEAN_CACHE=1 env var)
# Previously ran unconditionally on every import, costing ~3-5s per startup
# by forcing Python to recompile all .pyc files from source.
import os as _os

from core.platform import configure_environment

configure_environment()

from core.constants import VIOLA_VERSION
from core.console import console

if _os.environ.get("VIOLA_CLEAN_CACHE") == "1":
    try:
        from bootstrap.cache_cleaner import startup_cache_clean

        _cache_result = startup_cache_clean()
    except Exception as _cache_err:
        import sys

        console(f"Warning: Startup cache clean failed: {_cache_err}", file=sys.stderr)

from bootstrap.cache_cleaner import (
    auto_fix_import_error,
    clear_pycache_directories,
    invalidate_and_reimport,
    invalidate_module,
    startup_cache_clean,
)

__all__ = [
    "__version__",
    "auto_fix_import_error",
    "clear_pycache_directories",
    "invalidate_and_reimport",
    "invalidate_module",
    "startup_cache_clean",
]

__version__ = VIOLA_VERSION
