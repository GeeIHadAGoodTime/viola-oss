"""Agent tool handlers for music playback and playback controls.

These handlers are called by the MCP wrappers in
``mcp_servers/core_tools/server.py``. They use Viola's existing runtime
playback APIs so the agent does not duplicate player internals.
"""

from __future__ import annotations

from typing import Any

from core.cache import BaseTTLCache, Cache
from core.logging_config import get_logger
from intent.tool_types import ToolResult

logger = get_logger(__name__)

_PROVIDER_NAME_MAP: dict[str, str] = {
    "spotify": "spotify",
    "spotify_cdp": "spotify",
    "youtube": "youtube_iframe",
    "youtube_iframe": "youtube_iframe",
    "youtube music": "youtube_music",
    "youtube_music": "youtube_music",
    "local": "local",
    "my library": "local",
    "local files": "local",
}

_PROVIDER_SOURCE_MAP: dict[str, str] = {
    "local": "local",
    "spotify": "spotify_cdp",
}
_CONNECTABLE_PROVIDER_IDS = {
    "spotify",
    "youtube_music",
}
_CONNECTABLE_PROVIDER_ALIASES = {
    "spotify_cdp": "spotify",
    "youtube music": "youtube_music",
}

# Remembers the volume level a user had *before* they muted, keyed by
# user_id, so `unmute` can restore it instead of guessing (issue #2755).
#
# Why this is needed: this module is a stateless set of tool handlers --
# every call re-reads live player state from the local runtime and there is
# no request-to-request memory otherwise. `mute` always drives the runtime
# volume to 0, so by the time `unmute` runs, the freshly-read
# `/v1/player/state` volume is 0 too and there is nothing left to restore
# from. Desktop is one-user-per-install (CLAUDE.md Multi-Tenant Rule);
# user_id may legitimately be "" for the single local account, which is
# fine here since it is the real identity passed by every caller on this
# path, not a global "default" fallback -- and the cloud tool path
# (`cloud_music_bridge.cloud_playback_control`) carries the identical fix
# for its own per-user session. A bounded, TTL-evicting cache (not a plain
# dict) so a muted-and-never-unmuted session can't grow this store forever
# (#2755 hardening).
_PRE_MUTE_VOLUME_CACHE: Cache[str, int] = BaseTTLCache(max_size=512, ttl_seconds=6 * 3600.0)


def _runtime_url(path: str) -> str:
    from config.settings import settings

    port = getattr(settings, "api_port", None) or 8756
    return "http://127.0.0.1:%s%s" % (port, path)


def _auth_headers() -> dict[str, str]:
    from ui.security.bootstrap import load_bootstrap_api_key

    headers: dict[str, str] = {}
    api_key = load_bootstrap_api_key()
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _normalize_provider(provider: str) -> str:
    raw = provider.strip().lower().replace("-", "_")
    provider_id = _PROVIDER_NAME_MAP.get(raw)
    if not provider_id:
        raise ValueError("Unknown music provider: %s" % provider)
    return provider_id


def _resolve_required_user_id(user_id: str = "") -> str:
    resolved = str(user_id or "").strip()
    if resolved:
        return resolved
    try:
        from core.user_context import get_current_user_id

        resolved = get_current_user_id().strip()
        if resolved:
            return resolved
    except (ImportError, LookupError) as exc:
        logger.debug("user_context unavailable for music provider change: %s", exc)
    raise ValueError("user_id is required for user-scoped music provider changes")


def _select_provider(provider_id: str, user_id: str = "") -> None:
    from music.providers.active_provider import handle_provider_switch
    from ui.settings_manager import get_settings_manager

    scoped_user_id = _resolve_required_user_id(user_id)
    settings_manager = get_settings_manager()
    current = settings_manager.get("active_music_provider_id", None, user_id=scoped_user_id)
    if current != provider_id:
        handle_provider_switch(current, provider_id, music_service=None)
        settings_manager.set_user_setting(scoped_user_id, "active_music_provider_id", provider_id)


def _active_music_provider_id(user_id: str = "") -> str:
    try:
        from music.providers.active_provider import get_active_music_provider_id

        return str(get_active_music_provider_id(user_id=user_id or None) or "").strip()
    except Exception:
        return ""


def _local_runtime_playback_user_supported(user_id: str) -> bool:
    resolved = str(user_id or "").strip()
    if not resolved:
        return True
    from core.user_context import is_desktop_local_principal

    if is_desktop_local_principal(resolved):
        return True
    # One-user-per-install desktop: the signed-in GoTrue account IS the local
    # runtime user (CLAUDE.md Multi-Tenant Rule). When the principal equals THIS
    # install's authenticated desktop account, it plays through the local runtime
    # exactly like the bootstrap device-* principal — otherwise a managed-AI
    # conversational "play me a song" resolves the track but is refused local
    # playback (#2647). Scoped to the single install's own signed-in account
    # (never another customer's, since only that one account has a live desktop
    # session) and unavailable on cloud surfaces, where
    # get_desktop_authenticated_user_id() raises LookupError and playback routes
    # to the cloud session before this gate is ever reached.
    from core.user_context import get_desktop_authenticated_user_id

    try:
        return resolved == get_desktop_authenticated_user_id()
    except LookupError:
        return False


def _require_local_runtime_playback_user(user_id: str) -> None:
    if _local_runtime_playback_user_supported(user_id):
        return
    raise ValueError("Music playback requires a user-scoped playback session for non-local users")


async def _annotate_saved_provider_fallback(
    result: dict[str, Any],
    *,
    active_provider_before: str,
    requested_provider: str,
) -> None:
    saved_provider = active_provider_before.strip().lower()
    if saved_provider not in {"spotify", "spotify_cdp"}:
        return
    if requested_provider in {"spotify", "spotify_cdp"}:
        return

    result_provider = str(result.get("provider") or requested_provider or "").strip().lower()
    if result_provider in {"spotify", "spotify_cdp"}:
        return

    try:
        from intent.tools.music_connect import check_music_provider_status_data

        status = await check_music_provider_status_data("spotify")
    except Exception:
        return

    if status.get("connected") is True:
        return

    result.setdefault("fallback_reason", "spotify_not_connected")
    result.setdefault("fallback_from_provider", "spotify")
    result.setdefault("fallback_to_provider", result_provider or "youtube_music")
    result.setdefault("connect_tool", "connect_music_provider")
    login_url = status.get("login_url_to_show_user")
    if login_url:
        result.setdefault("login_url_to_show_user", login_url)


