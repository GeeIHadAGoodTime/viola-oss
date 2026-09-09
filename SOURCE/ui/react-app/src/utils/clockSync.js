/**
 * NTP-style 4-timestamp Clock Synchronization for multi-room audio sync.
 *
 * Synchronizes the browser spoke's clock with the Hub's monotonic clock
 * so that PCM chunk play_at timestamps can be accurately scheduled.
 *
 * Clock reference: uses audioContext.currentTime (audio hardware clock)
 * when available and running, falling back to performance.now()/1000.
 * This ensures the offset maps directly between hub time and the clock
 * domain used for scheduling AudioBufferSourceNodes, eliminating the
 * perfToCtx bridge calculation that introduced drift.
 *
 * Protocol (all timestamps in seconds):
 *   t1 = spoke send time    (audioContext.currentTime or performance.now()/1000)
 *   t2 = hub receive time   (time.monotonic())
 *   t3 = hub send time      (time.monotonic())
 *   t4 = spoke receive time (audioContext.currentTime or performance.now()/1000)
 *
 * Computations:
 *   RTT    = (t4 - t1) - (t3 - t2)        // network round-trip
 *   offset = ((t2 - t1) + (t3 - t4)) / 2  // hub_clock - spoke_clock
 *
 * Usage:
 *   const sync = new ClockSync(websocket, audioContext);
 *   await sync.synchronize();        // runs N probes, takes median
 *   const hubNow = sync.toHubTime(); // convert local time to hub time
 *   const localT = sync.toLocalTime(playAt); // convert hub play_at to local
 *
 * The offset accounts for asymmetric WiFi latency (upload != download).
 */

// ------------------------------------------------------------------ //
// Configuration                                                       //
// ------------------------------------------------------------------ //

/** Number of clock probes per sync round */
const PROBE_COUNT = 7;

/** Delay between probes (ms) */
const PROBE_INTERVAL_MS = 100;

/**
 * Maximum RTT to accept a probe (ms) — STARTUP phase.
 *
 * During startup (before we have ``_BASELINE_SAMPLE_COUNT`` kept probes)
 * accept any probe up to 2 s.  This is intentionally generous so cloud /
 * cellular spokes (RTT 200–500 ms) can sync at all, where the old hard
 * 200 ms cap rejected every probe and left the spoke playing with
 * offset=0 forever.  The payload midpoint estimator is noisier at high
 * RTT, so the drift corrector does the heavy lifting of absorbing any
 * residual error — better degraded-sync than silent-no-sync.
 */
const STARTUP_MAX_RTT_MS = 2000;

/**
 * Steady-state RTT cap is ``max(STEADY_FLOOR_MS, baseline_rtt × MULT)``.
 *
 * Once we have a baseline (median of ``_BASELINE_SAMPLE_COUNT`` kept
 * probes), we tighten the filter to twice that baseline — on a LAN that
 * collapses to ~200 ms, and on a wide-area link it scales up to roughly
 * the real network's jitter ceiling.  The floor stops the cap from
 * pinching the happy-path LAN case.
 */
const STEADY_FLOOR_MS = 200;
const STEADY_BASELINE_MULT = 2;

/**
 * Kept-probe count required to exit startup mode and lock a baseline.
 * Ten samples give a stable median while keeping warm-up short.
 */
const BASELINE_SAMPLE_COUNT = 10;

/** Rolling RTT history depth for baseline computation (samples). */
const RTT_HISTORY_DEPTH = 20;

/** Minimum valid probes required for a sync round to succeed */
const MIN_VALID_PROBES = 1;

/** Periodic re-sync interval (ms). 5 seconds gives 12 samples/min for drift rate. */
const RESYNC_INTERVAL_MS = 5_000;

/** Number of recent offset samples to keep for drift rate calculation */
const DRIFT_WINDOW_SIZE = 60;

/** EMA smoothing factor for drift rate (0..1). Lower = smoother, slower. */
const DRIFT_EMA_ALPHA = 0.3;

// ------------------------------------------------------------------ //
// ClockSync                                                           //
// ------------------------------------------------------------------ //

