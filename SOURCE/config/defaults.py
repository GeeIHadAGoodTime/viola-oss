"""
User-tunable default settings.

This module is the single source of truth for defaults that users may override
via settings UI or environment variables.
"""

from __future__ import annotations

# =============================================================================
# UI / VOLUME
# =============================================================================

DEFAULT_VOLUME = 80
DEFAULT_VOLUME_STEP = 10

DEFAULT_UI_MODE = "native_qt"
DEFAULT_THEME = "dark"
DEFAULT_ACCENT_COLOR = (
    "#6B2E1B"  # Mahogany — heritage/audiophile copper. User-customizable via Settings → Preferences → Accent Color.
)
DEFAULT_SHOW_NOTIFICATIONS = True
DEFAULT_LOCALE = "en-US"

# =============================================================================
# DESKTOP COMPUTER USE
# =============================================================================

COMPUTER_USE_APP_WHITELIST_DEFAULT: tuple[str, ...] = ()
COMPUTER_USE_SCREENSHOT_FORMAT_DEFAULT = "png"
COMPUTER_USE_SCREENSHOT_QUALITY_DEFAULT = 80
COMPUTER_USE_MAX_CHARS_PER_TYPE_DEFAULT = 2000
COMPUTER_USE_LOG_WINDOW_TITLES_ENABLED_DEFAULT = False

# =============================================================================
# TRAY / SHORTCUTS / DEBUG TOOLS
# =============================================================================

TRAY_ICON_ENABLED_DEFAULT = True
TRAY_ICON_ACTIVATION_DEFAULT = "start"

SHORTCUTS_ENABLED_DEFAULT = True
SHORTCUTS_ACTIVATION_DEFAULT = "start"

DEBUG_TOOLS_ENABLED_DEFAULT = False
DEBUG_TOOLS_ACTIVATION_DEFAULT = "manual"

# =============================================================================
# MUSIC / AUTOPLAY
# =============================================================================

AUTOPLAY_ENABLED_DEFAULT = True
AI_AUTOPLAY_ENABLED_DEFAULT = True
AUTOPLAY_MIN_QUEUE_DEFAULT = 7

# =============================================================================
# VOICE / STT / TTS
# =============================================================================

# Viola is a voice-first assistant, so a fresh install listens for its name.
#
# This was "push_to_talk" from 2026-04-25 (278b6006b) until now. That revert was
# deliberate and correct at the time: the launch audit
# (docs/codex_audits_2026_04_25/desktop.md) asked for PTT "until the wake path has
# clean first-run model/device/permission UX", because back then the wake model
# might not ship, the mic permission check failed open, and a wake downgrade was
# silent. All three are fixed -- the model is bundled (viola.spec:265 keeps
# temporal_cnn.onnx), the permission check fails closed, and an unavailable wake
# path now publishes a structured degraded voice_status instead of failing quietly
# (bootstrap/factory.py::_resolve_wake_configuration).
#
# Shipping PTT also contradicted our own user documentation, which has always
# stated this default as wake_word (docs/user/SETTINGS.md), so a fresh install did
# not behave the way the product says it behaves.
#
# Safe by construction: bootstrap re-checks the model and PyAudio at startup and
# downgrades to push-to-talk when either is missing (bootstrap/factory.py:932-947),
# so a machine that cannot do wake still gets a working push-to-talk install.
DEFAULT_VOICE_MODE = "wake_word"
DEFAULT_PTT_HOTKEY = "space"
# Mute hotkey: hard-gates the wake/STT pipeline (mic mute), mirroring PTT's
# key-binding shape but deliberately using a different default combo so the
# two device hotkeys never collide out of the box (see cross-field
# validation in ui/settings_api.py).
DEFAULT_MUTE_HOTKEY = "ctrl+m"
DEFAULT_MUTE_HOTKEY_DISPLAY = "Ctrl+M"
MIC_MUTED_DEFAULT = False
DEFAULT_WAKE_SENSITIVITY = 0.90