_PROVIDER_FAMILIES: tuple[frozenset[str], ...] = (
    frozenset({"spotify", "spotify_cdp"}),
    frozenset({"youtube", "youtube_iframe", "youtube_music"}),
    frozenset({"local", "local_library"}),
)


def _provider_family(value: str) -> str:
    normalized = (value or "").strip().lower().replace("-", "_")
    if not normalized:
        return ""
    for family in _PROVIDER_FAMILIES:
        if normalized in family:
            return sorted(family)[0]
    return normalized


async def _annotate_requested_provider_fallback(
    result: dict[str, Any],
    *,
    requested_provider: str,
) -> None:
    """Disclose when the runtime fell back to a provider the user did not ask for.

    G9: when the user explicitly requests provider X (e.g. spotify) but the
    runtime delivers playback on a different family (e.g. youtube_iframe), set
    structured fallback_used / fallback_from_provider / fallback_to_provider
    fields so the model can name the actual provider in its reply. Best-effort:
    when the requested provider is connectable and not connected, also set
    fallback_reason and login_url_to_show_user.

    Structured tool-result fields only; no prompt fragments or message rewrites.
    Per CLAUDE.md the runtime trusts the model with raw context.
    """
    requested_family = _provider_family(requested_provider)
    actual_family = _provider_family(str(result.get("provider") or ""))
    if not requested_family or not actual_family:
        return
    if requested_family == actual_family:
        return

    result.setdefault("fallback_used", True)
    result.setdefault("fallback_from_provider", requested_family)
    result.setdefault("fallback_to_provider", actual_family)

    status_provider = _connectable_provider_for_failure(requested_family, "")
    if not status_provider:
        return
    try:
        from intent.tools.music_connect import check_music_provider_status_data

        status = await check_music_provider_status_data(status_provider)
    except (
        ImportError,
        OSError,
        ValueError,
        RuntimeError,
        AttributeError,
        TypeError,
    ) as exc:
        logger.debug("provider status probe failed for %s: %s", status_provider, exc)
        return
    if status.get("connected") is False:
        result.setdefault("fallback_reason", "%s_not_connected" % status_provider)
        login_url = status.get("login_url_to_show_user")
        if login_url:
            result.setdefault("login_url_to_show_user", login_url)


def _connectable_provider_for_failure(provider_id: str, active_provider_before: str) -> str:
    candidate = (provider_id or active_provider_before).strip().lower()
    candidate = candidate.replace("-", "_")
    candidate = _CONNECTABLE_PROVIDER_ALIASES.get(candidate, candidate)
    if candidate in _CONNECTABLE_PROVIDER_IDS:
        return candidate
    return ""


async def _annotate_no_provider_connected(
    result: dict[str, Any],
    *,
    provider_id: str,
    active_provider_before: str,
) -> None:
    status_provider = _connectable_provider_for_failure(provider_id, active_provider_before)
    if not status_provider:
        return

    try:
        from intent.tools.music_connect import check_music_provider_status_data

        status = await check_music_provider_status_data(status_provider)
    except Exception:
        return

    if status.get("ok") is not True or status.get("connected") is not False:
        return

    # R4-P1-L (2026-05-30): retired recommended_next_tool /
    # next_action / recommended_next_tool_args directive fields. The
    # error_category + provider + connected fields are structured
    # signals the model reads; the runtime no longer names the next
    # tool call the model should make.
    result.setdefault("error_category", "no_provider_connected")
    result.setdefault("provider", status_provider)
    result.setdefault("connected", False)
    result.setdefault("logged_in", bool(status.get("logged_in")))
    if status.get("login_url_to_show_user"):
        result.setdefault("login_url_to_show_user", status.get("login_url_to_show_user"))


def _classify_tool_query_match(query: str, now_playing: dict[str, Any]) -> str:
    """Reuse the executor's structured query-match label for tool envelopes."""
    from intent.command_executor import CommandExecutor

    return CommandExecutor._classify_query_match(query, {"now_playing": now_playing})


async def _stop_runtime_playback_after_mismatch() -> tuple[bool, str]:
    """Stop the unrelated candidate, and report whether the stop landed.

    This used to swallow every exception AND ignore the response, while the
    caller went on to state ``candidate_not_played: True`` unconditionally.
    When the stop actually failed, an unrelated track kept playing out loud
    while the tool told the model nothing had been played -- the false-success
    shape inverted. Returns ``(stopped, error)`` so the caller can report the
    real state of the room instead of the state it intended.
    """
    try:
        data = await _post_runtime("/v1/stop", {}, timeout=10.0)
    except _runtime_call_errors() as exc:
        logger.warning("stop after unrelated candidate failed: %s", exc)
        return False, str(exc)
    if data.get("ok"):
        return True, ""
    error = _response_error(data)
    logger.warning("stop after unrelated candidate was refused: %s", error)
    return False, error


def _apply_mismatch_stop_outcome(data: dict[str, Any], stopped: bool, stop_error: str) -> None:
    """Record what really happened to the unrelated candidate.

    ``state``/``playback_status`` are the fields ``media_tools`` and the model
    read to decide whether anything is playing, so they must follow the stop
    result rather than the intent behind it.
    """
    data["candidate_stopped"] = stopped
    if stopped:
        data["state"] = "not_played"
        data["playback_status"] = "candidate_not_played"
        return
    data["state"] = "unrelated_candidate_may_be_playing"
    data["playback_status"] = "candidate_stop_failed"
    if stop_error:
        data["stop_error"] = stop_error
    mismatch = data.get("mismatch")
    if isinstance(mismatch, dict):
        mismatch["candidate_not_played"] = False
        mismatch["candidate_stop_failed"] = True


