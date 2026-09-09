"""Narrow command-service bridge for explicit music commands."""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any

from music.exceptions import InvalidOperation

_PROVIDER_ALIASES = {
    "spotify": "spotify",
    "youtube": "youtube_iframe",
    "youtube music": "youtube_music",
    "local": "local",
    "local files": "local",
    "local library": "local",
    "my library": "local",
    "my local library": "local",
}

_PROVIDER_QUALIFIED_PLAY_PATTERN = re.compile(
    r"^\s*(?:please\s+)?(?:play|put\s+on)\s+"
    r"(?P<query>.+?)\s+"
    r"(?:on|from)\s+"
    r"(?P<provider>"
    r"spotify|youtube(?:\s+music)?|local(?:\s+files|\s+library)?|my\s+(?:local\s+)?library"
    r")\.?\s*$",
    re.I,
)

_PROVIDER_QUALIFIED_PLAY_NEXT_PATTERN = re.compile(
    r"^\s*(?:please\s+)?(?:play|put\s+on)\s+"
    r"(?P<query>.+?)\s+next\s+"
    r"(?:on|from)\s+"
    r"(?P<provider>"
    r"spotify|youtube(?:\s+music)?|local(?:\s+files|\s+library)?|my\s+(?:local\s+)?library"
    r")\.?\s*$",
    re.I,
)


@dataclass(frozen=True)
class ProviderQualifiedPlayCommand:
    """A direct play request that names the desired music provider."""

    query: str
    provider: str
    original_provider: str
    original_text: str
    play_next: bool = False


@dataclass(frozen=True)
class ExplicitMusicControlCommand:
    """A direct music transport request that should not go through the LLM."""

    intent: str
    params: dict[str, Any]
    original_text: str


_PAUSE_MUSIC_PATTERN = re.compile(
    r"^\s*(?:please\s+)?(?:pause|hold)\s+(?:the\s+)?(?:music|song|track|audio|playback)\.?\s*$",
    re.I,
)

_RESUME_MUSIC_PATTERN = re.compile(
    r"^\s*(?:please\s+)?(?:resume|unpause|continue)\s+(?:the\s+)?(?:music|song|track|audio|playback)\.?\s*$",
    re.I,
)

_VOLUME_SET_PATTERN = re.compile(
    r"^\s*(?:please\s+)?(?:set|change)\s+(?:the\s+)?"
    r"(?:(?:music|song|track|audio|playback)\s+)?volume\s+"
    r"(?:to|at)\s+(?P<level>\d{1,3})(?:\s*(?:percent|%))?\.?\s*$",
    re.I,
)

_VOLUME_UP_PATTERN = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"volume\s+up|"
    r"turn\s+(?:the\s+)?(?:music|song|track|audio|playback)\s+up|"
    r"turn\s+up\s+(?:the\s+)?(?:music|song|track|audio|playback)|"
    r"make\s+(?:the\s+)?(?:music|song|track|audio|playback)\s+louder|"
    r"increase\s+(?:the\s+)?(?:music|song|track|audio|playback)\s+volume"
    r")\.?\s*$",
    re.I,
)

_VOLUME_DOWN_PATTERN = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"volume\s+down|"
    r"turn\s+(?:the\s+)?(?:music|song|track|audio|playback)\s+down|"
    r"turn\s+down\s+(?:the\s+)?(?:music|song|track|audio|playback)|"
    r"make\s+(?:the\s+)?(?:music|song|track|audio|playback)\s+(?:quieter|softer)|"
    r"(?:lower|decrease)\s+(?:the\s+)?(?:music|song|track|audio|playback)\s+volume"
    r")\.?\s*$",
    re.I,
)

_SKIP_MUSIC_PATTERN = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"skip|"
    r"next|"
    r"skip\s+(?:the\s+)?(?:music|song|track|audio|playback)|"
    r"next\s+(?:the\s+)?(?:music|song|track|audio|playback)|"
    r"skip\s+to\s+(?:the\s+)?next\s+(?:song|track)|"
    r"go\s+to\s+(?:the\s+)?next\s+(?:song|track)"
    r")\.?\s*$",
    re.I,
)