# Desktop voice is English-first and latency-sensitive; use the smallest
# English-only faster-whisper model unless the user explicitly opts up.
DEFAULT_WHISPER_MODEL = "tiny.en"
DEFAULT_WHISPER_DEVICE = "cpu"

# Valid faster-whisper model identifiers. MUST stay in sync with the
# ``whisper_model`` Literal in config/settings.py (a test asserts this).
# SettingsManager coerces any out-of-set value (e.g. a stray "dark" written by a
# bad settings save) back to DEFAULT_WHISPER_MODEL so an invalid model can never
# silently break STT load. Strengthens the L8 — Settings persistence oracle.
VALID_WHISPER_MODELS = (
    "tiny",
    "tiny.en",
    "base",
    "base.en",
    "small",
    "small.en",
    "medium",
    "medium.en",
    "large",
    "large-v2",
)

TTS_ENABLED_DEFAULT = True
TTS_RATE_DEFAULT = 150
TTS_VOLUME_DEFAULT = 1.0

# =============================================================================
# KNOWLEDGE FOLDER
# =============================================================================
# "vault" stores blobs in a single AES-256-GCM append-only file (default — best
# at-rest encryption story). "folder" writes plaintext bytes to per-user
# subdirs under data_dir so Explorer/Finder can browse them and OS-level
# sync (OneDrive, Dropbox) can pick them up. Folder mode is opt-in and
# documented under Settings → Privacy.
DEFAULT_KNOWLEDGE_STORAGE_MODE = "vault"
# A voice assistant answers out loud. With this False, the only spoken replies came
# from a wake/voice turn (core/voice_command_handler.py::_speak_response); every
# reply to the desktop command box or a push-to-talk turn that reaches
# /v1/command was rendered silently, because that route resolves to a "http"/"web"
# channel and only speaks when this setting is on (ui/api/routes/command.py
# ::_should_speak_reply, added in 9db5961a3).
#
# Measured on a live desktop instance 2026-08-02, same command ("What is the
# capital of France?"), same WASAPI loopback capture of the real speaker:
#   speak_all_replies=False -> 0 non-zero samples in 45s, -91.0 dB mean AND max
#   speak_all_replies=True  -> a 0.65s utterance, -27.3 dB RMS, -10.8 dB peak
# The text reply was identical both times, so this setting was the whole
# difference between an assistant that talks and one that does not.
#
# Remote channels are unaffected: telegram / sms / phone / email are excluded at
# the route, so a message from elsewhere still never drives this machine's speaker,
# and voice turns stay owned by the voice handler so nothing is spoken twice.
# The user's own `voice_muted` and `tts_enabled` still win over this.
SPEAK_ALL_REPLIES_DEFAULT = True
VOICE_MUTED_DEFAULT = False

# =============================================================================
# AI
# =============================================================================

# Canonical managed model for first-party Viola execution.
# Keep the base/default, agent, and phone paths aligned to one value so
# settings migrations and provider fallbacks cannot silently drift apart.
DEFAULT_MANAGED_MODEL = "gpt-5.4-mini"

# Low-cost internal background model for summarization / extraction /
# compaction. This is not the user-facing route or agent default.
DEFAULT_BACKGROUND_TASK_MODEL = "gpt-5-nano"

# Base/default model for managed OpenAI execution.
DEFAULT_GPT_MODEL = DEFAULT_MANAGED_MODEL

# Default model for the Codex (ChatGPT subscription) source. Must be a model
# available to ALL Codex users — gpt-5.3-codex-spark is a ChatGPT *max-plan*-only
# model, so it cannot be the default (a normal Codex user would be defaulted to a
# model they cannot access). Spark stays a *selectable* option for max-plan users
# (see the popular/available model lists); it is just not the default.
DEFAULT_CODEX_MODEL = "gpt-5.4-mini"

# AI source: "managed" (Viola business key), "codex" (ChatGPT subscription),
# "byok" (bring your own API key), or "local" (on-device / localhost AI).
DEFAULT_AI_SOURCE = "managed"