/**
 * @typedef {Object} SyncProbe
 * @property {number} t1 - Spoke send time (seconds)
 * @property {number} t2 - Hub receive time (seconds)
 * @property {number} t3 - Hub send time (seconds)
 * @property {number} t4 - Spoke receive time (seconds)
 * @property {number} rtt - Round-trip time (seconds)
 * @property {number} offset - Clock offset (seconds)
 */

export class ClockSync {
  /**
   * @param {WebSocket} ws - The audio-stream WebSocket (text messages
   *   are used for clock sync alongside binary PCM frames).
   * @param {AudioContext} [audioCtx] - AudioContext whose currentTime is
   *   used as the spoke clock reference. When running, this is the audio
   *   hardware clock — the same oscillator that drives playback scheduling.
   *   Falls back to performance.now()/1000 if null or suspended.
   */
  constructor(ws, audioCtx = null) {
    /** @type {WebSocket} */
    this._ws = ws;

    /** @type {AudioContext|null} */
    this._audioCtx = audioCtx;

    /** @type {number} Current best offset estimate (hub_clock - spoke_clock) in seconds */
    this._offset = 0;

    /** @type {number} Current best RTT estimate in seconds */
    this._rtt = 0;

    /** @type {boolean} Whether at least one sync has completed */
    this._synced = false;

    /** @type {SyncProbe[]} All probes from the most recent sync round */
    this._lastProbes = [];

    /** @type {number|null} Re-sync interval ID */
    this._resyncTimer = null;

    /** @type {Map<number, {resolve: Function, t1: number}>} Pending probe callbacks keyed by t1 */
    this._pendingProbes = new Map();

    // Drift rate tracking: window of (localTime, offset) pairs from re-syncs.
    // localTime is _now() when the sync completed.
    /** @type {{time: number, offset: number}[]} */
    this._offsetHistory = [];

    /** @type {number} Smoothed drift rate in ppm (EMA-filtered) */
    this._driftRatePpm = 0;

    /** @type {number} Raw (unsmoothed) drift rate in ppm from latest window */
    this._rawDriftRatePpm = 0;

    /**
     * Rolling RTT history for the adaptive MAX_RTT_MS filter.  Only
     * successful, kept-in-median probes contribute.  Seconds.
     * @type {number[]}
     */
    this._rttHistory = [];

    /**
     * Computed baseline RTT (median of _rttHistory) once we have
     * ``BASELINE_SAMPLE_COUNT`` samples.  NaN until baseline is locked.
     * Seconds.
     * @type {number}
     */
    this._baselineRttSec = NaN;

    // Listen for clock_sync_reply messages
    this._onMessage = this._onMessage.bind(this);
    this._ws.addEventListener('message', this._onMessage);
  }

  /**
   * Return the current maximum acceptable RTT for a probe, in seconds.
   *
   * During startup (fewer than ``BASELINE_SAMPLE_COUNT`` kept probes),
   * this is ``STARTUP_MAX_RTT_MS`` — lenient so cloud spokes can sync.
   * Once a baseline is established, it tightens to
   * ``max(STEADY_FLOOR_MS, baseline × STEADY_BASELINE_MULT)``.
   *
   * @returns {number} Maximum acceptable RTT in seconds.
   */
  _getMaxRttSec() {
    if (Number.isNaN(this._baselineRttSec)) {
      return STARTUP_MAX_RTT_MS / 1000;
    }
    const steadyMs = Math.max(
      STEADY_FLOOR_MS,
      this._baselineRttSec * 1000 * STEADY_BASELINE_MULT,
    );
    return steadyMs / 1000;
  }

  /**
   * Return the current locked baseline RTT in seconds, or NaN if
   * startup is still in progress.  Exposed for diagnostics / a future
   * sync-quality indicator.
   *
   * @returns {number}
   */
  getBaselineRttSec() {
    return this._baselineRttSec;
  }

  /**
   * Push a successful probe's RTT into the rolling history and lock
   * the baseline once we have enough samples.  Called by
   * ``synchronize()`` for each probe accepted into the median.
   *
   * @param {number} rttSec
   * @private
   */
  _recordProbeRtt(rttSec) {
    this._rttHistory.push(rttSec);
    if (this._rttHistory.length > RTT_HISTORY_DEPTH) {
      this._rttHistory.shift();
    }
    if (
      Number.isNaN(this._baselineRttSec)
      && this._rttHistory.length >= BASELINE_SAMPLE_COUNT
    ) {
      this._baselineRttSec = _median(this._rttHistory);
    }
  }