_SEEK_MUSIC_PATTERN = re.compile(
    r"^\s*(?:please\s+)?(?:seek|jump|go|skip)\s+(?:to\s+)?(?P<target>.+?)\.?\s*$",
    re.I,
)

_COLON_TIME_PATTERN = re.compile(r"^\d+(?::\d{1,2}){1,2}$")
_VERBAL_TIME_PATTERN = re.compile(
    r"^\s*"
    r"(?:(?P<hours>\d+)\s*(?:hours?|hrs?|hr|h)\s*)?"
    r"(?:(?P<minutes>\d+)\s*(?:minutes?|mins?|min|m)\s*)?"
    r"(?:and\s*)?"
    r"(?:(?P<seconds>\d+)\s*(?:seconds?|secs?|sec|s)\s*)?"
    r"\s*$",
    re.I,
)


def _parse_seek_target_ms(target: str) -> int | None:
    normalized = " ".join(target.strip().lower().split())
    if not normalized:
        return None

    if normalized.isdigit():
        return int(normalized) * 1000

    if _COLON_TIME_PATTERN.match(normalized):
        parts = [int(part) for part in normalized.split(":")]
        if len(parts) == 2:
            minutes, seconds = parts
            if seconds >= 60:
                return None
            return (minutes * 60 + seconds) * 1000
        if len(parts) == 3:
            hours, minutes, seconds = parts
            if minutes >= 60 or seconds >= 60:
                return None
            return (hours * 3600 + minutes * 60 + seconds) * 1000

    verbal = _VERBAL_TIME_PATTERN.match(normalized)
    if verbal:
        values = {name: int(value) if value is not None else 0 for name, value in verbal.groupdict().items()}
        if any(values.values()):
            return (values["hours"] * 3600 + values["minutes"] * 60 + values["seconds"]) * 1000

    return None


def parse_provider_qualified_play_command(text: str) -> ProviderQualifiedPlayCommand | None:
    """Parse only exact provider-qualified play commands.

    Generic requests such as ``play drake`` intentionally return ``None`` so
    they remain on Viola's model/tool path.
    """
    play_next = False
    match = _PROVIDER_QUALIFIED_PLAY_NEXT_PATTERN.match(text)
    if match:
        play_next = True
    else:
        match = _PROVIDER_QUALIFIED_PLAY_PATTERN.match(text)
    if not match:
        return None

    query = " ".join(match.group("query").strip().split())
    provider_text = " ".join(match.group("provider").strip().lower().split())
    provider = _PROVIDER_ALIASES.get(provider_text)
    if not query or provider is None:
        return None

    return ProviderQualifiedPlayCommand(
        query=query,
        provider=provider,
        original_provider=provider_text,
        original_text=text,
        play_next=play_next,
    )


def _source_hint_for_provider_qualified_command(provider: str, query: str) -> str | None:
    if provider == "local":
        return "local"
    if provider == "spotify":
        return "spotify_cdp"
    if provider in {"youtube", "youtube_music", "youtube_iframe"}:
        lowered = query.lower()
        if "youtube.com/" in lowered or "youtu.be/" in lowered:
            return "url"
        return "ytsearch1"
    return None


def _iter_music_method_targets(music: object) -> list[object]:
    targets: list[object] = []
    seen: set[int] = set()

    def add(target: object | None) -> None:
        if target is None:
            return
        ident = id(target)
        if ident in seen:
            return
        targets.append(target)
        seen.add(ident)

    add(music)
    adapter_targets = getattr(music, "_targets", None)
    if callable(adapter_targets):
        try:
            adapter_target_values = adapter_targets()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            adapter_target_values = ()
        for target in adapter_target_values:
            add(target)
    for target in list(targets):
        for attr_name in ("player", "control_surface", "_control", "_player"):
            try:
                nested_target = getattr(target, attr_name, None)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                nested_target = None
            add(nested_target)
    return targets


