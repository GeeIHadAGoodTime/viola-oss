from __future__ import annotations

import asyncio
import contextlib
from http import HTTPStatus
from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse

from contracts.api_response import ResponseEnvelope, failure_response, success_response
from core.logging_config import get_logger
from core.platform import get_data_dir
from core.task_tracker import TaskTracker
from core.validation import validate_command_text
from fastapi import Depends, HTTPException, Request
from services.command.command_service_core import channel_key
from services.command.http_responses import build_429_response
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth
from ui.core.security import is_desktop_surface, is_loopback_request
from ui.security.config import get_security_config

log = get_logger(__name__)

# Channels whose reply is spoken out of this machine's speakers when the user
# has asked for every reply to be spoken. A Telegram / SMS / phone / email turn
# arrives on this same route and must never make the desktop talk, and a voice
# turn is already spoken by the voice handler
# (``core/voice_command_handler.py::_speak_response``), so speaking it here too
# would double it. What is left is exactly the local text surfaces: the desktop
# UI's own command box (``ui/react-app/.../MusicCommandInput.jsx`` and
# ``ui/qt_native/window_core.py`` both POST here with no channel, which the
# route defaults to "http") and the web chat.
_LOCALLY_SPOKEN_CHANNELS = frozenset({"http", "web"})

# Upper bound on one spoken reply, so a wedged synthesizer leaks one task and
# not one per command. Well above any real reply; this is a backstop, not a
# budget.
_SPEAK_REPLY_TIMEOUT_SECONDS = 300.0

# Speech outlives the request that triggered it (see ``_speak_reply``), so the
# tasks need an owner other than the request scope or they can be garbage
# collected mid-utterance.
_reply_speech_tasks = TaskTracker()


def _route_exists(context: ApiContext, path: str, method: str) -> bool:
    method = method.upper()
    for route in (*getattr(context.app.router, "routes", ()), *getattr(context.router, "routes", ())):
        if getattr(route, "path", None) != path:
            continue
        route_methods = getattr(route, "methods", set()) or set()
        if method in route_methods:
            return True
    return False


def _authenticated_user_id_for_request(request: Request) -> str:
    """Resolve the authenticated principal for mutable command routes."""
    user = getattr(request.state, "user", None)
    user_id = getattr(user, "id", None)
    if isinstance(user_id, str) and user_id.strip():
        return user_id.strip()

    user_context = getattr(request.state, "user_context", None)
    context_user_id = getattr(user_context, "user_id", None)
    if isinstance(context_user_id, str) and context_user_id.strip():
        return context_user_id.strip()

    security = get_security_config()
    desktop_loopback = is_desktop_surface() and is_loopback_request(request)
    if desktop_loopback:
        # #2646 / M-BILL-1 (#337): the managed-AI account gate at
        # ``requires_account_for_command`` (below, ~line 285) reads this
        # principal. It MUST prefer the signed-in desktop account over the
        # anonymous ``device-*`` id, or a GUI-signed-in install falsely fires
        # ``account_required`` on the cookieless viola-runner command path
        # (#2619 live repro). ``get_current_or_desktop_active_user_id`` is the
        # same account-preferring resolver PR #2031 wired into the managed-LLM
        # factory/provider-router — it returns the request principal, else the
        # signed-in GoTrue account, else the device id, so signed-out installs
        # still gate exactly as before. (The auth middleware's
        # ``_maybe_inject_local_user`` normally binds this principal on
        # ``request.state.user`` upstream; these legs cover the
        # auth-disabled / not-injected edge.)
        if not security.auth_enabled:
            from core.user_context import get_current_or_desktop_active_user_id

            return get_current_or_desktop_active_user_id()
        api_key = request.headers.get("X-API-Key")
        if api_key and security.auth_api_key:
            import hmac as _hmac

            if _hmac.compare_digest(api_key, security.auth_api_key):
                from core.user_context import get_current_or_desktop_active_user_id

                return get_current_or_desktop_active_user_id()

    log.warning(
        "Authenticated route missing user principal: method=%s path=%s",
        request.method,
        request.url.path,
    )
    raise HTTPException(
        status_code=HTTPStatus.UNAUTHORIZED,
        detail=failure_response(
            "authenticated_user_required",
            "Authentication is required for this command.",
            data={"intent": "unknown"},
        ),
    )


