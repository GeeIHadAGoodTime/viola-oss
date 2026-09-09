"""Telemetry reporter — builds JSON blob from accumulated counters.

Applies bounded random jitter to integer counts (light obfuscation — NOT
differential privacy; see _apply_noise), computes percentiles, and builds the
complete telemetry payload. No PII, no command text, no voice data. Sends only
over https (loopback http allowed for self-hosted dev — see send()).

Transmission is gated by four conditions (all must be True):
1. The user's telemetry opt-in choice — canonically SettingsManager's
   ``telemetry_opt_in`` (settings.json, the runtime truth for user
   preferences per CLAUDE.md Settings Resolution), default False. See
   ``_is_telemetry_enabled_by_user`` for why AppConfig.telemetry_enabled is
   NOT the authority here (#2175).
2. settings.telemetry_server_url is non-empty
3. User privacy consent for error reporting is granted
4. The launch kill-switch subsystem "telemetry" is enabled
"""

from __future__ import annotations

import json
import math
import platform
import random
import time
from typing import Any

import httpx

from core.constants import TIMEOUT_LONG
from core.logging_config import get_logger

logger = get_logger(__name__)

# Tracks timestamp of last successful send (epoch seconds, or None)
_last_sent_at: float | None = None

# Tracks the last send attempt regardless of outcome. Useful for
# distinguishing "we tried and failed" from "we never tried" in status
# output.
_last_attempt_at: float | None = None

# Number of consecutive failed send attempts since the most recent
# successful send. A non-zero value here means the operator's cloud
# ingest endpoint is rejecting or unreachable; surface this through the
# status endpoint so silent-failure outages are visible.
_consecutive_failures: int = 0

# Number of consecutive failures at which we log a single warning. We
# don't keep warning every cycle to avoid log spam, but we do want at
# least one bright signal that telemetry has stalled.
_FAILURE_WARN_THRESHOLD = 3


def _settings_manager_telemetry_opt_in() -> bool | None:
    """Return the user-facing telemetry opt-in when SettingsManager is available.

    ``telemetry_opt_in`` is the canonical key (SETTING_KEY_ALIASES in
    ui/settings_schema.py maps the legacy ``telemetry_enabled`` settings-key
    onto it, and SettingsManager migrates any already-persisted
    ``telemetry_enabled`` value on load — see
    tests/unit/test_settings_manager_dead_keys.py). The ``telemetry_enabled``
    fallback read below is defense-in-depth for a SettingsManager instance
    that predates that migration running against an un-migrated in-memory
    dict; on disk the key is always canonicalized to ``telemetry_opt_in``.
    """
    try:
        from ui.settings_manager import get_settings_manager

        mgr = get_settings_manager()
        value = mgr.get("telemetry_opt_in", None)
        if value is None:
            value = mgr.get("telemetry_enabled", None)
        if value is None:
            return None
        return bool(value)
    except Exception:
        logger.debug("Telemetry user opt-in check unavailable", exc_info=True)
        return None


def _is_telemetry_enabled_by_user(settings: Any) -> bool:
    """Return the user's telemetry opt-in choice — SettingsManager is authoritative.

    SettingsManager's ``telemetry_opt_in`` is the runtime truth for the
    user's choice (CLAUDE.md Settings Resolution: SettingsManager holds user
    preferences; AppConfig holds secrets/infrastructure only, and route
    handlers must never treat it as user-preference authority). It is
    checked FIRST and, whenever it is readable, is the ONLY thing consulted
    — including on desktop, where ``AppConfig.telemetry_enabled`` is nothing
    but a one-way mirror written by the ``/v1/settings`` PATCH handler
    (ui/settings_api.py) whenever a request body includes ``telemetry_opt_in``.
    That mirror is never re-synced at process startup, so an install that
    opted in during a previous session — and never happens to re-touch the
    Settings toggle afterward — would otherwise have gate 1 silently reset to
    "disabled" on every fresh launch even though settings.json still records
    the user's real choice as opted-in (the #2175 bug: the reporter was
    reading a stale AppConfig mirror instead of the SettingsManager key the
    Settings UI actually writes).

    AppConfig.telemetry_enabled is consulted only when SettingsManager itself
    cannot be resolved at all. On desktop that means SettingsManager is
    broken/unavailable, so this fails closed rather than trusting a
    possibly-stale .env value. On a non-desktop surface with no local
    SettingsManager (e.g. a cloud deployment), AppConfig is the only signal
    that exists, so it is used as-is.
    """
    user_opt_in = _settings_manager_telemetry_opt_in()
    if user_opt_in is not None:
        return user_opt_in

    surface = getattr(settings, "app_surface", "desktop")
    if not isinstance(surface, str):
        surface = "desktop"
    if surface.lower() == "desktop":
        # Desktop has SettingsManager as the user-facing source of truth.
        # If it cannot be read, fail closed instead of trusting .env.
        return False

    return bool(getattr(settings, "telemetry_enabled", False))


