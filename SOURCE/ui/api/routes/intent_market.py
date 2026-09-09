"""Intent Market API routes (Phase 5).

Exposes the third-party intent marketplace over HTTP so integrators can:
  - GET  /v1/market/status            — enabled/disabled + summary
  - POST /v1/market/manifest/validate — pre-publish validation
  - POST /v1/market/install           — install a validated manifest
  - GET  /v1/market/plugins           — list installed manifests
  - DELETE /v1/market/plugins/{name}  — uninstall
  - POST /v1/market/{intent}/check    — check capability for a resource

Everything here is gated behind the `VIOLA_ENABLE_INTENT_MARKET` env flag
AND the `intent_market_enabled` SettingsManager toggle.  When either is
off, all routes return 403 with `market_disabled`.  This lets operators
preview + test the market without exposing it to end-users by default.

Integrators: see docs/integrator/INTENT_MARKET.md for manifest schema,
capability model, submission process, and the "Hello Viola" sample.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse

from config import env
from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import Body, Depends
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_operator_auth

logger = get_logger(__name__)

_PLUGINS_DIR_NAME = "intent_market_plugins"


def _plugins_dir() -> Path:
    """Directory where installed plugin manifests are stored."""

    from config.settings import settings

    base = Path(getattr(settings, "data_dir", ".viola")) / _PLUGINS_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def _market_enabled() -> bool:
    """Both the env flag AND the user toggle must be on."""

    if not env.get_bool("VIOLA_ENABLE_INTENT_MARKET", default=False):
        return False
    try:
        from ui.settings_manager import get_settings_manager

        return bool(get_settings_manager().get("intent_market_enabled", False))
    except Exception:
        return False


def _disabled_response() -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content=failure_response(
            "market_disabled",
            "Intent Market is off. Set VIOLA_ENABLE_INTENT_MARKET=1 and "
            "enable 'Intent Market' in Settings > AI & Agents.",
        ),
    )


def register_intent_market_routes(context: ApiContext) -> None:
    """Register Phase 5 Intent Market routes on the feature router."""

    router = context.router

    @router.get(
        "/v1/market/status",
        tags=["market"],
        dependencies=[Depends(require_operator_auth)],
    )
    async def market_status() -> Any:
        """Return whether the market is enabled + a count of installed plugins."""

        enabled = _market_enabled()
        plugins = []
        if enabled:
            plugins = [p.stem for p in _plugins_dir().glob("*.json")]
        return success_response(
            {
                "enabled": enabled,
                "env_flag": env.get_bool("VIOLA_ENABLE_INTENT_MARKET", default=False),
                "installed_count": len(plugins),
                "installed": plugins,
                "integrator_docs": "docs/integrator/INTENT_MARKET.md",
            }
        )

    @router.post(
        "/v1/market/manifest/validate",
        tags=["market"],
        dependencies=[Depends(require_operator_auth)],
    )
    async def validate_manifest_endpoint(body: dict = Body(...)) -> Any:
        """Validate a manifest payload before install.

        Body: ``{"manifest": <object>}`` — the manifest content (not a path).
        Returns ``{is_valid, errors[], warnings[]}`` so integrators can
        fix issues in their CI before trying to install.
        """

        if not _market_enabled():
            return _disabled_response()

        try:
            from experimental.phase5_intent_market.intent_market.manifest_validator import (
                ManifestValidator,
            )
        except ImportError as exc:
            logger.warning("intent_market not importable: %s", exc)
            return JSONResponse(
                status_code=503,
                content=failure_response("market_not_installed", "Intent Market module unavailable."),
            )

        manifest = body.get("manifest")
        if not isinstance(manifest, dict):
            return JSONResponse(
                status_code=400,
                content=failure_response("invalid_payload", "Body must include 'manifest' object."),
            )

        validator = ManifestValidator()
        is_valid, errors, warnings = validator.validate(manifest)
        return success_response({"is_valid": is_valid, "errors": errors, "warnings": warnings})

    @router.post(
        "/v1/market/install",
        tags=["market"],
        dependencies=[Depends(require_operator_auth)],
    )
    async def install_plugin(body: dict = Body(...)) -> Any:
        """Install a validated manifest.

        Body: ``{"manifest": <object>}``.  The manifest is written to
        ``<data_dir>/intent_market_plugins/<name>.json`` after a pass
        through ManifestValidator.  Capabilities declared in the manifest
        are registered with the CapabilityBroker.
        """

        if not _market_enabled():
            return _disabled_response()

        manifest = body.get("manifest")
        if not isinstance(manifest, dict):
            return JSONResponse(
                status_code=400,
                content=failure_response("invalid_payload", "Body must include 'manifest' object."),
            )

        try:
            from experimental.phase5_intent_market.intent_market.capability_broker import (
                get_capability_broker,
            )
            from experimental.phase5_intent_market.intent_market.manifest_validator import (
                ManifestValidator,
            )
        except ImportError as exc:
            logger.warning("intent_market not importable: %s", exc)
            return JSONResponse(
                status_code=503,
                content=failure_response("market_not_installed", "Intent Market module unavailable."),
            )

        validator = ManifestValidator()
        is_valid, errors, warnings = validator.validate(manifest)
        if not is_valid:
            return JSONResponse(
                status_code=400,
                content=failure_response(
                    "manifest_invalid",
                    "Manifest failed validation.",
                    details={"errors": errors, "warnings": warnings},
                ),
            )

        name = manifest.get("metadata", {}).get("name") or manifest.get("name")
        if not name or not isinstance(name, str):
            return JSONResponse(
                status_code=400,
                content=failure_response("missing_name", "Manifest metadata.name is required."),
            )

        # Persist
        dest = _plugins_dir() / f"{name}.json"
        dest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        # Register capabilities
        broker = get_capability_broker()
        for intent_def in manifest.get("intents", []) or []:
            for cap in intent_def.get("capabilities", []) or []:
                broker.register_capability(
                    intent_name=intent_def.get("name", name),
                    capability=cap.get("type", ""),
                    scope=cap.get("scope", ""),
                    mode=cap.get("mode", "r"),
                )

        return success_response(
            {
                "installed": name,
                "intents": [i.get("name") for i in manifest.get("intents", []) or []],
                "warnings": warnings,
            }
        )

    @router.get(
        "/v1/market/plugins",
        tags=["market"],
        dependencies=[Depends(require_operator_auth)],
    )
    async def list_plugins() -> Any:
        """List installed plugin manifests."""

        if not _market_enabled():
            return _disabled_response()

        plugins = []
        for path in _plugins_dir().glob("*.json"):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                plugins.append(
                    {
                        "name": path.stem,
                        "version": manifest.get("metadata", {}).get("version"),
                        "author": manifest.get("metadata", {}).get("author"),
                        "description": manifest.get("metadata", {}).get("description"),
                        "intents": [i.get("name") for i in manifest.get("intents", []) or []],
                    }
                )
            except Exception:
                logger.exception("Failed to parse plugin manifest: %s", path)
        return success_response({"plugins": plugins, "count": len(plugins)})

    @router.delete(
        "/v1/market/plugins/{name}",
        tags=["market"],
        dependencies=[Depends(require_operator_auth)],
    )
    async def uninstall_plugin(name: str) -> Any:
        """Remove an installed plugin manifest."""

        if not _market_enabled():
            return _disabled_response()

        # name-safety: reject anything that could escape the plugins dir
        safe = Path(name).name
        if safe != name or not safe or safe.startswith("."):
            return JSONResponse(
                status_code=400,
                content=failure_response("invalid_name", "Plugin name must be a simple identifier."),
            )
        target = _plugins_dir() / f"{safe}.json"
        if not target.is_file():
            return JSONResponse(
                status_code=404,
                content=failure_response("not_installed", f"Plugin '{safe}' is not installed."),
            )
        target.unlink()
        return success_response({"uninstalled": safe})

    @router.post(
        "/v1/market/{intent_name}/check",
        tags=["market"],
        dependencies=[Depends(require_operator_auth)],
    )
    async def check_permission(intent_name: str, body: dict = Body(...)) -> Any:
        """Check whether an intent may access a resource.

        Body: ``{"capability": "local.storage", "resource": "nutrition/*",
                 "operation": "read"}``.  Useful for integrators to test
        their capability declarations before shipping.
        """

        if not _market_enabled():
            return _disabled_response()

        try:
            from experimental.phase5_intent_market.intent_market.capability_broker import (
                get_capability_broker,
            )
        except ImportError:
            return JSONResponse(
                status_code=503,
                content=failure_response("market_not_installed", "Intent Market module unavailable."),
            )

        capability = body.get("capability", "")
        resource = body.get("resource", "")
        operation = body.get("operation", "read")

        is_allowed, reason = get_capability_broker().check_permission(
            intent_name=intent_name,
            capability=capability,
            resource=resource,
            operation=operation,
        )
        return success_response({"allowed": is_allowed, "reason": reason})
