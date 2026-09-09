"""
Proactive post-action suggestion engine.

After certain commands, Viola can optionally append a helpful follow-up
suggestion -- like a real human assistant would. Suggestions are short
(under 10 words), TTS-friendly, and rate-limited to avoid being annoying.

Additionally, the engine provides *contextual* (proactive) suggestions
when the UI polls without a specific command type -- based on time of day,
playback state, idle duration, last command outcome, and user preferences.

Design rules:
- Rate limit: ~20% of eligible interactions show a suggestion.
- "First time in session" tracking: some suggestions only fire once per
  session to teach a feature without repeating it.
- No suggestions on busy/time-sensitive commands (timers, reminders).
- All suggestion strings must be under 10 words and TTS-speakable.
- Contextual suggestions guarantee at least one result when candidates exist.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime

from core.logging_config import get_logger

logger = get_logger(__name__)

# -------------------------------------------------------------------------
# Scoped session state -- process lifetime, partitioned by user/session
# -------------------------------------------------------------------------


@dataclass
class _SuggestionState:
    seen_command_types: set[str] = field(default_factory=set)
    last_command_type: str = ""
    last_command_ok: bool = True
    last_command_time: float = 0.0
    session_start_time: float = field(default_factory=time.monotonic)
    seen_contextual: set[str] = field(default_factory=set)
    user_model_cache: dict[str, object] | None = None
    user_model_loaded_at: float = 0.0


_states_by_user: dict[str, _SuggestionState] = {}
_USER_MODEL_TTL: float = 300.0  # 5 minutes


def _state_key(user_id: str | None = None) -> str:
    return str(user_id or "").strip()


def _state_for(user_id: str | None = None) -> _SuggestionState:
    key = _state_key(user_id)
    state = _states_by_user.get(key)
    if state is None:
        state = _SuggestionState()
        _states_by_user[key] = state
    return state


def reset_session_state(user_id: str | None = None) -> None:
    """Reset seen-command tracking (used in tests and on clean restart)."""
    if user_id is None:
        _states_by_user.clear()
        return
    _states_by_user.pop(_state_key(user_id), None)


def record_command(command_type: str, ok: bool = True, *, user_id: str | None = None) -> None:
    """Record the last command for contextual suggestion generation.

    Called from the intent pipeline after each command completes.

    Args:
        command_type: The intent / command name from PipelineResult.intent.
        ok: Whether the command succeeded.
    """
    state = _state_for(user_id)
    state.last_command_type = command_type or ""
    state.last_command_ok = ok
    state.last_command_time = time.monotonic()
    logger.debug(
        "Suggestion engine: recorded command_type=%s ok=%s user_id=%s",
        command_type,
        ok,
        _state_key(user_id) or "<none>",
    )


# -------------------------------------------------------------------------
# User model loader
# -------------------------------------------------------------------------


def _load_user_model(user_id: str | None = None) -> dict[str, object]:
    """Load an explicit user's model data, with per-user caching."""
    uid = _state_key(user_id)
    if not uid:
        return {}
    state = _state_for(uid)
    now = time.monotonic()
    if state.user_model_cache is not None and (now - state.user_model_loaded_at) < _USER_MODEL_TTL:
        return state.user_model_cache

    try:
        from services.user_model.profile import get_user_model

        data = get_user_model(user_id=uid).to_dict()
        if not isinstance(data, dict):
            data = {}
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        logger.debug("Failed to load user model for suggestion user_id=%s", uid, exc_info=True)
        data = {}

    state.user_model_cache = data
    state.user_model_loaded_at = now
    return data


def _get_user_name(user_id: str | None = None) -> str:
    """Get user's name from user model, or empty string."""
    model = _load_user_model(user_id)
    facts = model.get("facts", {})
    if isinstance(facts, dict):
        return str(facts.get("name", ""))
    return ""


def _get_user_music_pref(user_id: str | None = None) -> str:
    """Get user's preferred music genre/type from user model."""
    model = _load_user_model(user_id)
    prefs = model.get("preferences", {})
    if isinstance(prefs, dict):
        music = prefs.get("music", {})
        if isinstance(music, dict):
            return str(music.get("genre", "") or music.get("type", ""))
    return ""


def _get_user_location(user_id: str | None = None) -> str:
    """Get user's location for weather suggestions."""
    model = _load_user_model(user_id)
    prefs = model.get("preferences", {})
    if isinstance(prefs, dict):
        loc = prefs.get("location", {})
        if isinstance(loc, dict):
            return str(loc.get("briefing_city", ""))
    return ""