async def _reject_unrelated_candidate_if_needed(
    query: str,
    result: dict[str, Any],
) -> ToolResult | None:
    """Return a not-played mismatch envelope when the candidate is unrelated."""
    now_playing = {
        "title": result.get("title") or "",
        "artist": result.get("artist") or "",
    }
    for source_key, target_key in (
        ("track_uri", "url"),
        ("url", "url"),
        ("video_id", "video_id"),
        ("provider", "provider"),
    ):
        value = result.get(source_key)
        if value:
            now_playing[target_key] = value

    # Carry the title provenance flag into this flattened projection
    # (#2757 item 3 / #2806). ``_prefer_resolved_now_playing`` above keeps the
    # full resolved dict on ``result["now_playing"]``, but this rebuild reads
    # only the flat keys -- so without this the browser provider's query-echo
    # placeholder title compares against itself, scores "exact", and the
    # ``query_match`` field the model reads asserts a match nothing confirmed.
    resolved_now = result.get("now_playing")
    resolved_flag = resolved_now.get("title_unverified") if isinstance(resolved_now, dict) else None
    if resolved_flag or result.get("title_unverified"):
        now_playing["title_unverified"] = True

    query_match = _classify_tool_query_match(query, now_playing)
    result["query_match"] = query_match
    if query_match != "fallback_unrelated":
        return None

    stopped, stop_error = await _stop_runtime_playback_after_mismatch()
    data: dict[str, Any] = {
        "message": "music_query_mismatch",
        "query": query,
        "query_match": query_match,
        "verified": False,
        "candidate": now_playing,
        "mismatch": {
            "reason": "fallback_unrelated",
            "requested_query": query,
            "candidate_not_played": True,
        },
    }
    _apply_mismatch_stop_outcome(data, stopped, stop_error)
    if result.get("provider"):
        data["provider"] = result["provider"]
    for key in (
        "fallback_reason",
        "fallback_from_provider",
        "fallback_to_provider",
        "connect_tool",
        "login_url_to_show_user",
    ):
        if result.get(key):
            data[key] = result[key]
    return ToolResult(ok=False, data=data, error="fallback_unrelated")


def _runtime_call_errors() -> tuple[type[BaseException], ...]:
    """Exception types a local-runtime HTTP call can realistically raise.

    Resolved lazily because ``httpx`` is imported lazily inside
    ``_post_runtime`` / ``_get_runtime``; binding it at module scope would also
    break callers that swap ``sys.modules["httpx"]``. Narrow on purpose: a
    genuinely unexpected exception should keep travelling rather than be
    quietly turned into "the probe failed".
    """
    base: tuple[type[BaseException], ...] = (OSError, ValueError, RuntimeError, TypeError, AttributeError)
    try:
        import httpx
    except ImportError:
        return base
    http_error = getattr(httpx, "HTTPError", None)
    if isinstance(http_error, type) and issubclass(http_error, BaseException):
        return (http_error, *base)
    return base


def _response_error(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or error)
    if error:
        return str(error)
    message = payload.get("message")
    if message:
        return str(message)
    return "Request failed"


async def _post_runtime(path: str, payload: dict[str, Any] | None = None, *, timeout: float = 15.0) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(_runtime_url(path), json=payload or {}, headers=_auth_headers())
        data = resp.json()

    if not isinstance(data, dict):
        return {"ok": False, "error": "Runtime returned a non-object response"}
    data.setdefault("_status_code", resp.status_code)
    return data


async def _get_runtime(path: str, *, timeout: float = 10.0) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(_runtime_url(path), headers=_auth_headers())
        data = resp.json()

    if not isinstance(data, dict):
        return {"ok": False, "error": "Runtime returned a non-object response"}
    data.setdefault("_status_code", resp.status_code)
    return data


def _player_state_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _track_message(track: dict[str, Any], queue_size: int = 0) -> str:
    title = str(track.get("title") or track.get("name") or "").strip()
    artist = str(track.get("artist") or "").strip()
    if not title:
        return ""
    message = "Now playing: %s" % title
    if artist:
        message += " by %s" % artist
    if queue_size > 1:
        message += ". %d tracks queued." % queue_size
    return message


# Verification verdicts for a playback claim. These are what let the model
# tell "I checked and it is playing" apart from "the check never happened".
VERIFY_PLAYING = "live_state_playing"
VERIFY_NOT_PLAYING = "live_state_not_playing"
VERIFY_UNAVAILABLE = "runtime_state_unavailable"


async def _read_live_playback_state() -> dict[str, Any]:
    """Snapshot the live player, always stamping whether the probe succeeded.

    An empty dict used to mean two incompatible things -- "the runtime says
    nothing is playing" and "the probe never completed" -- so a caller that
    fell back on a failed probe could not tell it had verified nothing. Every
    return now carries ``probe_ok``.
    """
    try:
        state_payload = await _get_runtime("/v1/player/state", timeout=5.0)
    except _runtime_call_errors() as exc:
        logger.debug("live player-state probe failed: %s", exc)
        return {"probe_ok": False, "probe_error": str(exc)}
    if not state_payload.get("ok"):
        return {"probe_ok": False, "probe_error": _response_error(state_payload)}

    state_data = _player_state_data(state_payload)
    now = state_data.get("now_playing") or state_data.get("current")
    queue = state_data.get("queue")
    queue_size = len(queue) if isinstance(queue, list) else 0
    state: dict[str, Any] = {
        "probe_ok": True,
        "queue_size": queue_size,
        "is_playing": bool(state_data.get("is_playing")),
    }
    if isinstance(now, dict):
        state["now_playing"] = now
    volume = state_data.get("volume")
    if isinstance(volume, (int, float)):
        state["volume"] = max(0, min(100, int(volume)))
    position = state_data.get("position")
    if not isinstance(position, (int, float)):
        position_ms = state_data.get("position_ms")
        position = float(position_ms) / 1000.0 if isinstance(position_ms, (int, float)) else None
    if isinstance(position, (int, float)):
        state["position_seconds"] = float(position)
    return state


def _live_verification(state: dict[str, Any]) -> str:
    if not state.get("probe_ok"):
        return VERIFY_UNAVAILABLE
    now = state.get("now_playing")
    title = str(now.get("title") or now.get("name") or "").strip() if isinstance(now, dict) else ""
    if state.get("is_playing") and title:
        return VERIFY_PLAYING
    return VERIFY_NOT_PLAYING