# Provider-level managed-mode fallback is opt-in. Subscription/managed mode
# must not silently continue through Codex or another paid provider unless a
# deployment explicitly enables and configures that fallback.
DEFAULT_LLM_FALLBACK_ENABLED = False
DEFAULT_LLM_FALLBACK_CHAIN: tuple[str, ...] = ("managed",)
DEFAULT_LOCAL_LLM_ENABLED = False
DEFAULT_LOCAL_LLM_MODEL = "qwen3.5-9b-q4_k_m"

# Agent model — the user-facing model for voice commands and agent tasks.
DEFAULT_AGENT_MODEL = DEFAULT_MANAGED_MODEL

# Phone model — used in Pipecat telephony pipeline.
DEFAULT_PHONE_MODEL = DEFAULT_MANAGED_MODEL
CALL_HISTORY_RETENTION_DAYS_DEFAULT = 30
REQUIRE_ACCOUNT_FOR_PAID_ACTIONS_DEFAULT = True
PHONE_RECORD_CALLS_DEFAULT = True
PHONE_KEEP_TRANSCRIPT_DEFAULT = True
PHONE_ANNOUNCE_AI_ON_CALLS_DEFAULT = False
PHONE_AI_IDENTITY_ENFORCEMENT_DEFAULT = False

# Provider-aware route/agent defaults. Blank means the caller must supply a
# model or fail fast at validation/provider creation time. Custom
# OpenAI-compatible backends deliberately stay blank because Viola cannot infer
# a safe universal model name.
DEFAULT_MODEL_BY_PROVIDER: dict[str, str] = {
    "openai": DEFAULT_GPT_MODEL,
    "anthropic": "claude-haiku-4-5-20251001",
    "google": "gemini-2.5-flash",
    "ollama": "llama2",
    "local": "llama2",
    "openai_compatible": "",
}

DEFAULT_AGENT_MODEL_BY_PROVIDER: dict[str, str] = {
    "openai": DEFAULT_AGENT_MODEL,
    "anthropic": "claude-sonnet-4-5-20250929",
    "google": "gemini-2.5-flash",
    "ollama": "llama2",
    "local": "llama2",
    "openai_compatible": "",
}


def get_provider_default_model(provider: str) -> str:
    """Return the canonical route/conversation model for a provider."""
    normalized = (provider or "").strip().lower()
    return DEFAULT_MODEL_BY_PROVIDER.get(normalized, "")


def get_provider_default_agent_model(provider: str) -> str:
    """Return the canonical agent-tier model for a provider."""
    normalized = (provider or "").strip().lower()
    return DEFAULT_AGENT_MODEL_BY_PROVIDER.get(normalized, "")


def get_default_model_for_source(ai_source: str, provider: str, *, agent: bool = False) -> str:
    """Resolve the effective default model for an AI source + provider pair."""
    normalized_source = (ai_source or DEFAULT_AI_SOURCE).strip().lower()
    if normalized_source == "codex":
        return DEFAULT_CODEX_MODEL
    if normalized_source in {"managed", "subscription"}:
        return DEFAULT_AGENT_MODEL if agent else DEFAULT_GPT_MODEL
    if agent:
        return get_provider_default_agent_model(provider)
    return get_provider_default_model(provider)


def resolve_effective_model(
    *,
    ai_source: str = DEFAULT_AI_SOURCE,
    provider: str = "openai",
    agent: bool = False,
    candidates: tuple[str | None, ...] = (),
    fallback: str = "",
) -> str:
    """Return the effective model from explicit candidates plus canonical defaults.

    This helper is intentionally pure: callers provide any configured candidates
    in precedence order, and the helper returns the first non-empty string.
    If no candidate is set, it falls back to the canonical default for the
    ``ai_source`` / ``provider`` / ``agent`` tuple, then finally to an explicit
    fallback string when provided.
    """
    for candidate in candidates:
        if isinstance(candidate, str):
            normalized = candidate.strip()
            if normalized:
                return normalized

    default_model = get_default_model_for_source(ai_source, provider, agent=agent)
    if default_model:
        return default_model

    return fallback.strip() if isinstance(fallback, str) else ""


