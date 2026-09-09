"""Shared faster-whisper options for phone STT."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

PHONE_STT_NO_SPEECH_THRESHOLD = 0.6
PHONE_STT_HALLUCINATION_SILENCE_THRESHOLD_SECS = 1.0
PHONE_STT_TEMPERATURES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)

# faster-whisper's feature extractor is fixed at 16 kHz. When `transcribe()`
# receives a numpy ndarray it does NOT resample -- it skips decode_audio() and
# feeds the samples straight into the 16 kHz mel pipeline (faster_whisper
# transcribe.py: `if not isinstance(audio, np.ndarray): audio = decode_audio(...)`).
# Phone audio arrives at 8 kHz (Telnyx PCMU narrowband), so handing the raw 8 kHz
# ndarray to the model makes it interpret the speech at half rate -- pitch-halved
# and time-stretched ~2x -- which garbled real cellular recipient turns on the
# a8eb0e42 capstone call ("place a large pepperoni pizza" -> "credit insurance
# plan ... pay for rides"). The fix is to resample phone PCM up to 16 kHz before
# the model sees it.
WHISPER_NATIVE_SAMPLE_RATE = 16000


def phone_pcm_to_whisper_float(audio: bytes, source_sample_rate: int) -> np.ndarray:
    """Decode signed-16-bit mono PCM to a float32 ndarray at faster-whisper's 16 kHz.

    faster-whisper assumes any ndarray it is handed is already 16 kHz and never
    resamples it (see module note above), so phone audio captured at 8 kHz must be
    upsampled here or the model transcribes it pitch-halved and time-stretched.
    Returns float32 samples in [-1, 1] resampled to ``WHISPER_NATIVE_SAMPLE_RATE``.
    """

    import numpy as np

    samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
    if source_sample_rate and source_sample_rate != WHISPER_NATIVE_SAMPLE_RATE and samples.size:
        # Band-limited polyphase resample (anti-aliased) to whisper's native rate.
        from math import gcd

        from scipy.signal import resample_poly

        divisor = gcd(WHISPER_NATIVE_SAMPLE_RATE, source_sample_rate)
        up = WHISPER_NATIVE_SAMPLE_RATE // divisor
        down = source_sample_rate // divisor
        samples = resample_poly(samples, up, down).astype(np.float32)
    return samples


def phone_whisper_transcribe_options(
    *,
    language: Any,
    beam_size: int,
    hotwords: str = "",
    initial_prompt: str = "",
) -> dict[str, Any]:
    """Return the phone STT faster-whisper options.

    Keep this as a thin pass-through to faster-whisper's built-in protections:
    Silero VAD, Whisper's no-speech/log-prob/compression guards, temperature
    fallback, and word-timestamp hallucination skipping.
    """

    options: dict[str, Any] = {
        "language": language,
        "beam_size": beam_size,
        "hotwords": hotwords.strip(),
        "condition_on_previous_text": False,
        "vad_filter": True,
        "no_speech_threshold": PHONE_STT_NO_SPEECH_THRESHOLD,
        "compression_ratio_threshold": 2.4,
        "log_prob_threshold": -1.0,
        "temperature": PHONE_STT_TEMPERATURES,
        "word_timestamps": True,
        "hallucination_silence_threshold": PHONE_STT_HALLUCINATION_SILENCE_THRESHOLD_SECS,
    }
    prompt = initial_prompt.strip()
    if prompt:
        options["initial_prompt"] = prompt
    return options
