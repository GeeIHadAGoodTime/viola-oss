"""Read-only connector manifest and status API."""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import Depends, Request
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import get_current_user_id, require_auth

logger = get_logger(__name__)


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _profile_error_response(exc: Exception) -> JSONResponse:
    code = getattr(exc, "code", "profile_error")
    status = 404 if code in {"unknown_profile", "unknown_connector"} else 400
    return JSONResponse(status_code=status, content=failure_response(str(code), str(exc)))


def _require_llm_profile_connector(connector_id: str) -> str:
    from services.connectors.manifests import get_connector_manifest
    from services.connectors.profiles import ConnectorProfileError

    manifest = get_connector_manifest(connector_id)
    if manifest is None:
        raise ConnectorProfileError("unknown_connector", "Unknown connector: %s" % connector_id)
    if manifest.category != "llm":
        raise ConnectorProfileError(
            "unsupported_profile_category",
            "Connector profiles currently apply to LLM connectors only; use the category-specific connector actions for %s."
            % manifest.category,
        )
    return manifest.id


def _reinitialize_active_llm_router() -> None:
    """Refresh the in-memory command router after selected LLM profile changes."""

    from services.llm.provider_router import get_active_router

    router = get_active_router()
    if router is None:
        return
    router.reinitialize()
    logger.info("Reinitialized active LLM router after connector profile change")


