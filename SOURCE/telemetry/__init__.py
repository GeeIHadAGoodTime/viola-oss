"""Opt-in telemetry module — privacy-first, default OFF.

Lifecycle and retention contract
--------------------------------

This module is the *only* path by which desktop installs send aggregated
metrics to the operator's cloud. Everything here is in-memory; nothing is
written to disk on the desktop. The operator analytics SQLite (METRICS-1)
is cloud-surface-only, fed by uploads received from this pipeline.

Components:

- ``TelemetryAccumulator`` (singleton) — thread-safe, in-memory counters.
  Lives for the process lifetime. All list-typed fields (latency samples,
  drift values, conversation depth, etc.) are bounded with a soft cap
  (typically 10K samples, halved to 5K on overflow). No PII, no command
  text, no voice data. Dies with the process.

- ``TelemetryReporter`` — builds the upload blob from an accumulator
  snapshot, coarsens exact integer counts with bounded ±5% random jitter
  (light obfuscation — NOT differential privacy; see ``reporter._apply_noise``),
  and POSTs over https to ``{telemetry_server_url}/api/telemetry/ingest``
  (plain http allowed only for loopback). On HTTP 2xx, calls
  ``accumulator.reset()`` so the next cycle starts clean.

- ``TelemetryScheduler`` — daemon thread, fires every
  ``telemetry_send_interval_hours`` (default 4h) plus 0–60min jitter.
  Performs a final-send on graceful shutdown.

Send gates (all three must be True):

1. The user's telemetry opt-in — canonically SettingsManager's
   ``telemetry_opt_in`` (settings.json, the runtime truth for user
   preferences per CLAUDE.md Settings Resolution), defaults False.
   ``AppConfig.telemetry_enabled`` (.env) is NOT the authority for this gate
   — see ``telemetry.reporter._is_telemetry_enabled_by_user`` (#2175).
2. ``settings.telemetry_server_url`` — non-empty.
3. ``core.privacy_consent.is_error_reporting_consented()`` — explicit
   user consent (GDPR defense-in-depth).

Failure handling:

- A failed send (HTTP non-2xx or timeout) does NOT reset the accumulator,
  so the next cycle includes the prior period plus the new period. The
  accumulator's per-list cap prevents unbounded growth even across long
  outages.
- Consecutive failures are tracked on the reporter and surfaced via
  ``/v1/telemetry/status`` so a silent-failure outage is visible to the
  operator.

Cloud-side retention:

- Retention of received blobs is the operator's policy decision, owned by
  the cloud-side ingestion handler at ``admin.routes.create_public_router``
  → ``/api/telemetry/ingest``. This module does not impose or describe
  cloud retention.

What is NOT in this pipeline:

- The legacy operator analytics SQLite (``metrics.db`` / ``MetricsDB``)
  is cloud-surface-only as of METRICS-1. Desktop installs return a
  ``_NoOpMetricsWriter`` from ``admin.metrics_writer.get_metrics_writer``;
  no SQLite path is ever opened on a user's disk.
"""

from __future__ import annotations

import threading

from telemetry.accumulator import TelemetryAccumulator

_accumulator: TelemetryAccumulator | None = None
_accumulator_lock = threading.Lock()


def get_accumulator() -> TelemetryAccumulator:
    """Get or create the global telemetry accumulator singleton."""
    global _accumulator
    if _accumulator is None:
        with _accumulator_lock:
            if _accumulator is None:
                _accumulator = TelemetryAccumulator()
    return _accumulator
