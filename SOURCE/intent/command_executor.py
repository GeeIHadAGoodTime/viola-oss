"""
Command Executor and TTS Speaker classes for IntentPipeline.
Handles execution of voice commands and TTS feedback.

Room Routing:
    When params contain a ``target_room`` key, the executor routes the command
    to the appropriate target rather than executing locally:
    - Local room  -> execute locally (normal path)
    - Remote room -> forward via MultiRoomCommandForwarder.forward_to_room()
    - Group       -> forward via MultiRoomCommandForwarder.forward_to_group()
    - No match    -> execute locally and surface the in-house speaker-pairing flow
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict, cast

from config.defaults import DEFAULT_VOLUME_STEP
from core.json_types import JsonDict, to_json_value
from core.logging_config import get_logger
from core.task_tracker import TaskTracker
from utils.speaker_pairing_flow import (
    build_speaker_pairing_flow,
    build_speaker_pairing_payload,
    get_pairing_lan_ip,
    room_label,
)

try:
    from voice.synthesis.text_normalizer import (
        normalize_for_speech,  # type: ignore[import-not-found] # CLOUD-01: voice/ excluded from cloud build.
    )
except ImportError:
    # Cloud build excludes voice/ (Qt/audio/numpy/Kokoro stack). The formatter
    # is a best-effort markdown stripper for TTS; on cloud, downstream
    # messaging formatters handle channel-specific text shaping. Keep this
    # module importable with a no-op fallback rather than break the pipeline.
    def normalize_for_speech(text: str) -> str:  # type: ignore[no-redef] # CLOUD-01: no-op fallback when voice/ absent.
        return text


logger = get_logger(__name__)

_BROWSER_SPOKE_DEVICE_PREFIX = "browser-spoke:"
_NO_AUTH_MUSIC_PROVIDER_ID = "youtube_iframe"
_MUSIC_SEARCH_ACK = "Finding it..."
_PROVIDER_FAILURE_FALLBACK_MARKERS = (
    "auth",
    "connect",
    "login",
    "needs setup",
    "not connected",
    "reauth",
    "re-auth",
    "not linked",
    "not configured",
    "not registered",
    "provider unavailable",
    "provider is not available",
    "unsupported provider",
    "not supported",
    "sign in",
)
_MUSIC_PROVIDER_ALIASES = {
    "spotify": "spotify",
    "spotify cdp": "spotify",
    "spotify_cdp": "spotify",
    "youtube": "youtube_iframe",
    "youtube iframe": "youtube_iframe",
    "youtube_iframe": "youtube_iframe",
    "youtube music": "youtube_music",
    "youtube_music": "youtube_music",
    "local": "local",
    "local files": "local",
    "local library": "local",
    "my library": "local",
    "my local library": "local",
}
_MUSIC_PROVIDER_DISPLAY_NAMES = {
    "spotify": "Spotify",
    "youtube_iframe": "YouTube",
    "youtube_music": "YouTube Music",
    "local": "Local Library",
}
_MUSIC_PROVIDER_SOURCE_HINTS = {
    "local": "local",
    "spotify": "spotify_cdp",
}
_MUSIC_PROVIDER_FAMILIES = {
    "spotify": "spotify",
    "spotify_cdp": "spotify",
    "youtube": "youtube",
    "youtube_iframe": "youtube",
    "youtube_music": "youtube",
    "youtube_radio": "youtube",
    "ytsearch1": "youtube",
    "local": "local",
}


def _log_task_exception(task: asyncio.Task) -> None:
    """Log exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except Exception:
        return
    if exc:
        logger.error("Background task failed: %s", exc)


_PLAYBACK_VERIFY_TIMEOUT_SEC = 10.0
_PLAYBACK_VERIFY_POLL_SEC = 0.05
_EMBEDDED_PLAYBACK_VERIFY_TIMEOUT_SEC = 25.0
_EMBEDDED_YOUTUBE_PLAYBACK_MODES = frozenset({"embedded_iframe_webview", "embedded_webview"})


class CommandResult(TypedDict):
    success: bool
    message: str
    data: JsonDict
    error: str | None


# Type-only playback contract kept for the verifier return annotation.
class VerifyPlaybackResult(TypedDict):
    ok: bool
    message: str
    data: JsonDict
    error: str | None


class _AdapterEnvelopeError(Exception):
    """Raised when a music-adapter call returns an ``{"ok": False}`` envelope.

    The adapter (``backend/music_adapter.py``) converts player exceptions and
    browser-mode-unsupported checks into a failure envelope instead of
    raising, so a caller that just extracts a scalar off the result (as
    ``_apply_volume`` used to) silently discards the failure. Raising this
    from ``_apply_volume`` lets the volume handlers catch it and report the
    real outcome instead of echoing the requested level (#2758).
    """

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(str(error.get("message") or "The player reported a failure."))
        self.error = error


def _adapter_envelope_error(result: object, *, default_code: str) -> dict[str, Any] | None:
    """Extract the structured error from a music-adapter failure envelope.

    Mirrors ``ui/api/routes/control.py``'s ``_adapter_failure_error`` (#2736):
    the adapter returns ``{"ok": False, "error": ...}`` WITHOUT raising for
    browser-mode-unsupported / caught failures, so a caller that ignores the
    envelope turns a dead pause/resume/volume into a reported success (the
    MF-A/MF-B false-success class, #2758). Returns the normalized error dict
    when *result* is a failure envelope, else None. Handles both the
    structured ``{"code", "message"}`` error shape and the legacy bare-string
    error shape (``{"ok": False, "error": "unsupported"}``).
    """
    if not isinstance(result, dict) or result.get("ok") is not False:
        return None
    error = result.get("error")
    if isinstance(error, dict):
        normalized: dict[str, Any] = {
            "code": str(error.get("code") or default_code),
            "message": str(error.get("message") or "The player reported a failure."),
        }
        details = error.get("details")
        if isinstance(details, dict):
            normalized["details"] = details
        return normalized
    # Legacy bare-string envelope (e.g. browser-mode unsupported) carries the
    # human-readable explanation in data.reason rather than in error itself
    # (backend/music_adapter.py's _check_browser_mode_unsupported) - prefer it
    # over echoing the raw code string ("unsupported") as the user message.
    data = result.get("data")
    reason = data.get("reason") if isinstance(data, dict) and isinstance(data.get("reason"), str) else ""
    if error:
        return {"code": default_code, "message": reason or str(error)}
    return {"code": default_code, "message": reason or "The player reported a failure."}


def _extract_applied_volume(result: object) -> int | None:
    """Pull the volume the adapter actually applied out of its success envelope.

    Mirrors ``ui/api/routes/control.py``'s ``_extract_applied_volume`` (#2736).
    The music adapter's ``set_volume`` returns a success envelope shaped like
    ``{"ok": True, "data": {"intent": "set_volume", "value": <clamped>, ...}}``;
    surfacing the *applied* value keeps the reported level honest when the
    backend clamps or adjusts it, instead of always echoing the requested one.
    """
    if not isinstance(result, dict):
        return None
    candidates: list[Any] = []
    data = result.get("data")
    if isinstance(data, dict):
        candidates.append(data.get("value"))
        candidates.append(data.get("volume"))
        status = data.get("status")
        if isinstance(status, dict):
            candidates.append(status.get("volume"))
    candidates.append(result.get("volume"))
    for candidate in candidates:
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return max(0, min(100, int(candidate)))
    return None


