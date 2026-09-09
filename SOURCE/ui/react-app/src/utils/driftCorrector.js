/**
 * Drift Corrector — sample-level interpolation for continuous drift correction.
 *
 * Two inputs, two distinct roles:
 *   1. Clock sync getDriftRate() — NTP-measured crystal oscillator drift (ppm).
 *      This is the BASE rate (clamped ±MAX_BASE_DRIFT_PPM).
 *   2. Buffer depth — a proportional correction term on smoothed depth error
 *      (dead-zoned, clamped ±MAX_BUFFER_CORRECTION_PPM). The buffer depth
 *      TREND (least-squares slope) is computed for diagnostics only and is
 *      never fed back into the controller — see _computeEffectiveDrift.
 *
 * Applies correction by interpolating samples:
 *   - Positive effectivePpm: accumulator grows, remove a sample when >= 1.0
 *     → chunk has 959 samples → _nextPlayTime advances less → spoke plays FASTER
 *   - Negative effectivePpm: accumulator shrinks, insert a sample when <= -1.0
 *     → chunk has 961 samples → _nextPlayTime advances more → spoke plays SLOWER
 *
 * Buffer correction sign convention (DO NOT NEGATE correctionTerm):
 *   - Buffer LOW → smoothedError negative → correctionTerm negative
 *     → effectivePpm decreases → ADD sample → spoke SLOWER → buffer FILLS ✓
 *   - Buffer HIGH → smoothedError positive → correctionTerm positive
 *     → effectivePpm increases → REMOVE sample → spoke FASTER → buffer DRAINS ✓
 *
 * Linear interpolation at chunk midpoint: (sample[i] + sample[i+1]) / 2,
 * applied to both L and R channels. One sample at 48 kHz = 20.83 us —
 * completely inaudible as a gap or overlap between scheduled chunks.
 *
 * Buffer depth target maintenance: proportional gain adds a correction term
 * only when the jitter buffer moves meaningfully away from the 280 ms target.
 * Normal scheduler/receive-queue motion stays inside the dead zone.
 */

// ------------------------------------------------------------------ //
// Constants                                                           //
// ------------------------------------------------------------------ //

/** Audio sample rate (Hz) */
const SAMPLE_RATE = 48000;

/** Samples per chunk (20 ms at 48 kHz) */
const SAMPLES_PER_CHUNK = 960;

/** Stereo channels */
const CHANNELS = 2;

/** Bytes per sample (int16) */
const BYTES_PER_SAMPLE = 2;

/** Target buffer depth in chunks (280ms / 20ms = 14 chunks).
 *  WiFi equilibrium sits at 260-340ms (13-17 chunks).  At 10 chunks the
 *  corrector permanently drains 3-7 excess chunks at 500-1500ppm, causing
 *  audible ~50Hz buzzing.  14 chunks sits at the equilibrium center. */
const BUFFER_TARGET_CHUNKS = 14;

/** Window size for buffer depth trend least-squares fit (chunks).
 *  5 seconds at 50 chunks/s. Longer window = less sensitivity to transient events. */
const BUFFER_TREND_WINDOW = 250;

/** Minimum samples before buffer trend is considered valid */
const BUFFER_TREND_MIN_SAMPLES = 50;

/**
 * Proportional gain for buffer depth target maintenance (ppm per chunk of error).
 *
 * At 250 ppm/chunk and 5 effective-error chunks (after dead zone):
 *   1250 ppm correction -> fill rate 0.063 chunks/s -> recovery in ~80s.
 *   Time constant τ = 20000/250 = 80s.
 *
 * History: 150 was the original value but had τ=133s — burst buffer drains
 * of 6+ chunks (common with WiFi jitter) took >120s to recover, consistently
 * failing the monitor.  300 caused ±1000ppm oscillation at 3-chunk target.
 * At 14-chunk target, the 4-chunk dead zone absorbs normal scheduler jitter.
 * 250 balances recovery speed for real underruns/overfills with oscillation
 * safety margin at the 14-chunk target.
 */
const BUFFER_DEPTH_CORRECTION_GAIN = 250;

/** Maximum total correction rate in ppm (base + buffer correction). 2500 ppm = 0.25% — inaudible. */
const MAX_CORRECTION_PPM = 2500;

/** Maximum buffer-depth-derived correction in ppm. 2000 ppm = 0.2% — inaudible. */
const MAX_BUFFER_CORRECTION_PPM = 2000;

/** Maximum change in active drift per update cycle (ppm). EMA smoothing on buffer
 *  depth already prevents jitter; this just caps the ramp rate for sudden changes.
 *  Reduced from 200 to 100 to prevent rapid oscillation swings. */
const MAX_DRIFT_CHANGE_PER_CYCLE = 100;

/**
 * Dead zone: if buffer depth is within this many chunks of target, buffer
 * correction is zero. With a 14-chunk target, 4 chunks covers normal browser
 * scheduler/receive-queue motion so the corrector does not convert ordinary
 * queue variation into hundreds of ppm of sample-rate drift.
 */
const BUFFER_DEAD_ZONE_CHUNKS_SHRINK = 4;
const BUFFER_DEAD_ZONE_CHUNKS_GROW = 4;

/** Legacy: symmetric dead zone export for backwards compatibility */
const BUFFER_DEAD_ZONE_CHUNKS = BUFFER_DEAD_ZONE_CHUNKS_SHRINK;

