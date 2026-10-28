from __future__ import annotations

import asyncio

from fastapi.responses import JSONResponse

from contracts.api_response import failure_response
from core.logging_config import get_logger
from core.task_tracker import TaskTracker
from fastapi import Body, Depends
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth
from ui.api.routes.common import RouteToolbox
from ui.api.routes.error_handler import handle_route_error

log = get_logger(__name__)

# Narration is fire-and-forget, so its task needs an owner or it would be
# garbage-collected mid-sentence.
_narration_tasks = TaskTracker()
_SPEAK_TIMEOUT_SECONDS = 120.0


class _NarrationSlot:
    """The one first-run narration line that may be playing right now.

    First run used to let narration lines pile up: every step change asked for a
    new line while the previous one was still being spoken, the TTS engine
    queued them, and the user heard step 2's line while looking at step 4.
    Only the newest line is ever wanted, so starting one cuts the last, and the
    frontend cuts the current one when the user moves on.
    """

    def __init__(self) -> None:
        self.seq = 0
        self.task: asyncio.Task[None] | None = None


_narration = _NarrationSlot()


def _stop_audio_playback() -> None:
    """Silence the line that is playing now.

    Cancelling the narration task only stops it at its next await; the sentence
    already handed to the sound device keeps playing to the end. ``sd.stop()``
    ends that ``sd.play()`` and releases the engine's blocking ``sd.wait()``.
    """
    try:
        import sounddevice as sd

        sd.stop()
    # A missing or failing audio stack just means there is nothing to silence.
    except Exception as exc:  # noqa: BLE001, RUF100
        log.debug("Narration playback stop failed: %s", exc)


async def _cut_current_narration() -> bool:
    """Stop the narration line in flight, if any. Returns whether one was cut."""
    task = _narration.task
    _narration.task = None
    if task is None or task.done():
        return False
    task.cancel()
    # Blocking PortAudio call: keep it off the event loop. Awaited so a
    # following line cannot be started and then silenced by this stop.
    await asyncio.to_thread(_stop_audio_playback)
    return True


def desktop_account_status() -> dict[str, object]:
    """The Viola account this desktop is signed in to, read from the backend.

    The same local read wake word and the agent use, so the Account page can
    show the backend's real sign-in state instead of only what the webview
    happened to restore (1.0.5 build 5, A2.7: the page showed the Sign In form
    while the backend was signed in). No network call, no token refresh.
    """
    try:
        from core.user_context import is_cloud_surface

        if is_cloud_surface():
            # One desktop install has one account store; the cloud has none.
            return {"ok": True, "signed_in": False, "email": None}
        from auth.desktop_session import get_desktop_account_identity

        identity = get_desktop_account_identity()
    except Exception:  # noqa: BLE001, RUF100 - an unreadable store reads as signed out
        log.debug("Desktop account identity unreadable", exc_info=True)
        return {"ok": True, "signed_in": False, "email": None}
    if not getattr(identity, "signed_in", False):
        return {"ok": True, "signed_in": False, "email": None}
    email = getattr(identity, "email", None)
    return {"ok": True, "signed_in": True, "email": email if isinstance(email, str) and email else None}