def _unconfirmed_playback_message(label: str, verification: str) -> str:
    """Say what is actually known when the live check did not confirm playback."""
    subject = label or "the track"
    if verification == VERIFY_UNAVAILABLE:
        return "I sent %s to the player but could not reach it to confirm playback started." % subject
    return "I sent %s to the player, and it does not report anything playing yet." % subject


async def _prefer_resolved_now_playing(
    result: dict[str, Any],
    *,
    fallback_queue_size: int = 0,
) -> dict[str, Any]:
    """Merge the live player state into a play result AND record the verdict.

    Two separate jobs, and the second one is why this exists: the live read is
    the only evidence the tool has that audio started, so the result must carry
    whether that read confirmed playback (``playback_verified`` +
    ``verification``). Previously the probe degraded to an empty dict on any
    transport failure and the caller returned the unverified result unchanged,
    so a result that was never checked was indistinguishable from one that was.
    """
    if result.get("playback_status") == "candidate_not_played":
        return result
    if isinstance(result.get("room_route"), dict) or isinstance(result.get("pairing_flow"), dict):
        # A room-route / pairing answer makes no playback claim to verify.
        return result

    live = await _read_live_playback_state()
    verification = _live_verification(live)
    result["verification"] = verification
    result["playback_verified"] = verification == VERIFY_PLAYING

    live_now = live.get("now_playing")
    live_queue_size = live.get("queue_size") or 0
    if not isinstance(live_queue_size, int):
        live_queue_size = 0

    now = result.get("now_playing")
    if not isinstance(now, dict):
        now = live_now if isinstance(live_now, dict) else None
    if not isinstance(now, dict):
        return result

    title = str(now.get("title") or now.get("name") or "").strip()
    if not title:
        return result
    query = str(result.get("query") or "").strip()
    if query and _classify_tool_query_match(query, now) == "fallback_unrelated":
        return result

    result["now_playing"] = now
    result["title"] = title
    artist = str(now.get("artist") or "").strip()
    if artist:
        result["artist"] = artist
    for source_key, target_key in (
        ("url", "track_uri"),
        ("video_id", "video_id"),
        ("provider", "provider"),
    ):
        value = now.get(source_key)
        if value:
            result[target_key] = value

    queue_size = live_queue_size or fallback_queue_size
    if queue_size:
        result["queue_size"] = queue_size
    message = _track_message(now, queue_size)
    if message:
        result["message"] = message
    return result


def _play_tool_result(result: dict[str, Any], *, fallback_label: str = "") -> ToolResult:
    """Turn a play result into an envelope that matches what was established.

    ``/v1/play`` answering ok means the player ACCEPTED the request, which is
    real but is not the same as audio playing -- that is what the live state
    read establishes. When the read confirms playback the envelope is a plain
    success; when it could not run, or ran and saw nothing playing, the
    envelope says ``unverified`` and the message stops asserting a track is
    playing. ``unverified`` is not failure: the request did land, and claiming
    it failed would be its own false report.
    """
    verification = result.get("verification")
    if verification is None or verification == VERIFY_PLAYING:
        # ``None`` = this result makes no playback claim to verify (a room
        # route / pairing answer), so there is nothing to qualify.
        return ToolResult(ok=True, data=result)

    label = str(result.get("title") or fallback_label or "").strip()
    artist = str(result.get("artist") or "").strip()
    if label and artist and artist not in label:
        label = "%s by %s" % (label, artist)
    result["message"] = _unconfirmed_playback_message(label, str(verification))
    return ToolResult(ok=True, data=result, unverified=True)


_SEEK_TOLERANCE_SECONDS = 1.5


async def _transport_result(action_name: str, before: dict[str, Any], response: dict[str, Any]) -> ToolResult:
    """Report pause/resume/stop from the player state, not from the request.

    ``/v1/<action>`` answering ok means the runtime accepted the command. The
    effect the user actually experiences is whether sound is coming out, and
    that is only knowable by reading the player back. Three outcomes, kept
    apart on purpose:

    * the state agrees -- plain success;
    * ``resume`` left nothing playing -- a real failure, the same call the
      cloud bridge and the instant-command resume path already make, because
      resuming with no track is a no-op the user hears as silence;
    * ``pause``/``stop`` still report playing, or the state could not be read
      at all -- ``unverified``. One read cannot separate a command that never
      landed from a state that has not caught up, and guessing either way is
      how a tool ends up reporting something it never saw.
    """
    after = await _read_live_playback_state()
    result_data: dict[str, Any] = {
        "action": action_name,
        "response": response,
    }
    if action_name in {"pause", "stop"}:
        result_data["was_playing"] = bool(before.get("is_playing")) if before.get("probe_ok") else None

    if not after.get("probe_ok"):
        result_data["verified"] = False
        result_data["message"] = "I sent %s to the player but could not reach it to confirm the change." % action_name
        return ToolResult(ok=True, data=result_data, unverified=True)

    is_playing = bool(after.get("is_playing"))
    result_data["is_playing"] = is_playing
    now = after.get("now_playing")
    if isinstance(now, dict):
        result_data["now_playing"] = now

    if action_name == "resume":
        if is_playing:
            result_data["verified"] = True
            result_data["message"] = "Playback resumed."
            return ToolResult(ok=True, data=result_data)
        result_data["verified"] = False
        result_data["message"] = "Nothing is playing -- there was no track to resume."
        return ToolResult(ok=False, data=result_data, error="Playback did not resume.")

    if not is_playing:
        result_data["verified"] = True
        if action_name == "pause":
            was_playing = result_data.get("was_playing")
            result_data["message"] = (
                "Playback paused." if was_playing is not False else "Nothing was playing, so there is nothing to pause."
            )
        else:
            result_data["message"] = "Playback stopped."
        return ToolResult(ok=True, data=result_data)

    result_data["verified"] = False
    result_data["message"] = "I sent %s to the player, but it still reports playing." % action_name
    return ToolResult(ok=True, data=result_data, unverified=True)