/** EMA smoothing factor for buffer depth correction.
 *  0.15 balances stability (peak already filters scheduler drain noise)
 *  with responsiveness to real buffer changes (~7s time constant). */
const BUFFER_DEPTH_EMA_ALPHA = 0.15;

/** Fast-track alpha when buffer drops significantly below smoothed depth.
 *  Triggers when peak is >3 chunks below smoothed.  0.6 tracks a crash
 *  in ~2 updates (1s) instead of ~4 updates (2s) at 0.4.  Faster
 *  detection means the buffer correction kicks in 1-2s earlier. */
const BUFFER_DEPTH_EMA_ALPHA_FAST = 0.6;

/** Peak buffer depth EMA window size (chunks). 25 chunks at 50 chunks/s = 500 ms. */
const _EMA_WINDOW_CHUNKS = 25;

/** EMA smoothing factor for buffer drift trend ppm (reduces jitter in slope estimate) */
const BUFFER_TREND_EMA_ALPHA = 0.1;

/** Maximum base drift rate in ppm.
 *  Devices typically drift ±50-300ppm.  AmazonBasics headphones have been
 *  measured at 280ppm, iPhones at 200-260ppm.  Previous cap of 200ppm
 *  caused persistent buffer drain on devices with >200ppm drift — the
 *  uncorrected residual (e.g. 80ppm for a 280ppm device) drains 1.67ms/s
 *  = 200ms over 120s, triggering monitor FAILs.  500ppm covers typical
 *  consumer hardware while still rejecting measurement noise spikes. */
const MAX_BASE_DRIFT_PPM = 500;

/**
 * Legacy: was used to switch between clock and buffer drift signals.
 * No longer used — base rate is always clock_drift_ppm, buffer depth
 * management is handled by correction_term. Kept for export compatibility.
 */
const AGREEMENT_THRESHOLD_PPM = 20;

/** Rolling window size for buffer depth min/median/max stats (60 s at 50 chunks/s) */
const DEPTH_STATS_WINDOW_SIZE = 3000;

/** Perturbation detection: if peak buffer depth changes by more than this many
 *  chunks between EMA windows, freeze buffer correction to prevent overcorrection.
 *  Set very high (500 = 10 seconds) so it only fires on true extreme events
 *  (full pipeline reset, not normal scheduler drain/trim cycles).
 *  The EMA smoothing itself handles ordinary transients without needing a freeze. */
const PERTURBATION_THRESHOLD_CHUNKS = 500;

/** Duration (ms) to freeze buffer correction after a perturbation is detected. */
const PERTURBATION_FREEZE_MS = 1000;

/**
 * Warmup duration (ms). During warmup, buffer-derived corrections are
 * suppressed because the jitter buffer hasn't filled to its target yet.
 * Clock-based drift correction still applies — clock drift is real and
 * doesn't need settling time.
 */
const WARMUP_DURATION_MS = 5000;  // Reduced from 15s: timestamp anchoring stabilises faster

// ------------------------------------------------------------------ //
// Sample readers                                                       //
// ------------------------------------------------------------------ //

/**
 * Read a 16-bit sample from a DataView and normalize to [-1, 1].
 * @param {DataView} pcm
 * @param {number} byteOffset
 * @returns {number}
 */
function _readInt16(pcm, byteOffset) {
  return pcm.getInt16(byteOffset, true) / 32768;
}

/**
 * Read a 24-bit little-endian signed sample from a DataView and normalize to [-1, 1].
 * @param {DataView} pcm
 * @param {number} byteOffset
 * @returns {number}
 */
function _readInt24(pcm, byteOffset) {
  const b0 = pcm.getUint8(byteOffset);
  const b1 = pcm.getUint8(byteOffset + 1);
  const b2 = pcm.getUint8(byteOffset + 2);
  // Assemble as unsigned then sign-extend
  let val = b0 | (b1 << 8) | (b2 << 16);
  if (val & 0x800000) {
    val |= 0xFF000000; // sign-extend to 32-bit
  }
  return val / 8388608;
}

// ------------------------------------------------------------------ //
// DriftCorrector                                                      //
// ------------------------------------------------------------------ //

/**
 * @typedef {Object} DriftMetrics
 * @property {number} activeDriftPpm   - Effective drift rate used for correction
 * @property {number} clockDriftPpm    - Drift rate from clock sync
 * @property {number} bufferDriftPpm   - Drift rate from buffer depth trend
 * @property {number} accumulator      - Current fractional-sample accumulator
 * @property {number} totalAdded       - Samples inserted since start
 * @property {number} totalRemoved     - Samples removed since start
 * @property {number} chunksProcessed  - Total chunks processed
 * @property {number} bufferDepthMinMs - 60 s rolling minimum buffer depth (ms)
 * @property {number} bufferDepthMedianMs - 60 s rolling median buffer depth (ms)
 * @property {number} bufferDepthMaxMs - 60 s rolling maximum buffer depth (ms)
 */

/**
 * @typedef {Object} CorrectedChunk
 * @property {Float32Array} left             - Left channel float32 samples
 * @property {Float32Array} right            - Right channel float32 samples
 * @property {number}       sampleCount      - Number of output samples (959, 960, or 961)
 * @property {number}       correctionApplied - 0 = none, +1 = sample added, -1 = sample removed
 */

