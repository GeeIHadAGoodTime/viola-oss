"""Desktop-side handler for the ``agent.dispatch`` whole-turn relay.

This is the desktop half of the cloud->desktop auto-link. The fine-grained
capability handlers in :mod:`handlers` answer one bounded action each
(``files.file_list``, ``smart_home.ha_control``, ...). ``agent.dispatch`` is
different in kind: the cloud forwards an *entire* natural-language turn and
the desktop runs it through its OWN intent pipeline -- the user's machine,
files, and LLM key -- then returns the agent's answer.

It delegates to the canonical desktop command path, :class:`CommandService`,
exactly as the desktop's ``POST /v1/command`` route does. No reimplementation:
the relay turn is just another command on the user's own desktop.

Fail-safe posture (mirrors the rest of the companion client):

* Returns a JSON-serializable dict on success.
* Returns ``{"error": "<reason>"}`` for an expected failure (no pipeline
  wired yet, command service unavailable). The client turns that into an
  ``error`` frame and the cloud falls back to handling the turn itself.
* Never raises for an ordinary failure -- a raised exception is caught by
  the client and reported as an ``error`` frame.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger
from core.user_context import get_current_or_desktop_active_user_id

logger = get_logger(__name__)


def _error(reason: str) -> dict[str, Any]:
    return {"error": reason}


def _desktop_command_service() -> Any | None:
    """Return the desktop's process-wide :class:`CommandService`, or ``None``.

    The desktop builds exactly one ``CommandService`` and stores it on
    ``app.state.command_service`` when the ``/v1/command`` route registers
    (see ``ui/api/routes/command.py``). The companion client runs in the same
    process, so it reaches that singleton through ``ui.server.get_app()`` --
    the same process-wide app accessor ``ai_controller`` uses for Telegram
    notifications. Returns ``None`` when the app or command service is not
    yet wired; the caller turns that into an honest error frame.
    """
    try:
        from ui.server import get_app

        app = get_app()
    except Exception:
        logger.debug("Companion agent.dispatch: desktop app accessor unavailable")
        return None
    if app is None:
        return None
    service = getattr(getattr(app, "state", None), "command_service", None)
    if service is None:
        logger.debug("Companion agent.dispatch: desktop command_service not yet wired")
    return service


def _extract_message(envelope: Any) -> str:
    """Pull the user-visible message out of a command envelope."""
    if not isinstance(envelope, dict):
        return ""
    data = envelope.get("data")
    if isinstance(data, dict):
        for key in ("message", "response", "answer", "text"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for key in ("message", "response", "answer"):
        value = envelope.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def handle_agent_dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    """Run a relayed cloud turn through the desktop's own intent pipeline.

    Payload (set by the cloud relay):
        ``text``       -- the natural-language turn (required).
        ``history``    -- optional prior turns for multi-turn context.
        ``cloud_user_id`` -- the cloud account's user id, for audit only.
        ``session_id`` / ``trace_id`` -- optional correlation ids.

    Returns a dict the cloud surfaces to the browser:
        ``message``    -- the agent's user-facing answer.
        ``intent`` / ``route_type`` / ``source`` -- routing metadata.
        ``ok``         -- whether the turn completed without error.
        ``card``       -- optional rich card from the desktop result.
        ``executed_on`` -- always ``"desktop"`` so the cloud can label it.
        ``raw_response`` -- the desktop result data, for parity with the
                            cloud's own dispatch shape.
    """
    text = str(payload.get("text") or payload.get("command") or "").strip()
    if not text:
        return _error("agent.dispatch requires a non-empty text turn.")

    service = _desktop_command_service()
    if service is None:
        # No pipeline wired -> honest failure; the cloud falls back to its
        # own handling so the user never gets a dead turn.
        return _error("The desktop is online but its command pipeline is not ready yet.")

    history = payload.get("history")
    if not isinstance(history, list):
        history = None

    cloud_user_id = str(payload.get("cloud_user_id") or "").strip()
    trace_id = payload.get("trace_id")
    # Per-turn id first. This becomes CommandRequest.request_id, which the
    # desktop's command pipeline uses as its IDEMPOTENCY key -- so it has to
    # identify this request, not this conversation. Reading ``session_id``
    # first meant every relayed turn from one phone shared a key that is
    # constant per (user, device), and the pipeline replayed the first turn's
    # answer for every later turn until the ledger entry expired an hour later.
    request_id = payload.get("request_id") or payload.get("session_id")

    try:
        from intent.permissions.remote_relay import unconsented_remote_relay
        from services.command import CommandRequest

        command_request = CommandRequest(
            text=text,
            history=history,
            # The relayed turn runs on the user's own desktop, against the
            # desktop's single local library/account. user_id is the desktop
            # principal -- never the cloud id -- so per-user stores stay
            # correctly scoped to this machine's owner. That principal is the
            # install's ACTIVE one (the signed-in GoTrue account when there is
            # a desktop session, else the bootstrap device identity), not the
            # bare device identity: a relayed turn arrives outside any request
            # context, so the bare device resolver answered with the anonymous
            # device even on a signed-in desktop, and every user-scoped desktop
            # store the turn touches -- settings (including ``ai_source``,
            # which decides who gets billed), library, memory -- was read under
            # the wrong one of this install's two principals.
            user_id=get_current_or_desktop_active_user_id(),
            channel="companion_relay",
            request_id=str(request_id) if request_id else None,
            trace_id=str(trace_id) if trace_id else None,
        )
        # The cloud bridge does NOT require a consent grant for
        # ``agent.dispatch`` (it gates only ``desktop.*`` and
        # ``smart_home.ha_control``), and requiring one would break the
        # default-on auto-link relay. So the turn runs, but the powers that
        # consent exists to protect stay closed inside it -- otherwise
        # asking for the whole turn is a way to get strictly more than the
        # bounded, consent-gated request would have granted. See
        # ``intent/permissions/remote_relay.py``.
        with unconsented_remote_relay():
            result = await service.execute(command_request)
    except Exception:
        logger.exception("Companion agent.dispatch failed running the desktop pipeline")
        return _error("The desktop could not complete that request.")

    try:
        envelope = result.to_envelope()
    except Exception:
        logger.exception("Companion agent.dispatch could not serialize the desktop result")
        return _error("The desktop completed the request but its response was malformed.")

    message = _extract_message(envelope)
    data = envelope.get("data") if isinstance(envelope, dict) else None
    intent = str(getattr(result, "intent", "") or "") or None
    card = data.get("card") if isinstance(data, dict) else None
    ok = bool(getattr(result, "ok", False))

    if not message:
        message = "Done." if ok else "The desktop could not complete that request."

    response: dict[str, Any] = {
        "ok": ok,
        "message": message,
        "intent": intent,
        "route_type": "answer" if ok else "error",
        "source": "companion_desktop",
        "executed_on": "desktop",
        "cloud_user_id": cloud_user_id,
    }
    if isinstance(card, dict):
        response["card"] = card
    if isinstance(data, dict):
        response["raw_response"] = data
        # Surface the desktop's effective LLM source so the cloud's billing
        # accounting can decide whether the managed-spend cap applies. A
        # relayed turn that ran on the user's OWN key costs Viola nothing.
        ai_source = data.get("ai_source")
        if isinstance(ai_source, str) and ai_source.strip():
            response["desktop_ai_source"] = ai_source.strip()
    if "desktop_ai_source" not in response:
        response["desktop_ai_source"] = _desktop_ai_source()
    return response


def _desktop_ai_source() -> str:
    """Return the desktop's configured ``ai_source`` (managed / byok / codex).

    The cloud uses this to bill correctly: a relayed turn that ran on the
    user's own desktop key (``byok`` / ``codex``) must NOT count against the
    cloud user's managed-LLM spend cap; a desktop still on Viola's managed
    LLM keeps costing Viola money and stays accountable.
    """
    try:
        from config.defaults import DEFAULT_AI_SOURCE
        from ui.settings_manager import get_settings_manager

        value = get_settings_manager().get(
            "ai_source", DEFAULT_AI_SOURCE, user_id=get_current_or_desktop_active_user_id()
        )
        return str(value or DEFAULT_AI_SOURCE).strip().lower()
    except Exception:
        # Unknown -> assume managed so billing fails safe toward charging
        # Viola's account rather than silently giving away managed compute.
        return "managed"