# -------------------------------------------------------------------------
# Suggestion table
#
# Each entry is:
#   command_type (str)  -- matches PipelineResult.intent or a logical group
#   rate (float)        -- probability [0, 1] that the suggestion fires
#   first_only (bool)   -- only fire once per session if True
#   text (str)          -- the suggestion to append (must be < 10 words)
# -------------------------------------------------------------------------

_SUGGESTION_TABLE: list[dict[str, object]] = [
    # Play music -- teach thumbs-up on first play of the session
    {
        "command_types": {
            "play_default_playlist",
            "play_my_favorites",
            "play_playlist",
            "play_music",
            "play",
        },
        "rate": 1.0,  # always show on first play; gated by first_only
        "first_only": True,
        "session_key": "play_music",
        "text": "Like it? Say thumbs up to save it.",
        "action": "thumbs up",
    },
    # "What's playing?" -- remind user about thumbs-up
    {
        "command_types": {"what_playing"},
        "rate": 0.30,
        "first_only": False,
        "session_key": None,
        "text": "Say thumbs up to save it.",
        "action": "thumbs up",
    },
    # Morning briefing -- natural next step
    {
        "command_types": {"morning_briefing"},
        "rate": 0.30,
        "first_only": False,
        "session_key": None,
        "text": "Want me to play some music?",
        "action": "play some music",
    },
    # Weather check -- teach recurring checks (low rate, not first-only)
    {
        "command_types": {"get_weather"},
        "rate": 0.15,
        "first_only": False,
        "session_key": None,
        "text": "Want me to check tomorrow too?",
        "action": "what's the weather tomorrow",
    },
    # Knowledge / AI answers -- encourage follow-up conversation (Tier 1)
    {
        "command_types": {"answer", "ai_answer"},
        "rate": 0.20,
        "first_only": False,
        "session_key": None,
        "text": "Anything else about that?",
        "action": "tell me more",
    },
    # Skip / next track -- offer queue info
    {
        "command_types": {"skip_track", "next_track", "skip"},
        "rate": 0.25,
        "first_only": True,
        "session_key": "skip_info",
        "text": "Ask what's in the queue to preview.",
        "action": "what's in the queue",
    },
    # Pause / resume -- offer stop or continue
    {
        "command_types": {"pause_music", "pause"},
        "rate": 0.20,
        "first_only": True,
        "session_key": "pause_info",
        "text": "Say resume when you're ready.",
        "action": "resume",
    },
    # Volume change -- teach range
    {
        "command_types": {"volume_up", "volume_down", "set_volume"},
        "rate": 0.15,
        "first_only": True,
        "session_key": "volume_info",
        "text": "You can also say a specific number.",
        "action": "set volume to 50",
    },
    # Web search -- encourage refinement
    {
        "command_types": {"web_search", "search"},
        "rate": 0.20,
        "first_only": False,
        "session_key": None,
        "text": "Want me to search for something else?",
        "action": "search for something else",
    },
    # System info
    {
        "command_types": {"system_info", "system_status"},
        "rate": 0.25,
        "first_only": True,
        "session_key": "system_info",
        "text": "I can also control system settings.",
        "action": "what can you do",
    },
    # Timers and reminders -- user is busy, NO suggestion (intentionally absent)
    # set_timer, set_reminder, set_alarm -> no entry = no suggestion
]

# Build a lookup: command_type -> list of matching suggestion entries
_LOOKUP: dict[str, list[dict[str, object]]] = {}
for _entry in _SUGGESTION_TABLE:
    for _ct in _entry["command_types"]:  # type: ignore[union-attr]
        _LOOKUP.setdefault(_ct, []).append(_entry)


def maybe_suggest_followup(
    command_type: str,
    response: dict[str, object] | None = None,
    context: dict[str, object] | None = None,
    *,
    user_id: str | None = None,
    _rng: random.Random | None = None,
) -> dict[str, str | None] | None:
    """
    Return an optional follow-up suggestion for the given command.

    Args:
        command_type: The intent / command name from PipelineResult.intent.
        response:     The handler response dict (currently unused; reserved for
                      future context-aware suggestions).
        context:      Optional extra context dict (reserved for future use).
        user_id:      Authenticated user owner. When absent, only generic
                      suggestions are generated and no profile is read.
        _rng:         Seeded Random instance (for deterministic testing only).
                      Callers should NEVER pass this in production.

    Returns:
        A dict with ``text`` (display label) and ``action`` (command to execute
        when clicked), or None.
    """
    if not command_type:
        return None

    del response, context

    entries = _LOOKUP.get(command_type)
    if not entries:
        return None

    state = _state_for(user_id)
    rng = _rng if _rng is not None else random

    for entry in entries:
        session_key = entry.get("session_key")
        first_only = bool(entry.get("first_only", False))
        rate = float(entry.get("rate", 0.20))
        text = str(entry.get("text", ""))
        action = entry.get("action")

        # first_only: skip if we already showed this suggestion this session
        if first_only and session_key and session_key in state.seen_command_types:
            continue

        # Rate limiting
        if rng.random() >= rate:
            continue

        # Mark as seen so first_only suggestions don't repeat
        if first_only and session_key:
            state.seen_command_types.add(session_key)

        logger.debug(
            "Suggestion engine: appending suggestion for command_type=%s user_id=%s",
            command_type,
            _state_key(user_id) or "<none>",
        )
        return {"text": text, "action": str(action) if action else text}

    return None