def _telemetry_kill_switch_reason() -> str | None:
    """Return None when telemetry is enabled, otherwise a fail-closed reason."""
    try:
        from backend.launch_kill_switches import get_store

        store = get_store()
        if store.is_enabled("telemetry"):
            return None
        state = store.get_state("telemetry")
        return str(state.get("reason") or "telemetry kill-switch disabled")
    except (ImportError, RuntimeError, TypeError, ValueError):
        logger.debug("Telemetry kill-switch check failed", exc_info=True)
        return "telemetry kill-switch unavailable"


def _percentile(values: list[float], pct: float) -> float:
    """Compute a percentile from a sorted list."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = (pct / 100.0) * (len(sorted_vals) - 1)
    lower = math.floor(idx)
    upper = min(lower + 1, len(sorted_vals) - 1)
    frac = idx - lower
    return round(sorted_vals[lower] * (1 - frac) + sorted_vals[upper] * frac, 1)


def _is_telemetry_transport_secure(server_url: str) -> bool:
    """Return True if the telemetry endpoint is safe to send to in cleartext-terms.

    https is always accepted. Plain http is accepted ONLY for loopback hosts
    (localhost / 127.0.0.1 / [::1]) so a self-hosted operator can run the ingest
    server on the same machine during development. Any other http URL is
    rejected — aggregate behavioral telemetry must not cross a network in
    cleartext (SEC-049).
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(server_url.strip())
    except ValueError:
        return False

    scheme = (parsed.scheme or "").lower()
    if scheme == "https":
        return True
    if scheme == "http":
        host = (parsed.hostname or "").lower()
        return host in ("localhost", "127.0.0.1", "::1")
    return False


def resolve_telemetry_server_url(settings: Any) -> str:
    """Resolve the telemetry/funnel destination origin, with a product default.

    Precedence:
      1. An explicitly configured ``telemetry_server_url`` — self-hosted
         operators point this at their own ingest origin.
      2. Otherwise the app's cloud API origin (``api_base_url``, default
         ``https://api.useviola.com``) — the desktop product's telemetry ingest
         and public funnel endpoints both live there.

    Returning a non-empty origin does NOT enable sending: transmission stays
    gated by opt-in + privacy consent + kill-switch (see ``should_send``). This
    only ensures an opted-in, consenting install actually has a destination —
    the missing wiring that left ``telemetry_server_url`` empty in every shipped
    build (nothing ever populated it) and so silently dropped every first_run /
    telemetry send. That gap is why the growth oracle's ``first_run`` read 0
    all-time against real installs phoning home: the beacon had nowhere to go.
    """
    explicit = (getattr(settings, "telemetry_server_url", "") or "").strip()
    if explicit:
        return explicit
    return (getattr(settings, "api_base_url", "") or "").strip()


