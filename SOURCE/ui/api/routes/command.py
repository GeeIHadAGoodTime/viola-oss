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
# route defaults to "http") and the web chat. Saved ChatMode threads have
# their own completion route, which invokes this same policy with "chat_mode".
# The set itself is ``core.reply_speech_settings.LOCALLY_SPOKEN_CHANNELS``, shared
# with the line that says a background task has finished.

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

    The three settings are read by ``core.reply_speech_settings`` (shared with
    the line that says a background task has finished); its docstring explains
    why every default there comes from ``config.defaults`` and never a literal.

    ``voice_muted`` is checked here because this is the first code that decides
    whether to speak from user settings, and a path that speaks straight
    through the user's own mute would be a new bug rather than a fixed one.
    """
    from core.reply_speech_settings import is_locally_spoken_channel, read_reply_speech_settings

    if not is_locally_spoken_channel(channel_key(channel_value)):
        return False
    if not is_desktop_surface():
        return False

    return read_reply_speech_settings(user_id).typed_allowed


def _new_streamed_reply(
    context: ApiContext,
    *,
    channel_value: Any,
    user_id: str,
    stream_id: str | None,
    show_on_stage: bool = True,
) -> Any | None:
    """A reply that may start speaking while the model still writes it, or None.

    Only when this reply would be spoken at all (``_should_speak_reply``) and
    the engine can speak a stream. Push-to-talk, touch and Chat tab turns use it.
    """
    try:
        if not _should_speak_reply(channel_value, user_id):
            return None
        engine = _reply_tts_engine(context)
        from voice.streamed_reply import new_streamed_reply

        def _start(reply: Any) -> Any:
            async def _run() -> None:
                try:
                    task = asyncio.ensure_future(reply.speak())
                    while True:
                        done, _pending = await asyncio.wait({task}, timeout=_SPEAK_REPLY_TIMEOUT_SECONDS)
                        if done:
                            task.result()
                            break
                        # A reading the user asked for (read_aloud) may run longer
                        # than the backstop; anything else is abandoned as before.
                        if not reply.gate.read_in_full:
                            task.cancel()
                            raise TimeoutError
                except TimeoutError:
                    log.warning("Spoken reply exceeded %.0fs; abandoning", _SPEAK_REPLY_TIMEOUT_SECONDS)
                except Exception as exc:  # noqa: BLE001, RUF100 - a reply that cannot be spoken is still a reply
                    log.warning("Streamed spoken reply failed: %s", exc)

            return _reply_speech_tasks.create_task(_run())

        return new_streamed_reply(
            engine,
            user_id=user_id,
            stream_id=stream_id,
            start_speaking=_start,
            show_on_stage=show_on_stage,
        )
    # Deciding whether to speak must never change what the caller gets back.
    except Exception as exc:  # noqa: BLE001, RUF100
        log.debug("Streamed spoken reply skipped: %s", exc)
        return None


def _silent_stage_reply(channel_value: Any, stream_id: str | None, user_id: str) -> Any:
    """Live stage text for a reply on a local text surface that is not being spoken."""
    from core.reply_speech_settings import is_locally_spoken_channel

    if not is_locally_spoken_channel(channel_key(channel_value)) or not is_desktop_surface():
        return contextlib.nullcontext()
    from voice.stage_reply import stage_reply

    return stage_reply(stream_id, user_id=user_id)


def _is_quick_music_action(data: Any) -> bool:
    try:
        from services.llm.answer_stream import is_quick_music_action
    except Exception:  # noqa: BLE001, RUF100 - without the rule the reply is spoken as before
        return False
    return is_quick_music_action(data)


def _speak_reply_now(
    context: ApiContext,
    envelope: Any,
    *,
    channel_value: Any,
    user_id: str,
    streamed_reply: Any | None = None,
) -> None:
    """Speak the reply aloud when the user has asked for every reply to be spoken.

    Fire-and-forget on purpose. The caller already has the reply text -- it is
    in this envelope and it went out over the ``chat_response`` broadcast
    before this runs -- so holding the HTTP response open for the length of the
    speech would only add latency and put long replies past client budgets
    (``ui/qt_native/window_core.py`` posts with 10s).

    With a ``streamed_reply`` that already started speaking, only the part of
    the final reply not yet spoken is added to it.
    """
    message = _command_stream_message(envelope)
    data = envelope.get("data") if isinstance(envelope, dict) else None
    already_spoken = bool(isinstance(data, dict) and data.get("spoken"))
    if streamed_reply is not None:
        started = bool(streamed_reply.started)
        streamed_reply.finish(message, already_spoken=already_spoken or not started)
        if started:
            return
    if not _should_speak_reply(channel_value, user_id):
        return
    if not message:
        return
    quick_music_action = _is_quick_music_action(data)
    if already_spoken and not quick_music_action:
        # Something upstream already voiced this turn; speaking it again would
        # double it. Same contract the voice handler reads. (A quick music
        # action such as "next" is marked spoken so no words follow the track
        # change; it still gets the done tone below.)
        return
    engine = _reply_tts_engine(context)
    speak = getattr(engine, "speak", None) if engine is not None else None
    if not callable(speak):
        return
    timeout = _SPEAK_REPLY_TIMEOUT_SECONDS
    try:
        from voice.streamed_reply import play_done_tone, whole_reply_speech
    except Exception:  # noqa: BLE001, RUF100 - without the long-answer rule the reply is still spoken
        log.debug("Long-answer rule unavailable; speaking the reply whole", exc_info=True)
    else:
        # Long answers say their lead and point at the card; a turn of quick
        # music actions that all worked gets the done tone, not words.
        spoken_text = whole_reply_speech(message, engine, streamed_reply, result=data)
        if spoken_text is None:
            play_done_tone()
            return
        message = spoken_text
        reading = None
        try:
            from voice.streamed_reply import full_reading

            reading = full_reading(message, engine, streamed_reply)
        except Exception:  # noqa: BLE001, RUF100 - a capped reading is still a reading
            log.debug("Full reading unavailable", exc_info=True)
        if reading is not None:
            # "Read it out loud": the whole answer, with no length cap; the
            # backstop grows with it (about 17 characters a second spoken).
            full = reading

            def speak(_text: str) -> Any:
                return full.speak()

            timeout = max(_SPEAK_REPLY_TIMEOUT_SECONDS, len(message) / 8.0)
        elif streamed_reply is not None and streamed_reply.gate.read_in_full:
            # An engine that cannot stream caps one speak() at 800 characters or
            # 10 sentences (voice/synthesis/text_normalizer.py): "read it out
            # loud" on this PC's pyttsx3 fallback said 832 of 3,012 characters
            # (build-12 live check). Say it in parts that each fit.
            parts = _whole_reading_parts(message)
            whole_speak = speak

            async def speak(_text: str) -> None:
                for part in parts:
                    spoken = whole_speak(part)
                    if asyncio.iscoroutine(spoken):
                        await spoken

            timeout = max(_SPEAK_REPLY_TIMEOUT_SECONDS, len(message) / 8.0)

    async def _run() -> None:
        try:
            spoken = speak(message)
            if asyncio.iscoroutine(spoken):
                await asyncio.wait_for(spoken, timeout=timeout)
        except TimeoutError:
            log.warning("Spoken reply exceeded %.0fs; abandoning", timeout)
            return
        # An audio device can fail any way at all, and a reply that cannot be
        # spoken is still a reply.
        except Exception as exc:  # noqa: BLE001, RUF100
            # The engine already retried once without unspeakable tokens; name
            # them (never the reply text) so a silent reply is diagnosable.
            log.warning("Spoken reply failed: %s tokens=%s", exc, list(getattr(exc, "tokens", ())))
            return
        try:
            from admin.instrumentation import record_feature_used

            record_feature_used("tts")
        except Exception:  # noqa: BLE001, RUF100 - usage telemetry must never affect speech
            log.debug("TTS feature usage instrumentation skipped")

    _reply_speech_tasks.create_task(_run())


_READING_PART_CHARS = 600
_READING_PART_SENTENCES = 8


def _whole_reading_parts(text: str) -> list[str]:
    """Split a full reading into parts that each stay under a whole-reply speak() cap."""
    from services.llm.answer_stream import all_sentences

    parts: list[str] = []
    current: list[str] = []
    size = 0
    for sentence in all_sentences(text) or [text]:
        if current and (size + len(sentence) + 1 > _READING_PART_CHARS or len(current) >= _READING_PART_SENTENCES):
            parts.append(" ".join(current))
            current, size = [], 0
        current.append(sentence)
        size += len(sentence) + 1
    if current:
        parts.append(" ".join(current))
    return parts


def _speak_reply(
    context: ApiContext,
    envelope: Any,
    *,
    channel_value: Any,
    user_id: str,
    streamed_reply: Any | None = None,
) -> None:
    """Speak the reply if configured to, and never let that break the reply.

    Speech is a side effect of answering, so a failure to speak must not change
    what the caller gets back. One call site sits inside the account-gate
    preflight's own broad ``except``, where a raise here would fall through and
    run the command the gate just refused.
    """
    try:
        _speak_reply_now(
            context,
            envelope,
            channel_value=channel_value,
            user_id=user_id,
            streamed_reply=streamed_reply,
        )
    # Deciding whether to speak must never change what the caller gets back.
    except Exception as exc:  # noqa: BLE001, RUF100
        log.debug("Spoken reply skipped: %s", exc)


# The fields of a command result that ask the app to do something on screen
# (open a panel, a settings tab, a room). Copied into the stage's
# ``chat_response`` broadcast and into the Chat tab's final stream event.
_UI_ACTION_KEYS = (
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
)


def ui_action_fields(data: Any) -> dict[str, Any]:
    """The on-screen actions a command result asked for (ai_data included), or {}."""
    if not isinstance(data, dict):
        return {}
    fields: dict[str, Any] = {}
    ai_data = data.get("ai_data")
    if isinstance(ai_data, dict):
        fields["ai_data"] = ai_data
    for key in _UI_ACTION_KEYS:
        if key in data:
            fields[key] = data[key]
    return fields


def _ask_user_questions(stream_id: str | None) -> list[str]:
    """Questions ask_user put to the user this turn that are still waiting for an answer.

    A typed turn cannot wait for a reply mid-request, so ask_user returns the
    question for the model to put in its answer. When the answer does not
    include it, the user never sees what was asked (1.0.5 build 11, bug 20).
    """
    if not stream_id:
        return []
    try:
        from services.llm.stream_bus import get_stream_tool_events
    except Exception:  # noqa: BLE001, RUF100 - no stream registry means no questions to show
        return []
    questions: list[str] = []
    for event in get_stream_tool_events(stream_id):
        if str(event.get("tool_name") or event.get("name") or "") != "ask_user":
            continue
        if str(event.get("status") or "") != "ok":
            continue
        output = event.get("tool_output")
        if isinstance(output, str) and output and "needs_user_reply" not in output:
            continue  # answered in the same request (a channel that can wait)
        tool_input = event.get("tool_input")
        question = tool_input.get("question") if isinstance(tool_input, dict) else None
        if isinstance(question, str) and question.strip() and question.strip() not in questions:
            questions.append(question.strip())
    return questions


def _folded(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def show_pending_questions(envelope: Any, stream_id: str | None) -> None:
    """Put a waiting ask_user question into the reply when the reply leaves it out (in place)."""
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict):
        return
    message = data.get("message")
    if not isinstance(message, str):
        message = ""
    missing = [q for q in _ask_user_questions(stream_id) if _folded(q) not in _folded(message)]
    if not missing:
        return
    data["message"] = "\n\n".join(part for part in (message.strip(), *missing) if part)


class TypedTurn:
    """What every typed command shares around ``CommandService.execute``.

    Used by ``POST /v1/command`` (stage, push-to-talk) and by the Chat tab
    (``chat_mode.py``), so both behave the same: the reply may speak while it
    is written, the speech gate sees the turn's tool events (a turn of quick
    music actions gets the done tone instead of words, ``read_aloud`` reads the
    whole answer, a long answer says its lead), and the on-screen actions a
    tool asked for travel with the result.
    """

    def __init__(
        self,
        context: ApiContext,
        *,
        channel_value: Any,
        user_id: str,
        stream_id: str | None,
        show_on_stage: bool = True,
    ) -> None:
        self._context = context
        self.channel_value = channel_value
        self.user_id = user_id
        self.stream_id = stream_id
        self._show_on_stage = show_on_stage
        # Speak the reply while the model writes it (when it is spoken at all).
        self.streamed_reply = _new_streamed_reply(
            context,
            channel_value=channel_value,
            user_id=user_id,
            stream_id=stream_id,
            show_on_stage=show_on_stage,
        )

    @contextlib.contextmanager
    def running(self) -> Any:
        """Wrap ``execute``: the speech gate and live stage text listen to the turn's stream."""
        if self.streamed_reply is not None:
            reply_context = self.streamed_reply.attached()
        elif self._show_on_stage:
            # A reply that is not spoken still shows on the stage as it is written.
            reply_context = _silent_stage_reply(self.channel_value, self.stream_id, self.user_id)
        else:
            reply_context = contextlib.nullcontext()
        try:
            with reply_context:
                yield self
        except BaseException:
            self.cancel()
            raise

    def cancel(self) -> None:
        """No speech for this turn (failed, rate limited or stopped); safe to call twice."""
        reply, self.streamed_reply = self.streamed_reply, None
        if reply is not None:
            reply.cancel()

    def stop_speaking(self) -> str:
        """The user pressed Stop: this turn says nothing more and its speech is cut now.

        Only this turn's own speech: when its reply has not started speaking,
        nothing is called on the engine (another reply or a reminder playing
        goes on). Safe to call twice. Returns what was done, for the Stop log line.
        """
        reply, self.streamed_reply = self.streamed_reply, None
        if reply is None:
            return "no speech"
        silence = getattr(reply, "silence", None)
        if not callable(silence):
            reply.cancel()
            return "queue closed"
        return "cut" if silence() else "not speaking yet; queue dropped"

    def finish_envelope(self, envelope: Any) -> dict[str, Any]:
        """Complete the reply text in place; return the on-screen actions it asked for."""
        show_pending_questions(envelope, self.stream_id)
        data = envelope.get("data") if isinstance(envelope, dict) else None
        return ui_action_fields(data)

    def speak(self, envelope: Any) -> None:
        """Speak the finished reply (or play the done tone) under the user's settings."""
        _speak_reply(
            self._context,
            envelope,
            channel_value=self.channel_value,
            user_id=self.user_id,
            streamed_reply=self.streamed_reply or _tools_only_reply(self.stream_id),
        )