# -------------------------------------------------------------------------
# Contextual / proactive suggestions
#
# These are served when the UI polls GET /v1/suggestions without a specific
# command_type -- providing ambient, context-aware suggestions based on
# time of day, playback state, idle time, last command outcome, and user
# preferences from the user model.
# -------------------------------------------------------------------------


def _time_of_day_period() -> str:
    """Return a coarse time-of-day bucket: morning, afternoon, evening, night."""
    hour = datetime.now().hour
    if 5 <= hour < 12:
        return "morning"
    elif 12 <= hour < 17:
        return "afternoon"
    elif 17 <= hour < 21:
        return "evening"
    else:
        return "night"


def _is_playing() -> bool:
    """Check if music is currently playing (safe import)."""
    try:
        from core.state_selectors import select_is_playing

        return select_is_playing()
    except Exception:
        return False


def _has_active_timers() -> bool:
    """Check if any timers are active (safe import)."""
    try:
        from core.state_selectors import get_active_timers

        timers = get_active_timers()
        return len(timers) > 0
    except Exception:
        return False


def _seconds_since_last_command(state: _SuggestionState) -> float:
    """Seconds since the last recorded command, or since session start."""
    if state.last_command_time > 0:
        return time.monotonic() - state.last_command_time
    return time.monotonic() - state.session_start_time