export class DriftCorrector {
  /**
   * @param {number} [bufferTargetChunks] - Target buffer depth in chunks (default 25)
   */
  constructor(bufferTargetChunks) {
    /** @type {number} Configurable buffer target */
    this._bufferTargetChunks = bufferTargetChunks || BUFFER_TARGET_CHUNKS;
    /** @type {number} Fractional-sample accumulator */
    this._accumulator = 0;

    /** @type {number} */
    this._totalAdded = 0;

    /** @type {number} */
    this._totalRemoved = 0;

    /** @type {number} */
    this._chunksProcessed = 0;

    /** @type {number} Active drift rate used for the last correction (ppm) */
    this._activeDriftPpm = 0;

    /**
     * @type {number} Buffer-depth-derived drift rate (ppm).
     *
     * DIAGNOSTIC ONLY.  Exposed in getMetrics()/getSnapshot() for
     * debug visibility but NOT fed into _computeEffectiveDrift —
     * buffer management is handled by correctionTerm (proportional
     * controller on buffer depth).  Combining buffer_drift into the
     * base rate caused active_drift_ppm oscillation that drained the
     * buffer to underrun (see _computeEffectiveDrift comment, lines
     * 794-805).  Do not plug this value back into the controller.
     */
    this._bufferDriftPpm = 0;

    /** @type {number} Clock-sync-derived drift rate (ppm) */
    this._clockDriftPpm = 0;

    /** @type {boolean} When false, process() passes through without correction */
    this._enabled = true;

    /** @type {number} EMA-smoothed buffer depth (chunks), initialized on first sample */
    this._smoothedBufferDepth = NaN;

    /** @type {number} EMA-smoothed buffer trend ppm */
    this._smoothedBufferTrendPpm = 0;

    /** @type {number} Previous active drift for rate-limiting */
    this._prevActiveDriftPpm = 0;

    // Buffer depth trend tracking: rolling (time, depthChunks) window
    /** @type {{time: number, depth: number}[]} */
    this._depthHistory = [];

    // Rolling buffer depth values for min/median/max statistics
    /** @type {number[]} */
    this._depthStatsWindow = [];

    // Cached depth stats (recomputed every 50 chunks to avoid sorting on every call)
    /** @type {{min: number, median: number, max: number}} */
    this._cachedDepthStats = { min: 0, median: 0, max: 0 };

    /** @type {number} Buffer corrections suppressed until this time (ms, performance.now() domain) */
    this._warmupEndTime = performance.now() + WARMUP_DURATION_MS;

    /** @type {number} Peak buffer depth in current EMA window */
    this._emaPeakDepth = 0;
    /** @type {number} Call counter for EMA window (resets every _EMA_WINDOW_CHUNKS calls) */
    this._emaWindowCounter = 0;

    /** @type {boolean} Latch to apply only one immediate low-buffer correction per crash */
    this._lowBufferCrashCorrectionLatched = false;

    /** @type {number} Previous raw buffer depth for perturbation detection */
    this._prevRawBufferDepth = NaN;

    /** @type {number} Peak buffer depth in the current perturbation window */
    this._perturbPeakDepth = 0;
    /** @type {number} Previous perturbation window peak (for stable comparison) */
    this._prevPerturbPeak = NaN;

    /** @type {number} Buffer correction frozen until this time (ms, performance.now() domain) */
    this._perturbationFreezeUntil = 0;

    /** @type {number} Last computed base drift ppm (clock/buffer blend) — for debug */
    this._lastBasePpm = 0;

    /** @type {number} Last computed buffer correction term ppm — for debug */
    this._lastCorrectionTerm = 0;

    /** @type {number} Last time the buffer trend least-squares was computed (ms) */
    this._lastTrendComputeTime = 0;
  }

  // ---------------------------------------------------------------- //
  // Public API                                                        //
  // ---------------------------------------------------------------- //

  /**
   * Enable or disable drift correction at runtime.
   * When disabled, process() returns exactly 960 samples with no interpolation.
   * @param {boolean} enabled
   */
  setEnabled(enabled) {
    this._enabled = !!enabled;
  }

  /**
   * @returns {boolean} Whether drift correction is enabled
   */
  isEnabled() {
    return this._enabled;
  }

  /**
   * Update the buffer target (e.g. after receiving hub config).
   * Resets warmup timer — buffer needs time to settle to the new depth.
   * Clears trend history — old slope data at the old target is invalid.
   * @param {number} chunks - New buffer target in chunks
   */
  setBufferTarget(chunks) {
    // Skip reset if target hasn't changed — prevents observer effect where
    // diagnostic polls broadcast unchanged config and reset warmup,
    // permanently disabling drift correction.
    if (chunks === this._bufferTargetChunks) return;

    this._bufferTargetChunks = chunks;
    this._warmupEndTime = performance.now() + WARMUP_DURATION_MS;
    this._depthHistory = [];
    this._smoothedBufferTrendPpm = 0;
    this._smoothedBufferDepth = NaN;
    this._emaPeakDepth = 0;
    this._emaWindowCounter = 0;
    this._lowBufferCrashCorrectionLatched = false;
  }

