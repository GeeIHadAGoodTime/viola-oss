"""Runtime MCP server configuration helpers.

The production controller and local inspection tools must build the same MCP
server list. Keep side effects such as OAuth token export in the caller.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.logging_config import get_logger

from .launcher import _validate_server_command
from .types import ServerConfig

logger = get_logger(__name__)


@dataclass(slots=True)
class RuntimeMCPServerConfigs:
    """MCP server configs split the way production starts them."""

    fast_configs: list[ServerConfig]
    browser_config: ServerConfig | None

    @property
    def all_configs(self) -> list[ServerConfig]:
        configs = list(self.fast_configs)
        if self.browser_config is not None:
            configs.append(self.browser_config)
        return configs


def _get_settings(settings_obj: Any | None) -> Any:
    if settings_obj is not None:
        return settings_obj
    from config.settings import settings

    return settings


def _build_browser_config(
    settings_obj: Any,
    executable: str,
    *,
    browser_surface: str | None = None,
) -> ServerConfig:
    """Pick the browser MCP server by app surface, not a user toggle.

    Desktop surface drives the embedded Qt webview through the CDP server, so
    the user can watch the agent work.  Cloud/headless container surfaces run
    the Playwright server in-process so it shares the cloud browser session
    pool with the user-visible streaming surface.
    Surface is read from the canonical ``app_surface`` setting via
    ``services.computer_use.cloud_guard.is_cloud_surface``.
    """
    from services.computer_use.cloud_guard import is_cloud_surface

    requested_surface = str(browser_surface or "").strip().lower()
    if requested_surface in {"phone", "headless", "background", "cloud"}:
        logger.info("Browser tools: pooled Playwright server (%s surface)", requested_surface)
        return ServerConfig(
            name="browser",
            transport="inprocess",
            module="mcp_servers.browser.server",
        )

    if is_cloud_surface(settings_obj):
        logger.info("Browser tools: pooled Playwright server (cloud/headless surface)")
        return ServerConfig(
            name="browser",
            transport="inprocess",
            module="mcp_servers.browser.server",
        )

    logger.info("Browser tools: CDP embedded browser (desktop surface)")
    return ServerConfig(
        name="browser",
        transport="inprocess",
        module="mcp_servers.browser_cdp.server",
    )


def _append_google_workspace_config(
    configs: list[ServerConfig],
    settings_obj: Any,
    environ: Mapping[str, str],
) -> None:
    from services.oauth.google import is_google_restricted_features_enabled

    if not is_google_restricted_features_enabled(settings_obj):
        logger.debug("Google Workspace MCP server disabled by restricted Google launch gate")
        return

    gws_path = getattr(settings_obj, "google_workspace_mcp_path", "")
    if not gws_path or environ.get("VIOLA_TEST_BYPASS_LIMITS") == "1":
        return

    gws_server = Path(gws_path)
    if not gws_server.exists():
        logger.warning("Google Workspace MCP server not found at %s", gws_path)
        return

    viola_client_id = getattr(settings_obj, "google_client_id", "") or ""
    viola_api_port = getattr(settings_obj, "api_port", 8756)
    gws_env: dict[str, str] = {
        "GEMINI_CLI_WORKSPACE_FORCE_FILE_STORAGE": "true",
    }
    if viola_client_id:
        gws_env["WORKSPACE_CLIENT_ID"] = viola_client_id
        gws_env["WORKSPACE_CLOUD_FUNCTION_URL"] = "http://127.0.0.1:%s/auth/internal/google" % viola_api_port

    configs.append(
        ServerConfig(
            name="google-workspace",
            transport="stdio",
            command="node",
            args=[str(gws_server)],
            env=gws_env,
            namespace=False,
        )
    )
    logger.info("Google Workspace MCP server enabled")


def _append_external_provider_configs(configs: list[ServerConfig], settings_obj: Any) -> None:
    from config.providers import EXTERNAL_PROVIDERS

    for provider_key, provider_cfg in EXTERNAL_PROVIDERS.items():
        requires_field = provider_cfg.get("requires", "")
        if not requires_field:
            continue
        if not getattr(settings_obj, str(requires_field), False):
            logger.debug(
                "External provider '%s' disabled (%s=false)",
                provider_key,
                requires_field,
            )
            continue
        provider_args = provider_cfg.get("args", [])
        configs.append(
            ServerConfig(
                name=provider_key,
                transport=str(provider_cfg.get("transport", "stdio")),
                command=str(provider_cfg.get("command", "")),
                args=(list(provider_args) if isinstance(provider_args, list) else []),
                namespace=True,
            )
        )
        logger.info("External provider '%s' enabled", provider_key)


def _append_configured_external_servers(configs: list[ServerConfig], settings_obj: Any) -> None:
    raw_ext = getattr(settings_obj, "mcp_external_servers", "")
    if not raw_ext:
        return
    try:
        from services.oauth.google import is_google_restricted_features_enabled

        google_restricted_enabled = is_google_restricted_features_enabled(settings_obj)
        ext_list = json.loads(raw_ext)
        if not isinstance(ext_list, list):
            return
        for entry in ext_list:
            if not isinstance(entry, dict) or "name" not in entry:
                continue
            if str(entry.get("name", "")).strip() == "google-workspace" and not google_restricted_enabled:
                logger.debug(
                    "Skipping configured google-workspace MCP server because restricted Google features are disabled"
                )
                continue
            ext_cfg = ServerConfig(
                name=entry["name"],
                transport="stdio",
                command=entry.get("command", ""),
                args=entry.get("args", []),
                env=entry.get("env", {}),
                namespace=True,
            )
            rejection = _validate_server_command(ext_cfg)
            if rejection:
                logger.warning(
                    "Rejected MCP server command: %s",
                    entry.get("command", ""),
                )
                continue
            configs.append(ext_cfg)
    except (json.JSONDecodeError, TypeError) as parse_err:
        logger.warning("Failed to parse mcp_external_servers: %s", parse_err)


def build_runtime_mcp_server_configs(
    settings_obj: Any | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    executable: str | None = None,
    browser_surface: str | None = None,
) -> RuntimeMCPServerConfigs:
    """Return the production MCP server configs for the current settings."""

    settings_obj = _get_settings(settings_obj)
    environ = os.environ if environ is None else environ
    executable = executable or sys.executable

    browser_config = _build_browser_config(settings_obj, executable, browser_surface=browser_surface)
    configs: list[ServerConfig] = [
        ServerConfig(
            name="core-tools",
            transport="inprocess",
            module="mcp_servers.core_tools.server",
        ),
    ]
    # computer-use drives the local desktop (screen/keyboard/mouse). Its module
    # (mcp_servers.computer_use.server) is desktop-only and not shipped to the
    # cloud image, so on cloud it fails to connect (ModuleNotFoundError) and only
    # adds noise. Register it on desktop surfaces only.
    from services.computer_use.cloud_guard import is_cloud_surface

    if not is_cloud_surface(settings_obj):
        configs.append(
            ServerConfig(
                name="computer-use",
                transport="inprocess",
                module="mcp_servers.computer_use.server",
            )
        )
    _append_google_workspace_config(configs, settings_obj, environ)
    _append_external_provider_configs(configs, settings_obj)
    _append_configured_external_servers(configs, settings_obj)

    return RuntimeMCPServerConfigs(
        fast_configs=configs,
        browser_config=browser_config,
    )