async def _seek_result(
    action_name: str,
    target_seconds: float,
    response: dict[str, Any],
    *,
    delta_seconds: float | None = None,
) -> ToolResult:
    """Report where the player actually is, not where it was asked to go."""
    after = await _read_live_playback_state()
    result_data: dict[str, Any] = {
        "action": action_name,
        "requested_position_seconds": target_seconds,
        "response": response,
    }
    if delta_seconds is not None:
        result_data["delta_seconds"] = delta_seconds

    observed = after.get("position_seconds")
    if not after.get("probe_ok") or not isinstance(observed, (int, float)):
        result_data["verified"] = False
        result_data["message"] = (
            "I asked the player to move to %.1f seconds but could not read back its position." % target_seconds
        )
        return ToolResult(ok=True, data=result_data, unverified=True)

    result_data["position_seconds"] = float(observed)
    if abs(float(observed) - target_seconds) <= _SEEK_TOLERANCE_SECONDS:
        result_data["verified"] = True
        result_data["message"] = "Moved to %.1f seconds." % float(observed)
        return ToolResult(ok=True, data=result_data)

    result_data["verified"] = False
    result_data["message"] = "I asked the player to move to %.1f seconds; it reports %.1f seconds." % (
        target_seconds,
        float(observed),
    )
    return ToolResult(ok=True, data=result_data, unverified=True)


def _normalize_rating_value(value: str) -> str:
    raw = value.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "thumbs_up": "thumbs_up",
        "up": "thumbs_up",
        "like": "thumbs_up",
        "liked": "thumbs_up",
        "positive": "thumbs_up",
        "thumbsup": "thumbs_up",
        "thumbs_down": "thumbs_down",
        "down": "thumbs_down",
        "dislike": "thumbs_down",
        "disliked": "thumbs_down",
        "negative": "thumbs_down",
        "thumbsdown": "thumbs_down",
    }
    normalized = aliases.get(raw)
    if not normalized:
        raise ValueError("Rating value must be 'thumbs_up' or 'thumbs_down'.")
    return normalized


async def view_queue_handler() -> ToolResult:
    """Return the current playback queue snapshot from Viola's music player.

    Calls the local /v1/queue endpoint so the agent can describe what is
    playing now and what is queued next. Returns now-playing track plus
    upcoming tracks (title, artist when available, total queue size).
    """
    try:
        import httpx

        from config.settings import settings
        from ui.security.bootstrap import load_bootstrap_api_key

        port = getattr(settings, "api_port", None) or 8756
        url = "http://127.0.0.1:%s/v1/queue" % port
        headers = {}
        api_key = load_bootstrap_api_key()
        if api_key:
            headers["X-API-Key"] = api_key

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
            data = resp.json()

        if resp.status_code != 200:
            return ToolResult(
                ok=False,
                data=None,
                error="Queue request returned %s" % resp.status_code,
            )

        # /v1/queue's handler (QueueService.snapshot()) returns a flat legacy
        # dict with no "data" key, so record_and_call's ensure_envelope
        # (contracts/api_response.py:from_legacy) nests every field except
        # "ok"/"error" under "data" on the wire. Every sibling reader in this
        # module unwraps that nesting (_player_state_data, _response_data,
        # etc.); this handler read the top level directly and always saw
        # None/[] for now_playing/queue, so it reported an empty queue no
        # matter what was actually playing (#4787).
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]

        now = data.get("now_playing") or data.get("current") or None
        items = data.get("queue") or data.get("items") or []
        size = data.get("queue_size") if isinstance(data.get("queue_size"), int) else len(items)

        preview = []
        for i, item in enumerate(items[:10]):
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("name") or ""
            artist = item.get("artist") or ""
            line = title + (" — " + artist if artist else "")
            if line.strip():
                preview.append("%d. %s" % (i + 1, line))

        if now and isinstance(now, dict):
            now_title = now.get("title") or now.get("name") or ""
            now_artist = now.get("artist") or ""
            now_line = now_title + (" by " + now_artist if now_artist else "")
        else:
            now_line = ""

        if not now_line and not preview:
            message = "The queue is empty. Nothing is playing right now."
        else:
            parts = []
            if now_line:
                parts.append("Now playing: %s." % now_line)
            if preview:
                parts.append("Up next:\n" + "\n".join(preview))
            if size and (size > len(items) or len(items) > 10):
                parts.append("(%d total)" % size)
            message = " ".join(parts) if not preview else parts[0] + "\n" + "\n".join(parts[1:])

        return ToolResult(
            ok=True,
            data={
                "message": message,
                "now_playing": now,
                "queue": items,
                "queue_size": size,
            },
        )
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="Failed to read queue: %s" % exc)


