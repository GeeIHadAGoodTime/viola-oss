"""REST endpoint exposing the proactive suggestion engine.

GET /v1/suggestions?command_type=<intent>

Returns a contextual follow-up suggestion for the given command type, or an
empty suggestion when none applies.  The UI can call this after displaying a
command response to optionally show a TTS-friendly nudge to the user.

Rate-limiting and session de-duplication are handled entirely inside the
suggestion engine — callers do not need to implement their own guards.
"""

from __future__ import annotations

from contracts.api_response import success_response
from core.logging_config import get_logger
from fastapi import Depends, Query
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import get_current_user_id, require_auth
from ui.api.routes.common import RouteToolbox

log = get_logger(__name__)

SUGGESTIONS_ROUTE = "/v1/suggestions"


def register_suggestions_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    """Register the GET /v1/suggestions endpoint on the context router."""
    router = context.router

    @router.get(SUGGESTIONS_ROUTE, dependencies=[Depends(require_auth)])
    async def get_suggestion(
        command_type: str = Query(
            default="",
            description="The intent/command type to look up a suggestion for.",
        ),
        user_id: str = Depends(get_current_user_id),
    ):
        """Return an optional follow-up suggestion for the given command type.

        The suggestion engine is rate-limited (~20% of eligible interactions)
        and tracks first-time-in-session nudges.  A ``null`` suggestion field
        means no suggestion applies right now — the UI should remain silent.
        """

        async def _inner():
            try:
                from intent.suggestion_engine import (
                    get_contextual_suggestion,
                    maybe_suggest_followup,
                )

                ct = command_type.strip()
                if ct and ct not in ("", "proactive", "idle"):
                    # Reactive: specific command type provided
                    result = maybe_suggest_followup(ct, user_id=user_id)
                else:
                    # Proactive: no command type -- generate contextual suggestion
                    result = get_contextual_suggestion(user_id=user_id)

                # result is dict {"text": ..., "action": ...} or None
                suggestion_text = None
                suggestion_action = None
                if isinstance(result, dict):
                    suggestion_text = result.get("text")
                    suggestion_action = result.get("action")
                elif isinstance(result, str):
                    # Backward compat: plain string
                    suggestion_text = result
                    suggestion_action = result

                if user_id and suggestion_text:
                    try:
                        from services.profile.personalization_audit import (
                            PersonalizationAuditError,
                            require_personalization_event,
                        )
                    except ImportError as audit_exc:
                        log.warning(
                            "Personalization audit unavailable for suggestion; withholding suggestion: %s",
                            audit_exc,
                        )
                        suggestion_text = None
                        suggestion_action = None
                    else:
                        try:
                            require_personalization_event(
                                user_id,
                                "suggestion_materialized",
                                details={
                                    "mode": "reactive" if ct else "proactive",
                                    "command_type": ct or "proactive",
                                    "has_action": bool(suggestion_action),
                                },
                            )
                        except PersonalizationAuditError as audit_exc:
                            log.warning(
                                "Personalization audit failed for suggestion; withholding suggestion: %s",
                                audit_exc,
                            )
                            suggestion_text = None
                            suggestion_action = None

                log.debug(
                    "Suggestions endpoint: command_type=%s suggestion_present=%s",
                    command_type,
                    bool(suggestion_text),
                )
                return success_response(
                    {
                        "command_type": command_type,
                        "suggestion": suggestion_text,
                        "action": suggestion_action,
                    }
                )
            except Exception as exc:
                log.warning("Suggestion engine lookup failed: %s", exc)
                # Graceful degradation — return no suggestion rather than an error
                return success_response(
                    {
                        "command_type": command_type,
                        "suggestion": None,
                        "action": None,
                    }
                )

        return await toolbox.record_and_call(_inner, route=SUGGESTIONS_ROUTE, method="GET")

    log.info("Suggestions route registered at %s", SUGGESTIONS_ROUTE)


__all__ = [
    "SUGGESTIONS_ROUTE",
    "register_suggestions_routes",
]