  /**
   * Acknowledge an external buffer trim so the perturbation detector doesn't
   * mistake the depth drop for a network event and freeze correction.
   * Call this from the spoke engine immediately after trimming the buffer.
   * @param {number} newDepthChunks - Buffer depth after trim
   */
  notifyTrim(newDepthChunks) {
    this._prevRawBufferDepth = newDepthChunks;
    // Reset perturbation peak so the post-trim depth doesn't look like a jump.
    this._perturbPeakDepth = newDepthChunks;
    this._prevPerturbPeak = newDepthChunks;
  }

  /**
   * Process a chunk's PCM data, applying drift correction if needed.
   *
   * Deinterleaves int16 stereo PCM into separate float32 L/R channels.
   * If the accumulator has crossed the +/-1.0 threshold, one sample is
   * removed (interpolate two into one) or added (insert interpolated value)
   * at the chunk midpoint for minimal audible impact.
   *
   * When disabled (setEnabled(false)), skips all drift computation and
   * returns a straight deinterleave with exactly 960 samples.
   *
   * @param {DataView} pcmInt16 - Interleaved PCM LE (960 samples x 2 channels, int16 or int24)
   * @param {number} clockDriftPpm - Drift rate from clock sync (ppm)
   * @param {number} bufferDepthChunks - Current jitter buffer depth in chunks
   * @param {boolean} [is24bit=false] - If true, PCM data is 24-bit (3 bytes/sample)
   * @returns {CorrectedChunk}
   */
  process(pcmInt16, clockDriftPpm, bufferDepthChunks, is24bit) {
    this._chunksProcessed++;
    this._clockDriftPpm = clockDriftPpm;

    // Select sample reader based on bit depth
    const bps = is24bit ? 3 : BYTES_PER_SAMPLE;
    const readSample = is24bit ? _readInt24 : _readInt16;

    // Bypass mode: straight deinterleave, no correction
    if (!this._enabled) {
      const left = new Float32Array(SAMPLES_PER_CHUNK);
      const right = new Float32Array(SAMPLES_PER_CHUNK);
      this._deinterleaveGeneric(pcmInt16, left, right, bps, readSample);
      return { left, right, sampleCount: SAMPLES_PER_CHUNK, correctionApplied: 0 };
    }

    // Record buffer depth for trend analysis
    const now = performance.now() / 1000;
    this._recordBufferDepth(now, bufferDepthChunks);

    // Compute effective drift rate (combines both signals + buffer target correction)
    const effectivePpm = this._computeEffectiveDrift(bufferDepthChunks);
    this._activeDriftPpm = effectivePpm;

    // Update accumulator: fractional samples to correct this chunk
    // (effectivePpm is already clamped and rate-limited by _computeEffectiveDrift)
    this._accumulator += effectivePpm * SAMPLES_PER_CHUNK / 1e6;

    // If the actual buffer crashes far below target, force one immediate
    // sample insertion so playback slows this chunk instead of waiting for EMA.
    const immediateCrashThreshold = this._bufferTargetChunks - 3;
    const inWarmup = performance.now() < this._warmupEndTime;
    const inPerturbationFreeze = performance.now() < this._perturbationFreezeUntil;
    if (!inWarmup && !inPerturbationFreeze && bufferDepthChunks < immediateCrashThreshold) {
      if (!this._lowBufferCrashCorrectionLatched) {
        this._accumulator = Math.min(this._accumulator, -1.0);
        this._lowBufferCrashCorrectionLatched = true;
      }
    } else {
      this._lowBufferCrashCorrectionLatched = false;
    }

    // Determine correction action
    let correctionApplied = 0;
    if (this._accumulator >= 1.0) {
      // Spoke is consuming faster than hub produces -> remove a sample
      correctionApplied = -1;
      this._accumulator -= 1.0;
      this._totalRemoved++;
    } else if (this._accumulator <= -1.0) {
      // Spoke is consuming slower -> add a sample
      correctionApplied = 1;
      this._accumulator += 1.0;
      this._totalAdded++;
    }

    // Recompute depth stats every 50 chunks (~1 second) instead of on every getMetrics() call
    if (this._chunksProcessed % 50 === 0) {
      this._recomputeDepthStats();
    }

    // Deinterleave -> float32 with optional sample correction
    const sampleCount = SAMPLES_PER_CHUNK + correctionApplied;
    const left = new Float32Array(sampleCount);
    const right = new Float32Array(sampleCount);

    if (correctionApplied === 0) {
      this._deinterleaveGeneric(pcmInt16, left, right, bps, readSample);
    } else if (correctionApplied === -1) {
      this._deinterleaveRemoveSampleGeneric(pcmInt16, left, right, bps, readSample);
    } else {
      this._deinterleaveAddSampleGeneric(pcmInt16, left, right, bps, readSample);
    }

    return { left, right, sampleCount, correctionApplied };
  }

  /**
   * Get current drift correction metrics.
   * @returns {DriftMetrics}
   */
  getMetrics() {
    const stats = this._getDepthStats();
    return {
      activeDriftPpm: this._activeDriftPpm,
      clockDriftPpm: this._clockDriftPpm,
      bufferDriftPpm: this._bufferDriftPpm,
      accumulator: this._accumulator,
      totalAdded: this._totalAdded,
      totalRemoved: this._totalRemoved,
      chunksProcessed: this._chunksProcessed,
      bufferDepthMinMs: stats.min * 20,
      bufferDepthMedianMs: stats.median * 20,
      bufferDepthMaxMs: stats.max * 20,
    };
  }

