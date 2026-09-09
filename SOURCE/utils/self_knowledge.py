"""Self-knowledge context builder for LLM system prompt injection.

Assembles a minimal snapshot of Viola's current state so the LLM has
real-time awareness of essential context on every invocation.

Keeps only what the model *needs* to answer correctly and cannot discover
via tools:
- Current date/time (needed for relative time references)
- User identity (personalization)
- Playback summary (one line: playing/paused + track)
- Active music player volume (only while playback is active, to disambiguate
  player volume from OS/desktop volume)
- Active timers (user expects status awareness)
- User preferences (weather location, locale)

Removed sections (noise the model either doesn't need or can discover
from tool descriptions/results): queue listing, shuffle/repeat/
autoplay, provider, playlists, liked songs, multiroom details,
capabilities, uptime, recent interactions, settings diff, memories
(the detailed memory context in ai_controller is the real one).

Usage::

    from utils.self_knowledge import get_self_knowledge_builder

    builder = get_self_knowledge_builder()
    context = builder.build()
    # -> compact multi-line string, ~40-80 tokens
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Text sanitisation (extracted from the legacy ContextBuilder)
# ---------------------------------------------------------------------------


def sanitize_for_prompt(text: str, max_len: int = 80) -> str:
    """Sanitize user-provided text for safe inclusion in LLM prompts.

    Removes control characters, escapes quotes, collapses whitespace, and
    truncates to *max_len* characters.
    """
    if not text or not isinstance(text, str):
        return ""
    sanitized = "".join(c for c in text if c.isprintable() or c in " \t")
    sanitized = sanitized.replace('"', "'").replace("\n", " ").replace("\r", " ")
    sanitized = " ".join(sanitized.split())
    return sanitized[:max_len].strip()


# ---------------------------------------------------------------------------
# SelfKnowledgeBuilder
# ---------------------------------------------------------------------------


class SelfKnowledgeBuilder:
    """Assembles a minimal snapshot of Viola's state for LLM prompts.

    Read-only -- never modifies state.  Invoked on every LLM call.
    Uses a 2-second TTL cache for volatile state to stay within the
    <5 ms latency budget.
    """

    _CACHE_TTL: float = 2.0
    _MAX_OUTPUT_CHARS: int = 800

    def __init__(self) -> None:
        self._cached_output: dict[str, str] = {}
        self._cache_times: dict[str, float] = {}
        # Optional MCP hub reference (kept for backward compat, unused now)
        self._mcp_hub: Any = None
        # Optional music player reference for direct state reads
        self._music_player: Any = None
        # Per-build snapshot of player state (avoids repeated lock acquisitions)
        self._current_player_state: Any = None
        # Per-build user context (set at start of build(), cleared at end)
        self._current_user_name: str = ""
        self._current_user_key: str = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_mcp_hub(self, hub: Any) -> None:
        """Register the MCP hub (kept for backward compat)."""
        self._mcp_hub = hub

    def set_music_player(self, player: Any) -> None:
        """Register the music player for direct state reads.

        Accepts either a raw MusicPlayer or a MusicControllerAdapter
        (which wraps the player as ``.player``).
        """
        if hasattr(player, "player") and not hasattr(player, "_playback_sm"):
            self._music_player = player.player
        else:
            self._music_player = player

    def build(self, force_refresh: bool = False, user_name: str = "", user_key: str = "") -> str:
        """Return the self-knowledge context string.

        Cached for ``_CACHE_TTL`` seconds per user_key.
        """
        now = time.monotonic()
        cache_key = user_key or "_default"
        if not force_refresh and cache_key in self._cached_output:
            if (now - self._cache_times.get(cache_key, 0)) < self._CACHE_TTL:
                return self._cached_output[cache_key]

        self._current_user_name = user_name
        self._current_user_key = user_key
        self._current_player_state = self._get_player_state_direct()

        lines: list[str] = []

        self._add_datetime(lines)
        self._add_user_identity(lines)
        self._add_playback(lines)
        self._add_timers(lines)
        self._add_user_preferences(lines)
        self._add_capability_context(lines)

        output = "\n".join(lines)
        if len(output) > self._MAX_OUTPUT_CHARS:
            output = output[: self._MAX_OUTPUT_CHARS]

        self._current_player_state = None
        self._current_user_name = ""
        self._current_user_key = ""

        self._cached_output[cache_key] = output
        self._cache_times[cache_key] = now
        return output

    # ------------------------------------------------------------------
    # Section builders (each wrapped in try/except)
    # ------------------------------------------------------------------

    def _add_datetime(self, lines: list[str]) -> None:
        try:
            # The user's own wall clock, not the server's. A cloud container
            # runs in UTC, so telling the model "it is 8pm UTC" made it resolve
            # "tomorrow" against the wrong calendar day for anyone west of
            # Greenwich (#3557). Falls back to process-local when no per-user
            # zone is bound, which is what desktop has always used.
            from services.user_timezone import active_timezone

            aware = datetime.now(tz=active_timezone())
            now = aware.replace(tzinfo=None)
            lines.append("Date: %s" % now.strftime("%A, %B %d, %Y"))
            utc_offset = aware.strftime("%z")
            offset_str = "UTC%s%s:%s" % (
                utc_offset[0],
                utc_offset[1:3],
                utc_offset[3:5],
            )
            lines.append(
                "Local time: %s (%s, %s)"
                % (
                    now.strftime("%I:%M %p").lstrip("0"),
                    aware.strftime("%Z"),
                    offset_str,
                )
            )
        except Exception as exc:
            logger.debug("Self-knowledge: datetime section failed: %s", exc)

    def _get_player_state_direct(self) -> Any | None:
        """Read playback state directly from the music player.

        Returns a lightweight namespace with is_playing and now_playing,
        or None if the player is unavailable.
        """
        player = self._music_player
        if player is None:
            return None
        try:
            _sm = getattr(player, "_playback_sm", None)
            if _sm is not None:
                is_playing = _sm.is_playing
            else:
                is_playing = getattr(player, "_is_playing", False) and not getattr(player, "_paused", False)

            _svc_state = getattr(player, "_state", None)
            now_playing = getattr(_svc_state, "now_playing", None) if _svc_state is not None else None

            volume = None
            if _svc_state is not None:
                volume = getattr(_svc_state, "volume", None)
            if volume is None:
                volume = getattr(player, "_volume", None)

            class _Snapshot:
                __slots__ = ("is_playing", "now_playing", "volume")

            snap = _Snapshot()
            snap.is_playing = is_playing
            snap.now_playing = now_playing
            snap.volume = volume
            return snap
        except Exception as exc:
            logger.warning("Direct player attribute read failed: %s", exc)
        return None

    def _add_playback(self, lines: list[str]) -> None:
        """One-line playback summary: status + track name."""
        try:
            ps = self._current_player_state
            if ps is not None:
                is_playing = getattr(ps, "is_playing", False)
                track = getattr(ps, "now_playing", None)
            else:
                from core.state_selectors import (
                    select_is_playing,
                    select_now_playing,
                )

                is_playing = select_is_playing()
                track = select_now_playing()

            if not track or not getattr(track, "title", None):
                lines.append("Playback: stopped")
                return

            title = sanitize_for_prompt(str(track.title), max_len=60)
            artist = sanitize_for_prompt(str(getattr(track, "artist", "") or ""), max_len=40)

            status = "playing" if is_playing else "paused"
            track_part = '"%s"' % title
            if artist:
                track_part += " by %s" % artist

            lines.append("Playback: %s %s" % (status, track_part))
            volume = getattr(ps, "volume", None)
            if is_playing and isinstance(volume, (int, float)):
                lines.append(
                    "Music player volume: %d%% (separate from system/OS output volume)" % max(0, min(100, int(volume)))
                )
        except Exception as exc:
            logger.debug("Self-knowledge: playback section failed: %s", exc)
            lines.append("Playback: stopped")

    def _add_user_identity(self, lines: list[str]) -> None:
        """Inject current user identity if available."""
        if self._current_user_name:
            lines.append("Speaking with: %s" % self._current_user_name)

    def _add_timers(self, lines: list[str]) -> None:
        try:
            from core.state_selectors import get_active_timers

            timers = get_active_timers()
            if not timers:
                return
            parts = []
            for t in timers:
                remaining = t.get("remaining_seconds", 0)
                name = t.get("name", "timer")
                mins, secs = divmod(int(remaining), 60)
                parts.append("%s: %d:%02d remaining" % (name, mins, secs))
            lines.append("Timers: %s" % "; ".join(parts))
        except Exception as exc:
            logger.debug("Self-knowledge: timers section failed: %s", exc)

    def _add_user_preferences(self, lines: list[str]) -> None:
        """Inject user preferences from SettingsManager."""
        try:
            from ui.settings_manager import get_settings_manager

            sm = get_settings_manager()
        except Exception:
            return

        prefs = []
        weather = sm.get("weather_location", "")
        if weather:
            prefs.append("weather location: %s" % weather)

        provider = sm.get("active_music_provider_id")
        if provider:
            prefs.append("music provider: %s" % provider)

        locale_val = sm.get("locale", "")
        if locale_val and locale_val != "auto":
            prefs.append("locale: %s" % locale_val)

        if prefs:
            lines.append("User preferences: %s" % ", ".join(prefs))

    def _add_memory_archive_notice(self, lines: list[str], user_id: str) -> None:
        """Surface a transient one-shot mention when memory was recently archived.

        Pattern matches the rest of self_knowledge: emit live state, let the LLM
        decide whether/how to mention it. Notice auto-expires after the TTL in
        services.memory.dir._ARCHIVE_NOTICE_TTL_SECONDS.
        """
        try:
            from services.memory.dir import get_recent_archive_notice

            notice = get_recent_archive_notice(user_id)
        except Exception as exc:
            logger.debug("Self-knowledge: archive notice lookup failed: %s", exc)
            return
        if not notice:
            return
        try:
            count = int(notice.get("event_count", 0))
            total_bytes = int(notice.get("total_bytes", 0))
            if count <= 0:
                return
            noun = "older memory entries were" if count > 1 else "an older memory entry was"
            lines.append(
                "Memory note: %s archived recently (%d bytes moved to memory/archive/). "
                "Mention casually only if relevant — the user may not yet know." % (noun, total_bytes)
            )
        except Exception as exc:
            logger.debug("Self-knowledge: archive notice formatting failed: %s", exc)

    def _add_capability_context(self, lines: list[str]) -> None:
        """Inject current autonomy tier and saved shortcuts for this user."""
        try:
            from core.user_context import get_current_user_id

            user_id = get_current_user_id()
        except LookupError:
            return
        except Exception as exc:
            logger.debug("Self-knowledge: capability user context failed: %s", exc)
            return

        self._add_memory_archive_notice(lines, user_id)

        try:
            from services.user_capabilities import list_for_user
            from services.user_capabilities.tiers import TIER_DESCRIPTIONS
            from ui.settings_manager import get_settings_manager

            sm = get_settings_manager()
            tier = str(sm.get("agent_autonomy", "ensemble", user_id=user_id) or "ensemble").strip().lower()
            description = TIER_DESCRIPTIONS.get(tier, TIER_DESCRIPTIONS["ensemble"])
            # Frame the tier as internal permission state so the model does not
            # surface the raw label/value to users as their "plan" -- when asked
            # "what plan am I on" the model was echoing "capability tier: symphony"
            # verbatim (issue #1406). This is descriptive self-knowledge, not a
            # per-query hint.
            lines.append(
                "Tool-permission level (internal setting, not the user's billing plan or subscription): %s -- %s"
                % (tier, description)
            )
            if bool(sm.get("auto_propose_shortcuts", False, user_id=user_id)):
                lines.append(
                    "Proactive shortcut proposals are enabled — feel free to offer shortcut creation "
                    "when patterns recur."
                )
            # Extension paths: remind the agent she can grow her own toolbox.
            # Discovery flows through tool descriptions; this line is the live
            # state-side reinforcement so missing-capability scenarios surface
            # a propose-install response rather than a dead-end refusal.
            lines.append(
                "Extension paths: mcp_servers (add MCP tool servers), "
                "self_manage install_package (pip dependency), "
                "user_capabilities (create user shortcuts)."
            )
            capabilities = list_for_user(user_id, user_tier=tier)
        except Exception as exc:
            logger.debug("Self-knowledge: capability section failed: %s", exc)
            return

        if not capabilities:
            return

        parts: list[str] = []
        for capability in capabilities:
            if capability.get("disabled"):
                continue
            if not capability.get("runnable_on_current_surface", True):
                continue
            trigger = capability.get("trigger")
            phrases = trigger.get("phrases", []) if isinstance(trigger, dict) else []
            if not phrases:
                continue
            name = sanitize_for_prompt(str(capability.get("name", "")), max_len=40)
            if not capability.get("runnable", True):
                required_tier = sanitize_for_prompt(str(capability.get("required_tier", "higher tier")), max_len=20)
                name = "%s (needs %s)" % (name, required_tier)
            phrase = sanitize_for_prompt(phrases[0], max_len=60)
            if name and phrase:
                parts.append('%s ("%s")' % (name, phrase))
            if len(parts) >= 5:
                break
        if parts:
            lines.append("Custom shortcuts: %s" % "; ".join(parts))


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_builder: SelfKnowledgeBuilder | None = None


def get_self_knowledge_builder() -> SelfKnowledgeBuilder:
    """Get or create the module-level SelfKnowledgeBuilder singleton."""
    global _builder
    if _builder is None:
        _builder = SelfKnowledgeBuilder()
    return _builder