def register_connector_routes(context: ApiContext) -> None:
    """Register connector manifest/status routes on the feature router."""

    router = context.router

    @router.get("/v1/connectors", tags=["connectors"], dependencies=[Depends(require_auth)])
    async def list_connectors(category: str | None = None) -> dict[str, Any]:
        from services.connectors.manifests import build_connector_manifest_payload

        return success_response(build_connector_manifest_payload(category))

    @router.get("/v1/connectors/status", tags=["connectors"], dependencies=[Depends(require_auth)])
    async def connector_status(
        category: str | None = None,
        user_id: str = Depends(get_current_user_id),
    ) -> dict[str, Any]:
        from services.connectors.status import build_connector_status_payload

        return success_response(await build_connector_status_payload(user_id, category))

    @router.get("/v1/connectors/certification", tags=["connectors"], dependencies=[Depends(require_auth)])
    async def connector_certification(
        category: str | None = None,
        connector_id: str | None = None,
        user_id: str = Depends(get_current_user_id),
    ) -> dict[str, Any]:
        from services.connectors.certification import build_connector_certification_payload

        return success_response(await build_connector_certification_payload(user_id, category, connector_id))

    @router.get(
        "/v1/connectors/{connector_id}/certification", tags=["connectors"], dependencies=[Depends(require_auth)]
    )
    async def connector_certification_detail(
        connector_id: str,
        user_id: str = Depends(get_current_user_id),
    ) -> dict[str, Any]:
        from services.connectors.certification import build_connector_certification_payload

        return success_response(await build_connector_certification_payload(user_id, connector_id=connector_id))

    @router.post("/v1/connectors/certification/evaluate", tags=["connectors"], dependencies=[Depends(require_auth)])
    async def connector_certification_evaluate(
        request: Request,
        user_id: str = Depends(get_current_user_id),
    ) -> dict[str, Any]:
        from services.connectors.certification import build_connector_certification_payload

        body = await _json_body(request)
        category = body.get("category") if isinstance(body.get("category"), str) else None
        connector_id = body.get("connector_id") if isinstance(body.get("connector_id"), str) else None
        evidence = body.get("evidence") if isinstance(body.get("evidence"), dict) else None
        return success_response(
            await build_connector_certification_payload(
                user_id,
                category=category,
                connector_id=connector_id,
                evidence=evidence,
            )
        )

    @router.get("/v1/connectors/profiles", tags=["connectors"], dependencies=[Depends(require_auth)])
    async def list_connector_profiles(
        category: str | None = None,
        user_id: str = Depends(get_current_user_id),
    ) -> dict[str, Any]:
        from services.connectors.profiles import get_connection_profile_store, profile_payload

        store = get_connection_profile_store()
        return success_response(profile_payload(store, user_id, category))

    @router.post("/v1/connectors/profiles", tags=["connectors"], dependencies=[Depends(require_auth)])
    async def save_connector_profile(
        request: Request,
        user_id: str = Depends(get_current_user_id),
    ) -> Any:
        from services.connectors.profiles import ConnectorProfileError, get_connection_profile_store

        body = await _json_body(request)
        store = get_connection_profile_store()
        try:
            connector_id = _require_llm_profile_connector(str(body.get("connector_id", "")))
            selected_before = store.get_selected_profile_id(user_id, "llm")
            profile = store.save_profile(
                user_id,
                connector_id,
                profile_id=body.get("profile_id") if isinstance(body.get("profile_id"), str) else None,
                display_name=body.get("display_name") if isinstance(body.get("display_name"), str) else None,
                base_url=body.get("base_url") if isinstance(body.get("base_url"), str) else None,
                model=body.get("model") if isinstance(body.get("model"), str) else None,
                api_key=body.get("api_key", None),
                enabled=body.get("enabled") if isinstance(body.get("enabled"), bool) else None,
                metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else None,
                selected=bool(body.get("selected", False)),
            )
            selected = store.get_selected_profile_id(user_id, profile.category) == profile.profile_id
            if selected_before == profile.profile_id or selected:
                _reinitialize_active_llm_router()
            return success_response({"profile": profile.to_dict(selected=selected)})
        except ConnectorProfileError as exc:
            return _profile_error_response(exc)

    @router.patch("/v1/connectors/profiles/{profile_id}", tags=["connectors"], dependencies=[Depends(require_auth)])
    async def update_connector_profile(
        profile_id: str,
        request: Request,
        user_id: str = Depends(get_current_user_id),
    ) -> Any:
        from services.connectors.profiles import ConnectorProfileError, get_connection_profile_store

        body = await _json_body(request)
        store = get_connection_profile_store()
        existing = store.get_profile(user_id, profile_id)
        if existing is None:
            return _profile_error_response(
                ConnectorProfileError("unknown_profile", "Unknown connector profile: %s" % profile_id)
            )
        try:
            selected_before = store.get_selected_profile_id(user_id, existing.category)
            profile = store.save_profile(
                user_id,
                existing.connector_id,
                profile_id=profile_id,
                display_name=body.get("display_name") if isinstance(body.get("display_name"), str) else None,
                base_url=body.get("base_url") if isinstance(body.get("base_url"), str) else None,
                model=body.get("model") if isinstance(body.get("model"), str) else None,
                api_key=body.get("api_key", None),
                enabled=body.get("enabled") if isinstance(body.get("enabled"), bool) else None,
                metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else None,
                selected=bool(body.get("selected", False)),
            )
            selected = store.get_selected_profile_id(user_id, profile.category) == profile.profile_id
            if selected_before == profile.profile_id or selected:
                _reinitialize_active_llm_router()
            return success_response({"profile": profile.to_dict(selected=selected)})
        except ConnectorProfileError as exc:
            return _profile_error_response(exc)

    @router.post(
        "/v1/connectors/profiles/{profile_id}/select",
        tags=["connectors"],
        dependencies=[Depends(require_auth)],
    )
    async def select_connector_profile(
        profile_id: str,
        user_id: str = Depends(get_current_user_id),
    ) -> Any:
        from services.connectors.profiles import ConnectorProfileError, get_connection_profile_store

        store = get_connection_profile_store()
        try:
            profile = store.select_profile(user_id, profile_id)
            _reinitialize_active_llm_router()
            return success_response({"profile": profile.to_dict(selected=True)})
        except ConnectorProfileError as exc:
            return _profile_error_response(exc)

    @router.post(
        "/v1/connectors/profiles/{profile_id}/validate",
        tags=["connectors"],
        dependencies=[Depends(require_auth)],
    )
    async def validate_connector_profile(
        profile_id: str,
        request: Request,
        user_id: str = Depends(get_current_user_id),
    ) -> Any:
        from services.connectors.profiles import (
            ConnectorProfileError,
            get_connection_profile_store,
            validate_llm_profile,
        )

        store = get_connection_profile_store()
        body = await _json_body(request)
        try:
            return success_response(
                await validate_llm_profile(
                    store,
                    user_id,
                    profile_id,
                    probe_tools=bool(body.get("probe_tools", False)),
                )
            )
        except ConnectorProfileError as exc:
            return _profile_error_response(exc)

    @router.delete("/v1/connectors/profiles/{profile_id}", tags=["connectors"], dependencies=[Depends(require_auth)])
    async def delete_connector_profile(
        profile_id: str,
        user_id: str = Depends(get_current_user_id),
    ) -> Any:
        from services.connectors.profiles import get_connection_profile_store

        store = get_connection_profile_store()
        selected_before = store.get_selected_profile_id(user_id, "llm")
        deleted = store.delete_profile(user_id, profile_id)
        if not deleted:
            return JSONResponse(
                status_code=404,
                content=failure_response("unknown_profile", "Unknown connector profile: %s" % profile_id),
            )
        if selected_before == profile_id:
            _reinitialize_active_llm_router()
        return success_response({"profile_id": profile_id, "deleted": True})


__all__ = ["register_connector_routes"]