def _music_method(music: object, name: str) -> Any:
    for target in _iter_music_method_targets(music):
        method = getattr(target, name, None)
        if callable(method):
            return method
    return None


def parse_explicit_music_control_command(text: str) -> ExplicitMusicControlCommand | None:
    """Parse exact music-control commands that must act without model routing."""
    if _PAUSE_MUSIC_PATTERN.match(text):
        return ExplicitMusicControlCommand(intent="pause_music", params={}, original_text=text)
    if _RESUME_MUSIC_PATTERN.match(text):
        return ExplicitMusicControlCommand(intent="resume_music", params={}, original_text=text)
    volume_set = _VOLUME_SET_PATTERN.match(text)
    if volume_set:
        return ExplicitMusicControlCommand(
            intent="volume",
            params={"level": int(volume_set.group("level"))},
            original_text=text,
        )
    if _VOLUME_UP_PATTERN.match(text):
        return ExplicitMusicControlCommand(intent="volume_up", params={}, original_text=text)
    if _VOLUME_DOWN_PATTERN.match(text):
        return ExplicitMusicControlCommand(intent="volume_down", params={}, original_text=text)
    seek_match = _SEEK_MUSIC_PATTERN.match(text)
    if seek_match:
        position_ms = _parse_seek_target_ms(seek_match.group("target"))
        if position_ms is not None:
            return ExplicitMusicControlCommand(
                intent="seek",
                params={"position_ms": position_ms},
                original_text=text,
            )
    if _SKIP_MUSIC_PATTERN.match(text):
        return ExplicitMusicControlCommand(intent="skip_track", params={}, original_text=text)
    return None


async def execute_provider_qualified_play_command(
    *,
    text: str,
    music: object,
    user_id: str = "",
) -> dict[str, Any] | None:
    """Execute an explicit provider-qualified play request through CommandExecutor."""
    parsed = parse_provider_qualified_play_command(text)
    if parsed is None:
        return None

    if parsed.play_next:
        play_next_method = _music_method(music, "play_next")
        if not callable(play_next_method):
            result: dict[str, Any] = {
                "success": False,
                "message": "Music player does not support play next.",
                "data": {
                    "intent": "play_next",
                    "query": parsed.query,
                    "provider": parsed.provider,
                    "source": None,
                },
                "error": "play_next_not_supported",
            }
        else:
            source_hint = _source_hint_for_provider_qualified_command(parsed.provider, parsed.query)
            try:
                if source_hint:
                    play_next_result = play_next_method(parsed.query, source_hint)
                else:
                    play_next_result = play_next_method(parsed.query)
                if inspect.isawaitable(play_next_result):
                    play_next_result = await play_next_result
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                result = {
                    "success": False,
                    "message": str(exc) or "Failed to queue next track.",
                    "data": {
                        "intent": "play_next",
                        "query": parsed.query,
                        "provider": parsed.provider,
                        "source": source_hint,
                    },
                    "error": "play_next_failed",
                }
            else:
                enqueued: Any = play_next_result
                if hasattr(enqueued, "model_dump"):
                    enqueued = enqueued.model_dump()
                success = enqueued is not None
                if isinstance(enqueued, dict) and (enqueued.get("ok") is False or enqueued.get("success") is False):
                    success = False
                result = {
                    "success": success,
                    "message": "music_queued_next" if success else "music_queue_next_failed",
                    "data": {
                        "intent": "play_next",
                        "query": parsed.query,
                        "provider": parsed.provider,
                        "source": source_hint,
                        "enqueued": enqueued,
                    },
                    "error": None if success else "play_next_failed",
                }

        data_obj = result.get("data") if isinstance(result, dict) else None
        data = dict(data_obj) if isinstance(data_obj, dict) else {}
        data["command_bridge"] = {
            "kind": "provider_qualified_play_next",
            "query": parsed.query,
            "provider": parsed.provider,
            "original_provider": parsed.original_provider,
            "original_text": parsed.original_text,
        }

        return {
            "success": bool(result.get("success")) if isinstance(result, dict) else False,
            "intent": "play_next",
            "data": data,
            "error": result.get("error") if isinstance(result, dict) else "command_execution_failed",
        }

    from intent.command_executor import CommandExecutor

    executor = CommandExecutor(music)
    result = await executor.execute_command(
        "play_music",
        {
            "query": parsed.query,
            "provider": parsed.provider,
            "_user_id": user_id,
        },
    )

    data_obj = result.get("data") if isinstance(result, dict) else None
    data = dict(data_obj) if isinstance(data_obj, dict) else {}
    message = result.get("message") if isinstance(result, dict) else None
    if isinstance(message, str) and message:
        data.setdefault("message", message)
    data["command_bridge"] = {
        "kind": "provider_qualified_play",
        "query": parsed.query,
        "provider": parsed.provider,
        "original_provider": parsed.original_provider,
        "original_text": parsed.original_text,
    }

    return {
        "success": bool(result.get("success")) if isinstance(result, dict) else False,
        "intent": "play_music",
        "data": data,
        "error": result.get("error") if isinstance(result, dict) else "command_execution_failed",
    }