async def play_music_handler(query: str, provider: str = "", target_room: str = "", user_id: str = "") -> ToolResult:
    """Search for and play music through Viola's internal music player.

    This calls the same /v1/play endpoint that the rule interpreter uses,
    so it goes through yt-dlp search, YouTube Music, Spotify, or local
    library depending on configuration.

    Args:
        query: What to play: artist, song, genre, mood, or any music query.
        provider: Optional provider override, such as spotify, youtube_music,
            youtube, or local. When provided, Viola switches the active
            provider before starting playback.
        target_room: Optional Viola room, room group, or paired Spoke speaker.
            When provided, the runtime returns structured room-route and
            pairing-flow facts when the target is not paired.

    Returns:
        ToolResult with playback status and queued track info.
    """
    query = query.strip()
    if not query:
        return ToolResult(ok=False, data=None, error="Music query cannot be empty.")

    try:
        from core.user_context import user_scope

        scoped_user_id = str(user_id or "").strip()
        _require_local_runtime_playback_user(scoped_user_id)
        active_provider_before = _active_music_provider_id(scoped_user_id)
        provider_id = ""
        source = None
        if provider.strip():
            provider_id = _normalize_provider(provider)
            scoped_user_id = _resolve_required_user_id(scoped_user_id)
            with user_scope(scoped_user_id):
                active_provider_before = _active_music_provider_id(scoped_user_id)
            # #2776: do NOT persist the provider switch here. Persisting
            # before /v1/play is attempted means a failed playback attempt
            # has already flipped the user's default provider. The switch
            # is committed below, only after /v1/play reports ok.
            source = _PROVIDER_SOURCE_MAP.get(provider_id)

        request_payload: dict[str, Any] = {"query": query}
        if source:
            request_payload["source"] = source
        if target_room.strip():
            request_payload["target_room"] = target_room.strip()
        data = await _post_runtime("/v1/play", request_payload)
        status_code = int(data.get("_status_code", 0) or 0)

        if status_code == 200 and data.get("ok"):
            if provider_id:
                with user_scope(scoped_user_id):
                    _select_provider(provider_id, scoped_user_id)
            # record_and_call/ensure_envelope (contracts/api_response.py)
            # always nests a legacy {"ok": ..., <fields>} handler return under
            # "data" when the handler itself did not set a top-level "data"
            # key -- so /v1/play's plain {"enqueued": {...}} success response
            # (the no-target_room path, ui/api/routes/control.py post_play)
            # arrives here exactly as {"ok": True, "data": {"enqueued": {...}}},
            # indistinguishable BY SHAPE from the room-route/CommandExecutor
            # response (which sets "data" itself server-side and so is left
            # alone by the envelope wrapper). `isinstance(routed_data, dict)`
            # can no longer tell the two apart -- it is true for both -- so it
            # silently routed every ordinary play through the room-route
            # branch, which reads `result.get("query_match")` for a field the
            # plain path never has, and skipped `_reject_unrelated_candidate_if_needed`
            # entirely for the same reason. That is the exact false-success
            # class (#2757/#2806) this file's own comments describe: an
            # unrelated candidate's stop-and-reject safety net went unreachable
            # for every default (no target_room) play command (#4787 sibling).
            # `target_room` is the real, deterministic condition the server
            # branches on (it is the same string this call sent as
            # request_payload["target_room"]), so branch on that instead of on
            # response shape.
            routed_data = data.get("data") if isinstance(data.get("data"), dict) else {}
            if target_room.strip():
                top_level_message = str(data.get("message") or "")
                nested_message = str(routed_data.get("message") or "")
                routed_message = top_level_message or nested_message
                result = dict(routed_data)
                if routed_message:
                    result.setdefault("message", routed_message)
                result.setdefault("query", query)
                if provider_id:
                    result.setdefault("provider", provider_id)
                await _annotate_requested_provider_fallback(
                    result,
                    requested_provider=provider_id,
                )
                await _annotate_saved_provider_fallback(
                    result,
                    active_provider_before=active_provider_before,
                    requested_provider=provider_id,
                )
                # #2776 verified: this is the room-route path, whose backend
                # contract (intent/command_executor.py's play command, ~line
                # 1342: `verified_data["query_match"] = query_match`) ALWAYS
                # sets query_match on this dict before returning success --
                # confirmed by reading the full command_executor success path,
                # not speculative. This guard is reliable.
                if result.get("query_match") == "fallback_unrelated":
                    stopped, stop_error = await _stop_runtime_playback_after_mismatch()
                    _apply_mismatch_stop_outcome(result, stopped, stop_error)
                    return ToolResult(ok=False, data=result, error="fallback_unrelated")
                result = await _prefer_resolved_now_playing(result)
                return _play_tool_result(result)

            # Plain /v1/play response: {"enqueued": {...}}, nested under
            # "data" by the envelope wrapper (see comment above).
            enqueued = routed_data.get("enqueued") or {}

            # The backend title is the only report of what was actually
            # resolved. Echoing the user's own query back as ``title`` turned
            # the REQUEST into the answer -- "Now playing: <whatever the user
            # asked for>" was true by construction whatever the player did.
            # Keep the echo (the model still needs something to name) but mark
            # its provenance with the same ``title_unverified`` flag the
            # browser provider and command_executor already use.
            resolved_title = str(enqueued.get("title") or "").strip()
            title_from_query = not resolved_title
            title = resolved_title or query
            artist = enqueued.get("artist", "")
            queue_size = enqueued.get("queue_size", 0)
            track_uri = enqueued.get("url") or ""
            video_id = enqueued.get("video_id") or ""
            if not track_uri and video_id:
                track_uri = "https://www.youtube.com/watch?v=%s" % video_id
            provider_result = enqueued.get("provider") or provider_id or "youtube_music"

            label = title
            if artist:
                label = "%s by %s" % (title, artist)
            if title_from_query:
                message = "I asked the player for %r; it has not reported which track it loaded." % query
            else:
                msg_parts = ["Now playing: %s" % label]
                if queue_size and queue_size > 1:
                    msg_parts.append("%d tracks queued." % queue_size)
                message = " ".join(msg_parts)

            result = {
                "message": message,
                "title": title,
                "artist": artist,
                "queue_size": queue_size,
                "query": query,
            }
            if title_from_query:
                result["title_unverified"] = True
                result["title_source"] = "request_query"
            if track_uri:
                result["track_uri"] = track_uri
            if video_id:
                result["video_id"] = video_id
            if provider_result:
                result["provider"] = provider_result
            result = await _prefer_resolved_now_playing(result, fallback_queue_size=queue_size)
            await _annotate_requested_provider_fallback(
                result,
                requested_provider=provider_id,
            )
            await _annotate_saved_provider_fallback(
                result,
                active_provider_before=active_provider_before,
                requested_provider=provider_id,
            )
            mismatch = await _reject_unrelated_candidate_if_needed(query, result)
            if mismatch is not None:
                return mismatch

            return _play_tool_result(result, fallback_label=label)

        error_message = _response_error(data) or "Playback failed"
        failure_data = dict(data) if isinstance(data, dict) else {"error": error_message}
        failure_data.setdefault("error", error_message)
        if provider_id:
            failure_data.setdefault("provider", provider_id)
        await _annotate_no_provider_connected(
            failure_data,
            provider_id=provider_id,
            active_provider_before=active_provider_before,
        )
        return ToolResult(
            ok=False,
            data=failure_data,
            error=error_message,
            error_category=failure_data.get("error_category"),
        )

    except Exception as exc:
        return ToolResult(
            ok=False,
            data=None,
            error="Failed to play music: %s" % exc,
        )


