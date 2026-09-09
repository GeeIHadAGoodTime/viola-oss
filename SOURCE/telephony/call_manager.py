"""Phone Call Manager — Pipecat-based voice AI phone calls.

Manages the full lifecycle of outbound phone calls:
    dial → conversation → transcript → summary

Uses Pipecat framework with:
    - Telnyx Call Control API (outbound dialing)
    - TelnyxTransport (WebSocket media bridge — see telnyx_transport.py)
    - Local faster-whisper STT (speech recognition, MKL single-threaded)
    - canonical phone LLM (conversation intelligence)
    - Local Kokoro TTS (speech synthesis)

Architecture:
    CallManager creates a Pipecat Pipeline per call. The pipeline wires:
        TelnyxInput → VAD → WhisperSTT → ContextAggregator(user) →
        LLM → TTS → TelnyxOutput → ContextAggregator(assistant)

    Telnyx streams bidirectional PCMU audio over WebSocket; the transport
    resamples between the 8 kHz wire format and Viola's 16 kHz pipeline.
    Our TelnyxTransport bridges that to Pipecat's frame processing.

    Conversation flow: once Telnyx media connects, the phone model opens from
    the unified phone context and a short first-opening guard prevents repeated
    hello turns from starving the first audible TTS frame.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import contextvars
import importlib
import inspect
import json
import math
import os
import re
import shutil
import signal
import socket
import sqlite3
import threading
import time
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from config import defaults
from core.constants import (
    BIND_ALL_INTERFACES,
    LOCALHOST,
    LOCALHOST_NAME,
    TIMEOUT_SHUTDOWN,
)
from core.cpu_quota import effective_cpu_count
from core.exceptions import ErrorContext, ErrorSeverity, ServiceError
from core.logging_config import get_logger
from core.platform import configure_environment, get_cache_dir, get_data_dir
from core.user_profile import build_phone_info_manifest
from telephony.call_context import (
    build_phone_system_instruction,
    build_phone_volatile_context,
)
from telephony.call_disclosures import (
    PHONE_CALL_RETENTION_DAYS,
    detect_called_party_opt_out,
    detect_persistence_objection,
)
from telephony.call_queue import PhoneCallQueue, QueuedOutboundCall
from telephony.call_tools import (
    PIPELINE_FUNCTION_TIMEOUT_MARGIN_SECS,
    consult_user_pipeline_timeout_secs,
)
from telephony.caller_validation import is_assistant_caller_name, validate_caller_name
from telephony.config import TelnyxConfig
from telephony.number_pool import NumberPool
from telephony.number_validation import normalize_us_e164
from telephony.openai_chat import phone_chat_completion_options
from telephony.opt_out import add_opt_out, is_opted_out
from telephony.phone_latency_trace import (
    PhoneLatencyTraceProcessor,
    PhoneLatencyTraceRecorder,
    PhoneLatencyTraceWriter,
)
from telephony.phone_stt_options import (
    PHONE_STT_NO_SPEECH_THRESHOLD,
    phone_pcm_to_whisper_float,
    phone_whisper_transcribe_options,
)
from telephony.phone_tos import get_phone_tos
from telephony.precall_planner import (
    merge_info_manifests,
    plan_phone_call_prerequisites,
)
from telephony.recording_storage import RecordingStorage, create_recording_storage
from telephony.remote_voice import maybe_remote_first_kokoro, remote_transcribe_pcm
from telephony.telnyx_dial_response import telnyx_dial_call_control_id
from telephony.usage import company_phone_billing_available, get_phone_billing
from telephony.user_settings_lookup import load_cloud_user_settings_blob
from telephony.voicemail_detection import PipecatVoicemailDetectionHandler

logger = get_logger(__name__)

_TRANSCRIPT_LOG_PREVIEW_CHARS = 50
_PHONE_NUMBER_LOG_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{6,}\d)(?!\d)")
PHONE_TRANSCRIPT_DIR = get_data_dir() / "phone_transcripts"
_KOKORO_MODEL_FILENAME = "kokoro-v1.0.onnx"
_KOKORO_VOICES_FILENAME = "voices-v1.0.bin"
# Multi-tenant: per-user partition under PHONE_TRANSCRIPT_DIR.  See
# ``_user_transcript_dir`` — transcripts are stored under
# ``phone_transcripts/by_user/<sha256(user_id)>/<call_id>.json`` so a
# call id known to one tenant cannot reach into another tenant's file.
_PHONE_TRANSCRIPT_USER_PARTITION = "by_user"


def _hash_user_id(user_id: str | None) -> str:
    """Return a stable hex-hash of ``user_id`` for filesystem partitioning."""
    import hashlib

    raw = (user_id or "").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _user_transcript_dir(user_id: str | None, transcript_dir: Path | None = None) -> Path:
    """Return the per-user transcript directory.

    A missing ``user_id`` is refused — callers must resolve the owner
    before reading or writing.  This raises ``ValueError`` rather than
    returning the legacy unpartitioned path so we never silently fall
    back to a global location.
    """
    if not user_id:
        raise ValueError("phone transcript directory requires a user_id")
    base = (transcript_dir or PHONE_TRANSCRIPT_DIR) / _PHONE_TRANSCRIPT_USER_PARTITION
    return base / _hash_user_id(user_id)


_AUTO_TIMEZONE_SENTINELS = frozenset({"auto", "user.timezone"})


def _clean_timezone_setting(value: object, *, allow_auto: bool = False) -> str | None:
    text = str(value or "").strip()
    if text.lower() in _AUTO_TIMEZONE_SENTINELS and not allow_auto:
        return None
    return text or None


def _first_timezone_setting(
    settings_blob: dict[str, object], keys: tuple[str, ...], *, allow_auto: bool = False
) -> str | None:
    for key in keys:
        resolved = _clean_timezone_setting(settings_blob.get(key), allow_auto=allow_auto)
        if resolved is not None:
            return resolved
    return None


@dataclass(frozen=True)
class _PhoneCallSetupSettings:
    record_phone_calls: bool = defaults.PHONE_RECORD_CALLS_DEFAULT
    keep_phone_transcript: bool = defaults.PHONE_KEEP_TRANSCRIPT_DEFAULT
    announce_ai_on_calls: bool = defaults.PHONE_ANNOUNCE_AI_ON_CALLS_DEFAULT
    phone_ai_identity_enforcement: bool = defaults.PHONE_AI_IDENTITY_ENFORCEMENT_DEFAULT


def _record_phone_calls_override_value(raw_value: object) -> bool | None:
    text = (str(raw_value or "")).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return None


def _phone_call_setup_settings_from_blob(
    settings_blob: dict[str, object],
    *,
    record_phone_calls_override: bool | None = None,
) -> _PhoneCallSetupSettings:
    record_phone_calls = defaults.PHONE_RECORD_CALLS_DEFAULT
    keep_phone_transcript = defaults.PHONE_KEEP_TRANSCRIPT_DEFAULT
    announce_ai_on_calls = defaults.PHONE_ANNOUNCE_AI_ON_CALLS_DEFAULT
    phone_ai_identity_enforcement = defaults.PHONE_AI_IDENTITY_ENFORCEMENT_DEFAULT

    if record_phone_calls_override is None:
        record_phone_calls = settings_blob.get("record_phone_calls", defaults.PHONE_RECORD_CALLS_DEFAULT) is True
    else:
        record_phone_calls = record_phone_calls_override
    keep_phone_transcript = (
        settings_blob.get("keep_phone_transcript", defaults.PHONE_KEEP_TRANSCRIPT_DEFAULT) is not False
    )
    announce_ai_on_calls = (
        settings_blob.get("announce_ai_on_calls", defaults.PHONE_ANNOUNCE_AI_ON_CALLS_DEFAULT) is True
    )
    phone_ai_identity_enforcement = (
        settings_blob.get(
            "phone_ai_identity_enforcement",
            defaults.PHONE_AI_IDENTITY_ENFORCEMENT_DEFAULT,
        )
        is True
    )
    return _PhoneCallSetupSettings(
        record_phone_calls=record_phone_calls,
        keep_phone_transcript=keep_phone_transcript,
        announce_ai_on_calls=announce_ai_on_calls,
        phone_ai_identity_enforcement=phone_ai_identity_enforcement,
    )


async def _resolve_phone_call_setup_settings(
    user_id: str | None,
    *,
    cloud_mode: bool,
    record_phone_calls_override: object = None,
) -> _PhoneCallSetupSettings:
    override = _record_phone_calls_override_value(record_phone_calls_override)
    if user_id:
        cloud_settings = await load_cloud_user_settings_blob(user_id, fail_closed=cloud_mode)
        if cloud_settings is not None:
            return _phone_call_setup_settings_from_blob(
                cloud_settings,
                record_phone_calls_override=override,
            )

    try:
        from ui.settings_manager import get_settings_manager

        settings_manager = get_settings_manager()
        if override is None:
            record_phone_calls = (
                settings_manager.get(
                    key="record_phone_calls",
                    user_id=user_id,
                    default=defaults.PHONE_RECORD_CALLS_DEFAULT,
                )
                is True
            )
        else:
            record_phone_calls = override
        return _PhoneCallSetupSettings(
            record_phone_calls=record_phone_calls,
            keep_phone_transcript=(
                settings_manager.get(
                    key="keep_phone_transcript",
                    user_id=user_id,
                    default=defaults.PHONE_KEEP_TRANSCRIPT_DEFAULT,
                )
                is not False
            ),
            announce_ai_on_calls=(
                settings_manager.get(
                    key="announce_ai_on_calls",
                    user_id=user_id,
                    default=defaults.PHONE_ANNOUNCE_AI_ON_CALLS_DEFAULT,
                )
                is True
            ),
            phone_ai_identity_enforcement=(
                settings_manager.get(
                    key="phone_ai_identity_enforcement",
                    user_id=user_id,
                    default=defaults.PHONE_AI_IDENTITY_ENFORCEMENT_DEFAULT,
                )
                is True
            ),
        )
    except (
        AttributeError,
        ImportError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        logger.debug("Phone call setup SettingsManager lookup failed: %s", exc)
        return (
            _PhoneCallSetupSettings(record_phone_calls=override) if override is not None else _PhoneCallSetupSettings()
        )


async def _resolve_phone_context_timezone_name(user_id: str | None, *, cloud_mode: bool = False) -> str | None:
    cloud_settings_seen = False
    if user_id:
        cloud_settings = await load_cloud_user_settings_blob(user_id, fail_closed=cloud_mode)
        if cloud_settings is not None:
            cloud_settings_seen = True
            resolved = _first_timezone_setting(
                cloud_settings,
                ("timezone", "calendar_timezone", "quiet_hours_timezone"),
            )
            if resolved is not None:
                return resolved
        elif cloud_mode:
            raise RuntimeError("Cloud phone context timezone lookup requires the cloud settings repository")

    if not cloud_settings_seen:
        try:
            from ui.settings_manager import get_settings_manager

            settings_manager = get_settings_manager()
            for key in ("timezone", "calendar_timezone", "quiet_hours_timezone"):
                value = settings_manager.get(key=key, user_id=user_id, default=None)
                resolved = _clean_timezone_setting(value)
                if resolved is not None:
                    return resolved
        except (
            AttributeError,
            ImportError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            logger.debug("Phone context timezone SettingsManager lookup failed: %s", exc)

    try:
        from config.settings import settings as _app_settings

        for key in ("calendar_timezone", "quiet_hours_timezone"):
            resolved = _clean_timezone_setting(getattr(_app_settings, key, None), allow_auto=True)
            if resolved is not None:
                return resolved
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError) as exc:
        logger.debug("Phone context timezone AppConfig lookup failed: %s", exc)
    return None


_STREAM_URL_PREFLIGHT_TIMEOUT_SECS = TIMEOUT_SHUTDOWN
_WSS_DEFAULT_PORT = 443
_LOCAL_STREAM_HOSTS = frozenset({BIND_ALL_INTERFACES, LOCALHOST, LOCALHOST_NAME, "::1"})
_STREAM_URL_SECRET_QUERY_TOKENS = frozenset({"secret", "signature", "token"})

# -- Unified billing policy (PHONE-05/06/11) ---------------------------------
# Failed calls can still cost Telnyx money (call setup, carrier fees), so
# apply a tiny cost floor without inflating the user's actual duration ledger.
_MIN_BILLED_COST_USD = 0.01
_BILLABLE_FAILURE_STATES: frozenset[str] = frozenset({"failed", "timeout", "no_answer", "cancelled"})

# Module-level loguru bridge (installed once per process)
_loguru_bridge_installed = False
_loguru_bridge_handler_id: int | None = None

# Phone STT warm cache. Pipecat processor instances are stateful across
# pipeline cancellation, so we cache the expensive faster-whisper model and
# create fresh STT processors per call.
_PHONE_STT_PRELOAD_DISABLED_VALUES = {"0", "false", "no", "off"}
_PHONE_STT_CPU_FALLBACK_DEVICE = "cpu"
_PHONE_STT_CPU_FALLBACK_COMPUTE_TYPE = "int8"
_PHONE_TTS_PRELOAD_DISABLED_VALUES = _PHONE_STT_PRELOAD_DISABLED_VALUES
_PHONE_TTS_PROVIDER_AUTO = "auto"
_PHONE_TTS_PROVIDER_CUDA = "cuda"
_PHONE_TTS_PROVIDER_CPU = "cpu"
_PHONE_TTS_CUDA_EP = "CUDAExecutionProvider"
_PHONE_TTS_CPU_EP = "CPUExecutionProvider"
_PHONE_TTS_WARMUP_TEXT = "OK."
_PHONE_STT_MAX_BIAS_TERMS = 96
_PHONE_STT_INITIAL_PROMPT_MAX_CHARS = 480
_PHONE_STT_GENERIC_HOTWORDS: tuple[str, ...] = (
    "yes",
    "no",
    "okay",
    "bye",
    "hello",
    "thanks",
    "please",
    "hold",
    "transfer",
    "appointment",
    "reservation",
    "schedule",
    "order",
    "pickup",
    "delivery",
    "large",
    "larges",
    "extra",
    "medium",
    "small",
    "total",
    "ready",
    "confirmation",
    "number",
    "try",
    "again",
)
_PHONE_STT_HOTWORD_STOPWORDS = frozenset(
    {
        "and",
        "are",
        "but",
        "for",
        "from",
        "have",
        "one",
        "the",
        "this",
        "that",
        "with",
        "you",
        "your",
    }
)
_PHONE_STT_HOTWORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'_-]{1,}")
# SMART-TURN DESIGN POINT (2026-07-04). Pipecat's LocalSmartTurnAnalyzerV3 docs:
# "functions optimally when the VAD analyzer is configured with a short stop_secs
# value, with a recommended default of 0.2 seconds." The Smart-Turn model, not the
# raw VAD silence timer, decides end-of-turn semantically; a longer VAD stop_secs
# just adds fixed dead time to every endpoint before Smart-Turn even runs. 0.2 is
# the vendor design point and shaves ~200ms off post-speech-to-first-audio on every
# turn. This constant feeds BOTH the VADParams and the SmartTurnParams below (and
# the loopback rig), so Silero VAD and Smart Turn stay on the same short window.
PHONE_VAD_SILENCE_SECS = 0.2
# Less-yielding interruption threshold (2026-06-24). While Viola is speaking, the
# recipient must say at least this many words for their turn to START and flush
# Viola's pending turn; a 1-2 word backchannel ("yeah", "uh-huh", "mm-hm") does
# NOT interrupt her. When Viola is NOT speaking, MinWordsUserTurnStartStrategy
# falls back to 1 word internally, so ordinary back-and-forth keeps single-word
# latency. 3 rejects backchannels while still yielding to a genuine multi-word
# interruption ("stop, wait, I have a question"). Consumed where the phone
# pipeline builds UserTurnStrategies(start=[MinWordsUserTurnStartStrategy(...)]).
PHONE_INTERRUPTION_MIN_WORDS = 3
# Inbound runs at Telnyx's native telephone rate (8 kHz PCMU). Upsampling to
# 16 kHz made Silero VAD deaf to real recipient speech (2026-06-24 incident);
# at 8 kHz the model crosses its threshold on the same audio. faster-whisper
# transcribes 8 kHz natively, so STT is unaffected. Keep this in sync with
# telephony.telnyx_transport.TELNYX_SAMPLE_RATE.
PHONE_INBOUND_SAMPLE_RATE = 8000
# --- Remote voice worker (RunPod serverless STT/TTS) warm-state timings ---
# The remote worker scales to zero; only an ACTUAL inference resets its ~10s idle
# timer, and only a warmup inference RETURNING is a reliable warm signal (a worker
# can report "ready" while its model is still cold). We warm it the instant the
# user confirms the call (post-approval) so the OPENING turn runs warm, gate the
# dial on the returned warm signal (not a timer), and keep-alive-ping under the
# idle timeout so a slow warm can't let the worker recool while we wait. See
# telephony/remote_voice.py (warm_remote_voice).
#
# FOUNDER PRODUCT DECISION (2026-07-05): PREFER waiting until the worker is
# genuinely warm and delivering a warm call from the first word, over dialing fast
# into a cold call. The measured real cold-start is 16-37s (RunPod whisper workers
# run 15-45s cold), so the old 10s ceiling timed out BEFORE warm and dialed cold —
# the bad outcome. The whole wait happens BEFORE the recipient is dialed, so only
# the Viola user waits (shown a persistent "preparing" cue), and a 20-40s wait is
# acceptable for a delegated call. The ceiling therefore spans the full cold-start
# window; the gate still opens the INSTANT the real warm signal fires (early exit),
# and only a genuinely stuck/down worker hits the cap and dials anyway onto the
# existing remote_voice recovery + cooldown-decoupling net. It is a real-signal
# gate, never a fixed sleep.
REMOTE_WARM_DIAL_CEILING_S = 60.0  # max wait at the dial gate; spans the real 16-37s cold start before dialing anyway
REMOTE_WARM_KEEPALIVE_INTERVAL_S = (
    6.0  # < 10s idle timeout, margin for ping+jitter; holds warmth across the full ceiling wait
)
REMOTE_WARM_PING_TIMEOUT_S = 8.0  # per warmup inference; covers a ~7s cold start
# Cadence for re-emitting the "preparing to call" cue on the EXISTING call-lifecycle
# WS stream (call_started) while the dial gate is open. A single event then a long
# (up to ceiling) silence reads as frozen; a periodic refresh reads as working. Well
# under the wait, not spammy; idempotent on the frontend (refreshes the live-call
# meta, never resets the elapsed timer or re-opens the tab).
REMOTE_WARM_PREPARING_REFRESH_S = 5.0
# --- Phone pipeline idle-timeout budget (PHONE-15, revised 2026-07-06) ---
# Telnyx bills per second, but ONLY once the call CONNECTS. The pipeline's idle
# monitor (pipecat PipelineTask) starts a single timeout clock at the StartFrame
# — which fires when the pipeline STARTS, before the pre-dial warm gate and
# before the dial — and resets ONLY on real speech (Bot/UserSpeakingFrame). So
# the whole pre-first-audio window runs on that one clock with no reset:
#     warm-gate wait     (<= REMOTE_WARM_DIAL_CEILING_S)
#   + media-connect wait (<= PHONE_MEDIA_CONNECT_TIMEOUT_S)
#   + opening-TTS latency (a few seconds)
# If idle_timeout_secs <= that window the monitor cancels the call MID-WAIT.
# The original PHONE-15 flat 60s did exactly that: it equalled the 60s warm
# ceiling, so a real cold start (warm 26-37s + up to 30s connect + opening TTS)
# crossed 60s before the first speech frame and the call was killed before it
# rang. And bounding the *warm* wait buys nothing — no airtime is billed until
# connect. So the idle timeout must comfortably EXCEED the full pre-first-audio
# window, then still bound POST-connect mutual silence (the true PHONE-15 intent:
# both parties silent for a stretch => dead live call, hang up to stop the meter).
# DERIVE it from the warm ceiling + connect timeout so it tracks either if they
# change and the collision (idle <= pre-dial gate window) cannot silently return;
# the phone_idle_timeout_covers_warm_gate ratchet locks the inequality.
# Worst-case airtime on a stuck LIVE call is now ~150s (~$0.04) vs pipecat's 300s
# default (~$0.075) — the extra ~90s over the old (broken) 60s is negligible cost
# for never cancelling a real call before its first audio.
PHONE_MEDIA_CONNECT_TIMEOUT_S = 30.0  # wait for the Telnyx media stream after dial
PHONE_POST_CONNECT_SILENCE_BUDGET_S = 60.0  # mutual silence this long => dead live call
PHONE_PIPELINE_IDLE_TIMEOUT_S = (
    REMOTE_WARM_DIAL_CEILING_S + PHONE_MEDIA_CONNECT_TIMEOUT_S + PHONE_POST_CONNECT_SILENCE_BUDGET_S
)
# Silero VAD confidence threshold for telephone-band (8 kHz, 300-3400 Hz) audio.
# Cellular/PSTN speech yields lower Silero confidence than wideband mic audio;
# the library default (0.7) misses real recipient turns even at 8 kHz.
#
# CALIBRATION NOTE (2026-07-04): the "0 false starts on noise/silence" claim below
# is contradicted by call cc65aff3 (a real call where background/echo repeatedly
# flushed Viola's turn). Correctly-wrapped Silero (context+state, see
# telephony/phone_vad_calibration.py) scores real recipient speech ~0.88 median AND
# background ~0.31-0.87 median on that leg, so confidence ALONE does not separate
# them; the base gate is `confidence >= PHONE_VAD_CONFIDENCE AND volume >=
# PHONE_VAD_MIN_VOLUME` (pipecat vad_analyzer). The correct threshold trio is being
# set from LIVE per-turn samples (event `vad_calibration`) rather than the attenuated
# recording. These values are UNCHANGED for now; only the instrumentation to lock
# them ships in this change.
PHONE_VAD_CONFIDENCE = 0.3
# Minimum smoothed volume for the base VAD gate (pipecat default 0.6) and minimum
# sustained-speech duration before a turn STARTS (pipecat default 0.2s, the
# field-standard min-DURATION gate that should eventually replace the word-count
# start strategy). Pinned to the current framework defaults so behavior is unchanged
# while the `vad_calibration` samples reveal where the real recipient-vs-background
# boundary sits on each axis.
PHONE_VAD_MIN_VOLUME = 0.6
PHONE_VAD_START_SECS = 0.2
# Listen-first outbound opening (db18e514 native baseline). Outbound calls are
# caller-led, so Viola follows the recipient's first turn to its semantic end
# (Smart-Turn / VAD, no fixed window) before queueing her model-owned opening,
# and never talks over a live business greeting. The only timer is this
# silent-answer fallback: if the recipient never starts speaking, Viola leads
# after this many seconds so a silent pickup does not stall the call. Pipecat's
# native interruption handling owns turn-taking after that — no hand-rolled
# interruption shield, no one-shot opening latch.
#
# 4.0s, not 2.0s: a human commonly takes 2-5s to pick up and begin their greeting
# ("...hello, thanks for calling Tony's"). At 2.0s the timer beat normal human
# answer latency, so the fallback queued an opening just as the recipient started
# speaking — and their completing turn then drove a SECOND opening through the
# aggregator (capstone f6875e9b double-fire → choppy/restarted audio). Widening to
# 4.0s keeps the common slightly-slow human greeting on the listen-first path
# (one opening), while a genuinely silent pickup still gets exactly one opening
# after the wait. The TOCTOU confirm-grace in AnswerSettleObserver and the
# pre-queue yield guard below are the structural backstops; this value keeps the
# common case from ever reaching them.
_PHONE_ANSWER_SILENT_FALLBACK_SECS = 4.0
_PHONE_RING_WARMUP_AUDIO_SAMPLE_RATE = 16000
_PHONE_RING_WARMUP_AUDIO_SECONDS = 1
_PHONE_RING_STT_WARMUP_TIMEOUT_SECS = 3.0
_PHONE_RING_LLM_WARMUP_TIMEOUT_SECS = 8.0
_PHONE_RING_TTS_WARMUP_TIMEOUT_SECS = 3.0
_PHONE_MCP_TOOL_TIMEOUT_SECS = 180.0
# Pipeline-enforced (pipecat) timeout for MCP tool registrations. The handler's
# internal budget is _PHONE_MCP_TOOL_TIMEOUT_SECS (AgentExecutor total_timeout /
# tool_timeout), and pipecat's function-call clock starts BEFORE the handler
# runs — so registering the SAME number guarantees that whenever the executor
# uses its full budget, pipecat times the call out first and the late result is
# discarded with no LLM re-run (the consult_user dead-air class, capstone
# 67105503). The registered timeout must EXCEED the internal budget, derived
# from the same constant so the two can never drift.
_PHONE_MCP_TOOL_PIPELINE_TIMEOUT_SECS = _PHONE_MCP_TOOL_TIMEOUT_SECS + PIPELINE_FUNCTION_TIMEOUT_MARGIN_SECS
_PHONE_WS_DISCONNECT_HANGUP_GRACE_SECS = 5.0
_PHONE_BILLING_HEARTBEAT_INTERVAL_SECS = 30.0
_PHONE_NUMBER_POOL_HEARTBEAT_MAX_INTERVAL_SECS = 30.0
# Floor for the effective concurrent-outbound cap. The real cap is driven by
# TelnyxConfig.max_concurrent_calls (settings ``phone_global_max_concurrent``,
# default 25) via CallManager._max_active_outbound_calls() — never a second
# hard-coded number. This floor only guarantees the pump always admits at least
# one call even if a deployment misconfigures the cap to 0/negative.
_MIN_ACTIVE_OUTBOUND_CALLS = 1
_PHONE_DUPLICATE_TERMINAL_WINDOW_SECS = 10 * 60.0


def _phone_stt_bias_terms(*text_parts: str, include_generic: bool = True) -> tuple[str, ...]:
    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        normalized = term.strip().lower()
        if not normalized or normalized in seen:
            return
        terms.append(normalized)
        seen.add(normalized)

    if include_generic:
        for term in _PHONE_STT_GENERIC_HOTWORDS:
            add(term)

    for text in text_parts:
        for match in _PHONE_STT_HOTWORD_RE.finditer(str(text or "")):
            term = match.group(0).lower().strip("'_-")
            if len(term) < 3 or term in _PHONE_STT_HOTWORD_STOPWORDS:
                continue
            add(term)
            if len(terms) >= _PHONE_STT_MAX_BIAS_TERMS:
                break
        if len(terms) >= _PHONE_STT_MAX_BIAS_TERMS:
            break

    return tuple(terms)


def _phone_stt_hotwords(*text_parts: str) -> str:
    return " ".join(_phone_stt_bias_terms(*text_parts, include_generic=True))


def _phone_stt_initial_prompt(*text_parts: str) -> str:
    terms = _phone_stt_bias_terms(*text_parts, include_generic=False)
    if not terms:
        return ""
    prompt = "Call vocabulary: %s." % ", ".join(terms)
    return prompt[:_PHONE_STT_INITIAL_PROMPT_MAX_CHARS].rstrip(" ,.")


def _maybe_capture_phone_stt_input(audio: bytes, sample_rate: int, text: str) -> None:
    """Opt-in diagnostic: persist the EXACT audio the phone transcriber was handed
    plus the text it produced, so a live STT hallucination can be replayed offline
    byte-for-byte. The saved call recording is the clean recipient leg and does NOT
    reproduce streaming-STT phantoms (2026-06-25 call 3aa610d6: the live path
    invented "Tony's pizza it's a time you you" from a near-silent line that returns
    nothing when the recording is transcribed offline). Enabled only when
    VIOLA_PHONE_STT_CAPTURE is set; never runs in normal operation and never raises.
    """
    import os

    if not os.environ.get("VIOLA_PHONE_STT_CAPTURE"):
        return
    try:
        import json
        import threading
        import time
        import wave

        cap_dir = get_data_dir() / "phone_stt_capture"
        cap_dir.mkdir(parents=True, exist_ok=True)
        stamp = "%.6f_%d" % (time.time(), threading.get_ident())
        rate = int(sample_rate or 8000)
        with wave.open(str(cap_dir / ("stt_%s.wav" % stamp)), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(audio)
        (cap_dir / ("stt_%s.json" % stamp)).write_text(
            json.dumps({"sample_rate": rate, "bytes": len(audio), "text": text}),
            encoding="utf-8",
        )
    except (OSError, ValueError, TypeError, RuntimeError):
        logger.debug("phone STT capture failed", exc_info=True)


def _phone_stt_manifest_text(info_manifest: dict[str, list[str]] | None) -> str:
    if not info_manifest:
        return ""
    parts: list[str] = []
    for bucket in ("have", "dont_have"):
        values = info_manifest.get(bucket) or []
        if isinstance(values, (str, bytes)):
            values = [str(values)]
        for value in values:
            text = str(value or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


_TELNYX_HANGUP_TIMEOUT_SECS = 5.0
_TELNYX_HANGUP_URL = "https://api.telnyx.com/v2/calls/%s/actions/hangup"
_CALL_STATUSES_REQUIRING_HANGUP = frozenset(
    {
        "pending",
        "dialing",
        "ringing",
        "active",
        "failed",
        "timeout",
        "no_answer",
        "cancelled",
    }
)
_TELNYX_ERROR_PREVIEW_CHARS = 500
_SAFE_CALL_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_phone_stt_model_lock = threading.RLock()
# Cross-call transcribe concurrency cap. faster-whisper / CTranslate2 models are
# thread-safe for concurrent transcribe, so calls do NOT serialize behind a global
# lock (the pre-2026-07 shape); a bounded semaphore keeps peak CPU/memory bounded
# instead, which is what the original mkl_malloc-OOM pin (f845b558) actually needed.
# Built lazily so VIOLA_PHONE_STT_MAX_CONCURRENT / thread settings are read at
# first use, not import time.
_phone_stt_transcribe_slots: threading.BoundedSemaphore | None = None
_phone_stt_transcribe_slots_lock = threading.Lock()
_phone_stt_model: Any | None = None
_phone_stt_device_fallbacks: dict[tuple[str, str, str], tuple[str, str, str]] = {}
_phone_tts_runtime_lock = threading.RLock()
_phone_tts_runtime: _PhoneKokoroTTSRuntime | None = None
_phone_tts_provider_fallbacks: dict[tuple[str, str, str], tuple[str, str, str]] = {}


async def _require_owner_safety_control(key: str, *, user_id: str, action: str) -> None:
    try:
        from services.operator_controls import require_enabled_async

        decision = await require_enabled_async(key, user_id=user_id, action=action)
    except Exception as exc:
        logger.warning("Phone owner safety control check failed: %s", exc)
        raise ValueError("This capability is temporarily paused while safety controls recover.") from exc
    if not decision.allowed:
        raise ValueError(decision.public_message or "This capability is temporarily paused for safety.")


_phone_stt_warm_key: tuple[str, str, str] | None = None
_phone_tts_warm_key: tuple[str, str, str] | None = None
_shutdown_call_managers: weakref.WeakSet[Any] = weakref.WeakSet()
_shutdown_atexit_installed = False
_shutdown_signal_hooks_installed = False
_previous_signal_handlers: dict[int, Any] = {}

_PHONE_CONTROL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "enter_hold_mode",
        "press_button",
        "consult_user",
        "save_call_result",
        "end_call",
    }
)
_PHONE_DESKTOP_TOOL_BLOCKLIST: frozenset[str] = frozenset({"phone", "ask_user", "conference_in_user"})
_PHONE_BACKGROUND_BROWSER_SURFACE = "phone"
_VISIBLE_DESKTOP_BROWSER_MODULE = "mcp_servers.browser_cdp.server"


def _record_status_value(record: Any) -> str:
    status = getattr(record, "status", "")
    return str(getattr(status, "value", status) or "")


def _record_needs_telnyx_hangup(record: Any) -> bool:
    if bool(getattr(record, "_telnyx_hangup_dispatched", False)):
        return False
    call_control_id = str(getattr(record, "telnyx_call_control_id", "") or "")
    return bool(call_control_id and _record_status_value(record) in _CALL_STATUSES_REQUIRING_HANGUP)


def _sync_post_telnyx_hangup(api_key: str, call_control_id: str) -> bool:
    """Best-effort synchronous Telnyx hangup for signal/atexit paths."""
    if not api_key or not call_control_id:
        return False

    try:
        import httpx

        with httpx.Client(timeout=_TELNYX_HANGUP_TIMEOUT_SECS) as client:
            response = client.post(
                _TELNYX_HANGUP_URL % call_control_id,
                headers={"Authorization": "Bearer %s" % api_key},
            )
        if 200 <= response.status_code < 300 or response.status_code == 404:
            return True
        logger.warning(
            "Telnyx shutdown hangup returned HTTP %d for call_control_id=%s",
            response.status_code,
            call_control_id[:16],
        )
    except Exception as exc:
        logger.warning(
            "Telnyx shutdown hangup failed for call_control_id=%s: %s",
            call_control_id[:16],
            exc,
        )
    return False


async def _async_post_telnyx_hangup(api_key: str, call_control_id: str) -> bool:
    """Best-effort async Telnyx hangup for live call failure paths."""
    if not api_key or not call_control_id:
        return False

    try:
        import httpx

        async with httpx.AsyncClient(timeout=_TELNYX_HANGUP_TIMEOUT_SECS) as client:
            response = await client.post(
                _TELNYX_HANGUP_URL % call_control_id,
                headers={"Authorization": "Bearer %s" % api_key},
            )
        if 200 <= response.status_code < 300 or response.status_code == 404:
            return True
        logger.warning(
            "Telnyx hangup returned HTTP %d for call_control_id=%s",
            response.status_code,
            call_control_id[:16],
        )
    except Exception as exc:
        logger.warning("Telnyx hangup failed for call_control_id=%s: %s", call_control_id[:16], exc)
    return False


def _hangup_active_calls_on_shutdown() -> None:
    hung_up = 0
    for manager in list(_shutdown_call_managers):
        try:
            hung_up += manager.hangup_active_calls_for_shutdown()
        except Exception as exc:
            logger.warning("Phone shutdown hangup sweep failed: %s", exc)
    if hung_up:
        logger.warning("Phone shutdown hook hung up %d active Telnyx call(s)", hung_up)


def _phone_shutdown_signal_handler(signum: int, frame: Any) -> None:
    _hangup_active_calls_on_shutdown()
    previous = _previous_signal_handlers.get(int(signum))
    if callable(previous) and previous is not _phone_shutdown_signal_handler:
        previous(signum, frame)
        return
    if previous == signal.SIG_IGN:
        return
    if signum == getattr(signal, "SIGINT", None):
        raise KeyboardInterrupt
    raise SystemExit(128 + int(signum))


def _register_call_manager_shutdown_hooks(manager: Any) -> None:
    global _shutdown_atexit_installed, _shutdown_signal_hooks_installed
    _shutdown_call_managers.add(manager)

    if not _shutdown_atexit_installed:
        atexit.register(_hangup_active_calls_on_shutdown)
        _shutdown_atexit_installed = True

    if _shutdown_signal_hooks_installed:
        return

    if threading.current_thread() is not threading.main_thread():
        logger.debug("Phone shutdown signal hooks skipped outside main thread")
        return

    for signal_name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, signal_name, None)
        if signum is None:
            continue
        try:
            _previous_signal_handlers[int(signum)] = signal.getsignal(signum)
            signal.signal(signum, _phone_shutdown_signal_handler)
        except (OSError, ValueError):
            logger.debug("Phone shutdown signal hook unavailable for %s", signal_name)
    _shutdown_signal_hooks_installed = True


def _phone_function_failure_payload(function_name: str, exc: BaseException) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "Phone function %s failed: %s" % (function_name, exc),
        "error_category": "phone_function_call_exception",
        "retryable": False,
    }


def _guard_phone_function_handler(function_name: str, handler: Any, call_record: Any):
    """Convert phone function-call exceptions into LLM-visible envelopes."""

    async def guarded_phone_function_handler(params) -> None:
        phone_latency_trace = getattr(call_record, "_phone_latency_trace", None)
        if phone_latency_trace is not None:
            with suppress(Exception):
                phone_latency_trace.record_tool_call(function_name)
        # PHONE-15 path-drop recovery: an interactive tool call (anything other
        # than end_call itself or the bookkeeping save_call_result, which both ride
        # a genuine close) means the model is still working the call, not wrapping
        # up — so a previously-latched text-empty end_call is stale. Drop it so the
        # next spoken turn does not auto-hang-up mid-task.
        if function_name not in ("end_call", "save_call_result"):
            from telephony.call_tools import cancel_latched_end_call

            cancel_latched_end_call(call_record, reason="interactive_tool:%s" % function_name)
        try:
            await handler(params)
        except Exception as exc:
            logger.exception(
                "Phone function handler failed: call=%s function=%s",
                getattr(call_record, "call_id", "unknown"),
                function_name,
            )
            callback = getattr(params, "result_callback", None)
            if callback is None:
                return
            try:
                await callback(_phone_function_failure_payload(function_name, exc))
            except Exception:
                logger.exception("Phone function failure callback failed for %s", function_name)

    return guarded_phone_function_handler


@dataclass(frozen=True)
class PhoneLLMToolSurface:
    """Provider-ready phone tool surface plus name mapping for execution."""

    tools_schema: Any
    openai_tools: list[dict[str, Any]]
    mcp_name_by_llm_name: dict[str, str]
    desktop_tool_names: list[str]
    phone_tool_names: list[str]


@dataclass(slots=True)
class _PhoneToolRuntime:
    """Live MCP runtime used by phone-call function handlers."""

    user_id: str
    hub: Any
    approval_manager: Any
    visible_tools: list[dict[str, Any]]


def _phone_stt_preload_enabled() -> bool:
    import os

    raw = os.environ.get("VIOLA_PHONE_STT_PRELOAD", "1").strip().lower()
    return raw not in _PHONE_STT_PRELOAD_DISABLED_VALUES


_PHONE_STT_DEFAULT_CPU_THREADS = 4


def _phone_stt_cpu_threads() -> int:
    """CPU threads per Whisper transcribe (VIOLA_PHONE_STT_CPU_THREADS, default 4).

    The old hard pin of 1 (f845b558, mkl_malloc OOM guard) made CPU small.en
    inference ~2x slower than the 2.4 s turn budget; a bounded multi-thread
    default clears the budget while _phone_stt_max_concurrent() keeps total
    CPU/memory demand capped. Clamped to [1, effective_cpu_count()] -- the
    container's own cgroup CPU quota when the deployment is quota-capped
    (Docker --cpus / docker-compose deploy.resources.limits.cpus; #4433/C-613),
    the host's os.cpu_count() otherwise.
    """
    import os

    cpu_count = effective_cpu_count(default=_PHONE_STT_DEFAULT_CPU_THREADS)
    raw = os.environ.get("VIOLA_PHONE_STT_CPU_THREADS", "").strip()
    try:
        requested = int(raw) if raw else _PHONE_STT_DEFAULT_CPU_THREADS
    except ValueError:
        requested = _PHONE_STT_DEFAULT_CPU_THREADS
    return max(1, min(requested, cpu_count))


def _phone_stt_max_concurrent() -> int:
    """Concurrent transcribe cap (VIOLA_PHONE_STT_MAX_CONCURRENT).

    Default sizes total worker threads to the container's real CPU budget:
    effective_cpu_count() // cpu_threads, floored at 1.
    effective_cpu_count() auto-detects the cgroup CPU quota (Docker --cpus /
    docker-compose deploy.resources.limits.cpus / a Kubernetes CPU limit)
    when the deployment is quota-capped, so this stays correctly sized on
    every future container without any env var (#4433/C-613: os.cpu_count()
    used to report the HOST's cores from inside a quota-capped container,
    e.g. 16 inside an 8-CPU-capped viola-api, oversubscribing this pool 2x).
    VIOLA_PHONE_STT_MAX_CONCURRENT remains available to override explicitly
    when an operator wants a different value than the auto-detected default.
    """
    import os

    cpu_count = effective_cpu_count(default=_PHONE_STT_DEFAULT_CPU_THREADS)
    default = max(1, cpu_count // _phone_stt_cpu_threads())
    raw = os.environ.get("VIOLA_PHONE_STT_MAX_CONCURRENT", "").strip()
    try:
        requested = int(raw) if raw else default
    except ValueError:
        requested = default
    return max(1, requested)


def _phone_stt_transcribe_semaphore() -> threading.BoundedSemaphore:
    global _phone_stt_transcribe_slots
    with _phone_stt_transcribe_slots_lock:
        if _phone_stt_transcribe_slots is None:
            _phone_stt_transcribe_slots = threading.BoundedSemaphore(_phone_stt_max_concurrent())
        return _phone_stt_transcribe_slots


def _set_phone_stt_thread_env() -> None:
    import os

    configure_environment()
    threads = str(_phone_stt_cpu_threads())
    os.environ.setdefault("MKL_NUM_THREADS", threads)
    os.environ.setdefault("OMP_NUM_THREADS", threads)


def _phone_stt_model_key(config: TelnyxConfig) -> tuple[str, str, str]:
    return (
        config.whisper_model,
        config.whisper_device,
        config.whisper_compute_type,
    )


def _phone_stt_download_root(*, cache_root: Path | None = None) -> Path:
    root = (cache_root or get_cache_dir()) / "faster-whisper"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _load_phone_stt_model(model_name: str, device: str, compute_type: str) -> Any:
    from faster_whisper import WhisperModel

    configure_environment()
    # cpu_threads is passed explicitly (not just via OMP_NUM_THREADS setdefault)
    # because another component may have exported OMP_NUM_THREADS first, and the
    # OpenMP runtime reads it only once per process. Ignored on CUDA devices.
    return WhisperModel(
        model_name,
        device=device,
        compute_type=compute_type,
        cpu_threads=_phone_stt_cpu_threads(),
        download_root=str(_phone_stt_download_root()),
    )


def _warm_phone_stt_model(model: Any, *, model_name: str, device: str) -> None:
    """Prime CUDA kernels while the phone runtime is warming, not after speech."""
    if device.strip().lower() != "cuda":
        return
    try:
        import numpy as np

        language = None if _phone_stt_uses_multilingual(model_name) else "en"
        audio = np.zeros(16000, dtype=np.float32)
        segments, _ = model.transcribe(
            audio,
            language=language,
            beam_size=1,
            condition_on_previous_text=False,
        )
        for _segment in segments:
            pass
    except Exception as exc:  # noqa: BLE001, RUF100 - CUDA warmup is best-effort latency work; never break calls
        logger.warning("Phone STT CUDA warmup pass failed; first live turn may be slower: %s", exc)


def _ensure_phone_stt_model(config: TelnyxConfig) -> Any:
    """Load and retain the phone faster-whisper model for this process."""
    global _phone_stt_model, _phone_stt_warm_key

    _set_phone_stt_thread_env()
    requested_key = _phone_stt_model_key(config)
    with _phone_stt_model_lock:
        key = _phone_stt_device_fallbacks.get(requested_key, requested_key)
        if _phone_stt_model is not None and _phone_stt_warm_key == key:
            return _phone_stt_model

        model_name, device, compute_type = key
        start = time.perf_counter()
        logger.info(
            "Preloading phone STT model: model=%s device=%s compute_type=%s",
            model_name,
            device,
            compute_type,
        )
        try:
            _phone_stt_model = _load_phone_stt_model(model_name, device, compute_type)
        except Exception as exc:
            if device.strip().lower() != "cuda":
                raise
            fallback_key = (
                model_name,
                _PHONE_STT_CPU_FALLBACK_DEVICE,
                _PHONE_STT_CPU_FALLBACK_COMPUTE_TYPE,
            )
            logger.warning(
                "Phone STT CUDA model load failed; falling back to CPU: model=%s compute_type=%s error=%s",
                model_name,
                fallback_key[2],
                exc,
            )
            _phone_stt_device_fallbacks[requested_key] = fallback_key
            fallback_start = time.perf_counter()
            _phone_stt_model = _load_phone_stt_model(*fallback_key)
            _phone_stt_warm_key = fallback_key
            logger.info(
                "Phone STT CPU fallback model warm in %.3fs",
                time.perf_counter() - fallback_start,
            )
            return _phone_stt_model
        _warm_phone_stt_model(_phone_stt_model, model_name=model_name, device=device)
        _phone_stt_warm_key = key
        logger.info("Phone STT model warm in %.3fs", time.perf_counter() - start)
        return _phone_stt_model


def phone_is_configured() -> bool:
    """True when this install has the Telnyx credentials a local call needs.

    The three values below are exactly what ``intent.tools.phone_call._get_manager``
    requires before it will build a local ``CallManager`` at all, so an install
    missing any of them can never reach the local phone STT model.
    """
    from config.settings import settings

    return all(
        (getattr(settings, name, "") or "").strip()
        for name in ("telnyx_api_key", "telnyx_phone_number", "telnyx_sip_connection_id")
    )


def preload_phone_stt(config: TelnyxConfig | None = None, *, respect_disable: bool = True) -> bool:
    """Warm the local phone STT model without placing a call.

    With no explicit *config* this is a speculative startup warm, and it is
    skipped unless the install is actually set up for local phone calls.
    ``small.en`` is ~460 MB on disk and took ~120 s of multi-threaded CPU on a
    clean 4-vCPU VM, all of it spent while the user was trying to talk to Viola
    -- and on an install with no Telnyx credentials the warmed model can never
    be used, because ``_get_manager`` refuses to build a local ``CallManager``
    without them. An explicit *config* means a caller is really standing up a
    call path, so that warm still runs.
    """
    if respect_disable and not _phone_stt_preload_enabled():
        logger.info("Phone STT preload disabled by VIOLA_PHONE_STT_PRELOAD")
        return False

    if config is None and not phone_is_configured():
        logger.info("Phone STT preload skipped: no local phone credentials configured on this install")
        return False

    cfg = config or TelnyxConfig(
        api_key="_warmup",  # pragma: allowlist secret
        phone_number="+10000000000",
        sip_connection_id="_warmup",
    )
    if cfg.stt_provider != "local":
        logger.info("Phone STT preload skipped for provider=%s", cfg.stt_provider)
        return False

    try:
        _ensure_phone_stt_model(cfg)
        return True
    except Exception as exc:
        logger.warning("Phone STT preload failed: %s", exc)
        return False


def get_phone_stt_warm_state() -> dict[str, Any]:
    """Return diagnostic state for phone STT warmup tests and reports."""
    with _phone_stt_model_lock:
        key = _phone_stt_warm_key
        return {
            "warmed": _phone_stt_model is not None,
            "model": key[0] if key else "",
            "device": key[1] if key else "",
            "compute_type": key[2] if key else "",
            "model_id": id(_phone_stt_model) if _phone_stt_model is not None else 0,
        }


def reset_phone_stt_warm_state_for_tests() -> None:
    """Clear the phone STT cache for unit tests."""
    global _phone_stt_model, _phone_stt_warm_key
    with _phone_stt_model_lock:
        _phone_stt_model = None
        _phone_stt_warm_key = None
        _phone_stt_device_fallbacks.clear()


@dataclass(frozen=True)
class _PhoneKokoroTTSRuntime:
    model_path: Path
    voices_path: Path
    requested_provider: str
    provider: str
    session_providers: tuple[str, ...]
    session: Any
    kokoro: Any
    load_seconds: float
    warm_seconds: float


def _phone_tts_preload_enabled() -> bool:
    raw = os.environ.get("VIOLA_PHONE_TTS_PRELOAD", "1").strip().lower()
    return raw not in _PHONE_TTS_PRELOAD_DISABLED_VALUES


def _phone_tts_provider_preference() -> str:
    raw = (
        os.environ.get("VIOLA_PHONE_TTS_ONNX_PROVIDER")
        or os.environ.get("VIOLA_KOKORO_ONNX_PROVIDER")
        or os.environ.get("ONNX_PROVIDER")
        or _PHONE_TTS_PROVIDER_AUTO
    )
    provider = raw.strip().lower()
    if provider in {"cuda", _PHONE_TTS_CUDA_EP.lower()}:
        return _PHONE_TTS_PROVIDER_CUDA
    if provider in {"cpu", _PHONE_TTS_CPU_EP.lower()}:
        return _PHONE_TTS_PROVIDER_CPU
    return _PHONE_TTS_PROVIDER_AUTO


def _phone_tts_provider_plans(
    requested_provider: str, available_providers: tuple[str, ...]
) -> tuple[tuple[str, ...], ...]:
    if requested_provider == _PHONE_TTS_PROVIDER_CPU:
        return ((_PHONE_TTS_CPU_EP,),)
    if _PHONE_TTS_CUDA_EP not in available_providers:
        return ((_PHONE_TTS_CPU_EP,),)
    return ((_PHONE_TTS_CUDA_EP, _PHONE_TTS_CPU_EP), (_PHONE_TTS_CPU_EP,))


def _phone_tts_runtime_key(config: TelnyxConfig) -> tuple[str, str, str]:
    model_path, voices_path = _phone_kokoro_model_paths()
    return (str(model_path), str(voices_path), _phone_tts_provider_preference())


def _create_phone_kokoro_session(model_path: Path, providers: tuple[str, ...]) -> Any:
    import onnxruntime as ort

    preload = getattr(ort, "preload_dlls", None)
    if callable(preload):
        try:
            preload(directory="")
        except Exception as exc:  # noqa: BLE001, RUF100 - explicit providers below still decide fail/fallback.
            logger.debug("Phone Kokoro ONNX Runtime DLL preload skipped: %s", exc)
    return ort.InferenceSession(str(model_path), providers=list(providers))


def _phone_kokoro_from_session(session: Any, voices_path: Path) -> Any:
    from kokoro_onnx import Kokoro

    return Kokoro.from_session(session, str(voices_path))


def _warm_phone_kokoro_tts_runtime(kokoro: Any, *, voice: str) -> float:
    start = time.perf_counter()
    samples, sample_rate = kokoro.create(_PHONE_TTS_WARMUP_TEXT, voice=voice)
    if sample_rate <= 0 or len(samples) == 0:
        raise RuntimeError("Kokoro TTS warmup produced no audio")
    return time.perf_counter() - start


def _load_phone_kokoro_tts_runtime(
    model_path: Path,
    voices_path: Path,
    *,
    voice: str,
    requested_provider: str,
) -> _PhoneKokoroTTSRuntime:
    import onnxruntime as ort

    configure_environment()
    available_providers = tuple(ort.get_available_providers())
    plans = _phone_tts_provider_plans(requested_provider, available_providers)
    last_error: Exception | None = None
    for providers in plans:
        load_start = time.perf_counter()
        try:
            session = _create_phone_kokoro_session(model_path, providers)
            actual_providers = tuple(session.get_providers())
            if providers[0] == _PHONE_TTS_CUDA_EP and _PHONE_TTS_CUDA_EP not in actual_providers:
                raise RuntimeError(
                    "CUDAExecutionProvider was requested but ONNX Runtime activated %s" % ", ".join(actual_providers)
                )
            kokoro = _phone_kokoro_from_session(session, voices_path)
            warm_seconds = _warm_phone_kokoro_tts_runtime(kokoro, voice=voice)
            provider = _PHONE_TTS_PROVIDER_CUDA if _PHONE_TTS_CUDA_EP in actual_providers else _PHONE_TTS_PROVIDER_CPU
            return _PhoneKokoroTTSRuntime(
                model_path=model_path,
                voices_path=voices_path,
                requested_provider=requested_provider,
                provider=provider,
                session_providers=actual_providers,
                session=session,
                kokoro=kokoro,
                load_seconds=time.perf_counter() - load_start,
                warm_seconds=warm_seconds,
            )
        except Exception as exc:
            last_error = exc
            if providers[0] != _PHONE_TTS_CUDA_EP:
                raise
            logger.warning("Phone Kokoro CUDA session failed; falling back to CPU: %s", exc)

    if last_error is not None:
        raise last_error
    raise RuntimeError("No ONNX Runtime providers available for phone Kokoro TTS")


def _ensure_phone_kokoro_tts_runtime(config: TelnyxConfig) -> _PhoneKokoroTTSRuntime:
    """Load, warm, and retain one Kokoro ONNX runtime for phone TTS."""
    global _phone_tts_runtime, _phone_tts_warm_key

    requested_key = _phone_tts_runtime_key(config)
    with _phone_tts_runtime_lock:
        key = _phone_tts_provider_fallbacks.get(requested_key, requested_key)
        if _phone_tts_runtime is not None and _phone_tts_warm_key == key:
            return _phone_tts_runtime

        model_path = Path(key[0])
        voices_path = Path(key[1])
        requested_provider = key[2]
        logger.info(
            "Preloading phone Kokoro TTS runtime: provider=%s model=%s voices=%s",
            requested_provider,
            model_path,
            voices_path,
        )
        _phone_tts_runtime = _load_phone_kokoro_tts_runtime(
            model_path,
            voices_path,
            voice=config.tts_voice,
            requested_provider=requested_provider,
        )
        if requested_provider != _PHONE_TTS_PROVIDER_CPU and _phone_tts_runtime.provider == _PHONE_TTS_PROVIDER_CPU:
            fallback_key = (str(model_path), str(voices_path), _PHONE_TTS_PROVIDER_CPU)
            _phone_tts_provider_fallbacks[requested_key] = fallback_key
            key = fallback_key
        _phone_tts_warm_key = key
        logger.info(
            "Phone Kokoro TTS warm: requested_provider=%s provider=%s session_providers=%s load=%.3fs warm=%.3fs",
            _phone_tts_runtime.requested_provider,
            _phone_tts_runtime.provider,
            ",".join(_phone_tts_runtime.session_providers),
            _phone_tts_runtime.load_seconds,
            _phone_tts_runtime.warm_seconds,
        )
        return _phone_tts_runtime


def preload_phone_tts(
    config: TelnyxConfig | None = None,
    *,
    respect_disable: bool = True,
    raise_on_failure: bool = False,
) -> bool:
    """Warm the local phone Kokoro TTS runtime without placing a call."""
    if respect_disable and not _phone_tts_preload_enabled():
        logger.info("Phone TTS preload disabled by VIOLA_PHONE_TTS_PRELOAD")
        return False

    cfg = config or TelnyxConfig(
        api_key="_warmup",  # pragma: allowlist secret
        phone_number="+10000000000",
        sip_connection_id="_warmup",
    )
    if cfg.tts_provider != "local":
        logger.info("Phone TTS preload skipped for provider=%s", cfg.tts_provider)
        return False

    try:
        _ensure_phone_kokoro_tts_runtime(cfg)
        return True
    except Exception as exc:
        if raise_on_failure:
            raise
        logger.warning("Phone TTS preload failed: %s", exc)
        return False


def get_phone_tts_warm_state() -> dict[str, Any]:
    """Return diagnostic state for phone Kokoro TTS warmup tests and reports."""
    with _phone_tts_runtime_lock:
        key = _phone_tts_warm_key
        runtime = _phone_tts_runtime
        return {
            "warmed": runtime is not None,
            "model_path": key[0] if key else "",
            "voices_path": key[1] if key else "",
            "requested_provider": (runtime.requested_provider if runtime is not None else ""),
            "provider": runtime.provider if runtime is not None else "",
            "session_providers": (runtime.session_providers if runtime is not None else ()),
            "load_seconds": runtime.load_seconds if runtime is not None else 0.0,
            "warm_seconds": runtime.warm_seconds if runtime is not None else 0.0,
            "session_id": id(runtime.session) if runtime is not None else 0,
            "kokoro_id": id(runtime.kokoro) if runtime is not None else 0,
        }


def reset_phone_tts_warm_state_for_tests() -> None:
    """Clear the phone Kokoro TTS cache for unit tests."""
    global _phone_tts_runtime, _phone_tts_warm_key
    with _phone_tts_runtime_lock:
        _phone_tts_runtime = None
        _phone_tts_warm_key = None
        _phone_tts_provider_fallbacks.clear()


def _create_shared_phone_kokoro_tts_service(*, kokoro: Any, voice_id: str, **kwargs: Any) -> Any:
    from pipecat.services.kokoro.tts import KokoroTTSService
    from pipecat.services.tts_service import TTSService
    from pipecat.transcriptions.language import Language

    from telephony.continuous_stream_resampler import create_continuous_stream_resampler

    class _SharedPhoneKokoroTTSService(KokoroTTSService):
        def __init__(self) -> None:
            settings = KokoroTTSService.Settings(
                model=None,
                voice=voice_id,
                language=Language.EN,
            )
            TTSService.__init__(
                self,
                push_start_frame=True,
                push_stop_frames=True,
                settings=settings,
                **kwargs,
            )
            # Remote GPU voice endpoint (VIOLA_PHONE_VOICE_REMOTE_ENABLED,
            # default off): wrap the shared local Kokoro runtime in a
            # remote-first proxy with the identical create/create_stream
            # surface. With the flag off this returns the local runtime
            # untouched; on any remote failure the proxy delegates to it.
            self._kokoro = maybe_remote_first_kokoro(kokoro)
            # Use a clear-resistant resampler for the 24kHz->pipeline-rate stage.
            # pipecat's default create_stream_resampler() auto-clears its SoX
            # filter delay-line after >0.2s idle, and that clear DROPS audio
            # (the next chunks emit fewer samples while the filter re-primes).
            # Viola speaks sentence-by-sentence with >0.2s inter-sentence pauses,
            # so the default would clip the start of every sentence -> the
            # intermittent voice breakup founders heard on cloud calls. The
            # continuous resampler keeps history across pauses (no per-sentence
            # loss). See telephony/continuous_stream_resampler.py.
            self._resampler = create_continuous_stream_resampler()

    return _SharedPhoneKokoroTTSService()


_PHONE_RUNTIME_REQUIRED_MODULES: tuple[tuple[str, str], ...] = (
    ("aiofiles", "aiofiles"),
    ("pipecat", "pipecat-ai"),
    ("telnyx", "telnyx"),
    ("faster_whisper", "faster-whisper"),
    ("onnxruntime", "onnxruntime-gpu"),
    ("kokoro_onnx", "kokoro-onnx"),
    ("piper.voice", "piper-tts"),
    ("babel", "babel"),
    ("soundfile", "soundfile"),
    ("scipy", "scipy"),
    ("soxr", "soxr"),
)

_PHONE_RUNTIME_REQUIRED_BINARIES: tuple[tuple[str, str], ...] = (("espeak-ng", "espeak-ng"),)


def _ensure_phone_runtime_dependencies(tts_provider: str = "local") -> None:
    """Fail before billing/dial state is created if the phone runtime is incomplete."""
    missing = []
    provider = (tts_provider or "local").strip().lower()
    for module_name, package_name in _PHONE_RUNTIME_REQUIRED_MODULES:
        if module_name == "piper.voice" and provider != "piper":
            continue
        if module_name == "kokoro_onnx" and provider != "local":
            continue
        try:
            importlib.import_module(module_name)
        except (
            ImportError,
            AttributeError,
            OSError,
            KeyError,
            ValueError,
            RuntimeError,
        ):
            missing.append(package_name)
    if (tts_provider or "local").strip().lower() == "espeak":
        for binary_name, package_name in _PHONE_RUNTIME_REQUIRED_BINARIES:
            if shutil.which(binary_name) is None:
                missing.append(package_name)
    if missing:
        raise RuntimeError(
            "Phone runtime dependency missing: %s. Install cloud/desktop telephony requirements before dialing."
            % ", ".join(missing)
        )


def _phone_stt_uses_multilingual(model_name: str) -> bool:
    return not model_name.lower().endswith(".en")


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize a possibly naive datetime to UTC-aware, or None."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _billable_window_seconds(record: CallRecord) -> float:
    """Return the seconds the CARRIER leg was actually up — the only honest meter.

    THE BUG THIS EXISTS TO KILL (#2589). Billing used to be
    ``record.ended_at - record.started_at``, and NEITHER endpoint is the carrier
    leg:

    * ``ended_at`` is stamped ``datetime.now()`` in ``_run_call``'s ``finally``,
      i.e. when the PIPELINE tears down. On a clean carrier stop the media
      watchdog deliberately returns without stopping the pipeline (it must not
      race the completion path for terminal-status ownership), so with the
      transport gone no frames flow and the pipecat ``PipelineTask`` resolves
      only when ``PHONE_PIPELINE_IDLE_TIMEOUT_S`` (150s) expires. Every normal
      call therefore billed up to ~150 phantom seconds after the recipient was
      already gone. Real call ``3b144bfe`` logged 34.2s of conversation and
      settled 200.6s / 55152 microdollars — about 6x.
    * ``started_at`` is stamped when the MEDIA STREAM connects, which Telnyx's
      ``stream_establish_before_call_originate`` makes possible BEFORE the
      recipient picks up (see the ``carrier_answered`` note on ``CallRecord``),
      so ring time was billed as talk time.

    The fix anchors both ends to the carrier's own signals, which is exactly the
    window Telnyx bills US for, and is what the user actually received:

    * start -- ``carrier_answered_at`` (the ``call.answered`` webhook) when it is
      available and later than media-connect; otherwise ``started_at``.
    * end -- the EARLIEST proof the leg was over: ``carrier_hangup_at`` (the
      ``call.hangup`` webhook) or ``media_stopped_at`` (the watchdog's observation
      that the media stream ended and did not recover); otherwise the teardown
      stamp, which is the old behavior and the only option when no carrier
      evidence exists at all.

    Never exceeds the pipeline's own lifetime and never goes negative, so no
    input can make this bill MORE than the pre-fix code did. A collapsed window
    (contradictory carrier stamps) bills zero rather than silently falling back
    to the inflated teardown number: under-billing is our loss, over-billing is
    the user's, and a zero on a connected call is loud instead of invisible.
    """
    started = _as_utc(record.started_at)
    if started is None:
        return 0.0
    teardown = _as_utc(record.ended_at) or datetime.now(tz=UTC)

    # Hard outer bound: we can never bill beyond the pipeline's own lifetime.
    pipeline_seconds = max(0.0, (teardown - started).total_seconds())

    billable_start = started
    answered_at = _as_utc(record.carrier_answered_at)
    if answered_at is not None and answered_at > billable_start:
        billable_start = answered_at

    billable_end = teardown
    for candidate in (
        _as_utc(record.carrier_hangup_at),
        _as_utc(record.media_stopped_at),
        _as_utc(record.local_end_at),
    ):
        if candidate is not None and candidate < billable_end:
            billable_end = candidate

    if billable_end <= billable_start and billable_start is not started:
        # The END anchors are real carrier evidence; a START anchor that lands
        # after them means the ``call.answered`` webhook arrived out of order.
        # Fall back to media-connect, which is always <= any end anchor, rather
        # than zeroing a call the recipient really had. Never widens the window
        # past the pipeline clamp below.
        logger.warning(
            "Call %s: carrier answer %s is after the earliest end %s; falling back to "
            "media-connect %s as the billable start",
            record.call_id,
            billable_start,
            billable_end,
            started,
        )
        billable_start = started

    if billable_end <= billable_start:
        logger.warning(
            "Call %s: carrier billing window collapsed (answered=%s hangup=%s media_stop=%s "
            "local_end=%s started=%s teardown=%s); billing 0s rather than the teardown window",
            record.call_id,
            answered_at,
            _as_utc(record.carrier_hangup_at),
            _as_utc(record.media_stopped_at),
            _as_utc(record.local_end_at),
            started,
            teardown,
        )
        return 0.0

    return min(pipeline_seconds, (billable_end - billable_start).total_seconds())


def _billed_duration_seconds(record: CallRecord) -> float:
    """Return the billable duration for a CallRecord.

    Phone duration is metered by the actual connected seconds. Carrier setup
    fees for failed attempts are represented by ``_billed_cost_usd`` instead
    of inflating the user's minute ledger.

    This is the single choke point every settle path funnels through, so the
    carrier anchoring lives here (#2589): even if some future path re-inlines a
    raw ``ended_at - started_at`` into ``record.duration_seconds``, the number
    that reaches the money ledger still cannot exceed the carrier-proven window.
    """
    if record.started_at is not None:
        return _billable_window_seconds(record)
    return max(0.0, float(record.duration_seconds or 0.0))


def _billed_cost_usd(record: CallRecord) -> float:
    """Return the billable cost USD for a CallRecord.

    Enforces a minimum cost on failure/no-answer/timeout/cancel so the
    in-system ledger lines up with what Telnyx actually charges us.
    See PHONE-06/PHONE-11.
    """
    cost = float(record.estimated_cost_usd or 0.0)
    if record.status.value in _BILLABLE_FAILURE_STATES:
        cost = max(cost, _MIN_BILLED_COST_USD)
    return cost


def _recipient_state(record: Any) -> str:
    """Return one recipient-state label from signals the call stack actually tracks."""
    hold_handler = getattr(record, "_hold_handler", None)
    if hold_handler is not None and bool(getattr(hold_handler, "is_on_hold", False)):
        return "hold"

    if bool(getattr(record, "voicemail_detected", False)) and not bool(
        getattr(record, "human_takeover_detected", False)
    ):
        return "voicemail"

    return "human"


def _call_event_payload(record: Any, *, include_summary: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "call_id": str(getattr(record, "call_id", "") or ""),
        "phone_number": str(getattr(record, "phone_number", "") or ""),
        "task": str(getattr(record, "task", "") or ""),
        "status": _record_status_value(record),
        "started_at": (getattr(record, "started_at", None).isoformat() if getattr(record, "started_at", None) else ""),
        "duration_seconds": float(getattr(record, "duration_seconds", 0.0) or 0.0),
        "estimated_cost_usd": float(getattr(record, "estimated_cost_usd", 0.0) or 0.0),
        "current_cost_usd": float(getattr(record, "estimated_cost_usd", 0.0) or 0.0),
        "recipient_state": _recipient_state(record),
    }
    if include_summary:
        recording_paths = getattr(record, "recording_paths", {}) or {}
        payload.update(
            {
                "summary": str(getattr(record, "summary", "") or ""),
                "outcome": str(getattr(record, "outcome", "") or ""),
                "ended_at": (
                    getattr(record, "ended_at", None).isoformat() if getattr(record, "ended_at", None) else ""
                ),
                "has_recording": bool(recording_paths),
                "error": str(getattr(record, "error", "") or ""),
            }
        )
    return payload


def _log_text_preview(text: str) -> str:
    """Return a short log-safe preview of transcript text."""
    text = _mask_phone_numbers_in_text(text)
    if len(text) <= _TRANSCRIPT_LOG_PREVIEW_CHARS:
        return text
    return "%s..." % text[:_TRANSCRIPT_LOG_PREVIEW_CHARS]


def _mask_phone_numbers_in_text(text: str) -> str:
    """Mask phone-like substrings in free-form log text."""

    def _replace(match: re.Match[str]) -> str:
        return _mask_phone_number(match.group(0))

    return _PHONE_NUMBER_LOG_RE.sub(_replace, text)


def _mask_phone_number(phone_number: str | None) -> str:
    """Mask a phone number for logs, preserving only the last four digits."""
    if not phone_number:
        return ""
    digits = re.sub(r"\D", "", phone_number)
    if not digits:
        return "[masked]"
    return "****%s" % digits[-4:]


def _issuer_channel_info(channel: Any | None) -> dict[str, Any]:
    if channel is None:
        return {}

    info: dict[str, Any] = {
        "channel_type": str(getattr(channel, "channel_type", "") or "").strip(),
        "channel_class": type(channel).__name__,
    }
    for attr in (
        "session_id",
        "chat_id",
        "_chat_id",
        "room_id",
        "_room_id",
        "user_id",
        "_user_id",
        "phone_number",
        "caller_number",
        "email",
        "email_address",
    ):
        value = getattr(channel, attr, None)
        if value not in (None, ""):
            info[attr.lstrip("_")] = str(value)
    active_delivery = getattr(channel, "active_delivery", None)
    if active_delivery is not None:
        info["active_delivery"] = bool(active_delivery)
    return {key: value for key, value in info.items() if value not in (None, "")}


def _persist_call_transcript(record: CallRecord, transcript_dir: Path | None = None) -> Path:
    """Persist a completed call record for post-call debugging.

    Multi-tenant: the transcript path is partitioned by ``record.user_id``
    so one tenant cannot read another tenant's transcript even if the
    call id is known or guessed.
    """
    target_dir = _user_transcript_dir(record.user_id, transcript_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = target_dir / f"{record.call_id}.json"
    persisted_transcript = _persisted_transcript_entries(record)
    transcript_path.write_text(
        json.dumps(
            {
                "call_id": record.call_id,
                "user_id": record.user_id,
                "phone_number": record.phone_number,
                "task": record.task,
                "issuer_channel_info": record.issuer_channel_info,
                "started_at": record.started_at,
                "duration_seconds": record.duration_seconds,
                "status": record.status.value,
                "transcript": persisted_transcript,
                "summary": record.summary,
                "outcome": record.outcome,
                "error": record.error,
                "voicemail_detected": record.voicemail_detected,
                "voicemail_detected_at": record.voicemail_detected_at,
                "voicemail_detection_source": record.voicemail_detection_source,
                "human_takeover_detected": record.human_takeover_detected,
                "human_takeover_at": record.human_takeover_at,
                "disclosure_spoken": record.disclosure_spoken,
                "transcript_retention_enabled": record.transcript_retention_enabled,
                "transcript_persistence_stopped_reason": record.transcript_persistence_stopped_reason,
                "recording_stopped_reason": record.recording_stopped_reason,
                "recording_paths": dict(record.recording_paths),
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return transcript_path


def _persist_call_history_entry(record: CallRecord) -> Path:
    """Persist call metadata for the call-history API."""
    from telephony.call_history import CallHistoryEntry, save_call_history

    # PHONE-15: persist cost into metadata.json so per-call audits work.
    # Previously cost_breakdown was always {} on disk because the field was
    # never populated here, even though estimated_cost_usd was computed and
    # logged. Without this, every cost audit had to re-parse logs.
    cost_breakdown = {
        "estimated_cost_usd": float(record.estimated_cost_usd or 0.0),
        "duration_seconds": float(record.duration_seconds or 0.0),
        "llm_prompt_tokens": int(record.llm_prompt_tokens or 0),
        "llm_completion_tokens": int(record.llm_completion_tokens or 0),
    }
    persisted_transcript = _persisted_transcript_entries(record)
    entry = CallHistoryEntry(
        call_id=record.call_id,
        user_id=record.user_id,
        phone_number=record.phone_number,
        task=record.task,
        caller_name=record.caller_name,
        status=record.status.value,
        duration_seconds=record.duration_seconds,
        created_at=record.created_at.isoformat() if record.created_at else "",
        started_at=record.started_at.isoformat() if record.started_at else "",
        ended_at=record.ended_at.isoformat() if record.ended_at else "",
        transcript=persisted_transcript,
        summary=record.summary,
        outcome=record.outcome,
        recording_paths=dict(record.recording_paths),
        recording_enabled=bool(record.recording_enabled),
        recording_stopped_reason=str(record.recording_stopped_reason or ""),
        disclosure_spoken=bool(record.disclosure_spoken),
        cost_breakdown=cost_breakdown,
    )
    return save_call_history(entry)


def _load_persisted_call_transcript(
    call_id: str,
    transcript_dir: Path | None = None,
    *,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """Read a persisted call transcript.

    Multi-tenant: ``user_id`` is REQUIRED.  Without it we refuse to
    serve the file — knowing a call id is not the same as owning the
    call.  The legacy unpartitioned location is consulted as a
    read-only fallback ONLY when the on-disk record's ``user_id`` field
    matches the caller, so old desktop transcripts continue to work
    without leaking across tenants.
    """
    if not _SAFE_CALL_ID_RE.match(call_id):
        return None
    if not user_id:
        return None
    base = transcript_dir or PHONE_TRANSCRIPT_DIR
    candidate_paths: list[Path] = [
        base / _PHONE_TRANSCRIPT_USER_PARTITION / _hash_user_id(user_id) / ("%s.json" % call_id),
        # Legacy desktop fallback — must owner-check on read.
        base / ("%s.json" % call_id),
    ]
    for path in candidate_paths:
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("Could not load persisted phone transcript for call %s", call_id)
            continue
        if not isinstance(data, dict):
            continue
        # Owner-check legacy fallback location.
        owner_in_file = str(data.get("user_id") or "").strip()
        if not owner_in_file or owner_in_file != user_id:
            logger.warning(
                "Refusing legacy phone transcript for call %s: owner missing or mismatch",
                call_id,
            )
            continue
        return data
    return None


def _delete_persisted_call_transcripts(call_id: str, transcript_dir: Path | None = None) -> int:
    """Delete all persisted transcript files for ``call_id``.

    New transcripts live under per-user partitions; the legacy flat path is
    still checked so older installs are covered by the same retention policy.
    """
    if not _SAFE_CALL_ID_RE.match(call_id):
        return 0
    base = transcript_dir or PHONE_TRANSCRIPT_DIR
    deleted = 0
    candidate_paths: list[Path] = [base / ("%s.json" % call_id)]
    partition_root = base / _PHONE_TRANSCRIPT_USER_PARTITION
    if partition_root.exists():
        candidate_paths.extend(partition_root.glob("*/%s.json" % call_id))

    seen: set[Path] = set()
    for path in candidate_paths:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            if path.exists():
                path.unlink()
                deleted += 1
        except OSError as exc:
            logger.warning(
                "Call %s: failed to delete expired transcript JSON %s: %s",
                call_id,
                path,
                exc,
            )
    return deleted


def _is_cloud_llm_consented() -> bool:
    from core.privacy_consent import is_cloud_llm_consented

    return is_cloud_llm_consented()


def _ensure_cloud_llm_consent_for_phone_call() -> None:
    if _is_cloud_llm_consented():
        return
    logger.warning("Cloud LLM consent not given - AI phone call disabled")
    raise PermissionError("Cloud AI consent is required before making AI phone calls.")


def _phone_kokoro_model_paths(
    *,
    repo_root: Path | None = None,
    cache_root: Path | None = None,
) -> tuple[Path, Path]:
    root = repo_root or Path(__file__).resolve().parents[1]
    bundled_dir = root / "models" / "tts"
    bundled_model = bundled_dir / _KOKORO_MODEL_FILENAME
    bundled_voices = bundled_dir / _KOKORO_VOICES_FILENAME
    if bundled_model.exists() and bundled_voices.exists():
        return bundled_model, bundled_voices

    cache_dir = (cache_root or get_cache_dir()) / "kokoro-onnx"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / _KOKORO_MODEL_FILENAME, cache_dir / _KOKORO_VOICES_FILENAME


async def _ensure_cloud_voice_consent_for_phone_call(user_id: str | None) -> None:
    if not user_id:
        logger.warning("Cloud phone full-consent check missing user_id")
        raise PermissionError("Cloud authentication is required before making AI phone calls.")
    try:
        from services.cloud_consent import get_cloud_consent_service

        result = await get_cloud_consent_service().can_start_voice_session(user_id)
    except Exception as exc:
        logger.warning("Cloud phone full-consent lookup failed for user=%s: %s", user_id, exc)
        raise PermissionError("Cloud consent checks are temporarily unavailable. Try again in a moment.") from exc
    if result.allowed:
        return
    logger.warning("Cloud phone full consent missing for user=%s: %s", user_id, result.missing)
    raise PermissionError(result.message or "Cloud consent is required before making AI phone calls.")


def _phone_hold_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "enter_hold_mode",
            "description": (
                "Call this when the other party asks you to hold, wait, "
                "or says 'one moment please'. Mutes your microphone and "
                "waits efficiently until they return. Prefer this over keypad "
                "choices like 'press 2 to keep holding' when the task is simply "
                "to remain on the line."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


def _phone_control_openai_tools() -> list[dict[str, Any]]:
    from telephony.call_tools import (
        CONSULT_USER_TOOL,
        END_CALL_TOOL,
        PRESS_BUTTON_TOOL,
        SAVE_CALL_RESULT_TOOL,
    )

    return [
        _phone_hold_tool_schema(),
        PRESS_BUTTON_TOOL,
        CONSULT_USER_TOOL,
        SAVE_CALL_RESULT_TOOL,
        END_CALL_TOOL,
    ]


def _openai_tool_name(tool: dict[str, Any]) -> str:
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("name", "")).strip()
    return str(tool.get("name", "")).strip()


def _mcp_tools_to_phone_openai_tools(
    mcp_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not mcp_tools:
        return []
    if all(isinstance(tool.get("function"), dict) and tool.get("type") == "function" for tool in mcp_tools):
        return list(mcp_tools)

    from services.llm.providers.openai_compatible import _mcp_tools_to_openai

    return _mcp_tools_to_openai(mcp_tools)


def _openai_tools_to_pipecat_tools_schema(openai_tools: list[dict[str, Any]]) -> Any:
    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.adapters.schemas.tools_schema import ToolsSchema

    class _RawParametersFunctionSchema(FunctionSchema):
        """FunctionSchema variant that preserves the full OpenAI parameters object."""

        def __init__(self, *, name: str, description: str, parameters: dict[str, Any]) -> None:
            properties = parameters.get("properties")
            required = parameters.get("required")
            super().__init__(
                name=name,
                description=description,
                properties=properties if isinstance(properties, dict) else {},
                required=required if isinstance(required, list) else [],
            )
            self._raw_parameters = parameters

        def to_default_dict(self) -> dict[str, Any]:
            return {
                "name": self.name,
                "description": self.description,
                "parameters": self._raw_parameters,
            }

    standard_tools: list[FunctionSchema] = []
    for tool in openai_tools:
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name", "")).strip()
        if not name:
            continue
        parameters = function.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}, "required": []}
        elif parameters.get("type") != "object":
            parameters = {**parameters, "type": "object"}
        parameters.setdefault("properties", {})
        parameters.setdefault("required", [])
        standard_tools.append(
            _RawParametersFunctionSchema(
                name=name,
                description=str(function.get("description", "")),
                parameters=parameters,
            )
        )

    return ToolsSchema(standard_tools=standard_tools)


def build_phone_llm_tool_surface(
    mcp_tools: list[dict[str, Any]] | None = None,
    *,
    phone_mode: str = "local",
) -> PhoneLLMToolSurface:
    """Build the phone LLM tool surface from desktop MCP tools plus phone controls."""

    raw_mcp_tools = list(mcp_tools or [])
    normalized_phone_mode = str(phone_mode or "").strip().lower()
    desktop_openai_tools = _mcp_tools_to_phone_openai_tools(raw_mcp_tools)
    phone_openai_tools = _phone_control_openai_tools()

    combined: list[dict[str, Any]] = []
    index_by_name: dict[str, int] = {}
    mcp_name_by_llm_name: dict[str, str] = {}

    for raw_tool, openai_tool in zip(raw_mcp_tools, desktop_openai_tools, strict=False):
        llm_name = _openai_tool_name(openai_tool)
        if not llm_name:
            continue
        if llm_name in _PHONE_DESKTOP_TOOL_BLOCKLIST:
            continue
        if normalized_phone_mode == "cloud" and llm_name == "transmit_payment_to_call":
            continue
        if llm_name in index_by_name:
            continue
        index_by_name[llm_name] = len(combined)
        combined.append(openai_tool)
        mcp_name_by_llm_name[llm_name] = str(raw_tool.get("name", llm_name))

    for openai_tool in phone_openai_tools:
        llm_name = _openai_tool_name(openai_tool)
        if not llm_name:
            continue
        if llm_name in index_by_name:
            combined[index_by_name[llm_name]] = openai_tool
        else:
            index_by_name[llm_name] = len(combined)
            combined.append(openai_tool)
        mcp_name_by_llm_name[llm_name] = llm_name

    desktop_tool_names = [_openai_tool_name(tool) for tool in desktop_openai_tools if _openai_tool_name(tool)]
    phone_tool_names = [_openai_tool_name(tool) for tool in phone_openai_tools if _openai_tool_name(tool)]
    return PhoneLLMToolSurface(
        tools_schema=_openai_tools_to_pipecat_tools_schema(combined),
        openai_tools=combined,
        mcp_name_by_llm_name=mcp_name_by_llm_name,
        desktop_tool_names=desktop_tool_names,
        phone_tool_names=phone_tool_names,
    )


def _phone_provider_desktop_tools_from_runtime(
    phone_tool_runtime: Any | None,
    *,
    user_id: str | None,
) -> tuple[list[dict[str, Any]], Any | None]:
    if phone_tool_runtime is None:
        return [], None

    full_tools = list(getattr(phone_tool_runtime, "visible_tools", []) or [])
    hub = getattr(phone_tool_runtime, "hub", None)
    get_deferred_pool = getattr(hub, "get_deferred_tool_pool", None)
    if not callable(get_deferred_pool):
        return full_tools, None

    try:
        deferred_pool = get_deferred_pool(
            tier="symphony",
            interactive=True,
            user_id=user_id,
        )
    except Exception:  # noqa: BLE001, RUF100 - degrade to full tool surface, never break a live call
        logger.debug("Phone deferred tool surface unavailable; using full visible tool surface")
        return full_tools, None

    provider_tools = list(getattr(deferred_pool, "visible_tools", []) or [])
    return provider_tools, deferred_pool


def _phone_native_tool_name(tool: dict[str, Any]) -> str:
    name = str(tool.get("name", "") or "").strip()
    if name:
        return name
    return _openai_tool_name(tool)


def _phone_deferred_expansion_messages(*, llm_tool_name: str, tool_result: Any, executor: Any) -> list[dict[str, Any]]:
    try:
        from intent.tool_result_mappers import map_tool_result_to_content

        content = map_tool_result_to_content(tool_name=llm_tool_name, tool_result=tool_result, executor=executor)
    except Exception:  # noqa: BLE001, RUF100 - expansion is optional; tool result still returns to the model
        logger.debug("Phone deferred ToolSearch content mapping failed", exc_info=True)
        return []

    if not isinstance(content, list) or not content:
        return []
    return [
        {
            "role": "tool",
            "content": [
                {
                    "type": "tool_result",
                    "content": content,
                }
            ],
        }
    ]


def _phone_llm_has_function(llm: Any, function_name: str) -> bool:
    has_function = getattr(llm, "has_function", None)
    if callable(has_function):
        try:
            return bool(has_function(function_name))
        except Exception:  # noqa: BLE001, RUF100 - fall back to private storage probe if available
            logger.debug(
                "Phone LLM has_function probe failed for %s",
                function_name,
                exc_info=True,
            )

    functions = getattr(llm, "_functions", None)
    return isinstance(functions, dict) and function_name in functions


def _register_phone_mcp_llm_function(
    *,
    llm: Any,
    llm_tool_name: str,
    phone_mcp_tool_handler: Any,
    call_record: CallRecord,
) -> bool:
    if llm_tool_name in _PHONE_CONTROL_TOOL_NAMES:
        return False
    if _phone_llm_has_function(llm, llm_tool_name):
        return False

    guarded = _guard_phone_function_handler(llm_tool_name, phone_mcp_tool_handler, call_record)
    try:
        llm.register_function(
            llm_tool_name,
            guarded,
            cancel_on_interruption=False,
            timeout_secs=_PHONE_MCP_TOOL_PIPELINE_TIMEOUT_SECS,
        )
    except TypeError as exc:
        if "timeout_secs" not in str(exc):
            raise
        llm.register_function(
            llm_tool_name,
            guarded,
            cancel_on_interruption=False,
        )
    return True


def _expand_phone_deferred_tool_references(
    *,
    llm_tool_name: str,
    tool_result: Any,
    executor: Any,
    mcp_name_by_llm_name: dict[str, str],
    provider_desktop_tools: list[dict[str, Any]],
    full_mcp_tools: list[dict[str, Any]],
    llm_context: Any,
    llm: Any,
    phone_mode: str,
    phone_mcp_tool_handler: Any,
    call_record: CallRecord,
) -> int:
    """Make ToolSearch-selected deferred desktop tools reachable on the phone path."""

    if llm_tool_name not in {"ToolSearch", "tool_search"}:
        return 0
    if llm_context is None or llm is None:
        return 0

    try:
        from intent.tools.deferred_tool_schemas import (
            expand_tools_with_deferred_references,
        )
    except Exception:  # noqa: BLE001, RUF100 - optional dependency; do not break live phone calls
        logger.debug("Phone deferred tool expansion import failed", exc_info=True)
        return 0

    allowed_names = {_phone_native_tool_name(tool) for tool in full_mcp_tools if isinstance(tool, dict)}
    allowed_names.discard("")
    if not allowed_names:
        return 0

    messages: list[dict[str, Any]] = []
    get_messages = getattr(llm_context, "get_messages", None)
    if callable(get_messages):
        try:
            messages.extend(list(get_messages() or []))
        except Exception:  # noqa: BLE001, RUF100 - synthetic ToolSearch message below is the important part
            logger.debug(
                "Phone deferred expansion could not read LLM context messages",
                exc_info=True,
            )
    messages.extend(
        _phone_deferred_expansion_messages(
            llm_tool_name=llm_tool_name,
            tool_result=tool_result,
            executor=executor,
        )
    )

    expanded_desktop_tools = expand_tools_with_deferred_references(
        provider_desktop_tools,
        messages,
        allowed_names=allowed_names,
        user_id=str(getattr(call_record, "user_id", "") or ""),
    )
    if expanded_desktop_tools is None:
        return 0

    existing_names = {_phone_native_tool_name(tool) for tool in provider_desktop_tools if isinstance(tool, dict)}
    expanded_names = {_phone_native_tool_name(tool) for tool in expanded_desktop_tools if isinstance(tool, dict)}
    added_native_names = expanded_names - existing_names
    if not added_native_names:
        return 0

    expanded_surface = build_phone_llm_tool_surface(
        expanded_desktop_tools,
        phone_mode=phone_mode,
    )
    new_llm_names = set(expanded_surface.mcp_name_by_llm_name) - set(mcp_name_by_llm_name)
    llm_context.set_tools(expanded_surface.tools_schema)
    provider_desktop_tools[:] = expanded_desktop_tools
    mcp_name_by_llm_name.clear()
    mcp_name_by_llm_name.update(expanded_surface.mcp_name_by_llm_name)

    registered = 0
    for new_llm_name in sorted(new_llm_names):
        if new_llm_name in _PHONE_CONTROL_TOOL_NAMES:
            continue
        if _register_phone_mcp_llm_function(
            llm=llm,
            llm_tool_name=new_llm_name,
            phone_mcp_tool_handler=phone_mcp_tool_handler,
            call_record=call_record,
        ):
            registered += 1

    logger.info(
        "Phone deferred tool surface expanded after ToolSearch: added=%d registered=%d total=%d",
        len(added_native_names),
        registered,
        len(expanded_surface.openai_tools),
    )
    return registered


def _maybe_build_phone_codex_client(config: Any) -> Any:
    """Return an AsyncOpenAI client wired through codex-auth, or None.

    The phone pipeline's TracedOpenAIResponsesLLMService accepts an optional
    pre-built client via the ``openai_client`` kwarg. When the user has run
    ``codex login`` and consented to cloud LLM use, we route the phone agent
    through the same Codex/ChatGPT-Plus path the desktop agent uses
    (services/llm/factory.py:_create_codex_provider) — zero per-token cost,
    same Responses API surface as the api.openai.com endpoint, same
    reasoning-effort support.

    Returns None when:
      - cloud LLM consent is not given
      - ~/.codex/auth.json is missing (user hasn't run `codex login`)
      - the configured llm_model isn't a gpt-5.x family model (Codex endpoint
        constraint per knowledge/PHONE-11)
      - any unexpected error during client construction (defensive)

    On None, the LLM service falls back to the config's openai_api_key.
    """
    try:
        # Honor the user's explicit AI source. The codex/ChatGPT-Plus path is a
        # cost optimization, but it must only engage when the user actually chose
        # ai_source=codex. Routing the phone through codex while the user selected
        # managed silently ignores their choice AND fails hard when the codex
        # subscription is out (429 usage_limit) instead of using the managed key
        # (2026-06-25: ai_source=managed phone runs still hit the dead codex path).
        from ui.settings_manager import get_settings_manager

        ai_source = str(get_settings_manager().get("ai_source", "") or "").strip().lower()
        if ai_source != "codex":
            logger.debug("Phone LLM Codex path skipped: ai_source=%r is not codex", ai_source)
            return None

        from core.privacy_consent import is_cloud_llm_consented

        if not is_cloud_llm_consented():
            logger.debug("Phone LLM Codex path skipped: cloud LLM consent missing")
            return None

        model = str(getattr(config, "llm_model", "") or "").strip().lower()
        if not model.startswith("gpt-5"):
            logger.debug(
                "Phone LLM Codex path skipped: model=%r not in gpt-5.x family",
                model,
            )
            return None

        from services.llm.codex_auth import (
            create_codex_openai_client,
            is_codex_available,
        )

        if not is_codex_available():
            logger.debug("Phone LLM Codex path skipped: ~/.codex/auth.json not found")
            return None

        client = create_codex_openai_client()
        logger.info("Phone LLM auth: using Codex/ChatGPT-Plus subscription (model=%s)", model)
        return client
    except Exception:
        logger.exception("Phone LLM Codex client construction failed; falling back to api_key")
        return None


def _create_phone_voicemail_classifier_llm(
    config: Any,
    *,
    openai_client: Any,
    user_id: str,
    task_trace_writer: Any | None = None,
) -> Any:
    """Build the separate Pipecat LLM processor used only for voicemail classification."""

    from pipecat.services.openai.responses.llm import OpenAIResponsesLLMService

    from telephony.traced_openai_responses_llm_service import (
        TracedOpenAIResponsesLLMService,
    )

    service = TracedOpenAIResponsesLLMService(
        api_key=config.openai_api_key,
        settings=OpenAIResponsesLLMService.Settings(
            model=config.llm_model,
            # No reasoning.summary: same PHONE-LATENCY-01 reason as the main phone LLM.
            # This is the one-shot voicemail classifier, which sits directly in front of
            # Viola's FIRST spoken word, so a second output stream is worst here.
            extra={"reasoning": {"effort": defaults.resolve_reasoning_effort("none", config.llm_model)}},
        ),
        task_trace_writer=task_trace_writer,
        openai_client=openai_client,
    )
    if getattr(service, "_client", None) is not None:
        service._client = _wrap_openai_client_for_phone_spend_accounting(
            service._client,
            user_id=user_id,
        )
    return service


@dataclass(frozen=True)
class _OpenAITokenUsage:
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return max(1, int(self.input_tokens) + int(self.output_tokens))


def _coerce_nonnegative_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and value.is_integer():
        return max(0, int(value))
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return max(0, int(stripped))
    return default


def _get_mapping_or_attr(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _estimate_openai_token_usage(payload: dict[str, Any], *, default_output_tokens: int) -> _OpenAITokenUsage:
    requested_output = (
        _coerce_nonnegative_int(payload.get("max_output_tokens"))
        or _coerce_nonnegative_int(payload.get("max_completion_tokens"))
        or _coerce_nonnegative_int(payload.get("max_tokens"))
        or _coerce_nonnegative_int(default_output_tokens)
        or 1
    )
    try:
        serialized = json.dumps(payload, default=str, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        serialized = str(payload)
    # Conservative approximation: OpenAI text payloads are roughly 4 chars/token.
    estimated_input = max(1, math.ceil(len(serialized) / 4))
    return _OpenAITokenUsage(input_tokens=estimated_input, output_tokens=requested_output)


def _usage_from_openai_usage(value: Any, fallback: _OpenAITokenUsage) -> _OpenAITokenUsage:
    if value is None:
        return fallback

    input_tokens = (
        _coerce_nonnegative_int(_get_mapping_or_attr(value, "input_tokens"))
        or _coerce_nonnegative_int(_get_mapping_or_attr(value, "prompt_tokens"))
        or fallback.input_tokens
    )
    output_tokens = (
        _coerce_nonnegative_int(_get_mapping_or_attr(value, "output_tokens"))
        or _coerce_nonnegative_int(_get_mapping_or_attr(value, "completion_tokens"))
        or fallback.output_tokens
    )
    input_details = _get_mapping_or_attr(value, "input_tokens_details") or _get_mapping_or_attr(
        value,
        "prompt_tokens_details",
    )
    cached_tokens = (
        _coerce_nonnegative_int(_get_mapping_or_attr(input_details, "cached_tokens"))
        or _coerce_nonnegative_int(_get_mapping_or_attr(value, "cached_tokens"))
        or _coerce_nonnegative_int(_get_mapping_or_attr(value, "cache_read_tokens"))
    )
    return _OpenAITokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
    )


def _usage_from_chat_completion_payload(
    payload: dict[str, Any],
    fallback: _OpenAITokenUsage,
) -> _OpenAITokenUsage:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    return _usage_from_openai_usage(usage, fallback)


def _usage_from_response_object(value: Any, fallback: _OpenAITokenUsage) -> _OpenAITokenUsage:
    response = _get_mapping_or_attr(value, "response") or value
    usage = _get_mapping_or_attr(response, "usage")
    return _usage_from_openai_usage(usage, fallback)


def _estimated_spend_cents(model: str, usage: _OpenAITokenUsage) -> int:
    try:
        from services.llm.pricing import calculate_cost_cents

        cents = calculate_cost_cents(
            model,
            usage.input_tokens,
            usage.output_tokens,
            usage.cached_tokens,
        )
    except Exception as exc:
        logger.debug("OpenAI spend estimate failed for model %s: %s", model, exc)
        return 1
    return max(1, math.ceil(float(cents)))


class _OpenAISpendAccounting:
    def __init__(
        self,
        *,
        user_id: str,
        model: str,
        estimated_usage: _OpenAITokenUsage,
        operation: str,
        settle_spend: bool = True,
    ) -> None:
        self.user_id = user_id or ""
        self.model = model
        self.estimated_usage = estimated_usage
        self.operation = operation
        self.settle_spend = settle_spend
        self._reserved = False
        self._settled = False
        self._spend_reservation: Any | None = None

    async def reserve(self) -> None:
        if not company_phone_billing_available():
            return  # User-owned provider accounts have no company spend reservation.
        from billing.managed_llm_budget import reserve_managed_llm_spend_cap_async
        from core.exceptions import LLMQuotaExceededError
        from services.llm.rate_limiter import get_rate_limiter

        gate = await reserve_managed_llm_spend_cap_async(
            self.user_id,
            estimated_cents=_estimated_spend_cents(self.model, self.estimated_usage),
        )
        if not gate.allowed:
            raise LLMQuotaExceededError(
                user_id=self.user_id or "<missing>",
                limit_type="managed_llm_spend_cap",
                current=gate.spent_cents,
                limit=gate.budget_cents,
                reset_at=gate.resets_at,
            )
        self._spend_reservation = gate.reservation

        await get_rate_limiter().reserve(
            self.user_id,
            estimated_tokens=self.estimated_usage.total_tokens,
        )
        self._reserved = True

    async def settle(self, actual_usage: _OpenAITokenUsage | None = None, *, failed: bool = False) -> None:
        if self._settled:
            return
        self._settled = True

        usage = actual_usage or self.estimated_usage
        rate_limiter_actual = 0 if failed else usage.total_tokens
        if self._reserved:
            try:
                from services.llm.rate_limiter import get_rate_limiter

                await get_rate_limiter().settle(
                    self.user_id,
                    self.estimated_usage.total_tokens,
                    rate_limiter_actual,
                )
            except Exception:
                # Settle failure leaks the rate-limiter reservation — slow
                # user-lockout if it recurs.  Promote DEBUG -> ERROR via
                # logger.exception so the failure surfaces in prod logs.
                logger.exception(
                    "OpenAI rate-limiter settle failed (reserved tokens leaked) operation=%s user_id=%s",
                    self.operation,
                    self.user_id,
                )

        if not self.settle_spend or not self.user_id or not company_phone_billing_available():
            return

        try:
            from billing.plan_limiter import get_plan_limiter

            limiter = get_plan_limiter()
            if self._spend_reservation is not None:
                limiter.settle_spend_reservation(
                    self._spend_reservation,
                    actual_cents=_estimated_spend_cents(self.model, usage),
                    failed=failed,
                )
            elif not failed:
                limiter.settle_spend(
                    self.user_id,
                    self.model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cached_tokens,
                )
        except Exception:
            # A settle failure (in practice a transient Postgres timeout in the
            # plan-limiter worker loop — billing/plan_limiter.py:_pg_run's 15s
            # future.result) means we could not reconcile this call's reservation
            # down to its actual cost. The reserved estimate was already debited
            # fail-closed at reserve time, so the worst-case impact is a small
            # over-hold against the user's OWN spend counter — never a spend-cap
            # *breach*. It MUST NOT block the user. Blocking here is the same
            # class as the 2026-06-23 phone-billing lockout (commit 3fa40607): a
            # transient DB blip during settle added the paying user to the live
            # worker's in-memory _blocked_users set (no auto-unblock), denying
            # every later request for the worker's lifetime while the DB spend
            # and blocked_users tables were empty. Surface the (possibly leaked)
            # reservation loudly for ops reconciliation, but leave the account
            # fully usable; do NOT block_user and do NOT raise billing-closed.
            logger.exception(
                "OpenAI spend settle failed for operation=%s user_id=%s model=%s; "
                "reservation may be leaked (transient settle error, NOT a cap breach) — "
                "leaving account usable. Reconcile the leaked hold via billing ops if it recurs.",
                self.operation,
                self.user_id,
                self.model,
            )


def _openai_accounting_from_payload(
    payload: dict[str, Any],
    *,
    user_id: str,
    operation: str,
    default_output_tokens: int,
) -> _OpenAISpendAccounting:
    model = str(payload.get("model") or "").strip()
    estimated_usage = _estimate_openai_token_usage(payload, default_output_tokens=default_output_tokens)
    return _OpenAISpendAccounting(
        user_id=user_id,
        model=model,
        estimated_usage=estimated_usage,
        operation=operation,
    )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class _AccountedOpenAIStream:
    def __init__(self, stream: Any, accounting: _OpenAISpendAccounting) -> None:
        self._stream = stream
        self._accounting = accounting
        self._iterator: Any = None
        self._actual_usage: _OpenAITokenUsage | None = None
        self._failed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    def __aiter__(self) -> _AccountedOpenAIStream:
        if self._iterator is None:
            self._iterator = self._stream.__aiter__()
        return self

    async def __anext__(self) -> Any:
        if self._iterator is None:
            self._iterator = self._stream.__aiter__()
        try:
            event = await self._iterator.__anext__()
        except StopAsyncIteration:
            await self._settle_success()
            raise
        except Exception:
            self._failed = True
            await self._accounting.settle(_OpenAITokenUsage(0, 0), failed=True)
            raise

        response = _get_mapping_or_attr(event, "response")
        usage = _get_mapping_or_attr(response, "usage") if response is not None else None
        if usage is not None:
            self._actual_usage = _usage_from_openai_usage(usage, self._accounting.estimated_usage)
        return event

    async def _settle_success(self) -> None:
        if self._failed:
            return
        await self._accounting.settle(self._actual_usage)

    async def close(self) -> None:
        try:
            close = getattr(self._stream, "close", None)
            if callable(close):
                await _maybe_await(close())
        finally:
            await self._settle_success()

    async def aclose(self) -> None:
        try:
            aclose = getattr(self._stream, "aclose", None)
            if callable(aclose):
                await _maybe_await(aclose())
            else:
                await self.close()
                return
        finally:
            await self._settle_success()


class _AccountedResponsesResource:
    def __init__(self, wrapped: Any, *, user_id: str, operation: str) -> None:
        self._wrapped = wrapped
        self._user_id = user_id
        self._operation = operation

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    async def create(self, **kwargs: Any) -> Any:
        payload = dict(kwargs)
        accounting = _openai_accounting_from_payload(
            payload,
            user_id=self._user_id,
            operation=self._operation,
            default_output_tokens=payload.get("max_output_tokens") or 1024,
        )
        await accounting.reserve()
        try:
            response = await self._wrapped.create(**kwargs)
        except Exception:
            await accounting.settle(_OpenAITokenUsage(0, 0), failed=True)
            raise

        if payload.get("stream") or hasattr(response, "__aiter__"):
            return _AccountedOpenAIStream(response, accounting)

        await accounting.settle(_usage_from_response_object(response, accounting.estimated_usage))
        return response


class _AccountedOpenAIClient:
    _viola_spend_accounted = True

    def __init__(self, wrapped: Any, *, user_id: str, operation: str) -> None:
        self._wrapped = wrapped
        self._user_id = user_id
        self._operation = operation

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    @property
    def responses(self) -> _AccountedResponsesResource:
        return _AccountedResponsesResource(
            self._wrapped.responses,
            user_id=self._user_id,
            operation=self._operation,
        )


def _wrap_openai_client_for_phone_spend_accounting(openai_client: Any, *, user_id: str) -> Any:
    if openai_client is None or getattr(openai_client, "_viola_spend_accounted", False):
        return openai_client
    return _AccountedOpenAIClient(
        openai_client,
        user_id=user_id,
        operation="phone_call_llm",
    )


async def create_accounted_openai_chat_completion(
    *,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    max_output_tokens: int,
    user_id: str,
    operation: str,
) -> dict[str, Any]:
    import httpx

    from core.constants import TIMEOUT_LONG

    payload = {
        "model": model,
        "messages": messages,
        **phone_chat_completion_options(model, max_output_tokens),
    }
    accounting = _openai_accounting_from_payload(
        payload,
        user_id=user_id,
        operation=operation,
        default_output_tokens=max_output_tokens,
    )
    await accounting.reserve()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_LONG) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": "Bearer %s" % api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
    except Exception:
        await accounting.settle(_OpenAITokenUsage(0, 0), failed=True)
        raise

    await accounting.settle(_usage_from_chat_completion_payload(data, accounting.estimated_usage))
    return data


def _tool_result_callback_payload(tool_result: Any) -> dict[str, Any]:
    ok = bool(getattr(tool_result, "ok", False))
    payload: dict[str, Any] = {
        "success": ok,
        "data": getattr(tool_result, "data", None),
    }
    error = getattr(tool_result, "error", None)
    if error:
        payload["error"] = str(error)
    to_llm_text = getattr(tool_result, "to_llm_text", None)
    if callable(to_llm_text):
        payload["result"] = to_llm_text()
    return payload


def _telnyx_response_preview(exc: Exception) -> tuple[int | None, str]:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
    body = str(getattr(response, "text", "") or getattr(exc, "body", "") or "").strip()
    if body:
        body = body[:_TELNYX_ERROR_PREVIEW_CHARS]
    return status_code, body


def _describe_telnyx_dial_error(exc: Exception) -> str:
    status_code, body = _telnyx_response_preview(exc)
    if status_code is not None:
        if body:
            return "Telnyx dial failed (HTTP %s): %s" % (status_code, body)
        return "Telnyx dial failed (HTTP %s): %s" % (status_code, exc)
    return "Telnyx dial failed: %s" % exc


def _redact_stream_url(stream_url: str) -> str:
    parsed = urlparse(stream_url)
    if not parsed.query:
        return stream_url

    redacted_pairs: list[tuple[str, str]] = []
    changed = False
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        key_lower = key.lower()
        if any(token in key_lower for token in _STREAM_URL_SECRET_QUERY_TOKENS):
            redacted_pairs.append((key, "<redacted>"))
            changed = True
        else:
            redacted_pairs.append((key, value))
    if not changed:
        return stream_url
    return urlunparse(parsed._replace(query=urlencode(redacted_pairs)))


class StreamUrlPreflightError(ServiceError):
    """Raised when no safe Telnyx media stream URL is available for dialing."""

    severity = ErrorSeverity.HIGH
    retryable = True

    def __init__(self, stream_url: str, reason: str, *, cause: Exception | None = None) -> None:
        redacted_stream_url = _redact_stream_url(stream_url)
        super().__init__(
            "Telnyx stream URL preflight failed: %s" % reason,
            ErrorContext(
                component="telephony",
                operation="stream_url_preflight",
                params={"stream_url": redacted_stream_url, "reason": reason},
                user_message="Phone media is not reachable. Check the phone tunnel before dialing.",
                recovery_hint="Verify TELNYX_PUBLIC_WS_URL or restart the Cloudflare phone tunnel.",
            ),
            cause=cause,
        )
        self.stream_url = redacted_stream_url
        self.reason = reason


def get_phone_tunnel(port: int = 8770):
    """Resolve the phone tunnel helper lazily so unit tests can patch this seam."""
    from telephony.tunnel import get_phone_tunnel as _get_phone_tunnel

    return _get_phone_tunnel(port)


def _resolve_dns_for_stream_host(host: str) -> str:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="phone-dns-preflight")
    future = executor.submit(socket.gethostbyname, host)
    try:
        return future.result(timeout=_STREAM_URL_PREFLIGHT_TIMEOUT_SECS)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _stream_url_preflight_failure_reason(stream_url: str) -> str | None:
    parsed = urlparse(stream_url)
    if parsed.scheme.lower() != "wss":
        return "local Telnyx media requires a public wss:// URL"

    host = (parsed.hostname or "").strip().lower()
    if not host:
        return "configured stream URL has no host"
    if host in _LOCAL_STREAM_HOSTS:
        return "configured stream URL host %s is not publicly reachable by Telnyx" % host
    if "trycloudflare.com" in host:
        return "configured stream URL is an expiring Cloudflare quick-tunnel hostname"

    try:
        port = parsed.port or _WSS_DEFAULT_PORT
    except ValueError as exc:
        return "configured stream URL has an invalid port: %s" % exc

    try:
        resolved_ip = _resolve_dns_for_stream_host(host)
    except FutureTimeoutError:
        return "DNS lookup for %s timed out after %.1fs" % (
            host,
            _STREAM_URL_PREFLIGHT_TIMEOUT_SECS,
        )
    except OSError as exc:
        return "DNS lookup for %s failed: %s" % (host, exc)

    try:
        with socket.create_connection((host, port), timeout=_STREAM_URL_PREFLIGHT_TIMEOUT_SECS):
            pass
    except OSError as exc:
        return "TCP connect to %s:%d failed after resolving %s: %s" % (
            host,
            port,
            resolved_ip,
            exc,
        )

    return None


def _resolve_quick_tunnel_stream_url(config: TelnyxConfig, stale_stream_url: str, reason: str) -> str:
    logger.warning(
        "Configured Telnyx stream URL %r failed preflight (%s); trying Cloudflare quick tunnel fallback",
        _redact_stream_url(stale_stream_url),
        reason,
    )
    try:
        fallback_url = get_phone_tunnel(config.ws_port).ensure_running()
    except Exception as exc:
        logger.error(
            "Telnyx stream URL preflight failed and quick tunnel fallback could not start: %s",
            exc,
        )
        raise StreamUrlPreflightError(stale_stream_url, reason, cause=exc) from exc

    if not fallback_url or not fallback_url.lower().startswith("wss://"):
        fallback_reason = "quick tunnel fallback returned non-wss stream URL %r" % fallback_url
        logger.error("Telnyx stream URL preflight failed: %s", fallback_reason)
        raise StreamUrlPreflightError(stale_stream_url, fallback_reason)

    logger.info("Using Cloudflare quick tunnel fallback for Telnyx media after preflight failure")
    return fallback_url


def _resolve_stream_url_for_dial(config: TelnyxConfig, fallback_stream_url: str) -> str:
    """Return the Telnyx media stream URL for a dial request.

    Local phone calls prefer the stable Viola_app named tunnel. Before dialing,
    preflight the configured wss:// hostname so a stale DNS/ingress config does
    not leak into Telnyx as a 422 media-stream failure.
    """
    stream_url = (config.stream_ws_url or fallback_stream_url or "").strip()
    if config.mode != "local":
        return stream_url or fallback_stream_url

    # Independent installations use explicitly configured ingress. Starting an
    # unrelated quick tunnel would change the authenticated media endpoint.
    if not company_phone_billing_available():
        stale_reason = _stream_url_preflight_failure_reason(stream_url)
        if stale_reason is not None:
            raise StreamUrlPreflightError(stream_url, stale_reason)
        return stream_url

    stale_reason = _stream_url_preflight_failure_reason(stream_url)
    if stale_reason is not None:
        return _resolve_quick_tunnel_stream_url(config, stream_url, stale_reason)
    return stream_url


async def _remote_warm_loop(
    call_id: str,
    warm_ready: asyncio.Event,
    started_monotonic: float,
    *,
    ping_timeout_s: float,
    keepalive_s: float,
) -> None:
    """Own the remote-voice worker's warm state for one outbound call.

    Fires ``warm_remote_voice()`` (one warmup inference at the remote RunPod
    STT/TTS worker) in a loop. Sets ``warm_ready`` the first time a warmup
    inference RETURNS within the timeout — the only reliable warm signal, since
    the worker can report "ready" while its model is still cold, and only an
    ACTUAL inference resets its idle timer. After warm, keeps pinging at
    ``keepalive_s`` (below the worker's idle timeout) so warm -> dial -> ring ->
    opening cannot let the worker recool. The caller cancels this task once real
    call traffic sustains warmth (opening turn) and in the finally cleanup.

    Stateless: holds nothing at module scope; the Event and timings are the
    caller's. Never raises out of the task (``warm_remote_voice`` never raises
    per its contract; the guard covers surprises so the task cannot die silently
    and strand the keep-alive).
    """
    from telephony import remote_voice

    while True:
        ping_started = time.monotonic()
        try:
            warm = await asyncio.to_thread(remote_voice.warm_remote_voice, timeout_s=ping_timeout_s)
        # Best-effort: a warm ping must never break the call (contract says never raises).
        except Exception as exc:  # noqa: BLE001, RUF100
            logger.debug("Call %s: remote warm ping raised (ignored): %s", call_id, exc)
            warm = False
        now = time.monotonic()
        if warm:
            if not warm_ready.is_set():
                logger.info(
                    "Call %s: remote-worker warm-signal returned at t=%.3fs (ping=%.3fs) — dial gate may open",
                    call_id,
                    now - started_monotonic,
                    now - ping_started,
                )
                warm_ready.set()
            else:
                logger.debug(
                    "Call %s: remote-worker keep-alive ping ok at t=%.3fs (ping=%.3fs)",
                    call_id,
                    now - started_monotonic,
                    now - ping_started,
                )
            await asyncio.sleep(keepalive_s)
        else:
            logger.info(
                "Call %s: remote-worker still cold at t=%.3fs (ping=%.3fs) — retrying",
                call_id,
                now - started_monotonic,
                now - ping_started,
            )
            # A cold miss already blocked ~ping_timeout_s inside warm_remote_voice;
            # a short floor prevents a hot spin on any fast/no-op False.
            await asyncio.sleep(0.5)


async def _await_remote_warm_gate(
    call_id: str,
    warm_ready: asyncio.Event,
    started_monotonic: float,
    *,
    ceiling_s: float,
) -> bool:
    """Block the dial until the remote worker reports a real warm signal.

    Waits up to ``ceiling_s`` for ``warm_ready`` (set by ``_remote_warm_loop``
    when a warmup inference RETURNED). Returns True if warm before the ceiling,
    False if the ceiling was hit while still cold — in which case the caller
    dials anyway (the reworked ``remote_voice`` cooldown, which retries + uses a
    cold worker once warm within the same call, plus the keep-alive loop, are
    the safety net). This is a signal gate, never a fixed timer / "assume warm
    after N seconds".
    """
    gate_wait_start = time.monotonic()
    if warm_ready.is_set():
        # Diagnostic: one line per call, grep-able by "dial-gate diagnostic". The
        # outcome token distinguishes a real warm-signal open from a cap-fallthrough
        # (a fixed timer would ALWAYS report the ceiling as the wait; a signal gate
        # reports the actual sub-ceiling wait). gate_wait = time spent at the gate;
        # warm_wait = time since warm-start (setup + gate).
        logger.info(
            "Call %s: dial-gate diagnostic outcome=warm-signal-hit(pre-set) "
            "gate_wait=%.3fs warm_wait=%.3fs ceiling=%.1fs — dialing immediately",
            call_id,
            time.monotonic() - gate_wait_start,
            time.monotonic() - started_monotonic,
            ceiling_s,
        )
        return True
    try:
        await asyncio.wait_for(warm_ready.wait(), timeout=ceiling_s)
    except TimeoutError:
        logger.warning(
            "Call %s: dial-gate diagnostic outcome=cap-fallthrough gate_wait=%.3fs "
            "warm_wait=%.3fs ceiling=%.1fs — remote worker still cold; dialing anyway "
            "(cooldown + keep-alive are the safety net)",
            call_id,
            time.monotonic() - gate_wait_start,
            time.monotonic() - started_monotonic,
            ceiling_s,
        )
        return False
    logger.info(
        "Call %s: dial-gate diagnostic outcome=warm-signal-hit gate_wait=%.3fs "
        "warm_wait=%.3fs ceiling=%.1fs — dial-gate opened on warm signal",
        call_id,
        time.monotonic() - gate_wait_start,
        time.monotonic() - started_monotonic,
        ceiling_s,
    )
    return True


def _install_pipecat_loguru_bridge() -> int | None:
    """Bridge Pipecat's loguru output into Viola's structured logger.

    Without this, Pipecat's internal logs (VAD events, LLM calls, frame
    processing) are invisible in viola-qt.log because Pipecat uses loguru
    directly while Viola uses core.logging_config.
    """
    global _loguru_bridge_handler_id, _loguru_bridge_installed
    if _loguru_bridge_installed:
        return _loguru_bridge_handler_id
    try:
        from loguru import logger as loguru_logger  # noqa: TID251

        def _pipecat_to_viola(message):
            rec = message.record
            level = rec["level"].name
            origin = "%s:%s:%s" % (rec["name"], rec["function"], rec["line"])
            text = _mask_phone_numbers_in_text(str(rec["message"]).rstrip())
            if level in ("DEBUG", "TRACE"):
                logger.debug("pipecat| %s - %s", origin, text)
            elif level == "WARNING":
                logger.warning("pipecat| %s - %s", origin, text)
            elif level == "ERROR":
                logger.error("pipecat| %s - %s", origin, text)
            else:
                logger.info("pipecat| %s - %s", origin, text)

        _loguru_bridge_handler_id = loguru_logger.add(
            _pipecat_to_viola,
            level="DEBUG",
            filter=lambda record: str(record["name"]).startswith("pipecat"),
            format="{message}",
            colorize=False,
        )
        _loguru_bridge_installed = True
        logger.info("Pipecat loguru -> Viola logger bridge installed")
        return _loguru_bridge_handler_id
    except Exception:
        logger.debug("Could not install Pipecat loguru bridge")
        return None


def _reset_pipecat_loguru_bridge_for_tests() -> None:
    """Remove the Pipecat loguru bridge so tests can assert propagation."""
    global _loguru_bridge_handler_id, _loguru_bridge_installed
    if _loguru_bridge_handler_id is not None:
        try:
            from loguru import logger as loguru_logger  # noqa: TID251

            loguru_logger.remove(_loguru_bridge_handler_id)
        except Exception:
            logger.debug("Could not remove Pipecat loguru bridge during test reset")
    _loguru_bridge_handler_id = None
    _loguru_bridge_installed = False


# ---------------------------------------------------------------------------
# Call status and record
# ---------------------------------------------------------------------------


class CallStatus(Enum):
    """Lifecycle states for a phone call."""

    QUEUED = "queued"
    PENDING = "pending"
    DIALING = "dialing"
    RINGING = "ringing"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    NO_ANSWER = "no_answer"
    VOICEMAIL = "voicemail"
    CANCELLED = "cancelled"


_TERMINAL_CALL_STATUSES = frozenset(
    {
        CallStatus.COMPLETED,
        CallStatus.FAILED,
        CallStatus.TIMEOUT,
        CallStatus.NO_ANSWER,
        CallStatus.VOICEMAIL,
        CallStatus.CANCELLED,
    }
)


def _is_terminal_call_status(status: CallStatus) -> bool:
    return status in _TERMINAL_CALL_STATUSES


# Telnyx ``hangup_cause`` values (from the authoritative ``call.hangup`` webhook)
# that mean the recipient never had a real conversation with us -- the call was
# not answered, was busy, or was rejected by the carrier/callee. A call whose
# carrier hangup cause is one of these must NOT be reported COMPLETED-success,
# regardless of whether the media stream connected (issue #2796). Telnyx maps
# SIP causes onto this small vocabulary; see
# https://developers.telnyx.com/docs/voice/programmable-voice/receiving-webhooks
_NON_ANSWER_HANGUP_CAUSES: frozenset[str] = frozenset(
    {
        "no_answer",
        "timeout",
        "user_busy",
        "busy",
        "call_rejected",
        "rejected",
        "unallocated_number",
        "invalid_number_format",
        "no_route_destination",
        "normal_unspecified",
    }
)


def _derive_natural_end_status(record: CallRecord) -> CallStatus:
    """Derive the honest terminal status for a call that reached the natural
    end of its pipeline while still marked ACTIVE.

    ACTIVE is set the moment Telnyx connects its media stream, which -- with
    ``stream_establish_before_call_originate`` -- can happen BEFORE the callee
    actually answers. So "the pipeline ran to the end while ACTIVE" is NOT by
    itself proof the call was answered. This reconciles ACTIVE against the
    authoritative carrier signals delivered by the ``/webhooks/telnyx`` handler
    (``call.answered`` / ``call.hangup`` with ``hangup_cause``):

      1. A carrier hangup cause that means not-answered/busy/rejected -> NO_ANSWER.
         This is the strongest signal and is trusted whenever present.
      2. Otherwise, if the carrier explicitly confirmed answer -> COMPLETED.
      3. Otherwise, if the carrier hung up WITHOUT ever confirming answer
         (media connected but ``call.answered`` never arrived) -> NO_ANSWER.
         This catches the pre-answer-media / dropped-before-answer case.
      4. Otherwise -- no carrier signals were received at all (webhooks not
         wired, or none arrived before terminal derivation) -> COMPLETED,
         preserving prior behavior so a deployment without the answer/hangup
         webhook is not regressed. (The remaining race -- a genuinely
         unanswered call whose ``call.hangup`` lands AFTER this derivation --
         is corrected by ``CallManager.handle_call_hangup``, which since #3385
         corrects the BILLING LEDGER as well as the in-memory record, so the
         honest outcome no longer depends on winning that race.)

    Returns COMPLETED or NO_ANSWER; the caller applies it only when the record
    is still ACTIVE at natural pipeline end.
    """
    cause = (record.carrier_hangup_cause or "").strip().lower()
    if cause and cause in _NON_ANSWER_HANGUP_CAUSES:
        return CallStatus.NO_ANSWER
    if record.carrier_answered:
        return CallStatus.COMPLETED
    if cause:
        # Carrier hung up but never sent call.answered -> not truly answered.
        return CallStatus.NO_ANSWER
    return CallStatus.COMPLETED


async def _resolve_natural_end_status(record: CallRecord, api_key: str) -> None:
    """Apply the honest terminal status at the natural end of a call's pipeline.

    Two billing-honesty concerns, both from the same natural-end code path
    (mirrors #2813's ``dispatch_telnyx_end_call_hangup`` pattern):

    1. (#2796) Don't blindly promote ACTIVE -> COMPLETED: derive the honest
       status via ``_derive_natural_end_status``. NO_ANSWER (and any other
       non-COMPLETED derived status) stays in ``_CALL_STATUSES_REQUIRING_HANGUP``,
       so it's safe to apply immediately -- the hangup safety net below and the
       caller's `finally`-block retry still fire for it.

    2. (#2824) A derived COMPLETED is different: COMPLETED sits OUTSIDE
       ``_CALL_STATUSES_REQUIRING_HANGUP``, so committing it before the Telnyx
       leg is actually confirmed torn down would let a persistently-failing
       hangup end the record COMPLETED while Telnyx keeps billing. So a
       derived COMPLETED is held pending and only committed once the hangup is
       confirmed: already dispatched earlier, no live call_control_id to tear
       down, or the hangup attempted here succeeds. On a failed/unconfirmed
       hangup, ``record.status`` is left ACTIVE (still in
       ``_CALL_STATUSES_REQUIRING_HANGUP``) so ``_record_needs_telnyx_hangup``
       keeps evaluating True and the finally-block retry / watchdog /
       shutdown-hangup paths keep trying to tear the leg down instead of the
       record falsely reporting the call ended while Telnyx keeps billing.

    Mutates ``record.status`` and (on a successful hangup dispatched here)
    ``record._telnyx_hangup_dispatched``. No-op on both counts if the record
    was not ACTIVE and the leg's hangup was already confirmed.
    """
    was_active = record.status == CallStatus.ACTIVE
    completed_pending_hangup_confirmation = False
    if was_active:
        derived_status = _derive_natural_end_status(record)
        if derived_status == CallStatus.COMPLETED:
            completed_pending_hangup_confirmation = True
        else:
            record.status = derived_status
            logger.info(
                "Call %s: natural end reconciled ACTIVE->%s (carrier_answered=%s hangup_cause=%s)",
                record.call_id,
                derived_status.value,
                record.carrier_answered,
                record.carrier_hangup_cause or "<none>",
            )

    # PHONE-15: pipeline_future may have resolved via Pipecat-internal
    # cancellation (e.g. idle_timeout, force-cancel) WITHOUT raising
    # TimeoutError or CancelledError. In that path, the Telnyx voice leg is
    # still alive on Telnyx's side and will keep billing until
    # time_limit_secs (default 600s) auto-fires. Dispatch hangup explicitly
    # so we don't pay for the post-pipeline tail.
    #
    # The end_call tool path sets _telnyx_hangup_dispatched=True before
    # reaching here, so this branch is a no-op for that path. The TIMEOUT
    # branch in _run_call also dispatches hangup before falling through, so
    # this is purely a safety net for the natural-end / idle-cancel case
    # that previously leaked billing minutes. It runs regardless of
    # `was_active` -- it is a general safety net for any path reaching
    # natural end with a live, not-yet-hung-up leg.
    hangup_confirmed = bool(record._telnyx_hangup_dispatched) or not record.telnyx_call_control_id
    if not hangup_confirmed:
        try:
            if await _async_post_telnyx_hangup(api_key, str(record.telnyx_call_control_id)):
                record._telnyx_hangup_dispatched = True
                hangup_confirmed = True
                logger.info(
                    "Call %s: Telnyx hangup dispatched on natural pipeline end",
                    record.call_id,
                )
        except Exception:  # noqa: BLE001, RUF100 - logged + reconciled; never crashes pipeline
            logger.warning(
                "Call %s: natural-end Telnyx hangup failed",
                record.call_id,
                exc_info=True,
            )

    if completed_pending_hangup_confirmation:
        if hangup_confirmed:
            record.status = CallStatus.COMPLETED
        else:
            logger.warning(
                "Call %s: natural-end hangup not confirmed; leaving record ACTIVE "
                "so retry/reconciliation keeps tearing down the Telnyx leg instead "
                "of falsely reporting the call ended",
                record.call_id,
            )


@dataclass
class CallRecord:
    """Tracks a single phone call from dial to hangup.

    Created by CallManager.make_call(), updated throughout the call,
    and returned to the caller with transcript and summary.

    ``user_id`` is the authenticated owner of the call. All ownership
    checks on API endpoints key on this field. Never populated from
    ``caller_name`` — if no authenticated user is available, the call
    is refused (see ``CallManager.make_call``).
    """

    call_id: str
    phone_number: str
    task: str
    caller_name: str
    extra_context: str = ""
    user_id: str = ""
    status: CallStatus = CallStatus.PENDING
    transcript: list[dict[str, str]] = field(default_factory=list)
    summary: str = ""
    outcome: str = ""
    # When the call RECORD was created, i.e. when the dial was requested. Set
    # unconditionally at construction, unlike ``started_at`` below, which only
    # becomes non-None once the Telnyx media stream connects (_run_call). A call
    # that never connects -- no answer, busy, rejected, a dial failure -- is
    # still persisted to call history, and before this field it landed there
    # with NO placed-at timestamp at all, so the call log rendered "Unknown
    # date" for it (#3554).
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: float = 0.0
    error: str | None = None
    telnyx_call_control_id: str | None = None
    # Authoritative carrier answer/hangup reconciliation (issue #2796).
    # ``record.status = ACTIVE`` is driven by the media-stream ``start`` event,
    # which can fire BEFORE the recipient actually answers (Telnyx
    # ``stream_establish_before_call_originate``). So media-connected is NOT
    # proof of a real answer. These fields carry the ground-truth carrier
    # signals from the ``/webhooks/telnyx`` handler so terminal status can be
    # derived honestly instead of defaulting a media-connected leg to
    # COMPLETED-success:
    #   * carrier_answered  -- set True on the Telnyx ``call.answered`` event
    #   * carrier_hangup_cause -- the ``hangup_cause`` from ``call.hangup``
    #     (e.g. ``no_answer`` / ``user_busy`` / ``call_rejected`` /
    #     ``normal_clearing``); "" until a hangup event lands.
    carrier_answered: bool = False
    carrier_answered_at: datetime | None = None
    carrier_hangup_cause: str = ""
    # WHEN the carrier said the leg ended, not just why (#2589). ``call.hangup``
    # used to yield only ``carrier_hangup_cause`` and the timestamp was thrown
    # away, so billing had no honest end anchor and fell back to the pipeline
    # teardown stamp up to PHONE_PIPELINE_IDLE_TIMEOUT_S (150s) later. Read by
    # ``_billable_window_seconds``; never used for terminal-status decisions,
    # which stay exactly where they were.
    carrier_hangup_at: datetime | None = None
    # The moment the Telnyx media stream ended AND failed to recover, recorded by
    # ``_watch_telnyx_ws_disconnect``. Second honest end anchor for the same
    # window, covering the case where the media socket dies but the ``call.hangup``
    # webhook is late, lost, or (desktop mode) never routed to us at all.
    media_stopped_at: datetime | None = None
    # The first moment any IN-PROCESS path concluded the call was over, e.g.
    # ``cancel_call``. That path already stamped an honest ``ended_at``, but
    # ``_run_call``'s ``finally`` overwrites ``ended_at`` unconditionally with the
    # teardown time, so the honest value was lost before billing could use it.
    # Preserved here instead (#2589).
    local_end_at: datetime | None = None
    voicemail_detected: bool = False
    voicemail_detected_at: datetime | None = None
    voicemail_detection_source: str = ""
    voicemail_prompt_ready: bool = False
    # Flips True the instant Viola's ONE voicemail message has actually been
    # delivered to TTS (a non-empty assistant turn COMPLETED after voicemail
    # detection -- LLMFullResponseEndFrame with text, set by the assistant
    # transcript collector which honors InterruptionFrame, so an interrupted /
    # undelivered generation never flips it). This is the real
    # "message-was-spoken" signal that drives _VoicemailResponseGate's
    # accounting: BEFORE it is True the gate keeps letting the voicemail
    # context re-fire (so an interrupt-then-refire still delivers the message);
    # AFTER it is True the gate suppresses further recording-prompt-triggered
    # turns (no repeat). Replaces the brittle one-frame
    # _voicemail_context_frames_to_allow count that dropped the post-interrupt
    # re-fire and left 57s of dead air (closing call 857c52b2, 2026-06-27).
    voicemail_message_delivered: bool = False
    human_takeover_detected: bool = False
    human_takeover_at: datetime | None = None
    info_manifest: dict[str, list[str]] = field(default_factory=dict)
    recording_enabled: bool = False
    recording_paths: dict[str, str] = field(default_factory=dict)
    disclosure_text_confirmed: bool = False
    disclosure_spoken: bool = False
    transcript_retention_enabled: bool = True
    transcript_persistence_stop_index: int | None = None
    transcript_persistence_stopped_reason: str = ""
    recording_started_after_disclosure: bool = False
    recording_stopped_reason: str = ""
    issuer_channel: Any | None = field(default=None, repr=False)
    issuer_channel_info: dict[str, Any] = field(default_factory=dict)
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    # PHONE-15: tracks whether the in-call agent has delivered at least one
    # full assistant turn. The end_call tool refuses before this flips True
    # (defends against hallucinated early-end emissions). Replaces the prior
    # 15-second temporal floor that false-positively blocked legitimate quick
    # calls (line confirmations, voicemail-leave-and-go, etc.).
    first_assistant_turn_complete: bool = False
    # Set the instant end_call commits (before the spoken-close grace window).
    # The end-call lifecycle guard reads this to avoid starting a new recipient
    # turn while carrier hangup is draining. The semantic owner/recipient
    # boundary belongs in telephony.call_context, not in this transport flag.
    _end_call_committed: bool = field(default=False, repr=False)
    # PHONE-15 path-drop recovery: holds a telephony.call_tools._LatchedEndCall
    # when the model emitted end_call in a text-empty tool-only turn (which the
    # PHONE-15 guard refuses). The next spoken assistant turn completes the hangup
    # from this latch, so a forgotten re-emit no longer strands the call open.
    _pending_end_call: Any = field(default=None, repr=False)
    _llm_service: Any = field(default=None, repr=False)
    _pipeline_task: Any = field(default=None, repr=False)
    _telnyx_hangup_dispatched: bool = field(default=False, repr=False)
    # Owner-takeover conference (conference_in_user): the extra outbound leg that
    # dials the owner into the live call is a SEPARATE Telnyx call. It is dialed
    # with end_conference_on_exit=False and bounded only by time_limit_secs, so if
    # the primary call ends first nothing hangs it up and it keeps billing until
    # the server time limit fires (#2800). The handler records the dialed leg's
    # control id here so every primary-call teardown path hangs it up in lockstep.
    _conference_leg_call_control_id: str | None = field(default=None, repr=False)
    _conference_leg_hangup_dispatched: bool = field(default=False, repr=False)
    # Holds a telephony.cost_tracker.CostTracker so _estimate_cost can
    # read real token counts from Pipecat metrics (PHONE-14).
    _cost_tracker: Any = field(default=None, repr=False)
    _hold_handler: Any = field(default=None, repr=False)
    _voicemail_handler: Any = field(default=None, repr=False)
    _payment_sensitive_segment: Any = field(default=None, repr=False)
    queue_id: str = ""
    queue_position: int = 0
    queued_at: float = 0.0
    duplicate_request_suppressed: bool = False


def _enable_recording_after_disclosure(record: CallRecord) -> None:
    if not record.recording_enabled or record.recording_stopped_reason:
        return
    for tee in (
        getattr(record, "_inbound_tee", None),
        getattr(record, "_outbound_tee", None),
    ):
        if tee is not None and hasattr(tee, "enable_recording"):
            tee.enable_recording()
    record.recording_started_after_disclosure = True


async def queue_model_driven_outbound_opening(
    record: CallRecord,
    task: Any,
    *,
    reason: str = "answer_settle",
) -> bool:
    """Ask the pipeline LLM to open the call from the current phone context.

    DE-BANDAID 2026-06-24: this is back to the native db18e514 baseline shape.
    It queues exactly one ``LLMRunFrame`` so the model writes Viola's opening
    from the live phone context. There is NO one-shot latch and NO interruption
    shield: if the recipient talks over the opening, Pipecat's native
    interruption handling (``allow_interruptions=True`` + Smart-Turn/VAD)
    cancels the bot turn, and when the recipient's turn completes the user
    aggregator emits a fresh ``LLMRunFrame`` — so a cut-off opening re-fires
    naturally with full context instead of being permanently suppressed.

    The only state checked is the natural "did Viola already speak / is this
    voicemail" guard, which prevents double-opening when the model already led.
    """

    if record.voicemail_prompt_ready or record.voicemail_detected or _record_has_assistant_turn(record):
        logger.info(
            "Call %s: skipped model-driven outbound opening because voicemail/assistant speech already started",
            record.call_id,
        )
        return False

    if any(entry.get("role") == "viola" for entry in record.transcript):
        logger.debug(
            "Call %s: model-driven outbound opening skipped because Viola speech already exists",
            record.call_id,
        )
        return False

    from pipecat.frames.frames import LLMRunFrame

    voicemail_handler = getattr(record, "_voicemail_handler", None)
    if voicemail_handler is not None and hasattr(voicemail_handler, "on_outbound_opening_queued"):
        await voicemail_handler.on_outbound_opening_queued(reason)
    await task.queue_frame(LLMRunFrame())
    logger.info("Call %s: queued model-driven outbound opening via %s", record.call_id, reason)
    return True


async def queue_outbound_opening_after_answer_settle(
    record: CallRecord,
    task: Any,
    answer_settle_observer: Any,
    *,
    silent_answer_fallback_seconds: float = _PHONE_ANSWER_SILENT_FALLBACK_SECS,
) -> str:
    """Listen-first, then let the model open — the db18e514 native path.

    Outbound calls are caller-led, but a live answer can begin with a real
    business greeting. We follow the recipient's first turn to its *semantic*
    end (Pipecat Smart-Turn / VAD via ``wait_for_recipient_opening``) so Viola
    does not talk over "thanks for calling Tony's pizza." The lone timer is the
    silent-answer fallback so a silent pickup never stalls the call. No
    transcript classification, no fixed window cutting a real greeting off
    mid-sentence.

    The opening is queued natively, with no double-fire:

    * Recipient spoke first → the pipeline's user aggregator already emits its
      own ``LLMRunFrame`` when that turn completes (native turn-taking), and the
      model writes Viola's opening from full context. We do NOT also queue one.
    * Silent pickup → no recipient turn means no aggregator ``LLMRunFrame``, so
      here (and only here) we queue the single opening ``LLMRunFrame`` ourselves.
    * Recipient's turn never semantically ends (Phase-2 cap, issue #2797) → the
      aggregator's own ``LLMRunFrame`` will never fire for a turn that never
      completes, so we queue the opening ourselves here too, same as the
      silent-pickup case.

    With the one-shot latch and interruption shield gone, a cut-off opening
    re-fires naturally: native interruption handling cancels the bot turn and
    the aggregator emits a fresh ``LLMRunFrame`` when the recipient's interrupting
    turn ends.
    """

    # Opening-window instrumentation (#2587): every stage between the recipient's
    # greeting ending and Viola's opening reaching the model gets a named trace
    # stage, so the pre-first-word gap is decomposed instead of inferred.
    latency_trace = getattr(record, "_phone_latency_trace", None)

    def _record_opening_stage(stage: str, started_at: float, **payload: Any) -> None:
        if latency_trace is None:
            return
        with suppress(AttributeError, OSError, TypeError, ValueError):
            latency_trace.record_opening_stage(
                stage,
                elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
                **payload,
            )

    settle_started = time.perf_counter()
    try:
        heard_opening = await answer_settle_observer.wait_for_recipient_opening(
            silent_answer_fallback_seconds=silent_answer_fallback_seconds,
        )
        _record_opening_stage(
            "answer_settle_wait",
            settle_started,
            heard_recipient_opening=heard_opening,
            recipient_turn_capped=bool(getattr(answer_settle_observer, "recipient_turn_capped", False)),
            opening_text_chars=len(str(getattr(answer_settle_observer, "recipient_opening_text", "") or "")),
        )
        logger.info(
            "Call %s: answer-settle complete (heard_recipient_opening=%s)",
            record.call_id,
            heard_opening,
        )
    except (AttributeError, RuntimeError, TypeError, ValueError):
        logger.debug(
            "Call %s: answer-settle wait failed; proceeding",
            record.call_id,
            exc_info=True,
        )
        heard_opening = False

    if heard_opening:
        # One-shot voicemail (2026-06-27): the recipient's first turn is complete, so
        # the full greeting is in hand. Classify human-vs-voicemail ONCE now, which
        # releases Viola's buffered opening (live human) or drops it and switches to
        # leaving a message (voicemail). Fails safe to human on empty/error. This
        # replaces the persistent parallel classifier that held every turn ~5.6s.
        voicemail_handler = getattr(record, "_voicemail_handler", None)
        if voicemail_handler is not None and hasattr(voicemail_handler, "classify_opening"):
            # Prefer the answer-settle observer's race-free capture of the
            # recipient's opening turn. It sits right after STT and accumulates
            # the greeting's TranscriptionFrame in-order with the turn-end
            # signal, so it is populated the instant wait_for_recipient_opening
            # returns. record.transcript is filled by a downstream observer
            # asynchronously, so reading it here can race ahead of the greeting
            # landing and hand the classifier an empty string (which fails safe
            # to CONVERSATION and never detects a real voicemail). Fall back to
            # the transcript only if the observer captured nothing.
            greeting = str(getattr(answer_settle_observer, "recipient_opening_text", "") or "").strip()
            greeting_source = "answer_settle_observer" if greeting else "transcript_fallback"
            if not greeting:
                greeting = " ".join(
                    str(entry.get("text") or "")
                    for entry in (getattr(record, "transcript", []) or [])
                    if str(entry.get("role") or "") == "them"
                ).strip()
                if not greeting:
                    greeting_source = "empty"
            classify_started = time.perf_counter()
            try:
                await voicemail_handler.classify_opening(greeting)
                _record_opening_stage(
                    "voicemail_classify_opening",
                    classify_started,
                    greeting_chars=len(greeting),
                    greeting_source=greeting_source,
                )
            except (RuntimeError, ValueError, TypeError, AttributeError):
                _record_opening_stage(
                    "voicemail_classify_opening",
                    classify_started,
                    greeting_chars=len(greeting),
                    greeting_source="failed:%s" % greeting_source,
                )
                logger.debug(
                    "Call %s: one-shot voicemail classify_opening failed; opening proceeds",
                    record.call_id,
                    exc_info=True,
                )
        # The recipient's completed turn drives the model's opening through the
        # native user-aggregator LLMRunFrame; queueing one here would double-fire.
        return "recipient_turn_opened"

    # Phase-2 cap (issue #2797): the recipient started speaking but their turn
    # never semantically ended (continuous hold music/noise, or a Telnyx
    # teardown that drops the stream without emitting UserStoppedSpeaking), so
    # wait_for_recipient_opening's absolute ceiling fired instead of a real
    # completed turn. The aggregator will never emit its own LLMRunFrame for a
    # turn that never finishes, so — unlike the silent-answer fallback below —
    # we queue Viola's opening ourselves here rather than deferring to a turn
    # completion that is never coming.
    if getattr(answer_settle_observer, "recipient_turn_capped", False):
        logger.warning(
            "Call %s: answer-settle Phase 2 capped without a completed recipient "
            "turn; queuing the opening directly instead of waiting on a turn "
            "that will never finish",
            record.call_id,
        )
        queued = await queue_model_driven_outbound_opening(record, task, reason="phase2_cap")
        return "phase2_cap_queued" if queued else "skipped"

    # Final yield-to-real-greeting guard. The fallback only fires on a genuinely
    # silent pickup, but a narrow window remains between wait_for_recipient_opening
    # returning False and this queue: if the recipient has started speaking by now,
    # their turn will drive the aggregator's own opening — queueing here too would
    # double-fire (capstone f6875e9b: choppy/restarted opening). Defer to the real
    # turn instead. recipient_started_speaking latches True for the life of the
    # call, so this never suppresses a true silent pickup.
    if getattr(answer_settle_observer, "recipient_started_speaking", False):
        logger.info(
            "Call %s: silent-answer fallback yielding — recipient started speaking before queue",
            record.call_id,
        )
        return "recipient_turn_opened"

    queued = await queue_model_driven_outbound_opening(record, task, reason="silent_answer")
    return "silent_answer_queued" if queued else "skipped"


def _phone_function_result_properties(*, run_llm: bool = True) -> Any:
    from pipecat.frames.frames import FunctionCallResultProperties

    return FunctionCallResultProperties(run_llm=run_llm)


async def _deliver_phone_function_result(params: Any, payload: Any, *, run_llm: bool = True) -> None:
    try:
        await params.result_callback(
            payload,
            properties=_phone_function_result_properties(run_llm=run_llm),
        )
    except TypeError as exc:
        if "properties" not in str(exc):
            raise
        await params.result_callback(payload)


def _stop_persistent_call_capture(record: CallRecord, reason: str) -> None:
    if record.recording_enabled and not record.recording_stopped_reason:
        record.recording_stopped_reason = reason
        for tee in (
            getattr(record, "_inbound_tee", None),
            getattr(record, "_outbound_tee", None),
        ):
            if tee is not None and hasattr(tee, "disable_recording"):
                tee.disable_recording()
    if record.transcript_retention_enabled and record.transcript_persistence_stop_index is None:
        record.transcript_persistence_stop_index = len(record.transcript)
        record.transcript_persistence_stopped_reason = reason


def _persisted_transcript_entries(record: CallRecord) -> list[dict[str, str]]:
    if not record.transcript_retention_enabled or not record.disclosure_spoken:
        return []
    entries = list(record.transcript)
    stop_index = record.transcript_persistence_stop_index
    if stop_index is not None:
        return entries[: max(0, stop_index)]
    return entries


def _should_persist_call_transcript(record: CallRecord) -> bool:
    return record.transcript_retention_enabled and record.disclosure_spoken


def _make_disclosure_watchdog(record: CallRecord) -> Any | None:
    from telephony.disclosure_watchdog import DisclosureWatchdog

    def _mark_disclosure_text_confirmed() -> None:
        record.disclosure_text_confirmed = True

    return DisclosureWatchdog(
        caller_name=record.caller_name,
        recording_enabled=record.recording_enabled,
        transcript_retention_enabled=record.transcript_retention_enabled,
        on_disclosure_spoken=_mark_disclosure_text_confirmed,
    )


def _make_ai_identity_watchdog(
    record: CallRecord,
    *,
    enabled: bool,
    proactive_disclosure: bool = False,
) -> Any | None:
    from telephony.ai_identity_watchdog import AIIdentityWatchdog

    return (
        AIIdentityWatchdog(caller_name=record.caller_name, proactive_disclosure=proactive_disclosure)
        if enabled
        else None
    )


def _make_disclosure_playback_marker(record: CallRecord, disclosure_watchdog: Any | None) -> Any | None:
    if disclosure_watchdog is None or getattr(disclosure_watchdog, "_expected_disclosure", None) is None:
        return None

    from telephony.disclosure_playback_marker import DisclosurePlaybackMarker

    def _mark_disclosure_spoken() -> None:
        record.disclosure_spoken = True
        _enable_recording_after_disclosure(record)

    return DisclosurePlaybackMarker(
        is_disclosure_confirmed=lambda: bool(getattr(disclosure_watchdog, "disclosure_confirmed", False)),
        on_disclosure_spoken=_mark_disclosure_spoken,
    )


@dataclass(frozen=True)
class _PreparedOutboundCall:
    phone_number: str
    task: str
    caller_name: str
    extra_context: str
    max_duration: int | None
    user_id: str
    user_tier: str
    issuer_channel: Any | None
    issuer_channel_info: dict[str, Any]
    info_manifest: dict[str, Any]


def _resolve_caller_name_from_pre_call_plan(candidate: str, precall_plan: PreCallPlan) -> str:
    resolved_name = validate_caller_name((precall_plan.resolved or {}).get("owner_caller_name", ""))
    if resolved_name and not is_assistant_caller_name(resolved_name):
        return resolved_name
    if is_assistant_caller_name(candidate):
        raise ValueError("Please set your name in Settings before making calls.")
    return candidate


def _record_has_assistant_turn(record: CallRecord) -> bool:
    for entry in getattr(record, "transcript", []) or []:
        role = str(entry.get("role") or "").strip().lower()
        text = str(entry.get("text") or "").strip()
        if text and role in {"assistant", "viola"}:
            return True
    return False


# ---------------------------------------------------------------------------
# Transcript collector (Pipecat frame processor)
# ---------------------------------------------------------------------------


class TranscriptCollector:
    """Collects conversation transcript entries for a CallRecord."""

    def __init__(self, record: CallRecord | None = None) -> None:
        self._record = record
        self.entries: list[dict[str, str]] = record.transcript if record is not None else []

    @staticmethod
    def _timestamp(ts: str | None = None) -> str:
        if ts:
            return str(ts)
        return datetime.now(tz=UTC).isoformat()

    def add_user(self, text: str, ts: str | None = None) -> None:
        """Record what the other party said (STT output)."""
        from intent.log_redaction import redact_card_data

        cleaned = str(redact_card_data(text)).strip()
        if cleaned:
            self.entries.append({"role": "them", "text": cleaned, "ts": self._timestamp(ts)})
            if self._record is not None:
                if detect_persistence_objection(cleaned):
                    _stop_persistent_call_capture(
                        self._record,
                        "called party objected to recording or transcription",
                    )
                if detect_called_party_opt_out(cleaned):
                    add_opt_out(
                        self._record.phone_number,
                        "called party requested no further AI calls",
                    )

    def add_assistant(self, text: str, ts: str | None = None) -> None:
        """Record what Viola said (TTS input)."""
        from intent.log_redaction import redact_card_data

        cleaned = str(redact_card_data(text)).strip()
        if cleaned:
            self.entries.append({"role": "viola", "text": cleaned, "ts": self._timestamp(ts)})

    def to_text(self) -> str:
        """Format transcript as readable text."""
        lines = []
        for entry in self.entries:
            speaker = "Them" if entry["role"] == "them" else "Viola"
            lines.append("%s: %s" % (speaker, entry["text"]))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Call Manager
# ---------------------------------------------------------------------------


class CallManager:
    """Manages outbound phone calls using Pipecat + Telnyx.

    Usage::

        config = TelnyxConfig(api_key="...", phone_number="+1...", sip_connection_id="...")
        manager = CallManager(config)

        record = await manager.make_call(
            phone_number="+15551234567",
            task="Book a haircut appointment for tomorrow afternoon",
            caller_name="Alex",
        )

        # Poll or await completion
        while record.status in (CallStatus.PENDING, CallStatus.DIALING, CallStatus.ACTIVE):
            await asyncio.sleep(1)

        print(record.summary)
        print(record.transcript)
    """

    def __init__(self, config: TelnyxConfig, *, queue_store: PhoneCallQueue | None = None) -> None:
        if not config.is_configured:
            raise ValueError("TelnyxConfig is incomplete. Need api_key, phone_number, sip_connection_id.")
        self.config = config
        self._active_calls: dict[str, CallRecord] = {}
        self._call_records: dict[str, CallRecord] = {}
        self._call_tasks: dict[str, asyncio.Task[None]] = {}
        self._billing_heartbeat_tasks: dict[str, asyncio.Task[None]] = {}
        self._call_queue = queue_store or PhoneCallQueue()
        self._queued_issuer_channels: dict[str, Any] = {}
        self._queue_pump_lock = asyncio.Lock()
        self._queue_pump_task: asyncio.Task[None] | None = None
        self._phone_tool_runtimes: dict[str, _PhoneToolRuntime] = {}
        self._phone_tool_runtime_lock = asyncio.Lock()
        self._number_pool_heartbeat_tasks: dict[str, asyncio.Task[None]] = {}
        self._stt_hotwords = ""
        self._stt_initial_prompt = ""
        _register_call_manager_shutdown_hooks(self)
        self._number_pool = NumberPool(
            numbers=list(config.phone_numbers),
            default=config.phone_number,
        )
        # PHONE-09: credentials for S3 recording storage are loaded from
        # environment only and never stored on TelnyxConfig. The factory
        # raises if required values are missing in S3 mode.
        from telephony.config import load_recording_secrets

        recording_secrets = load_recording_secrets() if config.recording_storage == "s3" else {}
        self._recording_storage: RecordingStorage = create_recording_storage(
            storage_type=config.recording_storage,
            s3_bucket=config.recording_s3_bucket,
            s3_endpoint=config.recording_s3_endpoint,
            s3_prefix=config.recording_s3_prefix,
            s3_access_key=recording_secrets.get("access_key", ""),
            s3_secret_key=recording_secrets.get("secret_key", ""),
            s3_key_hash_secret=recording_secrets.get("key_hash_secret", ""),
        )
        self._schedule_queue_pump()

    @property
    def active_call_count(self) -> int:
        """Number of currently active calls."""
        return len(self._active_calls)

    def _active_outbound_call_count(self) -> int:
        """Number of live outbound calls occupying the phone surface."""
        return sum(1 for record in self._active_calls.values() if not _is_terminal_call_status(record.status))

    def _max_active_outbound_calls(self) -> int:
        """Effective global cap on concurrently-live outbound calls.

        Honors the already-configured ``TelnyxConfig.max_concurrent_calls``
        (settings ``phone_global_max_concurrent``, default 25) so the single
        knob that already gates ``_make_call_unqueued`` also drives the queue
        pump — instead of a second hard-coded constant silently overriding it.
        Floored at ``_MIN_ACTIVE_OUTBOUND_CALLS`` so a misconfigured 0/negative
        cap never wedges the pump into admitting nothing.
        """
        try:
            configured = int(self.config.max_concurrent_calls)
        except (TypeError, ValueError):
            configured = _MIN_ACTIVE_OUTBOUND_CALLS
        return max(_MIN_ACTIVE_OUTBOUND_CALLS, configured)

    def _user_outbound_call_count(self, user_id: str) -> int:
        """Live (non-terminal) outbound calls owned by one user."""
        safe_user_id = str(user_id or "").strip()
        if not safe_user_id:
            return 0
        return sum(
            1
            for record in self._active_calls.values()
            if not _is_terminal_call_status(record.status)
            and str(getattr(record, "user_id", "") or "").strip() == safe_user_id
        )

    def _user_has_outbound_capacity(self, user_id: str, user_tier: str) -> bool:
        """Pre-check the per-plan concurrent limit before starting a queued call.

        The billing gate enforces a per-user concurrent-call limit (FREE/PRO 1,
        MAX 2 — ``PhoneBillingGate._RATE_LIMITS_BY_FAMILY``). Under the old
        global cap of 1 the pump only ever fired after the single live call
        ended, so a user's queued call always passed that gate. With the global
        cap raised, the pump fires while the user's own call is still live; if
        it dialed anyway, the billing gate would raise and the pump's error
        path would DROP the queued item. This pre-check lets the pump defer
        (keep queued) instead — the item starts when the owner's live call
        ends and re-pumps the queue.

        Reads the limit from the live billing gate (``get_phone_billing()``,
        the same object ``_make_call_unqueued`` consults) via its public
        ``concurrent_limit_for_tier``, so plan changes and the test-mode
        bypass apply identically to both checks. On any resolution failure —
        including a gate object without the method (unit-test mocks) — it
        returns True so the authoritative billing gate stays the decider. A
        limit of 0/negative also returns True: such users can never gain
        capacity, so deferring would park their items in the queue forever —
        attempting the call lets the gate reject it with its own reason (old
        behavior).
        """
        try:
            limit = int(get_phone_billing().concurrent_limit_for_tier(user_tier))
        except Exception:  # noqa: BLE001, RUF100 - any pre-check failure falls through to the authoritative gate
            logger.debug("Per-user concurrent-limit pre-check unavailable; deferring to billing gate")
            return True
        if limit <= 0:
            return True
        return self._user_outbound_call_count(user_id) < limit

    def _schedule_queue_pump(self) -> None:
        """Start the next queued call soon, if an event loop is available."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._queue_pump_task is not None and not self._queue_pump_task.done():
            return
        self._queue_pump_task = loop.create_task(
            self._start_next_queued_call_if_idle(),
            name="phone-call-queue-pump",
        )

    async def list_call_queue(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """Return queued outbound calls, optionally scoped to one owner."""
        items = await self._call_queue.list(user_id=user_id or None)
        return [item.to_payload() for item in items]

    async def remove_queued_call(self, position: int, *, user_id: str | None = None) -> dict[str, Any] | None:
        """Remove a queued outbound call by visible queue position."""
        removed = await self._call_queue.remove_position(position, user_id=user_id or None)
        if removed is None:
            return None
        self._queued_issuer_channels.pop(removed.queue_id, None)
        await self._broadcast_call_queue_update(removed.user_id)
        return removed.to_payload()

    async def _broadcast_call_queue_update(self, user_id: str) -> None:
        user_id = str(user_id or "").strip()
        if not user_id:
            return
        try:
            from ui.websocket.event_hub import get_event_hub

            hub = get_event_hub()
            if hub is None:
                return
            await hub.broadcast(
                "call_queue_updated",
                {"queue": await self.list_call_queue(user_id=user_id)},
                user_id=user_id,
                force=True,
            )
        except Exception as exc:
            logger.debug("Phone queue WS update failed for user=%s: %s", user_id, exc)

    @staticmethod
    def _record_from_queued_item(item: QueuedOutboundCall) -> CallRecord:
        return CallRecord(
            call_id=item.queue_id,
            phone_number=item.phone_number,
            task=item.task,
            caller_name=item.caller_name,
            extra_context=item.extra_context,
            user_id=item.user_id,
            status=CallStatus.QUEUED,
            info_manifest=dict(item.info_manifest or {}),
            issuer_channel_info=dict(item.issuer_channel_info or {}),
            queue_id=item.queue_id,
            queue_position=item.position,
            queued_at=item.queued_at,
        )

    async def _enqueue_prepared_call(self, prepared: _PreparedOutboundCall) -> CallRecord:
        item = QueuedOutboundCall.create(
            user_id=prepared.user_id,
            phone_number=prepared.phone_number,
            task=prepared.task,
            caller_name=prepared.caller_name,
            extra_context=prepared.extra_context,
            max_duration=prepared.max_duration,
            user_tier=prepared.user_tier,
            issuer_channel_info=prepared.issuer_channel_info,
            info_manifest=prepared.info_manifest,
        )
        queued = await self._call_queue.enqueue(item)
        if prepared.issuer_channel is not None:
            self._queued_issuer_channels[queued.queue_id] = prepared.issuer_channel
        await self._broadcast_call_queue_update(queued.user_id)
        logger.info(
            "Queued outbound phone call %s at position %d for user=%s",
            queued.queue_id,
            queued.position,
            queued.user_id,
        )
        return self._record_from_queued_item(queued)

    def _start_billing_heartbeat(self, record: CallRecord) -> None:
        self._cancel_billing_heartbeat(record.call_id)
        # mt-ok: record.user_id carries user scope into the bg heartbeat task
        self._billing_heartbeat_tasks[record.call_id] = asyncio.create_task(
            self._run_billing_heartbeat(record),
            name="phone-billing-heartbeat-%s" % record.call_id,
        )

    def _cancel_billing_heartbeat(self, call_id: str) -> asyncio.Task[None] | None:
        task = self._billing_heartbeat_tasks.pop(call_id, None)
        if task is not None and not task.done():
            task.cancel()
        return task

    async def _cancel_billing_heartbeat_and_wait(self, call_id: str) -> None:
        task = self._cancel_billing_heartbeat(call_id)
        if task is None:
            return
        if task is asyncio.current_task():
            return
        if not task.done():
            with suppress(asyncio.CancelledError):
                await task

    def _start_number_pool_heartbeat(self, call_id: str, number: str) -> None:
        self._cancel_number_pool_heartbeat(call_id)
        interval = min(
            _PHONE_NUMBER_POOL_HEARTBEAT_MAX_INTERVAL_SECS,
            max(1.0, float(getattr(self._number_pool, "stale_ttl_seconds", 2.0)) / 2.0),
        )
        self._number_pool_heartbeat_tasks[call_id] = asyncio.create_task(
            self._run_number_pool_heartbeat(call_id, number, interval),
            name="phone-number-pool-heartbeat-%s" % call_id,
        )

    def _cancel_number_pool_heartbeat(self, call_id: str) -> asyncio.Task[None] | None:
        task = self._number_pool_heartbeat_tasks.pop(call_id, None)
        if task is not None and not task.done():
            task.cancel()
        return task

    async def _cancel_number_pool_heartbeat_and_wait(self, call_id: str) -> None:
        task = self._cancel_number_pool_heartbeat(call_id)
        if task is None:
            return
        if task is asyncio.current_task():
            return
        if not task.done():
            with suppress(asyncio.CancelledError):
                await task

    async def _run_number_pool_heartbeat(self, call_id: str, number: str, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    self._number_pool.heartbeat(number)
                except (RuntimeError, OSError, sqlite3.Error) as exc:
                    logger.warning(
                        "Phone number-pool heartbeat failed for call %s: %s",
                        call_id,
                        exc,
                    )
        except asyncio.CancelledError:
            raise

    async def _run_billing_heartbeat(self, record: CallRecord) -> None:
        try:
            while True:
                await asyncio.sleep(_PHONE_BILLING_HEARTBEAT_INTERVAL_SECS)
                try:
                    await get_phone_billing().heartbeat(record.user_id, record.call_id)
                except Exception as exc:
                    logger.warning(
                        "Phone billing heartbeat failed for call %s: %s",
                        record.call_id,
                        exc,
                    )
                if record.status != CallStatus.ACTIVE:
                    continue
                try:
                    self._refresh_live_cost_estimate(record)
                    await self._broadcast_call_cost_update(record)
                except Exception as exc:
                    logger.warning("Phone cost update failed for call %s: %s", record.call_id, exc)
        except asyncio.CancelledError:
            raise

    def _refresh_live_cost_estimate(self, record: CallRecord) -> None:
        # Same meter as the final settle (#2589). The live figure the user
        # watches tick up and the figure they are finally charged must come from one
        # function, or they disagree — which is the whole bug this ticket is
        # about: a log line said 34.2s while the ledger row said 200.6s.
        if record.started_at is not None:
            record.duration_seconds = _billable_window_seconds(record)
        self._estimate_cost(record)

    async def _broadcast_call_ws_event(
        self,
        event_type: str,
        record: CallRecord,
        *,
        include_summary: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        user_id = str(record.user_id or "").strip()
        if not user_id:
            return
        try:
            from ui.websocket.event_hub import get_event_hub

            hub = get_event_hub()
            if hub is None:
                return
            payload = _call_event_payload(record, include_summary=include_summary)
            if extra:
                payload.update(extra)
            await hub.broadcast(event_type, payload, user_id=user_id, force=True)
        except Exception as exc:
            logger.debug(
                "Phone WS event %s failed for call %s: %s",
                event_type,
                record.call_id,
                exc,
            )

    async def _broadcast_call_cost_update(self, record: CallRecord) -> None:
        updated_at = datetime.now(tz=UTC).isoformat()
        recipient_state = _recipient_state(record)
        payload = {
            "updated_at": updated_at,
            "cost_usd": float(record.estimated_cost_usd or 0.0),
            "current_cost_usd": float(record.estimated_cost_usd or 0.0),
            "estimated_cost_usd": float(record.estimated_cost_usd or 0.0),
            "duration_seconds": float(record.duration_seconds or 0.0),
            "recipient_state": recipient_state,
        }
        try:
            from core.events.bus import get_event_bus
            from core.events.types import CallCostUpdate

            bus = get_event_bus()
            if bus:
                bus.publish(
                    CallCostUpdate(
                        user_id=record.user_id,
                        call_id=record.call_id,
                        cost_usd=float(record.estimated_cost_usd or 0.0),
                        duration_seconds=float(record.duration_seconds or 0.0),
                        recipient_state=recipient_state,
                        updated_at=updated_at,
                        source="telephony.call_manager",
                    )
                )
        except Exception as exc:
            logger.debug("Phone cost typed event failed for call %s: %s", record.call_id, exc)
        await self._broadcast_call_ws_event("call_cost_update", record, extra=payload)

    @staticmethod
    def _phone_runtime_uses_visible_browser(hub: Any) -> bool:
        configs = getattr(hub, "_configs", None)
        browser_config = None
        if isinstance(configs, dict):
            browser_config = configs.get("browser")
        browser_module = str(getattr(browser_config, "module", "") or "")
        return browser_module == _VISIBLE_DESKTOP_BROWSER_MODULE

    @staticmethod
    def _active_agent_tool_runtime(
        user_id: str | None = None,
    ) -> _PhoneToolRuntime | None:
        """Reuse a desktop agent's MCP hub when the same user already has one.

        Multi-tenant: the owner of the executor MUST match ``user_id`` —
        otherwise the phone tool would borrow another tenant's hub,
        approval surface, and tool list.
        """
        if not user_id:
            return None
        try:
            from intent.agent_executor import get_active_executor

            active = get_active_executor(user_id=user_id)
        except Exception:
            return None
        if active is None:
            return None
        if getattr(active, "_user_id", None) != user_id:
            return None

        hub = getattr(active, "_mcp_hub", None)
        approval_manager = getattr(active, "_approval", None)
        if hub is None or approval_manager is None:
            return None
        if CallManager._phone_runtime_uses_visible_browser(hub):
            logger.debug("Phone MCP runtime will not borrow visible desktop browser hub")
            return None
        try:
            visible_tools = hub.list_tools(tier="symphony", interactive=True)
        except TypeError:
            visible_tools = hub.list_tools()
        except Exception:
            return None
        if len(visible_tools) <= 70:
            return None
        return _PhoneToolRuntime(
            user_id=user_id,
            hub=hub,
            approval_manager=approval_manager,
            visible_tools=list(visible_tools),
        )

    @staticmethod
    async def _create_phone_tool_runtime(user_id: str) -> _PhoneToolRuntime | None:
        from mcp_hub.runtime_config import build_runtime_mcp_server_configs

        from intent.approval import ApprovalManager
        from mcp_hub import ApprovalBridge, MCPClientHub

        approval_manager = ApprovalManager()
        hub = MCPClientHub(approval_bridge=ApprovalBridge(approval_manager))
        runtime_configs = build_runtime_mcp_server_configs(browser_surface=_PHONE_BACKGROUND_BROWSER_SURFACE)

        await hub.initialize(runtime_configs.fast_configs)

        if any(config.name == "google-workspace" for config in runtime_configs.fast_configs) and user_id:
            try:
                from services.oauth.workspace_bridge import export_tokens_for_workspace

                asyncio.ensure_future(export_tokens_for_workspace(user_id))
            except Exception:
                logger.debug("Phone Workspace token seeding deferred to login")

        if runtime_configs.browser_config is not None:
            try:
                await hub.connect_server(runtime_configs.browser_config.name, runtime_configs.browser_config)
            except Exception:
                logger.exception("Phone MCP browser tool connection failed")

        try:
            from intent.tools.delegation import set_mcp_hub as _set_delegation_hub
            from intent.tools.self_management import set_mcp_hub as _set_self_mgmt_hub
            from intent.tools.tool_search import set_mcp_hub as _set_tool_search_hub

            _set_delegation_hub(hub)
            _set_self_mgmt_hub(hub)
            _set_tool_search_hub(hub)
        except Exception:
            logger.debug("Phone MCP helper wiring skipped")

        try:
            hub.refresh_dynamic_tool_definitions()
        except Exception:
            logger.debug("Phone dynamic API tool registration skipped")

        visible_tools = hub.list_tools(tier="symphony", interactive=True)
        return _PhoneToolRuntime(
            user_id=user_id,
            hub=hub,
            approval_manager=approval_manager,
            visible_tools=list(visible_tools),
        )

    async def _ensure_phone_tool_runtime(self, user_id: str) -> _PhoneToolRuntime | None:
        user_id = str(user_id or "").strip()
        if not user_id:
            return None
        active_runtime = self._active_agent_tool_runtime(user_id=user_id)
        if active_runtime is not None:
            self._phone_tool_runtimes[user_id] = active_runtime
            return active_runtime

        cached_runtime = self._phone_tool_runtimes.get(user_id)
        if cached_runtime is not None:
            try:
                cached_runtime.visible_tools = list(cached_runtime.hub.list_tools(tier="symphony", interactive=True))
            except TypeError:
                cached_runtime.visible_tools = list(cached_runtime.hub.list_tools())
            except Exception:
                logger.debug("Cached phone MCP tool listing failed; rebuilding runtime")
                self._phone_tool_runtimes.pop(user_id, None)
            else:
                return cached_runtime

        async with self._phone_tool_runtime_lock:
            cached_runtime = self._phone_tool_runtimes.get(user_id)
            if cached_runtime is not None:
                return cached_runtime
            try:
                runtime = await self._create_phone_tool_runtime(user_id)
                if runtime is not None:
                    self._phone_tool_runtimes[user_id] = runtime
                    logger.info(
                        "Phone MCP tool runtime initialized for user=%s: %d desktop tools",
                        user_id,
                        len(runtime.visible_tools),
                    )
                return runtime
            except Exception:
                logger.exception("Phone MCP tool runtime initialization failed")
                return None

    @staticmethod
    def _make_phone_mcp_tool_handler(
        *,
        executor: Any,
        mcp_name_by_llm_name: dict[str, str],
        call_record: CallRecord,
        deferred_tool_pool: Any | None = None,
        provider_desktop_tools: list[dict[str, Any]] | None = None,
        full_mcp_tools: list[dict[str, Any]] | None = None,
        llm_context: Any | None = None,
        llm: Any | None = None,
        phone_mode: str = "local",
    ):
        async def phone_mcp_tool_handler(params) -> None:
            llm_tool_name = str(getattr(params, "function_name", "") or "")
            mcp_tool_name = mcp_name_by_llm_name.get(llm_tool_name, llm_tool_name)
            arguments = dict(getattr(params, "arguments", {}) or {})
            logger.info(
                "Phone tool call: call=%s llm_tool=%s mcp_tool=%s",
                call_record.call_id,
                llm_tool_name,
                mcp_tool_name,
            )
            try:
                executor._original_command = call_record.task
                issuer_channel = getattr(call_record, "issuer_channel", None)
                pool_token = None
                if deferred_tool_pool is not None:
                    try:
                        from intent.tools.tool_search import (
                            reset_deferred_tool_pool,
                            set_deferred_tool_pool,
                        )

                        pool_token = set_deferred_tool_pool(deferred_tool_pool)
                    except Exception:  # noqa: BLE001, RUF100 - optional publish; never break the call
                        logger.debug(
                            "Phone ToolSearch deferred pool publish failed",
                            exc_info=True,
                        )
                        reset_deferred_tool_pool = None
                else:
                    reset_deferred_tool_pool = None
                try:
                    if issuer_channel is not None:
                        from messaging.channel import use_request_channel

                        with use_request_channel(issuer_channel):
                            tool_result = await executor._execute_tool(mcp_tool_name, arguments)
                    else:
                        tool_result = await executor._execute_tool(mcp_tool_name, arguments)
                finally:
                    if pool_token is not None and reset_deferred_tool_pool is not None:
                        reset_deferred_tool_pool(pool_token)
                await CallManager._maybe_start_phone_payment_confirmation(
                    executor=executor,
                    tool_result=tool_result,
                )
                if provider_desktop_tools is not None and full_mcp_tools is not None:
                    _expand_phone_deferred_tool_references(
                        llm_tool_name=llm_tool_name,
                        tool_result=tool_result,
                        executor=executor,
                        mcp_name_by_llm_name=mcp_name_by_llm_name,
                        provider_desktop_tools=provider_desktop_tools,
                        full_mcp_tools=full_mcp_tools,
                        llm_context=llm_context,
                        llm=llm,
                        phone_mode=phone_mode,
                        phone_mcp_tool_handler=phone_mcp_tool_handler,
                        call_record=call_record,
                    )
                await _deliver_phone_function_result(
                    params,
                    _tool_result_callback_payload(tool_result),
                    run_llm=True,
                )
            except Exception as exc:
                logger.exception("Phone MCP tool execution failed for %s", mcp_tool_name)
                await _deliver_phone_function_result(
                    params,
                    {
                        "success": False,
                        "error": "Tool execution error: %s" % exc,
                    },
                    run_llm=True,
                )

        return phone_mcp_tool_handler

    @staticmethod
    async def _maybe_start_phone_payment_confirmation(*, executor: Any, tool_result: Any) -> None:
        """Bridge phone MCP tool PAYMENT_GATE results into the hosted confirmation flow."""
        data = getattr(tool_result, "data", None)
        if not isinstance(data, dict) or not data.get("payment_gate"):
            return
        if getattr(executor, "_payment_gate_active", False):
            return

        try:
            from intent.agent_executor import (
                _dispatch_payment_confirmation_link_for_executor,
                _payment_gate_answer_with_confirm_url,
            )

            answer = str(data.get("answer") or getattr(executor, "_final_answer", None) or "").strip()
            if answer:
                executor._final_answer = answer
            payment_confirmation_ctx = await executor._prepare_payment_confirmation_session()
            confirmation_url = str(payment_confirmation_ctx.get("url") or "")
            final_answer = _payment_gate_answer_with_confirm_url(answer, confirmation_url)
            if final_answer:
                executor._final_answer = final_answer
                data["answer"] = final_answer
            data["confirmation_url"] = confirmation_url
            delivery = await _dispatch_payment_confirmation_link_for_executor(
                executor,
                payment_confirmation_ctx,
                confirmation_url,
            )
            if delivery:
                data["confirmation_link_delivery"] = delivery
            executor._payment_confirmation_ctx = payment_confirmation_ctx
            executor._payment_gate_active = True
            # mt-ok: payment_confirmation_ctx carries user_id; executor is per-call
            asyncio.ensure_future(executor._run_payment_confirmation_wait(payment_confirmation_ctx))
        except Exception:
            logger.exception("Phone PAYMENT_GATE confirmation setup failed")

    @staticmethod
    async def _maybe_start_phone_payment_confirmation_from_text(*, executor: Any, assistant_text: str) -> None:
        """Path A: trigger confirmation when assistant final-answer text starts with PAYMENT_GATE: prefix.

        PAY-GATE-11 promises channel-symmetric PAYMENT_GATE: model emits the gate, runtime
        catches it. Desktop loops detect via final_answer.startswith(_PAYMENT_GATE_PREFIX);
        phone needs the same. This hook fires on every completed assistant turn and routes
        the same way as the tool-based bridge when a text-prefix gate is observed.
        """
        if not assistant_text:
            return
        if getattr(executor, "_payment_gate_active", False):
            return
        try:
            from intent.agent_executor import (
                _PAYMENT_GATE_PREFIX,
                _dispatch_payment_confirmation_link_for_executor,
                _payment_gate_answer_with_confirm_url,
            )
        except Exception:
            return
        if not assistant_text.strip().startswith(_PAYMENT_GATE_PREFIX):
            return

        try:
            executor._final_answer = assistant_text.strip()
            payment_confirmation_ctx = await executor._prepare_payment_confirmation_session()
            confirmation_url = str(payment_confirmation_ctx.get("url") or "")
            final_answer = _payment_gate_answer_with_confirm_url(assistant_text.strip(), confirmation_url)
            if final_answer:
                executor._final_answer = final_answer
            await _dispatch_payment_confirmation_link_for_executor(
                executor,
                payment_confirmation_ctx,
                confirmation_url,
            )
            executor._payment_confirmation_ctx = payment_confirmation_ctx
            executor._payment_gate_active = True
            # mt-ok: payment_confirmation_ctx carries user_id; executor is per-call
            asyncio.ensure_future(executor._run_payment_confirmation_wait(payment_confirmation_ctx))
            logger.info(
                "Phone PAYMENT_GATE confirmation started from text-prefix detector: url=%s",
                confirmation_url[:120],
            )
        except Exception:
            logger.exception("Phone PAYMENT_GATE text-prefix confirmation setup failed")

    def _resolve_effective_user_id(self, user_id: str | None) -> str:
        """Resolve a stable authenticated user id for ownership and billing.

        Cloud mode requires a non-empty user_id provided by the auth
        dependency. Local/desktop callers may rely on an already-installed
        user_context, but this method never manufactures ownership from a
        device or display-name fallback.

        Raises:
            ValueError: If no authenticated user identity is available. We
                never derive ownership from caller_name (user-controlled free
                text).
        """
        if user_id:
            return user_id

        try:
            from core.user_context import get_current_user_id
        except ImportError as exc:
            raise ValueError("Authentication required to make phone calls.") from exc

        try:
            resolved = get_current_user_id()
        except LookupError as exc:
            raise ValueError("Authentication required to make phone calls.") from exc

        if not resolved:
            raise ValueError("Authentication required to make phone calls.")
        return resolved

    async def _ensure_phone_call_consents(self, user_id: str | None) -> None:
        if self.config.mode == "cloud":
            await _ensure_cloud_voice_consent_for_phone_call(user_id)
            return
        _ensure_cloud_llm_consent_for_phone_call()

    async def _prepare_outbound_call(
        self,
        phone_number: str,
        task: str,
        caller_name: str,
        extra_context: str = "",
        max_duration: int | None = None,
        user_id: str | None = None,
        user_tier: str = "free",
        issuer_channel: Any | None = None,
        issuer_channel_info: dict[str, Any] | None = None,
    ) -> _PreparedOutboundCall:
        """Validate a confirmed outbound-call request before dialing or queueing."""
        # Resolve the authenticated identity FIRST (#1310): an unauthenticated
        # caller must get "Authentication required" for ANY input, never a
        # differentiated number-format error. Validating the number before
        # checking auth lets an anonymous caller probe which numbers/formats
        # are accepted without ever authenticating — auth gates before any
        # other request processing.
        effective_user_id = self._resolve_effective_user_id(user_id)
        cleaned = normalize_us_e164(phone_number)
        await self._ensure_phone_call_consents(effective_user_id)
        await _require_owner_safety_control(
            "phone_outbound",
            user_id=effective_user_id,
            action="phone_call_prepare",
        )

        tos = get_phone_tos()
        tos_result = await tos.check(effective_user_id)
        if not tos_result.allowed:
            raise ValueError(tos_result.reason)

        validated_name = validate_caller_name(caller_name)
        if validated_name is None:
            raise ValueError("Please set your name in Settings before making calls.")

        if is_opted_out(cleaned):
            raise ValueError("That business has asked not to receive AI calls. You'll need to call them directly.")

        precall_plan = await plan_phone_call_prerequisites(
            user_id=effective_user_id,
            task=task,
            caller_name=validated_name,
        )
        validated_name = _resolve_caller_name_from_pre_call_plan(validated_name, precall_plan)

        return _PreparedOutboundCall(
            phone_number=cleaned,
            task=task,
            caller_name=validated_name,
            extra_context=extra_context,
            max_duration=max_duration,
            user_id=effective_user_id,
            user_tier=user_tier,
            issuer_channel=issuer_channel,
            issuer_channel_info=issuer_channel_info or _issuer_channel_info(issuer_channel),
            info_manifest=precall_plan.info_manifest,
        )

    @staticmethod
    def _normalized_call_text(value: str) -> str:
        return " ".join(str(value or "").split()).lower()

    def _is_same_call_request(self, record: CallRecord, prepared: _PreparedOutboundCall) -> bool:
        normalized_task = self._normalized_call_text(prepared.task)
        normalized_caller = self._normalized_call_text(prepared.caller_name)
        normalized_extra = self._normalized_call_text(prepared.extra_context)
        record_task = self._normalized_call_text(record.task)
        record_caller = self._normalized_call_text(record.caller_name)
        record_extra = self._normalized_call_text(getattr(record, "extra_context", ""))
        return (
            record.user_id == prepared.user_id
            and record.phone_number == prepared.phone_number
            and record_task == normalized_task
            and record_caller == normalized_caller
            and record_extra == normalized_extra
        )

    @staticmethod
    def _is_recent_duplicate_terminal_call(record: CallRecord, now: datetime) -> bool:
        status = _record_status_value(record)
        if status not in {item.value for item in _TERMINAL_CALL_STATUSES}:
            return False
        ended_at = record.ended_at
        if ended_at is None:
            return False
        if ended_at.tzinfo is None:
            ended_at = ended_at.replace(tzinfo=UTC)
        age_seconds = (now - ended_at.astimezone(UTC)).total_seconds()
        return 0 <= age_seconds <= _PHONE_DUPLICATE_TERMINAL_WINDOW_SECS

    @staticmethod
    def _merge_duplicate_issuer_channel(record: CallRecord, prepared: _PreparedOutboundCall) -> None:
        if prepared.issuer_channel is not None and getattr(record, "issuer_channel", None) is None:
            record.issuer_channel = prepared.issuer_channel
            record.issuer_channel_info = prepared.issuer_channel_info

    def _find_duplicate_call(self, prepared: _PreparedOutboundCall) -> CallRecord | None:
        now = datetime.now(tz=UTC)
        for active_record in self._active_calls.values():
            active_status = _record_status_value(active_record)
            if self._is_same_call_request(active_record, prepared) and (
                active_status in {"pending", "dialing", "ringing", "active"}
                or self._is_recent_duplicate_terminal_call(active_record, now)
            ):
                logger.warning(
                    "Duplicate phone call request suppressed: returning existing call %s",
                    active_record.call_id,
                )
                self._merge_duplicate_issuer_channel(active_record, prepared)
                active_record.duplicate_request_suppressed = True
                return active_record

        active_call_ids = set(self._active_calls)
        for call_id, record in self._call_records.items():
            if call_id in active_call_ids:
                continue
            if self._is_same_call_request(record, prepared) and self._is_recent_duplicate_terminal_call(record, now):
                logger.warning(
                    "Duplicate recent-terminal phone call request suppressed: returning existing call %s",
                    record.call_id,
                )
                self._merge_duplicate_issuer_channel(record, prepared)
                record.duplicate_request_suppressed = True
                return record
        return None

    async def make_call(
        self,
        phone_number: str,
        task: str,
        caller_name: str,
        extra_context: str = "",
        max_duration: int | None = None,
        user_id: str | None = None,
        user_tier: str = "free",
        issuer_channel: Any | None = None,
        issuer_channel_info: dict[str, Any] | None = None,
    ) -> CallRecord:
        """Initiate or queue an outbound phone call.

        Up to ``config.max_concurrent_calls`` outbound calls may be live at
        once (global cap, default 25 via ``phone_global_max_concurrent``).
        Once the cap is reached — or anything is already queued (FIFO) — later
        confirmed calls are queued and auto-dialed in order, without a second
        confirmation, as live calls end and free a slot.
        """
        prepared = await self._prepare_outbound_call(
            phone_number=phone_number,
            task=task,
            caller_name=caller_name,
            extra_context=extra_context,
            max_duration=max_duration,
            user_id=user_id,
            user_tier=user_tier,
            issuer_channel=issuer_channel,
            issuer_channel_info=issuer_channel_info,
        )

        async with self._queue_pump_lock:
            duplicate = self._find_duplicate_call(prepared)
            if duplicate is not None:
                return duplicate

            queue_depth = await self._call_queue.count()
            if self._active_outbound_call_count() >= self._max_active_outbound_calls() or queue_depth > 0:
                queued = await self._enqueue_prepared_call(prepared)
                self._schedule_queue_pump()
                return queued

            return await self._make_call_unqueued(
                phone_number=prepared.phone_number,
                task=prepared.task,
                caller_name=prepared.caller_name,
                extra_context=prepared.extra_context,
                max_duration=prepared.max_duration,
                user_id=prepared.user_id,
                user_tier=prepared.user_tier,
                issuer_channel=prepared.issuer_channel,
                issuer_channel_info=prepared.issuer_channel_info,
            )

    async def _make_call_unqueued(
        self,
        phone_number: str,
        task: str,
        caller_name: str,
        extra_context: str = "",
        max_duration: int | None = None,
        user_id: str | None = None,
        user_tier: str = "free",
        issuer_channel: Any | None = None,
        issuer_channel_info: dict[str, Any] | None = None,
    ) -> CallRecord:
        """Initiate an outbound phone call.

        Args:
            phone_number: Number to dial (E.164: +1XXXXXXXXXX).
            task: What to accomplish ("Book appointment for Tuesday 2pm").
            caller_name: Who Viola is calling on behalf of (display label only).
            extra_context: Additional instructions for the AI.
            max_duration: Override max call duration in seconds.
            user_id: Authenticated owner of the call. If omitted, an
                authenticated ambient user_context must already be installed.
                Never derived from ``caller_name``.
            issuer_channel: Original request channel that issued this call.
                Used for mid-call consultation; never the recipient line.
            issuer_channel_info: Serializable channel reference for audit and
                cloud/request-response paths where the live channel object is
                not available.

        Returns:
            CallRecord with call_id for status tracking.

        Raises:
            RuntimeError: If concurrent call limit is reached.
            ValueError: If phone number format is invalid or user_id is
                missing when required.
            PermissionError: If cloud phone consent is not given.
        """
        # Resolve a stable user identity for ownership + billing FIRST (#1310).
        # Cloud mode MUST have an authenticated user_id; never fall back
        # to caller_name (which is user-controlled display text). Auth gates
        # before any other request processing, including number normalization
        # and the financial safety gate — see _prepare_outbound_call for the
        # full rationale (same ordering fix, same reasoning).
        effective_user_id = self._resolve_effective_user_id(user_id)
        cleaned = normalize_us_e164(phone_number)
        await self._ensure_phone_call_consents(effective_user_id)
        await _require_owner_safety_control(
            "phone_outbound",
            user_id=effective_user_id,
            action="phone_call_dial",
        )

        # ToS gate — keyed by authenticated user
        tos = get_phone_tos()
        tos_result = await tos.check(effective_user_id)
        if not tos_result.allowed:
            raise ValueError(tos_result.reason)

        # Caller name validation (still enforced for AI disclosure)
        validated_name = validate_caller_name(caller_name)
        if validated_name is None:
            raise ValueError("Please set your name in Settings before making calls.")

        # Check opt-out list before dialing
        if is_opted_out(cleaned):
            raise ValueError("That business has asked not to receive AI calls. You'll need to call them directly.")

        precall_plan = await plan_phone_call_prerequisites(
            user_id=effective_user_id,
            task=task,
            caller_name=validated_name,
        )
        validated_name = _resolve_caller_name_from_pre_call_plan(validated_name, precall_plan)

        prepared = _PreparedOutboundCall(
            phone_number=cleaned,
            task=task,
            caller_name=validated_name,
            extra_context=extra_context,
            max_duration=max_duration,
            user_id=effective_user_id,
            user_tier=user_tier,
            issuer_channel=issuer_channel,
            issuer_channel_info=issuer_channel_info or _issuer_channel_info(issuer_channel),
            info_manifest=precall_plan.info_manifest,
        )

        duplicate = self._find_duplicate_call(prepared)
        if duplicate is not None:
            return duplicate

        # Billing gate (usage limits)
        billing = get_phone_billing()
        gate = await billing.check_can_make_call(
            user_id=effective_user_id,
            phone_number=cleaned,
            tier=user_tier,
        )
        if not gate.allowed:
            raise ValueError(gate.reason)

        # Same counter and cap as the queue pump (_start_next_queued_call_if_idle):
        # counting non-terminal records via _active_outbound_call_count() keeps the
        # two gates consistent — raw len(self._active_calls) includes just-ended
        # records not yet reaped, which could make this backstop raise on a call
        # the pump had already admitted, dropping a queued item.
        if self._active_outbound_call_count() >= self._max_active_outbound_calls():
            raise RuntimeError(
                "Maximum concurrent calls reached (%d). Try again later." % self._max_active_outbound_calls()
            )

        _ensure_phone_runtime_dependencies(self.config.tts_provider)

        call_id = uuid.uuid4().hex[:8]
        record = CallRecord(
            call_id=call_id,
            phone_number=cleaned,
            task=task,
            caller_name=validated_name,
            extra_context=extra_context,
            user_id=effective_user_id,
            info_manifest=precall_plan.info_manifest,
            issuer_channel=issuer_channel,
            issuer_channel_info=issuer_channel_info or _issuer_channel_info(issuer_channel),
        )
        self._active_calls[call_id] = record
        self._call_records[call_id] = record
        # PHONE_TRANSCRIPT_ACTIVE: the manager instance + key under which the call
        # is registered. A live trace compares mgr_id here against the mgr_id in
        # PHONE_TRANSCRIPT_L2_DROP: same id + missing key => timing race (ii);
        # different id => the transcript bridge subscribed on a DIFFERENT manager
        # instance (i); id matches and key present => neither.
        logger.info(
            "PHONE_TRANSCRIPT_ACTIVE call=%s user=%s mgr_id=%s mode=%s",
            call_id,
            effective_user_id,
            id(self),
            self.config.mode,
        )

        try:
            duration = max_duration or self.config.max_call_duration
            from telephony.cost_tracker import estimate_phone_tts_reservation_cents

            estimated_spend_cents = max(
                1,
                1 + estimate_phone_tts_reservation_cents(self.config.tts_provider, duration),
            )
            await billing.record_call_start(
                effective_user_id,
                call_id,
                cleaned,
                user_tier,
                estimated_cents=estimated_spend_cents,
            )
            self._start_billing_heartbeat(record)

            coro = self._run_call(record, duration, extra_context, effective_user_id)
            call_task = asyncio.create_task(coro)
            call_task.add_done_callback(lambda _task, cid=call_id: self._cancel_billing_heartbeat(cid))
            self._call_tasks[call_id] = call_task

            logger.info(
                "Call %s initiated: %s → %s (task: %s)",
                call_id,
                _mask_phone_number(self.config.phone_number),
                _mask_phone_number(cleaned),
                _mask_phone_numbers_in_text(task[:80]),
            )
            return record
        except Exception:
            self._cancel_billing_heartbeat(call_id)
            self._active_calls.pop(call_id, None)
            self._call_records.pop(call_id, None)
            # If we got past record_call_start (which debits the reserved estimate
            # and opens a durable reservation ledger row) but failed to launch
            # _run_call — e.g. _start_billing_heartbeat or create_task raised — the
            # call task never runs, so its finally-block settle never fires and the
            # reserved hold would leak against this user's own counter. Release it
            # here as a failed/zero-cost settle. record_call_end is idempotent via
            # the reservation-ledger claim, so if _run_call DID start and races us,
            # exactly one settle wins (no double-refund, no leak).
            try:
                await billing.record_call_end(
                    effective_user_id,
                    call_id,
                    0.0,
                    0.0,
                    status=CallStatus.FAILED.value,
                )
            except Exception:
                logger.exception(
                    "Failed to release phone spend reservation after call-launch failure "
                    "for user=%s call=%s; the startup reservation reaper will recover it",
                    effective_user_id,
                    call_id,
                )
            raise

    async def _start_next_queued_call_if_idle(self) -> None:
        async with self._queue_pump_lock:
            # Items whose owner is at their per-plan concurrent limit are held
            # aside during this pass and restored afterwards. Restoring via
            # enqueue keeps each item's original queued_at, and the backend
            # orders by queued_at — so a deferred item keeps its FIFO position
            # while OTHER users' items behind it can still start (one tenant
            # queueing many calls must not starve everyone else's queue).
            deferred_items: list[QueuedOutboundCall] = []
            try:
                # Fill every free slot up to the configured cap in this single
                # pass. A cold burst of confirmed calls (all queued while zero
                # are live) must ramp straight to the cap here; returning after
                # the first start would pin throughput at one concurrent call
                # until a live call ended, silently re-imposing the old
                # 1-at-a-time limit even with the cap raised to 25.
                while self._active_outbound_call_count() < self._max_active_outbound_calls():
                    item = await self._call_queue.pop_next()
                    if item is None:
                        return
                    if not self._user_has_outbound_capacity(item.user_id, item.user_tier):
                        # Owner still has a live call and their plan allows no
                        # more; keep the item queued (NOT dropped) — it starts
                        # when their call ends and re-pumps the queue.
                        deferred_items.append(item)
                        continue
                    await self._broadcast_call_queue_update(item.user_id)
                    issuer_channel = self._queued_issuer_channels.pop(item.queue_id, None)
                    try:
                        await self._make_call_unqueued(
                            phone_number=item.phone_number,
                            task=item.task,
                            caller_name=item.caller_name,
                            extra_context=item.extra_context,
                            max_duration=item.max_duration,
                            user_id=item.user_id,
                            user_tier=item.user_tier,
                            issuer_channel=issuer_channel,
                            issuer_channel_info=item.issuer_channel_info,
                        )
                        logger.info(
                            "Started queued outbound phone call %s for user=%s",
                            item.queue_id,
                            item.user_id,
                        )
                        # Keep filling remaining slots; do NOT return after one start.
                    except Exception as exc:  # noqa: BLE001, RUF100 - failed start skips item, not the pump
                        logger.warning(
                            "Queued phone call %s could not be started and was skipped: %s",
                            item.queue_id,
                            exc,
                        )
            finally:
                for deferred in deferred_items:
                    try:
                        await self._call_queue.enqueue(deferred)
                    except Exception:  # noqa: BLE001, RUF100 - one failed restore must not abort the rest
                        logger.exception(
                            "Failed to restore deferred queued phone call %s for user=%s",
                            deferred.queue_id,
                            deferred.user_id,
                        )

    def get_record(self, call_id: str) -> CallRecord | None:
        """Return the CallRecord for a call_id or None.

        Does NOT enforce ownership — callers that need per-user isolation
        (cloud routes) should check ``record.user_id`` themselves.
        """
        return self._active_calls.get(call_id) or self._call_records.get(call_id)

    def get_active_call_for_user(self, user_id: str) -> CallRecord | None:
        """Return the user's currently-live call, or None.

        "Live" means a call that is on the line right now (DIALING / RINGING /
        ACTIVE) — NOT a queued call waiting behind it and NOT a terminal/ended
        call. Scoped to ``user_id`` so one account can never see another's call.

        This is the recovery path for a desktop tab opened mid-call: the
        frontend learns ``activeCallId`` from transient ``call_started`` /
        ``call_consultation`` WebSocket events, which are gone by the time a tab
        is opened later in the call. Without this lookup the panel falls back to
        the call-history list while a call is live (founder-observed symptom,
        first production call 2026-06-29).
        """
        safe_user_id = str(user_id or "").strip()
        if not safe_user_id:
            return None
        # _active_calls holds in-flight + just-ended-not-yet-reaped records;
        # filter to genuinely-live ones owned by this user, newest first.
        candidates = [
            record
            for record in self._active_calls.values()
            if str(getattr(record, "user_id", "") or "").strip() == safe_user_id
            and not _is_terminal_call_status(record.status)
            and record.status != CallStatus.QUEUED
        ]
        if not candidates:
            return None

        def _sort_key(record: CallRecord) -> tuple[int, float]:
            # Prefer an ACTIVE call over one still dialing/ringing; within a
            # status tier, prefer the most recently started.
            active_rank = 1 if record.status == CallStatus.ACTIVE else 0
            started = getattr(record, "started_at", None)
            started_ts = started.timestamp() if started is not None else 0.0
            return (active_rank, started_ts)

        return max(candidates, key=_sort_key)

    def get_record_by_call_control_id(self, call_control_id: str) -> CallRecord | None:
        """Return the record for a Telnyx call_control_id.

        Scans ``_active_calls`` FIRST (a live call is the common case), then
        falls back to ``_call_records``. The fallback is load-bearing for
        billing honesty (#3385): ``_run_call``'s ``finally`` pops the record out
        of ``_active_calls`` in the same block that settles billing, so a
        ``call.hangup`` proving no-answer that lands after that pop used to find
        NOTHING — the carrier's own proof was dropped on the floor and the
        ledger kept an optimistic ``completed`` for a call nobody answered.
        ``_call_records`` still holds the record, so the late event can still be
        reconciled. Telnyx call_control_ids are unique per leg, so widening the
        scan cannot mismatch a different call.
        """
        if not call_control_id:
            return None
        for record in self._active_calls.values():
            if record.telnyx_call_control_id == call_control_id:
                return record
        for record in self._call_records.values():
            if record.telnyx_call_control_id == call_control_id:
                return record
        return None

    async def handle_answering_machine_detection(
        self,
        call_control_id: str,
        result: str,
        event_type: str = "",
    ) -> bool:
        """Route Telnyx AMD webhook results into the active phone agent context."""
        record = self.get_record_by_call_control_id(call_control_id)
        if record is None:
            logger.info(
                "AMD result for unknown call_control_id=%s event=%s result=%s",
                call_control_id[:16],
                event_type,
                result,
            )
            return False

        handler = record._voicemail_handler
        if handler is not None and hasattr(handler, "on_amd_result"):
            return bool(await handler.on_amd_result(result, event_type))

        normalized = (result or "").strip().lower()
        if normalized == "machine":
            record.voicemail_detected = True
            record.voicemail_detected_at = datetime.now(tz=UTC)
            record.voicemail_detection_source = "amd"
            record.outcome = record.outcome or "Voicemail detected via AMD"
            return True
        return False

    async def handle_call_answered(self, call_control_id: str) -> bool:
        """Record the authoritative Telnyx ``call.answered`` signal (issue #2796).

        ACTIVE is set when the media stream connects, which can precede the
        real answer. Marking ``carrier_answered`` here gives terminal-status
        derivation ground truth that the callee actually picked up, so an
        answered call is honestly reported COMPLETED and an unanswered one is
        not.
        """
        record = self.get_record_by_call_control_id(call_control_id)
        if record is None:
            logger.info(
                "call.answered for unknown call_control_id=%s",
                call_control_id[:16],
            )
            return False
        if not record.carrier_answered:
            record.carrier_answered = True
            record.carrier_answered_at = datetime.now(tz=UTC)
            logger.info("Call %s: carrier confirmed answer", record.call_id)
        return True

    async def handle_call_hangup(self, call_control_id: str, hangup_cause: str = "") -> bool:
        """Record the authoritative Telnyx ``call.hangup`` cause (issue #2796).

        Stores ``hangup_cause`` so terminal-status derivation can tell a real
        conversation apart from a no-answer / busy / rejected leg. Also
        reconciles a LATE hangup: if the pipeline already ended and the record
        was optimistically marked COMPLETED, but the carrier now reports a
        non-answer cause (or reports a hangup with no answer ever confirmed),
        correct the terminal status to NO_ANSWER so an unanswered call is never
        left reported as completed-success and is billed as a failure. Only an
        optimistic COMPLETED is corrected -- an already-honest terminal status
        (FAILED/TIMEOUT/CANCELLED/NO_ANSWER/VOICEMAIL) is left untouched.
        """
        record = self.get_record_by_call_control_id(call_control_id)
        if record is None:
            logger.info(
                "call.hangup for unknown call_control_id=%s (cause=%s)",
                call_control_id[:16],
                hangup_cause or "unknown",
            )
            return False

        cause = (hangup_cause or "").strip().lower()
        record.carrier_hangup_cause = cause
        # #2589: keep the TIME, not just the cause. This is the authoritative
        # end of the billable leg. Earliest-wins so a Telnyx redelivery of the
        # same event cannot push the billed end later.
        now = datetime.now(tz=UTC)
        if record.carrier_hangup_at is None or now < record.carrier_hangup_at:
            record.carrier_hangup_at = now

        # Late-hangup reconciliation: correct an optimistic COMPLETED only.
        if record.status == CallStatus.COMPLETED:
            non_answer_cause = bool(cause) and cause in _NON_ANSWER_HANGUP_CAUSES
            never_answered = bool(cause) and not record.carrier_answered
            if non_answer_cause or never_answered:
                record.status = CallStatus.NO_ANSWER
                logger.info(
                    "Call %s: late call.hangup reconciled COMPLETED->no_answer (carrier_answered=%s hangup_cause=%s)",
                    record.call_id,
                    record.carrier_answered,
                    cause or "<none>",
                )
                await self._correct_settled_ledger_status(record, CallStatus.NO_ANSWER)
        return True

    async def handle_local_carrier_hangup(self, call_control_id: str, hangup_cause: str = "") -> bool:
        """End local media after a signed carrier event, even if its socket stays open."""
        if not await self.handle_call_hangup(call_control_id, hangup_cause):
            return False
        record = self.get_record_by_call_control_id(call_control_id)
        # The carrier already ended the leg; cleanup must not issue a new hangup.
        record._telnyx_hangup_dispatched = True
        if not _is_terminal_call_status(record.status):
            if record._pipeline_task is not None:
                from pipecat.frames.frames import EndFrame

                await record._pipeline_task.queue_frame(EndFrame(reason="Carrier ended local call"))
            else:
                record.status = (
                    CallStatus.VOICEMAIL if record.voicemail_detected else _derive_natural_end_status(record)
                )
                task = self._call_tasks.get(record.call_id)
                if task is not None:
                    task.cancel()
        return True

    async def _correct_settled_ledger_status(self, record: CallRecord, status: CallStatus) -> None:
        """Push a late carrier correction through to the billing ledger (#3385).

        Correcting ``record.status`` alone is not enough: ``_run_call``'s
        ``finally`` may have ALREADY settled this call from the optimistic
        COMPLETED, and a settled row cannot be re-settled (both billing backends
        guard the settle ``UPDATE`` with ``AND status = 'active'``). Without
        this, the money-side record of a call the carrier proved was never
        answered stayed ``completed`` permanently.

        Status-only and idempotent — see
        ``PhoneBillingGate.correct_settled_call_status``. Best-effort: this runs
        inside the Telnyx webhook request, which must return 200 within 20s or
        Telnyx retries the event, so a billing-store hiccup is logged and
        swallowed rather than turned into a retry storm. The correction is
        naturally re-applied if Telnyx redelivers.
        """
        user_id = str(getattr(record, "user_id", "") or "").strip()
        if not user_id:
            logger.warning(
                "Call %s: cannot correct settled ledger status (no user_id on record)",
                record.call_id,
            )
            return
        try:
            corrected = await get_phone_billing().correct_settled_call_status(
                user_id,
                record.call_id,
                status.value,
            )
        except Exception:
            logger.exception(
                "Call %s: failed to correct settled ledger status to %s after late carrier signal",
                record.call_id,
                status.value,
            )
            return
        if corrected:
            self._repersist_corrected_call_history(record)

    def _repersist_corrected_call_history(self, record: CallRecord) -> None:
        """Re-write the call-history entry so it agrees with the corrected ledger.

        ``_persist_call_history_entry`` snapshots ``record.status`` at the moment
        it runs, and ``_run_call``'s ``finally`` runs it AFTER the billing settle.
        A late carrier correction therefore has three possible landings, and only
        one of them used to leave the two records agreeing. Now that the record
        stays reachable via ``_call_records`` until ``cleanup_completed`` reaps
        it, the correction window is much wider than that block, so without this
        the money-side row would read ``no_answer`` while the user's own call log
        still read ``completed`` for the same call — a worse story than either
        record being wrong alone.

        Only re-writes an entry that ALREADY exists: if the finally block has not
        persisted history yet, it is about to, and it will pick up the corrected
        ``record.status`` on its own. ``save_call_history`` overwrites
        ``metadata.json`` by call id, so this is idempotent under Telnyx webhook
        redelivery. Best-effort for the same reason as the ledger correction: the
        webhook must return 200 within 20s.
        """
        try:
            from telephony.call_history import get_call_dir

            if not (get_call_dir(record.call_id) / "metadata.json").exists():
                return
            _persist_call_history_entry(record)
            logger.info(
                "Call %s: call-history entry re-persisted as %s after late carrier correction",
                record.call_id,
                record.status.value,
            )
        except Exception:
            logger.exception(
                "Call %s: failed to re-persist corrected call history; the billing ledger "
                "is corrected but the call-history entry may still read completed",
                record.call_id,
            )

    async def end_call(
        self,
        call_id: str,
        *,
        wait_for_cleanup: bool = True,
        user_id: str | None = None,
    ) -> bool:
        """Cancel or hang up an active call.

        Multi-tenant: ``user_id`` is REQUIRED.  A call id known to one
        tenant must NOT let them hang up another tenant's call.  We
        owner-check both the in-memory record and (when only the task
        is present) refuse — a task without a record is not safe to
        cancel from an unrelated user.

        Returns:
            True if the call was found, owned by ``user_id``, and
            cancelled.
        """
        if not user_id:
            return False
        record = self._active_calls.get(call_id)
        task = self._call_tasks.get(call_id)
        if record is None and task is None:
            return False
        if record is not None and not self._owner_check(record, user_id):
            logger.warning(
                "Refused end_call for %s: caller %s does not own the call",
                call_id,
                user_id,
            )
            return False
        if record is None:
            # Only a task is registered — we cannot prove ownership
            # without a record, so we refuse rather than cancel another
            # tenant's task.
            logger.warning(
                "Refused end_call for %s: no record found to authorize caller %s",
                call_id,
                user_id,
            )
            return False

        # Try to hang up via Telnyx API first
        if record and record.telnyx_call_control_id:
            call_control_id = str(record.telnyx_call_control_id or "")
            try:
                hung_up = await _async_post_telnyx_hangup(self.config.api_key, call_control_id)
                if hung_up:
                    record._telnyx_hangup_dispatched = True
                    logger.info("Call %s hung up via Telnyx API", call_id)
                else:
                    logger.warning("Telnyx hangup failed for %s, cancelling task", call_id)
            except Exception:
                logger.warning("Telnyx hangup failed for %s, cancelling task", call_id)

        # Also cancel the async task
        current_task = asyncio.current_task()
        if task and not task.done():
            task.cancel()
            if record and not _is_terminal_call_status(record.status):
                record.status = CallStatus.CANCELLED
            logger.info("Call %s cancelled by user", call_id)
            await self._cancel_billing_heartbeat_and_wait(call_id)
            self._active_calls.pop(call_id, None)
            if wait_for_cleanup and task is not current_task:
                with suppress(asyncio.CancelledError):
                    await task
                await self._cancel_billing_heartbeat_and_wait(call_id)
            return True

        await self._cancel_billing_heartbeat_and_wait(call_id)
        self._active_calls.pop(call_id, None)
        self._call_tasks.pop(call_id, None)
        self._schedule_queue_pump()
        if record and not _is_terminal_call_status(record.status):
            record.status = CallStatus.CANCELLED
            record.ended_at = datetime.now(tz=UTC)
            # ``_run_call``'s finally overwrites ended_at with the teardown time,
            # so keep this honest end for billing (#2589).
            if record.local_end_at is None:
                record.local_end_at = record.ended_at
            logger.info("Call %s cancelled by user", call_id)
            return True
        if task is not None:
            return True
        return False

    def hangup_active_calls_for_shutdown(self) -> int:
        """Best-effort synchronous hangup for signal/atexit shutdown paths."""
        hung_up = 0
        for record in list(self._active_calls.values()):
            # The owner-takeover conference leg is a separate outbound Telnyx
            # call bounded only by time_limit_secs. Hang it up here too so a
            # process exit does not leave it billing until the server time limit
            # (#2800). Independent of the primary leg's hangup need -- the leg can
            # still be live even when the primary no longer needs a hangup.
            conference_leg_id = str(getattr(record, "_conference_leg_call_control_id", "") or "")
            if conference_leg_id and not getattr(record, "_conference_leg_hangup_dispatched", False):
                if _sync_post_telnyx_hangup(self.config.api_key, conference_leg_id):
                    record._conference_leg_hangup_dispatched = True
                    logger.warning(
                        "Shutdown hook hung up conference owner-leg for call %s via Telnyx",
                        record.call_id,
                    )
            if not _record_needs_telnyx_hangup(record):
                continue
            call_control_id = str(record.telnyx_call_control_id or "")
            if _sync_post_telnyx_hangup(self.config.api_key, call_control_id):
                hung_up += 1
                record.status = CallStatus.CANCELLED
                record.error = record.error or "Viola process exited; active Telnyx call was hung up."
                logger.warning(
                    "Shutdown hook hung up active call %s via Telnyx",
                    record.call_id,
                )
        return hung_up

    def _owner_check(self, record: Any, user_id: str | None) -> bool:
        """Return True iff ``user_id`` owns ``record``.

        Without ``user_id`` we treat the lookup as unauthorized — phone
        call records carry transcripts and PII, so we refuse rather
        than fall back to "any record matching call_id".
        """
        if not user_id:
            return False
        owner = str(getattr(record, "user_id", "") or "")
        return bool(owner) and owner == user_id

    def get_status(self, call_id: str, *, user_id: str | None = None) -> dict[str, Any]:
        """Get current status of a call.

        Multi-tenant: ``user_id`` is REQUIRED.  A call id known to one
        tenant does not entitle them to read another tenant's status,
        so the manager owner-checks the in-memory record AND the
        persisted file before answering.
        """
        if not user_id:
            return {"call_id": call_id, "status": "not_found"}
        record = self._active_calls.get(call_id)
        if not record:
            record = self._call_records.get(call_id)
        if record is not None and not self._owner_check(record, user_id):
            return {"call_id": call_id, "status": "not_found"}
        if not record:
            persisted = _load_persisted_call_transcript(call_id, user_id=user_id)
            if persisted is not None:
                return {
                    "call_id": call_id,
                    "status": persisted.get("status", "unknown"),
                    "phone_number": persisted.get("phone_number", ""),
                    "task": persisted.get("task", ""),
                    "duration_seconds": round(float(persisted.get("duration_seconds") or 0.0), 1),
                    "transcript_lines": len(persisted.get("transcript") or []),
                    "error": persisted.get("error") or None,
                    "voicemail_detected": bool(persisted.get("voicemail_detected")),
                    "human_takeover_detected": bool(persisted.get("human_takeover_detected")),
                }
            return {"call_id": call_id, "status": "not_found"}

        # Same meter as the settle and as the persisted branch a few lines up
        # (#2589). This field is named ``duration_seconds`` in both branches, so
        # a live call and the same call once it lands must not report on two
        # different bases -- that divergence is the bug this ticket fixed.
        elapsed = 0.0
        if record.started_at:
            elapsed = _billable_window_seconds(record)

        return {
            "call_id": call_id,
            "status": record.status.value,
            "phone_number": record.phone_number,
            "task": record.task,
            "duration_seconds": round(elapsed, 1),
            "transcript_lines": len(record.transcript),
            "error": record.error or None,
            "voicemail_detected": record.voicemail_detected,
            "human_takeover_detected": record.human_takeover_detected,
        }

    def get_transcript(self, call_id: str, *, user_id: str | None = None) -> dict[str, Any]:
        """Get full transcript and summary for a call.

        Multi-tenant: ``user_id`` is REQUIRED — see :meth:`get_status`.
        Works for both active and completed calls; the persisted file
        is read from the per-user partition.
        """
        if not user_id:
            return {"call_id": call_id, "error": "Call not found"}
        record = self._active_calls.get(call_id)
        if not record:
            record = self._call_records.get(call_id)
        if record is not None and not self._owner_check(record, user_id):
            return {"call_id": call_id, "error": "Call not found"}
        if not record:
            persisted = _load_persisted_call_transcript(call_id, user_id=user_id)
            if persisted is not None:
                return {
                    "call_id": call_id,
                    "status": persisted.get("status", "unknown"),
                    "transcript": persisted.get("transcript") or [],
                    "summary": persisted.get("summary", ""),
                    "outcome": persisted.get("outcome", ""),
                    "duration_seconds": persisted.get("duration_seconds", 0),
                    "error": persisted.get("error") or None,
                    "voicemail_detected": bool(persisted.get("voicemail_detected")),
                    "voicemail_detected_at": persisted.get("voicemail_detected_at"),
                    "voicemail_detection_source": persisted.get("voicemail_detection_source", ""),
                    "human_takeover_detected": bool(persisted.get("human_takeover_detected")),
                    "human_takeover_at": persisted.get("human_takeover_at"),
                    "cost": {
                        "estimated_total_usd": 0.0,
                        "llm_prompt_tokens": 0,
                        "llm_completion_tokens": 0,
                    },
                }
            return {"call_id": call_id, "error": "Call not found"}

        return {
            "call_id": call_id,
            "status": record.status.value,
            "transcript": record.transcript,
            "summary": record.summary,
            "outcome": record.outcome,
            "duration_seconds": record.duration_seconds,
            "voicemail_detected": record.voicemail_detected,
            "voicemail_detected_at": (
                record.voicemail_detected_at.isoformat() if record.voicemail_detected_at else None
            ),
            "voicemail_detection_source": record.voicemail_detection_source,
            "human_takeover_detected": record.human_takeover_detected,
            "human_takeover_at": (record.human_takeover_at.isoformat() if record.human_takeover_at else None),
            "cost": {
                "estimated_total_usd": round(record.estimated_cost_usd, 4),
                "llm_prompt_tokens": record.llm_prompt_tokens,
                "llm_completion_tokens": record.llm_completion_tokens,
            },
        }

    # ------------------------------------------------------------------
    # Internal: run the actual call
    # ------------------------------------------------------------------

    async def _run_call(
        self,
        record: CallRecord,
        max_duration: int,
        extra_context: str,
        effective_user_id: str = "",
    ) -> None:
        """Execute a phone call using Pipecat pipeline + Telnyx.

        Flow:
        1. Create Pipecat pipeline (TelnyxTransport + Whisper + gpt-5.4-mini + Kokoro)
        2. Start the WebSocket server (transport)
        3. Dial via Telnyx REST API with stream_url pointing to our WS
        4. Wait for Telnyx to connect, run conversation
        5. Collect transcript, generate summary
        """
        transcript_collector = TranscriptCollector(record)
        from telephony.payment_sensitive_segment import (
            OUTCOME_ABORTED,
            PaymentSensitiveSegmentController,
        )

        payment_sensitive_segment = PaymentSensitiveSegmentController(call_id=record.call_id)
        payment_sensitive_segment.attach_transcript_collector(transcript_collector)
        record._payment_sensitive_segment = payment_sensitive_segment
        hold_handler = None
        acquired_number: str | None = None
        disconnect_watchdog_task: asyncio.Task[None] | None = None
        ring_warmup_task: asyncio.Task[None] | None = None
        phone_latency_trace: PhoneLatencyTraceRecorder | None = None
        pipeline_future: asyncio.Future | None = None

        # Remote-voice worker warm-state machine (see _run_call warm-start below).
        # Declared before the try so the finally can always cancel the keep-alive
        # even if the call fails before warm-start. remote_warm_started is stamped
        # at the real warm-start; nested readers see the updated value at call time.
        remote_warm_task: asyncio.Task[None] | None = None
        remote_warm_ready = asyncio.Event()
        remote_warm_started = 0.0

        async def _cancel_remote_warm(reason: str) -> None:
            """Stop + await the remote-worker warm keep-alive task (idempotent).

            Called when the opening turn speaks (real traffic sustains warmth) and
            in the finally cleanup — so the one task that owns the warm-state
            machine never leaks past the call.
            """
            nonlocal remote_warm_task
            task_ref = remote_warm_task
            remote_warm_task = None
            if task_ref is not None and not task_ref.done():
                logger.info(
                    "Call %s: stopping remote-worker warm keep-alive (%s) at t=%.3fs",
                    record.call_id,
                    reason,
                    ((time.monotonic() - remote_warm_started) if remote_warm_started else 0.0),
                )
                task_ref.cancel()
                with suppress(asyncio.CancelledError):
                    await task_ref

        # Intercept Pipecat's loguru output into Viola's logger so we can
        # see VAD events, LLM calls, and frame flow in viola-qt.log.
        _install_pipecat_loguru_bridge()

        try:
            await self._ensure_phone_call_consents(record.user_id or effective_user_id)
            record.status = CallStatus.DIALING

            # --- Import Pipecat components (lazy to avoid import cost) ---
            from pipecat.audio.vad.vad_analyzer import VADParams
            from pipecat.observers.user_bot_latency_observer import (
                UserBotLatencyObserver,
            )
            from pipecat.pipeline.pipeline import Pipeline
            from pipecat.pipeline.runner import PipelineRunner
            from pipecat.pipeline.task import PipelineParams, PipelineTask
            from pipecat.processors.aggregators.llm_context import LLMContext
            from pipecat.processors.aggregators.llm_response_universal import (
                LLMContextAggregatorPair,
                LLMUserAggregatorParams,
            )
            from pipecat.services.openai.llm import OpenAILLMService

            from telephony.phone_vad_calibration import CalibratingSileroVADAnalyzer
            from telephony.telnyx_transport import (
                TelnyxTransport,
                TelnyxTransportParams,
            )

            # --- Build pipeline components ---
            # Load order matters for memory: TTS first (Kokoro may fail and
            # free memory), then STT, so Whisper has a clean memory pool.

            # TTS — local Kokoro; paid cloud fallback is deliberately disabled.
            # Pass the call_id so the corruption guard's strip-log is
            # correlatable to this call's trace (turn-type + end_call signal).
            tts = self._create_tts(call_id=record.call_id)

            # Transport — our custom Telnyx WebSocket bridge
            # VAD is NOT on the transport — it goes on the user aggregator
            # (per official Pipecat pattern).
            transport_params = TelnyxTransportParams(
                host="0.0.0.0",  # nosec B104 — server must listen on all interfaces
                port=self.config.ws_port,
                stream_shared_secret=self.config.stream_shared_secret,
                audio_in_enabled=True,
                # Inbound stays at Telnyx's native 8 kHz so Silero VAD can detect
                # telephone-band recipient speech (see TELNYX_SAMPLE_RATE in
                # telnyx_transport.py — 16 kHz upsampling made Viola deaf to the
                # recipient, 2026-06-24). faster-whisper transcribes 8 kHz fine.
                audio_in_sample_rate=PHONE_INBOUND_SAMPLE_RATE,
                audio_out_enabled=True,
                audio_out_sample_rate=16000,
            )
            transport = TelnyxTransport(transport_params)

            def _capture_telnyx_call_control_id(call_control_id: str) -> None:
                record.telnyx_call_control_id = call_control_id
                logger.info(
                    "Call %s: captured Telnyx call_control_id from media stream: %s",
                    record.call_id,
                    call_control_id[:16],
                )

            transport.set_call_control_id_callback(_capture_telnyx_call_control_id)

            # Cloud mode: register transport so cloud_routes can inject WS
            if self.config.mode == "cloud":
                from telephony.cloud_routes import register_transport

                transport.set_cloud_mode()
                register_transport(record.call_id, transport)

            info_manifest = record.info_manifest or build_phone_info_manifest(user_id=record.user_id, task=record.task)
            info_manifest = merge_info_manifests(info_manifest)

            # STT — local faster-whisper (MKL threads limited to prevent OOM).
            # Bias with the same per-call facts used by the phone prompt so
            # known names/businesses are available before the model hears them.
            stt_bias_text = _phone_stt_manifest_text(info_manifest)
            self._stt_hotwords = _phone_stt_hotwords(record.task, record.caller_name, extra_context, stt_bias_text)
            self._stt_initial_prompt = _phone_stt_initial_prompt(
                record.task,
                record.caller_name,
                extra_context,
                stt_bias_text,
            )
            stt = self._create_stt()
            # Phase 1 streaming STT first pass (interims for turn-taking). Returns
            # None unless VIOLA_PHONE_STT_STREAMING is on AND the sherpa-onnx runtime
            # + model are present; a missing model fails open to whisper-only.
            streaming_stt = self._create_streaming_stt()

            # --- Remote voice worker warm-start (RunPod serverless STT/TTS) ---
            # ADDITIVE to _prewarm_call_runtime_during_ring, which seeds the LOCAL
            # Kokoro resampler + managed LLM warmup (a DIFFERENT layer, kept). This
            # warms the SEPARATE remote RunPod STT/TTS worker so the OPENING turn
            # does not run cold on slow local CPU. We are already post-approval
            # here (the user confirmed the call), so warming now honors the cost
            # rule (warm only after "yes"). One task owns the whole warm-state
            # machine: initial warm -> keep-alive ping loop, gated at the dial on
            # the RETURNED warm signal (not a timer).
            remote_warm_started = time.monotonic()
            # "Preparing to call" cue on the EXISTING call-lifecycle WS stream, the
            # instant the user confirmed: call_started makes the desktop pill show
            # this live call with its current status (DIALING here) — an existing
            # event + existing status, not a new channel/status. The real
            # call_started at answer re-broadcasts as ACTIVE; call_ended in finally
            # always clears the pill on every terminal path. It does NOT auto-open
            # the phone tab or the listen socket (founder direction 2026-06-29).
            await self._broadcast_call_ws_event("call_started", record)
            # Only run the warm machine when the remote worker is actually
            # configured/reachable. A cold, scaled-to-zero worker still reports
            # available (it is retried + used once warm within this call); an
            # UNCONFIGURED remote returns False, and we skip (local STT/TTS path).
            try:
                from telephony import remote_voice as _remote_voice_probe

                _remote_configured = bool(_remote_voice_probe.remote_voice_available())
            # Best-effort: the availability probe must never break the call.
            except Exception as exc:  # noqa: BLE001, RUF100
                logger.debug(
                    "Call %s: remote_voice_available probe failed: %s",
                    record.call_id,
                    exc,
                )
                _remote_configured = False
            if _remote_configured:
                # C-302a: adopt warmth an earlier pre-warm already PROVED, so the
                # dial gate does not re-pay a cold start that has already been
                # paid while the user was confirming the call. This is an
                # observation with a TTL (a warmup response that actually came
                # back, recently), never an assumption — a stale or absent
                # observation leaves the gate to prove warmth itself, exactly as
                # before. The founder's 2026-07-05 ruling is untouched: we still
                # never dial into a worker we have not seen warm.
                try:
                    from telephony import remote_voice as _remote_voice_prewarm

                    _already_warm = bool(_remote_voice_prewarm.is_remote_voice_warm())
                    _warm_age = _remote_voice_prewarm.remote_voice_warm_age_secs()
                    # The call's own keep-alive loop below takes over from here,
                    # so exactly one warm machine pings the worker at a time.
                    _remote_voice_prewarm.stop_remote_voice_prewarm()
                # Best-effort: adopting pre-warm state must never break the call.
                except Exception as exc:  # noqa: BLE001, RUF100
                    logger.debug(
                        "Call %s: pre-warm adoption failed (proving warmth in-call): %s",
                        record.call_id,
                        exc,
                    )
                    _already_warm = False
                    _warm_age = None
                if _already_warm:
                    remote_warm_ready.set()
                    logger.info(
                        "Call %s: adopted pre-warm warm signal (observed %.3fs ago) — "
                        "dial gate opens immediately instead of re-paying the cold start",
                        record.call_id,
                        float(_warm_age or 0.0),
                    )
                logger.info(
                    "Call %s: remote-worker warm-start (post-approval) at t=0.000s; "
                    "dial gated on warm signal (ceiling=%.1fs, keepalive=%.1fs, prewarmed=%s)",
                    record.call_id,
                    REMOTE_WARM_DIAL_CEILING_S,
                    REMOTE_WARM_KEEPALIVE_INTERVAL_S,
                    _already_warm,
                )
                remote_warm_task = asyncio.create_task(
                    _remote_warm_loop(
                        record.call_id,
                        remote_warm_ready,
                        remote_warm_started,
                        ping_timeout_s=REMOTE_WARM_PING_TIMEOUT_S,
                        keepalive_s=REMOTE_WARM_KEEPALIVE_INTERVAL_S,
                    ),
                    name="phone-remote-warm-%s" % record.call_id,
                )
            else:
                logger.info(
                    "Call %s: remote voice not configured/reachable — skipping remote "
                    "warm machine (local STT/TTS path)",
                    record.call_id,
                )

            from telephony.audio_tee_processor import AudioTeeProcessor

            inbound_tee = AudioTeeProcessor("inbound", payment_sensitive_controller=payment_sensitive_segment)
            outbound_tee = AudioTeeProcessor("outbound", payment_sensitive_controller=payment_sensitive_segment)
            record._inbound_tee = inbound_tee
            record._outbound_tee = outbound_tee

            try:
                from config.settings import settings as _app_settings

                # Env override (VIOLA_RECORD_PHONE_CALLS_OVERRIDE=true|false)
                # wins so devs / deployments can toggle without persisting to
                # the tracked settings.json. Empty string = no override.
                recording_override = getattr(_app_settings, "record_phone_calls_override", "")
            except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
                recording_override = ""

            phone_setup_settings = await _resolve_phone_call_setup_settings(
                record.user_id,
                cloud_mode=self.config.mode == "cloud",
                record_phone_calls_override=recording_override,
            )

            record_phone_calls = phone_setup_settings.record_phone_calls
            keep_phone_transcript = phone_setup_settings.keep_phone_transcript
            announce_ai_on_calls = phone_setup_settings.announce_ai_on_calls
            phone_ai_identity_enforcement = phone_setup_settings.phone_ai_identity_enforcement
            record.recording_enabled = bool(record_phone_calls)
            record.transcript_retention_enabled = bool(keep_phone_transcript)
            phone_context_timezone_name = await _resolve_phone_context_timezone_name(
                record.user_id,
                cloud_mode=self.config.mode == "cloud",
            )
            phone_volatile_context_builder = partial(
                build_phone_volatile_context,
                call_record=record,
                caller_name=record.caller_name,
                timezone_name=phone_context_timezone_name,
            )
            # Effective proactive AI disclosure for this call: the user opt-in
            # AND the per-deployment AI-disclosure capability. The AI-identity
            # watchdog handles this language-agnostically (SEC-059); recording
            # disclosure remains guarded before persisted recording starts.
            effective_announce_ai = bool(self.config.ai_disclosure_enabled and announce_ai_on_calls)

            # LLM — gpt-5.4-mini via OpenAI-compatible API
            # System prompt goes in Settings(system_instruction=...) per Pipecat docs
            system_prompt = build_phone_system_instruction(
                caller_name=record.caller_name,
                task=record.task,
                extra_context=extra_context,
                record_phone_calls=record.recording_enabled,
                keep_phone_transcript=record.transcript_retention_enabled,
                announce_ai_on_calls=effective_announce_ai,
                info_manifest=info_manifest,
                session_id="phone:%s" % record.call_id,
                include_volatile_context=False,
                timezone_name=phone_context_timezone_name,
            )
            from pipecat.services.openai.responses.llm import OpenAIResponsesLLMService

            from telephony.traced_openai_responses_llm_service import (
                TracedOpenAIResponsesLLMService,
            )

            # Construct a per-call TaskTraceWriter so the phone LLM service
            # emits trace v2 events. Same pattern as intent/agent_executor.py:3973.
            phone_task_trace = None
            try:
                from intent.task_trace import TaskTraceWriter

                phone_task_trace = TaskTraceWriter.for_task(
                    user_id=record.user_id,
                    task_id=record.call_id,
                    started_at=(record.started_at.isoformat() if record.started_at else None),
                )
                record._task_trace = phone_task_trace
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("CallManager: failed to construct phone TaskTraceWriter: %s", exc)

            phone_latency_trace = PhoneLatencyTraceRecorder(
                PhoneLatencyTraceWriter.for_call(
                    call_id=record.call_id,
                    user_id=record.user_id,
                    started_at=record.started_at or datetime.now(tz=UTC),
                ),
                source="telnyx_call_manager",
            )
            record._phone_latency_trace = phone_latency_trace
            phone_latency_trace.start(
                runtime="call_manager",
                mode=self.config.mode,
                llm_model=self.config.llm_model,
                tts_provider=self.config.tts_provider,
            )

            # Use the Responses API (not Chat Completions) so the phone agent
            # runs with reasoning_effort enabled. Chat Completions on
            # gpt-5.4-mini rejects reasoning_effort when tools are present,
            # which forces reasoning_tokens=0. The Responses API accepts
            # reasoning + tools together. Verified via trace v2 usage chunks
            # on 2026-05-06: switching from Chat Completions to Responses
            # eliminated the EV2 premature end_call pattern (5/20 -> 0/5).
            #
            # Auth path: prefer the user's Codex/ChatGPT-Plus subscription via
            # codex-auth's AsyncCodexTransport (zero per-token cost, same
            # Responses API surface). This matches services/llm/factory.py's
            # _create_codex_provider for the desktop agent. Fall back to the
            # config's direct OpenAI api_key when Codex isn't available so
            # production paths that haven't run `codex login` keep working.
            phone_llm_openai_client = _maybe_build_phone_codex_client(self.config)
            llm = TracedOpenAIResponsesLLMService(
                api_key=self.config.openai_api_key,
                settings=OpenAIResponsesLLMService.Settings(
                    model=self.config.llm_model,
                    system_instruction=system_prompt,
                    # Phone is real-time voice: use the canonical phone reasoning effort
                    # from DEFAULT_PHONE_REASONING_EFFORT (currently "low"). The rationale,
                    # including why "low" is NOT free and what it actually buys, lives on
                    # that constant in config/defaults.py — read it there rather than
                    # re-deriving it, and do not repeat the retired 2026-06-17 "no latency
                    # cost" figures. NEVER hard-code a heavier effort here — high reasoning
                    # adds a multi-second thinking pass to EVERY turn (the 2026-06-14
                    # conversational-latency regression). Gate: phone-reasoning-effort-low-latency.
                    # NO reasoning.summary here. PHONE-LATENCY-01 removed it on
                    # 2026-06-29 from traced_openai_responses_llm_service; it came back
                    # through this `extra=` door, which the ratchet did not inspect.
                    # The summary is a SECOND model output stream per turn, generated
                    # before the answer text and used only for trace logging, so the
                    # pipeline cannot start speaking until it finishes.
                    extra={
                        "reasoning": {
                            "effort": defaults.resolve_reasoning_effort(
                                defaults.DEFAULT_PHONE_REASONING_EFFORT,
                                self.config.llm_model,
                            ),
                        }
                    },
                ),
                task_trace_writer=phone_task_trace,
                payment_segment_controller=payment_sensitive_segment,
                phone_latency_recorder=phone_latency_trace,
                openai_client=phone_llm_openai_client,
                phone_volatile_context_builder=phone_volatile_context_builder,
            )
            if getattr(llm, "_client", None) is not None:
                llm._client = _wrap_openai_client_for_phone_spend_accounting(
                    llm._client,
                    user_id=record.user_id,
                )
            record._llm_service = llm
            voicemail_classifier_llm = _create_phone_voicemail_classifier_llm(
                self.config,
                openai_client=phone_llm_openai_client,
                user_id=record.user_id,
                task_trace_writer=phone_task_trace,
            )

            # PHONE-14: capture actual prompt/completion tokens from
            # Pipecat's LLM metrics. Pipecat 0.0.106 routes usage via a
            # MetricsFrame on the pipeline bus — the event_handler API
            # silently discards unknown names (logger.warning only), so
            # we insert a CostMetricsCollector FrameProcessor between the
            # LLM and downstream processors below.
            from telephony.cost_tracker import (
                CostMetricsCollector,
                CostTracker,
                TTSUsageCollector,
            )

            cost_tracker = CostTracker(
                call_id=record.call_id,
                llm_model=self.config.llm_model,
                phone_number=record.phone_number,
                tts_provider=self.config.tts_provider,
            )
            cost_metrics_collector = CostMetricsCollector(cost_tracker)
            tts_usage_collector = TTSUsageCollector(cost_tracker)
            record._cost_tracker = cost_tracker

            # --- Hold mode handler (mutes STT during business hold music) ---
            import telnyx

            telnyx_client = telnyx.AsyncTelnyx(api_key=self.config.api_key)
            from telephony.answer_settle import AnswerSettleObserver
            from telephony.call_tools import (
                make_consult_user_handler,
                make_end_call_handler,
                make_save_call_result_handler,
                press_button_handler,
            )
            from telephony.end_call_gate import EndCallGateProcessor
            from telephony.end_call_hangup import EndCallHangupAfterOutputProcessor
            from telephony.hold_handler import HoldModeHandler
            from telephony.language_handler import LanguageHandler
            from telephony.phone_playout_mark import PlayoutEndMarkProcessor
            from telephony.transcription_observer import (
                TranscriptFrameCollector,
                TranscriptionObserver,
            )

            phone_tool_runtime = await self._ensure_phone_tool_runtime(record.user_id)
            phone_provider_desktop_tools, phone_deferred_tool_pool = _phone_provider_desktop_tools_from_runtime(
                phone_tool_runtime,
                user_id=record.user_id,
            )
            if phone_deferred_tool_pool is not None and phone_tool_runtime is not None:
                logger.info(
                    "Phone provider tool surface deferred: visible=%d deferred=%d full=%d",
                    len(phone_provider_desktop_tools),
                    len(getattr(phone_deferred_tool_pool, "deferred_refs", []) or []),
                    len(phone_tool_runtime.visible_tools),
                )
            phone_tool_surface = build_phone_llm_tool_surface(
                phone_provider_desktop_tools,
                phone_mode=self.config.mode,
            )
            # Set tools on the LLM context so the LLM knows about them.
            # The phone surface starts with the reduced provider-visible
            # deferred surface; ToolSearch can expand it mid-call below.
            tools_schema = phone_tool_surface.tools_schema
            logger.info(
                "Phone LLM tool surface: desktop=%d phone_controls=%d total=%d",
                len(phone_tool_surface.desktop_tool_names),
                len(phone_tool_surface.phone_tool_names),
                len(phone_tool_surface.openai_tools),
            )
            context = LLMContext(tools=tools_schema)

            hold_handler = HoldModeHandler(llm=llm)
            record._hold_handler = hold_handler
            llm.register_function(
                "enter_hold_mode",
                _guard_phone_function_handler("enter_hold_mode", hold_handler.enter_hold, record),
                cancel_on_interruption=False,
            )
            llm.register_function(
                "press_button",
                _guard_phone_function_handler("press_button", press_button_handler, record),
                cancel_on_interruption=False,
            )
            llm.register_function(
                "consult_user",
                _guard_phone_function_handler("consult_user", make_consult_user_handler(record), record),
                cancel_on_interruption=False,
                # The pipeline timeout MUST exceed the handler's worst-case
                # internal consult wait (derived from the same constants in
                # call_tools). Without this override, pipecat's 10s default
                # killed the consult while it genuinely waited for the user,
                # discarded the late answer, and the LLM never re-ran — dead
                # air until hangup (capstone call 67105503, 2026-07-02).
                timeout_secs=consult_user_pipeline_timeout_secs(),
            )
            llm.register_function(
                "save_call_result",
                _guard_phone_function_handler("save_call_result", make_save_call_result_handler(record), record),
                cancel_on_interruption=False,
            )
            llm.register_function(
                "end_call",
                _guard_phone_function_handler(
                    "end_call",
                    make_end_call_handler(
                        telnyx_client=telnyx_client,
                        call_control_id="",
                        call_record=record,
                        hangup_after_output_drain=True,
                    ),
                    record,
                ),
                cancel_on_interruption=False,
            )

            if phone_tool_runtime is not None:
                from intent.agent_executor import AgentExecutor
                from services.conversation.session_identity import make_user_id

                request_tool_surface = None
                if hasattr(phone_tool_runtime.hub, "build_tool_surface"):
                    try:
                        request_tool_surface = phone_tool_runtime.hub.build_tool_surface(
                            tier="symphony",
                            interactive=True,
                            provider_native=phone_provider_desktop_tools,
                            step_log_visible=phone_tool_runtime.visible_tools,
                        )
                    except Exception:
                        logger.debug("Phone request tool-surface snapshot unavailable")

                phone_tool_executor = AgentExecutor(
                    llm_caller=llm,
                    approval_manager=phone_tool_runtime.approval_manager,
                    mcp_hub=phone_tool_runtime.hub,
                    channel=record.issuer_channel,
                    total_timeout=_PHONE_MCP_TOOL_TIMEOUT_SECS,
                    tool_timeout=_PHONE_MCP_TOOL_TIMEOUT_SECS,
                    user_id=make_user_id(record.user_id),
                    session_id="phone:%s" % record.call_id,
                    native_tools=phone_tool_runtime.visible_tools,
                    tool_surface=request_tool_surface,
                )
                if phone_deferred_tool_pool is not None:
                    phone_tool_executor._request_deferred_tool_pool = phone_deferred_tool_pool
                phone_mcp_tool_handler = self._make_phone_mcp_tool_handler(
                    executor=phone_tool_executor,
                    mcp_name_by_llm_name=phone_tool_surface.mcp_name_by_llm_name,
                    call_record=record,
                    deferred_tool_pool=phone_deferred_tool_pool,
                    provider_desktop_tools=phone_provider_desktop_tools,
                    full_mcp_tools=list(getattr(phone_tool_runtime, "visible_tools", []) or []),
                    llm_context=context,
                    llm=llm,
                    phone_mode=self.config.mode,
                )
                registered_desktop_tools = 0
                for llm_tool_name in phone_tool_surface.mcp_name_by_llm_name:
                    if llm_tool_name in _PHONE_CONTROL_TOOL_NAMES:
                        continue
                    llm.register_function(
                        llm_tool_name,
                        _guard_phone_function_handler(llm_tool_name, phone_mcp_tool_handler, record),
                        cancel_on_interruption=False,
                        timeout_secs=_PHONE_MCP_TOOL_PIPELINE_TIMEOUT_SECS,
                    )
                    registered_desktop_tools += 1
                logger.info(
                    "Phone registered %d desktop MCP tool handlers",
                    registered_desktop_tools,
                )

            language_handler = LanguageHandler(llm=llm, tts=tts)
            voicemail_handler = PipecatVoicemailDetectionHandler(
                llm=llm,
                classifier_llm=voicemail_classifier_llm,
                call_record=record,
            )
            record._voicemail_handler = voicemail_handler

            ai_identity_watchdog = _make_ai_identity_watchdog(
                record,
                enabled=phone_ai_identity_enforcement,
                proactive_disclosure=effective_announce_ai,
            )
            transcription_observer = TranscriptionObserver(
                hold_handler=hold_handler,
                language_handler=language_handler,
                on_recipient_transcript=(
                    ai_identity_watchdog.observe_recipient_transcript if ai_identity_watchdog is not None else None
                ),
            )
            user_transcript_collector = TranscriptFrameCollector(
                transcript_collector,
                capture_assistant=False,
            )
            _phone_text_gate_executor: Any = locals().get("phone_tool_executor", None)

            async def _on_assistant_text_complete(text: str) -> None:
                if _phone_text_gate_executor is not None:
                    await CallManager._maybe_start_phone_payment_confirmation_from_text(
                        executor=_phone_text_gate_executor,
                        assistant_text=text,
                    )
                # PHONE-15 path-drop recovery: this completed spoken turn is the
                # closing line the guard required after a refused text-empty
                # end_call. Complete the hangup now — the EndCallHangupFrame trails
                # this turn's TTS media, so the goodbye is heard, THEN the line
                # drops. No-op unless an end_call was actually latched.
                from telephony.call_tools import fire_latched_end_call_if_pending

                llm_service = getattr(record, "_llm_service", None)
                await fire_latched_end_call_if_pending(record, push_frame=getattr(llm_service, "push_frame", None))

            assistant_transcript_collector = TranscriptFrameCollector(
                transcript_collector,
                capture_user=False,
                on_assistant_complete=_on_assistant_text_complete,
            )
            payment_gate_text_filter = None
            if _phone_text_gate_executor is not None:
                from telephony.payment_gate_text_filter import (
                    PhonePaymentGateTextFilter,
                )

                payment_gate_text_filter = PhonePaymentGateTextFilter(on_payment_gate=_on_assistant_text_complete)
            disclosure_watchdog = _make_disclosure_watchdog(record)

            # Context + aggregators — VAD on the user aggregator drives
            # turn detection: when user stops speaking, aggregator flushes
            # accumulated transcription to LLM via LLMRunFrame.
            #
            # Phone transport audio has a tighter acoustic path than room mics.
            # PHONE_VAD_SILENCE_SECS (0.2, the Smart-Turn design point) is the
            # short post-speech silence Smart Turn recommends; Smart Turn itself
            # owns the semantic end-of-turn decision, so a longer raw window would
            # only add fixed dead time before it runs.
            from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
            from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (
                LocalSmartTurnAnalyzerV3,
            )
            from pipecat.processors.aggregators.dtmf_aggregator import DTMFAggregator
            from pipecat.turns.user_turn_strategies import UserTurnStrategies

            from telephony.phone_self_echo_guard import (
                BotSpeechEchoReference,
                BotTurnFloorState,
                SelfEchoGuardedMinWordsUserTurnStartStrategy,
            )
            from telephony.phone_turn_stop import build_phone_user_turn_stop_strategies

            smart_turn = LocalSmartTurnAnalyzerV3(
                params=SmartTurnParams(stop_secs=PHONE_VAD_SILENCE_SECS),
            )
            # LESS-YIELDING TURN-TAKING (2026-06-24). On a real call Viola was
            # fully interruptible: the default start strategies
            # (``[VADUserTurnStartStrategy, TranscriptionUserTurnStartStrategy]``)
            # flush her pending turn on ANY recipient sound — a single backchannel
            # ("yeah", "uh-huh", "mm-hm") cancels her mid-sentence, so she never
            # delivers a clean turn (live rung-3 050dbce8: 3x
            # turn_latency_incomplete reason=new_user_turn, 0 delivered turns).
            #
            # The Pipecat-native fix (NOT a hand-rolled classifier/shield) is the
            # word-count turn-start strategy. ``MinWordsUserTurnStartStrategy`` is
            # asymmetric BY DESIGN (min_words_user_turn_start_strategy.py): while
            # Viola is speaking it requires >= PHONE_INTERRUPTION_MIN_WORDS words to
            # start the recipient's turn (so a short backchannel does NOT interrupt),
            # but when Viola is NOT speaking it falls back to min_words=1, so normal
            # back-and-forth keeps its single-word latency — it only gates
            # *interruptions*, never ordinary replies. It must be the SOLE start
            # strategy: VADUserTurnStartStrategy would trigger a turn on any sound
            # and bypass the gate, and TranscriptionUserTurnStartStrategy is exactly
            # the min_words=1 behavior MinWords already subsumes when Viola is idle.
            #
            # PHONE_INTERRUPTION_MIN_WORDS: 1-2 word backchannels ("yeah", "uh huh",
            # "okay sure", "mm hmm") do not flush her; a genuine multi-word
            # interruption ("stop, wait, I have a question" / "sorry, can you repeat
            # that") clears the threshold and still cuts her off. Per founder:
            # erring slightly toward Viola completing her turn is acceptable (the
            # business accommodates the customer), but she MUST still yield to a real
            # interruption. use_interim=True lets a long interruption trigger off its
            # partial transcript so the yield is fast. Smart-Turn/VAD still owns
            # turn-STOP (when the recipient has finished); this changes only
            # turn-START (whether the recipient's speech counts as an interruption).
            # SELF-ECHO GUARD (2026-07-02, capstone 49c7106f). The inbound cloud
            # phone leg has no acoustic echo canceller: Viola's own opening TTS
            # plays out the recipient's handset speaker, their mic picks it up, and
            # Telnyx returns it on the inbound track. At PHONE_VAD_CONFIDENCE=0.3 the
            # echo trips VAD and Whisper transcribes Viola's own multi-word opening
            # as >= PHONE_INTERRUPTION_MIN_WORDS "recipient" words -> the plain
            # MinWordsUserTurnStartStrategy starts a user turn and cancels her
            # opening ("breaking up while I was silent"). SelfEchoGuarded... drops an
            # inbound transcript that is substantially Viola's OWN recently-spoken
            # words (ordered-bigram match against the live TTS text stream fed by the
            # bot-speech echo tap below), while leaving every genuine interruption
            # untouched -- so real barge-in still cuts her off. This is echo
            # cancellation at the turn layer, not model boxing.
            #
            # GENERATION-WINDOW FLOOR (call 6a632a0b). The base MinWords flag
            # ``_bot_speaking`` covers only BotStarted..BotStopped (audio actively
            # playing), so during the *generation* window (STT-done -> first-audio) it
            # falls back to min_words=1 and a 2-word backchannel ("can you?") flushed
            # Viola's pending turn (2x turn_latency_incomplete reason=new_user_turn).
            # BotTurnFloorState extends "bot has the floor" to that window: set on the
            # aggregator's on_user_turn_stopped (recipient yields, LLM run begins),
            # cleared on bot start/stop speaking and on a new user turn. The strategy
            # ORs it into its effective _bot_speaking so the SAME min_words gate applies
            # during generation. Driven by Pipecat's own turn lifecycle, not a shield.
            bot_speech_echo_reference = BotSpeechEchoReference()
            record._bot_speech_echo_reference = bot_speech_echo_reference
            bot_turn_floor_state = BotTurnFloorState()
            record._bot_turn_floor_state = bot_turn_floor_state
            turn_strategies = UserTurnStrategies(
                start=[
                    SelfEchoGuardedMinWordsUserTurnStartStrategy(
                        min_words=PHONE_INTERRUPTION_MIN_WORDS,
                        echo_reference=bot_speech_echo_reference,
                        floor_state=bot_turn_floor_state,
                        # Observe-only: barge-in candidates (with suppress/pass
                        # disposition) + bot speaking start/stop land in the shared
                        # per-call latency trace (ts+call_id auto-stamped). None when
                        # tracing is off; never affects the guard's disposition.
                        latency_trace=phone_latency_trace,
                        use_interim=True,
                    ),
                ],
                # SemanticEndOfTurnStopStrategy, not the stock pipecat one: because
                # MinWords above is the SOLE start strategy, a recipient turn STARTS on
                # their finalized transcript, and starting a turn resets every stop
                # strategy -- discarding the Smart-Turn verdict computed moments earlier
                # and dropping the stop strategy onto a fixed 800ms timer on EVERY turn.
                # The subclass carries the verdict across that reset. Full mechanism and
                # the rejected alternatives: telephony/phone_turn_stop.py.
                stop=build_phone_user_turn_stop_strategies(
                    turn_analyzer=smart_turn,
                    # Observe-only: records whether Smart-Turn called each turn complete
                    # and whether that shortened it, so the fast path's real fire rate is
                    # readable from a live call instead of unknowable.
                    latency_trace=phone_latency_trace,
                ),
            )

            user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
                context,
                user_params=LLMUserAggregatorParams(
                    vad_analyzer=CalibratingSileroVADAnalyzer(
                        params=VADParams(
                            confidence=PHONE_VAD_CONFIDENCE,
                            min_volume=PHONE_VAD_MIN_VOLUME,
                            start_secs=PHONE_VAD_START_SECS,
                            stop_secs=PHONE_VAD_SILENCE_SECS,
                        ),
                        on_sample=(phone_latency_trace.record_vad_sample if phone_latency_trace is not None else None),
                    ),
                    user_turn_strategies=turn_strategies,
                ),
            )

            # Drive the generation-window floor from Pipecat's own turn lifecycle: the
            # recipient yielding (turn stopped) opens Viola's generation window; a new
            # user turn starting (ordinary reply, or a genuine multi-word interruption
            # that cleared the gate) closes it. Bot start/stop speaking is handled inside
            # the strategy from the bot-speaking frames it already sees.
            @user_aggregator.event_handler("on_user_turn_stopped")
            async def _on_user_turn_stopped_floor(_agg: Any, _strategy: Any, _message: Any) -> None:
                bot_turn_floor_state.note_generation_started()

            @user_aggregator.event_handler("on_user_turn_started")
            async def _on_user_turn_started_floor(_agg: Any, _strategy: Any) -> None:
                bot_turn_floor_state.note_generation_ended()

            voicemail_handler.set_context_frame_target(user_aggregator)

            # Assemble the Pipecat pipeline (official Telnyx pattern)
            # transcription_observer and user_transcript_collector sit between
            # stt and user_aggregator: one routes call-control side effects,
            # the other persists user turns before context aggregation.
            # cost_metrics_collector sits immediately after llm so every
            # MetricsFrame is observed before tts consumes it (PHONE-14).
            # tts_usage_collector sits immediately before tts so only text that
            # survives payment/identity/disclosure filters is metered for paid TTS.
            # payment_gate_text_filter sits before transcript/tts so
            # PAYMENT_GATE text-prefix turns never reach Kokoro.
            # ai_identity_watchdog sits before disclosure/transcript/tts so
            # human claims or missed reactive AI identity answers never reach Kokoro.
            # disclosure_watchdog keeps recording/transcript disclosure fail-closed
            # while the model owns the first spoken opening.
            # assistant_transcript_collector sits after llm metrics capture and
            # before tts so outgoing Viola text is persisted.
            inbound_tee_processor = inbound_tee.create_processor()
            outbound_tee_processor = outbound_tee.create_processor()
            answer_settle_observer = AnswerSettleObserver()
            # answer_settle_observer sits DOWNSTREAM of `stt` — its designed position
            # (telephony/answer_settle.py: "right after STT"), restored 2026-08-06
            # (#4796, closing the #2587 defect). Pipecat STT services push
            # TranscriptionFrame DOWNSTREAM only, so from the old slot immediately
            # BEFORE `stt` the observer saw the audio and the aggregator's
            # upstream-broadcast UserStarted/StoppedSpeakingFrame — enough for
            # turn-following to look correct — but never the recipient's text. What
            # that cost, measured (CL-20260802-aca1): `_recipient_transcription_event`
            # was unreachable, so wait_for_recipient_opening burned the whole
            # _OPENING_TRANSCRIPTION_GRACE_SECS to timeout immediately in front of
            # Viola's FIRST spoken word on every single call (2.000s upstream of STT
            # vs 0.000s downstream on a real pipecat pipeline; the grace had been cut
            # to 0.4s to bound a ceiling that could never buy text), and
            # `recipient_opening_text` was always empty, so classify_opening always
            # fell back to scraping record.transcript instead of the race-free capture
            # its call site documents as preferred.
            #
            # The move could not land alone, and this is the ordering that mattered:
            # resolving answer-settle the instant STT lands puts the voicemail decision
            # AHEAD of the user aggregator's own handling of the very same
            # transcription, so ONE recipient utterance offers TWO triggers — the
            # classifier's published voicemail context and the aggregator's
            # turn-completion context. _VoicemailResponseGate admitted both, because
            # the only signal it had (`voicemail_message_delivered`) does not flip
            # until the generation's text reaches the transcript collector, which is
            # later than the second context arrives — so Viola generated her voicemail
            # message twice (tests/integration/telephony/test_loopback_phone_call_
            # session.py::test_loopback_voicemail_detector_leaves_one_message_then_
            # blocks_carrier_prompts, 3 == 2 at either grace value, so the trigger was
            # the observer position and never the grace). The prerequisite ships with
            # this move: the gate now has an admit-time one-message-at-a-time latch and
            # HOLDS a context that arrives mid-generation, resolved by the transcript
            # collector telling it whether that generation actually spoke
            # (note_voicemail_message_delivered -> drop the held repeat;
            # note_voicemail_generation_undelivered -> release it, so an interrupted
            # message still gets out — the 857c52b2 requirement).
            #
            # Keep this slot and the loopback rig's on the SAME side of `stt`: a rig on
            # the other side resolves answer-settle differently from a real call and
            # stops being an oracle for opening latency or voicemail
            # (scripts/check_phone_call_quality_guards.py pins both).
            pipeline_processors: list[Any] = [
                transport.input(),
                DTMFAggregator(),
                inbound_tee_processor,
                stt,
                answer_settle_observer,
            ]
            # Streaming STT sits immediately after batch whisper: both receive the
            # same InputAudioRawFrame (whisper passes audio through), whisper still
            # emits the authoritative final TranscriptionFrame, and the streaming
            # service emits InterimTranscriptionFrames that flow on to the user
            # aggregator's turn strategies (use_interim=True) for barge-in. None
            # when streaming is off/unavailable — pipeline is unchanged from today.
            if streaming_stt is not None:
                pipeline_processors.append(streaming_stt)
            if phone_latency_trace is not None:
                pipeline_processors.append(PhoneLatencyTraceProcessor(phone_latency_trace, stage="inbound"))
            end_call_gate = EndCallGateProcessor(record)
            pipeline_processors.extend(
                [
                    transcription_observer,
                    user_transcript_collector,
                    # One-shot voicemail (2026-06-27): no persistent parallel classifier
                    # branch in the pipeline. Its second user aggregator held every turn
                    # ~5.6s. The opening greeting is classified ONCE at answer_settle (see
                    # queue_outbound_opening_after_answer_settle -> classify_opening). The
                    # response_gate below still buffers ONLY the opening until that one-shot
                    # decision releases it.
                    end_call_gate,
                    user_aggregator,
                    voicemail_handler.response_gate(),
                    # PHONE-LATENCY: pre-LLM tap. Sits between the user aggregator/
                    # response-gate and the LLM so the aggregated context frame is
                    # observed the instant it is handed toward the model. Decomposes
                    # the previously-invisible STT-done -> LLM-request window into
                    # post_transcript_to_context_ms (this point) vs
                    # post_transcript_to_llm_dispatch_ms (_process_context entry).
                    *(
                        [PhoneLatencyTraceProcessor(phone_latency_trace, stage="llm_inbound")]
                        if phone_latency_trace is not None
                        else []
                    ),
                    # DE-BANDAID 2026-06-24: removed the hand-rolled first-opening
                    # interruption shield (born 9aea786a 2026-06-20). It suppressed Pipecat's
                    # native interruption handling and, on its 8s fallback release, let the
                    # opening be flushed -> empty turns/freeze. Native Smart-Turn/VAD (the
                    # 2026-06-18 db18e514 baseline) handles turn-taking; Viola leads, can be
                    # interrupted naturally, and is no longer locked to one shielded attempt.
                    llm,
                ]
            )
            # DE-BANDAID 2026-06-24: native opening trigger only. A short listen
            # window after media-connect (queue_outbound_opening_after_answer_settle
            # below) queues exactly one LLMRunFrame; if the recipient interrupts,
            # native interruption handling cancels the turn and the user aggregator
            # re-fires the opening when their turn completes — no guard processors.
            if phone_latency_trace is not None:
                pipeline_processors.append(PhoneLatencyTraceProcessor(phone_latency_trace, stage="llm"))
            pipeline_processors.append(cost_metrics_collector)
            if payment_gate_text_filter is not None:
                pipeline_processors.append(payment_gate_text_filter)
            if ai_identity_watchdog is not None:
                pipeline_processors.append(ai_identity_watchdog)
            if disclosure_watchdog is not None:
                pipeline_processors.append(disclosure_watchdog)
            disclosure_playback_marker = _make_disclosure_playback_marker(record, disclosure_watchdog)
            # Self-echo tap: feed Viola's live TTS text (post payment/identity/
            # disclosure filtering) into the echo reference so the guarded turn-start
            # strategy upstream can recognise her own voice returning off the
            # recipient's handset and NOT treat it as a barge-in (capstone 49c7106f).
            from telephony.phone_self_echo_guard import create_bot_speech_echo_tap

            bot_speech_echo_tap = create_bot_speech_echo_tap(bot_speech_echo_reference)
            pipeline_processors.extend(
                [
                    assistant_transcript_collector,
                    tts_usage_collector,
                    tts,
                    bot_speech_echo_tap,
                    # (one-shot voicemail: the persistent TTS gate is gone; the opening is
                    # already classified before Viola speaks, so no TTS buffering is needed)
                ]
            )
            if phone_latency_trace is not None:
                pipeline_processors.append(PhoneLatencyTraceProcessor(phone_latency_trace, stage="outbound"))
            end_call_hangup = EndCallHangupAfterOutputProcessor(
                telnyx_client=telnyx_client,
                call_control_id_getter=lambda: (
                    str(getattr(record, "telnyx_call_control_id", "") or "")
                    or str(getattr(transport, "call_control_id", "") or "")
                ),
                call_record=record,
                transport=transport,
            )
            # PLAYOUT-END ANCHOR (observe-only). Placed AFTER transport.output() so it
            # sees the downstream BotStoppedSpeakingFrame only once base_output has
            # drained the turn's last audio to the Telnyx WebSocket. It then taps the
            # existing Telnyx mark round-trip (same one end_call_hangup uses) with a
            # per-turn mark and stamps ``bot_playout_end`` into the shared latency trace
            # when Telnyx echoes it back — a played-to-line anchor strictly downstream of
            # the guard's queued-to-wire ``bot_speaking_stop``. Never gates/delays audio
            # or the guard; None-safe (only wired when tracing is on). Loopback transports
            # have no mark round-trip, so it self-skips there.
            playout_end_mark = (
                PlayoutEndMarkProcessor(
                    transport=transport,
                    recorder=phone_latency_trace,
                    call_id=str(getattr(record, "call_id", "") or ""),
                )
                if phone_latency_trace is not None
                else None
            )
            pipeline_processors.extend(
                [
                    outbound_tee_processor,
                    *([disclosure_playback_marker] if disclosure_playback_marker is not None else []),
                    transport.output(),
                    *([playout_end_mark] if playout_end_mark is not None else []),
                    end_call_hangup,
                    assistant_aggregator,
                ]
            )
            pipeline = Pipeline(pipeline_processors)
            first_bot_latency_observer = UserBotLatencyObserver()

            @first_bot_latency_observer.event_handler("on_first_bot_speech_latency")
            async def _on_first_bot_speech_latency(_observer: Any, latency: float) -> None:
                logger.info(
                    "Call %s: first bot speech latency %.3fs",
                    record.call_id,
                    float(latency),
                )
                if phone_latency_trace is not None:
                    phone_latency_trace.record_user_bot_latency(float(latency))
                # Opening turn has spoken — real call audio now sustains the
                # remote worker's warmth, so stop the keep-alive pinger. The
                # first-need of the remote worker has arrived; keeping the loop
                # running would only add redundant warmup inferences.
                await _cancel_remote_warm("opening turn spoke")

            params = PipelineParams(
                allow_interruptions=True,
                enable_metrics=True,
                observers=[first_bot_latency_observer],
                # The StartFrame propagated downstream carries these rates, and
                # SegmentedSTTService.start() reads its sample_rate from
                # StartFrame.audio_in_sample_rate. PipelineParams defaults to
                # 16000/24000 (pipecat task.py), but inbound telephone audio is
                # native 8 kHz PCMU. If the STT believes the inbound rate is
                # 16000 it skips the 8k->16k upsample in phone_pcm_to_whisper_float
                # and hands 8 kHz samples to whisper as if they were 16 kHz —
                # pitch-halved, time-stretched, GARBLED recipient transcripts
                # ("we'll hold it under J until 6 PM" -> "we're hoping to take a
                # quick team"). The inbound recording escapes this because it is
                # saved at the hardcoded PHONE_INBOUND_SAMPLE_RATE, not the STT's
                # belief. Pin both StartFrame rates to the transport's real rates
                # so every downstream consumer (STT especially) resamples
                # correctly. See telephony/phone_stt_options.py + the d391869f
                # real-cellular STT garble fix this completes.
                audio_in_sample_rate=PHONE_INBOUND_SAMPLE_RATE,
                audio_out_sample_rate=16000,
            )

            runner = PipelineRunner(handle_sigint=False)
            # PHONE-15 (revised): idle_timeout_secs must clear the ENTIRE
            # pre-first-audio window (warm gate + media connect + opening TTS),
            # which all run on the idle monitor's single clock with no reset,
            # then still bound post-connect mutual silence. It is DERIVED from
            # REMOTE_WARM_DIAL_CEILING_S + PHONE_MEDIA_CONNECT_TIMEOUT_S so it
            # can never again be <= the pre-dial gate window (which would cancel
            # a real cold-start call before it rings). Default cancel_on_idle_
            # timeout=True is kept: after real speech has started, this bounds a
            # genuinely dead live call. See the derivation block above; locked by
            # the phone_idle_timeout_covers_warm_gate ratchet.
            task = PipelineTask(pipeline, params=params, idle_timeout_secs=PHONE_PIPELINE_IDLE_TIMEOUT_S)
            record._pipeline_task = task

            # Start the pipeline (this starts the WS server)
            pipeline_coro = runner.run(task)
            pipeline_future = asyncio.ensure_future(pipeline_coro)

            # Wait for the WS server to actually bind (local mode only).
            # Cloud mode has no local WS server — the cloud bridge provides it.
            if self.config.mode != "cloud" and not await transport.wait_for_server_ready():
                raise RuntimeError("Local phone media listener did not start; no call was placed.")

            # --- Dial via Telnyx ---
            # Cloud mode dials the cloud media bridge. Local mode must dial the
            # stable Viola_app named tunnel (wss://phone.useviola.com); quick
            # tunnels are intentionally rejected before Telnyx sees them.
            stream_url = _resolve_stream_url_for_dial(self.config, transport.stream_url)
            if phone_latency_trace is not None:
                phone_latency_trace.record_media_path(mode=self.config.mode, stream_url=stream_url)

            # Acquire a phone number from the pool for this call
            acquired_number = self._number_pool.acquire()
            self._start_number_pool_heartbeat(record.call_id, acquired_number)
            logger.info(
                "Call %s: acquired outbound number %s from pool",
                record.call_id,
                _mask_phone_number(acquired_number),
            )

            dial_params: dict[str, Any] = {
                "connection_id": self.config.sip_connection_id,
                "from_": acquired_number,
                "to": record.phone_number,
                "stream_url": stream_url,
                "stream_codec": "PCMU",
                "stream_bidirectional_mode": "rtp",
                "stream_bidirectional_codec": "PCMU",
                "stream_bidirectional_sampling_rate": 8000,
                "stream_bidirectional_target_legs": "both",
                # Stream ONLY the recipient leg to us. `both_tracks` makes Telnyx
                # echo our own outbound TTS back as the `outbound` track, and the
                # pipecat TelnyxFrameSerializer deserializes every media event as an
                # InputAudioRawFrame without inspecting `media.track` -- so with
                # `both_tracks` Viola's own speech is fed into VAD, STT and the
                # inbound recording (the a8eb0e42 capstone recorded Viola's own order
                # as the recipient's audio). Viola's TTS still reaches the call via
                # `stream_bidirectional_mode: rtp`; this only controls what Telnyx
                # streams BACK to us, which must be the recipient leg alone.
                "stream_track": "inbound_track",
                "stream_establish_before_call_originate": True,
                "time_limit_secs": max_duration,
                "timeout_secs": 30,
                "command_id": "viola-dial-%s" % record.call_id,
                "client_state": base64.b64encode(record.call_id.encode("utf-8")).decode("ascii"),
            }

            if self.config.answering_machine_detection:
                dial_params["answering_machine_detection"] = "premium"
                if self.config.mode == "cloud" and self.config.cloud_url:
                    dial_params["webhook_url"] = "%s/webhooks/telnyx" % self.config.cloud_url.rstrip("/")

            if self.config.mode == "local" and self.config.public_webhook_url:
                dial_params["webhook_url"] = self.config.public_webhook_url

            logger.info(
                "Call %s: dialing %s via Telnyx (stream_url=%s, mode=%s)",
                record.call_id,
                _mask_phone_number(record.phone_number),
                _redact_stream_url(stream_url),
                self.config.mode,
            )

            # Gate the dial on the RETURNED remote-worker warm signal (not a
            # timer): wait up to the ceiling for a warmup inference to have
            # returned warm. If still cold at the ceiling, dial anyway — the
            # reworked remote_voice cooldown (a cold worker is retried + used once
            # warm within the call) and the keep-alive loop are the safety net.
            if remote_warm_task is not None:
                # Keep the "preparing to call" cue alive for the WHOLE warm wait
                # (up to the ceiling). A single call_started then a long silence
                # reads as frozen; re-emitting the SAME event (status still DIALING)
                # on a short cadence reads as working. Same event + existing status
                # + existing WS allowlist — no new channel. Idempotent on the
                # frontend: refreshes the live-call meta, never resets the elapsed
                # timer or re-opens the tab. Cancelled the instant the gate returns
                # (warm signal OR cap-fallthrough), before the dial proceeds; the
                # call_ended broadcast in finally clears the cue on every path.
                async def _refresh_preparing_cue() -> None:
                    refreshes = 0
                    while True:
                        await asyncio.sleep(REMOTE_WARM_PREPARING_REFRESH_S)
                        refreshes += 1
                        logger.info(
                            "Call %s: preparing-cue refresh #%d at t=%.3fs "
                            "(re-emitting call_started while dial gate open)",
                            record.call_id,
                            refreshes,
                            time.monotonic() - remote_warm_started,
                        )
                        await self._broadcast_call_ws_event("call_started", record)

                preparing_cue_task = asyncio.create_task(
                    _refresh_preparing_cue(),
                    name="phone-preparing-cue-%s" % record.call_id,
                )
                try:
                    await _await_remote_warm_gate(
                        record.call_id,
                        remote_warm_ready,
                        remote_warm_started,
                        ceiling_s=REMOTE_WARM_DIAL_CEILING_S,
                    )
                finally:
                    preparing_cue_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await preparing_cue_task

            try:
                dial_response = await telnyx_client.calls.dial(**dial_params)
            except Exception as exc:
                record.status = CallStatus.FAILED
                record.error = _describe_telnyx_dial_error(exc)
                logger.error("Call %s: %s", record.call_id, record.error)
                pipeline_future.cancel()
                with suppress(asyncio.CancelledError):
                    await pipeline_future
                return
            # Capture the leg id at DIAL time, before any media. The id lives at
            # dial_response.data.call_control_id; reading it off the response root
            # always yielded "" and left the record unmatchable by
            # get_record_by_call_control_id, so every carrier webhook for a call
            # that never connects media came back handled=False and the
            # #2796/#3385 ledger-correction safety net could not run (#3385).
            call_control_id = telnyx_dial_call_control_id(dial_response)
            if call_control_id:
                record.telnyx_call_control_id = call_control_id

            logger.info(
                "Telnyx call initiated: control_id=%s",
                call_control_id or "<awaiting media start>",
            )

            record.status = CallStatus.RINGING
            ring_warmup_task = asyncio.create_task(
                self._prewarm_call_runtime_during_ring(
                    record.call_id,
                    stt=stt,
                    llm=llm,
                    tts=tts,
                    llm_context=context,
                    voicemail_classifier=voicemail_classifier_llm,
                )
            )

            # Wait for Telnyx to connect its media stream
            try:
                connected = await transport.wait_for_connection(timeout=PHONE_MEDIA_CONNECT_TIMEOUT_S)
            except Exception:
                if ring_warmup_task is not None and not ring_warmup_task.done():
                    ring_warmup_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await ring_warmup_task
                raise
            if not connected:
                if ring_warmup_task is not None and not ring_warmup_task.done():
                    ring_warmup_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await ring_warmup_task
                record.status = CallStatus.NO_ANSWER
                record.error = "Telnyx media stream did not connect within %ds" % int(PHONE_MEDIA_CONNECT_TIMEOUT_S)
                logger.warning("Call %s: no media stream connection", record.call_id)
                pipeline_future.cancel()
                return
            if ring_warmup_task is not None and not ring_warmup_task.done():
                logger.debug(
                    "Call %s: ring warmup continuing after media connect",
                    record.call_id,
                )

            if not record.telnyx_call_control_id and transport.call_control_id:
                record.telnyx_call_control_id = transport.call_control_id

            record.status = CallStatus.ACTIVE
            record.started_at = datetime.now(tz=UTC)
            await self._broadcast_call_ws_event("call_started", record)
            disconnect_watchdog_task = asyncio.create_task(
                self._watch_telnyx_ws_disconnect(record, transport),
                name="phone-ws-disconnect-watchdog-%s" % record.call_id,
                context=contextvars.copy_context(),
            )

            logger.info("Call %s connected, pipeline running", record.call_id)
            # Listen-first native opening (db18e514): follow the recipient's first
            # turn to its semantic end (Smart-Turn / VAD) so Viola does not talk
            # over a live business greeting, then queue her one model-owned
            # opening. The lone timer is the silent-answer fallback so a silent
            # pickup never stalls the call.
            opening_queue_reason = await queue_outbound_opening_after_answer_settle(
                record,
                task,
                answer_settle_observer,
            )
            logger.info(
                "Call %s: model-driven outbound opening settle result=%s",
                record.call_id,
                opening_queue_reason,
            )

            # Run pipeline with timeout
            try:
                await asyncio.wait_for(pipeline_future, timeout=max_duration)
            except TimeoutError:
                logger.warning(
                    "Call %s hit max duration (%ds), ending",
                    record.call_id,
                    max_duration,
                )
                record.status = CallStatus.TIMEOUT
                # Hang up via Telnyx
                try:
                    active_call_control_id = record.telnyx_call_control_id or call_control_id
                    if active_call_control_id:
                        await telnyx_client.calls.actions.hangup(
                            call_control_id=active_call_control_id,
                        )
                    else:
                        logger.warning(
                            "Telnyx hangup skipped for %s: call_control_id unavailable",
                            record.call_id,
                        )
                except Exception:
                    logger.warning("Telnyx hangup on timeout failed for %s", record.call_id)

            # Frame processors persist the live transcript during the call. The
            # LLM context extraction remains only as a fallback for mocked or
            # older Pipecat paths that do not emit the expected text frames.
            if transcript_collector.entries:
                logger.info(
                    "Call %s: captured %d transcript entries from Pipecat frames",
                    record.call_id,
                    len(transcript_collector.entries),
                )
            elif context and hasattr(context, "messages"):
                logger.info(
                    "Call %s: no frame transcript entries; extracting fallback from %d LLM context messages",
                    record.call_id,
                    len(context.messages),
                )
                for msg in context.messages:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    try:
                        from intent.log_redaction import redact_card_data

                        content = redact_card_data(str(content))
                    except Exception:
                        content = str(content)
                    if role == "assistant" and content:
                        transcript_collector.add_assistant(content)
                        logger.debug(
                            "Call %s transcript [Viola]: %s",
                            record.call_id,
                            _log_text_preview(content),
                        )
                    elif role == "user" and content:
                        transcript_collector.add_user(content)
                        logger.debug(
                            "Call %s transcript [Them]: %s",
                            record.call_id,
                            _log_text_preview(content),
                        )
            else:
                logger.warning(
                    "Call %s: no LLM context messages available for transcript",
                    record.call_id,
                )

            record.transcript = transcript_collector.entries

            # #2796 / #2824: derive the honest terminal status at natural
            # pipeline end and only commit a derived COMPLETED once the
            # Telnyx leg's hangup is actually confirmed torn down. See
            # _resolve_natural_end_status for the full rationale.
            await _resolve_natural_end_status(record, self.config.api_key)

            # Estimate cost
            self._estimate_cost(record)

            # Generate post-call summary
            record.summary = await self._generate_summary(
                transcript_collector.to_text(),
                user_id=record.user_id,
            )

            # Post-call extraction and automation
            try:
                from telephony.post_call_actions import (
                    PostCallActionRunner,
                    extract_call_data,
                )

                if _is_cloud_llm_consented():
                    extraction = await extract_call_data(
                        transcript_collector.to_text(),
                        record.task,
                        self.config.openai_api_key,
                        user_id=record.user_id,
                    )
                    action_runner = PostCallActionRunner()
                    actions = await action_runner.run(extraction, record, record.user_id)
                    logger.info("Post-call actions: %s", actions)
                else:
                    logger.warning("Skipping post-call extraction/actions: cloud LLM consent not given")
            except Exception as exc:
                logger.warning("Post-call extraction/actions failed: %s", exc)
                # Non-fatal — call itself succeeded

            logger.info(
                "Call %s completed: %s (%.1fs, est. $%.4f)",
                record.call_id,
                record.status.value,
                record.duration_seconds,
                record.estimated_cost_usd,
            )

        except asyncio.CancelledError:
            if not _is_terminal_call_status(record.status):
                record.status = CallStatus.CANCELLED
            logger.info("Call %s was cancelled", record.call_id)

        except ImportError as exc:
            record.status = CallStatus.FAILED
            record.error = "Missing dependency: %s. Install with: pip install pipecat-ai telnyx" % exc
            logger.error("Phone call dependency missing: %s", exc)

        except Exception as exc:
            record.status = CallStatus.FAILED
            record.error = str(exc)
            logger.exception("Call %s failed", record.call_id)

        finally:
            # SAFETY CORE (payments): if the call ends while a payment segment is
            # still in flight, the card audio never reached the line. Settle the
            # segment as aborted so the transmit tool's waiter resolves at once
            # with the truth instead of waiting out its budget and reporting an
            # unconfirmed transmission. Without this the one honest outcome the
            # tool can prove -- "the call died before the card was spoken" --
            # would be indistinguishable from a slow pipeline.
            with suppress(Exception):
                if payment_sensitive_segment.active:
                    payment_sensitive_segment.abort_payment_segment(
                        payment_sensitive_segment.active_segment_id,
                        outcome=OUTCOME_ABORTED,
                    )

            # Covers preflight errors, user cancellation and carrier shutdown,
            # including failures before the normal runner wait is reached.
            if pipeline_future is not None and not pipeline_future.done():
                pipeline_future.cancel()
            if pipeline_future is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await pipeline_future

            # Stop the remote-worker warm keep-alive first (idempotent — the
            # opening-turn observer may already have cancelled it). Guarantees the
            # warm-state task never leaks past the call on any exit path.
            await _cancel_remote_warm("call cleanup (finally)")

            if disconnect_watchdog_task is not None and not disconnect_watchdog_task.done():
                disconnect_watchdog_task.cancel()
                with suppress(asyncio.CancelledError):
                    await disconnect_watchdog_task

            await self._cancel_billing_heartbeat_and_wait(record.call_id)
            await self._cancel_number_pool_heartbeat_and_wait(record.call_id)

            if _record_needs_telnyx_hangup(record):
                await _async_post_telnyx_hangup(
                    self.config.api_key,
                    str(record.telnyx_call_control_id or ""),
                )

            # Tie the owner-takeover conference leg's teardown to the primary
            # call's lifecycle. The leg is a separate outbound Telnyx call dialed
            # with end_conference_on_exit=False and bounded only by
            # time_limit_secs; if the primary call ends first, nothing else hangs
            # it up and it keeps billing until the server time limit (#2800).
            # Best-effort and idempotent -- a failure here must never break teardown.
            conference_leg_id = str(getattr(record, "_conference_leg_call_control_id", "") or "")
            if conference_leg_id and not record._conference_leg_hangup_dispatched:
                try:
                    if await _async_post_telnyx_hangup(self.config.api_key, conference_leg_id):
                        record._conference_leg_hangup_dispatched = True
                        logger.info(
                            "Call %s: conference owner-leg hangup dispatched on primary-call teardown",
                            record.call_id,
                        )
                except Exception:  # noqa: BLE001, RUF100 - best-effort billing hangup must never break teardown
                    logger.warning(
                        "Call %s: conference owner-leg teardown hangup failed",
                        record.call_id,
                        exc_info=True,
                    )

            # An earlier path may already have stamped an honest end (cancel_call).
            # Preserve it as a billing anchor before the teardown stamp lands on
            # top of it (#2589); ended_at itself stays the teardown time because
            # the stale-call reapers measure age from it.
            if record.ended_at is not None and record.local_end_at is None:
                record.local_end_at = record.ended_at
            record.ended_at = datetime.now(tz=UTC)
            if record.started_at:
                # #2589: bill the CARRIER leg, not the pipeline's lifetime. The
                # raw ``ended_at - started_at`` that used to live here charged
                # every normal call for the up-to-150s idle-timeout tail after
                # the recipient had already hung up, plus any pre-answer ring
                # time. ``_estimate_cost`` reads ``duration_seconds``, so fixing
                # the duration here fixes the money that follows from it.
                record.duration_seconds = _billable_window_seconds(record)
                self._estimate_cost(record)

            if phone_latency_trace is not None:
                with suppress(Exception):
                    phone_latency_trace.complete(
                        status=record.status.value,
                        duration_seconds=record.duration_seconds,
                    )

            # Record call end for billing (must use same user_id as record_call_start).
            # PHONE-05/06/11: ceil duration to >=1 minute for billed minutes and
            # apply a minimum cost on failed/no-answer/timeout so users can't
            # cancel out of an attempted call for free.
            billed_duration_seconds = _billed_duration_seconds(record)
            billed_cost_usd = _billed_cost_usd(record)

            try:
                billing = get_phone_billing()
                if effective_user_id:
                    await billing.record_call_end(
                        effective_user_id,
                        record.call_id,
                        billed_duration_seconds,
                        billed_cost_usd,
                        status=record.status.value,
                    )
            except Exception:
                logger.exception("Failed to record call end for billing")

            # Clean up hold handler (cancels any running timer task)
            if hold_handler is not None:
                try:
                    await hold_handler.cleanup()
                except Exception:
                    logger.warning("Hold handler cleanup failed")

            if ring_warmup_task is not None and not ring_warmup_task.done():
                ring_warmup_task.cancel()
                with suppress(asyncio.CancelledError):
                    await ring_warmup_task

            voicemail_handler = record._voicemail_handler
            if voicemail_handler is not None and hasattr(voicemail_handler, "cleanup"):
                try:
                    await voicemail_handler.cleanup()
                except Exception:
                    logger.warning("Voicemail handler cleanup failed")

            # Unregister cloud transport
            if self.config.mode == "cloud":
                try:
                    from telephony.cloud_routes import unregister_transport

                    unregister_transport(record.call_id)
                except Exception:
                    logger.debug("Transport unregister failed during call cleanup")

            # Release the acquired phone number back to the pool
            if acquired_number is not None:
                self._number_pool.release(acquired_number)
                logger.debug(
                    "Call %s: released number %s back to pool",
                    record.call_id,
                    _mask_phone_number(acquired_number),
                )

            # Anchor to the single terminal-status source of truth. A hardcoded
            # set here previously omitted CallStatus.VOICEMAIL, so a normal
            # voicemail call (Viola leaves a message and hangs up — the status
            # dispatch_telnyx_end_call_hangup assigns) skipped this whole block:
            # its recording was never flushed, its transcript/history never
            # persisted, the call_ended WS event/summary never broadcast, the
            # record leaked in _active_calls, and the queue pump never fired.
            # _is_terminal_call_status includes VOICEMAIL and is the same set the
            # rest of the module already trusts.
            if _is_terminal_call_status(record.status):
                await self._flush_call_recording(record)
                if _should_persist_call_transcript(record):
                    try:
                        _persist_call_transcript(record)
                        logger.info("Call %s: transcript persisted", record.call_id)
                    except Exception:
                        logger.exception("Call %s: failed to persist transcript", record.call_id)
                else:
                    logger.info("Call %s: transcript persistence skipped", record.call_id)
                try:
                    history_path = _persist_call_history_entry(record)
                    logger.info(
                        "Call %s: call history persisted to %s",
                        record.call_id,
                        history_path,
                    )
                except Exception:
                    logger.exception("Call %s: failed to persist call history", record.call_id)
                await self._cleanup_expired_call_artifacts()
                await self._broadcast_call_ws_event("call_ended", record, include_summary=True)
                self._active_calls.pop(record.call_id, None)
                await self._cancel_billing_heartbeat_and_wait(record.call_id)
                self._schedule_queue_pump()

            # Keep record accessible in _call_records but remove from active task tracking.
            self._call_tasks.pop(record.call_id, None)

    async def _cleanup_expired_call_artifacts(self, *, days: int = PHONE_CALL_RETENTION_DAYS) -> dict[str, int]:
        """Delete expired phone recordings, transcript JSON, history, and billing rows."""
        from telephony.call_history import delete_call_history, expired_call_ids

        expired = expired_call_ids(days)
        deleted = {"history": 0, "transcripts": 0, "recordings": 0, "billing_rows": 0}
        for call_id in expired:
            try:
                await self._recording_storage.delete(call_id)
                deleted["recordings"] += 1
            except Exception:
                logger.warning("Call %s: failed to delete expired recording artifacts", call_id)
            try:
                deleted["transcripts"] += _delete_persisted_call_transcripts(call_id)
            except OSError as exc:
                logger.warning(
                    "Call %s: failed to delete expired transcript JSON: %s",
                    call_id,
                    exc,
                )
            if delete_call_history(call_id):
                deleted["history"] += 1

        try:
            deleted["billing_rows"] = int(await get_phone_billing().cleanup_old_records(days))
        except Exception:
            logger.warning("Failed to clean up expired phone billing rows")

        if any(deleted.values()):
            logger.info("Phone retention cleanup deleted artifacts: %s", deleted)
        return deleted

    async def _flush_call_recording(self, record: CallRecord) -> None:
        if not record.recording_enabled:
            return
        if not record.disclosure_spoken or not record.recording_started_after_disclosure:
            logger.info(
                "Call %s: recording persistence skipped before disclosure",
                record.call_id,
            )
            return

        for direction, tee in (
            ("inbound", getattr(record, "_inbound_tee", None)),
            ("outbound", getattr(record, "_outbound_tee", None)),
        ):
            if tee is None:
                logger.warning(
                    "Call %s: %s recording tee missing at cleanup",
                    record.call_id,
                    direction,
                )
                continue

            try:
                audio_data = tee.flush(direction)
            except Exception as exc:
                logger.warning(
                    "Call %s: failed to flush %s recording: %s",
                    record.call_id,
                    direction,
                    exc,
                )
                continue

            if not audio_data:
                continue

            # Inbound runs at the native telephone rate (8 kHz); outbound (TTS)
            # runs at 16 kHz. Passing the right rate per direction keeps the
            # saved WAV from playing back at the wrong speed/pitch.
            recording_sample_rate = PHONE_INBOUND_SAMPLE_RATE if direction == "inbound" else 16000
            try:
                recording_uri = await self._recording_storage.save(
                    record.call_id,
                    direction,
                    audio_data,
                    sample_rate=recording_sample_rate,
                )
                if recording_uri:
                    record.recording_paths[direction] = recording_uri
            except Exception as exc:
                logger.warning(
                    "Call %s: failed to save %s recording: %s",
                    record.call_id,
                    direction,
                    exc,
                )

    async def _watch_telnyx_ws_disconnect(
        self,
        record: CallRecord,
        transport: Any,
        *,
        grace_seconds: float = _PHONE_WS_DISCONNECT_HANGUP_GRACE_SECS,
    ) -> None:
        """Force-hangup if Telnyx media ends mid-call and does not recover."""
        try:
            wait_for_disconnect = getattr(transport, "wait_for_media_disconnect", None)
            if wait_for_disconnect is None:
                wait_for_disconnect = transport.wait_for_unexpected_disconnect
            disconnected = await wait_for_disconnect()
            if not disconnected:
                return

            reason = str(getattr(transport, "media_disconnect_reason", "") or "websocket_closed")
            # #2589: the moment the media stream ended, captured BEFORE the
            # recovery grace sleep so the billable end is the real end and not
            # the end plus our own wait. Only committed to the record below, if
            # the stream does NOT come back.
            stream_ended_at = datetime.now(tz=UTC)
            effective_grace_seconds = 0.0 if reason == "telnyx_stop" else grace_seconds
            logger.warning(
                "Call %s: Telnyx media stream ended (%s); waiting %.1fs for recovery",
                record.call_id,
                reason,
                effective_grace_seconds,
            )
            if effective_grace_seconds > 0:
                await asyncio.sleep(effective_grace_seconds)

            if bool(getattr(transport, "is_connected", False)):
                logger.info(
                    "Call %s: Telnyx media stream recovered before watchdog hangup",
                    record.call_id,
                )
                return

            # The stream is really gone. This is honest proof the billable leg is
            # over (#2589) — the pipeline may sit here for the whole idle timeout
            # before its task resolves, and the user must not pay for that wait.
            # Earliest-wins: a later disconnect never extends the billed window.
            if record.media_stopped_at is None or stream_ended_at < record.media_stopped_at:
                record.media_stopped_at = stream_ended_at

            if record.status not in (
                CallStatus.DIALING,
                CallStatus.RINGING,
                CallStatus.ACTIVE,
            ):
                return

            # TERMINAL-STATUS OWNERSHIP (race fix): ``telnyx_stop`` means Telnyx
            # cleanly closed the media stream — a *normal* call end, not a failure.
            # On a normal end, the completion path in ``_run_call`` (pipeline_future
            # resolves -> persist transcript -> set COMPLETED) is the SINGLE
            # terminal-status authority. The watchdog must NOT race it: it neither
            # writes a terminal status nor calls ``end_call`` (which would
            # ``task.cancel()`` the still-running completion path mid-persist and
            # strand the call CANCELLED/ACTIVE instead of COMPLETED). The watchdog's
            # only remaining duty on a clean stop is the billing-leak guard:
            # dispatch the Telnyx hangup directly if it hasn't already happened.
            # FAILED is reserved for a genuinely ABNORMAL disconnect (e.g.
            # ``websocket_closed``) that did not recover within the grace window —
            # there the completion path may be wedged, so the watchdog stays the
            # authority and tears the call down as FAILED.
            if reason == "telnyx_stop":
                if _record_needs_telnyx_hangup(record):
                    try:
                        if await _async_post_telnyx_hangup(self.config.api_key, str(record.telnyx_call_control_id)):
                            record._telnyx_hangup_dispatched = True
                            logger.info(
                                "Call %s: watchdog dispatched Telnyx hangup on clean media "
                                "stop (terminal status owned by completion path)",
                                record.call_id,
                            )
                    except Exception:  # noqa: BLE001, RUF100 - best-effort billing hangup must never break teardown
                        logger.warning(
                            "Call %s: watchdog Telnyx hangup on clean media stop failed",
                            record.call_id,
                            exc_info=True,
                        )
                return

            record.status = CallStatus.FAILED
            record.error = "Telnyx media stream ended (%s); call audio path is unavailable." % reason
            await self.end_call(record.call_id, wait_for_cleanup=False, user_id=record.user_id)
            logger.warning(
                "Call %s: watchdog requested call teardown after media stream ended",
                record.call_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Call %s: Telnyx media disconnect watchdog failed", record.call_id)

    async def _prewarm_call_runtime_during_ring(
        self,
        call_id: str,
        *,
        stt: Any,
        llm: Any,
        tts: Any,
        llm_context: Any | None = None,
        voicemail_classifier: Any | None = None,
    ) -> None:
        """Best-effort warmup while Telnyx rings and no caller audio is flowing."""
        warmups: list[tuple[str, Any]] = [
            ("stt", self._prewarm_stt_silence(stt)),
            ("llm", self._prewarm_llm_ping(llm, llm_context=llm_context)),
            ("tts", self._prewarm_tts_first_audio(call_id, tts)),
        ]
        # The one-shot voicemail classifier is a SECOND LLM service, and its single
        # inference is serialized directly in front of Viola's first spoken word:
        # queue_outbound_opening_after_answer_settle awaits classify_opening, and
        # _VoicemailResponseGate holds the opening LLM context until that decision
        # lands. Left out of this warmup it paid a cold first request there on every
        # call — measured 3.960s cold versus 0.616-0.833s warm on gpt-5.4-mini
        # through the live Cloudflare gateway (#2587). Warming it here moves that
        # cost into the ring window, which is dead time anyway (27-33s of ringing on
        # the 2026-07-25 cloud calls). No tools/context: the classifier runs with its
        # own tiny prompt, so a bare ping is the right shape to open the connection
        # and prime the model.
        if voicemail_classifier is not None:
            warmups.append(("voicemail_classifier", self._prewarm_llm_ping(voicemail_classifier)))
        try:
            results = await asyncio.gather(
                *(coro for _, coro in warmups),
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            logger.debug("Call %s: ring warmup cancelled on media connect", call_id)
            raise

        for (name, _), result in zip(warmups, results, strict=False):
            if isinstance(result, Exception):
                logger.debug("Call %s: %s ring warmup skipped: %s", call_id, name, result)
            else:
                logger.debug("Call %s: %s ring warmup complete", call_id, name)

    async def _prewarm_stt_silence(self, stt: Any) -> None:
        silence_bytes = b"\x00" * (_PHONE_RING_WARMUP_AUDIO_SAMPLE_RATE * _PHONE_RING_WARMUP_AUDIO_SECONDS * 2)

        async def _consume() -> None:
            async for _frame in stt.run_stt(silence_bytes):
                pass

        await asyncio.wait_for(_consume(), timeout=_PHONE_RING_STT_WARMUP_TIMEOUT_SECS)

    async def _prewarm_llm_ping(self, llm: Any, *, llm_context: Any | None = None) -> None:
        from pipecat.processors.aggregators.llm_context import LLMContext

        kwargs: dict[str, Any] = {}
        if llm_context is not None:
            kwargs["tools"] = llm_context.tools
            kwargs["tool_choice"] = llm_context.tool_choice
        context = LLMContext(
            messages=[{"role": "user", "content": "Warm this phone session. Reply with ok."}],
            **kwargs,
        )
        await asyncio.wait_for(
            llm.run_inference(
                context,
                max_tokens=1,
            ),
            timeout=_PHONE_RING_LLM_WARMUP_TIMEOUT_SECS,
        )

    async def _prewarm_tts_first_audio(self, call_id: str, tts: Any) -> None:
        from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame

        # The ring warmup runs BEFORE the pipeline processes its StartFrame, so the
        # TTS service has not yet learned the pipeline's output sample rate
        # (TTSService.start sets _sample_rate = _init_sample_rate or
        # frame.audio_out_sample_rate). With _sample_rate still 0, Kokoro's stream
        # resampler raises "Sample rate should be over 0" and the warmup yields zero
        # audio on EVERY real call — the cold-start cost the warmup exists to absorb
        # is then paid on the first spoken word. Seed the rate the StartFrame WILL
        # negotiate so the warmup synthesizes at the SAME output rate the real call
        # uses, and the real StartFrame re-sets it identically.
        #
        # The seeded rate MUST match what TTSService.start will pick, because Kokoro
        # uses ONE soxr stream resampler for its whole lifetime and that resampler
        # locks to the (source_rate -> output_rate) pair of its FIRST resample.
        # Warming at a DIFFERENT output rate than the real call (e.g. warmup at 16k
        # while a loopback/8k-output call follows) locks the resampler to 24000->16000
        # and then every real turn — needing 24000->8000 — dies with
        # "SOXRStreamAudioResampler cannot be reused with different sample rates"
        # (no audio reaches the line; the call goes dead-air). So prefer the service's
        # own _init_sample_rate (the rate it was constructed with, which the StartFrame
        # honors when truthy) and fall back to the warmup default only when unset.
        if int(getattr(tts, "sample_rate", 0) or 0) <= 0:
            warmup_sample_rate = int(getattr(tts, "_init_sample_rate", 0) or 0) or _PHONE_RING_WARMUP_AUDIO_SAMPLE_RATE
            with suppress(AttributeError):
                tts._sample_rate = warmup_sample_rate

        async def _consume() -> None:
            audio_frame_count = 0
            audio_bytes = 0
            error_texts: list[str] = []
            async for _frame in tts.run_tts("OK.", context_id="ring-warmup-%s" % call_id):
                if isinstance(_frame, TTSAudioRawFrame):
                    audio_frame_count += 1
                    audio_bytes += len(_frame.audio or b"")
                elif isinstance(_frame, ErrorFrame):
                    error_texts.append(str(getattr(_frame, "error", "") or "unknown TTS error"))

            if audio_bytes <= 0:
                detail = "; ".join(error_texts) if error_texts else "no audio frames"
                logger.warning(
                    "Call %s: phone TTS warmup produced zero audio (provider=%s, frames=%d, detail=%s)",
                    call_id,
                    type(tts).__name__,
                    audio_frame_count,
                    detail,
                )
                raise RuntimeError("Phone TTS warmup produced zero audio: %s" % detail)

            logger.debug(
                "Call %s: phone TTS warmup produced %d bytes across %d frame(s) via %s",
                call_id,
                audio_bytes,
                audio_frame_count,
                type(tts).__name__,
            )

        try:
            await asyncio.wait_for(_consume(), timeout=_PHONE_RING_TTS_WARMUP_TIMEOUT_SECS)
        except TimeoutError:
            logger.warning(
                "Call %s: phone TTS warmup timed out after %.1fs (provider=%s)",
                call_id,
                _PHONE_RING_TTS_WARMUP_TIMEOUT_SECS,
                type(tts).__name__,
            )
            raise

    def _create_stt(self, *, stt_hotwords: str | None = None, stt_initial_prompt: str | None = None) -> Any:
        """Create STT service based on config provider selection.

        Providers:
        - "local" (default): Local faster-whisper. CPU threads per transcribe come
          from VIOLA_PHONE_STT_CPU_THREADS (default 4) and cross-call transcribe
          concurrency is capped by VIOLA_PHONE_STT_MAX_CONCURRENT, which bounds
          peak CPU/memory (the successor to the old MKL/OMP=1 mkl_malloc-OOM pin).
          Enables faster-whisper VAD/no-speech/hallucination guards before text is emitted.
        - "deepgram": Deepgram cloud STT (requires deepgram SDK).
        - "openai": OpenAI Whisper API STT.
        """
        provider = self.config.stt_provider

        if provider == "deepgram":
            try:
                from pipecat.services.deepgram.stt import DeepgramSTTService
            except (ImportError, Exception) as exc:
                raise ImportError(
                    "Deepgram STT requires the deepgram SDK. Install with: pip install pipecat-ai[deepgram]"
                ) from exc

            if not self.config.deepgram_api_key:
                raise ValueError("deepgram_api_key is required for Deepgram STT provider")

            logger.info("Using Deepgram cloud STT")
            return DeepgramSTTService(
                api_key=self.config.deepgram_api_key,
                settings=DeepgramSTTService.Settings(
                    language="en",
                ),
            )

        if provider == "openai":
            try:
                from pipecat.services.openai.stt import OpenAISTTService
            except (ImportError, Exception) as exc:
                raise ImportError(
                    "OpenAI STT requires the openai SDK. Install with: pip install pipecat-ai[openai]"
                ) from exc

            logger.info("Using OpenAI cloud STT")
            return OpenAISTTService(
                api_key=self.config.openai_api_key,
            )

        # Default: "local" — existing behavior
        # Cache the faster-whisper model but keep the Pipecat STT processor
        # per-call so cancelled pipeline state does not bleed into the next call.
        shared_model = _ensure_phone_stt_model(self.config)
        with _phone_stt_model_lock:
            effective_model_key = _phone_stt_warm_key or _phone_stt_model_key(self.config)
        resolved_hotwords = (self._stt_hotwords if stt_hotwords is None else stt_hotwords).strip()
        resolved_initial_prompt = (
            self._stt_initial_prompt if stt_initial_prompt is None else stt_initial_prompt
        ).strip()
        uses_multilingual = _phone_stt_uses_multilingual(self.config.whisper_model)
        if uses_multilingual:
            from telephony.multilingual_whisper import (
                MultilingualWhisperSTTService as BaseWhisperSTTService,
            )
        else:
            from pipecat.services.whisper.stt import (
                WhisperSTTService as BaseWhisperSTTService,
            )

        class _SharedPhoneWhisperSTTService(BaseWhisperSTTService):
            def __init__(
                self,
                *args,
                beam_size: int,
                hotwords: str = "",
                initial_prompt: str = "",
                **kwargs,
            ):
                self._beam_size = beam_size
                self._hotwords = hotwords.strip()
                self._initial_prompt = initial_prompt.strip()
                # Per-call lock: serializes transcribes WITHIN this call (one STT
                # processor per call), while concurrent calls proceed in parallel
                # under the process-wide _phone_stt_transcribe_semaphore() cap.
                self._stt_call_lock = threading.Lock()
                base_kwargs = dict(kwargs)
                if uses_multilingual:
                    base_kwargs["beam_size"] = beam_size
                    base_kwargs["hotwords"] = hotwords
                    base_kwargs["initial_prompt"] = initial_prompt
                super().__init__(*args, **base_kwargs)

            def _load(self):
                self._model = shared_model

            async def _transcribe_pcm_text(
                self,
                audio: bytes,
                *,
                initial_prompt: str,
                collect_metrics: bool,
            ) -> str:
                if not audio:
                    return ""
                if collect_metrics:
                    await self.start_processing_metrics()
                await asyncio.to_thread(self._stt_call_lock.acquire)
                try:
                    # Remote GPU voice endpoint (VIOLA_PHONE_VOICE_REMOTE_ENABLED,
                    # default off). Runs the SAME model/options server-side; any
                    # failure/timeout returns None and the local path below runs
                    # unchanged. An empty transcript is a valid remote result.
                    remote_text = await asyncio.to_thread(
                        remote_transcribe_pcm,
                        audio,
                        self.sample_rate,
                        language=self._settings.language,
                        beam_size=self._beam_size,
                        hotwords=self._hotwords,
                        initial_prompt=initial_prompt,
                    )
                    if remote_text is not None:
                        _maybe_capture_phone_stt_input(audio, self.sample_rate, remote_text)
                        return remote_text
                    # Resample phone PCM up to whisper's 16 kHz: faster-whisper does
                    # NOT resample an ndarray, so feeding raw 8 kHz audio garbles it.
                    audio_float = phone_pcm_to_whisper_float(audio, self.sample_rate)
                    kwargs = phone_whisper_transcribe_options(
                        language=self._settings.language,
                        beam_size=self._beam_size,
                        hotwords=self._hotwords,
                        initial_prompt=initial_prompt,
                    )

                    def _decode_text() -> str:
                        # Concurrency cap only around the CPU-heavy decode: the
                        # shared faster-whisper/CTranslate2 model is thread-safe
                        # for concurrent transcribe, so concurrent CALLS no longer
                        # serialize behind one global lock.
                        with _phone_stt_transcribe_semaphore():
                            segments, _ = self._model.transcribe(audio_float, **kwargs)
                            text = ""
                            for segment in segments:
                                text += "%s " % segment.text
                            return text

                    decoded_text = await asyncio.to_thread(_decode_text)
                    _maybe_capture_phone_stt_input(audio, self.sample_rate, decoded_text)
                    return decoded_text
                finally:
                    self._stt_call_lock.release()
                    if collect_metrics:
                        await self.stop_processing_metrics()

            async def _yield_transcription(self, text: str):
                from pipecat.frames.frames import TranscriptionFrame
                from pipecat.utils.time import time_now_iso8601

                clean_text = text if text.endswith(" ") else "%s " % text
                if clean_text.strip():
                    await self._handle_transcription(clean_text, True, self._settings.language)
                    logger.debug("Transcription: [%s]", clean_text.strip())
                    yield TranscriptionFrame(
                        clean_text,
                        self._user_id,
                        time_now_iso8601(),
                        self._settings.language,
                        finalized=True,
                    )

            async def run_stt(self, audio: bytes):
                if uses_multilingual:
                    async for frame in super().run_stt(audio):
                        yield frame
                    return

                from pipecat.frames.frames import ErrorFrame

                if not self._model:
                    yield ErrorFrame("Whisper model not available")
                    return

                text = await self._transcribe_pcm_text(
                    audio,
                    initial_prompt=self._initial_prompt,
                    collect_metrics=True,
                )
                async for frame in self._yield_transcription(text):
                    yield frame

        return _SharedPhoneWhisperSTTService(
            settings=_SharedPhoneWhisperSTTService.Settings(
                model=self.config.whisper_model,
                no_speech_prob=PHONE_STT_NO_SPEECH_THRESHOLD,
            ),
            device=effective_model_key[1],
            compute_type=effective_model_key[2],
            # Pin the STT's belief about the inbound rate to the real telephone
            # rate. STTService.start() resolves sample_rate as
            # `self._init_sample_rate or frame.audio_in_sample_rate`, so this
            # makes the 8k->16k resample in phone_pcm_to_whisper_float correct
            # even if a StartFrame ever carries a wrong audio_in_sample_rate
            # (the default-16000 PipelineParams bug that garbled recipient
            # transcripts). Defense-in-depth alongside the PipelineParams rate.
            sample_rate=PHONE_INBOUND_SAMPLE_RATE,
            beam_size=self.config.whisper_beam_size,
            hotwords=resolved_hotwords,
            initial_prompt=resolved_initial_prompt,
        )

    def _create_streaming_stt(self) -> Any:
        """Create the Phase-1 streaming STT first pass, or None to fail open.

        Returns a ``SherpaOnnxStreamingSTTService`` only when
        ``VIOLA_PHONE_STT_STREAMING`` selects a streaming mode AND both the
        sherpa-onnx runtime and the streaming model are present. In every other
        case (off, or model/runtime absent) it returns None and the pipeline runs
        today's batch-whisper-only path — a missing model never breaks a call.

        ``full`` mode is reserved for Phase 2 (flipping final-text authority to the
        streaming engine behind a live A/B); it is NOT wired here, so it is
        downgraded to ``interims`` — whisper stays the sole final authority in this
        lane, which is the no-accuracy-risk contract of Phase 1.
        """
        mode = str(getattr(self.config, "phone_stt_streaming", "off") or "off").strip().lower()
        if mode not in ("interims", "full"):
            return None

        try:
            from telephony.streaming_stt_service import (
                SherpaOnnxStreamingSTTService,
                streaming_stt_runtime_available,
            )
        except ImportError:
            logger.warning(
                "Phone streaming STT requested (mode=%s) but the service module failed to import; "
                "falling back to batch whisper.",
                mode,
            )
            return None

        if not streaming_stt_runtime_available():
            logger.warning(
                "Phone streaming STT requested (mode=%s) but the sherpa-onnx runtime or model is "
                "unavailable; falling back to batch whisper. Provision with "
                "`python scripts/download_models.py --streaming-stt`.",
                mode,
            )
            return None

        if mode == "full":
            logger.warning(
                "VIOLA_PHONE_STT_STREAMING=full is a Phase-2 setting (final-authority flip) not "
                "wired in this lane; running interims-only so whisper keeps final authority."
            )
        effective_mode = "interims"

        logger.info(
            "Phone streaming STT enabled (mode=%s, first-pass interims).",
            effective_mode,
        )
        return SherpaOnnxStreamingSTTService(
            mode=effective_mode,
            # Pin the inbound rate exactly like the whisper STT so the 8k->16k
            # front-end resamples correctly regardless of StartFrame beliefs.
            sample_rate=PHONE_INBOUND_SAMPLE_RATE,
        )

    def _create_tts(self, call_id: str = "") -> Any:
        """Create TTS service based on config provider selection.

        Providers:
        - "local": Local Kokoro TTS.
        - "piper": Local Piper neural TTS.
        - "espeak": Local espeak-ng TTS for low-latency cloud phone media.
        - "elevenlabs": ElevenLabs cloud TTS (requires elevenlabs SDK).

        All providers include SpeechTextFilter for normalizing currency,
        phone numbers, percentages, and times into TTS-friendly speech text.
        """
        configure_environment()

        from pipecat.services.tts_service import TextAggregationMode

        from telephony.tts_normalizer import SpeechTextFilter

        speech_filter = SpeechTextFilter()
        # Aggregate at the SENTENCE boundary, not per-token. Neural phone voices
        # (Piper/Kokoro) build prosody across a whole phrase; synthesizing one
        # token at a time makes every word its own flat, disconnected moment
        # ("Hell-o" syllable-by-syllable). TOKEN was an over-aggressive latency
        # hack that traded all natural flow for a few hundred ms of first-audio.
        # SENTENCE waits for the first complete sentence (Piper synthesizes one in
        # ~100ms), then streams subsequent sentences — natural flow at a small,
        # deliberate latency cost.
        phone_tts_kwargs = {
            "text_aggregation_mode": TextAggregationMode.SENTENCE,
            "text_filters": [speech_filter],
        }
        provider = self.config.tts_provider

        # Bind the shared SpeechTextFilter to whichever TTS service we build so
        # its corruption guard can read the ACTIVE TTS language (and never strip
        # a legitimate language-switch to a non-English supported language). We
        # construct, bind, then return for every provider.
        def _bound(tts: Any) -> Any:
            speech_filter.bind_tts(tts, call_id=call_id)
            return tts

        if provider == "espeak":
            from telephony.espeak_tts_service import EspeakNgTTSService

            logger.info("Using espeak-ng local phone TTS")
            return _bound(
                EspeakNgTTSService(
                    voice=os.environ.get("VIOLA_PHONE_ESPEAK_VOICE", "en-us").strip() or "en-us",
                    words_per_minute=int(os.environ.get("VIOLA_PHONE_ESPEAK_WPM", "185") or "185"),
                    stop_frame_timeout_s=0.5,
                    **phone_tts_kwargs,
                )
            )

        if provider == "elevenlabs":
            try:
                from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
            except (ImportError, Exception) as exc:
                raise ImportError(
                    "ElevenLabs TTS requires the elevenlabs SDK. Install with: pip install pipecat-ai[elevenlabs]"
                ) from exc

            if not self.config.elevenlabs_api_key:
                raise ValueError("elevenlabs_api_key is required for ElevenLabs TTS provider")

            logger.info("Using ElevenLabs cloud TTS")
            return _bound(
                ElevenLabsTTSService(
                    api_key=self.config.elevenlabs_api_key,
                    voice_id=self.config.tts_voice,
                    **phone_tts_kwargs,
                )
            )

        if provider != "local":
            if provider == "piper":
                from telephony.piper_tts import (
                    PiperPhoneTTSService,
                    phone_piper_model_paths,
                )

                model_path, config_path = phone_piper_model_paths(self.config.tts_voice)
                logger.info("Using local Piper phone TTS")
                return _bound(
                    PiperPhoneTTSService(
                        model_path=model_path,
                        config_path=config_path,
                        voice=self.config.tts_voice,
                        stop_frame_timeout_s=0.5,
                        sample_rate=16000,
                        **phone_tts_kwargs,
                    )
                )
            raise ValueError("Unsupported phone TTS provider: %s" % provider)

        # Default: "local" — fail closed if Kokoro is unavailable.
        try:
            runtime = _ensure_phone_kokoro_tts_runtime(self.config)
            logger.info(
                "Using shared local Kokoro phone TTS runtime: provider=%s session_providers=%s",
                runtime.provider,
                ",".join(runtime.session_providers),
            )
            return _bound(
                _create_shared_phone_kokoro_tts_service(
                    kokoro=runtime.kokoro,
                    voice_id=self.config.tts_voice,
                    stop_frame_timeout_s=0.5,
                    **phone_tts_kwargs,
                )
            )
        except (ImportError, Exception) as exc:
            logger.error("Kokoro TTS not available; refusing paid cloud TTS fallback: %s", exc)
            raise RuntimeError("Phone calls require local Kokoro TTS") from exc

    def _estimate_cost(self, record: CallRecord) -> None:
        """Estimate cost breakdown for the call.

        Uses pricing from services.llm.pricing (single source of truth).
        Telnyx rates from telephony.cost_tracker.

        Rough token estimate: ~50 tokens per transcript turn.
        """
        from services.llm.pricing import calculate_cost_usd
        from telephony.cost_tracker import (
            _TELNYX_REGULATORY_SURCHARGE,
            _get_telnyx_rate,
            estimate_phone_tts_cost_usd,
        )

        if record._cost_tracker is not None:
            breakdown = record._cost_tracker.build_breakdown(record.duration_seconds)
            record.llm_prompt_tokens = breakdown.llm_prompt_tokens
            record.llm_completion_tokens = breakdown.llm_completion_tokens
            record.estimated_cost_usd = breakdown.total_cost_usd
            logger.info(
                "Call %s cost estimate: $%.4f (telnyx=$%.4f, llm=$%.4f, stt=$%.4f, tts=$%.4f)",
                record.call_id,
                record.estimated_cost_usd,
                breakdown.telnyx_cost_usd,
                breakdown.llm_cost_usd,
                breakdown.stt_cost_usd,
                breakdown.tts_cost_usd,
            )
            return

        minutes = record.duration_seconds / 60.0
        rate = _get_telnyx_rate(record.phone_number)
        telnyx_cost = minutes * rate * (1 + _TELNYX_REGULATORY_SURCHARGE)

        # Estimate LLM tokens from transcript
        total_text = " ".join(str(e.get("text", "")) for e in record.transcript if isinstance(e, dict))
        est_tokens = len(total_text.split()) * 1.3  # ~1.3 tokens per word
        turns = len(record.transcript)
        # Each LLM call sends growing context: avg context ~= turns/2 * tokens_per_turn
        avg_prompt_tokens = int(turns * est_tokens * 0.5) + 200  # +200 for system prompt
        avg_completion_tokens = int(est_tokens * 0.4)  # ~40% of text is assistant

        record.llm_prompt_tokens = avg_prompt_tokens
        record.llm_completion_tokens = avg_completion_tokens

        # LLM cost from services.llm.pricing (single source of truth)
        llm_cost = calculate_cost_usd(self.config.llm_model, avg_prompt_tokens, avg_completion_tokens)
        assistant_text = " ".join(
            str(e.get("text", ""))
            for e in record.transcript
            if isinstance(e, dict) and str(e.get("role", "")).lower() == "viola"
        )
        tts_cost = estimate_phone_tts_cost_usd(self.config.tts_provider, len(assistant_text))

        record.estimated_cost_usd = telnyx_cost + llm_cost + tts_cost

        logger.info(
            "Call %s cost estimate: $%.4f (telnyx=$%.4f, llm=$%.4f, stt=$0, tts=$%.4f)",
            record.call_id,
            record.estimated_cost_usd,
            telnyx_cost,
            llm_cost,
            tts_cost,
        )

    async def _generate_summary(self, transcript_text: str, *, user_id: str = "") -> str:
        """Generate a concise post-call summary using the LLM.

        Falls back to a basic summary if the LLM call fails.
        """
        if not transcript_text.strip():
            return "No conversation recorded."

        if not _is_cloud_llm_consented():
            logger.warning("Skipping post-call summary: cloud LLM consent not given")
            lines = transcript_text.strip().split("\n")
            if len(lines) <= 3:
                return "Call transcript: " + _log_text_preview(transcript_text.strip())
            return "Call started with: %s\n...\nCall ended with: %s" % (
                _log_text_preview(lines[0]),
                _log_text_preview(lines[-1]),
            )

        try:
            prompt = (
                "Summarize this phone call in 2-3 sentences for the person who requested it.\n"
                "Include what was accomplished or not, key details such as times, prices, "
                "confirmation numbers, and any follow-up needed. Be factual. No preamble.\n\n"
                "CALL TRANSCRIPT:\n%s" % transcript_text
            )

            data = await create_accounted_openai_chat_completion(
                api_key=self.config.openai_api_key,
                model=self.config.llm_model,
                messages=[
                    {
                        "role": "system",
                        "content": "Summarize this phone call concisely.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_output_tokens=200,
                user_id=user_id,
                operation="phone_post_call_summary",
            )
            return data["choices"][0]["message"]["content"].strip()

        except Exception as exc:
            logger.warning("Summary generation failed: %s", exc)
            lines = transcript_text.strip().split("\n")
            if len(lines) <= 3:
                return "Call transcript: " + transcript_text.strip()
            return "Call started with: %s\n...\nCall ended with: %s" % (
                lines[0],
                lines[-1],
            )

    def cleanup_completed(self, max_age_seconds: float = 3600) -> int:
        """Remove completed call records older than max_age.

        Returns:
            Number of records cleaned up.
        """
        now = datetime.now(tz=UTC)
        to_remove = []
        records = dict(self._call_records)
        records.update(self._active_calls)
        for call_id, record in records.items():
            if record.ended_at:
                age = (now - record.ended_at).total_seconds()
                if age > max_age_seconds:
                    to_remove.append(call_id)

        for call_id in to_remove:
            self._active_calls.pop(call_id, None)
            self._cancel_billing_heartbeat(call_id)
            self._call_records.pop(call_id, None)

        return len(to_remove)
