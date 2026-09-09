"""Self-management tools for the agent.

Lets Viola read her own update status, bootstrap external providers
(Codex), and install packages.

Update path safety note (S10-UPDATE-001, 2026-05-22)
-----------------------------------------------------
The legacy ``check_for_updates`` and ``update_viola`` agent tools were
deleted. They ran ``git fetch`` / ``git stash`` / ``git pull origin
main`` / ``pip install`` / ``git stash pop`` against the user's working
tree -- a surface that has no meaning for the packaged installer end
users actually run. Concretely the old tools could:

* silently lose uncommitted user work when ``git stash pop`` hit a
  merge conflict (the legacy code logged "conflict — check manually"
  and returned ok=True);
* rebase a user on a feature branch onto ``main`` because the pull
  hardcoded ``origin main``;
* bypass the signed-installer + Authenticode + SHA-256 release-smoke
  checks that protect the production installer.

Claude Code (our parity reference) does not expose update as an agent
tool either -- ``claude update`` is a CLI command, not a chat action.

What replaces it: a read-only ``update_status`` action that reads the
public manifest via ``utils.update_checker.check_for_update`` and
steers the user toward manual reinstall with the signed Windows
installer. The manifest still enforces ``min_supported`` and
``max_version`` so the desktop can warn or block unsafe versions.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from intent.tool_types import ToolResult
from intent.tools.shell import sanitized_env

logger = get_logger(__name__)

# Viola project root (two levels up from intent/tools/)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Package name validation: must start with an alphanumeric character,
# followed by alphanumeric, hyphens, underscores, dots.  Optionally
# includes extras (e.g. "package[extra]") and version specifiers
# (including compound ranges like ">=1.0,<2.0").
# This strict pattern prevents pip argument injection (e.g. "--index-url")
# and shell metacharacter injection (e.g. "; rm -rf /").
_VALID_PACKAGE_NAME = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9_\-\.]*(\[.*\])?((>=|<=|==|!=|~=|>|<)[a-zA-Z0-9\.\*]+(,(>=|<=|==|!=|~=|>|<)[a-zA-Z0-9\.\*]+)*)?$"
)

# Shell metacharacters that must never appear in a package name.
# Note: > and < are excluded from this set because they appear in
# valid pip version specifiers (e.g. ">=1.0", "<2.0"). The regex
# pattern _VALID_PACKAGE_NAME constrains their usage to safe contexts.
_SHELL_METACHARACTERS = {";", "|", "&", "$", "`", "(", ")", "{", "}", "!"}

_MAX_OUTPUT = 4000

# Server name validation: alphanumeric, hyphens, underscores; must start with
# alphanumeric.  Keeps names safe for use as namespace prefixes.
_VALID_SERVER_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]*$")

# Hub reference injected by AIController at startup (same pattern as
# intent/tools/delegation.py).  Gives self-management tools live access to
# the MCP client hub without importing it at module level.
_mcp_hub: Any = None


def set_mcp_hub(hub: Any) -> None:
    """Wire the MCP hub reference for self-management operations.

    Called once during core-tools server wiring (alongside
    ``delegation.set_mcp_hub``) so register_mcp_server and
    list_mcp_servers can interact with the live hub.
    """
    global _mcp_hub
    _mcp_hub = hub


async def _run(cmd: str | list[str], cwd: str | None = None, timeout: float = 60.0) -> tuple[int, str, str]:
    """Run a subprocess and return (returncode, stdout, stderr).

    Accepts either a command string (split via shlex) or an explicit list
    of arguments.  Always uses create_subprocess_exec (no shell) to prevent
    shell injection.
    """
    if isinstance(cmd, str):
        args = shlex.split(cmd, posix=(sys.platform != "win32"))
    else:
        args = cmd
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or str(_PROJECT_ROOT),
        env=sanitized_env(),
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return (
        proc.returncode or 0,
        (stdout.decode("utf-8", errors="replace") if stdout else "")[:_MAX_OUTPUT],
        (stderr.decode("utf-8", errors="replace") if stderr else "")[:_MAX_OUTPUT],
    )


async def update_status() -> ToolResult:
    """Report the current Viola version and whether an update is available.

    This is a read-only check against the public update manifest — it does
    NOT modify the working tree, does NOT shell out, and does NOT trigger
    an install. Launch updates are manual reinstalls through the signed
    Windows installer. The agent's job here is to report status so the
    user can decide; it must never replicate an install path.

    Surfaces:
        * ``available`` (bool) — newer release than the local build.
        * ``current_version`` / ``latest_version``.
        * ``min_supported`` — the force-update floor.
        * ``required`` (bool) — set when current build is below
          ``min_supported``.
        * ``max_version`` / ``max_version_message`` — server-side
          rollback flag from the manifest.
        * ``max_version_issue`` (bool) — current build is strictly
          above the server's max_version cap; user should be told a
          known issue affects their build.
        * ``reason`` — when no update is offered, why (``frozen``,
          ``rollout_paused``, ``max_version``, ``below_user_minimum``).
        * ``message`` — human-facing summary that steers the user
          toward the signed installer download, never promising the
          agent will apply the install.
    """
    # check_for_update raises httpx errors on network failure; treat
    # them as a non-fatal "couldn't check" so the agent can report it
    # to the user instead of looking broken.
    import httpx

    from utils.update_checker import check_for_update

    try:
        # The user asked, right now -- bypass the background poll's short-TTL
        # manifest cache and its failure cooldown so the answer is live, and so a
        # "check for updates" right after fixing the wifi is not told to wait.
        result = await asyncio.to_thread(lambda: check_for_update(force_refresh=True))
    except httpx.HTTPError as exc:
        return ToolResult(
            ok=False,
            error="Could not reach the update server: %s" % exc,
        )
    except Exception as exc:
        return ToolResult(
            ok=False,
            error="Update check failed: %s" % exc,
        )

    current_version = str(result.get("current_version") or "")
    latest_version = str(result.get("latest_version") or "")
    available = bool(result.get("available"))
    required = bool(result.get("required"))
    max_version_issue = bool(result.get("max_version_issue"))
    max_version_message = str(result.get("max_version_message") or "")
    reason = str(result.get("reason") or "")

    if max_version_issue:
        message = (
            "Your build %s has a known issue%s. "
            "Download the signed installer from useviola.com/download to reinstall the recommended version."
        ) % (
            current_version,
            (": " + max_version_message) if max_version_message else "",
        )
    elif required:
        message = (
            "A required update is available (current: %s, latest: %s). "
            "Download the signed installer from useviola.com/download and reinstall manually."
        ) % (current_version, latest_version)
    elif available:
        message = (
            "An update is available (current: %s, latest: %s). "
            "Download the signed installer from useviola.com/download when you're ready; "
            "Viola will not apply it for you."
        ) % (current_version, latest_version)
    elif reason == "frozen":
        message = "Viola is up to date. The newer release is currently frozen by the team."
    elif reason == "rollout_paused":
        message = "Viola is up to date. The newer release is paused for staged rollout."
    elif reason == "below_user_minimum":
        message = "Viola is up to date for your pinned channel — newer releases are below your minimum version."
    else:
        message = "Viola is up to date (version %s)." % current_version

    data = dict(result)
    data["message"] = message
    return ToolResult(ok=True, data=data)


async def setup_codex(confirmed: bool = False) -> ToolResult:
    """Install and configure OpenAI Codex for task delegation.

    Checks for Node.js, installs Codex if missing, and enables it
    in the .env file.  The only manual step is ``codex login``.

    Args:
        confirmed: Must be True to actually write VIOLA_CODEX_ENABLED=true
            to .env (#2776). The filesystem write tool protects .env from
            generic writes; this setup path bypasses that protection, so it
            needs its own confirmation gate rather than mutating infra
            config unprompted. Mirrors the install_package M14 pattern.
    """
    steps: list[str] = []

    try:
        # 1. Check Node.js
        rc, out, _ = await _run("node --version", timeout=10.0)
        if rc != 0:
            # Nothing was installed and nothing was configured. ``installed:
            # False`` one level down did not reach the envelope, so the tool
            # reported a successful setup for a prerequisite check that failed
            # before any work started.
            return ToolResult(
                ok=False,
                error=(
                    "Node.js is not installed, so nothing was set up. Install Node.js 22+ from "
                    "https://nodejs.org and run Codex setup again."
                ),
                error_category="DEPENDENCY_MISSING",
                data={
                    "installed": False,
                    "codex_enabled": False,
                    "steps": steps,
                    "missing_dependency": "node",
                },
            )
        node_version = out.strip()
        steps.append("Node.js: %s" % node_version)

        # 2. Check if Codex is installed
        rc, out, _ = await _run("npx codex --version", timeout=15.0)
        if rc != 0:
            # Install Codex
            rc, out, err = await _run("npm install -g @openai/codex", timeout=60.0)
            if rc != 0:
                return ToolResult(
                    ok=False,
                    error="Failed to install Codex: %s" % (err or out)[:300],
                    data={"steps": steps},
                )
            steps.append("Codex: installed globally")
        else:
            steps.append("Codex: already installed (%s)" % out.strip()[:40])

        # 3. Enable in .env
        env_path = _PROJECT_ROOT / ".env"
        env_content = ""
        if env_path.exists():
            env_content = env_path.read_text(encoding="utf-8")

        if "VIOLA_CODEX_ENABLED=true" not in env_content:
            if not confirmed:
                return ToolResult(
                    ok=False,
                    error="confirmation_required",
                    data={
                        "installed": True,
                        "steps": steps,
                        "confirmation_required": True,
                        "message": (
                            "Codex and Node.js are ready. About to enable Codex delegation by adding "
                            "VIOLA_CODEX_ENABLED=true to .env. To proceed, call "
                            "self_manage(action='setup_codex', confirmed=True)."
                        ),
                    },
                )
            with open(env_path, "a", encoding="utf-8") as f:
                f.write("\n# Codex delegation (added by setup_codex)\nVIOLA_CODEX_ENABLED=true\n")
            steps.append(".env: VIOLA_CODEX_ENABLED=true added")
        else:
            steps.append(".env: VIOLA_CODEX_ENABLED already set")

        return ToolResult(
            ok=True,
            data={
                "installed": True,
                "steps": steps,
                "message": (
                    "Codex is installed and enabled. "
                    "Please run 'codex login' in your terminal to sign in "
                    "with your ChatGPT account, then restart me."
                ),
            },
        )
    except TimeoutError:
        # ``_run`` waits on communicate() with a timeout; it does not kill the
        # child, so npm may still be installing after this returns. Reporting a
        # flat failure asserts an outcome nobody observed -- the step that timed
        # out can still finish, and a retry would then hit an install that is
        # already there. Say the outcome is unknown instead of guessing.
        logger.warning("setup_codex timed out; the step that timed out may still be running")
        return ToolResult(
            ok=False,
            error="Codex setup timed out. The step that timed out may still be running, so its outcome is unknown.",
            error_category="setup_outcome_unknown",
            unverified=True,
            data={"steps": steps, "outcome_known": False},
        )
    except OSError as exc:
        # ``steps`` names exactly what completed before the failure; anything
        # not in it was not established, so no summary flag is invented here.
        return ToolResult(ok=False, error="Setup failed: %s" % exc, data={"steps": steps})


async def install_package(package: str, manager: str = "pip", confirmed: bool = False) -> ToolResult:
    """Install a Python or Node package.

    Args:
        package: Package name (e.g., "requests", "lodash").
                 Must be alphanumeric with hyphens/dots only — no shell injection.
        manager: "pip" or "npm" (default: pip).
        confirmed: If True, skip the user confirmation prompt (M14 fix).
    """
    # --- Sanitize and validate package name ---
    package = package.strip()

    # Reject flags / arguments (pip argument injection e.g. "--index-url")
    if package.startswith("-") or package.startswith("--"):
        logger.warning("Rejected package name containing flags: %s", package)
        return ToolResult(
            ok=False,
            error="Invalid package name: flags/arguments are not allowed ('%s')" % package,
        )

    # Reject shell metacharacters
    for ch in _SHELL_METACHARACTERS:
        if ch in package:
            logger.warning(
                "Rejected package name containing shell metacharacter '%s': %s",
                ch,
                package,
            )
            return ToolResult(
                ok=False,
                error="Invalid package name: contains disallowed character '%s'" % ch,
            )

    # Reject names that don't match the strict pattern
    if not package or not _VALID_PACKAGE_NAME.match(package):
        logger.warning("Rejected invalid package name: %s", package)
        return ToolResult(ok=False, error="Invalid package name: %s" % package)

    manager = manager.lower().strip()
    if manager not in ("pip", "npm"):
        return ToolResult(ok=False, error="Unsupported package manager: %s (use pip or npm)" % manager)

    # --- Confirmation prompt for unknown pip packages (M14 fix) ---
    # Only applies to pip; npm packages can't be checked via importlib.
    # Check if the pip package is already installed to avoid re-prompting.
    import importlib.util

    _module_name = package.replace("-", "_").split("[")[0].split("==")[0]
    try:
        _already_installed = importlib.util.find_spec(_module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        # A dotted module name whose parent package isn't importable (e.g.
        # "foo.bar" when "foo" isn't installed) raises out of find_spec
        # instead of returning None (#2776). Any lookup failure means "not
        # installed" — treat it as such instead of crashing the handler.
        _already_installed = False

    if manager == "pip" and not _already_installed and not confirmed:
        logger.warning("Package install confirmation required for uninstalled package: %s", package)
        _pypi_url = "https://pypi.org/project/%s" % package
        return ToolResult(
            ok=False,
            error="confirmation_required",
            data={
                "package": package,
                "manager": manager,
                "pypi_url": _pypi_url,
                "confirmation_required": True,
                "message": (
                    "About to install '%s' from PyPI (%s). "
                    "This will give the package full access to your system. "
                    "To proceed, call install_package('%s', confirmed=True)."
                )
                % (package, _pypi_url, package),
            },
        )

    if manager == "pip":
        cmd: list[str] = ["pip", "install", package]
    else:
        cmd = ["npm", "install", "-g", package]

    try:
        rc, out, err = await _run(cmd, timeout=120.0)
        if rc != 0:
            return ToolResult(
                ok=False,
                error="Installation failed: %s" % (err or out)[:300],
                data={"command": cmd},
            )

        return ToolResult(
            ok=True,
            data={
                "package": package,
                "manager": manager,
                "command": cmd,
                "output": out[:500],
                "message": "Successfully installed %s via %s." % (package, manager),
            },
        )
    except TimeoutError:
        # Same as setup_codex: the timeout abandons the wait, not the process.
        # pip/npm keeps installing, so "installation failed" is a claim about a
        # subprocess this handler stopped watching.
        logger.warning("install_package timed out for %s; the installer may still be running", package)
        return ToolResult(
            ok=False,
            error=(
                "The install of %s did not finish within the time limit. It may still be running, "
                "so whether the package ended up installed is unknown." % package
            ),
            error_category="install_outcome_unknown",
            unverified=True,
            data={"package": package, "manager": manager, "outcome_known": False},
        )
    except OSError as exc:
        return ToolResult(
            ok=False,
            error="Installation failed: %s" % exc,
            data={"package": package, "manager": manager},
        )


async def register_mcp_server(
    name: str,
    command: str,
    args: list[str] | None = None,
    transport: str = "stdio",
) -> ToolResult:
    """Connect a new MCP server to the running hub without restarting Viola.

    The server's tools become available immediately for the next agent
    request.  Only ``stdio`` transport is supported (SSE is not yet
    implemented in the hub).

    Args:
        name: Unique server identifier (alphanumeric, hyphens, underscores).
        command: Executable to launch (e.g. ``"uvx"``, ``"python"``, ``"node"``).
        args: Command arguments (e.g. ``["my-mcp-package==1.0.0"]``).
        transport: Connection transport — only ``"stdio"`` is supported.
    """
    # Block in non-desktop deployments: npx/uvx on the allowlist can
    # auto-download and execute arbitrary packages from npm/PyPI — RCE.
    from config.settings import settings as _app_config

    if getattr(_app_config, "deployment_mode", "user") != "user":
        return ToolResult(
            ok=False,
            error="MCP server registration is not available in cloud mode.",
        )

    if _mcp_hub is None:
        return ToolResult(ok=False, error="MCP hub not available")

    name = name.strip()
    if not name or not _VALID_SERVER_NAME.match(name):
        return ToolResult(ok=False, error="Invalid server name: '%s'" % name)

    if transport not in ("stdio", "inprocess"):
        return ToolResult(
            ok=False,
            error=("Unsupported transport '%s' — only 'stdio' is supported " "(SSE not yet implemented)" % transport),
        )

    command = command.strip()
    if not command:
        return ToolResult(ok=False, error="command must not be empty")

    from mcp_hub.types import ServerConfig

    config = ServerConfig(
        name=name,
        transport=transport,
        command=command,
        args=args or [],
        namespace=True,
        enabled=True,
    )

    # SEC-033: fail closed at the tool boundary on inline-code commands
    # (`python -c`, `node -e`, ...) and code-injecting env before we ever try to
    # launch. The launcher re-validates at spawn time; this gives the agent a
    # clear early rejection and covers transports that bypass the launcher.
    from mcp_hub.launcher import _validate_server_command

    rejection = _validate_server_command(config)
    if rejection:
        logger.warning("Rejected MCP server registration: %s", rejection)
        return ToolResult(ok=False, error=rejection)

    try:
        await _mcp_hub.connect_server(name, config)
        # connect_server returns None on success AND on a deliberate skip: a
        # launch-gated server (mcp_hub/client_hub.py's google-workspace branch)
        # logs and returns without creating a session, so "no exception" alone
        # reported a connected server for a connection that never happened.
        # get_server_status is the hub's own post-connect read -- a server it
        # never registered is absent from it entirely.
        status_reader = getattr(_mcp_hub, "get_server_status", None)
        connection_confirmed = False
        if callable(status_reader):
            server_status = str(status_reader().get(name) or "")
            if not server_status.startswith("connected"):
                logger.warning(
                    "MCP server '%s' was not registered by the hub after connect_server (status=%r)",
                    name,
                    server_status or "absent",
                )
                return ToolResult(
                    ok=False,
                    error="The hub did not register server '%s', so none of its tools are available." % name,
                    error_category="MCP_SERVER_NOT_REGISTERED",
                    data={"name": name, "transport": transport, "connected": False},
                )
            connection_confirmed = True
        tool_count = sum(1 for sn in _mcp_hub._tool_routes.values() if sn == name)
        logger.info(
            "Registered MCP server '%s' (%s) with %d tools",
            name,
            transport,
            tool_count,
        )
        return ToolResult(
            ok=True,
            # A hub that exposes no status read leaves the connection
            # unconfirmed; saying so beats inheriting the old assumption.
            unverified=not connection_confirmed,
            data={
                "name": name,
                "transport": transport,
                "command": command,
                "args": args or [],
                "tools_registered": tool_count,
                "connection_confirmed": connection_confirmed,
                "message": "Server '%s' connected — %d tools available." % (name, tool_count),
            },
        )
    except Exception as exc:
        # The exception text can include subprocess args/env/stack which may
        # carry user-submitted tokens (e.g. `--token=...`) or filesystem
        # paths.  Log internally for diagnostics; return only the exception
        # type to callers so MCP secrets cannot exit through ToolResult.error.
        logger.exception("Failed to register MCP server '%s'", name)
        return ToolResult(
            ok=False,
            error="Failed to connect server '%s' (%s)." % (name, type(exc).__name__),
        )


async def list_mcp_servers() -> ToolResult:
    """List all MCP servers registered with the running hub.

    Returns connection status and tool count for each server so the
    agent can see what's available and spot disconnected servers.
    """
    if _mcp_hub is None:
        return ToolResult(ok=False, error="MCP hub not available")

    try:
        status = _mcp_hub.get_server_status()
        return ToolResult(
            ok=True,
            data={
                "servers": status,
                "count": len(status),
            },
        )
    except Exception as exc:
        logger.exception("list_mcp_servers failed")
        return ToolResult(ok=False, error="Failed to list servers: %s" % exc)
