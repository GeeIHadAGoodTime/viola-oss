"""What the user gets when a timer runs out: the alarm sound, every time.

A timer used to end in one of two ways depending on how the model had set it:
the alarm tool rang, the timer tool just said "Your 2 min timer is done!" and
was over (1.0.5 build-9 laptop report). Jay's rule: a timer ending is always the
sound. It rings through the same ringer as an alarm, with the same Stop card and
the same "stop", so nobody has to learn two things. Speech is only the fallback
for a process that cannot ring at all.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


def timer_message(label: str) -> str:
    return f"Your {label} is done!" if label else "Your timer is done!"


def ring_for_timer(user_id: str, timer_id: str, label: str) -> bool:
    """Ring the alarm sound for an expired timer. Returns whether it started.

    Desktop only: the cloud surface has no speaker. Under pytest the real
    speaker is never opened unless a test opts in.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("VIOLA_ALLOW_ALARM_RING_IN_TESTS"):
        return False
    try:
        from core.user_context import is_cloud_surface

        if is_cloud_surface():
            return False
        from services.alarm_ringer import get_alarm_ringer

        return get_alarm_ringer().start(user_id, "timer-%s" % timer_id, label or "timer")
    except Exception:  # noqa: BLE001, RUF100 - a ringer failure must not lose the timer
        logger.warning("Timer ring could not start", exc_info=True)
        return False


def _speak(tts: Any, user_id: str, notification: str) -> None:
    """Fallback only. Called from the timer check thread, so it may have no loop."""
    from core.user_context import user_scope

    with user_scope(user_id):
        # LazyTTSEngine wraps the real engine; try speak_sync first
        speak_sync = getattr(tts, "speak_sync", None)
        if callable(speak_sync):
            speak_sync(notification)
            return
        speak = getattr(tts, "speak", None)
        if not callable(speak):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(speak(notification), loop)
        else:
            asyncio.run(speak(notification))


def alert_timer_completed(tts: Any, user_id: str, timer_id: str, label: str) -> bool:
    """Ring for an expired timer, or say it when ringing is impossible. Returns whether it rang."""
    if ring_for_timer(user_id, timer_id, label):
        logger.info("Timer ringing: %s (user=%s)", label, user_id)
        return True
    notification = timer_message(label)
    logger.info("Timer notification: %s (user=%s)", notification, user_id)
    try:
        _speak(tts, user_id, notification)
    except Exception as exc:  # noqa: BLE001, RUF100 - one failed channel must not stop the others
        logger.warning("Timer TTS notification failed: %s", exc)
    return False


__all__ = ["alert_timer_completed", "ring_for_timer", "timer_message"]
