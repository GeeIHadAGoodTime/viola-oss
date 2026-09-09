"""Launches and monitors external MCP server subprocesses.

Uses the MCP SDK's stdio_client transport to connect to external
servers running as child processes communicating over stdin/stdout.

Task-affinity contract (the 2026-07-04 boot-poison fix): the SDK's
``stdio_client`` and ``ClientSession`` context managers each own an anyio
TaskGroup, and anyio cancel scopes MUST be entered and exited in the same
task. Entering them in whatever task happens to call connect (boot task,
request task doing an auto-reconnect) and exiting them later from another
task — or leaking them when startup fails — corrupts that task's cancel-scope
stack and later throws cancellation into unrelated work ("Native agent loop
cancelled" on every /v1/command until restart). Therefore every stdio server
is owned by a dedicated task (`_own_server`) that enters the transport
contexts, proves the session alive, holds them open, and unwinds them — all
inside itself. Callers only ever await a result future or signal a stop
event; no transport cancel scope ever touches a caller's task.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

import anyio

from core.logging_config import get_logger

from .types import ServerConfig

logger = get_logger(__name__)

# Browser MCP subprocess (Playwright + Chromium) cold-start takes 20-30s.
# 10s was too short and caused intermittent "Unknown tool" failures when the
# MCP handshake timed out before Chromium finished initialising.
# 60s gives ample headroom for slow/cold Windows machines.
_STARTUP_TIMEOUT = 60.0
_SHUTDOWN_TIMEOUT = 5.0

# ---------------------------------------------------------------------------
# Allowlist of executables permitted for external MCP server commands.
# Only these base executables (without path) are accepted. This prevents
# arbitrary command execution via the VIOLA_MCP_EXTERNAL_SERVERS config
# or the EXTERNAL_PROVIDERS registry.
# ---------------------------------------------------------------------------
_ALLOWED_MCP_EXECUTABLES: set[str] = {
    "npx",
    "node",
    "python",
    "python3",
    "uvx",
    "pip",
    "npm",
}

# SEC-033 (sweep 2026-06-09): the executable allowlist alone is security theater
# — every allowlisted interpreter runs arbitrary INLINE code via standard args
# (`python -c "<code>"`, `node -e "<code>"`, `node -p`, `python -`), turning an
# "approve npx <package>" mental model into "approve arbitrary code." We block
# the inline-code arg forms outright; they have no legitimate MCP-server launch
# use (a real server is a module/package/script, not an inline string). Package
# launches (`npx <pkg>`, `uvx <pkg>`, `python -m <server>`, `node <script>`)
# stay allowed but are now gated by an honest approval prompt (command+args
# rendered) and the irreversible-confirmation gate.
_INLINE_CODE_ARG_FLAGS: dict[str, set[str]] = {
    "python": {"-c", "-", "--command"},
    "python3": {"-c", "-", "--command"},
    "node": {"-e", "--eval", "-p", "--print", "--require", "-r"},
    "npx": {"-e", "--eval", "-c", "--call", "-p", "--package=-"},
}

# Environment keys that can inject code into an otherwise-clean command
# (SEC-033 rider / SEC-036): `NODE_OPTIONS=--require=evil.js`,
# `PYTHONSTARTUP`, `PYTHONPATH`, `LD_PRELOAD`, etc. Rejected on the
# externally-supplied env overlay.
_DANGEROUS_ENV_KEYS: frozenset[str] = frozenset(
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


def _base_exe_name(command: str) -> str:
    """Extract the base executable name (no path, no .exe/.cmd/.bat suffix)."""
    from pathlib import PurePath

    exe_name = PurePath(command).name.lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if exe_name.endswith(suffix):
            exe_name = exe_name[: -len(suffix)]
    return exe_name


def _validate_server_command(config: ServerConfig) -> str | None:
    """Validate that a server config's command uses an allowed executable.

    Returns None if valid, or a human-readable error string if rejected.
    The check extracts the base executable name (stripping path and .exe
    suffix on Windows) and compares against the allowlist, then rejects
    inline-code arg forms and dangerous environment overrides.
    """
    import sys as _sys

    command = config.command.strip()
    if not command:
        return "Server '%s' requires 'command' in config" % config.name

    exe_name = _base_exe_name(command)

    # Also allow sys.executable (the Python running Viola) —
    # used for built-in servers like the browser server.
    own_python = _base_exe_name(_sys.executable)

    if exe_name not in _ALLOWED_MCP_EXECUTABLES and exe_name != own_python:
        return "Rejected MCP server '%s': executable '%s' is not in the allowlist %s" % (
            config.name,
            command,
            sorted(_ALLOWED_MCP_EXECUTABLES),
        )

    # Reject inline-code argument forms for the interpreter (python -c, node -e,
    # etc.) — these execute arbitrary code with no named package/module/script.
    inline_flags = _INLINE_CODE_ARG_FLAGS.get(exe_name)
    if exe_name == own_python:
        inline_flags = inline_flags or _INLINE_CODE_ARG_FLAGS.get("python")
    if inline_flags:
        for raw_arg in config.args or []:
            arg = str(raw_arg).strip()
            arg_token = arg.split("=", 1)[0].lower() if arg.startswith("-") else arg.lower()
            if arg_token in inline_flags or arg.lower() in inline_flags:
                return (
                    "Rejected MCP server '%s': inline-code argument '%s' is not allowed "
                    "(an MCP server must be a package/module/script, not inline code)." % (config.name, arg)
                )

    # Reject code-injecting environment overrides on the externally-supplied env.
    for env_key in config.env or {}:
        if str(env_key).strip().upper() in _DANGEROUS_ENV_KEYS:
            return (
                "Rejected MCP server '%s': environment variable '%s' can inject code "
                "into the subprocess and is not allowed." % (config.name, env_key)
            )

    return None


class _StdioServerHandle:
    """Handle to the owner task holding one stdio server's contexts open.

    Returned by ``MCPServerLauncher.launch`` as the ``streams_ctx`` element of
    its tuple and passed back to ``stop``. The transport contexts themselves
    never leave the owner task — see the module docstring.
    """

    __slots__ = ("owner_task", "server_name", "stop_event")

    def __init__(self, server_name: str, owner_task: asyncio.Task, stop_event: asyncio.Event) -> None:
        self.server_name = server_name
        self.owner_task = owner_task
        self.stop_event = stop_event


class MCPServerLauncher:
    """Launches external MCP servers as subprocesses using stdio transport."""

    async def launch(self, config: ServerConfig) -> tuple[Any, Any]:
        """Launch an external MCP server subprocess and connect via stdio.

        Spawns a dedicated owner task that enters the MCP SDK's stdio_client
        and ClientSession contexts, proves the session alive via
        ``initialize()``, and holds them open until ``stop`` is called. All
        transport cancel scopes are entered AND exited inside that owner task,
        so a failed launch is fully self-contained: nothing is registered
        against the caller's task and no cancellation state can leak into it.

        Args:
            config: Server configuration with command, args, env.

        Returns:
            Tuple of (ClientSession, _StdioServerHandle). The handle must be
            passed back to ``stop`` to shut the server down.

        Raises:
            RuntimeError: If the server fails to initialize within timeout or
                startup is torn down before the session is proven alive.
            FileNotFoundError: If the command is not found.
            ValueError: If the config is missing a command or uses a
                disallowed executable.
        """
        from mcp import StdioServerParameters

        if not config.command:
            raise ValueError("Server '%s' requires 'command' in config" % config.name)

        # Validate the executable against the allowlist
        rejection = _validate_server_command(config)
        if rejection:
            logger.warning("Rejected MCP server command: %s", config.command)
            raise ValueError(rejection)

        # Always pass the full parent environment so subprocess inherits
        # VIOLA_* vars (e.g. VIOLA_BROWSER_ALLOW_LOCALHOST).  The MCP
        # library's get_default_environment() strips most vars when env=None.
        merged_env = dict(os.environ)
        if config.env:
            merged_env.update(config.env)

        params = StdioServerParameters(
            command=config.command,
            args=config.args or [],
            env=merged_env,
        )

        logger.info(
            "Launching external MCP server '%s': %s %s",
            config.name,
            config.command,
            " ".join(config.args or []),
        )

        ready: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        stop_event = asyncio.Event()
        owner_task = asyncio.create_task(
            self._own_server(config.name, params, ready, stop_event),
            name="mcp-stdio-owner:%s" % config.name,
        )
        handle = _StdioServerHandle(config.name, owner_task, stop_event)

        try:
            session = await ready
        except BaseException as exc:
            # Startup failed (or this caller was torn down). The owner task
            # unwinds every transport context inside itself — wait for that
            # to finish so nothing is left behind, then surface the failure
            # as an ordinary Exception the hub's connect guards can catch.
            await self._stop_owner(handle)
            if ready.done() and not ready.cancelled():
                # If our own await was interrupted after the owner recorded a
                # startup failure, mark it retrieved so it never surfaces as
                # an unretrieved-future warning.
                with contextlib.suppress(BaseException):
                    ready.exception()
            if isinstance(exc, asyncio.CancelledError):
                task = asyncio.current_task()
                if task is not None and task.cancelling() == 0:
                    # Cancellation thrown into this task without anyone
                    # requesting it — leaked cleanup noise, not a real cancel.
                    # Surface it as a catchable startup failure instead.
                    raise RuntimeError(
                        "External MCP server '%s' startup was torn down before completing" % config.name
                    ) from exc
                # Genuine cancellation of the connecting task (e.g. boot
                # aborted): cleanup is done, honor the cancellation.
                raise
            # Ordinary startup failures propagate as-is; other BaseExceptions
            # (KeyboardInterrupt/SystemExit) must never be masked.
            raise

        logger.info("External MCP server '%s' connected", config.name)
        return session, handle

    async def _own_server(
        self,
        server_name: str,
        params: Any,
        ready: asyncio.Future,
        stop_event: asyncio.Event,
    ) -> None:
        """Own one stdio server's whole transport lifecycle in a single task.

        Enters stdio_client + ClientSession, initializes the session, reports
        it on ``ready``, then parks on ``stop_event``. Every exit path —
        startup failure, stop signal, cancellation — unwinds the contexts in
        this same task, which is the structural fix for the cancel-scope
        poison (anyio scopes are task-affine).
        """
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        try:
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    try:
                        with anyio.fail_after(_STARTUP_TIMEOUT):
                            await session.initialize()
                    except TimeoutError as exc:
                        raise RuntimeError(
                            "External MCP server '%s' failed to initialize within %ss" % (server_name, _STARTUP_TIMEOUT)
                        ) from exc
                    if not ready.done():  # pragma: no branch
                        ready.set_result(session)
                    await stop_event.wait()
        except BaseException as exc:
            if not ready.done():
                if isinstance(exc, Exception):
                    ready.set_exception(exc)
                else:
                    ready.set_exception(
                        RuntimeError("External MCP server '%s' startup failed: %r" % (server_name, exc))
                    )
            else:
                logger.debug("Stdio owner for '%s' ended with: %r", server_name, exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
            # Otherwise swallow: the failure has been delivered via `ready`
            # (startup) or is shutdown noise after a healthy run — either way
            # it must not become an unretrieved task exception.

    async def _stop_owner(self, handle: _StdioServerHandle) -> None:
        """Signal an owner task to unwind and wait for it to finish."""
        handle.stop_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(handle.owner_task), timeout=_SHUTDOWN_TIMEOUT)
        except TimeoutError:
            logger.warning(
                "Stdio owner for '%s' did not stop within %ss — cancelling",
                handle.server_name,
                _SHUTDOWN_TIMEOUT,
            )
            handle.owner_task.cancel()
            await asyncio.gather(handle.owner_task, return_exceptions=True)
        except asyncio.CancelledError:
            # Our caller is being cancelled: make sure the owner still dies,
            # then propagate the cancellation.
            handle.owner_task.cancel()
            await asyncio.gather(handle.owner_task, return_exceptions=True)
            raise
        except Exception as exc:  # noqa: BLE001, RUF100 - owner task swallows its own stop errors
            logger.debug("Stdio owner for '%s' stop error: %r", handle.server_name, exc)

    async def stop(self, streams_ctx: Any, session: Any) -> None:
        """Gracefully stop an external server connection.

        Args:
            streams_ctx: The _StdioServerHandle returned by ``launch`` (or, in
                legacy/mocked callers, a raw async context manager to close).
            session: The ClientSession to close (owned by the owner task when
                ``streams_ctx`` is a handle).
        """
        if isinstance(streams_ctx, _StdioServerHandle):
            await self._stop_owner(streams_ctx)
            return

        # Legacy path for callers holding raw contexts (unit-test mocks).
        try:
            await asyncio.wait_for(
                session.__aexit__(None, None, None),
                timeout=_SHUTDOWN_TIMEOUT,
            )
        except (asyncio.CancelledError, OSError, RuntimeError, TimeoutError) as exc:
            # MCP SDK may raise CancelledError or RuntimeError from cancel scope
            # crossing task boundaries — suppress during cleanup.
            logger.debug("Session cleanup error: %s", exc)

        try:
            await asyncio.wait_for(
                streams_ctx.__aexit__(None, None, None),
                timeout=_SHUTDOWN_TIMEOUT,
            )
        except (asyncio.CancelledError, OSError, RuntimeError, TimeoutError) as exc:
            logger.debug("Streams cleanup error: %s", exc)

    async def health_check(self, session: Any) -> bool:
        """Ping the server to check responsiveness.

        Args:
            session: The ClientSession to check.

        Returns:
            True if the server responds to list_tools within 5 seconds.
        """
        try:
            await asyncio.wait_for(session.list_tools(), timeout=5.0)
            return True
        except (OSError, RuntimeError, TimeoutError):
            return False