async def playback_control_handler(
    action: str,
    *,
    seek_to_seconds: float | None = None,
    seek_delta_seconds: float | None = None,
    volume_level: int | None = None,
    volume_step: int | None = None,
    user_id: str = "",
) -> ToolResult:
    """Control active playback through Viola's runtime playback APIs."""
    action_name = action.strip().lower()
    action_aliases = {
        "seek": "seek_to",
        "seek_absolute": "seek_to",
        "relative_seek": "seek_relative",
        "clear": "clear_queue",
        "clearqueue": "clear_queue",
        "skip_track": "skip",
        "next_track": "next",
        "advance": "next",
        "advance_track": "next",
        "previous_track": "previous",
        "prev": "previous",
        "back": "previous",
        "volume": "volume_set",
        "set_volume": "volume_set",
        "volume_up": "volume_up",
        "up_volume": "volume_up",
        "volume_down": "volume_down",
        "down_volume": "volume_down",
    }
    action_name = action_aliases.get(action_name, action_name)

    # Cloud surface: route transport/volume to the user-scoped cloud playback
    # session service instead of the localhost runtime, which errors for cloud
    # users. Desktop keeps the local runtime path below.
    from intent.tools.cloud_music_bridge import (
        cloud_playback_control,
        should_route_to_cloud_playback,
    )

    if should_route_to_cloud_playback():
        return await cloud_playback_control(
            action=action_name,
            seek_to_seconds=seek_to_seconds,
            seek_delta_seconds=seek_delta_seconds,
            volume_level=volume_level,
            volume_step=volume_step,
            user_id=user_id,
        )

    try:
        _require_local_runtime_playback_user(user_id)
        if action_name in {"volume_set", "volume_up", "volume_down", "mute", "unmute"}:
            scoped_user_id = str(user_id or "").strip()
            state = await _read_live_playback_state()
            previous_level = state.get("volume")
            if not isinstance(previous_level, int):
                previous_level = 50

            if action_name == "volume_set":
                if volume_level is None:
                    return ToolResult(
                        ok=False,
                        data=None,
                        error="volume_level is required for volume_set",
                    )
                target_level = int(volume_level)
            elif action_name == "volume_up":
                step = int(volume_step) if volume_step is not None else 10
                target_level = previous_level + max(0, step)
            elif action_name == "volume_down":
                step = int(volume_step) if volume_step is not None else 10
                target_level = previous_level - max(0, step)
            elif action_name == "mute":
                # Remember the level we are muting FROM (not 0) so a later
                # unmute has something real to restore -- see #2755.
                # NOTE: scoped_user_id may legitimately be "" (single local
                # desktop account) -- that is still a valid, real cache key
                # here, not an absent one, so it must NOT be excluded by a
                # truthiness check (an earlier draft of this fix did exactly
                # that and silently broke pre-mute storage for the default
                # local user; caught by
                # test_playback_control_handler_unmute_restores_pre_mute_level).
                if previous_level > 0:
                    _PRE_MUTE_VOLUME_CACHE.put(scoped_user_id, previous_level)
                target_level = 0
            else:
                # unmute. If we're not actually muted (volume already > 0)
                # this is a no-op echo of the current level -- ignore any
                # stale remembered value so an unmute-without-a-mute never
                # jumps to an old level. Only fall back to the remembered
                # pre-mute level when we are truly muted (volume == 0).
                if previous_level > 0:
                    target_level = previous_level
                else:
                    remembered = _PRE_MUTE_VOLUME_CACHE.get(scoped_user_id)
                    target_level = remembered if isinstance(remembered, int) and remembered > 0 else 50

            target_level = max(0, min(100, target_level))
            data = await _post_runtime("/v1/volume", {"level": target_level}, timeout=10.0)
            if data.get("ok"):
                if action_name == "unmute":
                    _PRE_MUTE_VOLUME_CACHE.invalidate(scoped_user_id)
                # The runtime reports the level it actually applied; only fall
                # back to re-reading the player, and finally to saying we do
                # not know. Echoing ``target_level`` would report the REQUEST
                # as the outcome, which is exactly the shape being removed.
                # /v1/volume's handler returns a flat {"ok", "volume", "error"}
                # dict with no "data" key, so ensure_envelope nests "volume"
                # under "data" on the wire (same class as #4787) -- check both
                # so a directly-nested envelope is read correctly instead of
                # always missing and paying for an extra live-state round trip.
                response_data = data.get("data") if isinstance(data.get("data"), dict) else {}
                applied = data.get("volume")
                if applied is None:
                    applied = response_data.get("volume")
                level_verified = isinstance(applied, (int, float))
                if not level_verified:
                    after = await _read_live_playback_state()
                    observed = after.get("volume")
                    if isinstance(observed, (int, float)):
                        applied = observed
                        level_verified = True
                    else:
                        applied = target_level
                result_data: dict[str, Any] = {
                    "message": (
                        "Music player volume set to %d" % int(applied)
                        if level_verified
                        else "I asked the music player for volume %d but it did not report the level it applied."
                        % int(target_level)
                    ),
                    "action": action_name,
                    "surface": "music_player",
                    "level": int(applied),
                    "requested_level": int(target_level),
                    "verified": level_verified,
                    "previous_level": previous_level,
                    "response": data,
                }
                for key in ("now_playing", "queue_size", "is_playing"):
                    if key in state:
                        result_data[key] = state[key]
                return ToolResult(ok=True, data=result_data, unverified=not level_verified)
            return ToolResult(ok=False, data=data, error=_response_error(data))

        if action_name in {"pause", "resume", "stop"}:
            before = await _read_live_playback_state()
            data = await _post_runtime("/v1/%s" % action_name, {}, timeout=10.0)
            if not data.get("ok"):
                return ToolResult(ok=False, data=data, error=_response_error(data))
            return await _transport_result(action_name, before, data)

        if action_name in {"skip", "next", "previous"}:
            endpoint = "/v1/skip" if action_name == "skip" else "/v1/%s" % action_name
            data = await _post_runtime(endpoint, {}, timeout=10.0)
            if data.get("ok"):
                live_state = await _read_live_playback_state()
                response_data = data.get("data")
                if isinstance(response_data, dict):
                    source_state = response_data
                else:
                    source_state = data
                now = source_state.get("now_playing")
                if not isinstance(now, dict):
                    now = live_state.get("now_playing")
                queue_size = source_state.get("queue_size")
                if not isinstance(queue_size, int):
                    queue_size = live_state.get("queue_size", 0)
                if not isinstance(queue_size, int):
                    queue_size = 0
                message = _track_message(now, queue_size) if isinstance(now, dict) else ""
                if not message:
                    message = {
                        "skip": "Skipped",
                        "next": "Next track",
                        "previous": "Previous track",
                    }[action_name]
                result_data: dict[str, Any] = {
                    "message": message,
                    "action": action_name,
                    "response": data,
                }
                for key in (
                    "track_id",
                    "next_track_id",
                    "mode",
                    "is_playing",
                    "autoplay",
                ):
                    value = source_state.get(key)
                    if value is not None:
                        result_data[key] = value
                if isinstance(now, dict):
                    result_data["now_playing"] = now
                result_data["queue_size"] = queue_size
                # "Skipped"/"Next track"/"Previous track" is the command, not
                # an observation: when neither the response nor the live read
                # names a track, nobody has established that a new track is
                # playing. Say which of the two happened.
                verified = (
                    bool(live_state.get("probe_ok")) and isinstance(now, dict) and bool(live_state.get("is_playing"))
                )
                result_data["verified"] = verified
                if not verified and not live_state.get("probe_ok"):
                    result_data["verification"] = VERIFY_UNAVAILABLE
                elif not verified:
                    result_data["verification"] = VERIFY_NOT_PLAYING
                else:
                    result_data["verification"] = VERIFY_PLAYING
                return ToolResult(ok=True, data=result_data, unverified=not verified)
            return ToolResult(ok=False, data=data, error=_response_error(data))

        if action_name == "clear_queue":
            data = await _post_runtime("/v1/queue/clear", {}, timeout=10.0)
            if not data.get("ok"):
                return ToolResult(ok=False, data=data, error=_response_error(data))
            after = await _read_live_playback_state()
            result_data = {
                "action": action_name,
                "response": data,
            }
            if not after.get("probe_ok"):
                result_data["message"] = "I cleared the queue but could not reach the player to confirm it is empty."
                result_data["verified"] = False
                return ToolResult(ok=True, data=result_data, unverified=True)
            remaining = after.get("queue_size")
            remaining = remaining if isinstance(remaining, int) else 0
            result_data["queue_size"] = remaining
            if remaining == 0:
                result_data["message"] = "Queue cleared."
                result_data["verified"] = True
                return ToolResult(ok=True, data=result_data)
            # The queue does not refill itself between the clear and this read,
            # so a non-empty queue here is not lag -- it is the clear not
            # having happened. This is the one transport effect a single read
            # settles outright, so it is reported as a failure, not as
            # "unconfirmed".
            result_data["message"] = "The queue still holds %d track(s) after clearing it." % remaining
            result_data["verified"] = False
            return ToolResult(ok=False, data=result_data, error="Queue was not cleared.")

        if action_name == "restart":
            seek_to_seconds = 0.0
            action_name = "seek_to"

        if action_name == "seek_to":
            if seek_to_seconds is None:
                return ToolResult(ok=False, data=None, error="seek_to_seconds is required for seek_to")
            target_seconds = max(0.0, float(seek_to_seconds))
            target_ms = round(target_seconds * 1000)
            data = await _post_runtime("/v1/seek", {"position": target_ms}, timeout=15.0)
            if not data.get("ok"):
                return ToolResult(ok=False, data=data, error=_response_error(data))
            return await _seek_result("seek_to", target_seconds, data)

        if action_name == "seek_relative":
            if seek_delta_seconds is None:
                return ToolResult(
                    ok=False,
                    data=None,
                    error="seek_delta_seconds is required for seek_relative",
                )
            state_payload = await _get_runtime("/v1/player/state")
            if not state_payload.get("ok"):
                return ToolResult(ok=False, data=state_payload, error=_response_error(state_payload))
            state_data = _player_state_data(state_payload)
            current = state_data.get("position")
            if not isinstance(current, (int, float)):
                position_ms = state_data.get("position_ms")
                current = float(position_ms) / 1000.0 if isinstance(position_ms, (int, float)) else 0.0
            target_seconds = max(0.0, float(current) + float(seek_delta_seconds))
            target_ms = round(target_seconds * 1000)
            data = await _post_runtime("/v1/seek", {"position": target_ms}, timeout=15.0)
            if not data.get("ok"):
                return ToolResult(ok=False, data=data, error=_response_error(data))
            return await _seek_result(
                "seek_relative",
                target_seconds,
                data,
                delta_seconds=float(seek_delta_seconds),
            )

        return ToolResult(ok=False, data=None, error="Unknown playback action: %s" % action)
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="Playback control failed: %s" % exc)