def _apply_noise(value: int) -> int:
    """Coarsen an exact integer count with bounded random jitter (+/-5%).

    This is NOT differential privacy. There is no privacy budget (epsilon), no
    calibrated global sensitivity, and the noise is bounded-uniform rather than
    drawn from a Laplace/Gaussian DP mechanism — so it provides no formal DP
    guarantee against a determined adversary observing many reports. What it
    does provide is light obfuscation: it stops an exact integer count (e.g.
    "exactly 137 wake activations") from leaving the device, which is the
    realistic privacy concern for opt-in aggregate product telemetry. The blob
    already carries no PII, no command text, and no audio.

    Honesty note (SEC-049): an earlier version of this docstring claimed
    "differential privacy (Laplace)". That was an overclaim — the mechanism was
    never Laplace and never satisfied DP. The claim was corrected here and in
    the module docstrings rather than dressing up the same mechanism as DP.
    """
    if value == 0:
        return 0
    scale = max(1, value * 0.05)
    noise = int(random.uniform(-scale, scale))
    return max(0, value + noise)


def _commands_per_day_bucket(commands_per_day: dict[str, int]) -> str:
    """Bucket commands-per-day into privacy-preserving ranges."""
    if not commands_per_day:
        return "0"
    avg = sum(commands_per_day.values()) / len(commands_per_day)
    if avg <= 5:
        return "1-5"
    if avg <= 15:
        return "6-15"
    if avg <= 50:
        return "16-50"
    if avg <= 100:
        return "51-100"
    return "100+"