def _command_stream_message(envelope: Any) -> str:
    """Extract the final user-visible message from a command envelope."""
    if not isinstance(envelope, dict):
        return ""
    data = envelope.get("data")
    if isinstance(data, dict):
        for key in ("message", "response", "text"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    for key in ("message", "response"):
        value = envelope.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _command_ledger_path() -> Path:
    return get_data_dir() / "runtime" / "command_ledger.json"


def _reply_tts_engine(context: ApiContext) -> Any | None:
    """The local TTS engine, or None on a surface with no audio device.

    Cloud builds construct ``Bindings(intent=None)``
    (``backend/cloud_api_context.py``), so a cloud container structurally
    cannot reach an engine here even before the desktop-surface check.
    """
    intent = getattr(context.bindings, "intent", None)
    if intent is None:
        return None
    return getattr(intent, "tts", None)


def _should_speak_reply(channel_value: Any, user_id: str) -> bool:
    """Whether this command's reply should be spoken aloud on this machine.

    ``speak_all_replies`` is the user's own setting for exactly this: "Viola
    speaks every response aloud, not just responses to voice commands"
    (``docs/user/SETTINGS.md``).

    Every default below is passed from ``config.defaults`` rather than written
    as a literal, and that is load-bearing rather than tidiness. All three keys
    are user-scoped (``ui/settings_schema.py``), and for a user-scoped read with
    a real signed-in principal ``SettingsManager.get`` resolves through
    ``get_user_setting`` -> ``_read_setting_from_blob``, which returns THE
    CALLER'S default when the user's blob has no row for the key -- it never
    consults ``DEFAULT_SETTINGS``. So a literal here silently outranks the
    shipped default for exactly the users whose blob is real: a hard-coded
    ``False`` left every signed-in install mute while a signed-out one spoke,
    and only the signed-out case (a device pseudo-identity, which does fall back
    to the global blob) would show up in a cold-install test.

    ``voice_muted`` is checked here because this is the first code that decides
    whether to speak from user settings, and a path that speaks straight
    through the user's own mute would be a new bug rather than a fixed one.
    """
    if channel_key(channel_value) not in _LOCALLY_SPOKEN_CHANNELS:
        return False
    if not is_desktop_surface():
        return False
    from config.defaults import (
        SPEAK_ALL_REPLIES_DEFAULT,
        TTS_ENABLED_DEFAULT,
        VOICE_MUTED_DEFAULT,
    )
    from ui.settings_manager import get_settings_manager

    settings_mgr = get_settings_manager()
    if not bool(settings_mgr.get("speak_all_replies", SPEAK_ALL_REPLIES_DEFAULT, user_id=user_id)):
        return False
    if bool(settings_mgr.get("voice_muted", VOICE_MUTED_DEFAULT, user_id=user_id)):
        return False
    return bool(settings_mgr.get("tts_enabled", TTS_ENABLED_DEFAULT, user_id=user_id))


def _speak_reply_now(context: ApiContext, envelope: Any, *, channel_value: Any, user_id: str) -> None:
    """Speak the reply aloud when the user has asked for every reply to be spoken.

    Fire-and-forget on purpose. The caller already has the reply text -- it is
    in this envelope and it went out over the ``chat_response`` broadcast
    before this runs -- so holding the HTTP response open for the length of the
    speech would only add latency and put long replies past client budgets
    (``ui/qt_native/window_core.py`` posts with 10s).
    """
    if not _should_speak_reply(channel_value, user_id):
        return
    message = _command_stream_message(envelope)
    if not message:
        return
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if isinstance(data, dict) and data.get("spoken"):
        # Something upstream already voiced this turn; speaking it again would
        # double it. Same contract the voice handler reads.
        return
    engine = _reply_tts_engine(context)
    speak = getattr(engine, "speak", None) if engine is not None else None
    if not callable(speak):
        return

    async def _run() -> None:
        try:
            spoken = speak(message)
            if asyncio.iscoroutine(spoken):
                await asyncio.wait_for(spoken, timeout=_SPEAK_REPLY_TIMEOUT_SECONDS)
        except TimeoutError:
            log.warning("Spoken reply exceeded %.0fs; abandoning", _SPEAK_REPLY_TIMEOUT_SECONDS)
            return
        # An audio device can fail any way at all, and a reply that cannot be
        # spoken is still a reply.
        except Exception as exc:  # noqa: BLE001, RUF100
            log.warning("Spoken reply failed: %s", exc)
            return
        try:
            from admin.instrumentation import record_feature_used

            record_feature_used("tts")
        except Exception:  # noqa: BLE001, RUF100 - usage telemetry must never affect speech
            log.debug("TTS feature usage instrumentation skipped")

    _reply_speech_tasks.create_task(_run())


def _speak_reply(context: ApiContext, envelope: Any, *, channel_value: Any, user_id: str) -> None:
    """Speak the reply if configured to, and never let that break the reply.

    Speech is a side effect of answering, so a failure to speak must not change
    what the caller gets back. One call site sits inside the account-gate
    preflight's own broad ``except``, where a raise here would fall through and
    run the command the gate just refused.
    """
    try:
        _speak_reply_now(context, envelope, channel_value=channel_value, user_id=user_id)
    # Deciding whether to speak must never change what the caller gets back.
    except Exception as exc:  # noqa: BLE001, RUF100
        log.debug("Spoken reply skipped: %s", exc)


def _get_command_service(context: ApiContext) -> Any:
    if context.command_service is not None:
        context.app.state.command_service = context.command_service
        return context.command_service

    existing = getattr(context.app.state, "command_service", None)
    if existing is not None:
        return existing

    from services.command import CommandService, CommandServiceContext
    from services.command.idempotency import IdempotencyLedger

    ledger = IdempotencyLedger(_command_ledger_path())
    ledger.reconcile()
    service = CommandService(
        context=CommandServiceContext(
            state=context.bindings.state,
            music=context.bindings.music,
            intent=context.bindings.intent,
            hub=context.hub,
            bindings=context.bindings,
            commands_total=context.commands_total,
            play_events_total=context.play_events_total,
            idempotency_ledger=ledger,
        )
    )
    context.app.state.command_service = service
    return service


def register_command_routes(context: ApiContext) -> None:
    """Register the canonical HTTP command route."""
    if _route_exists(context, "/v1/command", "POST"):
        log.debug("Skipping /v1/command registration because it already exists")
        return

    router = context.router
    app = context.app
    state = context.bindings.state
    music = context.bindings.music
    command_service = _get_command_service(context)

    @router.post("/v1/command", dependencies=[Depends(require_auth)])
    async def command(request: Request, body: dict[str, Any]) -> ResponseEnvelope:
        body = body or {}
        text = body.get("text") or body.get("command", "")
        raw_stream_id = body.get("stream_id") or request.headers.get("X-Stream-Id")
        stream_id: str | None = None
        if raw_stream_id:
            from services.llm.stream_bus import normalize_stream_id

            try:
                stream_id = normalize_stream_id(raw_stream_id)
            except ValueError:
                raise HTTPException(
                    status_code=HTTPStatus.BAD_REQUEST,
                    detail=failure_response(
                        "invalid_stream_id",
                        "Invalid stream id.",
                        data={"intent": "unknown"},
                    ),
                ) from None

        idempotency_key = (
            request.headers.get("Idempotency-Key")
            or str(body.get("idempotency_key") or body.get("request_id") or "").strip()
        )
        request_user = getattr(request.state, "user", None)
        user_id = _authenticated_user_id_for_request(request)
        is_valid, error_msg = validate_command_text(text)
        if not is_valid:
            log.debug("Command validation rejected: %s", error_msg)
            error_text = error_msg or "Your request couldn't be processed. Please try rephrasing."
            is_length_error = "too long" in error_text.lower()
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST,
                detail=failure_response(
                    "command_too_long" if is_length_error else "invalid_command",
                    error_text if is_length_error else "Your request couldn't be processed. Please try rephrasing.",
                    data={"intent": "unknown"},
                ),
            )

        if stream_id:
            from services.llm.stream_bus import bind_stream_producer

            try:
                bind_stream_producer(stream_id, owner_id=user_id)
            except PermissionError:
                raise HTTPException(
                    status_code=HTTPStatus.FORBIDDEN,
                    detail=failure_response(
                        "stream_access_denied",
                        "Stream access denied.",
                        data={"intent": "unknown"},
                    ),
                ) from None
            except FileExistsError:
                raise HTTPException(
                    status_code=HTTPStatus.CONFLICT,
                    detail=failure_response(
                        "stream_already_claimed",
                        "Stream already has an active command.",
                        data={"intent": "unknown"},
                    ),
                ) from None
            except ValueError:
                raise HTTPException(
                    status_code=HTTPStatus.BAD_REQUEST,
                    detail=failure_response(
                        "invalid_stream_id",
                        "Invalid stream id.",
                        data={"intent": "unknown"},
                    ),
                ) from None

        idempotency_claim = None
        if idempotency_key:
            if len(idempotency_key) > 512:
                raise HTTPException(
                    status_code=HTTPStatus.BAD_REQUEST,
                    detail=failure_response(
                        "invalid_idempotency_key",
                        "Idempotency-Key is too long.",
                        data={"intent": "unknown"},
                    ),
                )
            from services.idempotency import HTTP_IDEMPOTENCY_TTL_SECONDS, get_idempotency_store

            idempotency_claim = await get_idempotency_store().claim_http_response(
                user_id=user_id,
                idempotency_key=idempotency_key,
                ttl_seconds=HTTP_IDEMPOTENCY_TTL_SECONDS,
            )
            if idempotency_claim.response is not None:
                log.info("Idempotent /v1/command replay for user=%s", user_id)
                if stream_id:
                    from services.llm.stream_bus import finalize_stream

                    finalize_stream(stream_id, content=_command_stream_message(idempotency_claim.response))
                return idempotency_claim.response
            if idempotency_claim.in_progress:
                if stream_id:
                    from services.llm.stream_bus import finalize_stream

                    finalize_stream(
                        stream_id,
                        error=True,
                        message="That command is still running. Wait for it to finish instead of retrying.",
                    )
                return JSONResponse(
                    status_code=HTTPStatus.CONFLICT,
                    content=failure_response(
                        "idempotent_request_in_progress",
                        "That command is still running. Wait for it to finish instead of retrying.",
                        data={"intent": "unknown"},
                    ),
                )

        try:
            from admin.instrumentation import record_verified_command_funnel

            record_verified_command_funnel(
                user_id=user_id,
                email_verified=bool(getattr(request_user, "email_verified", False)),
                signup_at=getattr(request_user, "created_at", None),
            )
        except Exception as exc:
            log.debug("Command funnel metric skipped: %s", exc)

        # Resolved before the account gate because both the gate's reply and the
        # command's reply are spoken through the same channel decision.
        channel_value = body.get("origin_channel", body.get("channel", "http"))
        if isinstance(channel_value, str):
            channel_meta_keys = (
                "phone_number",
                "caller_number",
                "from_number",
                "to_number",
                "email",
                "email_address",
            )
            channel_meta = {key: body.get(key) for key in channel_meta_keys if body.get(key)}
            if channel_meta:
                channel_value = {
                    "origin_channel": channel_value,
                    **channel_meta,
                }

        try:
            from config.defaults import DEFAULT_AI_SOURCE
            from core.account_gate import (
                account_required_envelope_data,
                requires_account_for_command,
            )
            from ui.settings_manager import get_settings_manager

            settings_mgr = get_settings_manager()
            try:
                from config.settings import settings as app_settings

                ai_source_override = (getattr(app_settings, "ai_source_override", "") or "").strip()
            except Exception:
                ai_source_override = ""
            ai_source = ai_source_override or settings_mgr.get("ai_source", DEFAULT_AI_SOURCE, user_id=user_id)
            if requires_account_for_command(user_id, ai_source):
                envelope = success_response(account_required_envelope_data())
                try:
                    hub = getattr(app.state, "event_hub", None)
                    if hub is not None:
                        import time as _time

                        data = envelope.get("data") if isinstance(envelope, dict) else None
                        if isinstance(data, dict):
                            await hub.broadcast(
                                "chat_response",
                                {
                                    "text": data.get("message", ""),
                                    "intent": data.get("intent", "account_required"),
                                    "timestamp": _time.time(),
                                    "card": data.get("card"),
                                },
                                user_id=user_id,
                                force=True,
                            )
                except Exception as exc:
                    log.debug("account_required broadcast failed: %s", exc)
                if idempotency_claim is not None and idempotency_claim.is_owner:
                    from services.idempotency import get_idempotency_store

                    await get_idempotency_store().complete_http_response(idempotency_claim.cache_key, envelope)
                if stream_id:
                    from services.llm.stream_bus import finalize_stream

                    finalize_stream(stream_id, content=_command_stream_message(envelope))
                _speak_reply(context, envelope, channel_value=channel_value, user_id=user_id)
                return envelope
        except Exception as exc:
            log.debug("account_required preflight raised; falling through: %s", exc)

        try:
            from services.command import CommandRequest

            log.debug("Calling command_service.execute with text_chars=%d", len(text))

            command_request = CommandRequest(
                text=text,
                history=body.get("history") if "history" in body else None,
                user_id=user_id,
                channel=channel_value,
                request_id=idempotency_key or None,
                trace_id=body.get("trace_id"),
            )
            if stream_id:
                from services.llm.stream_bus import command_stream_context

                stream_context = command_stream_context(stream_id)
            else:
                stream_context = contextlib.nullcontext()
            with stream_context:
                result = await command_service.execute(command_request)

            if "rate_limited" in result.policy_flags:
                if idempotency_claim is not None and idempotency_claim.is_owner:
                    from services.idempotency import get_idempotency_store

                    await get_idempotency_store().abandon_http_response(idempotency_claim.cache_key)
                if stream_id:
                    from services.llm.stream_bus import finalize_stream

                    finalize_stream(
                        stream_id,
                        error=True,
                        message=str(result.data.get("message") or "Rate limit exceeded."),
                    )
                return build_429_response(result)

            envelope = result.to_envelope()

            try:
                hub = getattr(app.state, "event_hub", None)
                if hub is not None:
                    from ui.core.player_state import to_player_state

                    hub_authority = getattr(app.state, "hub_state_authority", None)
                    player_state = await asyncio.to_thread(
                        lambda: to_player_state(music, state, hub_authority=hub_authority).model_dump()
                    )
                    await hub.broadcast("state", player_state, user_id=user_id, force=True)
            except Exception as exc:
                log.debug("State broadcast after command failed: %s", exc)

            try:
                hub = getattr(app.state, "event_hub", None)
                if hub is not None:
                    import time as _time

                    data = envelope.get("data") if isinstance(envelope, dict) else None
                    message = data.get("message", "") if isinstance(data, dict) else ""
                    if message:
                        payload = {
                            "text": message,
                            "intent": data.get("intent", ""),
                            "timestamp": _time.time(),
                        }
                        card = data.get("card") if isinstance(data, dict) else None
                        if card:
                            payload["card"] = card
                        pairing_flow = data.get("pairing_flow") if isinstance(data, dict) else None
                        if pairing_flow:
                            payload["pairing_flow"] = pairing_flow
                        ai_data = data.get("ai_data") if isinstance(data, dict) else None
                        if isinstance(ai_data, dict):
                            payload["ai_data"] = ai_data
                        for key in (
                            "ui_action",
                            "rooms_modal_tab",
                            "path_identifier",
                            "panel_id",
                            "tab",
                            "sub_tab",
                            "section",
                            "initial_section",
                            "prefill",
                            "room_name",
                            "target_room",
                        ):
                            if isinstance(data, dict) and key in data:
                                payload[key] = data[key]
                        await hub.broadcast(
                            "chat_response",
                            payload,
                            user_id=user_id,
                            force=True,
                        )
            except Exception as exc:
                log.debug("Chat response broadcast failed: %s", exc)

            if idempotency_claim is not None and idempotency_claim.is_owner:
                from services.idempotency import get_idempotency_store

                await get_idempotency_store().complete_http_response(idempotency_claim.cache_key, envelope)
            if stream_id:
                from services.llm.stream_bus import finalize_stream

                finalize_stream(stream_id, content=_command_stream_message(envelope))
            _speak_reply(context, envelope, channel_value=channel_value, user_id=user_id)
            return envelope
        except Exception as exc:
            log.exception("Command execution failed")
            if idempotency_claim is not None and idempotency_claim.is_owner:
                try:
                    from services.idempotency import get_idempotency_store

                    await get_idempotency_store().abandon_http_response(idempotency_claim.cache_key)
                except Exception as abandon_exc:
                    log.debug("Failed to abandon idempotency claim after command error: %s", abandon_exc)
            if stream_id:
                try:
                    from services.llm.stream_bus import finalize_stream

                    finalize_stream(stream_id, error=True, message="Command failed. Please try again.")
                except Exception as stream_exc:
                    log.debug("Failed to finalize command stream after error: %s", stream_exc)
            return JSONResponse(
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                content=failure_response(
                    "command_execution_failed",
                    "Command failed. Please try again.",
                    data={
                        "intent": "unknown",
                    },
                ),
            )

    log.info("Command route registered at /v1/command")


__all__ = ["register_command_routes"]
