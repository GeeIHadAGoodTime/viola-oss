"""Telnyx telephony configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from config.defaults import DEFAULT_PHONE_MODEL
from core.logging_config import get_logger

logger = get_logger(__name__)

_DEFAULT_MAX_CONCURRENT_CALLS = 25
# Phone STT model. MUST NOT be base.en/tiny.en: those are too weak for 8 kHz
# telephony-band audio and fail whisper's compression/logprob thresholds, so
# faster-whisper falls back up the temperature ladder (0.0->1.0) and emits
# NON-DETERMINISTIC hallucinated text (the recurring "invented words / phantom
# turns"). Proven offline 2026-06-25: on a real carrier recording of Viola's
# clean TTS, base.en produced different garbage on identical audio across runs
# ("If you're already recording and trying to track..."), while small.en decoded
# stably and correctly at temp 0.0 ("Hi, this is Viola..."). small.en is the
# industry-minimum whisper tier for telephony ASR. The phone-stt-model-strength
# ratchet enforces this; see scripts/check_phone_stt_model_strength.py.
_DEFAULT_PHONE_WHISPER_MODEL = "small.en"
_MULTILINGUAL_PHONE_WHISPER_MODEL = "small"
_DEFAULT_PHONE_WHISPER_DEVICE = "cpu"
_DEFAULT_PHONE_WHISPER_COMPUTE_TYPE = "int8"
_DEFAULT_PHONE_WHISPER_BEAM_SIZE = 1
_SUPPORTED_TTS_PROVIDERS = frozenset({"local", "piper", "espeak", "elevenlabs"})

# Per-provider default voice. The phone TTS *provider* default is "local"
# (Kokoro), so default calls use the neural phone voice. The explicit Piper
# fallback still resolves to a Piper-compatible voice when selected.
# This map is the single source of truth so every TelnyxConfig construction site
# (desktop local, desktop->cloud, cloud_routes, loopback, tests) inherits a voice
# the chosen provider can actually speak.
DEFAULT_PIPER_PHONE_VOICE = "en_US-lessac-medium"
DEFAULT_KOKORO_PHONE_VOICE = "af_heart"
DEFAULT_ESPEAK_PHONE_VOICE = "en-us"
_PIPER_PHONE_VOICES = frozenset({DEFAULT_PIPER_PHONE_VOICE})
_PHONE_TTS_DEFAULT_VOICE_BY_PROVIDER = {
    "local": DEFAULT_KOKORO_PHONE_VOICE,  # Kokoro
    "piper": DEFAULT_PIPER_PHONE_VOICE,
    "espeak": DEFAULT_ESPEAK_PHONE_VOICE,
}
_PHONE_TTS_PROVIDERS_REQUIRING_EXPLICIT_VOICE = frozenset({"elevenlabs"})


def _append_stream_secret(url: str, secret: str) -> str:
    token = (secret or "").strip()
    if not token:
        return url
    parsed = urlparse(url)
    query_pairs = [
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() != "token"
    ]
    query_pairs.append(("token", token))
    return urlunparse(parsed._replace(query=urlencode(query_pairs)))


def _default_max_concurrent_calls() -> int:
    """Resolve the process-wide outbound call cap.

    This runs before ``AppConfig`` may be convenient to import in all callers,
    so it reads the launch override directly from the process environment.
    ``config.settings`` also declares the same field for cloud code that
    already has the settings singleton available.
    """
    raw = os.environ.get("VIOLA_PHONE_GLOBAL_MAX_CONCURRENT", "").strip()
    if not raw:
        return _DEFAULT_MAX_CONCURRENT_CALLS
    try:
        parsed = int(raw)
    except ValueError:
        logger.warning(
            "Invalid VIOLA_PHONE_GLOBAL_MAX_CONCURRENT=%r; using default %d",
            raw,
            _DEFAULT_MAX_CONCURRENT_CALLS,
        )
        return _DEFAULT_MAX_CONCURRENT_CALLS
    if parsed < 1:
        logger.warning(
            "VIOLA_PHONE_GLOBAL_MAX_CONCURRENT must be >= 1; using default %d",
            _DEFAULT_MAX_CONCURRENT_CALLS,
        )
        return _DEFAULT_MAX_CONCURRENT_CALLS
    return parsed


def _env_flag_enabled(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _default_phone_whisper_model() -> str:
    """Return the canonical phone STT model.

    Phone calls are US/English-first, so default to the smaller/faster
    English-only model. Keep multilingual ``small`` available for
    international testing without changing code.
    """
    explicit = os.environ.get("VIOLA_PHONE_WHISPER_MODEL", "").strip()
    if explicit:
        return explicit
    if _env_flag_enabled(os.environ.get("VIOLA_PHONE_WHISPER_MULTILINGUAL", "")):
        return _MULTILINGUAL_PHONE_WHISPER_MODEL
    return _DEFAULT_PHONE_WHISPER_MODEL


def _default_phone_whisper_compute_type() -> str:
    explicit = os.environ.get("VIOLA_PHONE_WHISPER_COMPUTE_TYPE", "").strip()
    if explicit:
        return explicit
    return _DEFAULT_PHONE_WHISPER_COMPUTE_TYPE


def _default_phone_whisper_device() -> str:
    explicit = os.environ.get("VIOLA_PHONE_WHISPER_DEVICE", "").strip()
    if explicit:
        return explicit
    return _DEFAULT_PHONE_WHISPER_DEVICE


def _default_phone_whisper_beam_size() -> int:
    explicit = os.environ.get("VIOLA_PHONE_WHISPER_BEAM_SIZE", "").strip()
    if not explicit:
        return _DEFAULT_PHONE_WHISPER_BEAM_SIZE
    try:
        parsed = int(explicit)
    except ValueError:
        logger.warning(
            "Invalid VIOLA_PHONE_WHISPER_BEAM_SIZE=%r; using default %d",
            explicit,
            _DEFAULT_PHONE_WHISPER_BEAM_SIZE,
        )
        return _DEFAULT_PHONE_WHISPER_BEAM_SIZE
    if parsed < 1:
        logger.warning(
            "VIOLA_PHONE_WHISPER_BEAM_SIZE must be >= 1; using default %d",
            _DEFAULT_PHONE_WHISPER_BEAM_SIZE,
        )
        return _DEFAULT_PHONE_WHISPER_BEAM_SIZE
    return parsed


def _default_phone_tts_provider() -> str:
    explicit = os.environ.get("VIOLA_PHONE_TTS_PROVIDER", "").strip()
    return explicit or "local"


def _default_phone_stt_streaming() -> str:
    """Streaming-STT first-pass mode for the phone lane (Phase 1 rollout).

    ``VIOLA_PHONE_STT_STREAMING`` in {off, interims, full}:
    - ``off`` (default): today's batch-whisper-only behavior. Fail-open: this is
      also the effective mode whenever the sherpa-onnx runtime or the streaming
      model is absent, so a missing model never breaks calls.
    - ``interims``: run the sherpa-onnx streaming engine as a first pass that emits
      interim transcripts for turn-taking/interruption; whisper keeps final-text
      authority (Phase 1, no accuracy risk).
    - ``full``: reserved for Phase 2 (flip final authority behind a live A/B). Not
      wired in this lane; the pipeline downgrades it to ``interims`` for safety.

    Default ``off`` deliberately: merging Phase 1 changes NOTHING on live calls
    until an operator flips this env var on a box that has provisioned the model
    (``python scripts/download_models.py --streaming-stt``).
    """
    raw = os.environ.get("VIOLA_PHONE_STT_STREAMING", "").strip().lower()
    if raw in {"off", "interims", "full"}:
        return raw
    return "off"


def _normalize_phone_tts_provider(tts_provider: str) -> str:
    provider = (tts_provider or "").strip().lower()
    if provider not in _SUPPORTED_TTS_PROVIDERS:
        raise ValueError("Unsupported phone TTS provider: %s" % tts_provider)
    return provider


def default_phone_tts_voice(tts_provider: str) -> str:
    """Return the provider-aware phone TTS default voice."""
    provider = _normalize_phone_tts_provider(tts_provider)
    voice = _PHONE_TTS_DEFAULT_VOICE_BY_PROVIDER.get(provider)
    if voice:
        return voice
    if provider in _PHONE_TTS_PROVIDERS_REQUIRING_EXPLICIT_VOICE:
        raise ValueError("Phone TTS provider %s requires an explicit phone TTS voice" % provider)
    raise ValueError("No default phone TTS voice for provider: %s" % provider)


def resolve_phone_tts_voice(tts_provider: str, tts_voice: str | None) -> str:
    """Resolve and validate the phone TTS voice for the chosen provider."""
    provider = _normalize_phone_tts_provider(tts_provider)
    voice = (tts_voice or "").strip()
    if not voice:
        voice = default_phone_tts_voice(provider)
    validate_phone_tts_voice(provider, voice)
    return voice


def validate_phone_tts_voice(tts_provider: str, tts_voice: str) -> None:
    """Fail closed when a provider is paired with a voice it cannot speak."""
    provider = _normalize_phone_tts_provider(tts_provider)
    voice = (tts_voice or "").strip()
    if not voice:
        raise ValueError("Phone TTS voice is required for provider: %s" % provider)
    if provider == "piper" and voice not in _PIPER_PHONE_VOICES:
        raise ValueError("Unsupported Piper phone voice: %s" % tts_voice)


@dataclass(frozen=True)
class TelnyxConfig:
    """Configuration for Telnyx-based phone calls.

    Constructed by the caller (intent tool or MCP server) from
    application settings.  Does NOT read env vars directly —
    that's the config layer's job.

    Attributes:
        api_key: Telnyx API v2 key.
        phone_number: Outbound caller ID in E.164 format (+1XXXXXXXXXX).
            Can be a Telnyx number or a verified external number.
        sip_connection_id: Telnyx SIP Connection ID for media routing.
        max_call_duration: Hard cap in seconds (default 600 = 10 min).
        max_concurrent_calls: Global concurrent call limit (default 25).
        answering_machine_detection: Enable Telnyx AMD (default True).
        whisper_model: faster-whisper model size (default "base.en").
        whisper_device: faster-whisper device (default "cpu"; set VIOLA_PHONE_WHISPER_DEVICE=cuda to opt into GPU).
        whisper_compute_type: faster-whisper compute type (default "int8").
        whisper_beam_size: faster-whisper beam size (default 1).
        llm_model: OpenAI-compatible model for conversation (default "gpt-5.4-mini").
        tts_voice: Provider-specific voice ID. Leave empty ("") to inherit the
            provider's default voice (Piper -> "en_US-lessac-medium",
            Kokoro -> "af_heart"); see ``default_phone_tts_voice``.
        openai_api_key: API key for the LLM provider.
        mode: "local" = pipeline runs on user's machine (needs tunnel),
              "cloud" = pipeline runs on api.useviola.com.
        cloud_url: Base URL of the cloud backend (for cloud mode).
        ws_port: Local WebSocket port for Telnyx media (local mode).
        public_ws_url: Public WebSocket URL for Telnyx to connect to.
            In cloud mode, derived from cloud_url.
            In local mode, set to tunnel URL (ngrok/cloudflared).
        stream_shared_secret: Shared secret appended to cloud stream URLs
            as a token query parameter and checked by the cloud media bridge.
    """

    api_key: str
    phone_number: str
    sip_connection_id: str
    openai_api_key: str = ""
    max_call_duration: int = 600
    max_concurrent_calls: int = field(default_factory=_default_max_concurrent_calls)
    answering_machine_detection: bool = True
    whisper_model: str = field(default_factory=_default_phone_whisper_model)
    whisper_device: str = field(default_factory=_default_phone_whisper_device)
    whisper_compute_type: str = field(default_factory=_default_phone_whisper_compute_type)
    whisper_beam_size: int = field(default_factory=_default_phone_whisper_beam_size)
    # Streaming-STT first-pass mode (Phase 1): off | interims | full. See
    # _default_phone_stt_streaming; default off keeps today's whisper-only path.
    phone_stt_streaming: str = field(default_factory=_default_phone_stt_streaming)
    llm_model: str = DEFAULT_PHONE_MODEL
    tts_voice: str = ""  # empty -> resolved per-provider in __post_init__
    mode: str = "cloud"
    cloud_url: str = "https://api.useviola.com"
    ws_port: int = 8770
    public_ws_url: str = ""
    public_webhook_url: str = ""
    stream_shared_secret: str = ""
    audio_recording_enabled: bool = False
    ai_disclosure_enabled: bool = True

    # --- Number pool (multi-user SaaS) ---
    phone_numbers: tuple[str, ...] = ()  # Pool of numbers (empty = use phone_number)

    # --- Cloud STT/TTS providers ---
    stt_provider: str = "local"  # "local" (whisper), "deepgram", "openai"
    tts_provider: str = field(
        default_factory=_default_phone_tts_provider
    )  # "local" (kokoro), "piper", "espeak", "elevenlabs"
    deepgram_api_key: str = ""
    elevenlabs_api_key: str = ""

    # --- Recording storage ---
    # NOTE on secrets (PHONE-09): this dataclass intentionally carries
    # *no* plaintext S3 credential fields. Access/secret keys and the
    # HMAC key-hash secret live in environment variables only and are
    # resolved in ``TelnyxConfig.load_recording_secrets`` so we never
    # serialize, log, or repr them.
    recording_storage: str = "local"  # "local" or "s3"
    recording_s3_bucket: str = ""
    recording_s3_endpoint: str = ""  # For R2: https://<account>.r2.cloudflarestorage.com
    recording_s3_prefix: str = "recordings/"

    def __post_init__(self) -> None:
        tts_provider = _normalize_phone_tts_provider(self.tts_provider)
        if tts_provider != self.tts_provider:
            object.__setattr__(self, "tts_provider", tts_provider)

        voice = resolve_phone_tts_voice(tts_provider, self.tts_voice)
        if voice != self.tts_voice:
            object.__setattr__(self, "tts_voice", voice)

    @property
    def is_configured(self) -> bool:
        """Check if minimum required fields are present."""
        return bool(self.api_key and self.phone_number and self.sip_connection_id)

    @property
    def stream_ws_url(self) -> str:
        """WebSocket URL for Telnyx media streaming.

        In cloud mode: wss://api.useviola.com/ws/phone-media
        In local mode: whatever public_ws_url is set to (tunnel URL),
                       or ws://0.0.0.0:{ws_port} for LAN-only testing.
        """
        if self.public_ws_url:
            return _append_stream_secret(self.public_ws_url, self.stream_shared_secret)
        if self.mode == "cloud":
            base = self.cloud_url.replace("https://", "wss://").replace("http://", "ws://")
            return _append_stream_secret("%s/ws/phone-media" % base, self.stream_shared_secret)
        return "ws://0.0.0.0:%d" % self.ws_port


def load_recording_secrets() -> dict[str, str]:
    """Load S3 recording credentials from environment-only secrets (PHONE-09).

    Secrets never live in ``TelnyxConfig`` and never enter the logs. The
    only supported source is the process environment (TID251 exempt: we
    must read env vars here, but we do so in a narrow, audited function).
    Operators deploying to AWS/R2 should provide the values via the
    platform secret store (AWS Secrets Manager, SOPS, etc.) and inject
    them as env vars at process start.

    Returns a dict with the three secret fields; values are empty strings
    when not configured so the caller can decide how to react. We log the
    *names* of missing fields but never the values.
    """
    import os

    env_keys = (
        "VIOLA_TELNYX_RECORDING_S3_ACCESS_KEY",
        "VIOLA_TELNYX_RECORDING_S3_SECRET_KEY",
        "VIOLA_TELNYX_RECORDING_S3_KEY_HASH_SECRET",
    )
    values = {name: os.environ.get(name, "").strip() for name in env_keys}

    missing = [name for name, v in values.items() if not v]
    if missing:
        logger.debug(
            "Telnyx recording secrets missing from env: %s",
            ",".join(missing),
        )

    return {
        "access_key": values["VIOLA_TELNYX_RECORDING_S3_ACCESS_KEY"],
        "secret_key": values["VIOLA_TELNYX_RECORDING_S3_SECRET_KEY"],
        "key_hash_secret": values["VIOLA_TELNYX_RECORDING_S3_KEY_HASH_SECRET"],
    }