class CommandExecutor:
    """
    Executes commands identified by the intent pipeline.
    Wraps music player operations for async command execution.
    """

    def __init__(self, music: object, tts: object | None = None):
        """
        Initialize command executor.

        Args:
            music: Music player/controller adapter
            tts: Optional TTS engine for spoken feedback
        """
        self.music = music
        self.tts = tts

    @staticmethod
    def _get_pairing_lan_ip() -> str:
        """Return the hub LAN IP used by the speaker pairing URL."""
        return get_pairing_lan_ip()

    @staticmethod
    def _room_label(target_room: str) -> str:
        """Return a normalized room label for structured routing metadata."""
        return room_label(target_room)

    def _build_speaker_pairing_flow(self, target_room: str) -> JsonDict:
        """Build UI metadata that opens the existing in-house speaker pairing QR flow."""
        return build_speaker_pairing_flow(target_room)

    def _get_browser_spoke_room(self, room_id: str, *, user_id: str | None = None) -> object | None:
        """Return a registered browser-spoke room for room_id, if one exists."""
        try:
            from services.multiroom.room_registry import get_room_registry

            room = (
                get_room_registry(user_id=user_id).get_room(room_id)
                if user_id
                else get_room_registry().get_room(room_id)
            )
        except Exception as exc:
            logger.debug("Browser spoke registry lookup failed for %s: %s", room_id, exc)
            return None

        device_id = getattr(room, "device_id", "")
        if isinstance(device_id, str) and device_id.startswith(_BROWSER_SPOKE_DEVICE_PREFIX):
            return room
        return None

    async def _route_to_browser_spoke_room(
        self,
        intent: str,
        params: JsonDict,
        match: Any,
        target_room: str,
        room: object,
    ) -> CommandResult:
        """Execute locally for a paired browser spoke and attach route facts."""
        status = getattr(room, "status", "online")
        if status != "online":
            return await self._fallback_to_local_for_offline_room(intent, params, match, target_room)

        local_result = await self._execute_local_command(intent, params)
        data: JsonDict = dict(local_result.get("data") or {})
        data.update(
            {
                "target_room": target_room,
                "room_name": match.display_name,
                "paired": True,
                "pairing_flow_available": False,
                "room_route": {
                    "status": "paired",
                    "requested_room": target_room,
                    "room_name": match.display_name,
                    "target_room_id": match.target_id,
                    "paired": True,
                    "transport": "browser_spoke_audio_stream",
                    "playback_source": "local_hub_stream",
                },
            }
        )
        return {
            "success": bool(local_result.get("success")),
            "message": str(local_result.get("message", "")),
            "data": data,
            "error": local_result.get("error"),
        }

    async def _fallback_to_local_for_offline_room(
        self,
        intent: str,
        params: JsonDict,
        match: Any,
        target_room: str,
    ) -> CommandResult:
        """Execute locally for a room that IS paired but is not connected right now.

        The room stays reported as paired. Telling the user a speaker they
        already paired "isn't paired yet" -- and offering the pairing flow again
        -- is the same dishonesty as reporting success on zero rooms: it hands
        the model a fact that contradicts the user's own room list (#4432).
        """
        local_result = await self._execute_local_command(intent, params)
        data: JsonDict = dict(local_result.get("data") or {})
        data.update(
            {
                "target_room": target_room,
                "room_name": match.display_name,
                "paired": True,
                "pairing_flow_available": False,
                "room_route": {
                    "status": "offline",
                    "requested_room": target_room,
                    "room_name": match.display_name,
                    "target_room_id": match.target_id,
                    "paired": True,
                    "fallback": "local",
                    "reason": "room_offline",
                },
            }
        )
        return {
            "success": bool(local_result.get("success")),
            "message": "room_paired_but_offline_played_locally",
            "data": data,
            "error": local_result.get("error"),
        }

    async def _fallback_to_local_for_unpaired_room(
        self,
        intent: str,
        params: JsonDict,
        target_room: str,
        *,
        reason: str,
    ) -> CommandResult:
        """Execute locally and attach speaker-pairing metadata for an unpaired room."""
        local_result = await self._execute_local_command(intent, params)
        data: JsonDict = dict(local_result.get("data") or {})
        room_label = self._room_label(target_room)
        data.update(
            {
                "target_room": target_room,
                "room_name": room_label,
                "paired": False,
                "pairing_flow_available": True,
                "room_route": {
                    "status": "unpaired",
                    "requested_room": target_room,
                    "room_name": room_label,
                    "paired": False,
                    "fallback": "local",
                    "reason": reason,
                    "pairing_path_identifier": "rooms.add_speaker",
                },
                "pairing_flow": self._build_speaker_pairing_flow(target_room),
            }
        )
        message = "room_unpaired_pairing_flow_available"
        if local_result.get("success"):
            return {
                "success": True,
                "message": message,
                "data": data,
                "error": None,
            }

        fallback_error = local_result.get("error") or "local_fallback_failed"
        data["local_fallback_error"] = fallback_error
        data["local_fallback_message"] = local_result.get("message", "")
        return {
            "success": True,
            "message": message,
            "data": data,
            "error": None,
        }

    async def _handle_pair_speaker_setup(self, params: JsonDict) -> CommandResult:
        """Surface the Add Room pairing flow without starting playback."""
        target_room_obj = params.get("target_room") or params.get("room_name") or params.get("room")
        target_room = (
            target_room_obj.strip() if isinstance(target_room_obj, str) and target_room_obj.strip() else "speaker"
        )
        data = build_speaker_pairing_payload(target_room)
        return {
            "success": True,
            "message": "speaker_pairing_flow_available",
            "data": data,
            "error": None,
        }

    # Maps GPT-style long command names to CommandExecutor handler names.
    _COMMAND_ALIASES: dict[str, str] = {
        "play_music": "play",
        "pause_music": "pause",
        "resume_music": "resume",
        "stop_music": "stop",
        "skip_track": "skip",
        "previous_track": "previous",
        "volume.set": "volume",
        "volume.up": "volume_up",
        "volume.down": "volume_down",
        "volume_set": "volume",
        "play_saved_playlist": "play_playlist",
        "create_playlist": "create_playlist",
        "make_playlist": "create_playlist",
        "delete_playlist": "delete_playlist",
        "remove_playlist": "delete_playlist",
        "add_to_queue": "play",
        "add_room": "pair_speaker_setup",
        "pair_speaker": "pair_speaker_setup",
        "setup_speaker": "pair_speaker_setup",
    }

    def _coerce_int(self, value: object, default: int) -> int:
        if isinstance(value, (int, float, bool, str)):
            try:
                return int(value)
            except (ValueError, TypeError):
                return default
        return default

    def _speak_music_search_ack(self) -> None:
        """Fire-and-forget acknowledgement for cold music search latency."""
        if not self.tts:
            return

        speak_async = getattr(self.tts, "speak_async", None)
        if callable(speak_async):
            try:
                speak_async(_MUSIC_SEARCH_ACK)
            except Exception as exc:
                logger.debug("Music search acknowledgement failed: %s", exc)
            return

        speak_method = getattr(self.tts, "say", None) or getattr(self.tts, "speak", None)
        if not callable(speak_method):
            return

        try:
            loop = asyncio.get_running_loop()
            if asyncio.iscoroutinefunction(speak_method):
                task = loop.create_task(speak_method(_MUSIC_SEARCH_ACK))
                task.add_done_callback(_log_task_exception)
            else:
                loop.run_in_executor(None, speak_method, _MUSIC_SEARCH_ACK)
        except Exception as exc:
            logger.debug("Music search acknowledgement scheduling failed: %s", exc)

    @staticmethod
    def _extract_error_text(result: dict) -> str:
        error_obj = result.get("error")
        message_obj = result.get("message")
        parts: list[str] = []

        if isinstance(error_obj, dict):
            for key in ("code", "message"):
                value = error_obj.get(key)
                if isinstance(value, str):
                    parts.append(value)
        elif isinstance(error_obj, str):
            parts.append(error_obj)
        elif error_obj is not None:
            parts.append(str(error_obj))

        if isinstance(message_obj, str):
            parts.append(message_obj)
        elif message_obj is not None:
            parts.append(str(message_obj))

        data_obj = result.get("data")
        if isinstance(data_obj, dict):
            for key in ("provider_name", "error_code"):
                value = data_obj.get(key)
                if isinstance(value, str):
                    parts.append(value)

        return " ".join(parts).lower()

    @staticmethod
    def _active_music_provider_id() -> str | None:
        try:
            from music.providers.active_provider import get_active_music_provider_id

            return get_active_music_provider_id()
        except Exception as exc:
            logger.debug("Failed to read active music provider for fallback: %s", exc)
            return None

    @staticmethod
    def _normalize_music_provider(provider_value: object) -> str | None:
        if not isinstance(provider_value, str):
            return None
        provider_text = provider_value.strip().lower()
        if not provider_text:
            return None
        provider_text = provider_text.replace("-", " ")
        provider_text = " ".join(provider_text.split())
        underscored = provider_text.replace(" ", "_")
        return _MUSIC_PROVIDER_ALIASES.get(provider_text) or _MUSIC_PROVIDER_ALIASES.get(underscored)

    @staticmethod
    def _extract_provider_suffix(query: str) -> tuple[str, str | None]:
        import re

        match = re.search(
            r"\s+(?:from|on)\s+"
            r"(?P<provider>spotify|spotify\s+cdp|youtube\s+music|youtube|youtube\s+iframe|"
            r"local(?:\s+files|\s+library)?|my\s+(?:local\s+)?library)\.?$",
            query,
            re.I,
        )
        if not match:
            return query, None
        cleaned_query = query[: match.start()].strip()
        provider_id = CommandExecutor._normalize_music_provider(match.group("provider"))
        if not cleaned_query or provider_id is None:
            return query, None
        return cleaned_query, provider_id

    def _select_music_provider_for_play(self, provider_id: str) -> tuple[str | None, bool]:
        from music.providers.active_provider import (
            get_active_music_provider_id,
            handle_provider_switch,
        )
        from ui.settings_manager import get_settings_manager

        current_provider_id = get_active_music_provider_id()
        if current_provider_id == provider_id:
            return current_provider_id, False

        handle_provider_switch(current_provider_id, provider_id, music_service=self.music)
        get_settings_manager().set("active_music_provider_id", provider_id)
        logger.info(
            "Explicit play provider selected: %s -> %s",
            current_provider_id,
            provider_id,
        )
        return current_provider_id, True

    @staticmethod
    def _provider_family(provider_value: object) -> str | None:
        if not isinstance(provider_value, str):
            return None
        lowered = provider_value.lower()
        normalized = lowered.replace("-", "_").replace(" ", "_")
        family = _MUSIC_PROVIDER_FAMILIES.get(normalized)
        if family is not None:
            return family
        if "spotify" in lowered:
            return "spotify"
        if "youtube" in lowered or "youtu.be" in lowered or lowered == "ytmusic":
            return "youtube"
        if "local" in lowered or lowered.startswith("file:") or ":\\" in lowered:
            return "local"
        return None

    @classmethod
    def _now_playing_provider_family(cls, now_playing: JsonDict) -> str | None:
        for key in ("provider", "source", "playback_mode", "resolver_path"):
            family = cls._provider_family(now_playing.get(key))
            if family is not None:
                return family

        resolver_extras = now_playing.get("resolver_extras")
        if isinstance(resolver_extras, dict):
            for value in resolver_extras.values():
                family = cls._provider_family(value)
                if family is not None:
                    return family

        url = now_playing.get("url")
        family = cls._provider_family(url)
        if family is not None:
            return family
        if now_playing.get("video_id"):
            return "youtube"
        return None

    @staticmethod
    def _classify_query_match(query: str, verified_data: JsonDict) -> str:
        """Compare the user's request to the played track and label the match quality.

        Returns one of:
          - ``exact``   - the request literally appears in title or artist
          - ``partial`` - the request shares one or more words with title/artist
          - ``unknown`` - the played track has no metadata to compare against
          - ``fallback_unrelated`` - request and track have no shared words
            (typical of fuzzy local hits returning unrelated tracks for genre
            requests with no metadata coverage)

        This is a structured signal for the LLM, not a scripted reply. The LLM
        decides on the next turn whether to retry with an explicit provider.
        """
        request = (query or "").strip().lower()
        if not request:
            return "unknown"

        np_obj = verified_data.get("now_playing")
        np = np_obj if isinstance(np_obj, dict) else {}
        title = str(np.get("title") or "").strip().lower()
        artist = str(np.get("artist") or "").strip().lower()
        # An unverified title is a query-echo placeholder (e.g. the browser
        # provider echoes the search string before the real track is known),
        # not evidence of a match. Exclude it from the comparison so we never
        # report a spurious "exact" just because the placeholder IS the query
        # (#2806). This reads a provenance data field, not the query or model
        # output -- it does not classify intent.
        if np.get("title_unverified"):
            title = ""
        haystack = " ".join(part for part in (title, artist) if part)
        if not haystack:
            return "unknown"

        if request in haystack:
            return "exact"

        def _tokens(text: str) -> set[str]:
            return {tok for tok in (t.strip() for t in text.replace("/", " ").split()) if len(tok) >= 3}

        request_tokens = _tokens(request)
        track_tokens = _tokens(haystack)
        if request_tokens & track_tokens:
            return "partial"
        return "fallback_unrelated"

    def _stop_after_provider_mismatch(self) -> None:
        stop_method = getattr(self.music, "stop", None)
        if not callable(stop_method):
            return
        try:
            stop_method()
        except Exception as exc:
            logger.debug("Failed to stop playback after provider mismatch: %s", exc)

    @staticmethod
    def _item_value(item: object, key: str) -> object:
        if isinstance(item, dict):
            return item.get(key)
        return getattr(item, key, None)

    @classmethod
    def _now_playing_track_key(cls, now_playing: object) -> str:
        if now_playing is None:
            return ""
        for key in ("id", "url", "video_id", "title"):
            value = cls._item_value(now_playing, key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @classmethod
    def _requires_position_advance_for_verification(cls, now_playing: object) -> bool:
        playback_mode = cls._item_value(now_playing, "playback_mode")
        if isinstance(playback_mode, str) and playback_mode in _EMBEDDED_YOUTUBE_PLAYBACK_MODES:
            return bool(cls._item_value(now_playing, "video_id"))

        provider_family = cls._provider_family(cls._item_value(now_playing, "provider"))
        source_family = cls._provider_family(cls._item_value(now_playing, "source"))
        has_video = bool(cls._item_value(now_playing, "video_id"))
        return has_video and (provider_family == "youtube" or source_family == "youtube")

    @classmethod
    def _state_position_ms(cls, state: object) -> int:
        position_ms = cls._item_value(state, "position_ms")
        if isinstance(position_ms, bool):
            return 0
        if isinstance(position_ms, (int, float)):
            return max(0, int(position_ms))

        position_seconds = cls._item_value(state, "position")
        if isinstance(position_seconds, bool):
            return 0
        if isinstance(position_seconds, (int, float)):
            return max(0, int(float(position_seconds) * 1000))
        return 0

    def _current_now_playing_key(self) -> str:
        state_getter = getattr(self.music, "state", None)
        state = state_getter() if callable(state_getter) else None
        if isinstance(state, dict):
            return self._now_playing_track_key(state.get("now_playing"))
        return self._now_playing_track_key(getattr(state, "now_playing", None))

    @staticmethod
    def _switch_to_no_auth_music_provider() -> bool:
        try:
            from ui.settings_manager import get_settings_manager

            get_settings_manager().set("active_music_provider_id", _NO_AUTH_MUSIC_PROVIDER_ID)
            return True
        except Exception as exc:
            logger.debug("Failed to switch music provider fallback: %s", exc)
            return False

    def _should_retry_with_no_auth_provider(self, result: dict, active_provider: str | None) -> bool:
        if active_provider in (None, _NO_AUTH_MUSIC_PROVIDER_ID, "youtube_music"):
            return False

        error_text = self._extract_error_text(result)
        return any(marker in error_text for marker in _PROVIDER_FAILURE_FALLBACK_MARKERS)

    async def _invoke_play(
        self,
        play_method: Callable[..., object],
        query: str,
        source: str | None = None,
    ) -> object:
        if asyncio.iscoroutinefunction(play_method):
            if source is not None:
                return await cast(Callable[[str, str], Awaitable[object]], play_method)(query, source)
            return await cast(Callable[[str], Awaitable[object]], play_method)(query)
        if source is not None:
            return cast(Callable[[str, str], object], play_method)(query, source)
        return cast(Callable[[str], object], play_method)(query)

    async def _play_with_no_auth_provider_fallback(
        self,
        play_method: Callable[..., object],
        query: str,
        first_result: dict,
    ) -> tuple[object, dict[str, object] | None]:
        active_provider = self._active_music_provider_id()
        if not self._should_retry_with_no_auth_provider(first_result, active_provider):
            return first_result, None

        if not self._switch_to_no_auth_music_provider():
            return first_result, None

        logger.info(
            "Retrying play command through no-auth provider after %s failure",
            active_provider,
        )
        fallback_result = await self._invoke_play(play_method, query)
        return fallback_result, {
            "from": active_provider or "unknown",
            "to": _NO_AUTH_MUSIC_PROVIDER_ID,
            "reason": "provider_auth_or_availability_failure",
        }

    async def _retry_unrelated_candidate_with_no_auth_provider(
        self,
        play_method: Callable[..., object],
        query: str,
    ) -> tuple[object, dict[str, object]] | None:
        active_provider = self._active_music_provider_id()
        if active_provider in (None, _NO_AUTH_MUSIC_PROVIDER_ID, "youtube_music"):
            return None

        if not self._switch_to_no_auth_music_provider():
            return None

        logger.info(
            "Retrying unrelated play candidate through no-auth provider after %s result",
            active_provider,
        )
        fallback_result = await self._invoke_play(play_method, query)
        return fallback_result, {
            "from": active_provider or "unknown",
            "to": _NO_AUTH_MUSIC_PROVIDER_ID,
            "reason": "unrelated_local_candidate",
        }

    def _read_current_volume(self) -> int:
        state_getter = getattr(self.music, "state", None)
        state = state_getter() if callable(state_getter) else None

        volume_obj: object | None
        if isinstance(state, dict):
            volume_obj = state.get("volume")
        else:
            volume_obj = getattr(state, "volume", None)

        if isinstance(volume_obj, (int, float, bool, str)):
            try:
                return max(0, min(100, int(volume_obj)))
            except (ValueError, TypeError) as exc:
                logger.debug("volume coerce skip on %r: %s", volume_obj, exc)

        get_volume_method = getattr(self.music, "get_volume", None)
        if callable(get_volume_method):
            try:
                return max(0, min(100, self._coerce_int(get_volume_method(), 50)))
            except Exception:
                logger.debug("Failed to read current volume from music backend")

        return 50

    async def _apply_volume(self, level: int) -> int:
        clamped_level = max(0, min(100, int(level)))
        set_volume_method = getattr(self.music, "set_volume", None)
        if callable(set_volume_method):
            if asyncio.iscoroutinefunction(set_volume_method):
                result = await cast(Callable[[int], Awaitable[object]], set_volume_method)(clamped_level)
            else:
                result = cast(Callable[[int], object], set_volume_method)(clamped_level)

            envelope_error = _adapter_envelope_error(result, default_code="volume_failed")
            if envelope_error is not None:
                raise _AdapterEnvelopeError(envelope_error)

            if isinstance(result, dict):
                applied = _extract_applied_volume(result)
                return applied if applied is not None else clamped_level

            if isinstance(result, (int, float, bool, str)):
                return max(0, min(100, self._coerce_int(result, clamped_level)))

        return clamped_level

    async def execute_command(self, intent: str, params: JsonDict) -> CommandResult:
        """
        Execute a command based on intent and parameters.

        If params contains a ``target_room`` key, the command is routed
        to the specified room/group instead of executing locally.

        Args:
            intent: The intent name (e.g., "play", "pause", "volume")
            params: Intent parameters. May include ``target_room`` for
                    room-targeted commands.

        Returns:
            dict with keys: success (bool), message (str), data (dict), error (str|None)
        """
        resolved_intent = self._COMMAND_ALIASES.get(intent, intent)
        if resolved_intent == "pair_speaker_setup":
            return await self._handle_pair_speaker_setup(params)

        if self.music is None:
            logger.error("No music controller available for command: %s", intent)
            return {
                "success": False,
                "message": "Music player is not available.",
                "data": {},
                "error": "no_music_player",
            }

        # Check for room routing
        target_room = params.pop("target_room", None) if isinstance(params, dict) else None
        if target_room and isinstance(target_room, str):
            return await self._route_to_room(intent, params, target_room)

        # Resolve aliases (GPT-style names → handler names)
        intent = resolved_intent

        try:
            # Route to appropriate handler based on intent
            handler_obj = getattr(self, f"_handle_{intent}", None)
            if callable(handler_obj):
                handler = cast(Callable[[JsonDict], Awaitable[CommandResult]], handler_obj)
                return await handler(params)

            # Fallback: try direct method call on music controller
            return await self._execute_direct(intent, params)

        except Exception as e:
            logger.exception("Error executing command %s: %s", intent, e)
            friendly_intent = intent.replace("_", " ")
            return {
                "success": False,
                "message": "I couldn't %s right now. Try again in a moment." % friendly_intent,
                "data": {},
                "error": "command_execution_failed",
            }

    async def _route_to_room(self, intent: str, params: JsonDict, target_room: str) -> CommandResult:
        """
        Route a command to a specific room or group.

        Resolves the room name, then either executes locally (if the room
        is the local device) or forwards to the remote room/group.

        Args:
            intent: The command intent name
            params: Command parameters (target_room already removed)
            target_room: The user-spoken room name to resolve

        Returns:
            CommandResult indicating success or failure
        """
        user_id = str(params.get("_user_id") or params.get("user_id") or "").strip() or None
        try:
            from services.multiroom.room_name_resolver import resolve_room_name

            match = resolve_room_name(target_room, user_id=user_id)
        except Exception as exc:
            logger.warning("Room name resolution unavailable: %s", exc)
            match = None

        if match is None:
            logger.info(
                "No in-house room or group found matching '%s'; executing locally and surfacing pairing flow",
                target_room,
            )
            return await self._fallback_to_local_for_unpaired_room(
                intent,
                params,
                target_room,
                reason="room_not_found",
            )

        # Local room -- execute normally
        if match.is_local and match.match_type == "room":
            logger.info(
                "Room '%s' is the local device -- executing locally",
                match.display_name,
            )
            return await self._execute_local_command(intent, params)

        if match.match_type == "room":
            browser_spoke_room = self._get_browser_spoke_room(match.target_id, user_id=user_id)
            if browser_spoke_room is not None:
                logger.info(
                    "Room '%s' is a paired browser spoke -- executing locally for hub stream",
                    match.display_name,
                )
                return await self._route_to_browser_spoke_room(
                    intent,
                    params,
                    match,
                    target_room,
                    browser_spoke_room,
                )

        # HA media_player -- route music command to HA device
        if match.match_type == "ha_media_player":
            return await self._route_to_ha_media_player(intent, params, match, target_room)

        # Remote room or group -- forward via command forwarder
        try:
            from services.multiroom.command_forwarder import get_command_forwarder

            forwarder = get_command_forwarder()
        except Exception as exc:
            logger.warning("Command forwarder unavailable: %s", exc)
            return {
                "success": False,
                "message": "Multi-room forwarding is not available.",
                "data": {"target_room": target_room},
                "error": "forwarder_unavailable",
            }

        # Build kwargs for the forwarder
        fwd_kwargs = self._build_forward_kwargs(intent, params)

        if match.match_type == "group":
            logger.info(
                "Forwarding '%s' to group '%s' (id=%s)",
                intent,
                match.display_name,
                match.target_id[:8],
            )
            try:
                room_ids: list[str] = []
                group = None

                if match.target_id != "__ALL_ROOMS__":
                    from ui.api.routes.room_groups import get_room_group_manager

                    mgr = get_room_group_manager()
                    if not user_id:
                        raise ValueError("user_id is required for room-group routing")
                    group = mgr.get_group(match.target_id, user_id=user_id)

                if group is None and match.target_id == "__ALL_ROOMS__":
                    from services.multiroom.room_registry import get_room_registry

                    # The registry factory is the single authority on which store
                    # holds this install's rooms (#4432). Re-deciding it here with
                    # a hard "user_id is required" was what still broke all-rooms
                    # for Viola's own music route, which carries no principal: the
                    # guard raised before the resolved registry was ever consulted.
                    # A missing principal is not an error on the desktop (one
                    # install, one owner), and on the cloud surface the factory
                    # hands back an empty process-local registry rather than any
                    # tenant's rooms -- so this reports "no rooms" loudly instead
                    # of ever reaching across accounts.
                    registry = get_room_registry(user_id=user_id) if user_id else get_room_registry()
                    room_ids = [room.id for room in registry.list_rooms() if not room.is_local]
                elif group is None or not group.room_ids:
                    return {
                        "success": False,
                        "message": "Failed to send command to group %s." % match.display_name,
                        "data": {"target_room": target_room},
                        "error": "group_forward_failed",
                    }
                else:
                    room_ids = list(group.room_ids)

                # Browser spokes all play the one hub stream, so they are reached
                # by executing locally once -- the same transport a single-spoke
                # room already uses -- not through the room-to-room forwarder,
                # which has no connection for them and would report every spoke
                # unreachable.
                spoke_room_ids: list[str] = []
                forwardable_room_ids: list[str] = []
                for rid in room_ids:
                    spoke_room = self._get_browser_spoke_room(rid, user_id=user_id)
                    if spoke_room is not None and getattr(spoke_room, "status", "online") == "online":
                        spoke_room_ids.append(rid)
                    else:
                        forwardable_room_ids.append(rid)

                delivered_room_ids: list[str] = []
                if spoke_room_ids:
                    local_result = await self._execute_local_command(intent, params)
                    if local_result.get("success"):
                        delivered_room_ids.extend(spoke_room_ids)

                for rid in forwardable_room_ids:
                    if await forwarder.forward_to_room(intent, rid, **fwd_kwargs):
                        delivered_room_ids.append(rid)

                from services.multiroom.group_forward_result import build_group_forward_result

                return build_group_forward_result(
                    intent=intent,
                    display_name=match.display_name,
                    target_room=target_room,
                    room_ids=room_ids,
                    delivered_room_ids=delivered_room_ids,
                )
            except Exception as exc:
                logger.exception("Group forwarding failed: %s", exc)
                return {
                    "success": False,
                    "message": "Failed to send command to group %s." % match.display_name,
                    "data": {"target_room": target_room},
                    "error": "group_forward_failed",
                }

        # Single remote room
        logger.info(
            "Forwarding '%s' to room '%s' (id=%s)",
            intent,
            match.display_name,
            match.target_id[:8],
        )
        success = await forwarder.forward_to_room(intent, match.target_id, **fwd_kwargs)

        if success:
            return {
                "success": True,
                "message": "Sent %s to %s" % (intent, match.display_name),
                "data": {"target_room": match.display_name},
                "error": None,
            }
        return {
            "success": False,
            "message": "Could not reach %s." % match.display_name,
            "data": {"target_room": match.display_name},
            "error": "forward_failed",
        }

    async def _route_to_ha_media_player(
        self,
        intent: str,
        params: JsonDict,
        match: Any,
        target_room: str | None = None,
    ) -> CommandResult:
        """Route a music command to a configured Home Assistant media_player entity.

        Translates Viola music intents (play, pause, volume, etc.) to
        HA media_player service calls only after in-house room resolution misses
        and the user has configured HA.
        """
        entity_id = match.target_id  # e.g., "media_player.kitchen_sonos"
        requested_room = target_room or match.display_name

        try:
            from services.smart_home.home_assistant import get_home_assistant

            ha = get_home_assistant()
            if not ha.is_configured:
                logger.info(
                    "Skipping HA media_player route for '%s'; provider is not configured",
                    requested_room,
                )
                return await self._fallback_to_local_for_unpaired_room(
                    intent,
                    params,
                    requested_room,
                    reason="room_not_paired",
                )
        except Exception as exc:
            logger.warning("HA client unavailable for media_player routing: %s", exc)
            return await self._fallback_to_local_for_unpaired_room(
                intent,
                params,
                requested_room,
                reason="room_route_unavailable",
            )

        # Map Viola intents to HA media_player services
        _HA_INTENT_MAP = {
            "pause": ("media_player/media_pause", {}),
            "resume": ("media_player/media_play", {}),
            "stop": ("media_player/media_stop", {}),
            "next": ("media_player/media_next_track", {}),
            "previous": ("media_player/media_previous_track", {}),
            "skip": ("media_player/media_next_track", {}),
        }

        if intent in _HA_INTENT_MAP:
            service_path, extra = _HA_INTENT_MAP[intent]
            result = await ha.call_service(entity_id, service_path.split("/")[1], **extra)
            if result.get("ok"):
                return {
                    "success": True,
                    "message": "%s on %s." % (intent.capitalize(), match.display_name),
                    "data": {"entity_id": entity_id},
                    "error": None,
                }
            return {
                "success": False,
                "message": "Failed to %s on %s." % (intent, match.display_name),
                "data": {"entity_id": entity_id},
                "error": result.get("error", "ha_service_failed"),
            }

        if intent == "volume" or intent == "set_volume":
            level = params.get("level") or params.get("volume")
            if level is not None:
                # HA volume is 0.0-1.0, Viola uses 0-100
                ha_vol = max(0.0, min(1.0, int(level) / 100.0))
                result = await ha.call_service(
                    entity_id,
                    "volume_set",
                    volume_level=ha_vol,
                )
                if result.get("ok"):
                    return {
                        "success": True,
                        "message": "Volume set to %s%% on %s." % (level, match.display_name),
                        "data": {"entity_id": entity_id, "volume": level},
                        "error": None,
                    }

        if intent == "play":
            # For "play <query> in the kitchen" — use media_player/play_media
            query = params.get("query") or params.get("song") or params.get("text", "")
            if query:
                result = await ha.call_service(
                    entity_id,
                    "play_media",
                    media_content_id=str(query),
                    media_content_type="music",
                )
                if result.get("ok"):
                    return {
                        "success": True,
                        "message": "room_media_play_started",
                        "data": {
                            "entity_id": entity_id,
                            "query": query,
                            "target_display_name": match.display_name,
                        },
                        "error": None,
                    }
            else:
                # No query — just resume playback
                result = await ha.call_service(entity_id, "media_play")
                if result.get("ok"):
                    return {
                        "success": True,
                        "message": "Resumed playback on %s." % match.display_name,
                        "data": {"entity_id": entity_id},
                        "error": None,
                    }

        # Unsupported intent for HA media_player
        logger.info(
            "Intent '%s' not supported for HA media_player %s",
            intent,
            entity_id,
        )
        return {
            "success": False,
            "message": "I can't do that on %s yet." % match.display_name,
            "data": {"entity_id": entity_id, "intent": intent},
            "error": "unsupported_ha_intent",
        }

    async def _execute_local_command(self, intent: str, params: JsonDict) -> CommandResult:
        """Execute a command through the normal local dispatch path."""
        resolved_intent = self._COMMAND_ALIASES.get(intent, intent)
        try:
            handler_obj = getattr(self, f"_handle_{resolved_intent}", None)
            if callable(handler_obj):
                handler = cast(Callable[[JsonDict], Awaitable[CommandResult]], handler_obj)
                return await handler(params)
            return await self._execute_direct(resolved_intent, params)
        except Exception as e:
            logger.exception("Error executing local command %s: %s", intent, e)
            friendly_intent = intent.replace("_", " ")
            return {
                "success": False,
                "message": "I couldn't %s right now. Try again in a moment." % friendly_intent,
                "data": {},
                "error": "command_execution_failed",
            }

    def _build_forward_kwargs(self, intent: str, params: JsonDict) -> dict:
        """
        Build keyword arguments for the command forwarder from intent params.

        Maps intent-specific parameters to the kwargs expected by
        ``_dispatch_to_room`` (query/source for play, level for volume).
        """
        kwargs: dict = {}
        resolved = self._COMMAND_ALIASES.get(intent, intent)

        if resolved in ("play", "play_music"):
            query_obj = params.get("query")
            if isinstance(query_obj, str):
                kwargs["query"] = query_obj
            source_obj = params.get("source")
            if isinstance(source_obj, str):
                kwargs["source"] = source_obj

        elif resolved in ("volume", "volume_set", "set_volume"):
            level_obj = params.get("level")
            if isinstance(level_obj, (int, float)):
                kwargs["level"] = int(level_obj)

        elif resolved in (
            "play_playlist",
            "create_playlist",
            "delete_playlist",
            "rename_playlist",
        ):
            playlist_name = self._extract_playlist_name(params)
            if playlist_name:
                kwargs["playlist_name"] = playlist_name
            if resolved == "rename_playlist":
                new_name_obj = params.get("new_name")
                if isinstance(new_name_obj, str):
                    new_name = new_name_obj.strip()
                    if new_name:
                        kwargs["new_name"] = new_name

        return kwargs

    async def _execute_direct(self, intent: str, params: JsonDict) -> CommandResult:
        """Execute command by calling method directly on music controller."""
        # Common intent mappings
        intent_map = {
            "play": "play",
            "pause": "pause",
            "resume": "resume",
            "stop": "stop",
            "skip": "skip",
            "next": "next",
            "previous": "previous",
            "volume": "set_volume",
            "volume_up": "volume_up",
            "volume_down": "volume_down",
            # Repeat modes - these are handled by explicit handlers
            "repeat_off": "repeat_off",
            "repeat_on": "repeat_on",
            "repeat_one": "repeat_one",
            "loop_song": "loop_song",
            "loop_playlist": "loop_playlist",
            "repeat": "repeat",
        }

        method_name = intent_map.get(intent, intent)
        method = getattr(self.music, method_name, None)

        if not callable(method):
            return {
                "success": False,
                "message": f"Unknown command: {intent}",
                "data": {},
                "error": "unknown_command",
            }

        # Execute the method
        try:
            if asyncio.iscoroutinefunction(method):
                if params:
                    result = await cast(Callable[..., Awaitable[object]], method)(**params)
                else:
                    result = await cast(Callable[[], Awaitable[object]], method)()
            else:
                if params:
                    result = cast(Callable[..., object], method)(**params)
                else:
                    result = cast(Callable[[], object], method)()

            return {
                "success": True,
                "message": f"{intent.replace('_', ' ').title()} executed",
                "data": {"result": to_json_value(result)} if result else {},
                "error": None,
            }
        except TypeError:
            # Method doesn't accept params, try without
            if asyncio.iscoroutinefunction(method):
                result = await cast(Callable[[], Awaitable[object]], method)()
            else:
                result = cast(Callable[[], object], method)()
            return {
                "success": True,
                "message": f"{intent.replace('_', ' ').title()} executed",
                "data": {"result": to_json_value(result)} if result else {},
                "error": None,
            }

    async def _handle_play(self, params: JsonDict) -> CommandResult:
        """Handle play command."""
        query_obj = params.get("query")
        query = query_obj.strip() if isinstance(query_obj, str) else ""
        if not query:
            return {
                "success": False,
                "message": "What do you want to hear?",
                "data": {},
                "error": "missing_query",
            }

        provider_obj = params.get("provider")
        provider_supplied = provider_obj is not None and (
            not isinstance(provider_obj, str) or bool(provider_obj.strip())
        )
        explicit_provider_id = self._normalize_music_provider(provider_obj)
        if provider_supplied and explicit_provider_id is None:
            return {
                "success": False,
                "message": "music_provider_unknown",
                "data": {"query": query, "provider": to_json_value(provider_obj)},
                "error": "unknown_provider",
            }

        source_obj = params.get("source")
        source_hint = source_obj.strip() if isinstance(source_obj, str) and source_obj.strip() else None

        from services.llm.route_tool_schemas import _clean_music_query

        # Strip leading conversational fillers ("me some", "the", "a", etc.)
        # so "play me some jazz" => query="jazz" reaches the music backend.
        # This executor remains the single chokepoint for play commands from
        # native route tools, MCP tools, and older command callers.
        query = _clean_music_query(query)
        if explicit_provider_id is None:
            query, explicit_provider_id = self._extract_provider_suffix(query)
        if not query:
            return {
                "success": False,
                "message": "What do you want to hear?",
                "data": {},
                "error": "missing_query_after_cleanup",
            }

        try:
            previous_provider_id = None
            provider_changed = False
            if explicit_provider_id is not None:
                previous_provider_id, provider_changed = self._select_music_provider_for_play(explicit_provider_id)
                source_hint = _MUSIC_PROVIDER_SOURCE_HINTS.get(explicit_provider_id, source_hint)

            play_method = getattr(self.music, "play", None)
            if not callable(play_method):
                return {
                    "success": False,
                    "message": "Music player does not support play.",
                    "data": {},
                    "error": "play_not_supported",
                }
            previous_track_key = self._current_now_playing_key()
            self._speak_music_search_ack()
            result = await self._invoke_play(play_method, query, source_hint)
            provider_fallback: dict[str, object] | None = None

            # Check if the music adapter returned a failure response
            if isinstance(result, dict):
                if result.get("ok") is False or result.get("success") is False:
                    if explicit_provider_id is None:
                        result, provider_fallback = await self._play_with_no_auth_provider_fallback(
                            play_method,
                            query,
                            result,
                        )

            if isinstance(result, dict):
                if result.get("ok") is False or result.get("success") is False:
                    error_msg = str(to_json_value(result.get("error", "play_failed")))
                    message = str(to_json_value(result.get("message", f"Failed to play: {query}")))
                    logger.error("Play command failed: %s - %s", error_msg, message)
                    failure_data: JsonDict = {
                        "query": query,
                        **({"provider": explicit_provider_id} if explicit_provider_id is not None else {}),
                    }
                    if explicit_provider_id is not None:
                        failure_data["provider_failure_detail"] = message
                    return {
                        "success": False,
                        "message": (
                            "music_provider_play_failed" if explicit_provider_id is not None else "music_play_failed"
                        ),
                        "data": failure_data,
                        "error": error_msg,
                    }

            verified = await self._verify_playback_started(query, previous_track_key=previous_track_key)
            verified_ok = verified.get("ok") is True
            verified_data: JsonDict = {"query": query}
            extra_data = verified.get("data")
            if isinstance(extra_data, dict):
                verified_data.update(extra_data)
            if provider_fallback:
                verified_data["provider_fallback"] = provider_fallback
            if explicit_provider_id is not None:
                verified_data["provider"] = explicit_provider_id
                if provider_changed and previous_provider_id is not None:
                    verified_data["provider_switched_from"] = previous_provider_id
                verified_data["provider_changed"] = provider_changed

            if not verified_ok:
                message = str(to_json_value(verified.get("message", "Playback unverified")))
                if explicit_provider_id is not None:
                    verified_data["provider_failure_detail"] = message
                return {
                    "success": False,
                    "message": (
                        "music_provider_playback_unverified"
                        if explicit_provider_id is not None
                        else "music_playback_unverified"
                    ),
                    "data": verified_data,
                    "error": str(to_json_value(verified.get("error", "playback_unverified"))),
                }

            if explicit_provider_id is not None:
                now_playing_obj = verified_data.get("now_playing")
                now_playing = now_playing_obj if isinstance(now_playing_obj, dict) else {}
                expected_family = self._provider_family(explicit_provider_id)
                actual_family = self._now_playing_provider_family(now_playing)
                if expected_family is not None and actual_family != expected_family:
                    self._stop_after_provider_mismatch()
                    display_name = _MUSIC_PROVIDER_DISPLAY_NAMES.get(explicit_provider_id, explicit_provider_id)
                    logger.error(
                        "Explicit provider playback mismatch: requested=%s actual=%s query=%s",
                        expected_family,
                        actual_family,
                        query,
                    )
                    return {
                        "success": False,
                        "message": "music_provider_mismatch",
                        "data": {
                            **verified_data,
                            "expected_provider_family": expected_family,
                            "actual_provider_family": actual_family,
                            "provider_display_name": display_name,
                        },
                        "error": "explicit_provider_mismatch",
                    }

            # R4-P1-K (2026-05-30): surface query-vs-track match signal as
            # STRUCTURED DATA only. The prior implementation also
            # branched runtime control flow on the classifier output
            # (stop playback, hidden retry with a no-auth provider,
            # rewrite the response to "not_played" / "candidate_not_played")
            # — that runtime branch was a regex/token-overlap classifier
            # driving destructive control flow behind the model's back.
            # The classifier output as data is fine; the runtime
            # branching on it was the bug. The model now reads
            # ``query_match`` + actual playback state and decides
            # whether to call ``stop_music`` + retry with a different
            # provider on the next turn. Parity bar: trust the model.
            query_match = self._classify_query_match(query, verified_data)
            verified_data["query_match"] = query_match

            logger.info("Play command succeeded for: %s", query[:50])
            # Spoke forwarding handled by lifecycle.py on_music_state_change
            verified_data["playback_status"] = "started"
            if provider_fallback:
                verified_data["playback_route"] = "provider_fallback"
            elif explicit_provider_id is not None:
                verified_data["playback_route"] = "explicit_provider"
                verified_data["provider_display_name"] = _MUSIC_PROVIDER_DISPLAY_NAMES.get(
                    explicit_provider_id,
                    explicit_provider_id,
                )
            else:
                verified_data["playback_route"] = "default_provider"

            return {
                "success": True,
                "message": "music_playback_started",
                "data": verified_data,
                "error": None,
            }
        except Exception as e:
            try:
                from music.providers.errors import (
                    MusicProviderUnavailableError,
                    MusicTrackNotFoundError,
                    NoActiveMusicProviderError,
                )

                provider_error_types = (
                    MusicProviderUnavailableError,
                    MusicTrackNotFoundError,
                    NoActiveMusicProviderError,
                )
            except Exception:
                provider_error_types = ()

            if provider_error_types and isinstance(e, provider_error_types):
                details_obj = getattr(e, "technical_details", None)
                details = details_obj if isinstance(details_obj, dict) else {}
                error_code = str(details.get("root_cause") or type(e).__name__)
                logger.warning("Play command provider failure for %s: %s", query[:50], e)
                return {
                    "success": False,
                    "message": "music_provider_unavailable",
                    "data": {
                        "query": query,
                        "provider_failure": details,
                        "provider_failure_detail": str(e),
                    },
                    "error": error_code,
                }
            logger.exception("Play command raised exception: %s", e)
            return {
                "success": False,
                "message": "Failed to play the requested track.",
                "data": {"query": query},
                "error": "play_failed",
            }

    async def _verify_playback_started(self, query: str, *, previous_track_key: str = "") -> VerifyPlaybackResult:
        started_at = time.monotonic()
        deadline = started_at + _PLAYBACK_VERIFY_TIMEOUT_SEC
        last_error_message: str | None = None
        last_loaded_track: JsonDict | None = None
        embedded_start_position_ms: int | None = None
        last_position_ms = 0
        embedded_deadline_extended = False

        while time.monotonic() <= deadline:
            state_getter = getattr(self.music, "state", None)
            state = state_getter() if callable(state_getter) else None
            position_ms = self._state_position_ms(state)
            last_position_ms = position_ms

            if isinstance(state, dict):
                playback_errors = state.get("playback_errors", [])
                is_playing = bool(state.get("is_playing", False))
                now_playing = state.get("now_playing")
            else:
                playback_errors = getattr(state, "playback_errors", []) if state is not None else []
                is_playing = bool(getattr(state, "is_playing", False)) if state is not None else False
                now_playing = getattr(state, "now_playing", None) if state is not None else None

            if isinstance(playback_errors, list) and playback_errors:
                first = playback_errors[0]
                if isinstance(first, dict):
                    msg = first.get("message")
                    if isinstance(msg, str) and msg.strip():
                        last_error_message = msg.strip()
                if last_error_message:
                    return {
                        "ok": False,
                        "message": f"Playback error: {last_error_message}",
                        "data": {"verified": False},
                        "error": "playback_error",
                    }

            now_playing_key = self._now_playing_track_key(now_playing)
            track_loaded = bool(now_playing_key and now_playing_key != previous_track_key)
            if is_playing or track_loaded:
                now_playing_title = self._item_value(now_playing, "title")
                now_playing_artist = self._item_value(now_playing, "artist")
                now_playing_video_id = self._item_value(now_playing, "video_id")
                now_playing_url = self._item_value(now_playing, "url")
                now_playing_id = self._item_value(now_playing, "id")
                now_playing_provider = self._item_value(now_playing, "provider")
                now_playing_source = self._item_value(now_playing, "source")
                now_playing_playback_mode = self._item_value(now_playing, "playback_mode")
                now_playing_capabilities = self._item_value(now_playing, "capabilities")
                now_playing_dict: JsonDict = {}
                if isinstance(now_playing_id, str):
                    now_playing_dict["id"] = now_playing_id
                if isinstance(now_playing_title, str):
                    now_playing_dict["title"] = now_playing_title
                if isinstance(now_playing_artist, str):
                    now_playing_dict["artist"] = now_playing_artist
                if now_playing_video_id:
                    now_playing_dict["video_id"] = now_playing_video_id
                if now_playing_url:
                    now_playing_dict["url"] = now_playing_url
                if isinstance(now_playing_provider, str):
                    now_playing_dict["provider"] = now_playing_provider
                if isinstance(now_playing_source, str):
                    now_playing_dict["source"] = now_playing_source
                if isinstance(now_playing_playback_mode, str):
                    now_playing_dict["playback_mode"] = now_playing_playback_mode
                resolver_path = self._item_value(now_playing, "resolver_path")
                resolver_extras = self._item_value(now_playing, "resolver_extras")
                if isinstance(resolver_path, str):
                    now_playing_dict["resolver_path"] = resolver_path
                if isinstance(resolver_extras, dict):
                    now_playing_dict["resolver_extras"] = {
                        str(key): to_json_value(value) for key, value in resolver_extras.items()
                    }
                if isinstance(now_playing_capabilities, dict):
                    resolver_path = now_playing_capabilities.get("resolver_path")
                    resolver_extras = now_playing_capabilities.get("resolver_extras")
                    if isinstance(resolver_path, str):
                        now_playing_dict["resolver_path"] = resolver_path
                    if isinstance(resolver_extras, dict):
                        now_playing_dict["resolver_extras"] = {
                            str(key): to_json_value(value) for key, value in resolver_extras.items()
                        }
                # Carry the title provenance flag through this projection
                # (#2757 item 3 / #2806). ``title_unverified`` marks a title we
                # have NOT confirmed against the played track -- the browser
                # provider echoes the search string as a placeholder before the
                # embedded player reports what actually loaded. It arrives
                # either at the top level (PlayerControlService's now-playing
                # payload) or inside ``capabilities`` (the raw QueueItem), and
                # ``_classify_query_match`` reads it off this dict. Dropping it
                # here made that query-echo title compare against itself and
                # report "exact", so the wrong-track signal silently claimed a
                # match nobody had verified. Only set it when truthy: absence
                # already means "not flagged", and writing False for providers
                # that never set the flag would assert a confirmation we did
                # not make.
                title_unverified = self._item_value(now_playing, "title_unverified")
                if not title_unverified and isinstance(now_playing_capabilities, dict):
                    title_unverified = now_playing_capabilities.get("title_unverified")
                if title_unverified:
                    now_playing_dict["title_unverified"] = True
                if track_loaded:
                    last_loaded_track = now_playing_dict
                if not is_playing:
                    await asyncio.sleep(_PLAYBACK_VERIFY_POLL_SEC)
                    continue
                requires_position_advance = self._requires_position_advance_for_verification(now_playing)
                if requires_position_advance:
                    if not embedded_deadline_extended:
                        deadline = max(deadline, started_at + _EMBEDDED_PLAYBACK_VERIFY_TIMEOUT_SEC)
                        embedded_deadline_extended = True
                    if embedded_start_position_ms is None:
                        embedded_start_position_ms = position_ms
                    if position_ms <= embedded_start_position_ms:
                        await asyncio.sleep(_PLAYBACK_VERIFY_POLL_SEC)
                        continue
                return {
                    "ok": True,
                    "message": "Playback verified",
                    "data": {
                        "verified": True,
                        "state": "playing",
                        "now_playing": now_playing_dict,
                        "query": query,
                        **(
                            {"position_advanced": True, "position_ms": position_ms} if requires_position_advance else {}
                        ),
                    },
                    "error": None,
                }

            await asyncio.sleep(_PLAYBACK_VERIFY_POLL_SEC)

        if last_loaded_track and self._requires_position_advance_for_verification(last_loaded_track):
            return {
                "ok": False,
                "message": "Embedded playback did not produce frontend position progress",
                "data": {
                    "verified": False,
                    "state": "loaded_not_advancing",
                    "now_playing": last_loaded_track,
                    "position_advanced": False,
                    "position_ms": last_position_ms,
                },
                "error": "embedded_playback_not_verified",
            }

        return {
            "ok": False,
            "message": "Playback did not start",
            "data": {
                "verified": False,
                "state": "loaded_not_playing" if last_loaded_track else "not_played",
                **({"now_playing": last_loaded_track} if last_loaded_track else {}),
            },
            "error": "playback_not_started",
        }

    def _extract_playlist_name(self, params: JsonDict) -> str:
        """Extract a playlist name from the common intent parameter fields."""
        for key in ("playlist_name", "playlist", "name", "query"):
            value = params.get(key)
            if isinstance(value, str):
                playlist_name = value.strip()
                if playlist_name:
                    return playlist_name
        return ""

    def _coerce_provider_id(self, provider_value: object) -> str | None:
        """Normalize provider values stored as strings or enums."""
        if isinstance(provider_value, str):
            provider_id = provider_value.strip()
            return provider_id or None

        enum_value = getattr(provider_value, "value", None)
        if isinstance(enum_value, str):
            provider_id = enum_value.strip()
            return provider_id or None

        return None

    def _auto_switch_provider_for_playlist(self, playlist_provider_name: str) -> str | None:
        """Switch the active provider when playlist playback requires a different backend."""
        from config.settings import settings
        from music.providers.active_provider import (
            get_active_music_provider_id,
            handle_provider_switch,
        )

        target_provider_id = playlist_provider_name.strip()
        if not target_provider_id:
            return None

        current_provider_id = get_active_music_provider_id() or settings.preferred_music_provider
        if current_provider_id == target_provider_id:
            return None

        logger.info(
            "Auto-switching provider for playlist playback: %s -> %s",
            current_provider_id,
            target_provider_id,
        )
        handle_provider_switch(current_provider_id, target_provider_id, music_service=self.music)

        try:
            from ui.settings_manager import get_settings_manager

            settings_mgr = get_settings_manager()
            settings_mgr.set("active_music_provider_id", target_provider_id)
        except Exception as exc:
            logger.warning(
                "Auto-switched playlist provider side effects ran, but failed to persist active provider %s: %s",
                target_provider_id,
                exc,
            )

        return current_provider_id

    async def _handle_play_playlist(self, params: JsonDict) -> CommandResult:
        """
        Handle play playlist command.

        Starts a playlist session - all songs from the playlist are cached
        and shuffled, then fed into the queue. When the playlist is exhausted,
        AI autoplay takes over with similar songs.
        """
        playlist_name = self._extract_playlist_name(params)
        if not playlist_name:
            return {
                "success": False,
                "message": "Which playlist?",
                "data": {},
                "error": "missing_playlist",
            }

        try:
            from music.playback_session import get_playback_session_controller
            from music.playlist_manager import get_playlist_manager

            # Initialize the session controller with playlist manager
            session_controller = get_playback_session_controller()
            playlist_mgr = session_controller._playlist_mgr
            if playlist_mgr is None:
                playlist_mgr = get_playlist_manager()
                session_controller._playlist_mgr = playlist_mgr

            _uid = str(params.get("_user_id", "") or params.get("user_id", "")) or None
            playlist_info = playlist_mgr.get_playlist(playlist_name, user_id=_uid)
            if not playlist_info:
                return {
                    "success": False,
                    "message": "Couldn't find playlist '%s'" % playlist_name,
                    "data": {},
                    "error": "playlist_not_found",
                }

            playlist_provider_id = self._coerce_provider_id(playlist_info.get("provider"))
            previous_provider_id = None
            if playlist_provider_id is not None:
                previous_provider_id = self._auto_switch_provider_for_playlist(playlist_provider_id)
                if previous_provider_id is not None:
                    logger.info(
                        "Playlist '%s' requested provider '%s'; switched from '%s'",
                        playlist_name,
                        playlist_provider_id,
                        previous_provider_id,
                    )

            # Start the playlist session
            success = await session_controller.start_playlist(playlist_name, shuffle=True)

            if not success:
                return {
                    "success": False,
                    "message": f"Couldn't find playlist '{playlist_name}'",
                    "data": {},
                    "error": "playlist_not_found",
                }

            # Get first song and start playing
            playlist_session = session_controller.get_playlist_session()
            if playlist_session and playlist_session.remaining_count() > 0:
                first_tracks = playlist_session.pop_next(1)
                if first_tracks:
                    first_track = first_tracks[0]
                    query = first_track.get("url") or first_track.get("video_id") or first_track.get("title")
                    if query:
                        query_str = query if isinstance(query, str) else str(to_json_value(query))
                        play_method = getattr(self.music, "play", None)
                        if callable(play_method):
                            if asyncio.iscoroutinefunction(play_method):
                                await cast(Callable[[str], Awaitable[object]], play_method)(query_str)
                            else:
                                cast(Callable[[str], object], play_method)(query_str)

            total = playlist_session.total_count() if playlist_session else 0
            return {
                "success": True,
                "message": f"Playing {playlist_name} playlist ({total} songs, shuffled)",
                "data": {
                    "playlist": playlist_name,
                    "total_songs": total,
                    "shuffled": True,
                    **(
                        {
                            "provider_switched_from": previous_provider_id,
                            "provider": playlist_provider_id,
                        }
                        if previous_provider_id is not None and playlist_provider_id is not None
                        else ({"provider": playlist_provider_id} if playlist_provider_id is not None else {})
                    ),
                },
                "error": None,
            }

        except Exception as e:
            logger.exception("Error playing playlist: %s", e)
            return {
                "success": False,
                "message": "Error playing the requested playlist.",
                "data": {},
                "error": "playlist_play_failed",
            }

    async def _handle_create_playlist(self, params: JsonDict) -> CommandResult:
        """Handle create_playlist command."""
        playlist_name = self._extract_playlist_name(params)
        if not playlist_name:
            return {
                "success": False,
                "message": "What should I call the new playlist?",
                "data": {},
                "error": "missing_playlist_name",
            }

        from config.settings import settings
        from music.playlist_manager import get_playlist_manager
        from music.providers.active_provider import get_active_music_provider_id

        provider_obj = params.get("provider")
        provider_id = self._coerce_provider_id(provider_obj)
        if provider_id is None:
            provider_id = get_active_music_provider_id() or settings.preferred_music_provider

        playlist_mgr = get_playlist_manager()
        _uid = str(params.get("_user_id", "") or params.get("user_id", "")) or None
        created_playlist = playlist_mgr.create_playlist(
            playlist_name,
            provider=provider_id,
            user_id=_uid,
        )
        if created_playlist is None:
            return {
                "success": False,
                "message": "Couldn't create playlist '%s'" % playlist_name,
                "data": {"playlist": playlist_name, "provider": provider_id},
                "error": "playlist_create_failed",
            }

        track_count = 0
        from music.providers.models import ProviderName

        if provider_id == ProviderName.LOCAL.value:
            stored_id = created_playlist.get("playlist_id")
            if stored_id:
                track_count = self._populate_local_playlist_from_library(str(stored_id))

        logger.info(
            "Created playlist '%s' using provider '%s'",
            playlist_name,
            provider_id,
        )

        if provider_id == ProviderName.LOCAL.value:
            if track_count > 0:
                suffix = "s" if track_count != 1 else ""
                message = "Created playlist '%s' with %d local track%s" % (
                    playlist_name,
                    track_count,
                    suffix,
                )
            else:
                message = (
                    "Created playlist '%s' — no local songs found yet. "
                    "Index your music library first." % playlist_name
                )
        else:
            message = "Created playlist '%s'" % playlist_name

        return {
            "success": True,
            "message": message,
            "data": {
                "playlist": playlist_name,
                "provider": provider_id,
                "track_count": track_count,
            },
            "error": None,
        }

    def _populate_local_playlist_from_library(self, playlist_id: str) -> int:
        """Add all indexed local library tracks to a newly created playlist.

        Returns the number of tracks added, or 0 on error.
        """
        try:
            from music.providers.local.db import get_local_library_repo

            repo = get_local_library_repo()
            tracks = repo.get_all_tracks()
            for position, row in enumerate(tracks):
                repo.add_to_playlist(int(playlist_id), row["id"], position)
            return len(tracks)
        except Exception:
            logger.exception(
                "Failed to populate local playlist %s from library",
                playlist_id,
            )
            return 0

    async def _handle_delete_playlist(self, params: JsonDict) -> CommandResult:
        """Handle delete_playlist command."""
        playlist_name = self._extract_playlist_name(params)
        if not playlist_name:
            return {
                "success": False,
                "message": "Which playlist should I delete?",
                "data": {},
                "error": "missing_playlist_name",
            }

        from music.playlist_manager import get_playlist_manager

        playlist_mgr = get_playlist_manager()
        _uid = str(params.get("_user_id", "") or params.get("user_id", "")) or None
        if not playlist_mgr.remove_playlist(playlist_name, user_id=_uid):
            return {
                "success": False,
                "message": "Playlist '%s' not found" % playlist_name,
                "data": {"playlist": playlist_name},
                "error": "playlist_not_found",
            }

        logger.info("Deleted playlist '%s'", playlist_name)
        return {
            "success": True,
            "message": "Deleted playlist '%s'" % playlist_name,
            "data": {"playlist": playlist_name},
            "error": None,
        }

    async def _handle_rename_playlist(self, params: JsonDict) -> CommandResult:
        """Handle rename_playlist command."""
        playlist_name = self._extract_playlist_name(params)
        if not playlist_name:
            return {
                "success": False,
                "message": "Which playlist should I rename?",
                "data": {},
                "error": "missing_playlist_name",
            }

        new_name_obj = params.get("new_name", "")
        new_name = new_name_obj.strip() if isinstance(new_name_obj, str) else ""
        if not new_name:
            return {
                "success": False,
                "message": "What should I rename '%s' to?" % playlist_name,
                "data": {"playlist": playlist_name},
                "error": "missing_new_name",
            }

        from music.playlist_manager import get_playlist_manager

        playlist_mgr = get_playlist_manager()
        _uid = str(params.get("_user_id", "") or params.get("user_id", "")) or None
        if playlist_mgr.get_playlist(new_name, user_id=_uid):
            return {
                "success": False,
                "message": "Playlist '%s' already exists" % new_name,
                "data": {"playlist": playlist_name, "new_name": new_name},
                "error": "playlist_already_exists",
            }
        if not playlist_mgr.get_playlist(playlist_name, user_id=_uid):
            return {
                "success": False,
                "message": "Playlist '%s' not found" % playlist_name,
                "data": {"playlist": playlist_name, "new_name": new_name},
                "error": "playlist_not_found",
            }
        if not playlist_mgr.rename_playlist(playlist_name, new_name, user_id=_uid):
            return {
                "success": False,
                "message": "Couldn't rename playlist '%s' to '%s'" % (playlist_name, new_name),
                "data": {"playlist": playlist_name, "new_name": new_name},
                "error": "playlist_rename_failed",
            }

        logger.info("Renamed playlist '%s' to '%s'", playlist_name, new_name)
        return {
            "success": True,
            "message": "Renamed playlist '%s' to '%s'" % (playlist_name, new_name),
            "data": {
                "playlist": new_name,
                "old_name": playlist_name,
                "new_name": new_name,
            },
            "error": None,
        }

    async def _handle_pause(self, params: JsonDict) -> CommandResult:
        """Handle pause command."""
        pause_method = getattr(self.music, "pause", None)
        if callable(pause_method):
            if asyncio.iscoroutinefunction(pause_method):
                result = await cast(Callable[[], Awaitable[object]], pause_method)()
            else:
                result = cast(Callable[[], object], pause_method)()

            envelope_error = _adapter_envelope_error(result, default_code="pause_failed")
            if envelope_error is not None:
                return {
                    "success": False,
                    "message": str(envelope_error["message"]),
                    "data": dict(result) if isinstance(result, dict) else {},
                    "error": str(envelope_error["code"]),
                }
            return {
                "success": True,
                "message": "Paused",
                "data": {},
                "error": None,
            }

        return {
            "success": False,
            "message": "Nothing is playing right now.",
            "data": {},
            "error": "no_player",
        }

    async def _handle_resume(self, params: JsonDict) -> CommandResult:
        """Handle resume command."""
        resume_method = getattr(self.music, "resume", None)
        if callable(resume_method):
            if asyncio.iscoroutinefunction(resume_method):
                result = await cast(Callable[[], Awaitable[object]], resume_method)()
            else:
                result = cast(Callable[[], object], resume_method)()

            envelope_error = _adapter_envelope_error(result, default_code="resume_failed")
            if envelope_error is not None:
                return {
                    "success": False,
                    "message": str(envelope_error["message"]),
                    "data": dict(result) if isinstance(result, dict) else {},
                    "error": str(envelope_error["code"]),
                }
            return {
                "success": True,
                "message": "Resumed",
                "data": {},
                "error": None,
            }

        return {
            "success": False,
            "message": "Nothing is playing right now.",
            "data": {},
            "error": "no_player",
        }

    async def _handle_skip(self, params: JsonDict) -> CommandResult:
        """Handle skip/next command."""
        method = getattr(self.music, "skip", None) or getattr(self.music, "next", None)
        if callable(method):
            if asyncio.iscoroutinefunction(method):
                skip_result = await cast(Callable[[], Awaitable[object]], method)()
            else:
                skip_result = cast(Callable[[], object], method)()

            data = dict(skip_result) if isinstance(skip_result, dict) else {}
            success = bool(data.get("ok", data.get("success", True)))
            error = data.get("error") if isinstance(data.get("error"), str) else None
            message = data.get("message") if isinstance(data.get("message"), str) else ""
            now = data.get("now_playing")
            if not message and isinstance(now, dict):
                title = str(now.get("title") or now.get("name") or "").strip()
                artist = str(now.get("artist") or "").strip()
                if title:
                    message = "Now playing: %s" % title
                    if artist:
                        message += " by %s" % artist
            if not message:
                message = "Skipped" if success else "Skip failed"
            return {
                "success": success,
                "message": message,
                "data": data,
                "error": error,
            }

        return {
            "success": False,
            "message": "Nothing is playing right now.",
            "data": {},
            "error": "no_player",
        }

    async def _handle_volume(self, params: JsonDict) -> CommandResult:
        """Handle volume set command."""
        try:
            level = self._coerce_int(params.get("level", 50), 50)
            level = await self._apply_volume(level)

            return {
                "success": True,
                "message": f"Volume set to {level}",
                "data": {"level": level},
                "error": None,
            }
        except (ValueError, TypeError):
            return {
                "success": False,
                "message": "Invalid volume level",
                "data": {},
                "error": "invalid_volume",
            }
        except _AdapterEnvelopeError as exc:
            return {
                "success": False,
                "message": str(exc.error["message"]),
                "data": {},
                "error": str(exc.error["code"]),
            }

    async def _handle_volume_up(self, params: JsonDict) -> CommandResult:
        """Handle relative volume-up command."""
        step = max(
            0,
            self._coerce_int(params.get("step", DEFAULT_VOLUME_STEP), DEFAULT_VOLUME_STEP),
        )
        try:
            level = await self._apply_volume(self._read_current_volume() + step)
        except _AdapterEnvelopeError as exc:
            return {
                "success": False,
                "message": str(exc.error["message"]),
                "data": {},
                "error": str(exc.error["code"]),
            }
        return {
            "success": True,
            "message": f"Volume set to {level}",
            "data": {"level": level},
            "error": None,
        }

    async def _handle_volume_down(self, params: JsonDict) -> CommandResult:
        """Handle relative volume-down command."""
        step = max(
            0,
            self._coerce_int(params.get("step", DEFAULT_VOLUME_STEP), DEFAULT_VOLUME_STEP),
        )
        try:
            level = await self._apply_volume(self._read_current_volume() - step)
        except _AdapterEnvelopeError as exc:
            return {
                "success": False,
                "message": str(exc.error["message"]),
                "data": {},
                "error": str(exc.error["code"]),
            }
        return {
            "success": True,
            "message": f"Volume set to {level}",
            "data": {"level": level},
            "error": None,
        }

    # ------------------------------------------------------------------ #
    # Repeat Mode Commands
    # ------------------------------------------------------------------ #

    async def _handle_repeat_off(self, params: JsonDict) -> CommandResult:
        """Handle repeat off command."""
        try:
            from models.state_manager import RepeatMode
            from music.playback_session import get_playback_session_controller

            controller = get_playback_session_controller()
            controller.set_repeat_mode(RepeatMode.OFF)

            return {
                "success": True,
                "message": "Repeat is now off",
                "data": {"repeat_mode": "off"},
                "error": None,
            }
        except Exception:
            logger.exception("Error setting repeat off")
            return {
                "success": False,
                "message": "Could not change repeat mode",
                "data": {},
                "error": "repeat_mode_failed",
            }

    async def _handle_repeat_on(self, params: JsonDict) -> CommandResult:
        """Handle repeat on (repeat all/queue) command."""
        try:
            from models.state_manager import RepeatMode
            from music.playback_session import get_playback_session_controller

            controller = get_playback_session_controller()
            controller.set_repeat_mode(RepeatMode.ALL)

            return {
                "success": True,
                "message": "Repeat all is now on",
                "data": {"repeat_mode": "all"},
                "error": None,
            }
        except Exception:
            logger.exception("Error setting repeat on")
            return {
                "success": False,
                "message": "Could not change repeat mode",
                "data": {},
                "error": "repeat_mode_failed",
            }

    async def _handle_repeat_one(self, params: JsonDict) -> CommandResult:
        """Handle repeat one (loop song) command."""
        try:
            from models.state_manager import RepeatMode
            from music.playback_session import get_playback_session_controller

            controller = get_playback_session_controller()
            controller.set_repeat_mode(RepeatMode.ONE)

            return {
                "success": True,
                "message": "Repeat one is now on. This song will loop.",
                "data": {"repeat_mode": "one"},
                "error": None,
            }
        except Exception:
            logger.exception("Error setting repeat one")
            return {
                "success": False,
                "message": "Could not change repeat mode",
                "data": {},
                "error": "repeat_mode_failed",
            }

    async def _handle_loop_song(self, params: JsonDict) -> CommandResult:
        """Alias for repeat one - loop current song."""
        return await self._handle_repeat_one(params)

    async def _handle_loop_playlist(self, params: JsonDict) -> CommandResult:
        """Alias for repeat all - loop playlist/queue."""
        return await self._handle_repeat_on(params)

    async def _handle_repeat(self, params: JsonDict) -> CommandResult:
        """Handle cycling repeat mode command."""
        try:
            from music.playback_session import get_playback_session_controller

            controller = get_playback_session_controller()
            new_mode = controller.cycle_repeat_mode()

            mode_messages = {
                "off": "Repeat is now off",
                "all": "Repeat all is now on",
                "one": "Repeat one is now on. This song will loop.",
            }

            return {
                "success": True,
                "message": mode_messages.get(new_mode.value, f"Repeat mode: {new_mode.value}"),
                "data": {"repeat_mode": new_mode.value},
                "error": None,
            }
        except Exception:
            logger.exception("Error cycling repeat mode")
            return {
                "success": False,
                "message": "Could not change repeat mode",
                "data": {},
                "error": "repeat_mode_failed",
            }

    # ------------------------------------------------------------------ #
    # Favorites, Playlist Defaults, Status, Help
    # ------------------------------------------------------------------ #

    async def _handle_play_favorites(self, params: JsonDict) -> CommandResult:
        """
        Handle play favorites command.

        Routes through PlaybackSessionController so favorites playback
        gets auto-refill when exhausted (same as playlist mode).
        """
        import random

        from music.rating_system import get_rating_system

        rating_system = get_rating_system()
        favorites = rating_system.get_favorites()

        if not favorites:
            return {
                "success": False,
                "message": "You haven't liked any songs yet! Use the heart button to like songs.",
                "data": {},
                "error": "no_favorites",
            }

        shuffle_obj = params.get("shuffle", True)
        shuffle = bool(shuffle_obj) if isinstance(shuffle_obj, (bool, int)) else True
        limit_obj = params.get("limit", 15)
        limit = min(int(limit_obj) if isinstance(limit_obj, (int, float, str)) else 15, 30)

        # Build track dicts from SongRating objects
        tracks: list[JsonDict] = []
        for fav in favorites:
            track: JsonDict = {}
            if fav.video_id:
                track["video_id"] = fav.video_id
                track["url"] = fav.video_id
            if fav.title:
                track["title"] = fav.title
            if fav.artist:
                track["artist"] = fav.artist
            if track:
                tracks.append(track)

        if not tracks:
            return {
                "success": False,
                "message": "Your favorites couldn't be loaded.",
                "data": {},
                "error": "favorites_empty",
            }

        # Select and shuffle
        if shuffle and len(tracks) > limit:
            selected = random.sample(tracks, limit)
        else:
            selected = tracks[:limit]

        if shuffle:
            random.shuffle(selected)

        try:
            from music.playback_session import (
                PlaybackMode,
                PlaylistSession,
                get_playback_session_controller,
            )
            from music.playlist_manager import get_playlist_manager

            session_controller = get_playback_session_controller()
            if session_controller._playlist_mgr is None:
                session_controller._playlist_mgr = get_playlist_manager()

            # Create a playlist session from favorites data so auto-refill works
            session_controller._playlist_session = PlaylistSession(
                playlist_name="favorites",
                original_tracks=selected,
            )
            if session_controller._state_mgr is not None:
                session_controller._state_mgr.set_playback_mode(PlaybackMode.PLAYLIST)

            # Pop first track and start playback
            playlist_session = session_controller.get_playlist_session()
            if playlist_session and playlist_session.remaining_count() > 0:
                first_tracks = playlist_session.pop_next(1)
                if first_tracks:
                    first_track = first_tracks[0]
                    query = first_track.get("url") or first_track.get("video_id") or first_track.get("title")
                    if query:
                        query_str = str(to_json_value(query))
                        play_method = getattr(self.music, "play", None)
                        if callable(play_method):
                            if asyncio.iscoroutinefunction(play_method):
                                await cast(Callable[[str], Awaitable[object]], play_method)(query_str)
                            else:
                                cast(Callable[[str], object], play_method)(query_str)

            total = len(selected)
            return {
                "success": True,
                "message": f"Playing {total} of your favorite songs!",
                "data": {"total_songs": total, "shuffled": shuffle},
                "error": None,
            }
        except Exception as e:
            logger.exception("Error playing favorites: %s", e)
            return {
                "success": False,
                "message": "Favorites playback failed due to a service error. Try playing a specific song or playlist instead.",
                "data": {},
                "error": "favorites_play_failed",
            }

    async def _handle_set_default_playlist(self, params: JsonDict) -> CommandResult:
        """Handle set_default_playlist command."""
        name_obj = params.get("playlist_name") or params.get("playlist") or params.get("query")
        playlist_name = name_obj.strip() if isinstance(name_obj, str) else ""
        if not playlist_name:
            return {
                "success": False,
                "message": "Which playlist would you like to set as default?",
                "data": {},
                "error": "missing_playlist_name",
            }

        from music.playlist_manager import get_playlist_manager

        playlist_mgr = get_playlist_manager()
        _uid = str(params.get("_user_id", "") or params.get("user_id", "")) or None
        if not playlist_mgr.get_playlist(playlist_name, user_id=_uid):
            return {
                "success": False,
                "message": f"Playlist '{playlist_name}' not found",
                "data": {},
                "error": "playlist_not_found",
            }

        if playlist_mgr.set_default_playlist(playlist_name, user_id=_uid):
            return {
                "success": True,
                "message": f"Set {playlist_name} as your default playlist. You can ask me to play your default music.",
                "data": {"playlist": playlist_name},
                "error": None,
            }
        return {
            "success": False,
            "message": f"Failed to set {playlist_name} as default",
            "data": {},
            "error": "set_default_failed",
        }

    async def _handle_status(self, params: JsonDict) -> CommandResult:
        """Handle status / what's playing command."""
        state_getter = getattr(self.music, "state", None)
        state = state_getter() if callable(state_getter) else None

        if state is not None:
            now_playing = getattr(state, "now_playing", None)
            title = getattr(now_playing, "title", None) if now_playing else None
            if isinstance(title, str) and title.strip():
                is_playing = bool(getattr(state, "is_playing", False))
                artist = getattr(now_playing, "artist", None)

                if is_playing:
                    msg = f"Now playing: {title}"
                else:
                    msg = f"{title} is paused"

                if isinstance(artist, str) and artist.strip():
                    msg += f" by {artist}"
                return {
                    "success": True,
                    "message": msg,
                    "data": {
                        "title": title,
                        "artist": artist or "",
                        "is_paused": not is_playing,
                    },
                    "error": None,
                }

        return {
            "success": True,
            "message": "Nothing is currently playing",
            "data": {},
            "error": None,
        }

    async def _handle_help(self, params: JsonDict) -> CommandResult:
        """Handle help command with context-aware response based on current configuration."""
        from config.settings import settings
        from ui.settings_manager import get_settings_manager

        try:
            has_ai = bool(getattr(settings, "openai_api_key", "")) and bool(getattr(settings, "enable_gpt", False))
        except Exception:
            has_ai = False

        try:
            sm = get_settings_manager()
            active_provider = sm.get("active_music_provider_id", "") or sm.get("preferred_music_provider", "")
            has_music_provider = bool(active_provider)
        except Exception:
            has_music_provider = False

        if not has_ai:
            help_text = (
                "I need an API key for voice commands. I can guide AI setup. "
                "Currently only basic playback commands are available: "
                "play, pause, stop, next, previous, volume up/down."
            )
        elif not has_music_provider:
            help_text = (
                "Your AI assistant is ready! "
                "I can help connect Spotify or YouTube Music. "
                "You can also ask me questions, check the weather, or manage your calendar."
            )
        else:
            help_text = (
                "I can play music, pause, resume, skip tracks, adjust volume, "
                "play your favorites, and answer questions. "
                "Try saying 'play some jazz' or 'what\u2019s playing'."
            )

        return {
            "success": True,
            "message": help_text,
            "data": {},
            "error": None,
        }


class TTSSpeaker:
    """
    Handles TTS (Text-to-Speech) output for voice command feedback.
    Provides async-safe methods for speaking status and responses.
    """

    def __init__(self, tts: object | None, task_tracker: TaskTracker | None = None):
        """
        Initialize TTS speaker.

        Args:
            tts: TTS engine instance (can be None if TTS disabled)
            task_tracker: Optional task tracker for managing background TTS tasks
        """
        self.tts = tts
        self._task_tracker = task_tracker or TaskTracker()
        # Default channel for single-user desktop / tests. Per-request
        # reads route via the ``_channel`` property below, which
        # consults the task-local contextvar published by
        # ``IntentPipeline._process_inner``. This prevents concurrent
        # different-user requests from racing the TTS suppression gate
        # (user B's Discord request flipping user A's voice speaker
        # into "non-voice, skip TTS" mid-response) — CHAN-R7.
        self._default_channel: object | None = None

    @property
    def _channel(self) -> object | None:
        """Return the per-request channel, falling back to the default.

        Resolves whichever channel the active request has published via
        ``messaging.channel.use_request_channel``. Callers reading
        ``channel_type`` in the TTS gate always see the channel that
        actually made the request — never an unrelated user's.
        """
        from messaging.channel import get_request_channel

        requested = get_request_channel()
        if requested is not None:
            return requested
        return self._default_channel

    @_channel.setter
    def _channel(self, channel: object | None) -> None:
        """Back-compat setter — stores the default slot only.

        Legacy callers that did ``speaker._channel = ch`` continue to
        work and update the default, but the request-scoped contextvar
        still wins while a request is in-flight.
        """
        self._default_channel = channel

    async def speak(self, text: str) -> None:
        """
        Speak text asynchronously.

        Args:
            text: Text to speak
        """
        if not self.tts or not text:
            return

        # Suppress TTS for text-based channels (Telegram, Discord, etc.)
        if self._channel is not None:
            ch_type = getattr(self._channel, "channel_type", "voice")
            if ch_type != "voice":
                return

        try:
            speak_method = getattr(self.tts, "say", None) or getattr(self.tts, "speak", None)
            if speak_method is None:
                return
            spoken_text = normalize_for_speech(text)
            if not spoken_text:
                return

            if asyncio.iscoroutinefunction(speak_method):
                await speak_method(spoken_text)
            else:
                # Run sync method in executor to avoid blocking
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, speak_method, spoken_text)
        except Exception as e:
            logger.debug("TTS speak error: %s", e)

    def speak_async(self, text: str) -> None:
        """
        Schedule TTS to speak in background (fire-and-forget).

        Args:
            text: Text to speak
        """
        if not self.tts or not text:
            return

        # Suppress TTS for text-based channels (Telegram, Discord, etc.)
        if self._channel is not None:
            ch_type = getattr(self._channel, "channel_type", "voice")
            if ch_type != "voice":
                return

        try:
            speak_method = getattr(self.tts, "say", None) or getattr(self.tts, "speak", None)
            if speak_method is None:
                return
            spoken_text = normalize_for_speech(text)
            if not spoken_text:
                return

            if asyncio.iscoroutinefunction(speak_method):
                # Schedule as task
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        if self._task_tracker:
                            # Use task tracker to create and track the task
                            self._task_tracker.create_task(speak_method(spoken_text))
                        else:
                            _tts_task = loop.create_task(speak_method(spoken_text))
                            _tts_task.add_done_callback(_log_task_exception)
                    else:
                        asyncio.run(speak_method(spoken_text))
                except RuntimeError as e:
                    # No running event loop - TTS will be skipped
                    logger.debug("Cannot schedule TTS (no event loop): %s", e)
            else:
                # Sync method - just call it
                speak_method(spoken_text)
        except Exception as e:
            logger.debug("TTS speak_async error: %s", e)

    async def speak_status(self, intent: str) -> None:
        """
        Speak a status message for an intent before execution.

        Args:
            intent: The intent name being executed
        """
        # Map intents to brief spoken status messages
        status_messages = {
            "play": "Playing",
            "pause": "Pausing",
            "resume": "Resuming",
            "stop": "Stopping",
            "skip": "Skipping",
            "next": "Next track",
            "previous": "Previous track",
            "volume": "Setting volume",
            "volume.set": "Setting volume",
            "volume_up": "Volume up",
            "volume_down": "Volume down",
            "volume.up": "Volume up",
            "volume.down": "Volume down",
            "repeat_off": "Repeat off",
            "repeat_on": "Repeat all",
            "repeat_one": "Repeat one",
            "loop_song": "Looping song",
            "loop_playlist": "Looping playlist",
            "repeat": "Changing repeat mode",
            "play_playlist": "Playing playlist",
            "create_playlist": "Creating playlist",
            "make_playlist": "Creating playlist",
            "delete_playlist": "Deleting playlist",
            "remove_playlist": "Deleting playlist",
            "rename_playlist": "Renaming playlist",
        }

        message = status_messages.get(intent)
        if message:
            await self.speak(message)


__all__ = ["CommandExecutor", "TTSSpeaker"]