def get_contextual_suggestion(
    *,
    user_id: str | None = None,
    _rng: random.Random | None = None,
    _now: datetime | None = None,
) -> dict[str, str | None] | None:
    """Return a proactive contextual suggestion based on current state.

    This is called when the UI polls without a specific command_type.
    It considers: time of day, playback state, idle time, last command
    outcome, user preferences, and session history.

    The function guarantees that if any candidates are generated, at least
    one will be returned (fallback selection after weighted random pass).

    Args:
        user_id: Authenticated user id. When absent, only generic suggestions
                 are generated and no profile or learned model is read.
        _rng: Seeded Random for deterministic testing. Do not use in prod.
        _now: Override current time for testing. Do not use in prod.

    Returns:
        A dict with ``text`` (display label) and ``action`` (command to
        execute when clicked), or None.
    """
    state = _state_for(user_id)
    rng = _rng if _rng is not None else random
    now = _now or datetime.now()
    period = _time_of_day_period() if _now is None else _get_period_from_dt(now)
    idle_seconds = _seconds_since_last_command(state)
    playing = _is_playing()

    # Build a weighted list of candidate suggestions based on context
    candidates: list[tuple[str, str, float, str]] = []
    # Each tuple: (category, text, weight, action)

    # --- Error recovery suggestions ---
    if not state.last_command_ok and state.last_command_type and idle_seconds < 120:
        candidates.append(
            (
                "error_recovery",
                "That didn't work. Want to try again?",
                0.80,
                state.last_command_type,
            )
        )

    # --- Post-music suggestions (playing right now) ---
    if playing:
        if "playlist_queue" not in state.seen_contextual:
            candidates.append(
                (
                    "playlist_queue",
                    "Want to add this to a playlist?",
                    0.40,
                    "add this to a playlist",
                )
            )
        if "thumbs_up_remind" not in state.seen_contextual:
            candidates.append(
                (
                    "thumbs_up_remind",
                    "Say thumbs up to save this track.",
                    0.35,
                    "thumbs up",
                )
            )

    # --- Time-of-day greetings (always available when no recent command) ---
    if idle_seconds > 10 or not state.last_command_type:
        user_name = _get_user_name(user_id)
        greeting_name = (" %s!" % user_name) if user_name else "!"

        if period == "morning" and "morning_greeting" not in state.seen_contextual:
            candidates.append(
                (
                    "morning_greeting",
                    "Good morning%s Want a briefing or weather?" % greeting_name,
                    0.90,
                    "morning briefing",
                )
            )
        elif period == "afternoon" and "afternoon_greeting" not in state.seen_contextual:
            genre = _get_user_music_pref(user_id)
            if genre:
                candidates.append(
                    (
                        "afternoon_greeting",
                        "Want to hear some %s?" % genre,
                        0.80,
                        "play %s" % genre,
                    )
                )
            else:
                candidates.append(
                    (
                        "afternoon_greeting",
                        "Good afternoon%s Want to play some music?" % greeting_name,
                        0.80,
                        "play some music",
                    )
                )
        elif period == "evening" and "evening_greeting" not in state.seen_contextual:
            candidates.append(
                (
                    "evening_greeting",
                    "Good evening%s Want some relaxing music?" % greeting_name,
                    0.80,
                    "play relaxing music",
                )
            )
        elif period == "night" and "night_greeting" not in state.seen_contextual:
            candidates.append(
                (
                    "night_greeting",
                    "Need a timer or some ambient sounds?",
                    0.70,
                    "play ambient sounds",
                )
            )

    # --- Timer check suggestions ---
    if _has_active_timers() and "timer_check" not in state.seen_contextual:
        candidates.append(
            (
                "timer_check",
                "You have active timers. Want a status?",
                0.50,
                "timer status",
            )
        )

    # --- Timer completion follow-ups ---
    if state.last_command_type in ("timer_fired", "timer_complete", "timer_done"):
        if idle_seconds < 60:
            candidates.append(
                (
                    "timer_followup",
                    "Timer done! Need to set another?",
                    0.70,
                    "set a timer",
                )
            )

    # --- User preference-based suggestions ---
    if not playing and idle_seconds > 15:
        genre = _get_user_music_pref(user_id)
        if genre and "pref_music" not in state.seen_contextual:
            candidates.append(
                (
                    "pref_music",
                    "Want me to play some %s?" % genre,
                    0.50,
                    "play %s" % genre,
                )
            )

        location = _get_user_location(user_id)
        if location and "pref_weather" not in state.seen_contextual:
            city = location.split(",")[0].strip()
            candidates.append(
                (
                    "pref_weather",
                    "Want the weather for %s?" % city,
                    0.40,
                    "weather for %s" % city,
                )
            )

    # --- Idle state suggestions (no recent commands) ---
    if idle_seconds > 60 and not playing:
        if "idle_capabilities" not in state.seen_contextual:
            candidates.append(
                (
                    "idle_capabilities",
                    "Try music, timers, weather, or web search.",
                    0.60,
                    "what can you do",
                )
            )
        elif "idle_music" not in state.seen_contextual:
            candidates.append(
                (
                    "idle_music",
                    "Want me to play something?",
                    0.50,
                    "play something",
                )
            )

    # --- Post-command contextual nudges ---
    if state.last_command_type and idle_seconds < 90:
        music_intents = {
            "play_default_playlist",
            "play_my_favorites",
            "play_playlist",
            "play_music",
            "play",
            "resume_music",
            "resume",
        }
        if state.last_command_type in music_intents and state.last_command_ok:
            if "post_music_queue" not in state.seen_contextual:
                candidates.append(
                    (
                        "post_music_queue",
                        "Ask what's playing for track info.",
                        0.35,
                        "what's playing",
                    )
                )

        if state.last_command_type == "get_weather" and state.last_command_ok:
            if "post_weather_briefing" not in state.seen_contextual:
                candidates.append(
                    (
                        "post_weather_briefing",
                        "Want a full morning briefing?",
                        0.40,
                        "morning briefing",
                    )
                )

    if not candidates:
        return None

    # --- Weighted random selection with guaranteed fallback ---
    # First pass: try weighted random (respects rate limits)
    for category, text, weight, action in candidates:
        if rng.random() < weight:
            state.seen_contextual.add(category)
            logger.debug(
                "Suggestion engine: contextual suggestion category=%s user_id=%s",
                category,
                _state_key(user_id) or "<none>",
            )
            return {"text": text, "action": action}

    # Fallback: if all random checks failed, pick the highest-weight candidate.
    # This guarantees that when we HAVE candidates, we return one.
    best_category, best_text, _best_weight, best_action = max(candidates, key=lambda c: c[2])
    state.seen_contextual.add(best_category)
    logger.debug(
        "Suggestion engine: fallback contextual suggestion category=%s user_id=%s",
        best_category,
        _state_key(user_id) or "<none>",
    )
    return {"text": best_text, "action": best_action}


def _get_period_from_dt(dt: datetime) -> str:
    """Return time-of-day period from a datetime (for testing)."""
    hour = dt.hour
    if 5 <= hour < 12:
        return "morning"
    elif 12 <= hour < 17:
        return "afternoon"
    elif 17 <= hour < 21:
        return "evening"
    else:
        return "night"
