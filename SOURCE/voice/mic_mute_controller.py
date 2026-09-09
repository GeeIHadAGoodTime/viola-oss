"""Mic-mute control: the single place that flips SettingsManager's
``mic_muted`` flag and hard-gates the wake/STT pipeline to match.

Both the mute hotkey (React -> POST /v1/settings, applied in
``ui/settings_api.py``) and the tray "Mute Microphone" toggle
(``ui/qt_native/webview_window.py``) call into this module so there is
exactly one mute code path instead of two copies of the same
persist-then-gate logic. Mirrors the voice_mode -> WakeDetectorFacade
toggle pattern (Bug 34.9 / 34.10) via ``WakeDetectorFacade.sync_to_state``.
"""

from __future__ import annotations

from core.logging_config import get_logger

logger = get_logger(__name__)


def set_mic_muted(muted: bool, *, user_id: str | None = None) -> bool:
    """Persist ``mic_muted`` via SettingsManager and hard-gate the wake
    detector to match.

    Callers that already went through the settings-mutation HTTP endpoint
    (``ui/settings_api.py``) should NOT call this — it would persist twice.
    Use this for direct, in-process callers such as the system tray toggle.

    Returns the persisted value.
    """
    from ui.settings_manager import get_settings_manager
    from voice.wake_detector.facade import WakeDetectorFacade

    settings_mgr = get_settings_manager()
    muted = bool(muted)
    settings_mgr.update({"mic_muted": muted}, save_immediately=True, user_id=user_id)
    voice_mode = settings_mgr.get("voice_mode", "push_to_talk")
    WakeDetectorFacade.sync_to_state(voice_mode, muted)
    logger.info("Mic muted set to %s (voice_mode=%s)", muted, voice_mode)
    return muted


def toggle_mic_muted(*, user_id: str | None = None) -> bool:
    """Flip ``mic_muted`` and return the new value."""
    from ui.settings_manager import get_settings_manager

    settings_mgr = get_settings_manager()
    current = bool(settings_mgr.get("mic_muted", False))
    return set_mic_muted(not current, user_id=user_id)