class _ToolsOnlyReply:
    """The speech gate's view of a turn whose reply is not streamed live.

    With no live reply (an engine that cannot stream, or a hook that may replace
    the answer) the gate never saw the turn's tools, so the done tone and
    read_aloud were lost: "set the volume to 30" spoke words on this PC's
    pyttsx3 fallback (build-12 live check). The stream bus records every tool
    event of the turn, so the gate is fed from that record at the end.
    """

    started = False

    def __init__(self, gate: Any) -> None:
        self.gate = gate

    def finish(self, final_message: str | None, *, already_spoken: bool = False) -> str:
        return ""


def _tools_only_reply(stream_id: str | None) -> _ToolsOnlyReply | None:
    if not stream_id:
        return None
    try:
        from services.llm.answer_stream import FinalAnswerSpeechGate
        from services.llm.stream_bus import get_stream_tool_events

        events = get_stream_tool_events(stream_id)
        if not events:
            return None
        gate = FinalAnswerSpeechGate(live=False, reason="not_streamed")
        for event in events:
            gate.feed({"tool": event})
        return _ToolsOnlyReply(gate)
    except Exception:  # noqa: BLE001, RUF100 - without the record the reply is spoken as before
        log.debug("Tool record for the speech gate unavailable", exc_info=True)
        return None


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
            turn = TypedTurn(
                context,
                channel_value=channel_value,
                user_id=user_id,
                stream_id=stream_id,
            )
            with stream_context, turn.running():
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
                turn.cancel()
                return build_429_response(result)

            envelope = result.to_envelope()
            ui_fields = turn.finish_envelope(envelope)

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
                        payload.update(ui_fields)
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
            turn.speak(envelope)
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