# Reasoning effort tiers (gpt-5.x reasoning models only).
#
# Per-family valid values (from OpenAI API error responses):
#   gpt-5.x:             "minimal" | "low" | "medium" | "high"
#   gpt-5.3-codex-spark:             "low" | "medium" | "high" | "xhigh"
#   gpt-5.4.x:           "none"    | "low" | "medium" | "high" | "xhigh"
# "none" and "minimal" are equivalent — near-zero thinking tokens.
# Use ``resolve_reasoning_effort`` to map a semantic value to the
# correct per-family API string.
#
# Tier selection bakeoff: data/bakeoff/routing_latency_20260419_041657.md
#   - gpt-5.4-mini @ effort=none → 602ms p50 total, 100% routing accuracy
#   - previous default "low" on routing burnt reasoning tokens for no benefit
DEFAULT_ROUTING_REASONING_EFFORT = "none"  # ASK/ROUTE/conversational
DEFAULT_AGENT_REASONING_EFFORT = "medium"  # interactive root agent tier
DEFAULT_BACKGROUND_AGENT_REASONING_EFFORT = "high"  # delegated child agents
# Phone Pipecat pipeline. Two things about this value are commonly misremembered:
#
# 1. It is NOT free. "low" costs roughly a second per turn versus "none" (none p50
#    0.63-0.73s, low 1.70-2.12s at the real production request shape). The 2026-06-17
#    "at no latency cost" claim is REFUTED: that bench's "full phone prompt" was one
#    prompt component with 2 toy tools, not the real instruction with ~26 tools.
# 2. It is NOT held for consult determinism. The 2026-06-17 flip cited "none was
#    non-deterministic" about consulting the owner on mid-call choices, but that was
#    measured on the PRE-HOIST prompt; the same commit hoisted the consult guidance and
#    recorded hoisted-at-none as 32/32. Re-measured 2026-08-08 on the loopback oracle at
#    n=48 per setting (l17_consult_owner_mid_call): none and low BOTH fired consult_user
#    48/48, invented a preference 0/48, and wrote the correct slot 48/48.
#
# What "low" does hold is the PRE-CONSULT SPOKEN BRIDGE (the dead-air fix in d1cb79a23,
# after a real call left the recipient in ~24s of silence): low spoke the bridge 48/48,
# none only 42/48 (Fisher two-tailed p=0.027), and 3/48 of none's runs said nothing at
# all. That is the sole surviving reason this is "low" and not "none".
#
# So the flip to "none" is worth ~1s/turn and is blocked only on making that bridge hold
# at "none" -- a prompt fix, never a runtime classifier (candidate C-1114). A probe of
# hoisted+strengthened bridge guidance reached 24/24 at "none", but it pushed the
# owner-takeover item past the L17 first-7-items consult ordering constraint, so the
# landable shape is still open.
#
# Never hard-code an effort literal in telephony/; gate: phone-reasoning-effort-low-latency.
DEFAULT_PHONE_REASONING_EFFORT = "low"
DEFAULT_CODEX_REASONING_EFFORT = "medium"  # ChatGPT Plus subscription path
AUTO_PROPOSE_SHORTCUTS_DEFAULT = False

# Legacy alias — remaining call sites fall back here. New code should
# pick the tier-specific constant explicitly.
DEFAULT_REASONING_EFFORT = DEFAULT_AGENT_REASONING_EFFORT
ENABLE_GPT_DEFAULT = True

VALID_REASONING_EFFORTS = frozenset(
    {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    }
)