  /**
   * Set or update the AudioContext reference.
   *
   * Useful when the AudioContext is created after ClockSync, or when
   * a new AudioContext replaces a closed one.
   *
   * @param {AudioContext|null} audioCtx
   */
  setAudioContext(audioCtx) {
    this._audioCtx = audioCtx;
  }

  // ---------------------------------------------------------------- //
  // Public API                                                        //
  // ---------------------------------------------------------------- //

  /**
   * Run a full synchronization round (multiple probes, median filter).
   *
   * @param {number} [probeCount=PROBE_COUNT] Number of probes to send.
   * @returns {Promise<{offset: number, rtt: number, probes: number}>}
   *   Resolved when sync completes or rejects if too few valid probes.
   */
  async synchronize(probeCount = PROBE_COUNT) {
    const probes = [];
    const maxRttSec = this._getMaxRttSec();

    for (let i = 0; i < probeCount; i++) {
      try {
        const probe = await this._sendProbe();
        // Adaptive RTT filter: generous at startup so cloud spokes can
        // sync at all, tightens to ~2× baseline once locked.
        if (probe.rtt <= maxRttSec) {
          probes.push(probe);
          this._recordProbeRtt(probe.rtt);
        }
      } catch {
        // Probe timed out or WS error — skip
      }

      // Wait between probes (skip after the last one)
      if (i < probeCount - 1) {
        await _sleep(PROBE_INTERVAL_MS);
      }
    }

    if (probes.length < MIN_VALID_PROBES) {
      throw new Error(
        `Clock sync failed: 0/${probeCount} valid probes ` +
        `(maxRttMs=${(maxRttSec * 1000).toFixed(0)})`
      );
    }

    // Sort by RTT — lower RTT probes are more accurate
    probes.sort((a, b) => a.rtt - b.rtt);

    // Take the median offset (robust to outliers)
    const offsets = probes.map(p => p.offset);
    const medianOffset = _median(offsets);

    // Take the minimum RTT as the best estimate
    const bestRtt = probes[0].rtt;

    this._offset = medianOffset;
    this._rtt = bestRtt;
    this._synced = true;
    this._lastProbes = probes;

    // Record offset for drift rate tracking
    this._recordOffset(this._now(), medianOffset);

    return {
      offset: medianOffset,
      rtt: bestRtt,
      probes: probes.length,
    };
  }

  /**
   * Start periodic re-synchronization.
   *
   * @param {number} [intervalMs=RESYNC_INTERVAL_MS] Interval between re-syncs.
   */
  startPeriodicSync(intervalMs = RESYNC_INTERVAL_MS) {
    this.stopPeriodicSync();
    this._resyncTimer = setInterval(async () => {
      try {
        await this.synchronize();
      } catch {
        // Re-sync failure is non-fatal; keep using the last good offset
      }
    }, intervalMs);
  }

  /** Stop periodic re-synchronization. */
  stopPeriodicSync() {
    if (this._resyncTimer !== null) {
      clearInterval(this._resyncTimer);
      this._resyncTimer = null;
    }
  }

  /**
   * Convert a local spoke time to hub time.
   *
   * @param {number} [localTimeSec] Local time in seconds (default: now).
   * @returns {number} Corresponding hub time in seconds.
   */
  toHubTime(localTimeSec) {
    const local = localTimeSec !== undefined ? localTimeSec : this._now();
    return local + this._offset;
  }

  /**
   * Convert a hub play_at timestamp to local spoke time.
   *
   * @param {number} hubTimeSec Hub time in seconds (from PCM chunk header).
   * @returns {number} Corresponding local time in seconds (audioContext.currentTime scale
   *   when AudioContext is running, performance.now()/1000 scale otherwise).
   */
  toLocalTime(hubTimeSec) {
    return hubTimeSec - this._offset;
  }

  /**
   * Get the current offset estimate.
   *
   * @returns {{offset: number, rtt: number, synced: boolean, probeCount: number}}
   */
  getState() {
    return {
      offset: this._offset,
      rtt: this._rtt,
      synced: this._synced,
      probeCount: this._lastProbes.length,
    };
  }

