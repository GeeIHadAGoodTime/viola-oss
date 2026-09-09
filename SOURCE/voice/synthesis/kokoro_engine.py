"""Kokoro-82M TTS Engine.

Local, high-quality neural TTS using Kokoro-82M via ONNX Runtime.
Produces model-native-rate / 16-bit / mono PCM bytes suitable for local
playback and conversion into the multi-room audio pipeline.

License: Apache-2.0 (Kokoro) + Apache-2.0 (ONNX Runtime).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from config.settings import settings
from core.constants import AUDIO_INT16_MAX, SAMPLE_RATE_24K, SAMPLE_RATE_48K
from core.logging_config import get_logger
from core.quiet_hours import tts_volume_for_now
from voice.synthesis.opener_cache import DEFAULT_VARIANTS, OpenerCache

# Per-sentence watchdog. A single Kokoro inference past 30 s is almost
# always a stuck ONNX session; cancel and let the caller retry.
STREAM_TIMEOUT_SECONDS = 30.0

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from kokoro_onnx import Kokoro

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Package/runtime availability probe
# ---------------------------------------------------------------------------

# Cached result of the one-time kokoro_onnx package probe:
#   None  -> not probed yet
#   ""    -> probe passed
#   "..." -> probe failed; value is the error message
_KOKORO_PKG_PROBE_ERROR: str | None = None


def _probe_kokoro_package() -> str:
    """Probe everything the ``Kokoro()`` constructor needs beyond the model files.

    The constructor's hidden requirements (learned the hard way — 1.0.1
    shipped with model files present but the package data missing, so
    ``is_available()`` said yes while every synthesis returned empty bytes,
    lane-4 P0 L4-2):

      * ``import kokoro_onnx`` must succeed. This is the load-bearing check:
        ``kokoro_onnx.config`` executes ``DEFAULT_VOCAB = get_vocab()`` at
        import, which opens ``kokoro_onnx/config.json`` *package data* — in a
        frozen build that file only exists if the bundle collected it.
      * eSpeak-NG is optional for the open-source core. The phonemizer reports
        a clear runtime error when Kokoro is used without a user-installed
        system eSpeak-NG library/data directory.

    Returns "" when everything the constructor needs is present, else the
    error message. Result is cached: a missing package cannot heal without a
    reinstall, and the import itself is cached by ``sys.modules`` anyway.
    """
    global _KOKORO_PKG_PROBE_ERROR
    if _KOKORO_PKG_PROBE_ERROR is not None:
        return _KOKORO_PKG_PROBE_ERROR

    error = ""
    try:
        import kokoro_onnx
    except (ImportError, AttributeError, OSError, KeyError, ValueError, RuntimeError) as exc:
        # Import executes kokoro_onnx.config.get_vocab(): a frozen bundle
        # missing package data raises FileNotFoundError (OSError); corrupt
        # JSON raises JSONDecodeError (ValueError) / KeyError; broken module
        # deps raise ImportError/AttributeError/RuntimeError.
        error = "kokoro_onnx package not importable (missing package data such as config.json?): %s" % exc

    _KOKORO_PKG_PROBE_ERROR = error
    if error:
        logger.error("Kokoro TTS runtime probe failed -- TTS would be silent: %s", error)
    return error


# ---------------------------------------------------------------------------
# Text preprocessing
# ---------------------------------------------------------------------------

# Match most emoji, pictographs, symbols, and modifier sequences
_EMOJI_RE = re.compile(
    "["
    "\U0001f600-\U0001f64f"  # Emoticons
    "\U0001f300-\U0001f5ff"  # Misc Symbols and Pictographs
    "\U0001f680-\U0001f6ff"  # Transport and Map
    "\U0001f1e0-\U0001f1ff"  # Flags
    "\U00002702-\U000027b0"  # Dingbats
    "\U0001f900-\U0001f9ff"  # Supplemental Symbols
    "\U0001fa00-\U0001fa6f"  # Chess Symbols
    "\U0001fa70-\U0001faff"  # Symbols Extended-A
    "\U00002600-\U000026ff"  # Misc symbols
    "\U0000fe00-\U0000fe0f"  # Variation Selectors
    "\U0000200d"  # Zero Width Joiner
    "\U00002b50"  # Star
    "\U00002b05-\U00002b07"  # Arrows
    "\U0000231a-\U0000231b"  # Watch/Hourglass
    "\U000023e9-\U000023f3"  # Media buttons
    "\U000023f8-\U000023fa"  # More media buttons
    "]+",
    flags=re.UNICODE,
)

# Sentence boundary: split after sentence-ending punctuation followed by space
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

# Character count threshold for chunking long text into sentences
_CHUNK_THRESHOLD = 200

# Safety limit: reject text longer than this to prevent memory/CPU exhaustion.
# 5000 chars ≈ ~1000 words ≈ ~60s synthesis time.
_MAX_TEXT_LENGTH = 5000


def _strip_emoji(text: str) -> str:
    """Remove emoji characters that would confuse the phonemizer."""
    cleaned = _EMOJI_RE.sub("", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def _split_sentences(text: str) -> list[str]:
    """Split text on sentence boundaries (.!? followed by whitespace)."""
    parts = _SENTENCE_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# Resampling: soxr preferred, scipy fallback (VP-6)
# ---------------------------------------------------------------------------

_SOXR_AVAILABLE: bool | None = None  # tri-state: None = untested


def _soxr_resample(samples: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Resample *samples* using soxr (HQ) with scipy fallback.

    soxr provides higher quality and speed than scipy.signal.resample for
    audio resampling.  If ``soxr`` is not installed the function falls back
    to ``scipy.signal.resample`` transparently.
    """
    global _SOXR_AVAILABLE

    if from_rate == to_rate:
        return samples

    # Try soxr first (cached availability check)
    if _SOXR_AVAILABLE is not False:
        try:
            import soxr  # type: ignore[import-untyped]

            _SOXR_AVAILABLE = True
            return soxr.resample(samples, from_rate, to_rate, quality="HQ")
        except ImportError:
            _SOXR_AVAILABLE = False
            logger.debug("soxr not installed, falling back to scipy.signal.resample")
        except Exception:
            logger.debug("soxr resample failed, falling back to scipy", exc_info=True)

    # Fallback to scipy
    from scipy.signal import resample

    target_len = int(len(samples) * to_rate / from_rate)
    return resample(samples, target_len)