  /**
   * Get detailed internal state for debugging drift correction issues.
   * @returns {Object}
   */
  getDebugState() {
    const inWarmup = performance.now() < this._warmupEndTime;
    return {
      smoothedBaseDriftPpm: Math.round(this._lastBasePpm * 100) / 100,
      smoothedBufferTrendPpm: Math.round(this._smoothedBufferTrendPpm * 100) / 100,
      correctionTerm: Math.round(this._lastCorrectionTerm * 100) / 100,
      activeDriftPpm: Math.round(this._activeDriftPpm * 100) / 100,
      samplesAdded: this._totalAdded,
      samplesRemoved: this._totalRemoved,
      smoothedBufferDepthChunks: Math.round(this._smoothedBufferDepth * 100) / 100,
      smoothedBufferDepthMs: Math.round(this._smoothedBufferDepth * 20 * 100) / 100,
      bufferTargetChunks: this._bufferTargetChunks,
      bufferTargetMs: this._bufferTargetChunks * 20,
      bufferErrorChunks: Number.isNaN(this._smoothedBufferDepth)
        ? 0
        : Math.round((this._smoothedBufferDepth - this._bufferTargetChunks) * 100) / 100,
      deadZoneChunksShrink: BUFFER_DEAD_ZONE_CHUNKS_SHRINK,
      deadZoneChunksGrow: BUFFER_DEAD_ZONE_CHUNKS_GROW,
      isWarmup: inWarmup,
      warmupRemainingMs: inWarmup ? this._warmupEndTime - performance.now() : 0,
      isPerturbationFreeze: performance.now() < this._perturbationFreezeUntil,
      perturbationFreezeRemainingMs: Math.max(0, this._perturbationFreezeUntil - performance.now()),
      depthHistoryLength: this._depthHistory.length,
      accumulator: Math.round(this._accumulator * 1000) / 1000,
      clockDriftPpm: Math.round(this._clockDriftPpm * 100) / 100,
      bufferDriftPpm: Math.round(this._bufferDriftPpm * 100) / 100,
    };
  }

  /**
   * Reset all state (call on reconnect or buffer reset).
   */
  reset() {
    this._accumulator = 0;
    this._totalAdded = 0;
    this._totalRemoved = 0;
    this._chunksProcessed = 0;
    this._activeDriftPpm = 0;
    this._bufferDriftPpm = 0;
    this._clockDriftPpm = 0;
    this._smoothedBufferDepth = NaN;
    this._smoothedBufferTrendPpm = 0;
    this._prevActiveDriftPpm = 0;
    this._depthHistory = [];
    this._depthStatsWindow = [];
    this._cachedDepthStats = { min: 0, median: 0, max: 0 };
    this._warmupEndTime = performance.now() + WARMUP_DURATION_MS;
    this._prevRawBufferDepth = NaN;
    this._perturbationFreezeUntil = 0;
    this._emaPeakDepth = 0;
    this._emaWindowCounter = 0;
    this._perturbPeakDepth = 0;
    this._prevPerturbPeak = NaN;
    this._lowBufferCrashCorrectionLatched = false;
    this._lastBasePpm = 0;
    this._lastCorrectionTerm = 0;
    this._lastTrendComputeTime = 0;
  }

  // ---------------------------------------------------------------- //
  // Deinterleave variants                                             //
  // ---------------------------------------------------------------- //

  /**
   * Standard deinterleave: int16 interleaved stereo -> float32 L/R.
   * @param {DataView} pcm
   * @param {Float32Array} left
   * @param {Float32Array} right
   * @private
   */
  _deinterleave(pcm, left, right) {
    this._deinterleaveGeneric(pcm, left, right, BYTES_PER_SAMPLE, _readInt16);
  }

  /**
   * Generic deinterleave: reads interleaved stereo -> float32 L/R.
   * Works for both 16-bit and 24-bit by accepting a bytesPerSample and reader function.
   * @param {DataView} pcm
   * @param {Float32Array} left
   * @param {Float32Array} right
   * @param {number} bps - Bytes per sample (2 or 3)
   * @param {function} readSample - Function(pcm, byteOffset) -> float
   * @private
   */
  _deinterleaveGeneric(pcm, left, right, bps, readSample) {
    const stride = CHANNELS * bps;
    for (let i = 0; i < SAMPLES_PER_CHUNK; i++) {
      const off = i * stride;
      left[i] = readSample(pcm, off);
      right[i] = readSample(pcm, off + bps);
    }
  }

  /**
   * Deinterleave with one sample removed at the midpoint.
   * @private
   */
  _deinterleaveRemoveSample(pcm, left, right) {
    this._deinterleaveRemoveSampleGeneric(pcm, left, right, BYTES_PER_SAMPLE, _readInt16);
  }