  /**
   * Get the current clock drift rate in parts per million (ppm).
   *
   * Positive ppm means the spoke clock runs faster than the hub clock
   * (offset is decreasing over time). Negative means spoke is slower.
   *
   * The value is EMA-smoothed to avoid noisy jumps from individual
   * re-sync measurements.
   *
   * @returns {number} Drift rate in ppm (0 if insufficient data).
   */
  getDriftRate() {
    return this._driftRatePpm;
  }

  /**
   * Get detailed drift diagnostics.
   *
   * @returns {{ppm: number, rawPpm: number, samples: number, windowSec: number}}
   */
  getDriftState() {
    const hist = this._offsetHistory;
    const windowSec = hist.length >= 2
      ? hist[hist.length - 1].time - hist[0].time
      : 0;
    return {
      ppm: this._driftRatePpm,
      rawPpm: this._rawDriftRatePpm,
      samples: hist.length,
      windowSec,
    };
  }

  // ---------------------------------------------------------------- //
  // Drift rate tracking (internal)                                    //
  // ---------------------------------------------------------------- //

  /**
   * Record a new (time, offset) sample and recompute drift rate.
   *
   * @param {number} localTimeSec - _now() when sync completed (audioCtx or perf fallback).
   * @param {number} offset - Median offset from the sync round (seconds).
   * @private
   */
  _recordOffset(localTimeSec, offset) {
    this._offsetHistory.push({ time: localTimeSec, offset });

    // Trim to window size
    while (this._offsetHistory.length > DRIFT_WINDOW_SIZE) {
      this._offsetHistory.shift();
    }

    // Need at least 2 samples spanning > 2 seconds for a meaningful slope
    if (this._offsetHistory.length < 2) {
      return;
    }

    const first = this._offsetHistory[0];
    const last = this._offsetHistory[this._offsetHistory.length - 1];
    const dt = last.time - first.time;

    if (dt < 2.0) {
      return; // Not enough time span
    }

    // Least-squares linear fit: offset = slope * time + intercept
    // slope = drift rate in seconds/second
    const rawSlope = this._leastSquaresSlope();

    // Convert to ppm: 1 ppm = 1e-6 seconds/second
    // slope is delta_offset / delta_time (seconds per second)
    // If offset = hub - spoke, and offset is increasing, spoke is falling behind
    // → spoke clock is slower → negative drift from spoke's perspective
    // Convention: positive ppm = spoke faster, so negate the slope
    const rawPpm = -rawSlope * 1e6;
    this._rawDriftRatePpm = rawPpm;

    // EMA smoothing
    if (this._offsetHistory.length <= 2) {
      // First measurement — initialize directly
      this._driftRatePpm = rawPpm;
    } else {
      this._driftRatePpm =
        DRIFT_EMA_ALPHA * rawPpm +
        (1 - DRIFT_EMA_ALPHA) * this._driftRatePpm;
    }
  }

  /**
   * Compute the least-squares slope of (time, offset) pairs.
   *
   * Uses relative times AND relative offsets to avoid catastrophic
   * cancellation with large absolute values (e.g., offset ~490,000s
   * when the hub has been running for days while the spoke page just loaded).
   *
   * @returns {number} Slope in seconds/second (delta_offset / delta_time).
   * @private
   */
  _leastSquaresSlope() {
    const n = this._offsetHistory.length;
    if (n < 2) return 0;

    // Use relative times AND relative offsets to preserve precision.
    // Without relative offsets, n*sumXY and sumX*sumY are both ~O(n*T*offset)
    // and their difference (the drift signal) is ~O(n*T*drift) — losing
    // ~log10(offset/drift) significant digits to subtractive cancellation.
    const t0 = this._offsetHistory[0].time;
    const y0 = this._offsetHistory[0].offset;

    let sumX = 0, sumY = 0, sumXX = 0, sumXY = 0;
    for (const sample of this._offsetHistory) {
      const x = sample.time - t0;
      const y = sample.offset - y0;
      sumX += x;
      sumY += y;
      sumXX += x * x;
      sumXY += x * y;
    }

    const denom = n * sumXX - sumX * sumX;
    if (Math.abs(denom) < 1e-15) return 0;

    return (n * sumXY - sumX * sumY) / denom;
  }