# Default voice — warm, natural American-English female
_DEFAULT_VOICE = "af_heart"

# Default speed multiplier (1.0 = normal)
_DEFAULT_SPEED = 1.0


def _cfg_bool(cfg: object, name: str, default: bool) -> bool:
    raw = getattr(cfg, name, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on", "y"}
    return default


def _cfg_float(cfg: object, name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = getattr(cfg, name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, value))


def _cfg_int(cfg: object, name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = getattr(cfg, name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, value))


class KokoroTTSEngine:
    """Neural TTS engine backed by Kokoro-82M (ONNX).

    Implements the ``TTSPort`` protocol expected by the voice synthesis
    factory.  The ONNX model is loaded lazily on first use so that app
    startup is not penalised by the ~310 MB model load.

    Thread safety: a ``threading.Lock`` serialises calls to the ONNX
    session which is not safe for concurrent inference.
    """

    def __init__(
        self,
        model_path: str | Path | None = None,
        voices_path: str | Path | None = None,
        voice: str | None = None,
        speed: float | None = None,
        volume: float | None = None,
        config: object | None = None,
    ) -> None:
        self._config = config
        cfg = config or settings

        # Resolve paths: explicit arg → config/settings attr → default
        self._model_path = self._resolve_path(
            model_path,
            cfg,
            config_attr="tts_kokoro_model_path",
            default="models/tts/kokoro-v1.0.onnx",
        )
        self._voices_path = self._resolve_path(
            voices_path,
            cfg,
            config_attr="tts_kokoro_voices_path",
            default="models/tts/voices-v1.0.bin",
        )

        # Voice / speed / volume
        self._voice = voice or getattr(cfg, "tts_kokoro_voice", None) or _DEFAULT_VOICE
        raw_speed = speed if speed is not None else getattr(cfg, "tts_rate", 150)
        # tts_rate is WPM (default 150).  Map to Kokoro speed multiplier:
        #   150 WPM → 1.0x,  200 WPM → 1.33x,  100 WPM → 0.67x
        self._speed = (
            float(raw_speed) / 150.0 if isinstance(raw_speed, (int, float)) and raw_speed > 5 else _DEFAULT_SPEED
        )
        self._base_volume = volume if volume is not None else getattr(cfg, "tts_volume", 80)

        self._kokoro: Kokoro | None = None
        self._lock = threading.Lock()
        self._load_error: str | None = None
        self._speak_lock: asyncio.Lock | None = None
        self._opener_cache = self._create_opener_cache(cfg)
        self._opener_cache_build_thread: threading.Thread | None = None
        self._last_sample_rate = SAMPLE_RATE_24K
        self._post_fx_enabled = _cfg_bool(cfg, "tts_post_fx_enabled", True)
        self._loudness_target_lufs = _cfg_float(
            cfg,
            "tts_loudness_target_lufs",
            -16.0,
            minimum=-30.0,
            maximum=-10.0,
        )
        self._voice_blend_spec = self._normalize_voice_blend(getattr(cfg, "tts_voice_blend", None))
        self._voice_blend_key: tuple[tuple[str, float], ...] | None = None
        self._voice_blend_style: np.ndarray | None = None
        self._speed_jitter_pct = _cfg_float(
            cfg,
            "tts_speed_jitter_pct",
            0.02,
            minimum=0.0,
            maximum=0.10,
        )
        self._intersentence_gap_ms = _cfg_int(
            cfg,
            "tts_intersentence_gap_ms",
            180,
            minimum=0,
            maximum=1000,
        )

    # ------------------------------------------------------------------
    # Lazy model loading
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> bool:
        """Load the model on first use.  Returns True if ready."""
        if self._kokoro is not None:
            return True
        if self._load_error is not None:
            return False

        from pathlib import Path

        _mp = Path(self._model_path)
        _vp = Path(self._voices_path)
        if not _mp.exists():
            self._load_error = "Model file not found: %s" % _mp
            logger.warning(
                "Kokoro TTS model file missing: %s -- TTS will be silent. "
                "Download from https://github.com/thewh1teagle/kokoro-onnx/releases",
                _mp,
            )
            return False
        if not _vp.exists():
            self._load_error = "Voices file not found: %s" % _vp
            logger.warning(
                "Kokoro voices file missing: %s -- TTS will be silent. "
                "Download from https://github.com/thewh1teagle/kokoro-onnx/releases",
                _vp,
            )
            return False

        t0 = time.perf_counter()
        try:
            from kokoro_onnx import Kokoro

            model_p = str(self._model_path)
            voices_p = str(self._voices_path)
            logger.info(
                "Loading Kokoro TTS model (first use): model=%s, voices=%s",
                model_p,
                voices_p,
            )
            self._kokoro = Kokoro(model_p, voices_p)

            # Suppress noisy but harmless "words count mismatch" warnings
            # from the phonemizer library used internally by kokoro-onnx.
            logging.getLogger("phonemizer").setLevel(logging.ERROR)

            elapsed = time.perf_counter() - t0
            logger.info("Kokoro TTS model loaded in %.2fs", elapsed)
            # Kick off opener-cache build in the background so first-matched
            # opener doesn't pay a 10–15 s synchronous rendering cost. The
            # build thread takes ``self._lock`` per variant so it interleaves
            # safely with concurrent user synthesis (which simply waits one
            # variant's worth of time, ~300 ms, for the lock).
            self._kick_off_opener_cache_build()
            return True
        except Exception as exc:
            self._load_error = str(exc)
            logger.error("Failed to load Kokoro TTS model: %s", exc)
            return False

    def prewarm(self) -> bool:
        """Eagerly load the ONNX model so the first spoken reply doesn't pay the
        ~310 MB Kokoro load inline (critical_path.md WL-2).

        Idempotent: a no-op once the model is loaded (or if a load already
        failed). Returns True when the model is ready. Intended to be called at
        startup from a background thread (see
        VoicePipelineInitializer._prewarm_synthesizer) so the load never blocks
        the first turn.
        """
        return self._ensure_loaded()

    def _kick_off_opener_cache_build(self) -> None:
        """Schedule the opener cache build on a daemon thread (one-shot)."""
        if self._opener_cache is None:
            return
        if self._opener_cache_build_thread is not None:
            return
        thread = threading.Thread(
            target=self._build_opener_cache_bg,
            name="opener-cache-build",
            daemon=True,
        )
        self._opener_cache_build_thread = thread
        thread.start()

    def _build_opener_cache_bg(self) -> None:
        """Load existing cache or render new variants in the background."""
        if self._opener_cache is None:
            return
        try:
            if self._opener_cache.load():
                logger.info("Opener cache loaded from disk")
                return
            logger.info("Building opener cache in background")
            ok = self._opener_cache.build(self._synthesize_opener_cache_variant)
            if ok:
                logger.info("Opener cache built successfully")
            else:
                logger.warning("Opener cache build failed; cache disabled this session")
        except Exception:
            logger.exception("Opener cache background build crashed")

    # ------------------------------------------------------------------
    # TTSPort protocol
    # ------------------------------------------------------------------

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """Synthesize *text* and return raw mono int16 PCM bytes.

        Emoji characters are stripped before synthesis.  Text longer than
        ``_CHUNK_THRESHOLD`` characters is split into sentences and each
        sentence is synthesised independently, reducing perceived latency
        for long responses.
        """
        if not text or not text.strip():
            return b""

        # Honour the tts_enabled config flag
        cfg = self._config or settings
        if not getattr(cfg, "tts_enabled", True):
            return b""

        # Strip emoji before phonemisation
        text = _strip_emoji(text)
        if not text:
            return b""

        # Safety: truncate excessively long text to prevent memory/CPU exhaustion
        if len(text) > _MAX_TEXT_LENGTH:
            logger.warning(
                "TTS text truncated from %d to %d characters",
                len(text),
                _MAX_TEXT_LENGTH,
            )
            text = text[:_MAX_TEXT_LENGTH]

        # For long text, chunk into sentences for lower latency
        if len(text) > _CHUNK_THRESHOLD:
            return await self._synthesize_chunked(text, voice)

        return await self._run_synthesize_with_watchdog(text, voice)

    async def _run_synthesize_with_watchdog(self, text: str, voice: str | None) -> bytes:
        """Run one ``_synthesize_locked`` call, guarded by the watchdog."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._synthesize_locked, text, voice),
                timeout=STREAM_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "Kokoro synthesis exceeded %.0fs watchdog text_length=%d; dropping chunk",
                STREAM_TIMEOUT_SECONDS,
                len(text),
            )
            return b""

    async def _synthesize_chunked(self, text: str, voice: str | None) -> bytes:
        """Synthesise long text sentence-by-sentence and concatenate."""
        sentences = _split_sentences(text)
        if not sentences:
            return b""

        # Single sentence or failed split -- synthesise in one go
        if len(sentences) == 1:
            return await self._run_synthesize_with_watchdog(sentences[0], voice)

        logger.debug("Chunked synthesis: %d sentences from %d chars", len(sentences), len(text))
        chunks: list[bytes] = []
        for sentence in sentences:
            chunk = await self._run_synthesize_with_watchdog(sentence, voice)
            if chunk:
                chunks.append(chunk)
        return self._join_sentence_chunks(chunks, sentences)

    def _synthesize_locked(self, text: str, voice: str | None, speed: float | None = None) -> bytes:
        """Thread-safe wrapper around ``_synthesize_internal``.

        ``speed`` overrides the per-utterance jitter when callers need a
        specific speed multiplier (e.g. opener-cache variant generation
        from ``OpenerCache.build``). Passing ``None`` keeps the normal
        jitter-around-default behavior.
        """
        with self._lock:
            if not self._ensure_loaded():
                logger.warning(
                    "Kokoro model not available, returning empty bytes text_length=%d",
                    len(text),
                )
                return b""

            try:
                return self._synthesize_internal(text, voice, speed)
            except Exception as exc:
                logger.error(
                    "Kokoro synthesis failed text_length=%d: %s",
                    len(text),
                    exc,
                )
                return b""

    def _synthesize_internal(self, text: str, voice: str | None, speed: float | None = None) -> bytes:
        """Blocking synthesis — MUST be called under ``self._lock``.

        ``speed=None`` applies the engine's normal jittered speed; an
        explicit float is honored verbatim so the opener cache (and any
        future callers) can render variants at exact speeds.
        """
        assert self._kokoro is not None  # guarded by _ensure_loaded

        selected_voice, voice_label = self._resolve_voice_for_create(voice)
        if speed is None:
            speed = self._speed_with_jitter()
        t0 = time.perf_counter()

        samples, sample_rate = self._kokoro.create(
            text,
            voice=selected_voice,
            speed=speed,
            lang="en-us",
        )

        elapsed = time.perf_counter() - t0
        audio_duration = len(samples) / sample_rate
        logger.debug(
            "Kokoro synthesised %.2fs audio in %.2fs (RTF %.2f) voice=%s",
            audio_duration,
            elapsed,
            elapsed / audio_duration if audio_duration > 0 else 0,
            voice_label,
        )

        # Telemetry: record TTS synthesis latency
        try:
            from admin.instrumentation import record_tts_latency

            record_tts_latency(elapsed * 1000)
        except Exception:
            logger.debug("TTS latency telemetry unavailable")

        samples = np.asarray(samples, dtype=np.float32)
        self._last_sample_rate = int(sample_rate)
        if self._post_fx_enabled:
            from voice.synthesis.audio_postfx import process_voice

            samples = process_voice(samples, int(sample_rate), self._loudness_target_lufs)
        from voice.synthesis.audio_postfx import apply_attack_declick

        samples = apply_attack_declick(samples, int(sample_rate))
        pcm_native = self._float_to_int16(samples)

        # Apply volume scaling, including quiet-hours time-of-day policy.
        volume = self._current_volume()
        if volume < 1.0:
            pcm_native = (
                (pcm_native.astype(np.float32) * volume).clip(-AUDIO_INT16_MAX, AUDIO_INT16_MAX).astype(np.int16)
            )

        return pcm_native.tobytes()

    def _current_volume(self) -> float:
        return tts_volume_for_now(self._base_volume)

    def _resolve_voice_for_create(self, requested_voice: str | None) -> tuple[str | np.ndarray, str]:
        """Return the Kokoro voice argument and a log-safe label."""
        if requested_voice:
            return requested_voice, requested_voice

        blended = self._get_voice_blend_style()
        if blended is not None:
            assert self._voice_blend_key is not None
            label = "+".join("%s:%.2f" % (voice_id, weight) for voice_id, weight in self._voice_blend_key)
            return blended, "blend(%s)" % label

        return self._voice, self._voice

    def _get_voice_blend_style(self) -> np.ndarray | None:
        """Build and cache a weighted Kokoro style tensor if configured."""
        if not self._voice_blend_spec:
            return None
        assert self._kokoro is not None

        key = tuple((voice_id, float(weight)) for voice_id, weight in self._voice_blend_spec)
        if self._voice_blend_style is not None and self._voice_blend_key == key:
            return self._voice_blend_style

        get_voice_style = getattr(self._kokoro, "get_voice_style", None)
        if not callable(get_voice_style):
            logger.warning("Kokoro voice blending requested, but get_voice_style is unavailable")
            return None

        total_weight = sum(weight for _, weight in key)
        if total_weight <= 0.0:
            return None

        blended: np.ndarray | None = None
        for voice_id, weight in key:
            style = np.asarray(get_voice_style(voice_id), dtype=np.float32)
            contribution = style * np.float32(weight / total_weight)
            blended = contribution if blended is None else blended + contribution

        self._voice_blend_key = key
        self._voice_blend_style = blended
        return self._voice_blend_style

    @staticmethod
    def _normalize_voice_blend(raw: object) -> list[tuple[str, float]] | None:
        """Normalize config voice blend input into positive-weight pairs."""
        if raw in (None, "", []):
            return None
        if not isinstance(raw, list):
            return None

        pairs: list[tuple[str, float]] = []
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                return None
            voice_id, weight = item
            if not isinstance(voice_id, str) or not voice_id.strip():
                return None
            try:
                numeric_weight = float(weight)
            except (TypeError, ValueError):
                return None
            if numeric_weight > 0.0:
                pairs.append((voice_id.strip(), numeric_weight))
        return pairs or None

    def _speed_with_jitter(self) -> float:
        """Return the base speed with per-utterance random variation."""
        if self._speed_jitter_pct <= 0.0:
            return self._speed
        jitter = random.uniform(-self._speed_jitter_pct, self._speed_jitter_pct)
        return max(0.5, self._speed * (1.0 + jitter))

    def _join_sentence_chunks(self, chunks: list[bytes], sentences: list[str]) -> bytes:
        """Join independently synthesized sentence chunks with polish."""
        if not chunks:
            return b""
        if len(chunks) == 1:
            return chunks[0]

        from voice.synthesis.audio_postfx import crossfade_pcm_bytes, silence_pcm_bytes

        sample_rate = self.last_sample_rate
        out: list[bytes] = []
        previous_speech: bytes | None = None
        for i, chunk in enumerate(chunks):
            if previous_speech is not None:
                chunk = crossfade_pcm_bytes(previous_speech, chunk, sample_rate)
            out.append(chunk)
            previous_speech = chunk
            if i + 1 < len(chunks):
                gap_ms = self._gap_after_sentence_ms(sentences[i], sentences[i + 1])
                silence = silence_pcm_bytes(sample_rate, gap_ms)
                if silence:
                    out.append(silence)
        return b"".join(out)

    def _smooth_sentence_boundary(self, previous_pcm: bytes | None, current_pcm: bytes) -> bytes:
        """Crossfade the next sentence start against the previous sentence tail."""
        if previous_pcm is None:
            return current_pcm
        from voice.synthesis.audio_postfx import crossfade_pcm_bytes

        return crossfade_pcm_bytes(previous_pcm, current_pcm, self.last_sample_rate)

    async def _sleep_sentence_gap(self, current_sentence: str, next_sentence: str | None) -> None:
        """Sleep for the configured conversational gap before the next sentence."""
        if next_sentence is None:
            return
        gap_ms = self._gap_after_sentence_ms(current_sentence, next_sentence)
        if gap_ms > 0:
            await asyncio.sleep(gap_ms / 1000.0)

    def _gap_after_sentence_ms(self, current_sentence: str, next_sentence: str) -> int:
        base = self._intersentence_gap_ms
        if base <= 0:
            return 0
        next_start = next_sentence.lstrip().lower()
        if next_start.startswith(("and ", "also ", "plus ")):
            return min(base, 80)
        if current_sentence.rstrip().endswith("?"):
            return max(base, 300)
        return base

    @staticmethod
    def _broadcast_pcm_to_spokes(pcm_bytes: bytes, sample_rate: int = SAMPLE_RATE_24K) -> bool:
        """Fan hub-local TTS PCM to browser spokes over /ws/audio-stream.

        Returns True when the audio was actually handed to a spoke manager, so
        the caller can tell "the hub speaker failed but a spoke played it" from
        "nobody heard this at all".
        """
        try:
            from voice.synthesis.spoke_tts_broadcast import should_skip_spoke_tts_broadcast

            if should_skip_spoke_tts_broadcast():
                return False

            from ui.api.routes.audio_stream import get_active_audio_stream_manager

            manager = get_active_audio_stream_manager()
            if manager is None:
                return False
            manager.broadcast_tts_pcm(pcm_bytes, sample_rate)
        except Exception:
            # ratchet: critical-path-visibility — reaching here means spokes ARE
            # connected (should_skip/manager-None already returned) so a failure
            # silently drops Viola's voice from the multiroom mesh.
            logger.warning("Spoke TTS broadcast failed; multiroom spokes will not play this response", exc_info=True)
            return False
        return True

    @staticmethod
    def _play_pcm_raw(pcm_bytes: bytes, sample_rate: int = SAMPLE_RATE_24K) -> bool:
        """Play raw PCM bytes via sounddevice (no capture guard).

        Callers that need the hub-local capture guard must manage it
        themselves so the guard can span multiple sequential playback
        calls (streaming TTS).

        Returns True when the audio reached a speaker — the hub's own, or a
        connected spoke's. A False here means Viola's reply was rendered as
        text and never made a sound, which the user is told about below.

        This used to catch every playback error, write a log line, and return
        None. Nothing upstream could tell success from failure, so a broken
        output device produced a perfectly normal-looking answer that the user
        simply never heard, with the only trace in a log file they will never
        open.
        """
        played_locally = False
        broadcasted = False
        try:
            import sounddevice as sd

            pcm = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / AUDIO_INT16_MAX
            sd.play(pcm, samplerate=sample_rate)
            broadcasted = KokoroTTSEngine._broadcast_pcm_to_spokes(pcm_bytes, sample_rate)
            sd.wait()
            played_locally = True
        except ImportError:
            # No sounddevice at all: a headless/cloud process, where hub-local
            # playback was never the delivery path. Not a user-facing fault.
            logger.warning("sounddevice not available -- cannot play audio locally")
        except Exception:
            logger.exception("Local audio playback failed")
        finally:
            if not broadcasted:
                broadcasted = KokoroTTSEngine._broadcast_pcm_to_spokes(pcm_bytes, sample_rate)

        if not played_locally and not broadcasted:
            KokoroTTSEngine._report_inaudible_reply()
        return played_locally or broadcasted

    @staticmethod
    def _report_inaudible_reply() -> None:
        """Tell the user their reply was never audible. Best-effort, never raises."""
        try:
            from core.user_notice import notify_user

            notify_user("local_playback_failed", level="warning")
        except (ImportError, RuntimeError, OSError, TypeError, ValueError, AttributeError, LookupError):
            logger.debug("Could not surface the inaudible-reply notice", exc_info=True)

    @staticmethod
    def _play_pcm_locally(pcm_bytes: bytes, sample_rate: int = SAMPLE_RATE_24K) -> bool:
        """Play TTS on the hub speaker.

        Thin wrapper retained so tests can patch ``_play_pcm_locally``
        without having to patch ``_play_pcm_raw`` (which is also used
        from the streaming TTS loop).  Post-2026-04-12 there is no
        hub-local capture guard: TTS plays through sounddevice and is
        captured by ProcTap loopback along with other audio, which is
        the correct behaviour in the source-and-spoke architecture
        (every spoke — including the local one — hears the TTS).

        Returns whether the audio actually reached a speaker.
        """
        return KokoroTTSEngine._play_pcm_raw(pcm_bytes, sample_rate)

    async def speak(self, text: str) -> None:
        """Synthesize and play *text* on the default audio output.

        For long text (> ``_CHUNK_THRESHOLD`` chars), sentences are
        synthesised and played **one at a time** so the user hears the
        first sentence while later sentences are still being generated
        (streaming TTS).  Short text is synthesised in a single shot.
        """
        from voice.synthesis.text_normalizer import normalize_for_speech

        text = normalize_for_speech(text)
        if not text or not text.strip():
            return

        try:
            from admin.instrumentation import record_feature_used

            record_feature_used("tts")
        except Exception:
            logger.debug("Feature usage telemetry unavailable")

        # Lazy-create the asyncio lock (can't create in __init__ — no event loop yet)
        if self._speak_lock is None:
            self._speak_lock = asyncio.Lock()

        cached_opener_pcm = await asyncio.to_thread(self._lookup_opener_cache, text)
        if cached_opener_pcm:
            async with self._speak_lock:
                # Worker thread: _speak_cached_pcm ends in a blocking sd.wait(),
                # so calling it inline froze the event loop (and with it every
                # local API request) for the whole utterance. The streaming path
                # below already offloads playback the same way.
                await asyncio.to_thread(self._speak_cached_pcm, cached_opener_pcm)
            return

        # Determine whether to use streaming path
        clean_text = _strip_emoji(text)
        if not clean_text:
            return
        if len(clean_text) > _MAX_TEXT_LENGTH:
            clean_text = clean_text[:_MAX_TEXT_LENGTH]

        use_streaming = len(clean_text) > _CHUNK_THRESHOLD
        sentences = _split_sentences(clean_text) if use_streaming else []
        # Fall back to single-shot if split produces 0 or 1 sentence
        if len(sentences) <= 1:
            use_streaming = False

        async with self._speak_lock:
            if use_streaming:
                await self._speak_streaming(clean_text, sentences)
            else:
                await self._speak_single(text)

    def _create_opener_cache(self, cfg: object) -> OpenerCache | None:
        if getattr(cfg, "tts_opener_cache_enabled", True) is False:
            return None

        raw_variants = getattr(cfg, "tts_opener_cache_variants", DEFAULT_VARIANTS)
        variants = raw_variants if isinstance(raw_variants, int) else DEFAULT_VARIANTS
        voice_blend = getattr(cfg, "tts_voice_blend", "")
        if not isinstance(voice_blend, str | int | float | bool | list | tuple | dict | type(None)):
            voice_blend = ""

        return OpenerCache(
            voice_id=self._voice,
            voice_blend=voice_blend,
            speed_default=self._speed,
            variants=variants,
        )

    def _lookup_opener_cache(self, text: str) -> bytes | None:
        # Be defensive about missing attributes: some unit tests instantiate
        # via ``KokoroTTSEngine.__new__`` to bypass model loading, which skips
        # ``__init__`` and therefore never sets ``self._config`` or
        # ``self._opener_cache``.
        if getattr(self, "_opener_cache", None) is None:
            return None
        cfg = getattr(self, "_config", None) or settings
        if getattr(cfg, "tts_opener_cache_enabled", True) is False:
            return None
        # Pass synthesize_variant=None so the lookup never blocks on a
        # synchronous build. If the background build hasn't finished yet,
        # fall through to normal synthesis (one chunk of ~300 ms) rather
        # than freezing the user for ~10 s while 50 variants render.
        return self._opener_cache.lookup(text, synthesize_variant=None)

    def _synthesize_opener_cache_variant(self, text: str, speed: float) -> bytes:
        # Pass None so the engine's voice/blend resolution runs. Otherwise the
        # cache builds in ``self._voice`` (a string) and bypasses ``tts_voice_blend``,
        # which would cause cached openers to play in a different voice from the
        # rest of the response. (Branch A's `_resolve_voice_for_create` only
        # consults the blend when the explicit voice argument is falsy.)
        return self._synthesize_locked(text, None, speed)

    def _speak_cached_pcm(self, pcm_bytes: bytes) -> None:
        monitor = None
        try:
            from diagnostics.wake_state_sync import get_state_sync_monitor

            monitor = get_state_sync_monitor()
            monitor.update_tts_state(is_speaking=True)
        except Exception as e:
            logger.debug("TTS state tracking unavailable: %s", e)

        from contextlib import AbstractContextManager, nullcontext

        duck_ctx: AbstractContextManager[object]
        try:
            from utils.audio_ducking import duck_context

            duck_ctx = duck_context()
        except Exception as e:
            logger.debug("Audio ducking unavailable: %s", e)
            duck_ctx = nullcontext()

        try:
            with duck_ctx:
                self._play_pcm_locally(pcm_bytes)
        finally:
            if monitor is not None:
                try:
                    monitor.update_tts_state(is_speaking=False)
                except Exception as e:
                    logger.debug("TTS state tracking update failed: %s", e)

    async def _speak_single(self, text: str) -> None:
        """Single-shot synthesize-then-play (original path)."""
        monitor = None
        try:
            from diagnostics.wake_state_sync import get_state_sync_monitor

            monitor = get_state_sync_monitor()
            monitor.update_tts_state(is_speaking=True)
        except Exception as e:
            logger.debug("TTS state tracking unavailable: %s", e)

        from contextlib import AbstractContextManager, nullcontext

        duck_ctx: AbstractContextManager[object]
        try:
            from utils.audio_ducking import duck_context

            duck_ctx = duck_context()
        except Exception as e:
            logger.debug("Audio ducking unavailable: %s", e)
            duck_ctx = nullcontext()

        try:
            with duck_ctx:
                pcm_bytes = await self.synthesize(text)
                if not pcm_bytes:
                    return
                # Worker thread for the same reason as the cached-opener and
                # streaming paths: _play_pcm_locally blocks on sd.wait() until
                # the audio finishes, which would otherwise stall the event loop
                # serving the local API for the length of every spoken line.
                await asyncio.to_thread(self._play_pcm_locally, pcm_bytes, self.last_sample_rate)
        finally:
            if monitor is not None:
                try:
                    monitor.update_tts_state(is_speaking=False)
                except Exception as e:
                    logger.debug("TTS state tracking update failed: %s", e)

    async def _speak_streaming(self, full_text: str, sentences: list[str]) -> None:
        """Synthesize and play each sentence as it is ready (streaming).

        Ducking, ``is_speaking``, and the hub-local capture guard each
        activate **once** at the start and deactivate **once** at the
        end — they are NOT toggled per-sentence.
        """
        t_start = time.perf_counter()
        ttfb_logged = False

        # --- diagnostics: is_speaking flag (once) ---
        monitor = None
        try:
            from diagnostics.wake_state_sync import get_state_sync_monitor

            monitor = get_state_sync_monitor()
            monitor.update_tts_state(is_speaking=True)
        except Exception as e:
            logger.debug("TTS state tracking unavailable: %s", e)

        # --- ducking context (once) ---
        from contextlib import AbstractContextManager, nullcontext

        duck_ctx: AbstractContextManager[object]
        try:
            from utils.audio_ducking import duck_context

            duck_ctx = duck_context()
        except Exception as e:
            logger.debug("Audio ducking unavailable: %s", e)
            duck_ctx = nullcontext()

        try:
            with duck_ctx:
                logger.debug(
                    "Streaming TTS: %d sentences from %d chars (prefetch pipeline)",
                    len(sentences),
                    len(full_text),
                )

                # VP-4: Prefetch pipeline — synthesize sentence N+1 while
                # playing sentence N, reducing inter-sentence gaps to near
                # zero.  The first sentence is synthesised eagerly, then
                # each subsequent sentence is kicked off before playback
                # of the current one begins.

                prefetch_task: asyncio.Task[bytes] | None = None
                previous_pcm: bytes | None = None

                for i, sentence in enumerate(sentences):
                    # If we have a prefetched result, await it; otherwise
                    # synthesise this sentence now (first iteration or
                    # after a prefetch skip).
                    if prefetch_task is not None:
                        pcm = await prefetch_task
                        prefetch_task = None
                    else:
                        pcm = await asyncio.to_thread(
                            self._synthesize_locked,
                            sentence,
                            None,
                        )

                    # Kick off synthesis of the NEXT sentence while we
                    # play the current one.
                    if i + 1 < len(sentences):
                        next_sentence = sentences[i + 1]
                        prefetch_task = asyncio.ensure_future(
                            asyncio.to_thread(
                                self._synthesize_locked,
                                next_sentence,
                                None,
                            )
                        )

                    if not pcm:
                        continue
                    pcm = self._smooth_sentence_boundary(previous_pcm, pcm)

                    # Log TTFB on first audible chunk
                    if not ttfb_logged:
                        ttfb_ms = (time.perf_counter() - t_start) * 1000
                        ttfb_logged = True
                        if ttfb_ms > 500:
                            logger.warning(
                                "Streaming TTS TTFB %.0f ms (> 500 ms threshold)",
                                ttfb_ms,
                            )
                        else:
                            logger.info("Streaming TTS TTFB %.0f ms", ttfb_ms)

                    # Play this sentence immediately (uses _play_pcm_raw
                    # because the capture guard is managed at this level).
                    # While this plays, the prefetch task synthesises the
                    # next sentence in parallel.
                    await asyncio.to_thread(self._play_pcm_raw, pcm, self.last_sample_rate)
                    previous_pcm = pcm
                    logger.debug(
                        "Streaming TTS: played sentence %d/%d (%d bytes)",
                        i + 1,
                        len(sentences),
                        len(pcm),
                    )
                    if i + 1 < len(sentences):
                        await self._sleep_sentence_gap(sentence, sentences[i + 1])

                # Await any remaining prefetch (shouldn't happen in normal
                # flow, but guard against edge cases)
                if prefetch_task is not None:
                    remaining_pcm = await prefetch_task
                    if remaining_pcm:
                        remaining_pcm = self._smooth_sentence_boundary(previous_pcm, remaining_pcm)
                        await asyncio.to_thread(self._play_pcm_raw, remaining_pcm, self.last_sample_rate)
        finally:
            # Clear is_speaking (once)
            if monitor is not None:
                try:
                    monitor.update_tts_state(is_speaking=False)
                except Exception as e:
                    logger.debug("TTS state tracking update failed: %s", e)

    # ------------------------------------------------------------------
    # LLM-streaming TTS: speak tokens as they arrive from the LLM
    # ------------------------------------------------------------------

    async def speak_streaming(self, text_chunks: AsyncIterator[str]) -> str:
        """Speak LLM-streamed text sentence-by-sentence with prefetch.

        Buffers incoming tokens until a sentence boundary is detected, then
        synthesises and plays that sentence immediately while the LLM
        continues generating.  Uses the same prefetch pipeline as
        ``_speak_streaming`` to overlap synthesis of sentence N+1 with
        playback of sentence N.

        Args:
            text_chunks: Async iterator yielding text fragments (LLM tokens).

        Returns:
            The full concatenated text that was spoken.
        """
        from voice.synthesis.text_normalizer import normalize_for_speech

        # Lazy-create the asyncio lock
        if self._speak_lock is None:
            self._speak_lock = asyncio.Lock()

        t_start = time.perf_counter()
        ttfb_logged = False

        # Collect full text for caller (history / logging)
        full_text_parts: list[str] = []
        buffer = ""
        sentences_queue: list[str] = []

        # --- Phase 1: drain the async iterator, buffering into sentences ---
        # We process sentences as they become available (interleaving drain
        # and playback) rather than draining everything first.

        async with self._speak_lock:
            # --- diagnostics: is_speaking flag (once) ---
            monitor = None
            try:
                from diagnostics.wake_state_sync import get_state_sync_monitor

                monitor = get_state_sync_monitor()
                monitor.update_tts_state(is_speaking=True)
            except Exception as e:
                logger.debug("TTS state tracking unavailable: %s", e)

            # --- ducking context (once) ---
            from contextlib import AbstractContextManager, nullcontext

            duck_ctx: AbstractContextManager[object]
            try:
                from utils.audio_ducking import duck_context

                duck_ctx = duck_context()
            except Exception as e:
                logger.debug("Audio ducking unavailable: %s", e)
                duck_ctx = nullcontext()

            try:
                with duck_ctx:
                    prefetch_task: asyncio.Task[bytes] | None = None
                    sentence_count = 0
                    previous_pcm: bytes | None = None
                    previous_sentence: str | None = None

                    async for chunk in text_chunks:
                        full_text_parts.append(chunk)
                        buffer += chunk

                        # Check for complete sentences
                        parts = _SENTENCE_RE.split(buffer)
                        if len(parts) > 1:
                            # All but the last part are complete sentences
                            for part in parts[:-1]:
                                cleaned = _strip_emoji(part.strip())
                                if cleaned:
                                    cleaned = normalize_for_speech(cleaned)
                                    if cleaned:
                                        sentences_queue.append(cleaned)
                            buffer = parts[-1]
                        elif buffer.rstrip()[-1:] in ".!?":
                            # Buffer ends with sentence punctuation (no trailing space yet)
                            cleaned = _strip_emoji(buffer.strip())
                            if cleaned:
                                cleaned = normalize_for_speech(cleaned)
                                if cleaned:
                                    sentences_queue.append(cleaned)
                            buffer = ""

                        # Play any queued sentences immediately
                        while sentences_queue:
                            sentence = sentences_queue.pop(0)
                            sentence_count += 1

                            # Wait for prefetch if available
                            if prefetch_task is not None:
                                pcm = await prefetch_task
                                prefetch_task = None
                            else:
                                pcm = await asyncio.to_thread(
                                    self._synthesize_locked,
                                    sentence,
                                    None,
                                )

                            # Kick off next sentence synthesis if more queued
                            if sentences_queue:
                                next_s = sentences_queue[0]
                                prefetch_task = asyncio.ensure_future(
                                    asyncio.to_thread(
                                        self._synthesize_locked,
                                        next_s,
                                        None,
                                    )
                                )

                            if not pcm:
                                continue
                            if previous_sentence is not None:
                                await self._sleep_sentence_gap(previous_sentence, sentence)
                            pcm = self._smooth_sentence_boundary(previous_pcm, pcm)

                            if not ttfb_logged:
                                ttfb_ms = (time.perf_counter() - t_start) * 1000
                                ttfb_logged = True
                                if ttfb_ms > 500:
                                    logger.warning(
                                        "LLM-streaming TTS TTFB %.0f ms (> 500 ms)",
                                        ttfb_ms,
                                    )
                                else:
                                    logger.info("LLM-streaming TTS TTFB %.0f ms", ttfb_ms)

                            await asyncio.to_thread(self._play_pcm_raw, pcm, self.last_sample_rate)
                            previous_pcm = pcm
                            previous_sentence = sentence

                    # --- Flush remaining buffer ---
                    if buffer.strip():
                        cleaned = _strip_emoji(buffer.strip())
                        if cleaned:
                            cleaned = normalize_for_speech(cleaned)
                            if cleaned:
                                sentences_queue.append(cleaned)
                                buffer = ""

                    # Play remaining sentences
                    for sentence in sentences_queue:
                        sentence_count += 1
                        if prefetch_task is not None:
                            pcm = await prefetch_task
                            prefetch_task = None
                        else:
                            pcm = await asyncio.to_thread(
                                self._synthesize_locked,
                                sentence,
                                None,
                            )
                        if pcm:
                            if previous_sentence is not None:
                                await self._sleep_sentence_gap(previous_sentence, sentence)
                            pcm = self._smooth_sentence_boundary(previous_pcm, pcm)
                            if not ttfb_logged:
                                ttfb_ms = (time.perf_counter() - t_start) * 1000
                                ttfb_logged = True
                                logger.info("LLM-streaming TTS TTFB %.0f ms", ttfb_ms)
                            await asyncio.to_thread(self._play_pcm_raw, pcm, self.last_sample_rate)
                            previous_pcm = pcm
                            previous_sentence = sentence

                    # Await any remaining prefetch
                    if prefetch_task is not None:
                        remaining_pcm = await prefetch_task
                        if remaining_pcm:
                            remaining_pcm = self._smooth_sentence_boundary(previous_pcm, remaining_pcm)
                            await asyncio.to_thread(self._play_pcm_raw, remaining_pcm, self.last_sample_rate)

                    elapsed = time.perf_counter() - t_start
                    logger.info(
                        "LLM-streaming TTS complete: %d sentences in %.2fs",
                        sentence_count,
                        elapsed,
                    )

            finally:
                if monitor is not None:
                    try:
                        monitor.update_tts_state(is_speaking=False)
                    except Exception as e:
                        logger.debug("TTS state tracking update failed: %s", e)

        return "".join(full_text_parts)

    # ------------------------------------------------------------------
    # Multi-room pipeline injection
    # ------------------------------------------------------------------

    @staticmethod
    def _inject_into_pipeline(pcm_mono: bytes, sample_rate: int = SAMPLE_RATE_24K) -> bool:
        """Convert TTS PCM and push it into the multi-room ChunkStamper.

        Args:
            pcm_mono: Raw mono / int16 LE PCM bytes from ``synthesize()``.
            sample_rate: Sample rate of ``pcm_mono``.

        Returns:
            True if bytes were successfully injected into the pipeline,
            False if no ChunkStamper is active (multi-room not enabled).
        """
        try:
            from audio_core.streaming.pipeline_wiring import (
                get_active_chunk_stamper,
            )

            stamper = get_active_chunk_stamper()
            if stamper is None:
                return False

            from audio_core.streaming.tts_format_bridge import (
                convert_tts_to_pipeline,
            )

            pipeline_pcm = convert_tts_to_pipeline(pcm_mono, source_rate=sample_rate)
            if not pipeline_pcm:
                return False

            stamper.on_capture_data(pipeline_pcm, SAMPLE_RATE_48K, 2, 2)
            logger.info(
                "TTS audio injected into multi-room pipeline: %d bytes (%d Hz mono) -> %d bytes (48k stereo)",
                len(pcm_mono),
                sample_rate,
                len(pipeline_pcm),
            )
            return True
        except Exception as exc:
            logger.error("Failed to inject TTS into multi-room pipeline: %s", exc)
            return False

    def is_available(self) -> bool:
        """Return True only if everything the ``Kokoro()`` constructor needs is present.

        That is: the model + voices files on disk AND the kokoro_onnx package
        runtime (its bundled ``config.json`` package data, importable deps,
        and a resolvable espeak-ng). Checking just the two model files is the
        1.0.1 false-green: the factory logged "engine created (model files
        present)" while every synthesis returned empty bytes because the
        frozen bundle lacked the package data (lane-4 P0 L4-2).

        When something is missing, an informative message is logged to guide
        the user through the first-run download / reinstall.
        """
        model_ok = Path(self._model_path).exists()
        voices_ok = Path(self._voices_path).exists()
        if model_ok and voices_ok:
            return not _probe_kokoro_package()

        if not model_ok:
            logger.warning(
                "Kokoro TTS model not found at %s. "
                "Download from: https://github.com/thewh1teagle/kokoro-onnx/releases "
                "and place model files in: %s",
                self._model_path,
                Path(self._model_path).parent,
            )
        if not voices_ok:
            logger.warning(
                "Kokoro TTS voices file not found at %s. "
                "Download from: https://github.com/thewh1teagle/kokoro-onnx/releases "
                "and place voices file in: %s",
                self._voices_path,
                Path(self._voices_path).parent,
            )
        return False

    # ------------------------------------------------------------------
    # Audio processing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _float_to_int16(samples: np.ndarray) -> np.ndarray:
        """Convert normalized float PCM to int16 without changing sample rate."""

        # float32 → int16
        scaled = np.clip(samples * AUDIO_INT16_MAX, -AUDIO_INT16_MAX - 1, AUDIO_INT16_MAX)
        return scaled.astype(np.int16)

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_path(
        explicit: str | Path | None,
        cfg: object,
        config_attr: str,
        default: str,
    ) -> Path:
        """Resolve a file path from explicit arg → config attr → default.

        Relative values (from any source — config defaults like
        ``tts_kokoro_model_path = "models/tts/..."`` are relative) anchor on
        get_project_root() (the repo in dev, _internal in a frozen install),
        never cwd: a Start-Menu launch's cwd is the read-only install dir, so a
        cwd-relative bundled-model path is never found.
        """
        from core.platform import get_project_root

        def _anchor(value: str | Path) -> Path:
            p = Path(value)
            return p if p.is_absolute() else get_project_root() / p

        if explicit is not None:
            return _anchor(explicit)

        from_cfg = getattr(cfg, config_attr, None)
        if from_cfg:
            return _anchor(str(from_cfg))

        return _anchor(default)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def say(self, text: str, **kwargs: object) -> None:
        """Alias for speak() to satisfy IntentTTSPort protocol."""
        await self.speak(text)

    def stop(self) -> None:
        """Release model resources."""
        with self._lock:
            self._kokoro = None
            logger.info("Kokoro TTS engine stopped")

    @property
    def last_sample_rate(self) -> int:
        """Sample rate of the most recent non-empty synthesized PCM."""
        return getattr(self, "_last_sample_rate", SAMPLE_RATE_24K)

    @property
    def voice(self) -> str:
        """Currently configured voice name."""
        return self._voice

    @voice.setter
    def voice(self, value: str) -> None:
        self._voice = value