def resolve_reasoning_effort(effort: str, model: str) -> str:
    """Map a semantic reasoning-effort value to the per-model-family API value.

    "none" and "minimal" are the same semantic (skip hidden thinking).  Which
    string the API accepts depends on the model family — gpt-5.x rejects
    "none"; gpt-5.3-codex-spark rejects both "none" and "minimal"; gpt-5.4.x
    rejects "minimal".  Other values (low/medium/high/xhigh) pass through
    unchanged.
    """
    if not effort:
        return effort
    m = (model or "").lower()
    is_codex_spark = "gpt-5.3-codex-spark" in m
    is_5_4 = "gpt-5.4" in m
    if effort in {"none", "minimal"} and is_codex_spark:
        return "low"
    if effort == "none" and not is_5_4:
        return "minimal"
    if effort == "minimal" and is_5_4:
        return "none"
    return effort


def _is_reasoning_family(model: str | None) -> bool:
    """Return True when *model* supports the reasoning config surface."""
    if not model:
        return False
    lower = model.lower()
    return any(tag in lower for tag in ("gpt-5", "o1", "o3", "o4"))


def get_configured_reasoning_effort(
    setting_or_tier: str,
    default_or_model: str | None = None,
    model: str | None = None,
) -> str:
    """Return the effective reasoning effort for either legacy or tier calls.

    Supported forms:
    - ``get_configured_reasoning_effort(setting_key, default, model)``
    - ``get_configured_reasoning_effort(tier, model)``
    """
    field_map = {
        "ask": "routing_reasoning_effort",
        "route": "routing_reasoning_effort",
        "routing": "routing_reasoning_effort",
        "conversation": "routing_reasoning_effort",
        "agent": "agent_reasoning_effort",
        "background_agent": "background_agent_reasoning_effort",
        "child_agent": "background_agent_reasoning_effort",
        "phone": "phone_reasoning_effort",
        "codex": "codex_reasoning_effort",
    }
    default_map = {
        "routing_reasoning_effort": DEFAULT_ROUTING_REASONING_EFFORT,
        "agent_reasoning_effort": DEFAULT_AGENT_REASONING_EFFORT,
        "background_agent_reasoning_effort": DEFAULT_BACKGROUND_AGENT_REASONING_EFFORT,
        "phone_reasoning_effort": DEFAULT_PHONE_REASONING_EFFORT,
        "codex_reasoning_effort": DEFAULT_CODEX_REASONING_EFFORT,
    }

    if model is None:
        tier_key = (setting_or_tier or "").strip().lower()
        field_name = field_map.get(tier_key, "routing_reasoning_effort")
        default_value = default_map[field_name]
        effective_model = default_or_model
    else:
        field_name = setting_or_tier
        default_value = default_or_model or DEFAULT_REASONING_EFFORT
        effective_model = model

    if effective_model and not _is_reasoning_family(effective_model):
        return "none"

    try:
        from ui.settings_manager import get_settings_manager

        sm = get_settings_manager()
        effort = sm.get(field_name, default_value)
        if isinstance(effort, str) and effort:
            candidate = effort.strip().lower()
            if candidate in VALID_REASONING_EFFORTS:
                return resolve_reasoning_effort(candidate, effective_model or "")
        if field_name == "routing_reasoning_effort":
            legacy_effort = sm.get("reasoning_effort", "")
            if isinstance(legacy_effort, str) and legacy_effort:
                candidate = legacy_effort.strip().lower()
                if candidate in VALID_REASONING_EFFORTS:
                    return resolve_reasoning_effort(candidate, effective_model or "")
    except Exception:
        return resolve_reasoning_effort(default_value, effective_model or "")

    return resolve_reasoning_effort(default_value, effective_model or "")


def pipecat_model_extra(model: str, effort: str | None = None) -> dict:
    """Extra API params for Pipecat's OpenAILLMService.

    gpt-5.x and o3/o4 reasoning models need ``reasoning`` params to avoid
    leaking thinking tokens into TTS output.  Returns an empty dict for
    non-reasoning models.
    """
    if any(tag in model.lower() for tag in ("gpt-5", "o3", "o4-")):
        selected = (
            resolve_reasoning_effort(effort, model) if effort else get_configured_reasoning_effort("phone", model)
        )
        return {"reasoning": {"effort": selected, "summary": "auto"}}
    return {}


