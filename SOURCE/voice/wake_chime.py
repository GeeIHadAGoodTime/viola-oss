"""Short, quiet confirmation chime played when a wake word is accepted.

A false wake used to be easy to miss: during music the only sign was the duck,
and otherwise the first thing the user heard was Viola answering a question
nobody asked. A soft two-note chime the moment a wake is accepted makes every
wake noticeable, so the user can say "never mind" (or just stay quiet) and the
turn ends silently.

The tone is generated in code (no audio asset). It plays on a background
thread so the wake path never waits for it, honours the user's speech mute
(``tts_enabled`` off or ``voice_muted`` on) and quiet hours (it is scaled with
the same quiet-hours volume as Viola's voice), and is handed to the wake
detector's echo canceller as it plays, like Viola's speech.
"""

from __future__ import annotations

import threading

import numpy as np

from core.logging_config import get_logger

logger = get_logger(__name__)

CHIME_SAMPLE_RATE = 24000
# Peak amplitude at full speech volume. Viola's speech peaks near -10 dBFS;
# this sits well under it (about -20 dBFS) so the chime is a cue, not an alert.
CHIME_PEAK = 0.1
_NOTE_SECONDS = 0.07
_GAP_SECONDS = 0.015
# Two rising notes (E6, A6): reads as "I'm listening" and is far from speech.
_NOTES_HZ = (1318.5, 1760.0)

_cached_chime: np.ndarray | None = None
_cache_lock = threading.Lock()


def generate_wake_chime(sample_rate: int = CHIME_SAMPLE_RATE, notes_hz: tuple[float, ...] = _NOTES_HZ) -> np.ndarray:
    """Return the chime as mono float32 in [-CHIME_PEAK, CHIME_PEAK] (about 155 ms).

    ``notes_hz``: the notes in order (rising for the wake chime; the
    nothing-heard tone plays them falling).
    """
    note_len = int(sample_rate * _NOTE_SECONDS)
    t = np.arange(note_len, dtype=np.float32) / float(sample_rate)
    # Fast attack, smooth exponential decay: soft, no click at either end.
    attack = max(1, int(sample_rate * 0.004))
    envelope = np.exp(-t / 0.025).astype(np.float32)
    envelope[:attack] *= np.linspace(0.0, 1.0, attack, dtype=np.float32)
    envelope[-attack:] *= np.linspace(1.0, 0.0, attack, dtype=np.float32)
    gap = np.zeros(int(sample_rate * _GAP_SECONDS), dtype=np.float32)
    parts: list[np.ndarray] = []
    for index, freq in enumerate(notes_hz):
        tone = np.sin(2.0 * np.pi * freq * t).astype(np.float32) * envelope
        parts.append(tone)
        if index < len(notes_hz) - 1:
            parts.append(gap)
    chime = np.concatenate(parts)
    peak = float(np.max(np.abs(chime))) or 1.0
    return (chime / peak * CHIME_PEAK).astype(np.float32)


def _chime_samples() -> np.ndarray:
    global _cached_chime
    with _cache_lock:
        if _cached_chime is None:
            _cached_chime = generate_wake_chime(CHIME_SAMPLE_RATE)
        return _cached_chime


def _setting(key: str, default: object) -> object:
    try:
        from ui.settings_manager import get_settings_manager

        return get_settings_manager().get(key, default)
    except Exception:  # noqa: BLE001, RUF100 - a settings read must never break the wake path
        logger.debug("Wake chime could not read %s; using default", key, exc_info=True)
        return default


def wake_chime_volume() -> float:
    """Volume (0..1) for the chime now; 0 when the user has muted Viola's voice."""
    if not bool(_setting("tts_enabled", True)) or bool(_setting("voice_muted", False)):
        return 0.0
    try:
        from core.quiet_hours import tts_volume_for_now

        return float(tts_volume_for_now())
    except Exception:  # noqa: BLE001, RUF100 - fall back to a quiet chime rather than none
        logger.debug("Quiet-hours volume unavailable for the wake chime", exc_info=True)
        return 0.3


def _play_blocking(samples: np.ndarray, sample_rate: int) -> bool:
    """Play ``samples`` on the hub speaker. Returns whether they were played."""
    try:
        from audio_core.device_validation import resolve_output_device
        from audio_core.portaudio_guard import sounddevice_guard, sounddevice_playback_guard

        with sounddevice_guard():
            import sounddevice as sd

        with sounddevice_playback_guard():
            try:
                from audio_core.aec_reference_base import push_local_playback_reference

                push_local_playback_reference(samples, sample_rate)
            except Exception:  # noqa: BLE001, RUF100 - the reference tap must never stop the chime
                logger.debug("Wake chime AEC reference push failed", exc_info=True)
            # latency="low": with the default ("high") the Windows MME output
            # added 182 ms before the chime was heard on a test PC; "low" is
            # 93 ms. The tone is written in one call, so the smaller host
            # buffer cannot starve on a busy Python process.
            with sd.OutputStream(
                samplerate=sample_rate,
                device=resolve_output_device(sd),
                channels=1,
                dtype="float32",
                latency="low",
            ) as stream:
                stream.write(samples.reshape(-1, 1))
        return True
    except ImportError:
        logger.debug("sounddevice not available; wake chime skipped")
        return False
    except Exception:  # noqa: BLE001, RUF100 - a missing chime must never break a wake
        logger.warning("Wake chime playback failed", exc_info=True)
        return False


def play_wake_chime() -> bool:
    """Start the wake chime without blocking. Returns False when it is muted."""
    volume = wake_chime_volume()
    if volume <= 0.0:
        logger.debug("Wake chime skipped: Viola's voice is muted")
        return False
    samples = (_chime_samples() * min(1.0, volume)).astype(np.float32)
    threading.Thread(
        target=_play_blocking,
        args=(samples, CHIME_SAMPLE_RATE),
        daemon=True,
        name="wake-chime",
    ).start()
    return True


# The wake's notes falling: "I stopped listening and heard nothing". Played
# when a wake ends without a command (1.0.5 build-7 laptop report, A0.6: the
# turn used to end in silence), a little quieter than the wake chime.
_NOTHING_HEARD_NOTES_HZ = tuple(reversed(_NOTES_HZ))
_NOTHING_HEARD_GAIN = 0.8


def play_nothing_heard_tone() -> bool:
    """Start the nothing-heard tone without blocking. Returns False when muted."""
    volume = wake_chime_volume()
    if volume <= 0.0:
        logger.debug("Nothing-heard tone skipped: Viola's voice is muted")
        return False
    samples = generate_wake_chime(CHIME_SAMPLE_RATE, _NOTHING_HEARD_NOTES_HZ)
    samples = (samples * min(1.0, volume) * _NOTHING_HEARD_GAIN).astype(np.float32)
    threading.Thread(
        target=_play_blocking,
        args=(samples, CHIME_SAMPLE_RATE),
        daemon=True,
        name="wake-nothing-heard",
    ).start()
    return True


__all__ = ["generate_wake_chime", "play_nothing_heard_tone", "play_wake_chime", "wake_chime_volume"]
