"""The user's own settings for whether Viola speaks on this machine.

One reader for the three settings every locally spoken line checks, so the typed
reply (``ui/api/routes/command.py``) and the line that says a background task
has finished (``voice/agent_finish_speech.py``) cannot drift apart.

Every default is passed from ``config.defaults`` rather than written as a
literal, and that is load-bearing rather than tidiness. All three keys are
user-scoped (``ui/settings_schema.py``), and for a user-scoped read with a real
signed-in principal ``SettingsManager.get`` resolves through
``get_user_setting`` -> ``_read_setting_from_blob``, which returns THE CALLER'S
default when the user's blob has no row for the key -- it never consults
``DEFAULT_SETTINGS``. So a literal here silently outranks the shipped default
for exactly the users whose blob is real: a hard-coded ``False`` left every
signed-in install mute while a signed-out one spoke, and only the signed-out
case (a device pseudo-identity, which does fall back to the global blob) would
show up in a cold-install test.
"""

from __future__ import annotations

from dataclasses import dataclass

# The channels whose reply is spoken on this machine when the user asks for it:
# the desktop's own command box and web chat (``/v1/command``, "http"/"web")
# and saved Chat tab threads ("chat_mode"). A voice turn speaks through the
# voice handler, and every other known channel (Telegram, phone, email...) is
# remote: what it starts is never said out loud on the PC.
LOCALLY_SPOKEN_CHANNELS = frozenset({"http", "web", "chat_mode"})


def is_locally_spoken_channel(key: str) -> bool:
    """Whether a reply on this channel key may be spoken on this machine."""
    return str(key or "").strip().lower() in LOCALLY_SPOKEN_CHANNELS


@dataclass(frozen=True, slots=True)
class ReplySpeechSettings:
    """The user's speech switches, read once."""

    speak_all_replies: bool
    voice_muted: bool
    tts_enabled: bool

    @property
    def voice_allowed(self) -> bool:
        """Speech is on and not muted (what a reply to a spoken request needs)."""
        return self.tts_enabled and not self.voice_muted

    @property
    def typed_allowed(self) -> bool:
        """Also "Speak Responses Aloud" (what a reply to a typed request needs)."""
        return self.voice_allowed and self.speak_all_replies


def read_reply_speech_settings(user_id: str) -> ReplySpeechSettings:
    """Read ``speak_all_replies``, ``voice_muted`` and ``tts_enabled`` for this user."""
    from config.defaults import (
        SPEAK_ALL_REPLIES_DEFAULT,
        TTS_ENABLED_DEFAULT,
        VOICE_MUTED_DEFAULT,
    )
    from ui.settings_manager import get_settings_manager

    settings_mgr = get_settings_manager()
    return ReplySpeechSettings(
        speak_all_replies=bool(settings_mgr.get("speak_all_replies", SPEAK_ALL_REPLIES_DEFAULT, user_id=user_id)),
        voice_muted=bool(settings_mgr.get("voice_muted", VOICE_MUTED_DEFAULT, user_id=user_id)),
        tts_enabled=bool(settings_mgr.get("tts_enabled", TTS_ENABLED_DEFAULT, user_id=user_id)),
    )
