"""Thread-safe in-memory counter system for telemetry accumulation.

Provides single-line increment methods called from instrumentation points.
All methods are thread-safe — audio thread, Qt thread, and async handlers
can all call these without coordination.

No PII is ever stored. No command text. No voice data.
"""

from __future__ import annotations

import threading
import time
from typing import Any


class TelemetryAccumulator:
    """Thread-safe in-memory telemetry counter accumulator.

    Pre-allocates all counter slots to avoid allocation on the hot path.
    Uses a single lock for simplicity — contention is negligible since
    increments are nanosecond operations.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # Command counters
        self._commands_total: int = 0
        self._commands_rule_matched: int = 0
        self._commands_llm_routed: int = 0
        self._commands_agent_tasks: int = 0
        self._commands_agent_succeeded: int = 0
        self._commands_succeeded: int = 0
        self._commands_failed: int = 0
        self._commands_by_category: dict[str, int] = {}

        # Wake word counters
        self._wake_activations: int = 0
        self._wake_activations_during_playback: int = 0
        self._wake_false_positive_reports: int = 0
        self._wake_rapid_reactivations: int = 0
        self._wake_activation_to_command: int = 0
        self._last_wake_time: float = 0.0

        # Latency accumulators (raw values for percentile computation)
        self._latency_wake_to_stt: list[float] = []
        self._latency_stt: list[float] = []
        self._latency_intent_rule: list[float] = []
        self._latency_intent_llm: list[float] = []
        self._latency_tts: list[float] = []
        self._latency_end_to_end: list[float] = []

        # Cache counters
        self._cache_hits: int = 0
        self._cache_misses: int = 0

        # Gate hit counters
        self._gate_hits: dict[str, int] = {}

        # Error counters
        self._error_count: int = 0
        self._error_codes: dict[str, int] = {}

        # Music provider counters
        self._music_provider_counts: dict[str, int] = {}

        # Engagement tracking
        self._active_days: set[str] = set()
        self._commands_per_day: dict[str, int] = {}
        self._hour_histogram: list[int] = [0] * 24

        # Features used (counter dict: feature -> usage count)
        self._features_used: dict[str, int] = {}

        # Health counters
        self._sessions_started: int = 0
        self._sessions_clean_exits: int = 0
        self._session_crashes: int = 0
        self._crashes: int = 0
        self._unhandled_exceptions: int = 0
        self._llm_timeouts: int = 0
        self._playback_failures: int = 0

        # Phase 9: Multi-room sync quality
        self._sync_drift_values_ms: list[float] = []
        self._sync_corrections: dict[str, int] = {}  # type -> count
        self._sync_spoke_events: dict[str, int] = {}  # event -> count

        # Phase 10A: Per-tool agent tracking
        self._agent_tool_calls: dict[str, int] = {}
        self._agent_tool_latencies: dict[str, list[float]] = {}
        self._agent_tool_failures: dict[str, int] = {}
        self._agent_approval_counts: dict[str, int] = {}  # "SAFE_approved" etc.
        self._agent_chain_length_buckets: dict[str, int] = {}
        self._agent_chain_cap_hits: dict[str, int] = {}
        self._agent_chain_lengths: list[int] = []

        # Phase 10B: Messaging platform health
        self._messaging_events: dict[str, dict[str, int]] = {}  # platform -> {event -> count}

        # Pipeline efficiency tracking
        self._efficiency_total_requests: int = 0
        self._efficiency_local_requests: int = 0  # 0-token routes
        self._efficiency_tokens_used: int = 0
        self._efficiency_tokens_saved: int = 0
        self._efficiency_baseline_tokens: int = 0
        self._efficiency_by_route: dict[str, int] = {}  # route -> count
        self._efficiency_latency: list[float] = []

        # Multi-turn conversation tracking (WS7)
        self._conversation_depth_samples: list[int] = []  # turn counts
        self._conversation_cumulative_tokens: list[int] = []  # cumulative per convo

        # LLM cost tracking (for telemetry blob)
        self._llm_total_requests: int = 0
        self._llm_total_cost_microdollars: int = 0
        self._llm_input_tokens: int = 0
        self._llm_output_tokens: int = 0
        self._llm_cache_read_tokens: int = 0
        self._llm_cache_write_tokens: int = 0
        self._llm_web_search_requests: int = 0
        self._llm_latency_values: list[float] = []
        self._llm_by_tier: dict[str, int] = {}  # tier -> request count
        self._llm_by_type: dict[str, int] = {}  # request_type -> request count
        self._llm_cost_by_tier: dict[str, int] = {}  # tier -> cost microdollars
        self._llm_unknown_model_warning: bool = False
        # Per-model usage rows (parity with Claude Code's ModelUsage map).
        # Keyed by canonical model name → {input, output, cache_read, cache_write,
        # web_search, cost_microdollars, requests}.
        self._llm_by_model: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------
    # Generic increment methods
    # ------------------------------------------------------------------

    def increment(self, counter_name: str) -> None:
        """Increment a named counter by 1."""
        with self._lock:
            self._increment_internal(counter_name)

    def increment_n(self, counter_name: str, n: int) -> None:
        """Increment a named counter by n."""
        with self._lock:
            for _ in range(n):
                self._increment_internal(counter_name)

    def _increment_internal(self, counter_name: str) -> None:
        """Internal increment without lock (caller must hold lock)."""
        if counter_name == "commands_total":
            self._commands_total += 1
            # Track daily engagement
            today = time.strftime("%Y-%m-%d")
            self._active_days.add(today)
            self._commands_per_day[today] = self._commands_per_day.get(today, 0) + 1
            # Track hour histogram
            hour = int(time.strftime("%H"))
            self._hour_histogram[hour] += 1
        elif counter_name == "commands_rule_matched":
            self._commands_rule_matched += 1
        elif counter_name == "commands_llm_routed":
            self._commands_llm_routed += 1
        elif counter_name == "commands_agent_tasks":
            self._commands_agent_tasks += 1
        elif counter_name == "commands_agent_succeeded":
            self._commands_agent_succeeded += 1
        elif counter_name == "commands_succeeded":
            self._commands_succeeded += 1
        elif counter_name == "commands_failed":
            self._commands_failed += 1
        elif counter_name == "wake_activations":
            now = time.monotonic()
            self._wake_activations += 1
            # Check for rapid reactivation (< 10s gap)
            if self._last_wake_time > 0 and (now - self._last_wake_time) < 10.0:
                self._wake_rapid_reactivations += 1
            self._last_wake_time = now
        elif counter_name == "wake_activations_during_playback":
            self._wake_activations_during_playback += 1
        elif counter_name == "wake_false_positive_reports":
            self._wake_false_positive_reports += 1
        elif counter_name == "wake_activation_to_command":
            self._wake_activation_to_command += 1
        elif counter_name == "cache_hits":
            self._cache_hits += 1
        elif counter_name == "cache_misses":
            self._cache_misses += 1
        elif counter_name == "sessions_started":
            self._sessions_started += 1
        elif counter_name == "sessions_clean_exits":
            self._sessions_clean_exits += 1
        elif counter_name == "session_crashes":
            self._session_crashes += 1
        elif counter_name == "crashes":
            self._crashes += 1
        elif counter_name == "unhandled_exceptions":
            self._unhandled_exceptions += 1
        elif counter_name == "llm_timeouts":
            self._llm_timeouts += 1
        elif counter_name == "playback_failures":
            self._playback_failures += 1

    # ------------------------------------------------------------------
    # Specialized increment methods
    # ------------------------------------------------------------------

    def increment_gate_hit(self, gate_name: str) -> None:
        """Record a feature gate denial."""
        with self._lock:
            self._gate_hits[gate_name] = self._gate_hits.get(gate_name, 0) + 1

    def increment_command_category(self, category: str) -> None:
        """Record a command in a specific category."""
        with self._lock:
            self._commands_by_category[category] = self._commands_by_category.get(category, 0) + 1

    def increment_music_provider(self, provider: str) -> None:
        """Record music playback from a provider."""
        with self._lock:
            self._music_provider_counts[provider] = self._music_provider_counts.get(provider, 0) + 1

    def record_error(self, error_code: str) -> None:
        """Record an error occurrence."""
        with self._lock:
            self._error_count += 1
            self._error_codes[error_code] = self._error_codes.get(error_code, 0) + 1

    def record_feature_used(self, feature: str) -> None:
        """Record that a feature was used (increments counter)."""
        with self._lock:
            self._features_used[feature] = self._features_used.get(feature, 0) + 1

    # ------------------------------------------------------------------
    # Multi-room sync metrics (Phase 9)
    # ------------------------------------------------------------------

    def record_sync_drift(self, drift_ms: float) -> None:
        """Record a multi-room sync drift measurement."""
        with self._lock:
            self._sync_drift_values_ms.append(drift_ms)
            if len(self._sync_drift_values_ms) > 10000:
                self._sync_drift_values_ms[:] = self._sync_drift_values_ms[-5000:]

    def increment_sync_correction(self, correction_type: str) -> None:
        """Record a sync correction (pause, seek, reload, degrade)."""
        with self._lock:
            self._sync_corrections[correction_type] = self._sync_corrections.get(correction_type, 0) + 1

    def increment_sync_spoke(self, event: str) -> None:
        """Record a spoke event (connect, disconnect, reconnect, late_join)."""
        with self._lock:
            self._sync_spoke_events[event] = self._sync_spoke_events.get(event, 0) + 1

    # ------------------------------------------------------------------
    # Per-tool agent tracking (Phase 10A)
    # ------------------------------------------------------------------

    def increment_agent_tool(self, tool_name: str, *, latency_ms: float, success: bool) -> None:
        """Record an individual agent tool call."""
        with self._lock:
            self._agent_tool_calls[tool_name] = self._agent_tool_calls.get(tool_name, 0) + 1
            if tool_name not in self._agent_tool_latencies:
                self._agent_tool_latencies[tool_name] = []
            lat_list = self._agent_tool_latencies[tool_name]
            lat_list.append(latency_ms)
            if len(lat_list) > 1000:
                lat_list[:] = lat_list[-500:]
            if not success:
                self._agent_tool_failures[tool_name] = self._agent_tool_failures.get(tool_name, 0) + 1

    def increment_approval(self, tier: str, *, approved: bool) -> None:
        """Record an approval flow outcome."""
        with self._lock:
            key = f"{tier}_{'approved' if approved else 'rejected'}"
            self._agent_approval_counts[key] = self._agent_approval_counts.get(key, 0) + 1

    @staticmethod
    def _chain_length_bucket(chain_length: int) -> str:
        if chain_length <= 0:
            return "0"
        if chain_length <= 5:
            return "1-5"
        if chain_length <= 25:
            return "6-25"
        if chain_length <= 75:
            return "26-75"
        return "76+"

    def record_agent_chain(
        self,
        *,
        chain_length: int,
        cap: int | None,
        plan_family: str,
        outcome: str,
        capped: bool,
    ) -> None:
        """Record completed agent chain length for diagnostics."""
        bucket = self._chain_length_bucket(chain_length)
        plan_key = str(plan_family or "unknown").lower()
        with self._lock:
            self._agent_chain_length_buckets[bucket] = self._agent_chain_length_buckets.get(bucket, 0) + 1
            self._agent_chain_lengths.append(max(0, int(chain_length)))
            if len(self._agent_chain_lengths) > 1000:
                self._agent_chain_lengths[:] = self._agent_chain_lengths[-500:]
            if capped:
                self._agent_chain_cap_hits[plan_key] = self._agent_chain_cap_hits.get(plan_key, 0) + 1
            outcome_key = "outcome:%s" % (outcome or "unknown")
            self._agent_chain_cap_hits[outcome_key] = self._agent_chain_cap_hits.get(outcome_key, 0) + 1
            if cap is not None:
                cap_key = "cap:%s" % cap
                self._agent_chain_cap_hits[cap_key] = self._agent_chain_cap_hits.get(cap_key, 0) + 1

    # ------------------------------------------------------------------
    # Messaging platform health (Phase 10B)
    # ------------------------------------------------------------------

    def increment_messaging(self, platform: str, event: str) -> None:
        """Record a messaging platform health event."""
        with self._lock:
            if platform not in self._messaging_events:
                self._messaging_events[platform] = {}
            plat = self._messaging_events[platform]
            plat[event] = plat.get(event, 0) + 1

    # ------------------------------------------------------------------
    # Pipeline efficiency tracking
    # ------------------------------------------------------------------

    def record_conversation_turn(
        self,
        *,
        turn_depth: int,
        cumulative_tokens: int,
    ) -> None:
        """Record multi-turn conversation metrics (WS7).

        Args:
            turn_depth: The turn number in the current conversation (1-indexed).
            cumulative_tokens: Total tokens used in this conversation so far.
        """
        with self._lock:
            self._conversation_depth_samples.append(turn_depth)
            if len(self._conversation_depth_samples) > 10000:
                self._conversation_depth_samples[:] = self._conversation_depth_samples[-5000:]
            self._conversation_cumulative_tokens.append(cumulative_tokens)
            if len(self._conversation_cumulative_tokens) > 10000:
                self._conversation_cumulative_tokens[:] = self._conversation_cumulative_tokens[-5000:]

    def record_pipeline_request(
        self,
        *,
        route: str,
        total_tokens: int = 0,
        baseline_tokens: int = 0,
        tokens_saved: int = 0,
        latency_ms: float = 0,
    ) -> None:
        """Record a pipeline request for efficiency tracking."""
        with self._lock:
            self._efficiency_total_requests += 1
            self._efficiency_tokens_used += total_tokens
            self._efficiency_baseline_tokens += baseline_tokens
            self._efficiency_tokens_saved += tokens_saved
            self._efficiency_by_route[route] = self._efficiency_by_route.get(route, 0) + 1
            if total_tokens == 0:
                self._efficiency_local_requests += 1
            if latency_ms > 0:
                self._efficiency_latency.append(latency_ms)
                if len(self._efficiency_latency) > 10000:
                    self._efficiency_latency[:] = self._efficiency_latency[-5000:]

    # ------------------------------------------------------------------
    # LLM cost tracking
    # ------------------------------------------------------------------

    def record_llm_usage(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        web_search_requests: int = 0,
        cost_microdollars: int = 0,
        latency_ms: float = 0,
        tier: str = "free",
        request_type: str = "simple_command",
        model: str | None = None,
    ) -> None:
        """Record an LLM API call with token counts and cost.

        ``web_search_requests`` is the count of web-search server-tool
        invocations (Anthropic ``server_tool_use.web_search_requests``);
        ``cache_write_tokens`` is cache-creation tokens
        (``cache_creation_input_tokens``). Both feed into the cost-summary
        parity with Claude Code's per-session model usage map.
        """
        with self._lock:
            self._llm_total_requests += 1
            self._llm_total_cost_microdollars += cost_microdollars
            self._llm_input_tokens += input_tokens
            self._llm_output_tokens += output_tokens
            self._llm_cache_read_tokens += cache_read_tokens
            self._llm_cache_write_tokens += cache_write_tokens
            self._llm_web_search_requests += web_search_requests
            if latency_ms > 0:
                self._llm_latency_values.append(latency_ms)
                if len(self._llm_latency_values) > 10000:
                    self._llm_latency_values[:] = self._llm_latency_values[-5000:]
            self._llm_by_tier[tier] = self._llm_by_tier.get(tier, 0) + 1
            self._llm_by_type[request_type] = self._llm_by_type.get(request_type, 0) + 1
            self._llm_cost_by_tier[tier] = self._llm_cost_by_tier.get(tier, 0) + cost_microdollars
            if model:
                row = self._llm_by_model.setdefault(
                    model,
                    {
                        "requests": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_tokens": 0,
                        "cache_write_tokens": 0,
                        "web_search_requests": 0,
                        "cost_microdollars": 0,
                    },
                )
                row["requests"] += 1
                row["input_tokens"] += input_tokens
                row["output_tokens"] += output_tokens
                row["cache_read_tokens"] += cache_read_tokens
                row["cache_write_tokens"] += cache_write_tokens
                row["web_search_requests"] += web_search_requests
                row["cost_microdollars"] += cost_microdollars

    def mark_unknown_model_cost(self) -> None:
        """Mark that an unknown model was encountered during cost calc.

        Surfaced in ``snapshot()['llm']['unknown_model_warning']`` so any
        consumer that renders cost totals can append the
        ``costs may be inaccurate`` suffix.
        """
        with self._lock:
            self._llm_unknown_model_warning = True

    # ------------------------------------------------------------------
    # Latency recording
    # ------------------------------------------------------------------

    def record_latency(self, stage: str, latency_ms: float) -> None:
        """Record a latency measurement for a pipeline stage.

        stage: 'latency_wake_to_stt' | 'latency_stt' | 'latency_intent_rule' |
               'latency_intent_llm' | 'latency_tts' | 'latency_end_to_end'
        """
        with self._lock:
            target = getattr(self, f"_{stage}", None)
            if isinstance(target, list):
                target.append(latency_ms)
                # Cap at 10000 samples to bound memory
                if len(target) > 10000:
                    target[:] = target[-5000:]

    # ------------------------------------------------------------------
    # Snapshot methods (for reporter)
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Take a consistent snapshot of all counters.

        Returns a dict suitable for building the telemetry blob.
        Does NOT reset counters — call reset() separately after successful send.
        """
        with self._lock:
            return {
                "commands": {
                    "total": self._commands_total,
                    "rule_matched": self._commands_rule_matched,
                    "llm_routed": self._commands_llm_routed,
                    "agent_tasks": self._commands_agent_tasks,
                    "agent_succeeded": self._commands_agent_succeeded,
                    "succeeded": self._commands_succeeded,
                    "failed": self._commands_failed,
                    "by_category": dict(self._commands_by_category),
                },
                "wake_word": {
                    "activations": self._wake_activations,
                    "activations_during_playback": self._wake_activations_during_playback,
                    "false_positive_reports": self._wake_false_positive_reports,
                    "rapid_reactivations": self._wake_rapid_reactivations,
                    "activation_to_command": self._wake_activation_to_command,
                },
                "latency": {
                    "wake_to_stt": list(self._latency_wake_to_stt),
                    "stt": list(self._latency_stt),
                    "intent_rule": list(self._latency_intent_rule),
                    "intent_llm": list(self._latency_intent_llm),
                    "tts": list(self._latency_tts),
                    "end_to_end": list(self._latency_end_to_end),
                },
                "cache": {
                    "hits": self._cache_hits,
                    "misses": self._cache_misses,
                },
                "gate_hits": dict(self._gate_hits),
                "errors": {
                    "count": self._error_count,
                    "codes": list(self._error_codes.keys()),
                    "code_counts": dict(self._error_codes),
                },
                "music_provider_counts": dict(self._music_provider_counts),
                "engagement": {
                    "active_days": len(self._active_days),
                    "commands_per_day": dict(self._commands_per_day),
                    "hour_histogram": list(self._hour_histogram),
                },
                "features_used": dict(self._features_used),
                "health": {
                    "sessions_started": self._sessions_started,
                    "sessions_clean_exits": self._sessions_clean_exits,
                    "session_crashes": self._session_crashes,
                    "crashes": self._crashes,
                    "unhandled_exceptions": self._unhandled_exceptions,
                    "llm_timeouts": self._llm_timeouts,
                    "playback_failures": self._playback_failures,
                },
                "multi_room": {
                    "drift_values_ms": list(self._sync_drift_values_ms),
                    "corrections": dict(self._sync_corrections),
                    "spoke_events": dict(self._sync_spoke_events),
                },
                "agent": {
                    "tool_calls": dict(self._agent_tool_calls),
                    "tool_latencies": {k: list(v) for k, v in self._agent_tool_latencies.items()},
                    "tool_failures": dict(self._agent_tool_failures),
                    "approval_counts": dict(self._agent_approval_counts),
                    "chain_lengths": {
                        "buckets": dict(self._agent_chain_length_buckets),
                        "cap_hits": {
                            k: v
                            for k, v in self._agent_chain_cap_hits.items()
                            if not k.startswith("outcome:") and not k.startswith("cap:")
                        },
                        "outcomes": {
                            k.removeprefix("outcome:"): v
                            for k, v in self._agent_chain_cap_hits.items()
                            if k.startswith("outcome:")
                        },
                        "caps": {
                            k.removeprefix("cap:"): v
                            for k, v in self._agent_chain_cap_hits.items()
                            if k.startswith("cap:")
                        },
                        "samples": len(self._agent_chain_lengths),
                        "max": (max(self._agent_chain_lengths) if self._agent_chain_lengths else 0),
                        "avg": (
                            round(
                                sum(self._agent_chain_lengths) / len(self._agent_chain_lengths),
                                1,
                            )
                            if self._agent_chain_lengths
                            else 0.0
                        ),
                    },
                },
                "messaging": {k: dict(v) for k, v in self._messaging_events.items()},
                "efficiency": {
                    "total_requests": self._efficiency_total_requests,
                    "local_requests": self._efficiency_local_requests,
                    "tokens_used": self._efficiency_tokens_used,
                    "baseline_tokens": self._efficiency_baseline_tokens,
                    "tokens_saved": self._efficiency_tokens_saved,
                    "by_route": dict(self._efficiency_by_route),
                    "latency_values": list(self._efficiency_latency),
                    "conversation_depth_samples": list(self._conversation_depth_samples),
                    "conversation_cumulative_tokens": list(self._conversation_cumulative_tokens),
                },
                "llm": {
                    "total_requests": self._llm_total_requests,
                    "total_cost_microdollars": self._llm_total_cost_microdollars,
                    "input_tokens": self._llm_input_tokens,
                    "output_tokens": self._llm_output_tokens,
                    "cache_read_tokens": self._llm_cache_read_tokens,
                    "cache_write_tokens": self._llm_cache_write_tokens,
                    "web_search_requests": self._llm_web_search_requests,
                    "latency_values": list(self._llm_latency_values),
                    "by_tier": dict(self._llm_by_tier),
                    "by_type": dict(self._llm_by_type),
                    "cost_by_tier": dict(self._llm_cost_by_tier),
                    "by_model": {model: dict(row) for model, row in self._llm_by_model.items()},
                    "unknown_model_warning": self._llm_unknown_model_warning,
                },
            }

    def reset(self) -> None:
        """Reset all counters to zero. Call after successful telemetry send."""
        with self._lock:
            self._commands_total = 0
            self._commands_rule_matched = 0
            self._commands_llm_routed = 0
            self._commands_agent_tasks = 0
            self._commands_agent_succeeded = 0
            self._commands_succeeded = 0
            self._commands_failed = 0
            self._commands_by_category.clear()
            self._wake_activations = 0
            self._wake_activations_during_playback = 0
            self._wake_false_positive_reports = 0
            self._wake_rapid_reactivations = 0
            self._wake_activation_to_command = 0
            self._latency_wake_to_stt.clear()
            self._latency_stt.clear()
            self._latency_intent_rule.clear()
            self._latency_intent_llm.clear()
            self._latency_tts.clear()
            self._latency_end_to_end.clear()
            self._cache_hits = 0
            self._cache_misses = 0
            self._gate_hits.clear()
            self._error_count = 0
            self._error_codes.clear()
            self._music_provider_counts.clear()
            self._active_days.clear()
            self._commands_per_day.clear()
            self._hour_histogram = [0] * 24
            self._features_used.clear()
            self._sessions_started = 0
            self._sessions_clean_exits = 0
            self._session_crashes = 0
            self._crashes = 0
            self._unhandled_exceptions = 0
            self._llm_timeouts = 0
            self._playback_failures = 0
            self._sync_drift_values_ms.clear()
            self._sync_corrections.clear()
            self._sync_spoke_events.clear()
            self._agent_tool_calls.clear()
            self._agent_tool_latencies.clear()
            self._agent_tool_failures.clear()
            self._agent_approval_counts.clear()
            self._agent_chain_length_buckets.clear()
            self._agent_chain_cap_hits.clear()
            self._agent_chain_lengths.clear()
            self._messaging_events.clear()
            self._efficiency_total_requests = 0
            self._efficiency_local_requests = 0
            self._efficiency_tokens_used = 0
            self._efficiency_tokens_saved = 0
            self._efficiency_baseline_tokens = 0
            self._efficiency_by_route.clear()
            self._efficiency_latency.clear()
            self._conversation_depth_samples.clear()
            self._conversation_cumulative_tokens.clear()
            self._llm_total_requests = 0
            self._llm_total_cost_microdollars = 0
            self._llm_input_tokens = 0
            self._llm_output_tokens = 0
            self._llm_cache_read_tokens = 0
            self._llm_cache_write_tokens = 0
            self._llm_web_search_requests = 0
            self._llm_latency_values.clear()
            self._llm_by_tier.clear()
            self._llm_by_type.clear()
            self._llm_cost_by_tier.clear()
            self._llm_by_model.clear()
            self._llm_unknown_model_warning = False