  /**
   * Generic deinterleave with one sample removed at the midpoint.
   * Result: 959 output samples.
   * @param {DataView} pcm
   * @param {Float32Array} left
   * @param {Float32Array} right
   * @param {number} bps
   * @param {function} readSample
   * @private
   */
  _deinterleaveRemoveSampleGeneric(pcm, left, right, bps, readSample) {
    const stride = CHANNELS * bps;
    const removeAt = SAMPLES_PER_CHUNK >> 1; // midpoint (480)
    let outIdx = 0;

    for (let i = 0; i < SAMPLES_PER_CHUNK; i++) {
      const off = i * stride;
      const l = readSample(pcm, off);
      const r = readSample(pcm, off + bps);

      if (i === removeAt && i + 1 < SAMPLES_PER_CHUNK) {
        const nextOff = (i + 1) * stride;
        const lNext = readSample(pcm, nextOff);
        const rNext = readSample(pcm, nextOff + bps);
        left[outIdx] = (l + lNext) * 0.5;
        right[outIdx] = (r + rNext) * 0.5;
        outIdx++;
        i++; // skip next input sample (merged into this one)
      } else {
        left[outIdx] = l;
        right[outIdx] = r;
        outIdx++;
      }
    }
  }

  /**
   * Deinterleave with one sample inserted at the midpoint.
   * @private
   */
  _deinterleaveAddSample(pcm, left, right) {
    this._deinterleaveAddSampleGeneric(pcm, left, right, BYTES_PER_SAMPLE, _readInt16);
  }

  /**
   * Generic deinterleave with one sample inserted at the midpoint.
   * Result: 961 output samples.
   * @param {DataView} pcm
   * @param {Float32Array} left
   * @param {Float32Array} right
   * @param {number} bps
   * @param {function} readSample
   * @private
   */
  _deinterleaveAddSampleGeneric(pcm, left, right, bps, readSample) {
    const stride = CHANNELS * bps;
    const insertAfter = SAMPLES_PER_CHUNK >> 1; // midpoint (480)
    let outIdx = 0;

    for (let i = 0; i < SAMPLES_PER_CHUNK; i++) {
      const off = i * stride;
      const l = readSample(pcm, off);
      const r = readSample(pcm, off + bps);

      left[outIdx] = l;
      right[outIdx] = r;
      outIdx++;

      if (i === insertAfter) {
        if (i + 1 < SAMPLES_PER_CHUNK) {
          const nextOff = (i + 1) * stride;
          const lNext = readSample(pcm, nextOff);
          const rNext = readSample(pcm, nextOff + bps);
          left[outIdx] = (l + lNext) * 0.5;
          right[outIdx] = (r + rNext) * 0.5;
        } else {
          left[outIdx] = l;
          right[outIdx] = r;
        }
        outIdx++;
      }
    }
  }

  // ---------------------------------------------------------------- //
  // Buffer depth tracking                                             //
  // ---------------------------------------------------------------- //

  /**
   * Record a buffer depth sample and update the trend.
   * @param {number} timeSec - performance.now()/1000
   * @param {number} depthChunks - Current buffer depth in chunks
   * @private
   */
  _recordBufferDepth(timeSec, depthChunks) {
    // Perturbation detection: freeze buffer correction on large depth jumps
    // (e.g., track transitions, seek, rebuffer events).
    //
    // Uses peak-per-EMA-window comparison instead of raw per-call comparison.
    // process() is called from the scheduler drain loop, so within a single
    // tick the buffer goes [50, 49, 48, ..., 40].  Raw per-call comparison
    // between the drain's low-water mark (~5) and the next tick's first read
    // (~50) causes false perturbation freezes.  Peak-per-window captures
    // the stable fill level before drain and only fires on genuine events
    // (track transitions, seek, network outage recovery).
    if (depthChunks > this._perturbPeakDepth) {
      this._perturbPeakDepth = depthChunks;
    }
    // Reuse the EMA window counter to emit a peak every _EMA_WINDOW_CHUNKS
    // calls (shared counter is incremented in _computeEffectiveDrift).
    // Check on window boundary (counter == 0 means a window just closed).
    if (this._emaWindowCounter === 0 && !Number.isNaN(this._prevPerturbPeak)) {
      const jump = Math.abs(this._perturbPeakDepth - this._prevPerturbPeak);
      if (jump > PERTURBATION_THRESHOLD_CHUNKS) {
        this._perturbationFreezeUntil = performance.now() + PERTURBATION_FREEZE_MS;
      }
      this._prevPerturbPeak = this._perturbPeakDepth;
      this._perturbPeakDepth = 0;
    } else if (Number.isNaN(this._prevPerturbPeak) && this._emaWindowCounter === 0) {
      // First window: seed the previous peak
      this._prevPerturbPeak = this._perturbPeakDepth;
      this._perturbPeakDepth = 0;
    }
    this._prevRawBufferDepth = depthChunks;

    // If freeze just expired, reset the EMA to the current EMA peak (best
    // estimate of true fill level — raw depthChunks may be mid-drain).
    if (this._perturbationFreezeUntil > 0 && performance.now() >= this._perturbationFreezeUntil) {
      const bestEstimate = this._emaPeakDepth > 0 ? this._emaPeakDepth : depthChunks;
      this._smoothedBufferDepth = bestEstimate;
      this._perturbationFreezeUntil = 0;
    }

    this._depthHistory.push({ time: timeSec, depth: depthChunks });
    while (this._depthHistory.length > BUFFER_TREND_WINDOW) {
      this._depthHistory.shift();
    }

    this._depthStatsWindow.push(depthChunks);
    while (this._depthStatsWindow.length > DEPTH_STATS_WINDOW_SIZE) {
      this._depthStatsWindow.shift();
    }

    // Throttle least-squares to ~1 Hz (was 50 Hz). The O(N) regression over
    // _depthHistory is only used for debug metrics (_bufferDriftPpm), not for
    // _computeEffectiveDrift(), so running it every chunk is pure waste.
    const nowMs = performance.now();
    if (nowMs - this._lastTrendComputeTime >= 1000) {
      this._lastTrendComputeTime = nowMs;
      if (this._depthHistory.length >= BUFFER_TREND_MIN_SAMPLES) {
        const rawPpm = this._computeBufferDriftPpm();
        // EMA smooth the buffer trend ppm to reduce jitter from noisy slope estimates
        this._smoothedBufferTrendPpm += BUFFER_TREND_EMA_ALPHA * (rawPpm - this._smoothedBufferTrendPpm);
        this._bufferDriftPpm = this._smoothedBufferTrendPpm;
      }
    }
  }