class TelemetryReporter:
    """Builds the telemetry blob from an accumulator snapshot."""

    def __init__(
        self,
        accumulator: Any,
        *,
        install_id: str = "unknown",
        tier: str = "free",
        app_version: str = "0.0.0",
        wake_word_model: str = "viola_v2",
        rooms_active: int = 1,
        db_size_mb: float = 0.0,
    ) -> None:
        self._accumulator = accumulator
        self._install_id = install_id
        self._tier = tier
        self._app_version = app_version
        self._wake_word_model = wake_word_model
        self._rooms_active = rooms_active
        self._db_size_mb = db_size_mb
        self._period_start: str = ""
        self._period_end: str = ""

    def set_period(self, start: str, end: str) -> None:
        """Set the reporting period."""
        self._period_start = start
        self._period_end = end

    def build_blob(self) -> dict[str, Any]:
        """Build the complete telemetry blob.

        Returns a dict (not JSON string) matching the v2 telemetry schema.
        """
        snap = self._accumulator.snapshot()

        cmds = snap["commands"]
        ww = snap["wake_word"]
        latency = snap["latency"]
        cache = snap["cache"]
        engagement = snap["engagement"]
        health = snap["health"]

        # Compute percentiles from raw latency lists
        latency_ms = {}
        stage_map = {
            "wake_to_stt": latency.get("wake_to_stt", []),
            "stt": latency.get("stt", []),
            "intent_rule": latency.get("intent_rule", []),
            "intent_llm": latency.get("intent_llm", []),
            "tts": latency.get("tts", []),
            "end_to_end_rule": [],  # Computed below
            "end_to_end_llm": [],  # Computed below
        }

        # Estimate end-to-end from components
        e2e = latency.get("end_to_end", [])
        if e2e:
            stage_map["end_to_end_rule"] = e2e
            stage_map["end_to_end_llm"] = e2e

        for stage, values in stage_map.items():
            if values:
                latency_ms[stage] = {
                    "p50": _percentile(values, 50),
                    "p95": _percentile(values, 95),
                }

        # Music provider distribution (as percentages)
        provider_counts = snap.get("music_provider_counts", {})
        total_plays = sum(provider_counts.values()) or 1
        music_provider_pct = {p: round(c / total_plays * 100) for p, c in provider_counts.items()}

        # Coarsen exact integer counters with bounded jitter (not DP; see _apply_noise)
        total_cmds = _apply_noise(cmds["total"])
        activations = _apply_noise(ww["activations"])

        # Compute activation-to-command ratio
        activation_to_command = ww.get("activation_to_command", 0)
        act_to_cmd_ratio = round(activation_to_command / max(activations, 1), 2)

        # Multi-room drift buckets
        mr = snap.get("multi_room", {})
        drift_vals = mr.get("drift_values_ms", [])
        drift_buckets = {
            "under_50ms": 0,
            "50_500ms": 0,
            "500_3000ms": 0,
            "over_3000ms": 0,
        }
        for d in drift_vals:
            abs_d = abs(d)
            if abs_d < 50:
                drift_buckets["under_50ms"] += 1
            elif abs_d < 500:
                drift_buckets["50_500ms"] += 1
            elif abs_d < 3000:
                drift_buckets["500_3000ms"] += 1
            else:
                drift_buckets["over_3000ms"] += 1

        # Agent per-tool summary
        agent_snap = snap.get("agent", {})
        agent_tool_summary = {}
        for tool_name, call_count in agent_snap.get("tool_calls", {}).items():
            agent_tool_summary[tool_name] = {
                "calls": call_count,
                "failures": agent_snap.get("tool_failures", {}).get(tool_name, 0),
            }
            lats = agent_snap.get("tool_latencies", {}).get(tool_name, [])
            if lats:
                agent_tool_summary[tool_name]["p50_ms"] = _percentile(lats, 50)
                agent_tool_summary[tool_name]["p95_ms"] = _percentile(lats, 95)

        # LLM cost data
        llm_snap = snap.get("llm", {})
        llm_latency_vals = llm_snap.get("latency_values", [])
        avg_llm_latency = round(sum(llm_latency_vals) / len(llm_latency_vals)) if llm_latency_vals else 0

        blob: dict[str, Any] = {
            "v": 2,
            "period": (f"{self._period_start}/{self._period_end}" if self._period_start else ""),
            "install_id": self._install_id,
            "tier": self._tier,
            "os": platform.system().lower(),
            "app_version": self._app_version,
            "wake_word_model": self._wake_word_model,
            "rooms_active": self._rooms_active,
            "commands": {
                "total": total_cmds,
                "rule_matched": _apply_noise(cmds["rule_matched"]),
                "llm_routed": _apply_noise(cmds["llm_routed"]),
                "agent_tasks": _apply_noise(cmds["agent_tasks"]),
                "agent_succeeded": _apply_noise(cmds["agent_succeeded"]),
                "by_category": {k: _apply_noise(v) for k, v in cmds.get("by_category", {}).items()},
            },
            "wake_word": {
                "activations": activations,
                "activations_during_playback": _apply_noise(ww["activations_during_playback"]),
                "false_positive_reports": _apply_noise(ww["false_positive_reports"]),
                "rapid_reactivations": _apply_noise(ww["rapid_reactivations"]),
                "activation_to_command_ratio": act_to_cmd_ratio,
            },
            "latency_ms": latency_ms,
            "cache": {
                "hits": _apply_noise(cache["hits"]),
                "misses": _apply_noise(cache["misses"]),
            },
            "gate_hits": {k: _apply_noise(v) for k, v in snap.get("gate_hits", {}).items()},
            "errors": {
                "count": _apply_noise(snap["errors"]["count"]),
                "codes": snap["errors"]["codes"],
            },
            "music_provider_pct": music_provider_pct,
            "features_used": list(snap.get("features_used", {}).keys()),
            "engagement": {
                "active_days": engagement.get("active_days", 0),
                "commands_per_active_day_bucket": _commands_per_day_bucket(engagement.get("commands_per_day", {})),
                "hour_histogram": engagement.get("hour_histogram", [0] * 24),
            },
            "health": {
                # Release-health gates must act on exact crash/session counts;
                # randomized safety counters can hide a real one-crash cohort.
                "sessions_started": int(health.get("sessions_started", 0)),
                "sessions_clean_exits": int(health.get("sessions_clean_exits", 0)),
                "session_crashes": int(health.get("session_crashes", 0)),
                "crashes": int(health.get("crashes", 0)),
                "unhandled_exceptions": int(health.get("unhandled_exceptions", 0)),
                "llm_timeouts": _apply_noise(health.get("llm_timeouts", 0)),
                "playback_failures": _apply_noise(health.get("playback_failures", 0)),
                "db_size_mb": round(self._db_size_mb, 1),
            },
            "multi_room": {
                "drift_buckets": drift_buckets,
                "corrections": {k: _apply_noise(v) for k, v in mr.get("corrections", {}).items()},
                "spoke_events": {k: _apply_noise(v) for k, v in mr.get("spoke_events", {}).items()},
            },
            "agent": {
                "tool_summary": agent_tool_summary,
                "approval_counts": {k: _apply_noise(v) for k, v in agent_snap.get("approval_counts", {}).items()},
            },
            "messaging": {
                k: {ek: _apply_noise(ev) for ek, ev in v.items()} for k, v in snap.get("messaging", {}).items()
            },
            "llm": {
                "total_requests": _apply_noise(llm_snap.get("total_requests", 0)),
                "total_cost_microdollars": llm_snap.get("total_cost_microdollars", 0),
                "input_tokens": llm_snap.get("input_tokens", 0),
                "output_tokens": llm_snap.get("output_tokens", 0),
                "cache_read_tokens": llm_snap.get("cache_read_tokens", 0),
                "cache_write_tokens": llm_snap.get("cache_write_tokens", 0),
                # F-053: parity with SessionCostTracker / Claude Code's
                # cost-tracker.ts. The accumulator was already capturing
                # these; the reporter was the layer that dropped them.
                "web_search_requests": llm_snap.get("web_search_requests", 0),
                "by_model": {
                    str(model): {str(k): _apply_noise(v) for k, v in row.items()}
                    for model, row in (llm_snap.get("by_model") or {}).items()
                },
                "unknown_model_warning": bool(llm_snap.get("unknown_model_warning", False)),
                "avg_latency_ms": avg_llm_latency,
                "by_tier": {k: _apply_noise(v) for k, v in llm_snap.get("by_tier", {}).items()},
                "by_type": {k: _apply_noise(v) for k, v in llm_snap.get("by_type", {}).items()},
                "cost_by_tier": dict(llm_snap.get("cost_by_tier", {})),
            },
        }

        return blob

    def build_blob_json(self) -> str:
        """Build the telemetry blob as a JSON string."""
        return json.dumps(self.build_blob(), indent=2)

    # ------------------------------------------------------------------
    # Transmission gating
    # ------------------------------------------------------------------

    @staticmethod
    def user_opted_in() -> bool:
        """Return the resolved user telemetry opt-in (SettingsManager-authoritative).

        This is the same gate ``should_send()``/``send_reason()`` use for
        condition 1, exposed publicly so callers that need to *display* the
        user's choice (e.g. the ``/v1/telemetry/status`` transparency
        endpoint) report the value that actually gates sending instead of
        reading ``AppConfig.telemetry_enabled`` directly, which is only a
        possibly-stale mirror (see ``_is_telemetry_enabled_by_user``, #2175).
        """
        from config.settings import settings

        return _is_telemetry_enabled_by_user(settings)

    @staticmethod
    def should_send() -> bool:
        """Check whether all send conditions are met.

        1. The user's telemetry opt-in (SettingsManager ``telemetry_opt_in``,
           the canonical/authoritative key — see ``_is_telemetry_enabled_by_user``)
        2. telemetry_server_url is configured (non-empty)
        3. User privacy consent for error reporting is granted
        4. Telemetry launch kill-switch is enabled

        Returns True only when all conditions are True.
        """
        from config.settings import settings

        if not _is_telemetry_enabled_by_user(settings):
            return False

        if not resolve_telemetry_server_url(settings):
            return False

        # GDPR consent gate: never transmit telemetry without explicit user consent
        from core.privacy_consent import is_error_reporting_consented

        if not is_error_reporting_consented():
            return False

        if _telemetry_kill_switch_reason() is not None:
            return False

        return True

    @staticmethod
    def send_reason() -> str:
        """Return a human-readable reason for the current send state.

        Useful for the /v1/telemetry/status endpoint.
        """
        from config.settings import settings

        if not _is_telemetry_enabled_by_user(settings):
            return "Telemetry not opted in"

        if not resolve_telemetry_server_url(settings):
            return "Telemetry server not configured"

        from core.privacy_consent import is_error_reporting_consented

        if not is_error_reporting_consented():
            return "User privacy consent not granted"

        kill_reason = _telemetry_kill_switch_reason()
        if kill_reason is not None:
            return "Telemetry disabled by kill-switch: %s" % kill_reason

        return "All conditions met — telemetry will send"

    @staticmethod
    def last_sent_at() -> float | None:
        """Return epoch timestamp of last successful send, or None."""
        return _last_sent_at

    @staticmethod
    def last_attempt_at() -> float | None:
        """Return epoch timestamp of the last send attempt (success or failure).

        Distinguishes "we tried and the cloud rejected" from "we never
        tried" — useful for operator-side outage detection.
        """
        return _last_attempt_at

    @staticmethod
    def consecutive_failures() -> int:
        """Number of consecutive failed send attempts since last success.

        Resets to 0 on the next successful send. Non-zero values mean the
        configured ``telemetry_server_url`` is rejecting or unreachable;
        the accumulator continues to grow (bounded by per-list caps) until
        the next successful upload.
        """
        return _consecutive_failures

    async def send(self) -> bool:
        """Build and transmit the telemetry blob if all conditions are met.

        Returns True if the blob was sent successfully, False otherwise.
        Logs the reason for skipping if conditions are not met.
        """
        global _last_sent_at, _last_attempt_at, _consecutive_failures

        # --- Gate 1: opt-in (cheap config check) ---
        from config.settings import settings

        if not _is_telemetry_enabled_by_user(settings):
            logger.info("Telemetry skipped: not opted in")
            return False

        # --- Gate 2: server URL configured AND transport is encrypted ---
        server_url = resolve_telemetry_server_url(settings)
        if not server_url:
            logger.info("Telemetry skipped: telemetry_server_url not configured")
            return False

        # TELEMETRY-HTTPS (SEC-049): the blob carries aggregate behavioral
        # counters off the user's machine. Refuse to ship it over cleartext —
        # an on-path observer must not be able to read or tamper with it. https
        # is required; plain http is allowed ONLY for loopback (a self-hosted
        # operator running the ingest server on the same host for development).
        if not _is_telemetry_transport_secure(server_url):
            logger.warning(
                "Telemetry skipped: telemetry_server_url is not https "
                "(cleartext transport refused for non-loopback hosts)"
            )
            return False

        # --- Gate 3: user privacy consent (defense-in-depth) ---
        from core.privacy_consent import is_error_reporting_consented

        if not is_error_reporting_consented():
            logger.info("Telemetry skipped: user privacy consent not granted")
            return False

        # All gates passed — build and send
        kill_reason = _telemetry_kill_switch_reason()
        if kill_reason is not None:
            logger.info("Telemetry skipped: %s", kill_reason)
            return False

        blob = self.build_blob()
        url = server_url.rstrip("/") + "/api/telemetry/ingest"

        _last_attempt_at = time.time()
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_LONG) as client:
                resp = await client.post(url, json=blob)
                resp.raise_for_status()

            _last_sent_at = time.time()
            _consecutive_failures = 0
            logger.info("Telemetry sent successfully to %s", url)
            self._accumulator.reset()
            return True
        except Exception:
            _consecutive_failures += 1
            # Log every failure at warning level once we cross the
            # threshold; below that, exception() at info-equivalent so
            # we don't spam the log on a single transient blip.
            if _consecutive_failures >= _FAILURE_WARN_THRESHOLD:
                logger.warning(
                    "Telemetry send failed (%d consecutive failures); "
                    "accumulator will retain data until next successful send",
                    _consecutive_failures,
                )
            logger.exception("Telemetry send failed")
            return False
