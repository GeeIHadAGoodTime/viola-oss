"""Home Assistant conversation agent endpoint.

Allows Home Assistant to use Viola as a conversation agent in
its Assist pipeline. HA sends user voice transcripts here and
receives Viola's processed response.

Endpoint:
    POST /v1/ha/conversation  - Process a conversation turn

Configuration:
    Requires home_assistant_url and home_assistant_token in settings.
    HA's Assist pipeline should be configured to POST to this endpoint.

This is NOT an HA-specific interface — it's a generic webhook that
any voice pipeline can call. HA happens to be the first consumer.
"""

from __future__ import annotations

import hmac
import time
from typing import Any

from fastapi.responses import JSONResponse

from contracts.api_response import failure_response
from core.logging_config import get_logger
from fastapi import APIRouter, Header, Request

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/ha", tags=["home-assistant"])


def _is_ha_configured() -> bool:
    """Check if Home Assistant integration is configured."""
    try:
        from ui.settings_manager import get_settings_manager

        sm = get_settings_manager()
        url = sm.get("home_assistant_url", "")
        token = sm.get("home_assistant_token", "")
        return bool(url and token)
    except Exception:
        return False


def _validate_ha_token(provided_token: str) -> bool:
    """Validate that the provided token matches our configured HA token."""
    try:
        from ui.settings_manager import get_settings_manager

        sm = get_settings_manager()
        configured_token = sm.get("home_assistant_token", "")
        return bool(configured_token and hmac.compare_digest(provided_token, configured_token))
    except Exception:
        return False


def _get_pipeline(request: Request) -> Any:
    """Resolve the IntentPipeline from app state or LazyIntentBridge.

    Follows the same access pattern used by lifecycle.py and control.py:
    first check ``app.state.intent_pipeline``, then fall back to
    ``getattr(bindings.intent, "_pipeline", None)``.
    """
    app = request.app

    # Fast path: pipeline stored directly on app state
    pipeline = getattr(app.state, "intent_pipeline", None)
    if pipeline is not None:
        return pipeline

    # Fallback: resolve through ApiContext bindings
    ctx = getattr(app.state, "api_context", None)
    if ctx is not None:
        intent_proxy = getattr(ctx, "bindings", None)
        if intent_proxy is not None:
            intent_proxy = getattr(intent_proxy, "intent", None)
        if intent_proxy is not None:
            return getattr(intent_proxy, "_pipeline", None)

    return None


@router.post("/conversation")
async def ha_conversation(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Any:
    """Process a conversation turn from Home Assistant.

    HA sends the user's voice transcript. Viola processes it through
    its intent pipeline and returns the response text.

    Request body (HA conversation agent format):
        {
            "text": "turn on the kitchen lights",
            "language": "en",
            "conversation_id": "optional-session-id"
        }

    Response (HA conversation agent format):
        {
            "response": {
                "speech": {
                    "plain": {
                        "speech": "Done, kitchen lights are on.",
                        "extra_data": null
                    }
                }
            },
            "conversation_id": "session-id"
        }
    """
    # Validate auth first — HA sends "Bearer <token>" in Authorization header.
    # Reject missing/invalid tokens with 401 before considering configuration
    # state so unauthenticated callers can't probe whether HA is configured.
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]

    if not _validate_ha_token(token):
        return JSONResponse(
            status_code=401,
            content=failure_response("unauthorized", "Invalid authorization token"),
        )

    if not _is_ha_configured():
        return JSONResponse(
            status_code=503,
            content=failure_response(
                "ha_not_configured",
                "Home Assistant integration is not configured",
            ),
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content=failure_response("invalid_body", "Request body must be JSON"),
        )

    text = body.get("text", "").strip()
    if not text:
        return JSONResponse(
            status_code=400,
            content=failure_response("empty_text", "No text provided"),
        )

    conversation_id = body.get("conversation_id") or "ha-%d" % int(time.time())
    language = body.get("language", "en")

    logger.info(
        "HA conversation: text=%s, lang=%s, conv_id=%s",
        text[:80],
        language,
        conversation_id,
    )

    # Process through Viola's intent pipeline
    try:
        response_text = await _process_through_pipeline(request, text, conversation_id)
    except PermissionError as exc:
        return JSONResponse(
            status_code=401,
            content=failure_response("authenticated_user_required", str(exc)),
        )
    except Exception:
        logger.exception("HA conversation processing failed")
        response_text = "I had trouble processing that request."

    # Return in HA conversation agent response format
    return {
        "response": {
            "speech": {
                "plain": {
                    "speech": response_text,
                    "extra_data": None,
                }
            }
        },
        "conversation_id": conversation_id,
    }


def _resolve_ha_owner_user_id() -> str:
    """Resolve the authenticated user_id whose Viola instance HA uses."""
    try:
        from core.user_context import get_current_user_id
    except ImportError as exc:
        raise PermissionError("Authenticated user_id required for Home Assistant conversation") from exc

    try:
        user_id = str(get_current_user_id() or "").strip()
    except LookupError as exc:
        raise PermissionError("Authenticated user_id required for Home Assistant conversation") from exc
    if not user_id:
        raise PermissionError("Authenticated user_id required for Home Assistant conversation")
    return user_id


async def _process_through_pipeline(request: Request, text: str, conversation_id: str) -> str:
    """Process text through Viola's intent pipeline.

    Routes the command through the same pipeline as voice commands,
    then extracts the response text from the PipelineResult.

    Multi-tenant: the HA bridge token authenticates the integration, but
    the command still needs a concrete user_id. Missing identity is rejected
    before the pipeline can pick a userless bucket.
    """
    try:
        pipeline = _get_pipeline(request)
        if pipeline is None:
            return "Viola's intent pipeline is not available."

        user_key = _resolve_ha_owner_user_id()
        result = await pipeline.process(text, user_key=user_key)

        # PipelineResult: .ok, .data (dict), .error, .intent
        if result is None:
            return "I processed your request."

        # Extract message from result.data dict
        data = getattr(result, "data", None)
        if isinstance(data, dict):
            message = data.get("message") or data.get("response") or ""
            if isinstance(message, str) and message:
                return message
            description = data.get("description")
            if isinstance(description, str) and description:
                return description

        # Check error
        error = getattr(result, "error", None)
        if error and isinstance(error, str):
            return "I couldn't do that: %s" % error

        ok = getattr(result, "ok", None)
        if ok:
            return "Done."

        return "I processed your request."

    except PermissionError:
        raise
    except Exception:
        logger.exception("Pipeline processing failed for HA conversation")
        return "Something went wrong processing your request."


def create_ha_conversation_router() -> APIRouter:
    """Factory function for the HA conversation agent router."""
    return router


__all__ = [
    "create_ha_conversation_router",
    "router",
]