  /**
   * Compute drift rate from buffer depth trend via least-squares.
   *
   * The slope of buffer depth (chunks/second) tells us the rate mismatch:
   *   - Negative slope (shrinking buffer) -> spoke consumes faster -> positive drift
   *   - Positive slope (growing buffer) -> spoke consumes slower -> negative drift
   *
   * Conversion: driftPpm = -slope * SAMPLES_PER_CHUNK / SAMPLE_RATE * 1e6
   *   = -slope * 960 / 48000 * 1e6 = -slope * 20000
   *
   * @returns {number} Drift rate in ppm
   * @private
   */
  _computeBufferDriftPpm() {
    const n = this._depthHistory.length;
    if (n < 2) return 0;

    const t0 = this._depthHistory[0].time;
    let sumX = 0, sumY = 0, sumXX = 0, sumXY = 0;
    for (const sample of this._depthHistory) {
      const x = sample.time - t0;
      const y = sample.depth;
      sumX += x;
      sumY += y;
      sumXX += x * x;
      sumXY += x * y;
    }

    const denom = n * sumXX - sumX * sumX;
    if (Math.abs(denom) < 1e-15) return 0;

    const slope = (n * sumXY - sumX * sumY) / denom;
    return -slope * SAMPLES_PER_CHUNK / SAMPLE_RATE * 1e6;
  }

  // ---------------------------------------------------------------- //
  // Effective drift computation                                       //
  // ---------------------------------------------------------------- //

  /**
   * Compute the effective drift rate from clock sync and buffer depth
   * correction term.
   *
   * Base rate = clock_drift_ppm (crystal oscillator skew, stable).
   * Buffer management = correction_term (proportional controller on depth error).
   *
   * Stabilization mechanisms:
   *   1. Dead zone: buffer correction = 0 when within ±40ms of target
   *   2. EMA smoothing: buffer depth smoothed before computing correction
   *   3. Buffer correction clamp: ±2000 ppm max from buffer depth
   *   4. Total drift clamp: ±2500 ppm max overall
   *   5. Rate limiting: max ±MAX_DRIFT_CHANGE_PER_CYCLE (100) ppm change per call
   *
   * @param {number} bufferDepthChunks - Current buffer depth
   * @returns {number} Effective correction rate in ppm
   * @private
   */
  _computeEffectiveDrift(bufferDepthChunks) {
    const clockPpm = this._clockDriftPpm;

    // Base rate = clock sync only. Clock drift measures the real crystal
    // oscillator skew between hub and spoke — stable at 2-80 ppm.
    //
    // Buffer depth management is handled entirely by correction_term
    // (proportional controller), so buffer_drift_ppm is no longer used
    // in the base rate. It was previously selected via an agreement
    // threshold, but buffer_drift is too noisy (±2400 ppm, spiking to
    // ±16000 during transients) and caused active_drift_ppm oscillation
    // that prevented buffer recovery.
    //
    // buffer_drift_ppm is still computed and reported for debug visibility.
    // Clamp to realistic range — real clock drift rarely exceeds ±200 ppm.
    const basePpm = Math.max(-MAX_BASE_DRIFT_PPM, Math.min(MAX_BASE_DRIFT_PPM, clockPpm));
    this._lastBasePpm = basePpm;

    // EMA-smooth the buffer depth using PEAK depth per EMA window (~500ms).
    //
    // Why peak, not raw: process() is called from the scheduler drain loop,
    // which pops chunks one-by-one.  In a single scheduling tick the buffer
    // goes [13, 12, 11, ..., 0].  A per-call EMA converges to the mid-drain
    // average (~4 chunks at peak=10) — far below actual fill level — causing
    // false "buffer too low" correction that drains the buffer to underrun.
    //
    // Using the per-window peak captures the true fill level (right after
    // chunk arrival, before scheduler drain).
    if (bufferDepthChunks > this._emaPeakDepth) {
      this._emaPeakDepth = bufferDepthChunks;
    }
    this._emaWindowCounter++;
    if (this._emaWindowCounter >= _EMA_WINDOW_CHUNKS) {
      const peakDepth = this._emaPeakDepth;
      if (Number.isNaN(this._smoothedBufferDepth)) {
        this._smoothedBufferDepth = peakDepth;
      } else {
        // Fast-track alpha for rapid response in two scenarios:
        // 1. Downward: peak drops below smoothed by 3+ chunks (buffer crash)
        // 2. Upward: peak rises above smoothed while correction is aggressively
        //    negative (<-500 ppm). Without this, the slow alpha can't track
        //    buffer recovery, so the corrector keeps draining — causing a death
        //    spiral where aggressive correction prevents the recovery it needs
        //    to see to reduce correction.
        const crashingDown = peakDepth < this._smoothedBufferDepth - 3;
        const recoveringUnderAggression = (
          peakDepth > this._smoothedBufferDepth + 1
          && this._lastCorrectionTerm < -500
        );
        const alpha = (crashingDown || recoveringUnderAggression)
          ? BUFFER_DEPTH_EMA_ALPHA_FAST
          : BUFFER_DEPTH_EMA_ALPHA;
        this._smoothedBufferDepth += alpha * (peakDepth - this._smoothedBufferDepth);
      }
      this._emaWindowCounter = 0;
      this._emaPeakDepth = 0;
    }

    // Buffer depth target maintenance with asymmetric dead zone and clamp.
    // During warmup or perturbation freeze, suppress buffer correction.
    // Asymmetric: tighter dead zone for shrinking buffer (silence risk) vs growing (just latency).
    const now = performance.now();
    const inWarmup = now < this._warmupEndTime;
    const inPerturbationFreeze = now < this._perturbationFreezeUntil;
    const smoothedError = this._smoothedBufferDepth - this._bufferTargetChunks;

    // Select dead zone based on direction: shrinking buffer is more dangerous
    const deadZone = smoothedError < 0 ? BUFFER_DEAD_ZONE_CHUNKS_SHRINK : BUFFER_DEAD_ZONE_CHUNKS_GROW;

    let correctionTerm;
    if (inWarmup || inPerturbationFreeze || Math.abs(smoothedError) < deadZone) {
      // Within dead zone — no buffer correction needed
      correctionTerm = 0;
    } else {
      // Only correct the portion outside the dead zone
      const effectiveError = smoothedError > 0
        ? smoothedError - deadZone
        : smoothedError + deadZone;
      correctionTerm = effectiveError * BUFFER_DEPTH_CORRECTION_GAIN;
      // Clamp buffer correction to ±2000 ppm
      correctionTerm = Math.max(-MAX_BUFFER_CORRECTION_PPM, Math.min(MAX_BUFFER_CORRECTION_PPM, correctionTerm));
    }

    this._lastCorrectionTerm = correctionTerm;
    let effectivePpm = basePpm + correctionTerm;

    // Clamp total active drift to ±2500 ppm (MAX_CORRECTION_PPM)
    effectivePpm = Math.max(-MAX_CORRECTION_PPM, Math.min(MAX_CORRECTION_PPM, effectivePpm));

    // Rate-limit: max ±100 ppm change per cycle to prevent oscillation
    const delta = effectivePpm - this._prevActiveDriftPpm;
    if (Math.abs(delta) > MAX_DRIFT_CHANGE_PER_CYCLE) {
      effectivePpm = this._prevActiveDriftPpm + Math.sign(delta) * MAX_DRIFT_CHANGE_PER_CYCLE;
    }
    this._prevActiveDriftPpm = effectivePpm;

    return effectivePpm;
  }