def pipecat_phone_model_extra(
    model: str,
    effort: str | None = None,
    *,
    tools_present: bool = False,
) -> dict:
    """Extra API params for phone/browser Pipecat sessions.

    Pipecat's OpenAILLMService uses Chat Completions, which accepts
    ``reasoning_effort`` rather than the Responses-style ``reasoning`` object.
    OpenAI rejects ``reasoning_effort`` when function tools are present on
    gpt-5.4-mini Chat Completions, so tool-bearing phone calls must omit it.
    """
    if tools_present:
        return {}
    if any(tag in model.lower() for tag in ("gpt-5", "o3", "o4-")):
        selected = (
            resolve_reasoning_effort(effort, model) if effort else get_configured_reasoning_effort("phone", model)
        )
        return {"reasoning_effort": selected}
    return {}


# =============================================================================
# PRIVACY CONSENT (opt-in required for all cloud data transmission)
# =============================================================================

# Cloud STT: user must consent before audio is sent to cloud transcription
CONSENT_CLOUD_STT_DEFAULT = False

# Cloud sync: user must consent before player state is sent to cloud relay
CONSENT_CLOUD_SYNC_DEFAULT = False

# Sentry error reporting: user must consent before error data is sent to Sentry
CONSENT_ERROR_REPORTING_DEFAULT = False

# Sentry session replay: user must opt in before masked replay data is recorded
CONSENT_SESSION_REPLAY_DEFAULT = False

# Vision clipboard: user must consent before clipboard text is attached to
# screen-awareness context sent to the vision LLM
CONSENT_VISION_CLIPBOARD_DEFAULT = False

# =============================================================================
# AUDIO RETENTION
# =============================================================================

# Whether wake audio logging (FP collection) is enabled at all
WAKE_AUDIO_LOGGING_ENABLED_DEFAULT = False

# Retention period in hours (files older than this are auto-deleted)
WAKE_AUDIO_RETENTION_HOURS_DEFAULT = 24

# How often (in seconds) to run cleanup of old audio files
WAKE_AUDIO_CLEANUP_INTERVAL_DEFAULT = 3600

# =============================================================================
# COMPANION DEVICE PAIRING
# =============================================================================

# Whether this desktop pairs as a companion device of the user's Viola cloud
# account. When enabled AND signed in, the cloud/browser/phone service can
# use the desktop's local features (on-disk music library, LAN smart home).
#
# Default ON, because signing in on the desktop IS the pairing act and the
# shipped relay UI already describes this as the default with no toggle
# (ui/react-app/src/components/DesktopRelayIndicator.jsx). A signed-out
# desktop still pairs with nothing: the companion client idles until the
# desktop's own GoTrue session exists, and it can only ever reach the one
# account that session belongs to. Kept as a setting so an install can opt
# out, not as a gate the product expects users to find and switch on -- it
# was default-off and unwritable by any shipped surface, which made desktop
# pairing unreachable for every real user.
COMPANION_ENABLED_DEFAULT = True

# Explicit override for the cloud-account bearer the companion client
# registers and connects with. Normally EMPTY and unused: the credential
# comes from the desktop's own signed-in GoTrue session, which refreshes
# itself past the 300 s access-token expiry (see
# services/companion_client/config.py). This setting exists only for a
# surface with no desktop sign-in (headless rig, test harness), is consulted
# only when no live session exists, and is never sent to the cloud as a
# setting (see settings_schema -- it relocates to the credential vault).
COMPANION_CLOUD_TOKEN_DEFAULT = ""

# Friendly device name advertised to the cloud. Empty -> auto-generated from
# the hostname (e.g. "Viola Desktop (HOSTNAME)").
COMPANION_DEVICE_NAME_DEFAULT = ""
