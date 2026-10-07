import asyncio
import importlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import suppress

_CUSTOMER_PROFILE_AT_IMPORT = os.getenv("VIOLA_KOKORO_PHONEMIZER") == "misaki-en"
_ORT_OPT_OUT_AT_IMPORT = os.getenv("ORT_DISABLE_TELEMETRY") == "1"
_ORT_PRESENT_AT_IMPORT = any(
    name == "onnxruntime" or name.startswith("onnxruntime.") for name in sys.modules
)


def _require_customer_telemetry_opt_out():
    # Official ORT 1.30.0 non-Windows telemetry initializes before callers can
    # invoke its Python API. The explicit customer profile must opt out before
    # this module imports it. Whole-application startup ordering is separately
    # verified by packaging; this check cannot undo an earlier third-party import.
    if (
        os.getenv("VIOLA_KOKORO_PHONEMIZER") == "misaki-en"
        and sys.platform != "win32"
        and (
            os.getenv("ORT_DISABLE_TELEMETRY") != "1"
            or not _CUSTOMER_PROFILE_AT_IMPORT
            or not _ORT_OPT_OUT_AT_IMPORT
            or _ORT_PRESENT_AT_IMPORT
        )
    ):
        raise RuntimeError(
            "Customer Kokoro requires both profile and ORT_DISABLE_TELEMETRY=1 before Python starts; "
            "import customer Kokoro before any other ONNX Runtime consumer"
        )


_require_customer_telemetry_opt_out()

# E402: enforce the customer privacy contract before all dependent imports.
import numpy as np  # noqa: E402
import onnxruntime as rt  # noqa: E402
from numpy.typing import NDArray  # noqa: E402

from .config import (  # noqa: E402
    MAX_PHONEME_LENGTH,
    SAMPLE_RATE,
    EspeakConfig,
    KoKoroConfig,
)
from .log import log  # noqa: E402
from .tokenizer import Tokenizer  # noqa: E402
from .trim import trim as trim_audio  # noqa: E402