  /** Clean up resources. */
  destroy() {
    this.stopPeriodicSync();
    this._ws.removeEventListener('message', this._onMessage);
    // Reject any pending probes
    for (const [, pending] of this._pendingProbes) {
      pending.resolve(null);
    }
    this._pendingProbes.clear();
  }

  // ---------------------------------------------------------------- //
  // Internal                                                          //
  // ---------------------------------------------------------------- //

  /**
   * Get the current spoke time in seconds.
   *
   * Uses audioContext.currentTime when the AudioContext is available and
   * running — this is the audio hardware clock (same crystal oscillator
   * that drives DAC sample clocking). Falls back to performance.now()/1000
   * when the AudioContext is null, suspended, or closed.
   *
   * @returns {number} Current time in seconds.
   * @private
   */
  _now() {
    if (this._audioCtx && this._audioCtx.state === 'running') {
      return this._audioCtx.currentTime;
    }
    return performance.now() / 1000;
  }

  /**
   * Send a single clock sync probe and wait for the reply.
   *
   * @returns {Promise<SyncProbe>}
   * @private
   */
  _sendProbe() {
    return new Promise((resolve, reject) => {
      if (this._ws.readyState !== WebSocket.OPEN) {
        reject(new Error('WebSocket not open'));
        return;
      }

      const t1 = this._now(); // seconds (audioContext.currentTime or perf fallback)

      // Store callback keyed by t1 (echoed back by hub)
      this._pendingProbes.set(t1, { resolve, t1 });

      // Timeout: if hub doesn't reply in 2 seconds, reject
      const timeout = setTimeout(() => {
        this._pendingProbes.delete(t1);
        reject(new Error('Clock sync probe timed out'));
      }, 2000);

      // Attach timeout to pending entry for cleanup
      const entry = this._pendingProbes.get(t1);
      entry.timeout = timeout;

      this._ws.send(JSON.stringify({
        type: 'clock_sync',
        t1,
      }));
    });
  }

  /**
   * WebSocket message handler — dispatches clock_sync_reply messages.
   *
   * @param {MessageEvent} event
   * @private
   */
  _onMessage(event) {
    // Binary frames are PCM data — ignore
    if (typeof event.data !== 'string') {
      return;
    }

    // Capture t4 BEFORE JSON.parse to avoid 0.1-0.5ms parse bias.
    // The hub captures t2 before parsing; symmetry requires the same here.
    const t4 = this._now(); // spoke receive time (seconds)

    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }

    if (msg.type !== 'clock_sync_reply') {
      return;
    }

    const t1 = msg.t1; // echoed back by hub
    const t2 = msg.t2; // hub receive time
    const t3 = msg.t3; // hub send time

    // Look up the pending probe by t1
    const entry = this._pendingProbes.get(t1);
    if (!entry) {
      return; // Stale or duplicate reply
    }

    this._pendingProbes.delete(t1);
    clearTimeout(entry.timeout);

    // NTP 4-timestamp calculations
    const rtt = (t4 - t1) - (t3 - t2);
    const offset = ((t2 - t1) + (t3 - t4)) / 2;

    entry.resolve({ t1, t2, t3, t4, rtt, offset });
  }
}

// ------------------------------------------------------------------ //
// Helpers                                                             //
// ------------------------------------------------------------------ //

/**
 * Compute the median of a sorted or unsorted numeric array.
 *
 * @param {number[]} arr
 * @returns {number}
 */
function _median(arr) {
  const sorted = [...arr].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 0) {
    return (sorted[mid - 1] + sorted[mid]) / 2;
  }
  return sorted[mid];
}

/**
 * Promise-based sleep.
 *
 * @param {number} ms
 * @returns {Promise<void>}
 */
function _sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// ------------------------------------------------------------------ //
// Exports                                                             //
// ------------------------------------------------------------------ //

export {
  PROBE_COUNT,
  PROBE_INTERVAL_MS,
  STARTUP_MAX_RTT_MS,
  STEADY_FLOOR_MS,
  STEADY_BASELINE_MULT,
  BASELINE_SAMPLE_COUNT,
  RTT_HISTORY_DEPTH,
  MIN_VALID_PROBES,
  RESYNC_INTERVAL_MS,
  DRIFT_WINDOW_SIZE,
  DRIFT_EMA_ALPHA,
};
