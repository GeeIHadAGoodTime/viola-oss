"""Shared environment-variable safety rules for child processes."""

from __future__ import annotations


# These variables can alter which code an otherwise-approved interpreter or
# dynamic executable loads before its named entry point runs.  Keep this list
# shared by every subprocess boundary so a launcher cannot accidentally accept
# an injection mechanism that another launcher already rejects.
CODE_INJECTION_ENV_KEYS: frozenset[str] = frozenset(
    {
        "NODE_OPTIONS",
        "PYTHONSTARTUP",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONEXECUTABLE",
        "PYTHONWARNINGS",
        "BASH_ENV",
        "ENV",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "DYLD_FRAMEWORK_PATH",
    }
)


def is_code_injection_env_key(key: object) -> bool:
    """Return whether *key* can inject code into a child process."""

    return str(key or "").strip().upper() in CODE_INJECTION_ENV_KEYS


__all__ = ["CODE_INJECTION_ENV_KEYS", "is_code_injection_env_key"]
