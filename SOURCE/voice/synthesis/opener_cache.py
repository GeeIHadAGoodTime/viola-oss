"""Pre-rendered TTS opener cache for short, repeated Viola responses."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np

from core.logging_config import get_logger
from core.platform import get_data_dir

SCHEMA_VERSION = 1
DEFAULT_VARIANTS = 5
MIN_VARIANTS = 2
MAX_VARIANTS = 12

CANONICAL_OPENERS: tuple[str, ...] = (
    "okay",
    "sure",
    "got it",
    "on it",
    "one moment",
    "alright",
    "yes",
    "right",
    "absolutely",
    "of course",
)

_TRAILING_PUNCTUATION_RE = re.compile(r"[\s.!,?:;]+$")
_INTERNAL_WHITESPACE_RE = re.compile(r"\s+")
_SPEED_JITTERS: tuple[float, ...] = (
    0.0,
    -0.03,
    0.03,
    -0.015,
    0.015,
    -0.0225,
    0.0225,
    -0.0075,
    0.0075,
    -0.026,
    0.026,
    0.012,
)

SynthesizeVariant = Callable[[str, float], bytes]
logger = get_logger(__name__)

# One build lock per cache file, shared across every OpenerCache instance in the
# process. Instances that share a voice/speed identity also share ``cache_path``,
# and each carries only its own ``threading.Lock``, so nothing stopped two of them
# rendering and writing the same file at once: the writes collided, and on Windows
# a concurrent ``os.replace`` onto the same destination fails outright with
# ERROR_ACCESS_DENIED. Serialising per path also means the second builder finds
# the finished file and skips its own 50 renders rather than repeating them on the
# CPU the user is trying to talk to.
_BUILD_LOCKS_GUARD = threading.Lock()
_BUILD_LOCKS: dict[Path, threading.Lock] = {}


def _build_lock_for(path: Path) -> threading.Lock:
    with _BUILD_LOCKS_GUARD:
        lock = _BUILD_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _BUILD_LOCKS[path] = lock
        return lock


def canonicalize_opener(text: str) -> str:
    """Normalize an already speech-normalized phrase for exact opener lookup."""
    normalized = _TRAILING_PUNCTUATION_RE.sub("", text.strip())
    normalized = _INTERNAL_WHITESPACE_RE.sub(" ", normalized)
    return normalized.casefold()


def clamp_variant_count(value: int) -> int:
    """Clamp configured opener variant count to the supported range."""
    return max(MIN_VARIANTS, min(MAX_VARIANTS, int(value)))


def opener_cache_hash(
    *,
    voice_id: str,
    voice_blend: object,
    speed_default: float,
    schema_version: int = SCHEMA_VERSION,
) -> str:
    """Return the stable cache identity hash for voice/speed/schema inputs."""
    payload = {
        "schema_version": schema_version,
        "speed_default": round(float(speed_default), 6),
        "voice_blend": voice_blend,
        "voice_id": voice_id,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _json_safe(value: object) -> object:
    return json.loads(json.dumps(value, sort_keys=True, default=str))


class OpenerCache:
    """Disk-backed cache of raw int16 PCM variants for canonical openers."""

    def __init__(
        self,
        *,
        voice_id: str,
        voice_blend: object = "",
        speed_default: float = 1.0,
        variants: int = DEFAULT_VARIANTS,
        cache_dir: str | Path | None = None,
        openers: Sequence[str] = CANONICAL_OPENERS,
        rng: random.Random | None = None,
    ) -> None:
        self.voice_id = voice_id
        self.voice_blend = voice_blend
        self.speed_default = float(speed_default)
        self.variant_count = clamp_variant_count(variants)
        self.openers = tuple(canonicalize_opener(opener) for opener in openers)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else get_data_dir() / "tts_cache"
        self.cache_hash = opener_cache_hash(
            voice_id=voice_id,
            voice_blend=voice_blend,
            speed_default=self.speed_default,
        )
        self.cache_path = self.cache_dir / ("openers.%s.npz" % self.cache_hash)
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._variants_by_opener: dict[str, list[bytes]] | None = None
        self._last_index_by_opener: dict[str, int] = {}

    def load(self) -> bool:
        """Load the current cache file if it matches this cache identity."""
        with self._lock:
            return self._load_locked()

    def build(self, synthesize_variant: SynthesizeVariant) -> bool:
        """Render all opener variants and write them to the cache file."""
        with self._lock:
            return self._build_locked(synthesize_variant)

    def lookup(self, text: str, synthesize_variant: SynthesizeVariant | None = None) -> bytes | None:
        """Return a non-recently-used PCM variant when *text* is a canonical opener."""
        opener = canonicalize_opener(text)
        if opener not in self.openers:
            return None

        with self._lock:
            if self._variants_by_opener is None:
                loaded = self._load_locked()
                if not loaded:
                    if synthesize_variant is None:
                        return None
                    # Build synchronously on the first opener hit.  This keeps
                    # Kokoro's lazy-startup behavior intact; the one-time render
                    # cost is paid only by installs that actually use the cache.
                    if not self._build_locked(synthesize_variant):
                        return None
            return self._select_variant_locked(opener)

    def _metadata(self) -> dict[str, object]:
        return {
            "openers": list(self.openers),
            "schema_version": SCHEMA_VERSION,
            "speed_default": self.speed_default,
            "variant_count": self.variant_count,
            "voice_blend": _json_safe(self.voice_blend),
            "voice_id": self.voice_id,
        }

    def _load_locked(self) -> bool:
        if not self.cache_path.exists():
            return False
        try:
            with np.load(self.cache_path, allow_pickle=False) as payload:
                metadata = json.loads(payload["metadata"].tobytes().decode("utf-8"))
                variant_counts = payload["variant_counts"].astype(np.int64).tolist()
                raw_variants = {
                    opener: [
                        payload["pcm_%d_%d" % (opener_index, variant_index)].astype(np.uint8).tobytes()
                        for variant_index in range(int(variant_count))
                    ]
                    for opener_index, (opener, variant_count) in enumerate(
                        zip(self.openers, variant_counts, strict=True)
                    )
                }
        except Exception:
            logger.exception("Failed to load TTS opener cache: %s", self.cache_path)
            return False

        if metadata != self._metadata():
            return False

        variants_by_opener: dict[str, list[bytes]] = {}
        for opener in self.openers:
            variants = raw_variants.get(opener)
            if not isinstance(variants, list):
                return False
            pcm_variants = [bytes(variant) for variant in variants if isinstance(variant, bytes | bytearray)]
            if not pcm_variants:
                return False
            variants_by_opener[opener] = pcm_variants

        self._variants_by_opener = variants_by_opener
        self._last_index_by_opener.clear()
        return True

    def _build_locked(self, synthesize_variant: SynthesizeVariant) -> bool:
        with _build_lock_for(self.cache_path):
            # Another builder for this exact cache identity may have finished
            # while we waited. Its output is byte-equivalent to ours, so load it
            # instead of re-rendering every variant.
            if self._variants_by_opener is None and self._load_locked():
                logger.info("Opener cache was built concurrently; loaded %s", self.cache_path)
                return True
            return self._render_and_write(synthesize_variant)

    def _render_and_write(self, synthesize_variant: SynthesizeVariant) -> bool:
        variants_by_opener: dict[str, list[bytes]] = {}
        try:
            for opener in self.openers:
                rendered: list[bytes] = []
                for speed in self._variant_speeds():
                    pcm = synthesize_variant(opener, speed)
                    if pcm:
                        rendered.append(bytes(pcm))
                if not rendered:
                    return False
                variants_by_opener[opener] = rendered

            self.cache_dir.mkdir(parents=True, exist_ok=True)
            arrays: dict[str, np.ndarray] = {
                "metadata": np.frombuffer(
                    json.dumps(self._metadata(), sort_keys=True, separators=(",", ":")).encode("utf-8"),
                    dtype=np.uint8,
                ),
                "variant_counts": np.array(
                    [len(variants_by_opener[opener]) for opener in self.openers],
                    dtype=np.int16,
                ),
            }
            for opener_index, opener in enumerate(self.openers):
                for variant_index, pcm in enumerate(variants_by_opener[opener]):
                    arrays["pcm_%d_%d" % (opener_index, variant_index)] = np.frombuffer(pcm, dtype=np.uint8)

            # Per-builder temp name. Two builders that share a cache identity
            # also share ``cache_path``, so a single fixed ".tmp" name meant the
            # second one truncated the first one's half-written file and then
            # both raced ``replace()`` -- on Windows that raises PermissionError
            # while the other handle is open, so a duplicated build could leave
            # the cache unwritten and log an exception. Unique names make each
            # write independent and each rename atomic; last writer wins, and
            # both wrote identical content.
            tmp_path = self.cache_path.with_name("%s.%d.%d.tmp" % (self.cache_path.name, os.getpid(), id(self)))
            try:
                with tmp_path.open("wb") as handle:
                    np.savez_compressed(handle, **arrays)
                tmp_path.replace(self.cache_path)
            finally:
                tmp_path.unlink(missing_ok=True)
        except Exception:
            logger.exception("Failed to build TTS opener cache: %s", self.cache_path)
            return False

        self._variants_by_opener = variants_by_opener
        self._last_index_by_opener.clear()
        return True

    def _variant_speeds(self) -> list[float]:
        jitters = _SPEED_JITTERS[: self.variant_count]
        return [self.speed_default * (1.0 + jitter) for jitter in jitters]

    def _select_variant_locked(self, opener: str) -> bytes | None:
        if self._variants_by_opener is None:
            return None
        variants = self._variants_by_opener.get(opener) or []
        if not variants:
            return None

        candidate_indices = list(range(len(variants)))
        previous_index = self._last_index_by_opener.get(opener)
        if previous_index in candidate_indices and len(candidate_indices) > 1:
            candidate_indices.remove(previous_index)

        selected_index = self._rng.choice(candidate_indices)
        self._last_index_by_opener[opener] = selected_index
        return variants[selected_index]