async def execute_explicit_music_control_command(
    *,
    text: str,
    music: object,
    user_id: str = "",
) -> dict[str, Any] | None:
    """Execute an explicit music transport request through CommandExecutor."""
    parsed = parse_explicit_music_control_command(text)
    if parsed is None:
        return None

    if parsed.intent == "skip_track":
        method = getattr(music, "skip", None) or getattr(music, "next", None)
        if not callable(method):
            result = {
                "success": False,
                "data": {"intent": "skip_track"},
                "error": "skip_unavailable",
            }
        else:
            try:
                skip_result = method()
                if inspect.isawaitable(skip_result):
                    skip_result = await skip_result
            except (AttributeError, InvalidOperation, RuntimeError, TypeError, ValueError) as exc:
                result = {
                    "success": False,
                    "data": {
                        "intent": "skip_track",
                        "message": str(exc) or "No next track in queue",
                    },
                    "error": "skip_unavailable",
                }
            else:
                success = True
                error: object = None
                data: dict[str, Any] = {"intent": "skip_track"}
                if isinstance(skip_result, dict):
                    success = bool(skip_result.get("ok", skip_result.get("success", True)))
                    error = skip_result.get("error")
                    data.update(skip_result)
                result = {
                    "success": success,
                    "data": data,
                    "error": error,
                }
    elif parsed.intent == "seek":
        position_ms = int(parsed.params["position_ms"])
        seek = getattr(music, "seek", None)
        if not callable(seek):
            result: dict[str, Any] = {
                "success": False,
                "data": {"intent": "seek", "position_ms": position_ms},
                "error": "seek_unavailable",
            }
        else:
            seek_result = seek(position_ms)
            success = True
            error: object = None
            if isinstance(seek_result, dict):
                success = bool(seek_result.get("ok", seek_result.get("success", True)))
                error = seek_result.get("error")
            result = {
                "success": success,
                "data": {
                    "intent": "seek",
                    "position_ms": position_ms,
                    "position_seconds": position_ms / 1000.0,
                },
                "error": error,
            }
    else:
        from intent.command_executor import CommandExecutor

        executor = CommandExecutor(music)
        params = dict(parsed.params)
        if user_id:
            params["_user_id"] = user_id
        result = await executor.execute_command(parsed.intent, params)

    data_obj = result.get("data") if isinstance(result, dict) else None
    data = dict(data_obj) if isinstance(data_obj, dict) else {}
    message = result.get("message") if isinstance(result, dict) else None
    if isinstance(message, str) and message:
        data.setdefault("message", message)
    data["command_bridge"] = {
        "kind": "music_control",
        "control": parsed.intent,
        "original_text": parsed.original_text,
    }

    return {
        "success": bool(result.get("success")) if isinstance(result, dict) else False,
        "intent": parsed.intent,
        "data": data,
        "error": result.get("error") if isinstance(result, dict) else "command_execution_failed",
    }
