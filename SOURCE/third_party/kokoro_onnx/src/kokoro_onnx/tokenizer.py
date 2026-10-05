import ctypes
import ctypes.util
import os
import platform
import sys

from .config import DEFAULT_VOCAB, MAX_PHONEME_LENGTH, EspeakConfig
from .log import log


class Tokenizer:
    def __init__(self, espeak_config: EspeakConfig | None = None, vocab: dict = None):
        self.vocab = vocab or DEFAULT_VOCAB
        self._customer = None
        backend = os.getenv("VIOLA_KOKORO_PHONEMIZER", "espeak")
        if backend == "misaki-en":
            from . import _require_customer_telemetry_opt_out
            _require_customer_telemetry_opt_out()
            if espeak_config is not None:
                raise ValueError("Customer pronunciation cannot accept eSpeak configuration")
            from voice.customer_pronunciation import CustomerTokenizer
            self._customer = CustomerTokenizer(self.vocab)
            return
        if backend != "espeak":
            raise ValueError("Unknown Kokoro pronunciation backend")
        import phonemizer
        from phonemizer.backend.espeak.wrapper import EspeakWrapper
        self._phonemizer = phonemizer

        if not espeak_config:
            espeak_config = EspeakConfig()
        if not espeak_config.data_path:
            espeak_config.data_path = os.getenv("PHONEMIZER_ESPEAK_DATA_PATH")
        if not espeak_config.lib_path:
            espeak_config.lib_path = (
                os.getenv("PHONEMIZER_ESPEAK_LIBRARY")
                or ctypes.util.find_library("espeak-ng")
                or ctypes.util.find_library("espeak")
            )
        if not espeak_config.lib_path:
            raise RuntimeError(
                "Kokoro requires a separately installed system eSpeak-NG library. "
                "Set PHONEMIZER_ESPEAK_LIBRARY to its shared library path."
            )

        # Check if PHONEMIZER_ESPEAK_LIBRARY was set
        if os.getenv("PHONEMIZER_ESPEAK_LIBRARY"):
            espeak_config.lib_path = os.getenv("PHONEMIZER_ESPEAK_LIBRARY")

        # Check that the espeak-ng library can be loaded
        try:
            ctypes.cdll.LoadLibrary(espeak_config.lib_path)
        except Exception as e:
            log.error(f"Failed to load espeak shared library: {e}")
            log.warning("Falling back to system wide espeak-ng library")

            # Fallback system wide load
            error_info = (
                "Failed to load espeak-ng from fallback. Please install espeak-ng system wide.\n"
                "\tSee https://github.com/espeak-ng/espeak-ng/blob/master/docs/guide.md\n"
                "\tNote: you can specify shared library path using PHONEMIZER_ESPEAK_LIBRARY environment variable.\n"
                f"Environment:\n\t{platform.platform()} ({platform.release()}) | {sys.version}"
            )
            espeak_config.lib_path = ctypes.util.find_library(
                "espeak-ng"
            ) or ctypes.util.find_library("espeak")
            if not espeak_config.lib_path:
                raise RuntimeError(error_info)
            try:
                ctypes.cdll.LoadLibrary(espeak_config.lib_path)
            except Exception as e:
                raise RuntimeError(f"{e}: {error_info}")

        # Use the system library data directory unless explicitly overridden.
        if espeak_config.data_path:
            EspeakWrapper.set_data_path(espeak_config.data_path)
        EspeakWrapper.set_library(espeak_config.lib_path)

    @staticmethod
    def normalize_text(text) -> str:
        return text.strip()

    def tokenize(self, phonemes):
        if getattr(self, "_customer", None) is not None:
            return self._customer.tokenize(phonemes)
        if len(phonemes) > MAX_PHONEME_LENGTH:
            raise ValueError(
                f"text is too long, must be less than {MAX_PHONEME_LENGTH} phonemes"
            )
        return [i for i in map(self.vocab.get, phonemes) if i is not None]

    def phonemize(self, text, lang="en-us", norm=True) -> str:
        """
        lang can be 'en-us' or 'en-gb'
        """
        if getattr(self, "_customer", None) is not None:
            return self._customer.phonemize(text, lang=lang, norm=norm)
        if norm:
            text = Tokenizer.normalize_text(text)

        phonemes = self._phonemizer.phonemize(
            text, lang, preserve_punctuation=True, with_stress=True
        )
        phonemes = "".join(filter(lambda p: p in self.vocab, phonemes))
        return phonemes.strip()