class Kokoro:
    def __init__(
        self,
        model_path: str,
        voices_path: str,
        espeak_config: EspeakConfig | None = None,
        vocab_config: dict | str | None = None,
        *,
        customer_tokenizer=None,
    ):
        _require_customer_telemetry_opt_out()
        # Show useful information for bug reports
        log.debug(
            f"koko-onnx version {importlib.metadata.version('kokoro-onnx')} on {platform.platform()} {platform.version()}"
        )
        self.config = KoKoroConfig(model_path, voices_path, espeak_config)
        self.config.validate()
        if customer_tokenizer is not None:
            vocab = self._load_vocab(vocab_config)
            self.tokenizer = Tokenizer(espeak_config, vocab=vocab, customer_tokenizer=customer_tokenizer)

        # See list of providers https://github.com/microsoft/onnxruntime/issues/22101#issuecomment-2357667377
        providers = ["CPUExecutionProvider"]

        # Check if kokoro-onnx installed with kokoro-onnx[gpu] feature (Windows/Linux)
        gpu_enabled = importlib.util.find_spec("onnxruntime-gpu")
        if gpu_enabled:
            providers: list[str] = rt.get_available_providers()

        # Check if ONNX_PROVIDER environment variable was set
        env_provider = os.getenv("ONNX_PROVIDER")
        if env_provider:
            providers = [env_provider]

        log.debug(f"Providers: {providers}")
        self.sess = rt.InferenceSession(model_path, providers=providers)
        self.voices: np.ndarray = np.load(voices_path)

        if customer_tokenizer is None:
            vocab = self._load_vocab(vocab_config)
            self.tokenizer = Tokenizer(espeak_config, vocab=vocab)

    @classmethod
    def from_session(
        cls,
        session: rt.InferenceSession,
        voices_path: str,
        espeak_config: EspeakConfig | None = None,
        vocab_config: dict | str | None = None,
        *,
        customer_tokenizer=None,
    ):
        _require_customer_telemetry_opt_out()
        instance = cls.__new__(cls)
        instance.sess = session
        instance.config = KoKoroConfig(session._model_path, voices_path, espeak_config)
        instance.config.validate()
        if customer_tokenizer is not None:
            vocab = instance._load_vocab(vocab_config)
            instance.tokenizer = Tokenizer(espeak_config, vocab=vocab, customer_tokenizer=customer_tokenizer)
        instance.voices = np.load(voices_path)

        if customer_tokenizer is None:
            vocab = instance._load_vocab(vocab_config)
            instance.tokenizer = Tokenizer(espeak_config, vocab=vocab)
        return instance

    def _load_vocab(self, vocab_config: dict | str | None) -> dict:
        """Load vocabulary from config file or dictionary.

        Args:
            vocab_config: Path to vocab config file or dictionary containing vocab.

        Returns:
            Loaded vocabulary dictionary or empty dictionary if no config provided.
        """

        if isinstance(vocab_config, str):
            with open(vocab_config, encoding="utf-8") as fp:
                config = json.load(fp)
                return config["vocab"]
        if isinstance(vocab_config, dict):
            return vocab_config["vocab"]
        return {}

    def _create_audio(
        self, phonemes: str, voice: NDArray[np.float32], speed: float
    ) -> tuple[NDArray[np.float32], int]:
        log.debug(f"Phonemes: {phonemes}")
        start_t = time.time()
        tokens = np.array(self.tokenizer.tokenize(phonemes), dtype=np.int64)
        assert len(tokens) <= MAX_PHONEME_LENGTH, (
            f"Context length is {MAX_PHONEME_LENGTH}, but leave room for the pad token 0 at the start & end"
        )

        voice = voice[len(tokens)]
        tokens = [[0, *tokens, 0]]
        if "input_ids" in [i.name for i in self.sess.get_inputs()]:
            # Newer export versions
            inputs = {
                "input_ids": tokens,
                "style": np.array(voice, dtype=np.float32),
                "speed": np.array([speed], dtype=np.int32),
            }
        else:
            inputs = {
                "tokens": tokens,
                "style": voice,
                "speed": np.ones(1, dtype=np.float32) * speed,
            }

        audio = self.sess.run(None, inputs)[0]
        audio_duration = len(audio) / SAMPLE_RATE
        create_duration = time.time() - start_t
        rtf = create_duration / audio_duration
        log.debug(
            f"Created audio in length of {audio_duration:.2f}s for {len(phonemes)} phonemes in {create_duration:.2f}s (RTF: {rtf:.2f}"
        )
        return audio, SAMPLE_RATE

    def get_voice_style(self, name: str) -> NDArray[np.float32]:
        return self.voices[name]

    def _split_phonemes(self, phonemes: str) -> list[str]:
        """Split losslessly, preferring punctuation, then whitespace boundaries."""
        batches: list[str] = []
        start = 0
        while start < len(phonemes):
            end = min(start + MAX_PHONEME_LENGTH, len(phonemes))
            if end < len(phonemes):
                window = phonemes[start:end]
                for boundary in (r"[.,!?;]", r"\s"):
                    matches = list(re.finditer(boundary, window))
                    if matches:
                        end = start + matches[-1].end()
                        break
            batches.append(phonemes[start:end])
            start = end
        return batches

    def create(
        self,
        text: str,
        voice: str | NDArray[np.float32],
        speed: float = 1.0,
        lang: str = "en-us",
        is_phonemes: bool = False,
        trim: bool = True,
    ) -> tuple[NDArray[np.float32], int]:
        """
        Create audio from text using the specified voice and speed.
        """
        assert speed >= 0.5 and speed <= 2.0, "Speed should be between 0.5 and 2.0"

        customer = getattr(self.tokenizer, "_customer", None)
        if customer is not None:
            from voice.customer_voice_routing import require_customer_voice, uses_named_customer_voices

            if uses_named_customer_voices(customer):
                require_customer_voice(lang, voice)
                if voice not in self.voices:
                    raise ValueError("The selected customer voice is absent from the loaded asset")
        if isinstance(voice, str):
            assert voice in self.voices, f"Voice {voice} not found in available voices"
            voice = self.get_voice_style(voice)

        start_t = time.time()
        if is_phonemes:
            phonemes = text
        else:
            phonemes = self.tokenizer.phonemize(text, lang)
        # Create batches of phonemes by splitting spaces to MAX_PHONEME_LENGTH
        batched_phoenemes = self._split_phonemes(phonemes)

        audio = []
        log.debug(
            f"Creating audio for {len(batched_phoenemes)} batches for {len(phonemes)} phonemes"
        )
        for phonemes in batched_phoenemes:
            audio_part, _ = self._create_audio(phonemes, voice, speed)
            if trim:
                # Trim leading and trailing silence for a more natural sound concatenation
                # (initial ~2s, subsequent ~0.02s)
                audio_part, _ = trim_audio(audio_part)
            audio.append(audio_part)
        audio = np.concatenate(audio) if audio else np.empty(0, dtype=np.float32)
        log.debug(f"Created audio in {time.time() - start_t:.2f}s")
        return audio, SAMPLE_RATE

    async def create_stream(
        self,
        text: str,
        voice: str | NDArray[np.float32],
        speed: float = 1.0,
        lang: str = "en-us",
        is_phonemes: bool = False,
        trim: bool = True,
    ) -> AsyncGenerator[tuple[NDArray[np.float32], int], None]:
        """
        Stream audio creation asynchronously in the background, yielding chunks as they are processed.
        """
        assert speed >= 0.5 and speed <= 2.0, "Speed should be between 0.5 and 2.0"

        customer = getattr(self.tokenizer, "_customer", None)
        if customer is not None:
            from voice.customer_voice_routing import require_customer_voice, uses_named_customer_voices

            if uses_named_customer_voices(customer):
                require_customer_voice(lang, voice)
                if voice not in self.voices:
                    raise ValueError("The selected customer voice is absent from the loaded asset")
        if isinstance(voice, str):
            assert voice in self.voices, f"Voice {voice} not found in available voices"
            voice = self.get_voice_style(voice)

        if is_phonemes:
            phonemes = text
        else:
            phonemes = self.tokenizer.phonemize(text, lang)

        batched_phonemes = self._split_phonemes(phonemes)
        queue: asyncio.Queue[tuple[NDArray[np.float32], int] | None] = asyncio.Queue()

        async def process_batches():
            """Process phoneme batches in the background."""
            try:
                for i, phonemes in enumerate(batched_phonemes):
                    loop = asyncio.get_event_loop()
                    # Execute in separate thread since it's blocking operation
                    audio_part, sample_rate = await loop.run_in_executor(
                        None, self._create_audio, phonemes, voice, speed
                    )
                    if trim:
                        # Trim leading and trailing silence for a more natural sound concatenation
                        # (initial ~2s, subsequent ~0.02s)
                        audio_part, _ = trim_audio(audio_part)
                    log.debug(f"Processed chunk {i} of stream")
                    await queue.put((audio_part, sample_rate))
            finally:
                queue.put_nowait(None)  # Wake the consumer on completion or failure.

        # Retain ownership so closing/cancelling a stream stops remaining batches.
        task = asyncio.create_task(process_batches())
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task  # Propagate inference failures instead of hanging.

    def get_voices(self) -> list[str]:
        return list(sorted(self.voices.keys()))