def register_onboarding_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    router = context.router

    @router.get("/v1/onboarding/account", dependencies=[Depends(require_auth)])
    async def get_desktop_account():
        async def _inner():
            return await asyncio.to_thread(desktop_account_status)

        return await toolbox.record_and_call(_inner, route="/v1/onboarding/account", method="GET")

    @router.get("/v1/onboarding/status", dependencies=[Depends(require_auth)])
    async def get_onboarding_status():
        async def _inner():
            try:
                from ui.onboarding import get_onboarding_system
                from ui.settings_manager import get_settings_manager

                settings_mgr = get_settings_manager()
                onboarding_sys = get_onboarding_system(settings_mgr)
                return {
                    "ok": True,
                    "completed": onboarding_sys.is_onboarding_complete(),
                    "progress": onboarding_sys.get_progress(),
                }
            except Exception as exc:
                log.warning("Failed to get onboarding status: %s", exc)
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "onboarding_status_unavailable",
                        "Onboarding status is temporarily unavailable.",
                        data={"completed": True},
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/onboarding/status", method="GET")

    @router.get("/v1/onboarding/steps", dependencies=[Depends(require_auth)])
    async def get_onboarding_steps():
        async def _inner():
            try:
                from ui.onboarding import get_onboarding_system
                from ui.settings_manager import get_settings_manager

                settings_mgr = get_settings_manager()
                onboarding_sys = get_onboarding_system(settings_mgr)
                steps = [onboarding_sys.to_dict(step) for step in onboarding_sys._steps]
                return {
                    "ok": True,
                    "steps": steps,
                    "current_step": onboarding_sys._current_step,
                    "progress": onboarding_sys.get_progress(),
                }
            except Exception as exc:
                log.warning("Failed to get onboarding steps: %s", exc)
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "onboarding_steps_unavailable",
                        "Onboarding steps are temporarily unavailable.",
                        data={"steps": [], "current_step": 0},
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/onboarding/steps", method="GET")

    @router.post("/v1/onboarding/save", dependencies=[Depends(require_auth)])
    async def save_onboarding_step(body: dict = Body(...)):
        async def _inner():
            try:
                from ui.onboarding import OnboardingStep, get_onboarding_system
                from ui.settings_manager import get_settings_manager

                settings_mgr = get_settings_manager()
                onboarding_sys = get_onboarding_system(settings_mgr)

                step_id_str = body.get("step_id")
                raw_data = body.get("data", {})
                data = raw_data if isinstance(raw_data, dict) else {}
                step_id = OnboardingStep(step_id_str)
                persisted_step_data = dict(data)
                if step_id == OnboardingStep.AI_SETUP and "api_key" in persisted_step_data:
                    if persisted_step_data.get("api_key"):
                        persisted_step_data["api_key_saved"] = True
                    persisted_step_data.pop("api_key", None)
                elif step_id == OnboardingStep.MICROPHONE_TEST:
                    if any(key in persisted_step_data for key in ("transcript", "heard_text", "heardText")):
                        persisted_step_data["sample_detected"] = True
                    persisted_step_data.pop("transcript", None)
                    persisted_step_data.pop("heard_text", None)
                    persisted_step_data.pop("heardText", None)
                onboarding_sys.save_step_data(step_id, persisted_step_data)

                if step_id == OnboardingStep.VOICE_MODE and "voice_mode" in data:
                    settings_mgr.set("voice_mode", data["voice_mode"])
                elif step_id == OnboardingStep.AI_SETUP and "api_key" in data and data["api_key"]:
                    # BYOK path: write the API key through SettingsManager so it
                    # lands on the canonical ``llm_api_key`` surface that
                    # ``services.llm.factory.LLMFactory`` reads at runtime. The
                    # previous flow wrote to keyring slot ``viola/openai_api_key``
                    # and fell back to setting ``openai_api_key_encrypted`` — both
                    # are dead ends: no factory or AppConfig reader consumes
                    # them, so the user's BYOK key was silently dropped.
                    #
                    # SettingsManager routes secret-classed keys through the
                    # encrypted secret store (OS-keyring-backed via
                    # ``utils.secure_credentials``) and falls back to a
                    # restricted-perms file when keyring is unavailable. Either
                    # way, ``llm_api_key`` is the value the LLM factory reads,
                    # which is what BYOK requires.
                    settings_mgr.set("llm_api_key", data["api_key"])
                    settings_mgr.set("llm_provider", "openai")
                    settings_mgr.set("ai_enabled", True)
                    settings_mgr.set("enable_gpt", True)
                    log.info("BYOK api key stored via SettingsManager -> llm_api_key")

                return {"ok": True}
            except Exception as exc:
                log.warning("Failed to save onboarding step: %s", exc)
                return handle_route_error(exc, "save_onboarding_step")

        return await toolbox.record_and_call(_inner, route="/v1/onboarding/save", method="POST")

    @router.get("/v1/onboarding/check-mic-permission", dependencies=[Depends(require_auth)])
    async def check_mic_permission():
        """Check OS-level microphone permission before the capture test.

        Returns ok=True if permission is granted (or check is not applicable),
        ok=False with an error message if permission is denied.
        Called by the onboarding frontend before the mic capture test begins.
        """

        async def _inner():
            try:
                from ui.onboarding import MIC_BLOCKED, MIC_GRANTED, check_mic_permission_windows

                # Opening a PortAudio input stream blocks for most of a second
                # (measured ~0.9s on a healthy device) and it used to run
                # directly on the event loop, stalling every other local API
                # request for its duration. Same worker-thread hop that
                # ``complete_onboarding`` below already uses.
                state, message = await asyncio.to_thread(check_mic_permission_windows)
                # The check SUCCEEDING is ok:True regardless of the result — a
                # denied-but-successfully-observed permission is a valid negative
                # answer carried in ``permission_granted``, not an operation
                # failure. (Reserving ok:False for the exception path keeps the
                # false-success seam from turning a normal "denied" into HTTP 500.)
                #
                # ``permission_state`` is the honest three-way answer. This probe
                # cannot see the stack that actually captures (Chromium
                # getUserMedia), so "unknown" must stay distinguishable from
                # "blocked" — the frontend treats only a real capture failure as
                # a problem and uses this purely for guidance text.
                return {
                    "ok": True,
                    "permission_state": state,
                    "permission_granted": state == MIC_GRANTED,
                    "error_message": (message if state != MIC_GRANTED else None),
                    "blocked": state == MIC_BLOCKED,
                }
            except Exception as exc:
                log.warning("Mic permission check failed: %s", exc)
                # This is a last-resort fallback for an unexpected exception
                # (e.g. an import failure), not the normal denied/granted
                # path — check_mic_permission_windows() already returns
                # cleanly (no raise) on non-Windows hosts. Stay platform-
                # neutral here rather than naming a specific OS's settings
                # path (C-078: this used to hardcode Windows-only copy).
                return {
                    "ok": False,
                    "permission_state": "unknown",
                    "permission_granted": False,
                    "error_message": "Viola could not confirm microphone access. Check your microphone privacy settings and try again.",
                    "blocked": False,
                }

        return await toolbox.record_and_call(_inner, route="/v1/onboarding/check-mic-permission", method="GET")

    @router.post("/v1/onboarding/complete", dependencies=[Depends(require_auth)])
    async def complete_onboarding():
        async def _inner():
            try:
                from ui.onboarding import get_onboarding_system
                from ui.settings_manager import get_settings_manager

                settings_mgr = get_settings_manager()
                onboarding_sys = get_onboarding_system(settings_mgr)
                # Worker-thread hop: onboarding persistence can spawn a
                # subprocess — keep it off the event loop.
                persisted = await asyncio.to_thread(onboarding_sys.mark_onboarding_complete)
                if not persisted:
                    # Answering ok:True on a failed write is what made this
                    # silent: the user finished first run, the flag never
                    # landed, and every later launch walked them through the
                    # whole thing again with no error anywhere.
                    return JSONResponse(
                        status_code=500,
                        content=failure_response(
                            "onboarding_completion_not_persisted",
                            "Viola could not save that you finished setup, so it may ask again next time.",
                        ),
                    )
                return {"ok": True}
            except Exception as exc:
                log.debug("Operation failed: %s", exc)
                return handle_route_error(exc, "complete_onboarding")

        return await toolbox.record_and_call(_inner, route="/v1/onboarding/complete", method="POST")

    @router.post("/v1/tts/speak", dependencies=[Depends(require_auth)])
    async def tts_speak(body: dict = Body(...)):
        """Speak a line of text aloud in Viola's own voice.

        First-run narration called this route from day one
        (``ui/react-app/src/hooks/useVoiceOnboarding.js``) and it was never
        registered anywhere, so every narration line 404'd and fell back to the
        browser's Web Speech voice. That fallback does work inside QtWebEngine
        (measured: three SAPI voices available, ``speak()`` succeeds because the
        webview sets ``PlaybackRequiresUserGesture=False``), but it means a
        user's first ever encounter with Viola is narrated by a generic Windows
        system voice rather than by Viola.

        This route lives in the desktop-only onboarding group
        (``backend/cloud_route_manifest.py`` holds it off cloud), so cloud keeps
        returning 404 here — which is exactly what
        ``scripts/launch/condition4_launch_check.py``'s ``desktop_tts_spa_drift``
        probe expects.

        The reply reports whether speech was actually accepted, so a caller can
        fall back to browser speech instead of narrating to nobody.
        """

        async def _inner():
            try:
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    return JSONResponse(
                        status_code=400,
                        content=failure_response(
                            "tts_text_required",
                            "A non-empty 'text' field is required.",
                        ),
                    )

                from ui.settings_manager import get_settings_manager

                settings_mgr = get_settings_manager()
                # The user's own mute and master TTS switch still win. Nothing
                # else gates this: the caller asking for narration IS the
                # consent, so ``speak_all_replies`` (which governs whether
                # ordinary command replies are read aloud) is deliberately not
                # consulted here.
                if not bool(settings_mgr.get("tts_enabled", True)):
                    return {"ok": True, "spoken": False, "reason": "tts_disabled"}
                if bool(settings_mgr.get("voice_muted", False)):
                    return {"ok": True, "spoken": False, "reason": "muted"}

                intent = getattr(context.bindings, "intent", None)
                engine = getattr(intent, "tts", None) if intent is not None else None
                speak = getattr(engine, "speak", None) if engine is not None else None
                if not callable(speak):
                    return {"ok": True, "spoken": False, "reason": "no_engine"}

                async def _run() -> None:
                    try:
                        spoken = speak(text)
                        if asyncio.iscoroutine(spoken):
                            await asyncio.wait_for(spoken, timeout=_SPEAK_TIMEOUT_SECONDS)
                    except TimeoutError:
                        log.warning("Narration exceeded %.0fs; abandoning", _SPEAK_TIMEOUT_SECONDS)
                    # An audio device can fail any way at all, and losing the
                    # narration must never take the caller down with it.
                    except Exception as exc:  # noqa: BLE001, RUF100
                        log.warning("Narration failed: %s", exc)

                # A new line replaces the old one instead of queueing behind it.
                await _cut_current_narration()

                # Fire-and-forget on purpose: the caller paces itself and holding
                # the response open for the length of the sentence would stall
                # first run behind every line.
                _narration.seq += 1
                _narration.task = _narration_tasks.create_task(_run())
                return {"ok": True, "spoken": True, "narration_id": _narration.seq}
            # Narration is decoration. Any way this fails, first run continues
            # on the caller's browser-speech fallback rather than stopping.
            except Exception as exc:  # noqa: BLE001, RUF100
                log.warning("Failed to speak text: %s", exc)
                return handle_route_error(exc, "tts_speak")

        return await toolbox.record_and_call(_inner, route="/v1/tts/speak", method="POST")

    @router.post("/v1/tts/stop", dependencies=[Depends(require_auth)])
    async def tts_stop(body: dict | None = Body(default=None)):
        """Cut the first-run narration line that is playing.

        First run calls this when the user moves past a step, so a step's line
        never keeps talking over the next step. ``narration_id`` (from the speak
        reply) names the line the caller means; a stop for a line that a newer
        one already replaced is ignored, so a late stop cannot silence the next
        step's narration.
        """

        async def _inner():
            try:
                raw_id = body.get("narration_id") if isinstance(body, dict) else None
                if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id != _narration.seq:
                    return {"ok": True, "stopped": False, "reason": "superseded"}
                stopped = await _cut_current_narration()
                return {"ok": True, "stopped": stopped}
            # Narration is decoration; failing to stop it must not break first run.
            except Exception as exc:  # noqa: BLE001, RUF100
                log.warning("Failed to stop narration: %s", exc)
                return handle_route_error(exc, "tts_stop")

        return await toolbox.record_and_call(_inner, route="/v1/tts/stop", method="POST")

    log.info("🎓 Onboarding routes registered")