async def rate_track_handler(
    value: str,
    *,
    track_id: str = "",
    title: str = "",
    artist: str = "",
    user_id: str = "",
) -> ToolResult:
    """Rate the current track or an explicit track through Viola's rating APIs."""
    del user_id

    try:
        rating_value = _normalize_rating_value(value)
        explicit_track_id = track_id.strip()

        if explicit_track_id:
            track_rating = "up" if rating_value == "thumbs_up" else "down"
            request_payload: dict[str, Any] = {
                "track_id": explicit_track_id,
                "rating": track_rating,
            }
            if title.strip():
                request_payload["title"] = title.strip()
            if artist.strip():
                request_payload["artist"] = artist.strip()

            data = await _post_runtime("/v1/track/rate", request_payload, timeout=10.0)
            if data.get("ok"):
                return ToolResult(
                    ok=True,
                    data={
                        "message": "Track rated %s" % rating_value,
                        "value": rating_value,
                        "rating": track_rating,
                        "track_id": explicit_track_id,
                        "response": data,
                    },
                )
            return ToolResult(ok=False, data=data, error=_response_error(data))

        ui_rating = "liked" if rating_value == "thumbs_up" else "disliked"
        data = await _post_runtime("/v1/rating", {"rating": ui_rating}, timeout=10.0)
        if data.get("ok"):
            response_data = data.get("data") if isinstance(data.get("data"), dict) else {}
            rated_track_id = data.get("video_id") or response_data.get("video_id") or ""
            result_data: dict[str, Any] = {
                "message": "Current track rated %s" % rating_value,
                "value": rating_value,
                "rating": ui_rating,
                "response": data,
            }
            if rated_track_id:
                result_data["track_id"] = rated_track_id
            return ToolResult(ok=True, data=result_data)

        return ToolResult(ok=False, data=data, error=_response_error(data))
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="Failed to rate track: %s" % exc)