  // ---------------------------------------------------------------- //
  // Statistics                                                        //
  // ---------------------------------------------------------------- //

  /**
   * Return cached min/median/max of the rolling depth stats window.
   * Recomputed every 50 chunks (~1 second) by _recomputeDepthStats().
   * @returns {{min: number, median: number, max: number}} Values in chunks
   * @private
   */
  _getDepthStats() {
    return this._cachedDepthStats;
  }

  /**
   * Recompute depth stats from the rolling window. Called every 50 chunks
   * from process() to avoid sorting a 3000-element array on every getMetrics() call.
   * @private
   */
  _recomputeDepthStats() {
    const w = this._depthStatsWindow;
    if (w.length === 0) {
      this._cachedDepthStats = { min: 0, median: 0, max: 0 };
      return;
    }
    const sorted = [...w].sort((a, b) => a - b);
    const mid = sorted.length >> 1;
    const median = sorted.length % 2 === 0
      ? (sorted[mid - 1] + sorted[mid]) / 2
      : sorted[mid];
    this._cachedDepthStats = {
      min: sorted[0],
      median,
      max: sorted[sorted.length - 1],
    };
  }
}

// ------------------------------------------------------------------ //
// Exports                                                             //
// ------------------------------------------------------------------ //

export {
  SAMPLE_RATE,
  SAMPLES_PER_CHUNK,
  BUFFER_TARGET_CHUNKS,
  BUFFER_TREND_WINDOW,
  BUFFER_TREND_MIN_SAMPLES,
  BUFFER_DEPTH_CORRECTION_GAIN,
  BUFFER_DEAD_ZONE_CHUNKS,
  BUFFER_DEAD_ZONE_CHUNKS_SHRINK,
  BUFFER_DEAD_ZONE_CHUNKS_GROW,
  BUFFER_DEPTH_EMA_ALPHA,
  BUFFER_DEPTH_EMA_ALPHA_FAST,
  BUFFER_TREND_EMA_ALPHA,
  MAX_BASE_DRIFT_PPM,
  MAX_BUFFER_CORRECTION_PPM,
  MAX_CORRECTION_PPM,
  MAX_DRIFT_CHANGE_PER_CYCLE,
  AGREEMENT_THRESHOLD_PPM,
  WARMUP_DURATION_MS,
  PERTURBATION_THRESHOLD_CHUNKS,
  PERTURBATION_FREEZE_MS,
};
