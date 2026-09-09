/**
 * Spoke Audio Engine — receives PCM chunks over binary WebSocket,
 * buffers them, and schedules playback via Web Audio API.
 *
 * Binary frame format (3856 bytes):
 *   Header (16 bytes, little-endian):
 *     float64  play_at   (hub monotonic timestamp, seconds)
 *     uint32   sequence  (monotonic sequence number)
 *     uint16   flags     (bit 0 = silence)
 *     uint16   reserved
 *   PCM payload (3840 bytes):
 *     int16 interleaved stereo, 48 kHz, 960 samples per channel
 *
 * Architecture:
 *   Binary WS → frame parser → jitter buffer (sorted by play_at)
 *   → lookahead scheduler (25 ms tick; lookahead = syncAnchorSec +
 *     LOOKAHEAD_MARGIN_SEC, dynamic — see _lookaheadSec)
 *   → AudioBufferSourceNode.start(when) → AudioContext.destination
 *
 * Scheduling: Running counter anchored to AudioContext.currentTime at
 * playback start. Each chunk advances by correctedSamples / sampleRate
 * (959, 960, or 961 based on drift correction). Hub timestamps are NOT
 * used for per-chunk scheduling — only for clock sync drift rate and
 * desync detection.
 *
 * Recovery layering: in-place first, teardown last. The onstatechange
 * re-anchor (suspend/resume), safety/desync re-anchors, and the
 * scheduling-progress watch (_updateSchedulerProgressWatch — heals a
 * 'running' context that schedules nothing while chunks queue, via a
 * clock kick or counter re-anchor) all recover without dropping the
 * connection; the 5s silence/underrun watchdogs close the WebSocket
 * only as last resort.
 *
 * Clock sync is handled by the ClockSync class (clockSync.js) which
 * runs on the same WebSocket connection via text messages.
 */

import { ClockSync } from './clockSync.js';
import { DriftCorrector } from './driftCorrector.js';
import { decodeTtsFrame, isTtsFrame, playTtsPcm } from './ttsPlayback.js';

// ------------------------------------------------------------------ //
// Constants                                                           //
// ------------------------------------------------------------------ //

/** Sample rate matching the hub output */
const SAMPLE_RATE = 48000;

/** Samples per chunk (20ms at 48kHz) */
const SAMPLES_PER_CHUNK = 960;

/** Channels */
const CHANNELS = 2;

/** Bytes per sample (int16) */
const BYTES_PER_SAMPLE = 2;

/** Bytes per sample (int24) */
const BYTES_PER_SAMPLE_24 = 3;

/** Header size in bytes */
const HEADER_SIZE = 16;

/** Expected PCM payload size (16-bit) */
const PCM_SIZE = SAMPLES_PER_CHUNK * CHANNELS * BYTES_PER_SAMPLE; // 3840

/** Expected PCM payload size (24-bit) */
const PCM_SIZE_24 = SAMPLES_PER_CHUNK * CHANNELS * BYTES_PER_SAMPLE_24; // 5760

/** Total frame size (16-bit) */
const FRAME_SIZE = HEADER_SIZE + PCM_SIZE; // 3856

/** Total frame size (24-bit) */
const FRAME_SIZE_24 = HEADER_SIZE + PCM_SIZE_24; // 5776

/** Format version constants (stored in header reserved field) */
const FORMAT_16BIT = 0;
const FORMAT_24BIT = 1;

/** Silence flag bit mask */
const FLAG_SILENCE = 0x0001;

/** @deprecated No longer used — kept for reference. The old 96-sample
 *  crossfade ran on every chunk and caused 50 Hz distortion. Replaced
 *  by DECLICK_SAMPLES which only fires on discontinuity events. */
const CROSSFADE_SAMPLES = 96;  // eslint-disable-line no-unused-vars

/** De-click fade-in samples — applied ONLY after a discontinuity event
 *  (post-trim restart, underrun recovery, track transition).
 *  24 samples = 0.5ms at 48kHz — just enough to avoid a click, short
 *  enough to preserve waveform fidelity. */
const DECLICK_SAMPLES = 24;

/** Soft-limiter knee threshold (pre-gain).  Samples above this are
 *  compressed with a tanh-style curve to reduce inter-sample peak
 *  overload after the 0.8× gain node and DAC reconstruction filter. */
const SOFT_LIMIT_THRESHOLD = 0.85;

/** Concealment fade-out duration: samples to fade from last audio to silence
 *  when buffer underruns. 480 samples = 10ms at 48kHz. Makes single-chunk
 *  drops nearly inaudible by avoiding a hard transition to silence. */
const CONCEALMENT_FADE_SAMPLES = 480;

/** Default buffer target in seconds - minimum chunks needed before _startPlayback().
 *  Matches hub's min_buffer_ms (280ms) so reconnect doesn't play with a mismatched
 *  14-chunk buffer before hub config arrives.  Hub config overrides this on connect. */
const DEFAULT_BUFFER_TARGET_SEC = 0.280;
const IS_DEV = Boolean(import.meta.env?.DEV);

function redactSpokeAudioUrl(url) {
  try {
    const base = typeof window !== 'undefined' ? window.location.origin : 'http://localhost';
    const parsed = new URL(url, base);
    if (parsed.searchParams.has('spoke_token')) {
      parsed.searchParams.set('spoke_token', 'REDACTED');
    }
    return parsed.toString();
  } catch {
    return String(url).replace(/([?&]spoke_token=)[^&]+/g, '$1REDACTED');
  }
}

/** Minimum buffer in ms — ensures playback stability even with high-latency devices */
const MIN_BUFFER_MS = 40;

/** Scheduler tick interval (ms) */
const SCHEDULER_TICK_MS = 25;

/** Lookahead margin above sync anchor (seconds).
 *  The actual lookahead is computed dynamically as syncAnchorSec + this margin,
 *  so it adapts to hub config and device output latency.  Must be ≥100ms or
 *  the safety clamp fires constantly, starving buffer drain. */
const LOOKAHEAD_MARGIN_SEC = 0.140;

/** Silence watchdog timeout (ms) before forcing a WebSocket reconnect.
 *  Last resort — the scheduling-progress watch (_updateSchedulerProgressWatch)
 *  must get its in-place recovery attempts in before this fires. */
const SILENCE_WATCHDOG_TIMEOUT_MS = 5000;

/** Playback-stall trigger: accrued wall-time (ms) during which the engine is
 *  'playing' with chunks queued yet schedules nothing, before an in-place
 *  recovery attempt. Sized so at least two attempts fit before
 *  SILENCE_WATCHDOG_TIMEOUT_MS tears down the WebSocket
 *  (see _updateSchedulerProgressWatch). */
const PLAYBACK_STALL_TRIGGER_MS = 1500;

/** Max stalled wall-time credited per scheduler tick interval (ms). Caps the
 *  contribution of a single long timer gap (background throttling and
 *  main-thread starvation can delay ticks by seconds) so one stale interval
 *  can never trigger recovery by itself — the stall must persist across
 *  multiple observed ticks. */
const PLAYBACK_STALL_MAX_TICK_CREDIT_MS = 250;

/** The render clock counts as advancing when AudioContext.currentTime moved
 *  at least this fraction of elapsed wall time across the stall window.
 *  Below it, the stall is treated as a frozen render clock (needs a
 *  suspend/resume kick); above it, as a stranded running counter (needs a
 *  re-anchor). */
const FROZEN_CLOCK_MIN_ADVANCE_RATIO = 0.5;

/** Maximum jitter buffer size before dropping oldest (5 seconds) */
const MAX_BUFFER_CHUNKS = 250;

/** WebSocket reconnect delays (exponential backoff) */
const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 16000, 30000];

/** Hub timestamp desync threshold (seconds). If running counter diverges
 *  from expected hub timeline by more than this, re-anchor. */
const DESYNC_THRESHOLD_SEC = 2.0;

/** Maximum oracle-injected clock skew. 2500ppm is the drift-corrector clamp. */
const ORACLE_CLOCK_SKEW_MAX_PPM = 2500;

/** Sequence gap threshold to detect track transitions. Hub sequence numbers
 *  are monotonic within a track but may jump by hundreds on track change
 *  (new track starts a fresh stamper sequence). Small gaps (< 50) are
 *  likely network jitter or burst edges — not track transitions. */
const TRACK_TRANSITION_SEQ_GAP = 50;

/** Minimum calibrated timing change before we tear down future scheduled nodes. */
const CONFIG_REANCHOR_MIN_DELTA_MS = 20;

/** Minimum lead time when shifting future unscheduled chunks earlier. */
const MIN_SCHEDULE_AHEAD_SEC = 0.02;

/** Minimum healthy buffered chunks before a config re-anchor is allowed. */
const CONFIG_REANCHOR_MIN_BUFFER_CHUNKS = 8;

/** Extra chunks required before restarting after underrun/rebuffer recovery. */
const UNDERRUN_RECOVERY_EXTRA_CHUNKS = 5;

/** Minimum interval between config-driven re-anchors. */
const CONFIG_REANCHOR_MIN_INTERVAL_MS = 8000;

/** Config padding is a convergence nudge; allow one active re-anchor per connection. */
const CONFIG_REANCHOR_MAX_PER_CONNECTION = 1;

// ------------------------------------------------------------------ //
// SpokeAudioEngine                                                    //
// ------------------------------------------------------------------ //

/**
 * @typedef {Object} EngineMetrics
 * @property {string} state - 'disconnected' | 'connecting' | 'buffering' | 'playing' | 'underrun'
 * @property {number} bufferDepthMs - Current jitter buffer depth in ms
 * @property {number} chunksReceived - Total chunks received
 * @property {number} chunksPlayed - Total chunks scheduled for playback
 * @property {number} chunksDropped - Chunks that arrived too late
 * @property {number} chunksTrimmed - Chunks intentionally trimmed to maintain sync
 * @property {number} chunksSilence - Silence chunks received
 * @property {number} lastSequence - Last received sequence number
 * @property {number} sequenceGaps - Number of sequence gaps detected
 * @property {number} clockOffsetMs - Current clock offset in ms
 * @property {number} clockRttMs - Current clock RTT in ms
 * @property {number} driftPpm - Clock drift rate in ppm
 * @property {boolean} clockSynced - Whether clock sync has completed
 * @property {number} outputLatencyMs - Estimated output latency in ms
 */

export class SpokeAudioEngine {
  /**
   * @param {string} wsUrl - WebSocket URL for /ws/audio-stream
   * @param {function(EngineMetrics): void} [onMetrics] - Callback for metrics updates
   * @param {function(string): void} [onLog] - Callback for debug log messages
   */
  constructor(wsUrl, onMetrics, onLog) {
    this._wsUrl = wsUrl;
    this._onMetrics = onMetrics || (() => {});
    this._onLog = onLog || (() => {});
    this._onBackgroundChange = null;
    this._backgroundSince = null;

    // Buffer target: jitter buffer depth (how much to buffer for network resilience).
    // Updated by hub config min_buffer_ms or ?buffer= URL override.
    this._bufferTargetSec = DEFAULT_BUFFER_TARGET_SEC;
    this._bufferTargetChunks = Math.round(this._bufferTargetSec / 0.02);

    // Sync anchor: playout timing offset (how much to delay first chunk relative
    // to hub timestamp so audio exits spoke speaker at the same wall-clock instant
    // as hub speaker).  Decoupled from buffer target so a fat jitter buffer
    // doesn't push playout later.  Falls back to _bufferTargetSec if hub doesn't
    // send the new field.
    /** @type {number} Playout delay offset in seconds */
    this._syncAnchorSec = DEFAULT_BUFFER_TARGET_SEC;
    /** @type {number} Lookahead window = syncAnchor + margin (dynamic, adapts to config) */
    this._lookaheadSec = DEFAULT_BUFFER_TARGET_SEC + LOOKAHEAD_MARGIN_SEC;

    // Hub-computed padding for cross-spoke sync normalization.
    // The hub observes per-spoke effective playout delay and computes padding
    // so all spokes play audio at the same wall-clock instant, regardless of
    // platform pipeline depth or late-join backlog.
    /** @type {number} Hub-assigned padding in ms (positive = add scheduling delay) */
    this._hubPaddingMs = 0;
    /** @type {number} Sync anchor in ms before hub padding is applied */
    this._baseSyncAnchorMs = this._syncAnchorSec * 1000;
    /** @type {number} Config-driven active playback re-anchors on this connection */
    this._syncConfigReanchorCount = 0;
    /** @type {number} performance.now() of last config-driven active playback re-anchor */
    this._lastSyncConfigReanchorAt = 0;
    /** @type {boolean} True when the next underrun restart should reset hub calibration */
    this._pendingUnderrunReanchorReport = false;

    // Config gate: spoke waits for hub config before starting playback.
    // This ensures the spoke uses the correct buffer target for latency compensation.
    /** @type {boolean} Whether hub config has been received (or fallback triggered) */
    this._configReceived = false;
    /** @type {number|null} Fallback timer ID — starts playback with defaults if no config arrives */
    this._configFallbackTimer = null;
    /** @type {boolean} Whether ?buffer= URL override is active (ignores hub config) */
    this._bufferOverride = false;

    if (typeof window !== 'undefined') {
      const bufferParam = parseInt(new URLSearchParams(window.location.search).get('buffer'));
      if (bufferParam >= 50 && bufferParam <= 2000) {
        this._bufferTargetSec = bufferParam / 1000;
        this._syncAnchorSec = bufferParam / 1000;
        this._lookaheadSec = this._syncAnchorSec + LOOKAHEAD_MARGIN_SEC;
        this._bufferTargetChunks = Math.round(this._bufferTargetSec / 0.02);
        this._configReceived = true;   // Skip config gate — developer override
        this._bufferOverride = true;   // Ignore hub config messages
      }
    }

    /** @type {AudioContext|null} */
    this._audioCtx = null;

    /** @type {AnalyserNode|null} */
    this._analyser = null;

    /** @type {GainNode|null} Headroom gain (0.8) to prevent DAC clipping */
    this._gainNode = null;
    this._rampEndTime = null;

    /** @type {boolean} Whether audio output is muted */
    this._muted = false;

    /** @type {number} Volume level (0–1) to restore on unmute */
    this._volumeBeforeMute = 0.8;

    /** @type {WebSocket|null} */
    this._ws = null;

    /** @type {ClockSync|null} */
    this._clockSync = null;

    /** @type {string} */
    this._state = 'disconnected';

    // Jitter buffer: array of {playAt, sequence, flags, pcmInt16}
    // sorted by playAt ascending
    /** @type {Array<{playAt: number, sequence: number, flags: number, pcmInt16: DataView}>} */
    this._buffer = [];

    // Scheduling state
    /** @type {number|null} */
    this._schedulerTimer = null;

    /** @type {boolean} Whether we have anchored the running counter */
    this._timeMapped = false;

    // Output latency (for metrics display only, NOT used in scheduling)
    /** @type {number} Estimated output latency in seconds */
    this._outputLatency = 0;

    // Metrics
    this._chunksReceived = 0;
    this._chunksPlayed = 0;
    this._chunksDropped = 0;
    this._chunksTrimmed = 0;
    this._chunksSilence = 0;
    this._lastChunkPlayedTime = 0;
    this._lastSequence = -1;
    this._sequenceGaps = 0;

    // Drift correction (sample interpolation)
    /** @type {DriftCorrector} */
    this._driftCorrector = new DriftCorrector(this._bufferTargetChunks);
    /** @type {number} Effective queued+scheduled depth last sent into DriftCorrector */
    this._lastDriftBufferDepthChunks = this._bufferTargetChunks;
    /** @type {number} Extra clock skew in ppm used by the L5 oracle on localhost only */
    this._oracleClockSkewPpm = 0;
    /** @type {number} performance.now() deadline for oracle clock skew */
    this._oracleClockSkewUntil = 0;
    /** @type {Array<Object>} Recent oracle control and alignment events */
    this._oracleEvents = [];
    /** @type {number} Max oracle trace entries retained in diagnostics */
    this._oracleEventsMax = 100;

    // Active source node tracking (prevents unbounded accumulation)
    /** @type {number} Currently connected AudioBufferSourceNodes */
    this._activeSourceNodes = 0;
    /** @type {number} Peak active source nodes seen */
    this._peakActiveSourceNodes = 0;
    /** @type {Array<{source: AudioBufferSourceNode, when: number}>} Pending nodes not yet played */
    this._pendingSourceNodes = [];

    // Reconnection
    this._reconnectAttempt = 0;
    this._reconnectTimer = null;
    this._intentionalClose = false;

    // Underrun watchdog: fires if underrun state persists with no recovery
    this._underrunWatchdogTimer = null;

    // Scheduling-progress watch (in-place playback-stall recovery, no WS teardown).
    // Detects "playing + chunks queued + nothing scheduled" regardless of cause
    // (frozen render clock, stranded counter after main-thread starvation).
    /** @type {number} AudioContext.currentTime at last watch sample (-1 = no sample) */
    this._stallWatchLastCtxTime = -1;
    /** @type {number} performance.now() at last watch sample */
    this._stallWatchLastWallMs = 0;
    /** @type {number} _chunksPlayed at last watch sample */
    this._stallWatchLastChunksPlayed = 0;
    /** @type {number} Capped stalled wall-time credit toward PLAYBACK_STALL_TRIGGER_MS */
    this._stallCreditMs = 0;
    /** @type {number} Raw wall-time elapsed across the current stall window (ms) */
    this._stallWallMsRaw = 0;
    /** @type {number} Raw AudioContext.currentTime advance across the stall window (ms) */
    this._stallCtxMsRaw = 0;
    /** @type {boolean} True while a suspend/resume clock kick is in flight */
    this._stallRecoveryInFlight = false;
    /** @type {number} Total in-place playback-stall recoveries (cumulative, survives reconnects) */
    this._stallRecoveryCount = 0;
    /** @type {number|null} performance.now() of last playback-stall recovery */
    this._lastStallRecoveryAt = null;

    // Debug metrics exposed via window.__spokeAudioDebug for automated testing
    /** @type {number} RMS of most recently processed chunk's left channel */
    this._lastChunkRMS = 0;
    /** @type {number} Peak RMS seen across all scheduled chunks */
    this._peakChunkRMS = 0;
    /** @type {number} contextPlayTime - audioCtx.currentTime at last schedule */
    this._lastScheduleDelta = 0;
    /** @type {Object|null} Raw audio data sample from last processed chunk */
    this._lastAudioDataSample = null;

    // Layer 4: Reception diagnostics
    /** @type {number} RMS of raw int16 PCM data received */
    this._lastReceivedChunkRMS = 0;
    /** @type {boolean} Was the last received chunk non-silent? */
    this._lastReceivedNonSilent = false;

    // Layer 5: Conversion diagnostics
    /** @type {number} RMS of float32 data after conversion */
    this._lastConvertedRMS = 0;
    /** @type {number} Min float32 value */
    this._lastConvertedMin = 0;
    /** @type {number} Max float32 value */
    this._lastConvertedMax = 0;

    // Layer 6: Buffer fill diagnostics
    /** @type {number} RMS of AudioBuffer channel data after fill */
    this._lastBufferRMS = 0;
    /** @type {number} Sample count in buffer (959, 960, or 961) */
    this._lastBufferLength = 0;

    // Layer 8: AnalyserNode signal diagnostics
    /** @type {number} RMS from AnalyserNode */
    this._analyserRMS = 0;
    /** @type {number} Peak amplitude from AnalyserNode */
    this._analyserPeak = 0;

    // Clipping detection (pre-gain)
    /** @type {number} Cumulative count of samples with |value| > 0.99 */
    this._clipCount = 0;
    /** @type {number} Clips in the most recent chunk */
    this._clipCountLastChunk = 0;

    // Chunk boundary smoothing state
    /** @type {number} Last sample (left ch) of the previously played chunk */
    this._prevChunkLastSampleL = NaN;
    /** @type {number} Last sample (right ch) of the previously played chunk */
    this._prevChunkLastSampleR = NaN;
    /** @type {number} Total chunks where crossfade was applied */
    this._crossfadeAppliedCount = 0;
    /** @type {boolean} Set true on discontinuity events (trim, underrun, track change).
     *  When true, a short fade-in from zero is applied to the next chunk, then cleared. */
    this._needsDeclick = false;
    /** @type {number} Sum of |discontinuity| across all boundaries */
    this._boundaryDiscontinuitySum = 0;
    /** @type {number} Max |discontinuity| seen */
    this._boundaryMaxDiscontinuity = 0;
    /** @type {number} Number of chunk boundaries measured */
    this._boundaryCount = 0;

    // Recording tap state (chunk-level capture for fidelity analysis)
    /** @type {boolean} Whether we are currently recording */
    this._isRecording = false;
    /** @type {Array<{seq: number, rawLeft: Float32Array, corrLeft: Float32Array, corrRight: Float32Array, sampleCount: number}>} */
    this._recordedChunks = [];
    /** @type {number} Total corrected samples recorded */
    this._recordingLength = 0;

    // Last sequence number passed to AudioContext (most recently scheduled)
    /** @type {number} */
    this._lastScheduledSequence = -1;
    /** @type {number} Hub playAt timestamp of the last scheduled chunk */
    this._lastScheduledPlayAt = 0;

    // ── Echo detector: fingerprint ring for content-repeat detection ──
    // Keeps fingerprints of recent chunks (first 8 L-channel samples).
    // On each scheduled chunk, compares against the ring. If cosine
    // similarity > 0.92, logs ECHO_DETECTED with seq numbers and delay.
    /** @type {Array<{seq: number, time: number, fp: Float32Array}>} */
    this._echoFingerprints = [];
    /** @type {number} Max ring size (25 chunks ≈ 500ms at 50fps) */
    this._echoRingMax = 25;
    /** @type {number} Total echo detections */
    this._echoDetections = 0;

    // Track transition detection: re-anchor running counter at track boundaries
    /** @type {boolean} True when a large sequence gap was detected (track change) */
    this._trackTransitionPending = false;
    /** @type {number} First sequence number of the new track (after the gap) */
    this._newTrackFirstSeq = -1;

    // Bound handlers
    this._onWsOpen = this._onWsOpen.bind(this);
    this._onWsMessage = this._onWsMessage.bind(this);
    this._onWsClose = this._onWsClose.bind(this);
    this._onWsError = this._onWsError.bind(this);
    this._schedulerTick = this._schedulerTick.bind(this);
    this._onVisibilityChange = this._onVisibilityChange.bind(this);

    // Running counter for scheduling (advances by actual output samples)
    this._nextPlayTime = 0;

    // Diagnostic throttle counters (keeps diagnostics off the 50Hz hot path)
    /** @type {number} Incremented per chunk in _scheduleChunk */
    this._diagCounter = 0;
    /** @type {number} Incremented per scheduler tick for analyser throttle */
    this._analyserTickCounter = 0;
    /** @type {Object|null} Cached signal quality from AnalyserNode FFT */
    this._cachedSignalQuality = null;

    // Desync detection: track relationship between hub timestamps and running counter
    /** @type {number} Hub play_at of the first chunk seen after connect */
    this._firstHubTimestamp = 0;
    /** @type {number} _nextPlayTime when first chunk was scheduled */
    this._firstCounterTime = 0;
    /** @type {boolean} Whether we've recorded the first chunk's anchor */
    this._desyncAnchorSet = false;
    /** @type {number} Count of safety clamp triggers */
    this._safetyClampCount = 0;
    /** @type {number} Consecutive safety clamp triggers (reset on normal schedule) */
    this._consecutiveClamps = 0;
    /** @type {number} Timestamp of last continuous buffer trim (ms, performance.now()) */
    this._lastTrimTime = 0;

    // Schedule log for sync measurement diagnostics (Item 7)
    /** @type {Array<{seq: number, scheduledAt: number, playedAt: number}>} */
    this._scheduleLog = [];
    /** @type {number} Max entries in schedule log */
    this._scheduleLogMax = 100;
  }

  // ---------------------------------------------------------------- //
  // Public API                                                        //
  // ---------------------------------------------------------------- //

  /**
   * Start the engine. Must be called from a user gesture handler
   * (required for AudioContext creation on mobile browsers).
   */
  async start() {
    if (this._state !== 'disconnected') {
      return;
    }

    this._intentionalClose = false;

    // Set audio session type BEFORE creating AudioContext.
    // 'playback' bypasses the iOS silent switch so music plays even
    // when the hardware mute is on.  Do NOT use 'play-and-record'
    // here — that activates iOS's VoiceProcessingIO (VPIO) hardware
    // unit, which aggressively ducks playback whenever the microphone
    // picks up ANY sound (not just speech).  The session type is
    // switched to 'play-and-record' only when mic streaming is
    // actually started (see useVoiceStream.js), and reverted to
    // 'playback' when streaming stops.
    if (navigator.audioSession) {
      navigator.audioSession.type = 'playback';
    }

    // Create AudioContext with 48kHz sample rate
    this._audioCtx = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: SAMPLE_RATE,
    });
    // INSTRUMENTATION: log actual vs requested sample rate
    const actualSampleRate = this._audioCtx.sampleRate;
    if (actualSampleRate !== SAMPLE_RATE) {
      console.error('[SpokeAudio] CRITICAL: AudioContext sampleRate mismatch! requested=%d, actual=%d', SAMPLE_RATE, actualSampleRate);
    } else {
      if (IS_DEV) {
        console.log('[SpokeAudio] AudioContext sampleRate OK: %d', actualSampleRate);
      }
    }

    // Create GainNode for headroom (0.8x prevents DAC clipping)
    this._gainNode = this._audioCtx.createGain();
    this._gainNode.gain.value = 0.8;

    // Create AnalyserNode for signal flow verification (Layer 8)
    this._analyser = this._audioCtx.createAnalyser();
    this._analyser.fftSize = 2048;

    // Signal chain: source → gainNode → analyser → destination
    this._gainNode.connect(this._analyser);
    this._analyser.connect(this._audioCtx.destination);

    // Read output latency (for metrics display)
    this._updateOutputLatency();

    if (IS_DEV) {
      console.log('[SpokeAudio] AudioContext created (sampleRate=%d, state=%s)', SAMPLE_RATE, this._audioCtx.state);
    }
    this._log('AudioContext created (sampleRate=%d, state=%s)', SAMPLE_RATE, this._audioCtx.state);

    // Monitor AudioContext state changes — re-start playback if context
    // was suspended and just became running (critical for iOS where resume()
    // may complete asynchronously after the initial time mapping attempt).
    this._audioCtx.onstatechange = () => {
      if (IS_DEV) {
        console.log('[SpokeAudio] AudioContext state changed to:', this._audioCtx.state);
      }
      this._log('AudioContext state: %s', this._audioCtx.state);
      this._updateDebugObject();

      if (this._audioCtx.state === 'running') {
        // If we were waiting for the context to start, begin playback now.
        // Buffering requires config gate; underrun already had config.
        const requiredChunks = this._state === 'underrun'
          ? this._underrunRecoveryStartChunks()
          : this._bufferTargetChunks;
        if (((this._state === 'buffering' && this._configReceived) || this._state === 'underrun')
            && this._buffer.length >= requiredChunks) {
          this._log('AudioContext now running — starting playback');
          if (this._state === 'buffering') this._trimBurstExcess();
          this._startPlayback();
        }
        // If we were playing but the context was suspended and resumed,
        // re-anchor the running counter to the live clock
        if (this._state === 'playing' && this._timeMapped) {
          this._log('AudioContext resumed — re-anchoring running counter');
          // Trim excess accumulated while context was suspended (iOS async resume)
          if (this._buffer.length > this._bufferTargetChunks + 5) {
            this._trimBurstExcess();
          }
          this._recomputeTimeMapping();
        }
      } else if (this._audioCtx.state === 'interrupted') {
        this._log('AudioContext interrupted — waiting for running state');
      }
    };

    // Resume AudioContext if suspended/interrupted (autoplay policy / iOS interruptions)
    if (this._audioCtx.state === 'suspended' || this._audioCtx.state === 'interrupted') {
      await this._audioCtx.resume();
      this._log('AudioContext resumed');
    }

    // Expose debug object for Playwright / automated testing
    this._updateDebugObject();

    // Expose engine instance for runtime control
    if (typeof window !== 'undefined') {
      const self = this;
      window.__spokeEngine = {
        setVolume: (v) => this.setVolume(v),
        setMuted: (m) => this.setMuted(m),
        setDriftCorrectionEnabled: (e) => this.setDriftCorrectionEnabled(e),
        getBufferTarget: () => this.getBufferTarget(),
        getMetrics: () => this.getMetrics(),
        injectClockSkewForTest: (ppm, durationMs) => this.injectClockSkewForTest(ppm, durationMs),
        clearClockSkewForTest: () => this.clearClockSkewForTest(),
        forceRebufferForTest: (reason) => this.forceRebufferForTest(reason),
        forceAlignForTest: (reason) => this.forceAlignForTest(reason),
        getOracleTrace: () => this.getOracleTrace(),
        // Direct property accessors for sync measurement
        get _state() { return self._state; },
        get _chunksReceived() { return self._chunksReceived; },
        get _chunksPlayed() { return self._chunksPlayed; },
        get _jitterBuffer() { return self._buffer; },
        get _bufferTargetChunks() { return self._bufferTargetChunks; },
        get _nextPlayTime() { return self._nextPlayTime; },
        get _audioCtx() { return self._audioCtx; },
        get _consecutiveClamps() { return self._consecutiveClamps; },
        get _activeSourceNodes() { return self._activeSourceNodes; },
        get _peakActiveSourceNodes() { return self._peakActiveSourceNodes; },
        // Expose recording methods for diagnostics
        startRecording: () => self.startRecording(),
        stopRecording: () => self.stopRecording(),
        getRecordedChunkCount: () => self.getRecordedChunkCount(),
        getRecordedChunkStats: (s, c) => self.getRecordedChunkStats(s, c),
        getRecordedRawSamples: (i, ch) => self.getRecordedRawSamples(i, ch),
        getRecordedCorrectedSamples: (i, ch) => self.getRecordedCorrectedSamples(i, ch),
      };
    }

    // Listen for tab visibility changes
    document.addEventListener('visibilitychange', this._onVisibilityChange);

    // Connect WebSocket
    this._connect();
  }

  /**
   * Stop the engine and clean up all resources.
   */
  stop() {
    this._intentionalClose = true;
    this._setState('disconnected');

    // Clear reconnect timer
    if (this._reconnectTimer !== null) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }

    // Clear config fallback timer
    if (this._configFallbackTimer !== null) {
      clearTimeout(this._configFallbackTimer);
      this._configFallbackTimer = null;
    }

    // Clear underrun watchdog
    if (this._underrunWatchdogTimer !== null) {
      clearTimeout(this._underrunWatchdogTimer);
      this._underrunWatchdogTimer = null;
    }

    // Stop scheduler
    this._stopScheduler();

    // Destroy clock sync
    if (this._clockSync) {
      this._clockSync.destroy();
      this._clockSync = null;
    }

    // Close WebSocket
    if (this._ws) {
      this._ws.removeEventListener('open', this._onWsOpen);
      this._ws.removeEventListener('message', this._onWsMessage);
      this._ws.removeEventListener('close', this._onWsClose);
      this._ws.removeEventListener('error', this._onWsError);
      this._ws.close();
      this._ws = null;
    }

    // Stop recording if active
    this._isRecording = false;
    this._recordedChunks = [];
    this._recordingLength = 0;

    // Close AudioContext
    if (this._audioCtx) {
      this._audioCtx.close();
      this._audioCtx = null;
    }
    this._analyser = null;
    this._gainNode = null;

    // Remove visibility listener
    document.removeEventListener('visibilitychange', this._onVisibilityChange);

    // Clear buffer and reset state
    this._buffer = [];
    this._timeMapped = false;
    this._driftCorrector.reset();
    this._oracleClockSkewPpm = 0;
    this._oracleClockSkewUntil = 0;
    this._prevChunkLastSampleL = NaN;
    this._prevChunkLastSampleR = NaN;
    this._needsDeclick = false;
    this._boundaryDiscontinuitySum = 0;
    this._boundaryMaxDiscontinuity = 0;
    this._boundaryCount = 0;
    this._clipCount = 0;
    this._clipCountLastChunk = 0;
    this._desyncAnchorSet = false;
    this._safetyClampCount = 0;
    this._consecutiveClamps = 0;
    this._lastTrimTime = 0;
    this._pendingUnderrunReanchorReport = false;
    this._scheduleLog = [];
    this._backgroundSince = null;
    this._diagCounter = 0;
    this._analyserTickCounter = 0;
    this._cachedSignalQuality = null;
    this._trackTransitionPending = false;
    this._newTrackFirstSeq = -1;

    // Clean up debug objects
    if (typeof window !== 'undefined') {
      window.__spokeAudioDebug = null;
      window.__spokeEngine = null;
    }

    this._emitMetrics();
    this._log('Engine stopped');
  }

  /**
   * Enable or disable drift correction at runtime.
   * When disabled, the drift corrector passes chunks through unmodified
   * (exactly 960 samples, no interpolation).
   * @param {boolean} enabled
   */
  setDriftCorrectionEnabled(enabled) {
    this._driftCorrector.setEnabled(enabled);
    this._log('Drift correction %s', enabled ? 'enabled' : 'disabled');
    this._updateDebugObject();
  }

  /**
   * Return true when local browser automation may use oracle controls.
   * These controls exist for measured sync proof and remain disabled on
   * non-local hosts.
   * @returns {boolean}
   */
  _oracleControlsAllowed() {
    if (typeof window === 'undefined') return false;
    const host = window.location.hostname;
    return host === '127.0.0.1' || host === 'localhost' || host === '::1';
  }

  /**
   * Append a compact event to the oracle trace exposed in diagnostics.
   * @param {string} type
   * @param {Object} [details]
   * @private
   */
  _recordOracleEvent(type, details = {}) {
    const nowMs = (typeof performance !== 'undefined' && performance.now)
      ? performance.now()
      : Date.now();
    this._oracleEvents.push({
      type,
      atMs: Math.round(nowMs),
      audioContextTime: this._audioCtx ? Number(this._audioCtx.currentTime.toFixed(3)) : null,
      state: this._state,
      bufferDepthMs: this._buffer.length * 20,
      chunksPlayed: this._chunksPlayed,
      lastSequence: this._lastSequence,
      ...details,
    });
    if (this._oracleEvents.length > this._oracleEventsMax) {
      this._oracleEvents.shift();
    }
    this._updateDebugObject();
  }

  /**
   * Current oracle skew, auto-cleared when its duration expires.
   * @returns {number}
   * @private
   */
  _getOracleClockSkewPpm() {
    if (!this._oracleClockSkewPpm) {
      return 0;
    }
    const nowMs = (typeof performance !== 'undefined' && performance.now)
      ? performance.now()
      : Date.now();
    if (this._oracleClockSkewUntil > 0 && nowMs >= this._oracleClockSkewUntil) {
      const expired = this._oracleClockSkewPpm;
      this._oracleClockSkewPpm = 0;
      this._oracleClockSkewUntil = 0;
      this._recordOracleEvent('clock_skew_expired', { ppm: expired });
      return 0;
    }
    return this._oracleClockSkewPpm;
  }

  /**
   * Effective drift value fed to the production drift corrector.
   * @returns {number}
   * @private
   */
  _getEffectiveClockDriftPpm() {
    const raw = this._clockSync ? this._clockSync.getDriftRate() : 0;
    return raw + this._getOracleClockSkewPpm();
  }

  /**
   * Inject deterministic clock skew for the L5 oracle.
   * @param {number} ppm
   * @param {number} [durationMs]
   * @returns {Object}
   */
  injectClockSkewForTest(ppm, durationMs = 10000) {
    if (!this._oracleControlsAllowed()) {
      return { ok: false, error: 'oracle_controls_disabled' };
    }
    const clamped = Math.max(-ORACLE_CLOCK_SKEW_MAX_PPM, Math.min(ORACLE_CLOCK_SKEW_MAX_PPM, Number(ppm) || 0));
    const duration = Math.max(100, Math.min(60000, Number(durationMs) || 10000));
    const nowMs = (typeof performance !== 'undefined' && performance.now)
      ? performance.now()
      : Date.now();
    this._oracleClockSkewPpm = clamped;
    this._oracleClockSkewUntil = nowMs + duration;
    this._recordOracleEvent('clock_skew_injected', { ppm: clamped, durationMs: duration });
    return { ok: true, ppm: clamped, durationMs: duration };
  }

  /**
   * Clear oracle clock skew immediately.
   * @returns {Object}
   */
  clearClockSkewForTest() {
    const previous = this._oracleClockSkewPpm;
    this._oracleClockSkewPpm = 0;
    this._oracleClockSkewUntil = 0;
    this._recordOracleEvent('clock_skew_cleared', { ppm: previous });
    return { ok: true, previousPpm: previous };
  }

  /**
   * Force a real underrun/refill/re-anchor cycle without closing the socket.
   * The next binary frames refill the production jitter buffer and _startPlayback
   * performs the normal alignment path.
   * @param {string} [reason]
   * @returns {Object}
   */
  forceRebufferForTest(reason = 'oracle') {
    if (!this._oracleControlsAllowed()) {
      return { ok: false, error: 'oracle_controls_disabled' };
    }
    const dropped = this._buffer.length;
    this._cancelPendingNodes();
    this._buffer = [];
    this._timeMapped = false;
    this._driftCorrector.reset();
    this._desyncAnchorSet = false;
    this._consecutiveClamps = 0;
    this._syncConfigReanchorCount = 0;
    this._lastSyncConfigReanchorAt = 0;
    this._pendingUnderrunReanchorReport = true;
    this._prevChunkLastSampleL = 0.0;
    this._prevChunkLastSampleR = 0.0;
    this._needsDeclick = true;
    this._stopScheduler();
    this._setState('underrun');
    this._recordOracleEvent('forced_rebuffer', { reason, droppedChunks: dropped });
    return { ok: true, droppedChunks: dropped, state: this._state };
  }

  /**
   * Force the production alignment path to re-anchor immediately.
   * @param {string} [reason]
   * @returns {Object}
   */
  forceAlignForTest(reason = 'oracle') {
    if (!this._oracleControlsAllowed()) {
      return { ok: false, error: 'oracle_controls_disabled' };
    }
    if (!this._audioCtx) {
      return { ok: false, error: 'audio_context_unavailable' };
    }
    const oldNext = this._nextPlayTime;
    this._cancelPendingNodes();
    this._nextPlayTime = this._audioCtx.currentTime + this._syncAnchorSec;
    this._desyncAnchorSet = false;
    this._consecutiveClamps = 0;
    this._recordOracleEvent('forced_align', {
      reason,
      oldNextPlayTime: Number(oldNext.toFixed(3)),
      nextPlayTime: Number(this._nextPlayTime.toFixed(3)),
    });
    return { ok: true, state: this._state, nextPlayTime: this._nextPlayTime };
  }

  /**
   * Re-anchor scheduled playback to the next buffered hub chunk.
   *
   * Used when the hub sends calibrated sync padding while playback is already
   * running. Pending future nodes were scheduled with stale timing, so they
   * must be canceled and the running counter must be rebuilt from the same
   * hub timestamp path used at startup.
   * @param {string} reason
   * @returns {boolean}
   * @private
   */
  _reanchorPlaybackToBufferedChunk(reason) {
    if (!this._audioCtx || !this._clockSync || this._audioCtx.state !== 'running') {
      return false;
    }
    const syncState = this._clockSync.getState();
    if (!syncState.synced || this._buffer.length === 0) {
      return false;
    }

    const oldNext = this._nextPlayTime;
    const firstChunk = this._buffer[0];
    const perfToCtx = this._audioCtx.currentTime - performance.now() / 1000;
    const firstChunkLocalTime = firstChunk.playAt - syncState.offset;
    const nextPlayTime = Math.max(
      firstChunkLocalTime + perfToCtx + this._syncAnchorSec,
      this._audioCtx.currentTime + this._syncAnchorSec,
    );

    if (reason === 'config_padding_update' && nextPlayTime < oldNext + 0.005) {
      return false;
    }

    this._cancelPendingNodes();
    this._nextPlayTime = nextPlayTime;
    this._desyncAnchorSet = false;
    this._consecutiveClamps = 0;
    this._needsDeclick = true;
    this._recordOracleEvent('sync_config_reanchor', {
      reason,
      oldNextPlayTime: Number(oldNext.toFixed(3)),
      nextPlayTime: Number(this._nextPlayTime.toFixed(3)),
      sequence: firstChunk.sequence,
      syncAnchorMs: Math.round(this._syncAnchorSec * 1000),
      hubPaddingMs: this._hubPaddingMs,
    });
    this._log(
      'Sync config re-anchor: nextPlayTime=%.3f -> %.3f (seq=%d, padding=%dms)',
      oldNext,
      this._nextPlayTime,
      firstChunk.sequence,
      this._hubPaddingMs,
    );
    return true;
  }

  /**
   * Shift only future unscheduled chunks after hub padding changes.
   *
   * Pending Web Audio nodes stay intact to avoid an audible cancel/restart.
   * The scheduling cursor still has to follow signed padding deltas; otherwise
   * an early over-padding decision can leave the spoke permanently late after
   * the hub corrects the padding downward.
   *
   * @param {number} shiftMs Signed change in sync anchor.
   * @param {number} previousPaddingMs
   * @returns {boolean}
   * @private
   */
  _shiftFuturePlaybackForConfigUpdate(shiftMs, previousPaddingMs) {
    if (!this._audioCtx || Math.abs(shiftMs) < CONFIG_REANCHOR_MIN_DELTA_MS) {
      return false;
    }
    const oldNext = this._nextPlayTime;
    const minNextPlayTime = this._audioCtx.currentTime + MIN_SCHEDULE_AHEAD_SEC;
    this._nextPlayTime = Math.max(minNextPlayTime, this._nextPlayTime + shiftMs / 1000);
    this._desyncAnchorSet = false;
    this._consecutiveClamps = 0;
    this._recordOracleEvent('sync_config_shift', {
      oldNextPlayTime: Number(oldNext.toFixed(3)),
      nextPlayTime: Number(this._nextPlayTime.toFixed(3)),
      shiftMs: Math.round(shiftMs),
      previousPaddingMs: Math.round(previousPaddingMs),
      hubPaddingMs: Math.round(this._hubPaddingMs),
      syncAnchorMs: Math.round(this._syncAnchorSec * 1000),
    });
    return true;
  }

  /**
   * Decide whether a calibrated config change should rebuild active scheduling.
   *
   * Hub padding updates can arrive every sync report.  Rebuilding the active
   * Web Audio schedule on each small refinement cancels pending nodes and can
   * create exactly the gap the calibration is trying to remove.  Use a single
   * healthy-buffer startup correction; later drift is handled by the normal
   * drift corrector and by future chunks scheduled with the updated anchor.
   *
   * @param {number} previousPaddingMs
   * @param {number} previousSyncAnchorSec
   * @returns {boolean}
   * @private
   */
  _maybeReanchorForConfigUpdate(previousPaddingMs, previousSyncAnchorSec) {
    if (this._state !== 'playing') {
      return false;
    }

    const paddingDeltaMs = Math.abs(this._hubPaddingMs - previousPaddingMs);
    const signedAnchorDeltaMs = (this._syncAnchorSec - previousSyncAnchorSec) * 1000;
    const anchorDeltaMs = Math.abs(signedAnchorDeltaMs);
    if (Math.max(paddingDeltaMs, anchorDeltaMs) < CONFIG_REANCHOR_MIN_DELTA_MS) {
      return false;
    }

    if (this._syncConfigReanchorCount >= CONFIG_REANCHOR_MAX_PER_CONNECTION) {
      return this._shiftFuturePlaybackForConfigUpdate(signedAnchorDeltaMs, previousPaddingMs);
    }

    const minBufferChunks = Math.min(this._bufferTargetChunks, CONFIG_REANCHOR_MIN_BUFFER_CHUNKS);
    if (this._buffer.length < minBufferChunks) {
      return this._shiftFuturePlaybackForConfigUpdate(signedAnchorDeltaMs, previousPaddingMs);
    }

    const nowMs = (typeof performance !== 'undefined' && performance.now)
      ? performance.now()
      : Date.now();
    if (
      this._lastSyncConfigReanchorAt > 0
      && nowMs - this._lastSyncConfigReanchorAt < CONFIG_REANCHOR_MIN_INTERVAL_MS
    ) {
      return this._shiftFuturePlaybackForConfigUpdate(signedAnchorDeltaMs, previousPaddingMs);
    }

    if (this._reanchorPlaybackToBufferedChunk('config_padding_update')) {
      this._syncConfigReanchorCount += 1;
      this._lastSyncConfigReanchorAt = nowMs;
      return true;
    }

    if (!this._shiftFuturePlaybackForConfigUpdate(signedAnchorDeltaMs, previousPaddingMs)) {
      return false;
    }

    this._syncConfigReanchorCount += 1;
    this._lastSyncConfigReanchorAt = nowMs;
    return true;
  }

  /**
   * Return recent oracle events for Playwright.
   * @returns {Array<Object>}
   */
  getOracleTrace() {
    return this._oracleEvents.slice(-this._oracleEventsMax);
  }

  /**
   * Set output volume (0.0 to 1.0).
   * @param {number} value - Volume level (clamped to 0–1)
   */
  setVolume(value) {
    const v = Math.max(0, Math.min(1, value));
    this._volumeBeforeMute = v;
    if (this._gainNode && !this._muted) {
      if (this._audioCtx && this._rampEndTime !== null && this._audioCtx.currentTime < this._rampEndTime) {
        // Preserve the startup fade-in shape when a new volume target arrives mid-ramp.
        const currentGain = this._gainNode.gain.value;
        if (typeof this._gainNode.gain.cancelAndHoldAtTime === 'function') {
          this._gainNode.gain.cancelAndHoldAtTime(this._audioCtx.currentTime);
        } else {
          this._gainNode.gain.cancelScheduledValues(this._audioCtx.currentTime);
          this._gainNode.gain.setValueAtTime(currentGain, this._audioCtx.currentTime);
        }
        this._gainNode.gain.linearRampToValueAtTime(v, this._rampEndTime);
        if (IS_DEV) {
          console.log('[SpokeAudio] setVolume: ramp in progress, redirecting to %.4f (was heading to %.4f)', v, currentGain);
        }
      } else {
        if (IS_DEV) {
          console.log('[SpokeAudio] setVolume: direct gainNode.gain.value=%.4f (no active ramp)', v);
        }
        this._gainNode.gain.value = v;
        this._rampEndTime = null;
      }
    }
    this._log('Volume set to %.2f', v);
  }

  /**
   * Set muted state. When muted, gain is set to 0 but volume is preserved.
   * On unmute, restores to the stored volume (default 0.8 if none set).
   * @param {boolean} muted
   */
  setMuted(muted) {
    this._muted = !!muted;
    if (this._gainNode) {
      this._gainNode.gain.value = this._muted ? 0 : this._volumeBeforeMute;
    }
    this._log('Muted: %s (stored volume: %.2f)', this._muted, this._volumeBeforeMute);
  }

  /**
   * Get the current buffer target configuration.
   * @returns {{sec: number, chunks: number}}
   */
  getBufferTarget() {
    return { sec: this._bufferTargetSec, chunks: this._bufferTargetChunks };
  }

  /**
   * Compute signal quality metrics from the AnalyserNode FFT data.
   *
   * Returns dominant frequency, SNR, THD, and peak dB — useful for
   * measuring how cleanly a test tone survives the pipeline.
   *
   * Signal bandwidth: ±50 Hz around the peak bin, converted to bin count
   * dynamically. This captures spectral leakage from the test tone while
   * excluding energy from other frequency regions.
   *
   * @returns {{dominantFreq: number, snrDb: number, thd: number, peakDb: number}|null}
   */
  getSignalQuality() {
    if (!this._analyser || !this._audioCtx) return null;

    const fftData = new Float32Array(this._analyser.frequencyBinCount);
    this._analyser.getFloatFrequencyData(fftData); // dB values

    // Find the dominant frequency bin (skip bin 0 = DC)
    let peakBin = 1;
    let peakVal = -Infinity;
    for (let i = 1; i < fftData.length; i++) {
      if (fftData[i] > peakVal) {
        peakVal = fftData[i];
        peakBin = i;
      }
    }
    const binFreq = this._audioCtx.sampleRate / this._analyser.fftSize;
    const dominantFreq = peakBin * binFreq;

    // Signal bandwidth: ±50 Hz around peak, expressed in bins
    const signalBinsHalf = Math.ceil(50 / binFreq);

    // Compute signal power vs noise+harmonic power
    let signalPower = 0;
    let totalPower = 0;
    for (let i = 1; i < fftData.length; i++) {
      const linearPower = Math.pow(10, fftData[i] / 10);
      totalPower += linearPower;
      if (Math.abs(i - peakBin) <= signalBinsHalf) {
        signalPower += linearPower;
      }
    }
    const noisePower = totalPower - signalPower;
    const snrDb = 10 * Math.log10(signalPower / Math.max(noisePower, 1e-20));
    const thd = Math.sqrt(noisePower / Math.max(signalPower, 1e-20));

    return { dominantFreq, snrDb, thd, peakDb: peakVal };
  }

  /**
   * Get current engine metrics.
   * @returns {EngineMetrics}
   */
  _getScheduledAheadChunks() {
    if (!this._audioCtx || !Number.isFinite(this._nextPlayTime) || this._nextPlayTime <= 0) {
      return 0;
    }
    const chunkSec = SAMPLES_PER_CHUNK / SAMPLE_RATE;
    const scheduledAheadSec = Math.max(0, this._nextPlayTime - this._audioCtx.currentTime);
    return Math.max(0, Math.round(scheduledAheadSec / chunkSec));
  }

  _getEffectiveBufferDepthChunks(extraScheduledChunks = 0) {
    const rawQueued = Math.max(0, this._buffer.length);
    const scheduledAhead = this._getScheduledAheadChunks();
    return Math.max(
      0,
      Math.min(MAX_BUFFER_CHUNKS, rawQueued + scheduledAhead + Math.max(0, extraScheduledChunks)),
    );
  }

  _getQueuedBufferDepthChunks(extraQueuedChunks = 0) {
    const rawQueued = Math.max(0, this._buffer.length);
    return Math.max(
      0,
      Math.min(MAX_BUFFER_CHUNKS, rawQueued + Math.max(0, extraQueuedChunks)),
    );
  }

  _getCurrentlyRenderingEvent() {
    if (!this._audioCtx || this._scheduleLog.length === 0) {
      return null;
    }
    const now = this._audioCtx.currentTime;
    for (let i = this._scheduleLog.length - 1; i >= 0; i--) {
      if (this._scheduleLog[i].scheduledAt <= now + 0.001) {
        return this._scheduleLog[i];
      }
    }
    return null;
  }

  _getCurrentRenderTiming(clockState = null) {
    const event = this._getCurrentlyRenderingEvent();
    if (!event || !this._audioCtx) {
      return {
        sequence: -1,
        renderHubTimeMs: null,
        sequenceNormalizedRenderMs: null,
      };
    }
    const state = clockState || (this._clockSync ? this._clockSync.getState() : null);
    if (!state || !state.synced || typeof state.offset !== 'number') {
      return {
        sequence: event.seq,
        renderHubTimeMs: null,
        sequenceNormalizedRenderMs: null,
      };
    }
    const perfToCtx = this._audioCtx.currentTime - performance.now() / 1000;
    const audibleAt = (typeof event.playedAt === 'number') ? event.playedAt : event.scheduledAt;
    const renderPerfTimeSec = audibleAt - perfToCtx;
    const renderHubTimeMs = (renderPerfTimeSec + state.offset) * 1000;
    const normalizedRenderMs = (typeof event.hubPlayAt === 'number')
      ? renderHubTimeMs - event.hubPlayAt * 1000
      : renderHubTimeMs - event.seq * 20;
    return {
      sequence: event.seq,
      renderHubTimeMs,
      sequenceNormalizedRenderMs: normalizedRenderMs,
    };
  }

  _getCurrentlyRenderingSequence() {
    const event = this._getCurrentlyRenderingEvent();
    if (event) {
      return event.seq;
    }
    return -1;
  }

  getMetrics() {
    const clockState = this._clockSync ? this._clockSync.getState() : { offset: 0, rtt: 0, synced: false };
    const driftState = this._clockSync ? this._clockSync.getDriftState() : { ppm: 0 };
    const driftMetrics = this._driftCorrector.getMetrics();
    const oracleClockSkewPpm = this._getOracleClockSkewPpm();
    const effectiveDriftPpm = (driftState.ppm || 0) + oracleClockSkewPpm;
    const renderTiming = this._getCurrentRenderTiming(clockState);
    const currentlyPlayingSeq = renderTiming.sequence;
    const scheduledAheadChunks = this._getScheduledAheadChunks();
    const effectiveBufferDepthChunks = this._getEffectiveBufferDepthChunks();

    return {
      state: this._state,
      bufferDepthMs: this._buffer.length * 20,
      effectiveBufferDepthMs: effectiveBufferDepthChunks * 20,
      scheduledAheadMs: scheduledAheadChunks * 20,
      driftBufferDepthMs: this._lastDriftBufferDepthChunks * 20,
      chunksReceived: this._chunksReceived,
      chunksPlayed: this._chunksPlayed,
      chunksDropped: this._chunksDropped,
      chunksTrimmed: this._chunksTrimmed,
      chunksSilence: this._chunksSilence,
      lastSequence: this._lastSequence,
      currentlyPlayingSeq,
      currentRenderHubTimeMs: renderTiming.renderHubTimeMs,
      sequenceNormalizedRenderMs: renderTiming.sequenceNormalizedRenderMs,
      lastScheduledSeq: this._lastScheduledSequence,
      lastScheduledPlayAt: this._lastScheduledPlayAt,
      sequenceGaps: this._sequenceGaps,
      clockOffsetMs: clockState.offset * 1000,
      clockRttMs: clockState.rtt * 1000,
      driftPpm: effectiveDriftPpm,
      rawDriftPpm: driftState.ppm,
      clockSynced: clockState.synced,
      outputLatencyMs: this._outputLatency * 1000,
      muted: this._muted,
      volume: this._volumeBeforeMute,
      // Drift correction metrics
      driftCorrectionPpm: driftMetrics.activeDriftPpm,
      driftClockPpm: driftMetrics.clockDriftPpm,
      driftBufferPpm: driftMetrics.bufferDriftPpm,
      driftAccumulator: driftMetrics.accumulator,
      driftSamplesAdded: driftMetrics.totalAdded,
      driftSamplesRemoved: driftMetrics.totalRemoved,
      bufferDepthMinMs: driftMetrics.bufferDepthMinMs,
      bufferDepthMedianMs: driftMetrics.bufferDepthMedianMs,
      bufferDepthMaxMs: driftMetrics.bufferDepthMaxMs,
      crossfadeAppliedCount: this._crossfadeAppliedCount,
      // Re-anchor counter: incremented each time _cancelPendingNodes
      // fires (safety clamp, track transition, desync recovery, context
      // resume, or playback-stall recovery).  High counts indicate clock
      // instability; used by the sync-quality indicator.
      reAnchorCount: this._reAnchorCount || 0,
      lastReAnchorMsAgo:
        this._lastReAnchorAt != null && typeof performance !== 'undefined' && performance.now
          ? performance.now() - this._lastReAnchorAt
          : null,
      // Playback-stall recovery: in-place healing (clock kick / re-anchor)
      // for a 'running' context that schedules nothing while chunks queue.
      stallRecoveryCount: this._stallRecoveryCount,
      lastStallRecoveryMsAgo:
        this._lastStallRecoveryAt != null && typeof performance !== 'undefined' && performance.now
          ? performance.now() - this._lastStallRecoveryAt
          : null,
      oracleClockSkewPpm,
      oracleEvents: this._oracleEvents.slice(-50),
    };
  }

  // ---------------------------------------------------------------- //
  // Audio output recording                                            //
  // ---------------------------------------------------------------- //

  /**
   * Start recording per-chunk fidelity data from _scheduleChunk().
   *
   * For each scheduled chunk, captures:
   * - sequence number (for matching with hub-sent data)
   * - raw int16 PCM (what the hub sent, before drift correction)
   * - corrected float32 PCM (what we scheduled for playback)
   * - sample count (959, 960, or 961 — reveals drift correction action)
   *
   * Works reliably in headless browsers (no audio graph changes needed).
   */
  startRecording() {
    if (this._isRecording) return;

    /** @type {Array<{seq: number, rawLeft: Float32Array, corrLeft: Float32Array, corrRight: Float32Array, sampleCount: number}>} */
    this._recordedChunks = [];
    this._recordingLength = 0;
    this._isRecording = true;

    this._log('Recording started (chunk-tap)');
  }

  /**
   * Stop recording.
   * @returns {{chunks: number, totalSamples: number}}
   */
  stopRecording() {
    this._isRecording = false;
    const chunks = this._recordedChunks ? this._recordedChunks.length : 0;
    this._log('Recording stopped: %d chunks, %d samples', chunks, this._recordingLength);
    return { chunks, totalSamples: this._recordingLength };
  }

  /**
   * Get per-chunk fidelity data for analysis.
   *
   * Returns an array of chunk records. Each record contains:
   * - seq: sequence number
   * - sampleCount: output samples (959/960/961)
   * - rawLeft: raw int16->float32 left channel (960 samples, pre-drift-correction)
   * - corrLeft: corrected left channel (959-961 samples, post-drift-correction)
   *
   * @param {number} startIdx - First chunk index
   * @param {number} count - Number of chunks to return
   * @returns {Array<{seq: number, sampleCount: number, mae: number, maxErr: number}>}
   */
  getRecordedChunkStats(startIdx, count) {
    if (!this._recordedChunks) return [];
    const end = Math.min(startIdx + count, this._recordedChunks.length);
    const result = [];
    for (let i = startIdx; i < end; i++) {
      const c = this._recordedChunks[i];
      // Compute per-chunk MAE between raw and corrected (left channel)
      const minLen = Math.min(c.rawLeft.length, c.corrLeft.length);
      let sumAbsDiff = 0;
      let maxDiff = 0;
      for (let j = 0; j < minLen; j++) {
        const d = Math.abs(c.rawLeft[j] - c.corrLeft[j]);
        sumAbsDiff += d;
        if (d > maxDiff) maxDiff = d;
      }
      result.push({
        seq: c.seq,
        sampleCount: c.sampleCount,
        mae: minLen > 0 ? sumAbsDiff / minLen : 0,
        maxErr: maxDiff,
      });
    }
    return result;
  }

  /**
   * Get the raw (pre-drift-correction) samples for a specific chunk.
   * @param {number} chunkIdx - Chunk index in recording
   * @param {'left'} _channel
   * @returns {number[]} Array of float32 values
   */
  getRecordedRawSamples(chunkIdx, _channel) {
    if (!this._recordedChunks || chunkIdx >= this._recordedChunks.length) return [];
    return Array.from(this._recordedChunks[chunkIdx].rawLeft);
  }

  /**
   * Get the corrected (post-drift-correction) samples for a specific chunk.
   * @param {number} chunkIdx - Chunk index in recording
   * @param {'left'} channel
   * @returns {number[]} Array of float32 values
   */
  getRecordedCorrectedSamples(chunkIdx, channel) {
    if (!this._recordedChunks || chunkIdx >= this._recordedChunks.length) return [];
    const c = this._recordedChunks[chunkIdx];
    return Array.from(channel === 'right' ? c.corrRight : c.corrLeft);
  }

  /**
   * Get the total number of recorded chunks.
   * @returns {number}
   */
  getRecordedChunkCount() {
    return this._recordedChunks ? this._recordedChunks.length : 0;
  }

  // ---------------------------------------------------------------- //
  // WebSocket                                                         //
  // ---------------------------------------------------------------- //

  /** @private */
  _connect() {
    this._setState('connecting');
    this._log('Connecting to %s', redactSpokeAudioUrl(this._wsUrl));

    this._ws = new WebSocket(this._wsUrl);
    this._ws.binaryType = 'arraybuffer';
    this._ws.addEventListener('open', this._onWsOpen);
    this._ws.addEventListener('message', this._onWsMessage);
    this._ws.addEventListener('close', this._onWsClose);
    this._ws.addEventListener('error', this._onWsError);
  }

  /** @private */
  _onWsOpen() {
    if (IS_DEV) {
      console.log('[SpokeAudio] WebSocket connected to', redactSpokeAudioUrl(this._wsUrl));
    }
    this._log('WebSocket connected');
    this._reconnectAttempt = 0;

    // Initialize clock sync on this WebSocket
    this._clockSync = new ClockSync(this._ws);

    // Run initial sync, then signal hub to start audio
    this._runInitialSync();
  }

  /**
   * Run clock sync and, on success, tell the hub we're ready for audio.
   * Hub will not send any PCM frames until it receives this signal,
   * which prevents burst chunks from arriving with stale timestamps.
   * Retries on failure with a 2-second delay.
   * @private
   */
  async _runInitialSync() {
    try {
      const result = await this._clockSync.synchronize();
      this._log(
        'Clock sync OK: offset=%.2f ms, rtt=%.2f ms, probes=%d',
        result.offset * 1000,
        result.rtt * 1000,
        result.probes,
      );
      this._clockSync.startPeriodicSync();

      // Send register FIRST so hub has spoke metadata before sending config.
      // Then ready_for_audio triggers: hub sends config → burst → live frames.
      if (this._ws && this._ws.readyState === WebSocket.OPEN) {
        const roomName = (typeof localStorage !== 'undefined' && localStorage.getItem('viola_room_name')) || 'Speaker';
        this._ws.send(JSON.stringify({
          type: 'register',
          room_name: roomName,
        }));
        this._log('Sent register: room_name=%s', roomName);

        this._ws.send(JSON.stringify({ type: 'ready_for_audio' }));
        if (IS_DEV) {
          console.log('[SpokeAudio] Clock synced, sent ready_for_audio (offset=%.1fms, rtt=%.1fms)', result.offset * 1000, result.rtt * 1000);
        }
        this._log('Sent ready_for_audio to hub');

        // Config fallback: if hub doesn't send config within 3s, use defaults.
        // Handles old hubs that don't send config messages.
        if (!this._configReceived && !this._bufferOverride) {
          this._configFallbackTimer = setTimeout(() => {
            this._configFallbackTimer = null;
            if (!this._configReceived) {
              this._configReceived = true;
              this._log(
                'Config fallback: no config in 3s, using default buffer %dms',
                this._bufferTargetSec * 1000,
              );
              // If buffer is already full, start playback now
              if (this._state === 'buffering'
                  && this._buffer.length >= this._bufferTargetChunks) {
                this._trimBurstExcess();
                this._startPlayback();
              }
            }
          }, 3000);
        }
      }

      this._setState('buffering');
      this._emitMetrics();
    } catch (err) {
      if (IS_DEV) {
        console.warn('[SpokeAudio] Clock sync failed:', err.message, '— retrying in 2s');
      }
      this._log('Clock sync failed: %s — retrying in 2s', err.message);
      setTimeout(() => {
        if (this._ws && this._ws.readyState === WebSocket.OPEN && this._clockSync) {
          this._runInitialSync();
        }
      }, 2000);
    }
  }

  /** @private */
  _onWsMessage(event) {
    if (event.data instanceof ArrayBuffer) {
      if (isTtsFrame(event.data)) {
        const { pcmBuffer, sampleRate } = decodeTtsFrame(event.data);
        playTtsPcm(pcmBuffer, sampleRate);
        return;
      }
      this._handleBinaryFrame(event.data);
    } else if (typeof event.data === 'string') {
      // Text messages: clock sync handled by ClockSync internally,
      // but we also handle server-sent commands here
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'config') {
          this._applyConfig(data);
        } else if (data.type === 'set_volume' && typeof data.volume === 'number') {
          this.setVolume(data.volume);
        } else if (data.type === 'set_mute' && typeof data.muted === 'boolean') {
          this.setMuted(data.muted);
        } else if (data.type === 'request_diagnostics' && data.request_id) {
          this._handleDiagnosticsRequest(data.request_id);
        } else if (data.type === 'track_change') {
          // Explicit track change signal from hub — same as sequence gap detection
          if (this._state === 'playing') {
            this._trackTransitionPending = true;
            // Use next received sequence as the anchor point
            this._newTrackFirstSeq = this._lastSequence + 1;
            this._log('Track change message received — will re-anchor on next chunk');
          }
        }
      } catch {
        // Not JSON or not for us — ClockSync handles its own messages
      }
    }
  }

  /** @private */
  _onWsClose(event) {
    if (event.code !== 1000) {
      if (IS_DEV) {
        console.warn('[SpokeAudio] WebSocket closed abnormally (code=%d, reason=%s)', event.code, event.reason || 'none');
      }
    }
    this._log('WebSocket closed (code=%d, reason=%s)', event.code, event.reason || 'none');

    // Clean up clock sync
    if (this._clockSync) {
      this._clockSync.destroy();
      this._clockSync = null;
    }

    this._ws = null;
    this._stopScheduler();

    // Clear underrun watchdog — WS is gone, no point timing out
    if (this._underrunWatchdogTimer !== null) {
      clearTimeout(this._underrunWatchdogTimer);
      this._underrunWatchdogTimer = null;
    }

    this._buffer = [];
    this._timeMapped = false;
    this._driftCorrector.reset();
    this._oracleClockSkewPpm = 0;
    this._oracleClockSkewUntil = 0;
    this._desyncAnchorSet = false;
    this._safetyClampCount = 0;
    this._consecutiveClamps = 0;
    this._lastTrimTime = 0;
    this._scheduleLog = [];
    this._backgroundSince = null;
    this._trackTransitionPending = false;
    this._newTrackFirstSeq = -1;
    this._lastSequence = -1;
    this._lastScheduledSequence = -1;
    this._lastScheduledPlayAt = 0;
    this._lastChunkPlayedTime = 0;
    this._stallWatchLastCtxTime = -1;
    this._stallWatchLastWallMs = 0;
    this._stallWatchLastChunksPlayed = 0;
    this._stallRecoveryInFlight = false;
    this._resetStallWindow();
    this._rampEndTime = null;  // Clear stale ramp state from prior session
    this._syncConfigReanchorCount = 0;
    this._lastSyncConfigReanchorAt = 0;

    // Reset config state for reconnection (Break 9)
    if (!this._bufferOverride) {
      this._configReceived = false;
      this._bufferTargetSec = DEFAULT_BUFFER_TARGET_SEC;
      this._syncAnchorSec = DEFAULT_BUFFER_TARGET_SEC;
      this._lookaheadSec = DEFAULT_BUFFER_TARGET_SEC + LOOKAHEAD_MARGIN_SEC;
      this._bufferTargetChunks = Math.round(DEFAULT_BUFFER_TARGET_SEC / 0.02);
      this._hubPaddingMs = 0;
      this._baseSyncAnchorMs = this._syncAnchorSec * 1000;
      this._driftCorrector.setBufferTarget(Math.max(2, this._bufferTargetChunks));
      this._pendingSourceNodes = [];
    }
    if (this._configFallbackTimer !== null) {
      clearTimeout(this._configFallbackTimer);
      this._configFallbackTimer = null;
    }

    if (!this._intentionalClose) {
      this._scheduleReconnect();
    }
  }

  /** @private */
  _onWsError(event) {
    console.error('[SpokeAudio] WebSocket error', event);
    this._log('WebSocket error');
  }

  /** @private */
  _scheduleReconnect() {
    const delay = RECONNECT_DELAYS_MS[
      Math.min(this._reconnectAttempt, RECONNECT_DELAYS_MS.length - 1)
    ];
    this._reconnectAttempt++;
    this._setState('disconnected');
    this._log('Reconnecting in %d ms (attempt %d)', delay, this._reconnectAttempt);

    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      if (!this._intentionalClose) {
        this._connect();
      }
    }, delay);
  }

  // ---------------------------------------------------------------- //
  // Hub config handling                                               //
  // ---------------------------------------------------------------- //

  /**
   * Apply hub latency config.  Two independent values are used:
   *
   * sync_anchor_ms — playout timing offset.  Spoke delays its first chunk
   *     by this amount (minus spoke output latency) so audio exits the
   *     spoke speaker at the same wall-clock instant as the hub speaker.
   *
   * min_buffer_ms — jitter buffer floor.  Minimum ms of audio to buffer
   *     before starting playback and the target depth for drift correction.
   *     Independent of sync timing.
   *
   * Legacy: if the hub only sends target_total_ms (old protocol), it is
   * used for both sync anchor and buffer target (backward compat).
   *
   * @param {Object} config - Hub config message
   * @param {number} config.target_total_ms - Legacy total latency target
   * @param {number} [config.sync_anchor_ms] - Playout timing offset (new)
   * @param {number} [config.min_buffer_ms] - Jitter buffer floor (new)
   * @private
   */
  _applyConfig(config) {
    // Developer ?buffer= override: ignore hub config entirely
    if (this._bufferOverride) {
      this._log('Config ignored: ?buffer= URL override active');
      return;
    }

    const targetTotalMs = config.target_total_ms;
    if (typeof targetTotalMs !== 'number' || targetTotalMs <= 0) {
      this._log('Invalid config: target_total_ms=%s', targetTotalMs);
      return;
    }

    // Output latency is reported and calibrated, but not subtracted from
    // the scheduling anchor. Subtracting Chrome's ~52ms output latency from
    // a 60ms anchor collapses the lead time to the 20ms floor and makes the
    // drift corrector chase a false low-buffer condition.
    this._updateOutputLatency();
    const myLatencyMs = this._outputLatency * 1000;

    // --- Sync anchor (playout timing) ---
    // New field from hub; falls back to target_total_ms for old hubs.
    const syncAnchorMs = (typeof config.sync_anchor_ms === 'number' && config.sync_anchor_ms > 0)
      ? config.sync_anchor_ms
      : targetTotalMs;
    // iOS Safari Web Audio needs lead time to reliably schedule
    // AudioBufferSourceNode.start(when).  Reduced from 40ms to 20ms;
    // measured jitter is 2-4ms so 20ms is 5x margin without over-delaying.
    const MIN_SYNC_ANCHOR_MS = 20;

    // --- Hub-calibrated cross-spoke sync padding ---
    // Different devices have different audio pipeline depths (e.g. Safari's
    // WebKit pipeline is ~100ms deeper than Chrome's).  Rather than detecting
    // platforms with heuristics, the hub measures per-spoke effective playout
    // delay and sends a padding_ms correction so all spokes converge to the
    // same audible playout instant.  Hub padding is 0 until calibration completes
    // (first ~10s with 2+ spokes), and stays 0 for single-spoke setups.
    const previousPaddingMs = this._hubPaddingMs;
    const previousSyncAnchorSec = this._syncAnchorSec;
    const hubPaddingMs = (typeof config.padding_ms === 'number')
      ? Math.max(0, config.padding_ms)
      : 0;
    this._hubPaddingMs = hubPaddingMs;

    const baseAnchorMs = Math.max(MIN_SYNC_ANCHOR_MS, syncAnchorMs);
    this._baseSyncAnchorMs = baseAnchorMs;
    const anchorMs = baseAnchorMs + hubPaddingMs;
    this._syncAnchorSec = anchorMs / 1000;
    this._lookaheadSec = this._syncAnchorSec + LOOKAHEAD_MARGIN_SEC;

    // --- Buffer target (jitter protection) ---
    // New field from hub; falls back to legacy formula for old hubs.
    const minBufferMs = (typeof config.min_buffer_ms === 'number' && config.min_buffer_ms > 0)
      ? config.min_buffer_ms
      : Math.max(MIN_BUFFER_MS, targetTotalMs - myLatencyMs);
    const bufferMs = Math.max(MIN_BUFFER_MS, minBufferMs);

    this._bufferTargetSec = bufferMs / 1000;
    this._bufferTargetChunks = Math.round(bufferMs / 20);

    // Drift corrector target = queued jitter-buffer depth only.
    // Scheduled-ahead audio is intentional Web Audio lookahead, not extra
    // receive buffer. Counting it here made the DC drain healthy playback at
    // 1200-2000ppm even when the actual clock drift was single-digit ppm.
    const dcTargetChunks = Math.max(2, this._bufferTargetChunks);
    this._driftCorrector.setBufferTarget(dcTargetChunks);

    if (IS_DEV) {
      console.warn('[SYNC-DIAG] Config: sync_anchor_ms=' + syncAnchorMs + ' min_buffer_ms=' + minBufferMs
        + ' myLatency=' + myLatencyMs.toFixed(1) + ' hubPadding=' + hubPaddingMs
        + ' syncAnchorSec=' + this._syncAnchorSec.toFixed(3)
        + ' bufferMs=' + bufferMs + ' startupChunks=' + this._bufferTargetChunks
        + ' dcTargetChunks=' + dcTargetChunks);
    }

    // Clear fallback timer
    if (this._configFallbackTimer !== null) {
      clearTimeout(this._configFallbackTimer);
      this._configFallbackTimer = null;
    }

    const syncTimingChanged =
      Math.abs(hubPaddingMs - previousPaddingMs) > 1
      || Math.abs(this._syncAnchorSec - previousSyncAnchorSec) > 0.001;

    if (!this._configReceived) {
      // First config after connect
      this._configReceived = true;
      this._log(
        'Config applied: sync_anchor=%.0fms, buffer=%dms (%d chunks), my_latency=%.1fms, hub_padding=%dms',
        syncAnchorMs, bufferMs, this._bufferTargetChunks, myLatencyMs, hubPaddingMs,
      );
    } else {
      // Config update during playback (e.g. hub buffer or calibrated padding change).
      this._log(
        'Config updated: sync_anchor=%.0fms, buffer=%dms (%d chunks), my_latency=%.1fms, hub_padding=%dms',
        syncAnchorMs, bufferMs, this._bufferTargetChunks, myLatencyMs, hubPaddingMs,
      );
      if (syncTimingChanged) {
        this._maybeReanchorForConfigUpdate(previousPaddingMs, previousSyncAnchorSec);
      }
    }

    // If buffer is already full and we're in buffering state, start playback
    if (this._state === 'buffering' && this._buffer.length >= this._bufferTargetChunks) {
      this._trimBurstExcess();
      this._startPlayback();
    }
  }

  // ---------------------------------------------------------------- //
  // Diagnostics request handler                                       //
  // ---------------------------------------------------------------- //

  /**
   * Respond to a hub diagnostics request with full spoke state.
   * @param {string} requestId - Correlation ID from the hub
   * @private
   */
  _handleDiagnosticsRequest(requestId) {
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;

    const metrics = this.getMetrics();
    const debug = (typeof window !== 'undefined' && window.__spokeAudioDebug) || {};
    const roomName = (typeof localStorage !== 'undefined' && localStorage.getItem('viola_room_name')) || 'Speaker';
    const driftDebug = this._driftCorrector.getDebugState();

    // Compute current AudioContext time in hub clock for offset measurement
    const clockState = this._clockSync ? this._clockSync.getState() : { offset: 0, synced: false };
    let currentPlayTimeHub = 0;
    let pipelineLatencyMs = 0;
    if (this._audioCtx && clockState.synced) {
      // offset = hub_clock - spoke_clock, so hub_time = spoke_time + offset
      currentPlayTimeHub = this._audioCtx.currentTime + clockState.offset;
      // Schedule lead time: how far in the future the last scheduled chunk
      // will play (in spoke clock). Positive = chunk scheduled ahead of now.
      // Note: both values are in spoke clock to avoid clock domain mixing
      // that produced garbage like -1365ms when subtracting hub time from spoke time.
      if (this._lastScheduledPlayAt > 0) {
        pipelineLatencyMs = (this._lastScheduledPlayAt - this._audioCtx.currentTime) * 1000;
      }
    }

    const report = {
      type: 'diagnostics_report',
      request_id: requestId,
      room_name: roomName,
      data: {
        bufferDepthMs: metrics.bufferDepthMs,
        effectiveBufferDepthMs: metrics.effectiveBufferDepthMs,
        scheduledAheadMs: metrics.scheduledAheadMs,
        driftBufferDepthMs: metrics.driftBufferDepthMs,
        chunksReceived: metrics.chunksReceived,
        chunksPlayed: metrics.chunksPlayed,
        chunksDropped: metrics.chunksDropped,
        chunksTrimmed: metrics.chunksTrimmed,
        sequenceGaps: metrics.sequenceGaps,
        driftPpm: metrics.driftPpm,
        rawDriftPpm: metrics.rawDriftPpm,
        driftCorrectionPpm: metrics.driftCorrectionPpm,
        oracleClockSkewPpm: metrics.oracleClockSkewPpm,
        clockOffsetMs: metrics.clockOffsetMs,
        clockRttMs: metrics.clockRttMs,
        lastSequence: metrics.lastSequence,
        outputLatencyMs: metrics.outputLatencyMs,
        bufferTargetMs: this._bufferTargetSec * 1000,
        rms: debug.lastChunkRMS || 0,
        peakRms: debug.peakChunkRMS || 0,
        analyserRms: debug.analyserRMS || 0,
        state: metrics.state,
        clockSynced: metrics.clockSynced,
        bufferDepthMinMs: metrics.bufferDepthMinMs,
        bufferDepthMedianMs: metrics.bufferDepthMedianMs,
        bufferDepthMaxMs: metrics.bufferDepthMaxMs,
        pipelineHealthy: debug.pipelineHealthy || false,
        firstSilentLayer: debug.firstSilentLayer || null,
        // Signal layers (per-stage RMS)
        lastReceivedChunkRMS: debug.lastReceivedChunkRMS || 0,
        lastConvertedRMS: debug.lastConvertedRMS || 0,
        lastConvertedMin: debug.lastConvertedMin || 0,
        lastConvertedMax: debug.lastConvertedMax || 0,
        lastBufferRMS: debug.lastBufferRMS || 0,
        // Clipping detection
        clipCount: debug.clipCount || 0,
        clipRate: debug.clipRate || 0,
        // Chunk boundary continuity (click/pop detection)
        boundaryDiscontinuityAvg: debug.chunkBoundaryAvgDiscontinuity || 0,
        boundaryDiscontinuityMax: debug.chunkBoundaryMaxDiscontinuity || 0,
        boundaryDiscontinuityCount: debug.chunkBoundaryCount || 0,
        // Spectral quality (FFT-based)
        signalQuality: debug.signalQuality || null,
        // Pipeline internals
        audioContextState: debug.audioContextState || null,
        activeSourceNodes: debug.activeSourceNodes || 0,
        lastScheduleDelta: debug.lastScheduleDelta || 0,
        currentlyPlayingSeq: metrics.currentlyPlayingSeq,
        currentRenderHubTimeMs: metrics.currentRenderHubTimeMs,
        sequenceNormalizedRenderMs: metrics.sequenceNormalizedRenderMs,
        lastScheduledSeq: metrics.lastScheduledSeq,
        currentPlayTimeHub: currentPlayTimeHub,
        pipelineLatencyMs: Math.round(pipelineLatencyMs),
        userAgent: (typeof navigator !== 'undefined') ? navigator.userAgent : '',
        // Echo detection
        echoDetections: debug.echoDetections || 0,
        // Scheduler diagnostics — reveals WHY buffer grows/shrinks
        schedulerHeadroomMs: this._audioCtx
          ? Math.round((this._nextPlayTime - this._audioCtx.currentTime) * 1000)
          : 0,
        safetyClampCount: this._safetyClampCount || 0,
        reAnchorCount: metrics.reAnchorCount,
        lastReAnchorMsAgo: metrics.lastReAnchorMsAgo,
        stallRecoveryCount: metrics.stallRecoveryCount,
        lastStallRecoveryMsAgo: metrics.lastStallRecoveryMsAgo,
        syncAnchorMs: Math.round(this._syncAnchorSec * 1000),
        hubPaddingMs: Math.round(this._hubPaddingMs),
        appliedPaddingMs: Math.max(0, Math.round(this._syncAnchorSec * 1000 - this._baseSyncAnchorMs)),
        audioContextSampleRate: this._audioCtx ? this._audioCtx.sampleRate : 0,
        audioContextBaseLatency: this._audioCtx ? (this._audioCtx.baseLatency || 0) * 1000 : 0,
        bufferTargetChunks: this._bufferTargetChunks,
        // Drift corrector internals for debugging
        driftDebug: driftDebug,
        recentScheduleEvents: this._scheduleLog.slice(-100),
        oracleEvents: this._oracleEvents.slice(-50),
      },
    };

    try {
      this._ws.send(JSON.stringify(report));
      this._log('Sent diagnostics_report (request_id=%s)', requestId);
    } catch {
      // WebSocket may have closed between check and send
    }
  }

  // ---------------------------------------------------------------- //
  // Binary frame handling                                             //
  // ---------------------------------------------------------------- //

  /**
   * Parse and buffer an incoming binary frame.
   * @param {ArrayBuffer} data
   * @private
   */
  _handleBinaryFrame(data) {
    // Accept both 16-bit (3856) and 24-bit (5776) frame sizes
    let is24bit = false;
    if (data.byteLength === FRAME_SIZE_24) {
      is24bit = true;
    } else if (data.byteLength !== FRAME_SIZE) {
      this._log('Invalid frame size: %d (expected %d or %d)', data.byteLength, FRAME_SIZE, FRAME_SIZE_24);
      return;
    }

    const view = new DataView(data);

    // Parse header (little-endian)
    const playAt = view.getFloat64(0, true);
    const sequence = view.getUint32(8, true);
    const flags = view.getUint16(12, true);
    const formatVersion = view.getUint16(14, true);
    const isExplicit16bit = formatVersion === FORMAT_16BIT;
    const isSilence = (flags & FLAG_SILENCE) !== 0;

    // Confirm 24-bit from format version header field
    if (formatVersion === FORMAT_24BIT) {
      is24bit = true;
    } else if (isExplicit16bit && data.byteLength === FRAME_SIZE) {
      is24bit = false;
    }

    this._chunksReceived++;
    if (isSilence) {
      this._chunksSilence++;
    }

    // Sequence gap detection
    if (this._lastSequence >= 0) {
      const expected = this._lastSequence + 1;

      // Backward jump detection: stamper restarted (sequence reset to 0)
      // while the WebSocket stayed connected. Accept the new sequence epoch.
      if (sequence < expected && this._lastSequence - sequence > 1000) {
        this._log(
          'Sequence backward jump: %d → %d (stamper restart), resetting',
          this._lastSequence, sequence,
        );
        this._lastSequence = -1;
        // Treat as track transition — re-anchor when this chunk reaches scheduler
        if (this._state === 'playing') {
          this._trackTransitionPending = true;
          this._newTrackFirstSeq = sequence;
        }
        // Fall through to accept this chunk
      } else if (sequence !== expected && sequence > expected) {
        const gap = sequence - expected;
        this._sequenceGaps++;
        this._log('Sequence gap: expected %d, got %d (gap=%d)', expected, sequence, gap);

        // Track transition detection: large gap indicates hub switched tracks.
        // Small gaps are network jitter — only flag transitions for large jumps.
        if (gap > TRACK_TRANSITION_SEQ_GAP && this._state === 'playing') {
          this._trackTransitionPending = true;
          this._newTrackFirstSeq = sequence;
          this._log('Track transition detected (gap=%d) — will re-anchor at seq %d', gap, sequence);
        }
      } else if (sequence <= this._lastSequence) {
        // Ignore duplicates (sequence <= lastSequence)
        return;
      }
    }
    this._lastSequence = sequence;

    // Extract PCM payload as a DataView for later float32 conversion
    const pcmPayloadSize = is24bit ? PCM_SIZE_24 : PCM_SIZE;
    const pcmInt16 = new DataView(data, HEADER_SIZE, pcmPayloadSize);
    // INSTRUMENTATION: log first few chunks received (to verify sample range)
    if (IS_DEV && (this._chunksReceived <= 5 || this._state === 'buffering')) {
      let maxVal = -Infinity; let minVal = Infinity;
      const stride = is24bit ? 6 : 4; // bytes per stereo sample pair
      for (let i = 0; i < pcmPayloadSize; i += stride) {
        const s = is24bit
          ? pcmInt16.getUint8(i) | (pcmInt16.getUint8(i + 1) << 8) | (pcmInt16.getUint8(i + 2) << 16)
          : pcmInt16.getInt16(i, true);
        if (s > maxVal) maxVal = s;
        if (s < minVal) minVal = s;
      }
      console.log('[SpokeAudio] chunk %d: isSilence=%s, maxSample=%d, minSample=%d, state=%s', sequence, isSilence, maxVal, minVal, this._state);
    }

    // Insert into sorted buffer (almost always appended at end since
    // chunks arrive in order)
    const chunk = { playAt, sequence, flags, pcmInt16, is24bit };

    if (this._buffer.length === 0 || playAt >= this._buffer[this._buffer.length - 1].playAt) {
      this._buffer.push(chunk);
    } else {
      // Binary search for insertion point (rare: out-of-order arrival)
      let lo = 0;
      let hi = this._buffer.length;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (this._buffer[mid].playAt < playAt) {
          lo = mid + 1;
        } else {
          hi = mid;
        }
      }
      this._buffer.splice(lo, 0, chunk);
    }

    // Cap buffer size
    while (this._buffer.length > MAX_BUFFER_CHUNKS) {
      this._buffer.shift();
      this._chunksDropped++;
    }

    // Transition: buffering → playing when we have enough chunks AND config received.
    // Config gate (Break 3) ensures the spoke uses the hub-computed buffer target.
    // Chunks accumulate while waiting for config — this is fine.
    if (this._state === 'buffering'
        && this._configReceived
        && this._buffer.length >= this._bufferTargetChunks) {
      this._trimBurstExcess();
      this._startPlayback();
    }

    // Transition: underrun → buffering (scheduler will handle the rest)
    if (this._state === 'underrun' && this._buffer.length >= this._underrunRecoveryStartChunks()) {
      // Cancel the underrun watchdog — the hub is sending again.
      if (this._underrunWatchdogTimer !== null) {
        clearTimeout(this._underrunWatchdogTimer);
        this._underrunWatchdogTimer = null;
      }
      this._trimBurstExcess();
      this._log('Buffer refilled (%d chunks), resuming playback', this._buffer.length);
      this._startPlayback();
    }

    // Emit metrics periodically (every 25 chunks ≈ 500ms)
    if (this._chunksReceived % 25 === 0) {
      this._emitMetrics();
    }
  }

  // ---------------------------------------------------------------- //
  // Playback scheduling                                               //
  // ---------------------------------------------------------------- //

  /**
   * Trim burst excess: keep only the most recent bufferTargetChunks + margin.
   *
   * On connect, the hub sends a ring buffer burst (up to 125 frames = 2.5s).
   * With running counter scheduling, the spoke plays ALL frames in order and
   * never catches up — causing persistent delay equal to the burst size.
   * Trimming before playback start ensures the spoke begins near-live.
   * @private
   */
  _trimBurstExcess() {
    const maxKeep = this._bufferTargetChunks + 5;
    if (this._buffer.length > maxKeep) {
      const excess = this._buffer.length - maxKeep;
      this._buffer.splice(0, excess);
      this._chunksTrimmed += excess;
      // Tell drift corrector about the trim so it doesn't mistake the depth
      // drop for a network perturbation and freeze correction.
      this._driftCorrector.notifyTrim(this._buffer.length);
      // Reset crossfade state — trimmed chunks create a temporal gap
      this._prevChunkLastSampleL = NaN;
      this._prevChunkLastSampleR = NaN;
      this._needsDeclick = true;
      this._log(
        'Trimmed %d excess burst chunks (kept %d, target %d)',
        excess, this._buffer.length, this._bufferTargetChunks,
      );
    }
  }

  /**
   * Return the buffer depth required before restarting after an underrun.
   *
   * Initial playback can begin at the jitter-buffer target.  Recovery needs a
   * small extra cushion because the recovering spoke often also receives a
   * large sync-padding correction; restarting at the bare minimum immediately
   * drains below the common-sequence proof window.
   *
   * @returns {number}
   * @private
   */
  _underrunRecoveryStartChunks() {
    return this._bufferTargetChunks + UNDERRUN_RECOVERY_EXTRA_CHUNKS;
  }

  /**
   * Anchor the running counter and begin playback.
   *
   * Uses a simple running counter anchored to AudioContext.currentTime.
   * No hub timestamp mapping — hub timestamps are only used for clock
   * sync drift rate computation and desync detection.
  * @private
  */
  _startPlayback() {
    if (IS_DEV) {
      console.log('[SpokeAudio] _startPlayback() called: state=%s, gainValue=%.4f, audioCtxState=%s, audioCtxSampleRate=%d', this._state, this._gainNode ? this._gainNode.gain.value : -1, this._audioCtx ? this._audioCtx.state : 'null', this._audioCtx ? this._audioCtx.sampleRate : -1);
    }
    const wasUnderrun = this._state === 'underrun';
    if (!this._audioCtx || !this._clockSync) {
      return;
    }

    const syncState = this._clockSync.getState();
    if (!syncState.synced) {
      this._log('Clock not synced yet — staying in buffering');
      return;
    }

    // Ensure AudioContext is running before anchoring the counter.
    // On iOS, AudioContext starts suspended and resume() completes async.
    if (this._audioCtx.state === 'suspended' || this._audioCtx.state === 'interrupted') {
      this._audioCtx.resume().catch(() => {});
      this._log('AudioContext not running (state=%s) — deferring playback', this._audioCtx.state);
      return;
    }

    if (this._audioCtx.state !== 'running') {
      this._log('AudioContext in unexpected state=%s — deferring playback', this._audioCtx.state);
      return;
    }

    this._lastChunkPlayedTime = performance.now();

    // Cancel any source nodes still pending from a previous playback pass
    // (e.g. underrun recovery) before anchoring a new counter.  Without this,
    // old nodes overlap the new schedule and the active-node count grows
    // unboundedly — the root cause of iOS garbled audio.
    this._cancelPendingNodes();

    // Anchor running counter to hub timestamp of first buffered chunk.
    // Uses _syncAnchorSec (playout timing offset, decoupled from buffer depth)
    // so the spoke's total playout delay matches the hub's total playout
    // delay from ChunkStamper stamp to speaker.
    // syncState is already checked/read above.
    if (this._buffer.length > 0) {
      const firstChunkLocalTime = this._buffer[0].playAt - syncState.offset;
      // Convert performance.now()/1000 scale -> audioCtx.currentTime scale.
      // play_at - offset lands in perf.now domain (~page-uptime seconds).
      // audioCtx.currentTime counts from context creation (different epoch).
      // perfToCtx is the constant offset between the two clocks.
      const perfToCtx = this._audioCtx.currentTime - performance.now() / 1000;
      this._nextPlayTime = Math.max(
        firstChunkLocalTime + perfToCtx + this._syncAnchorSec,
        this._audioCtx.currentTime + this._syncAnchorSec,
      );
      if (IS_DEV) {
        console.warn('[SYNC-DIAG] Anchor: syncAnchorSec=%.3f, bufferTargetSec=%.3f, firstChunkLocalTime=%.3f, perfToCtx=%.3f, nextPlayTime=%.3f, ctxNow=%.3f, bufferChunks=%d',
          this._syncAnchorSec, this._bufferTargetSec, firstChunkLocalTime, perfToCtx,
          this._nextPlayTime, this._audioCtx.currentTime, this._buffer.length);
      }
    } else {
      this._nextPlayTime = this._audioCtx.currentTime + this._syncAnchorSec;
    }
    this._timeMapped = true;
    this._desyncAnchorSet = false;
    this._safetyClampCount = 0;
    this._consecutiveClamps = 0;

    this._log(
      'Running counter anchored (nextPlayTime=%.3f, ctx=%.3f, buffer=%d chunks)',
      this._nextPlayTime,
      this._audioCtx.currentTime,
      this._buffer.length,
    );

    if (wasUnderrun && this._pendingUnderrunReanchorReport) {
      const nowMs = (typeof performance !== 'undefined' && performance.now)
        ? performance.now()
        : Date.now();
      this._syncConfigReanchorCount += 1;
      this._reAnchorCount = (this._reAnchorCount || 0) + 1;
      this._lastSyncConfigReanchorAt = nowMs;
      this._recordOracleEvent('underrun_reanchor', {
        nextPlayTime: Number(this._nextPlayTime.toFixed(3)),
        syncAnchorMs: Math.round(this._syncAnchorSec * 1000),
      });
    }
    this._pendingUnderrunReanchorReport = false;

    this._setState('playing');

    // Fade-in ramp to eliminate click at start of audio after silence.
    // The AudioContext output is silence (0) before the first chunk plays.
    // Without a fade-in, the abrupt jump to audio amplitude causes a click.
    if (this._gainNode && this._audioCtx) {
      const targetGain = this._muted ? 0 : this._volumeBeforeMute;
      this._rampEndTime = this._audioCtx.currentTime + 0.1;
      this._gainNode.gain.cancelScheduledValues(this._audioCtx.currentTime);
      this._gainNode.gain.setValueAtTime(0, this._audioCtx.currentTime);
      this._gainNode.gain.linearRampToValueAtTime(
        targetGain,
        this._rampEndTime,
      );
    }

    this._startScheduler();
    this._emitMetrics();
  }

  /** @private */
  _startScheduler() {
    if (this._schedulerTimer !== null) {
      return;
    }
    this._schedulerTimer = setInterval(this._schedulerTick, SCHEDULER_TICK_MS);
  }

  /** @private */
  _stopScheduler() {
    if (this._schedulerTimer !== null) {
      clearInterval(this._schedulerTimer);
      this._schedulerTimer = null;
    }
  }

  /**
   * Scheduler tick: examine buffer and schedule chunks whose target
   * playout time is within the lookahead window.
   *
   * Uses a running counter (_nextPlayTime) that advances by the actual
   * output sample count (959, 960, or 961) divided by the sample rate.
   * Hub timestamps are NOT used for per-chunk scheduling.
   * @private
   */
  _schedulerTick() {
    if (!this._audioCtx || !this._timeMapped) {
      return;
    }

    // Scheduling-progress watch runs before the teardown watchdog below so
    // in-place recovery gets its attempts in first (last resort stays last).
    this._updateSchedulerProgressWatch();

    if (this._state === 'playing'
        && this._lastChunkPlayedTime > 0
        && performance.now() - this._lastChunkPlayedTime > SILENCE_WATCHDOG_TIMEOUT_MS
        && this._ws
        && this._ws.readyState === WebSocket.OPEN) {
      console.warn('[SpokeAudio] Silence watchdog triggered after %d ms without scheduled chunks', Math.round(performance.now() - this._lastChunkPlayedTime));
      this._log(
        'Silence watchdog triggered after %d ms without scheduled chunks — closing WebSocket',
        Math.round(performance.now() - this._lastChunkPlayedTime),
      );
      this._ws.close();
      return;
    }

    const now = this._audioCtx.currentTime;
    const horizon = now + this._lookaheadSec;
    let scheduled = 0;

    this._analyserTickCounter++;

    // --- Continuous buffer enforcement — prevent burst accumulation ---
    // On connect, the hub sends a ring buffer burst (up to 125 frames).
    // _trimBurstExcess() catches this on initial playback, but if chunks
    // accumulate during live playback (e.g., network stall then burst),
    // the running counter can't catch up because consumption = arrival rate.
    // This guard trims excess every tick to keep delay bounded.
    //
    // Continuous buffer enforcement — trim excess to keep inter-spoke
    // spread within the 40ms sync target.  At target+8, a spoke that
    // connects with a burst sits at 340ms for 200s while the drift
    // corrector slowly drains 3 extra chunks.  Meanwhile inter-spoke
    // spread reads 60ms (FAIL).  Tightened: trigger at target+3 (60ms
    // margin, still above natural ±1 chunk oscillation), trim to
    // target+1 (20ms above target — converges fully in ~60s via drift).
    const maxDepthChunks = this._bufferTargetChunks + 3; // 60ms margin
    if (this._state === 'playing' && this._chunksPlayed > 100
        && this._buffer.length > maxDepthChunks
        && performance.now() - this._lastTrimTime >= 2000) {
      const excess = this._buffer.length - (this._bufferTargetChunks + 1);
      const wasBefore = this._buffer.length;
      for (let i = 0; i < excess; i++) {
        this._buffer.shift();
      }
      this._chunksTrimmed += excess;
      this._lastTrimTime = performance.now();
      this._desyncAnchorSet = false;
      this._consecutiveClamps = 0;
      // Tell drift corrector about the trim so it doesn't mistake the depth
      // drop for a network perturbation and freeze correction.
      this._driftCorrector.notifyTrim(this._buffer.length);
      // Reset crossfade state — trimmed chunks create a temporal gap
      this._prevChunkLastSampleL = NaN;
      this._prevChunkLastSampleR = NaN;
      this._needsDeclick = true;
      this._log(
        'Buffer trim: discarded %d chunks (was %d, now %d, target %d)',
        excess, wasBefore, this._buffer.length, this._bufferTargetChunks,
      );
    }

    // Update output latency for metrics display (not used in scheduling)
    if (this._analyserTickCounter % 10 === 0) {
      this._updateOutputLatency();
    }

    // Send lightweight sync report to hub every ~1s (40 ticks at 25ms).
    // Hub uses effective playout-delay spread across spokes to compute
    // per-spoke padding for cross-platform sync normalization.
    if (this._state === 'playing' && this._analyserTickCounter % 40 === 20
        && this._ws && this._ws.readyState === WebSocket.OPEN) {
      const metrics = this.getMetrics();
      const roomName = (typeof localStorage !== 'undefined'
        && localStorage.getItem('viola_room_name')) || 'Speaker';
      this._ws.send(JSON.stringify({
        type: 'sync_report',
        room_name: roomName,
        headroom_ms: this._nextPlayTime
          ? Math.round((this._nextPlayTime - this._audioCtx.currentTime) * 1000)
          : 0,
        anchor_ms: Math.round(this._syncAnchorSec * 1000),
        output_latency_ms: Math.round(this._outputLatency * 1000),
        hub_padding_ms: Math.round(this._hubPaddingMs),
        base_anchor_ms: Math.round(this._baseSyncAnchorMs),
        applied_padding_ms: Math.max(0, Math.round(this._syncAnchorSec * 1000 - this._baseSyncAnchorMs)),
        currently_playing_seq: metrics.currentlyPlayingSeq,
        render_hub_time_ms: metrics.currentRenderHubTimeMs,
        sequence_normalized_render_ms: metrics.sequenceNormalizedRenderMs,
        re_anchor_count: metrics.reAnchorCount,
        last_scheduled_seq: metrics.lastScheduledSeq,
        last_sequence: this._lastSequence,
      }));
    }

    // --- Layer 8: Read AnalyserNode signal data (every 10th tick = 250ms) ---
    if (this._analyserTickCounter % 10 === 0 && this._analyser) {
      const buf = new Float32Array(this._analyser.fftSize);
      this._analyser.getFloatTimeDomainData(buf);
      let aSumSq = 0;
      let aPeak = 0;
      for (let i = 0; i < buf.length; i++) {
        const v = buf[i];
        aSumSq += v * v;
        const abs = v < 0 ? -v : v;
        if (abs > aPeak) aPeak = abs;
      }
      this._analyserRMS = Math.sqrt(aSumSq / buf.length);
      this._analyserPeak = aPeak;
    }

    // --- Scheduling loop: running counter, no hub timestamps ---
    while (this._buffer.length > 0) {
      if (this._nextPlayTime > horizon) break;

      const chunk = this._buffer.shift();

      // Track transition re-anchor: when the first chunk of a new track
      // reaches the scheduler, re-anchor the running counter to create a
      // clean sync point. This prevents offset accumulation across tracks
      // caused by the silence gap during hub's track switch.
      if (this._trackTransitionPending && chunk.sequence >= this._newTrackFirstSeq) {
        const oldNext = this._nextPlayTime;
        // Track transition: anchor to hub timestamp for consistency with _startPlayback.
        // Same clock domain conversion: perf.now -> audioCtx.currentTime via perfToCtx.
        const _txState = this._clockSync ? this._clockSync.getState() : { offset: 0 };
        const _txLocalTime = chunk.playAt - _txState.offset;
        const _txPerfToCtx = now - performance.now() / 1000;
        this._cancelPendingNodes();
        this._nextPlayTime = Math.max(_txLocalTime + _txPerfToCtx + this._syncAnchorSec, now + this._syncAnchorSec);
        this._trackTransitionPending = false;
        this._newTrackFirstSeq = -1;
        this._desyncAnchorSet = false;
        this._consecutiveClamps = 0;

        // Reset crossfade state so we don't blend old track into new track
        this._prevChunkLastSampleL = NaN;
        this._prevChunkLastSampleR = NaN;
        this._needsDeclick = true;
        // Reset the drift corrector — reset() re-arms its warmup window,
        // which suppresses buffer-derived correction while transition
        // turbulence settles (clock-term correction continues).
        this._driftCorrector.reset();
        this._driftCorrector.setBufferTarget(Math.max(2, this._bufferTargetChunks));

        this._log(
          'Track transition re-anchor: nextPlayTime=%.3f → %.3f (seq=%d, buffer=%d)',
          oldNext, this._nextPlayTime, chunk.sequence, this._buffer.length,
        );
      }

      // Safety clamp: re-anchor before computing scheduleAt if the running
      // counter fell far behind (e.g. iOS timer throttling in a background tab).
      if (now - this._nextPlayTime > 0.05) {
        this._log(
          'Safety re-anchor: nextPlayTime=%.3f far behind now=%.3f -> %.3f',
          this._nextPlayTime, now, now + this._bufferTargetSec,
        );
        this._cancelPendingNodes();
        this._nextPlayTime = now + this._bufferTargetSec;
        this._consecutiveClamps = 0;
        this._desyncAnchorSet = false;
      }

      // Use _syncAnchorSec (not 0.002) as the scheduling floor so iOS Safari
      // always gets enough lead time to reliably start AudioBufferSourceNode.
      // When clamped, re-align _nextPlayTime to scheduleAt so subsequent chunks
      // advance from the correct base (not from the stale behind value).
      const scheduleAt = Math.max(this._nextPlayTime, now + this._syncAnchorSec);
      if (scheduleAt > this._nextPlayTime + 0.001) {
        this._nextPlayTime = scheduleAt; // re-align counter to actual schedule
        this._safetyClampCount++;
        this._consecutiveClamps++;
        if (this._safetyClampCount <= 5 || this._safetyClampCount % 50 === 0) {
          this._log(
            'Safety clamp: nextPlayTime re-aligned to %.3f (now=%.3f, count=%d, consecutive=%d)',
            scheduleAt, now, this._safetyClampCount, this._consecutiveClamps,
          );
        }
        // Re-anchor (with node cancel) if stuck behind for > 10 consecutive chunks
        if (this._consecutiveClamps > 10) {
          this._log(
            'Re-anchoring: %d consecutive clamps, nextPlayTime=%.3f → %.3f',
            this._consecutiveClamps, this._nextPlayTime,
            now + this._syncAnchorSec,
          );
          this._cancelPendingNodes();
          this._nextPlayTime = now + this._syncAnchorSec;
          this._consecutiveClamps = 0;
          this._desyncAnchorSet = false;
        }
      } else {
        this._consecutiveClamps = 0;
      }

      // Desync detection: compare hub timestamp progression with running counter
      if (!this._desyncAnchorSet) {
        this._firstHubTimestamp = chunk.playAt;
        this._firstCounterTime = this._nextPlayTime;
        this._desyncAnchorSet = true;
      } else {
        const expectedDelta = chunk.playAt - this._firstHubTimestamp;
        const actualDelta = this._nextPlayTime - this._firstCounterTime;
        const desync = Math.abs(expectedDelta - actualDelta);
        if (desync > DESYNC_THRESHOLD_SEC) {
          this._log(
            'Desync detected: hub delta=%.3f, counter delta=%.3f, gap=%.3f — re-anchoring',
            expectedDelta, actualDelta, desync,
          );
          // Cancel not-yet-started nodes before resetting the counter — same
          // as every other re-anchor path. Without this, chunks scheduled from
          // the reset counter overlap the still-queued old nodes (double audio).
          this._cancelPendingNodes();
          this._nextPlayTime = now + this._syncAnchorSec;
          this._firstHubTimestamp = chunk.playAt;
          this._firstCounterTime = this._nextPlayTime;
          this._reAnchorCount = (this._reAnchorCount || 0) + 1;
          this._lastReAnchorAt = (typeof performance !== 'undefined' && performance.now)
            ? performance.now()
            : Date.now();
          this._recordOracleEvent('desync_reanchor', {
            expectedDelta: Number(expectedDelta.toFixed(3)),
            actualDelta: Number(actualDelta.toFixed(3)),
            desyncSec: Number(desync.toFixed(3)),
          });
        }
      }

      this._scheduleChunk(chunk, scheduleAt);
      scheduled++;
    }

    // Buffer underrun detection with concealment
    if (this._buffer.length === 0 && this._state === 'playing') {
      // Concealment: instead of hard silence, generate a fade-out chunk from
      // the last audio samples. This makes single-chunk drops nearly inaudible
      // by smoothly transitioning to silence over 10ms instead of an abrupt gap.
      if (this._audioCtx && !Number.isNaN(this._prevChunkLastSampleL)) {
        const ctx = this._audioCtx;
        const fadeLen = Math.min(CONCEALMENT_FADE_SAMPLES, SAMPLES_PER_CHUNK);
        const concealBuf = ctx.createBuffer(CHANNELS, SAMPLES_PER_CHUNK, SAMPLE_RATE);
        const concealL = concealBuf.getChannelData(0);
        const concealR = concealBuf.getChannelData(1);
        const startL = this._prevChunkLastSampleL;
        const startR = this._prevChunkLastSampleR;
        // Fade from last sample to silence over fadeLen samples
        for (let i = 0; i < fadeLen; i++) {
          const t = 1.0 - (i / fadeLen);
          concealL[i] = startL * t;
          concealR[i] = startR * t;
        }
        // Rest is already zeroed (silence)
        const concealSource = ctx.createBufferSource();
        concealSource.buffer = concealBuf;
        if (this._gainNode) {
          concealSource.connect(this._gainNode);
        } else {
          concealSource.connect(ctx.destination);
        }
        const concealWhen = Math.max(this._nextPlayTime, ctx.currentTime + 0.002);
        concealSource.start(concealWhen);
        this._nextPlayTime = concealWhen + SAMPLES_PER_CHUNK / SAMPLE_RATE;
        // Cleanup
        const chunkMs = (SAMPLES_PER_CHUNK / SAMPLE_RATE) * 1000;
        const safeMs = Math.max(0, (concealWhen - ctx.currentTime) * 1000 + chunkMs) + 100;
        setTimeout(() => {
          try { concealSource.disconnect(); } catch { /* already disconnected */ }
          concealSource.buffer = null;
        }, safeMs);
        this._log('Concealment: fade-to-silence chunk scheduled (%.3f)', concealWhen);
      }

      this._log('Buffer underrun — waiting for refill');
      this._setState('underrun');
      this._stopScheduler();
      this._syncConfigReanchorCount = 0;
      this._lastSyncConfigReanchorAt = 0;
      if (this._chunksPlayed > 250) {
        this._pendingUnderrunReanchorReport = true;
      }
      // Set prev samples to 0.0 so recovery de-clicks from silence
      // (matching what the listener heard during the underrun gap).
      this._prevChunkLastSampleL = 0.0;
      this._prevChunkLastSampleR = 0.0;
      this._needsDeclick = true;
      // Start a watchdog so the engine does not stay in underrun forever when
      // the hub goes silent.  The in-scheduler watchdog stops firing once the
      // scheduler is stopped, so we install a separate timer here.
      if (this._underrunWatchdogTimer === null) {
        this._underrunWatchdogTimer = setTimeout(() => {
          this._underrunWatchdogTimer = null;
          if (this._state === 'underrun') {
            this._log(
              'Underrun watchdog: no recovery in %dms — closing WS',
              SILENCE_WATCHDOG_TIMEOUT_MS,
            );
            if (this._ws && this._ws.readyState === WebSocket.OPEN) {
              this._ws.close();
            }
          }
        }, SILENCE_WATCHDOG_TIMEOUT_MS);
      }
    }

    // Emit metrics periodically (every 25th chunk ≈ 500ms at steady state)
    if (scheduled > 0 && this._chunksPlayed % 25 === 0) {
      this._emitMetrics();
    }
  }

  /**
   * Schedule a single chunk for playback via Web Audio API.
   *
   * Runs the PCM data through the drift corrector, which deinterleaves
   * int16 to float32 and may add or remove one sample via interpolation
   * for continuous drift correction.
   *
   * Advances _nextPlayTime by sampleCount / SAMPLE_RATE (the actual
   * output duration, which is 959/48000, 960/48000, or 961/48000).
   *
   * @param {{playAt: number, sequence: number, flags: number, pcmInt16: DataView}} chunk
   * @param {number} when - AudioContext time to start playback
   * @private
   */

  /**
   * Scheduling-progress watch: detect a wedged playback path — state
   * 'playing', chunks queued, AudioContext reporting 'running' — that
   * schedules nothing, and heal it in place instead of wedging silently
   * until the teardown watchdogs close the WebSocket.
   *
   * Live incident (2026-07-01, iPhone Safari foreground stall on video
   * tracks): two episodic wedge modes, neither of which fires the
   * onstatechange → _recomputeTimeMapping recovery because the context
   * state never leaves 'running':
   *   (a) frozen render clock — currentTime stops advancing; the buffer
   *       grew 280ms → 1540ms toward the 250-chunk cap with rendered RMS 0
   *       while receive RMS was 0.19;
   *   (b) main-thread starvation — the tick and WS ingestion stop for
   *       seconds, then ~1.8s of backlog arrives in one gulp and renders
   *       silent until a later recovery.
   * Recovery previously happened only via the 5s silence/underrun
   * watchdogs closing the WebSocket — reconnect backoff, clock re-sync,
   * and rebuffering every ~40-60s. This watch is trigger-agnostic: it
   * keys on observable non-progress (nothing scheduled while chunks are
   * queued), not on any single cause.
   *
   * The watch itself may be starved with everything else, so it reasons
   * from elapsed-time deltas between the ticks that actually ran and
   * caps per-interval credit (PLAYBACK_STALL_MAX_TICK_CREDIT_MS) — one
   * multi-second timer gap can never trigger recovery by itself.
   *
   * False-positive exclusions:
   * - Clock advancing with no chunks due (silence padding, paused hub):
   *   silence frames are scheduled like any chunk (progress), and a
   *   paused hub drains the buffer to empty — accrual requires queued
   *   chunks (this._buffer.length > 0).
   * - Background tab: hidden tabs are excluded outright (the visibility
   *   pause protocol intentionally stops frames there) on top of the
   *   per-interval credit cap for throttled timers.
   * - Suspended/interrupted context: onstatechange recovery owns those;
   *   accrual requires state === 'running'.
   * @private
   */
  _updateSchedulerProgressWatch() {
    const ctx = this._audioCtx;
    if (!ctx) return;

    const wallNowMs = performance.now();
    const ctxNowMs = ctx.currentTime * 1000;
    const chunksPlayedNow = this._chunksPlayed;
    const prevWallMs = this._stallWatchLastWallMs;
    const prevCtxMs = this._stallWatchLastCtxTime;
    const prevChunksPlayed = this._stallWatchLastChunksPlayed;
    this._stallWatchLastWallMs = wallNowMs;
    this._stallWatchLastCtxTime = ctxNowMs;
    this._stallWatchLastChunksPlayed = chunksPlayedNow;
    if (prevCtxMs < 0 || prevWallMs <= 0) return;

    const wallDeltaMs = wallNowMs - prevWallMs;
    if (wallDeltaMs <= 0) return;

    // Progress: the scheduler consumed at least one chunk since the last
    // observed tick — playback is moving, clear the stall window.
    if (chunksPlayedNow > prevChunksPlayed) {
      this._resetStallWindow();
      return;
    }

    // No scheduling progress across this interval. Accrue only when a
    // wedge is actually holding queued audio (see doc comment for the
    // excluded cases).
    if (this._state !== 'playing'
        || this._buffer.length === 0
        || ctx.state !== 'running'
        || this._stallRecoveryInFlight
        || (typeof document !== 'undefined' && document.visibilityState === 'hidden')) {
      this._resetStallWindow();
      return;
    }

    this._stallWallMsRaw += wallDeltaMs;
    this._stallCtxMsRaw += Math.max(0, ctxNowMs - prevCtxMs);
    this._stallCreditMs += Math.min(wallDeltaMs, PLAYBACK_STALL_MAX_TICK_CREDIT_MS);

    if (this._stallCreditMs >= PLAYBACK_STALL_TRIGGER_MS) {
      // Classify the wedge from the clock's behavior across the window:
      // frozen render clock (needs a suspend/resume kick) vs advancing
      // clock with a stranded running counter (needs a re-anchor).
      const clockFrozen =
        this._stallCtxMsRaw < this._stallWallMsRaw * FROZEN_CLOCK_MIN_ADVANCE_RATIO;
      this._resetStallWindow();
      this._recoverStalledPlayback(clockFrozen);
    }
  }

  /** Clear the scheduling-progress stall window accumulators. @private */
  _resetStallWindow() {
    this._stallCreditMs = 0;
    this._stallWallMsRaw = 0;
    this._stallCtxMsRaw = 0;
  }

  /**
   * Heal a playback stall in place. Never closes the WebSocket — the
   * silence/underrun teardown watchdogs remain the last resort if this
   * fails (PLAYBACK_STALL_TRIGGER_MS is sized so at least two attempts
   * happen before they fire).
   *
   * Both modes cancel pending nodes first (they were scheduled against a
   * stale timeline — same contract as every other re-anchor path) and
   * trim backlog via _trimBurstExcess, whose notifyTrim keeps the drift
   * corrector from reading the burst drop as network perturbation. That
   * makes recovery tolerant of the incident's ~1.8s one-gulp backlog
   * re-delivery instead of turning it into late playback plus pinned
   * drift correction.
   *
   * - clockFrozen: suspend()/resume() kick. The resume transition fires
   *   onstatechange('running'), which reuses the existing recovery
   *   vocabulary — _trimBurstExcess + forward-only _recomputeTimeMapping.
   * - counter stranded (clock advancing): direct re-anchor of the running
   *   counter to now + syncAnchor, mirroring the desync re-anchor path.
   *
   * @param {boolean} clockFrozen - render clock frozen vs counter stranded
   * @private
   */
  _recoverStalledPlayback(clockFrozen) {
    const ctx = this._audioCtx;
    if (!ctx || this._stallRecoveryInFlight) return;
    this._stallRecoveryCount += 1;
    this._lastStallRecoveryAt = performance.now();
    this._log(
      'Playback stall: ctx state=%s, %d chunks queued, no scheduling progress — %s recovery #%d',
      ctx.state, this._buffer.length,
      clockFrozen ? 'frozen-clock kick' : 're-anchor',
      this._stallRecoveryCount,
    );
    this._recordOracleEvent('playback_stall_recovery', {
      mode: clockFrozen ? 'clock_kick' : 'reanchor',
      attempt: this._stallRecoveryCount,
      ctxState: ctx.state,
    });

    this._cancelPendingNodes();
    this._trimBurstExcess();

    if (!clockFrozen) {
      // Stranded running counter: re-anchor to the live clock, same shape
      // as the desync re-anchor in the scheduler drain loop.
      this._nextPlayTime = ctx.currentTime + this._syncAnchorSec;
      this._desyncAnchorSet = false;
      this._consecutiveClamps = 0;
      return;
    }

    // Frozen render clock: kick it with a suspend/resume cycle. resume()
    // firing onstatechange('running') re-anchors via _recomputeTimeMapping.
    this._stallRecoveryInFlight = true;
    ctx.suspend()
      .then(() => ctx.resume())
      .catch(() => {
        // Kick failed (e.g. the platform rejects a programmatic resume) —
        // the teardown watchdogs recover via reconnect as last resort.
      })
      .then(() => {
        this._stallRecoveryInFlight = false;
        // Start the next measurement window fresh.
        this._stallWatchLastCtxTime = -1;
        this._stallWatchLastWallMs = 0;
        this._resetStallWindow();
      });
  }

  /**
   * Cancel all pending source nodes whose scheduled play time is in the future.
   * Called before re-anchoring to prevent stale nodes from overlapping new schedule.
   * @private
   */
  /**
   * Cancel pending (not-yet-started) source nodes.
   *
   * Called before re-anchoring to prevent stale nodes from overlapping
   * a new schedule.  Wraps the stop() calls in a short gain fade so
   * the transition is an imperceptible mute-and-restore rather than a
   * click.  Required whenever the scheduler detects desync or a
   * track-change gap — the old hard stop(0) produced audible pops on
   * cloud spokes whose clock-sync seed was too coarse.
   *
   * Fade sequence (all on ``_gainNode`` so every downstream node is
   * affected uniformly):
   *   t=0      → cancelAndHold current gain
   *   t=0…20ms → ramp to 0
   *   t=20ms  → source.stop() fires for each pending node
   *   t=20…40ms → ramp back to the prior gain
   *
   * The fade total (~40 ms) is below audible-gap threshold for music
   * and is covered by the normal jitter buffer depth on the receive
   * side, so new chunks scheduled in the same tick start playing
   * cleanly underneath the restoring ramp.
   *
   * @private
   */
  _cancelPendingNodes() {
    const ctx = this._audioCtx;
    if (!ctx || this._pendingSourceNodes.length === 0) return;
    const now = ctx.currentTime;

    const FADE_OUT_S = 0.020;
    const FADE_IN_S = 0.020;

    const gain = this._gainNode ? this._gainNode.gain : null;
    const restoreTarget = gain ? gain.value : 1.0;
    if (gain) {
      try {
        if (typeof gain.cancelAndHoldAtTime === 'function') {
          gain.cancelAndHoldAtTime(now);
        } else {
          gain.cancelScheduledValues(now);
          gain.setValueAtTime(restoreTarget, now);
        }
        gain.linearRampToValueAtTime(0, now + FADE_OUT_S);
        gain.linearRampToValueAtTime(restoreTarget, now + FADE_OUT_S + FADE_IN_S);
      } catch {
        // Gain ramping errors are non-fatal — fall through to the stop.
      }
    }

    const stopAt = now + FADE_OUT_S;
    for (const entry of this._pendingSourceNodes) {
      if (entry.when > now) {
        try { entry.source.stop(stopAt); } catch { /* already stopped */ }
      }
    }
    // onended fires for each stopped node → cleans up _pendingSourceNodes + _activeSourceNodes

    // Count re-anchor events for diagnostic / sync-quality surfaces.
    this._reAnchorCount = (this._reAnchorCount || 0) + 1;
    this._lastReAnchorAt = (typeof performance !== 'undefined' && performance.now)
      ? performance.now()
      : Date.now();
    this._recordOracleEvent('pending_nodes_cancelled', {
      cancelledNodes: this._pendingSourceNodes.length,
    });
  }

  _scheduleChunk(chunk, when) {
    const ctx = this._audioCtx;
    if (!ctx) return;

    this._diagCounter++;
    const doDiag = (this._diagCounter % 10 === 0);

    // Drift correction: deinterleave + sample interpolation
    const clockDriftPpm = this._getEffectiveClockDriftPpm();
    const bufferDepthChunks = this._getQueuedBufferDepthChunks(1);
    this._lastDriftBufferDepthChunks = bufferDepthChunks;
    const corrected = this._driftCorrector.process(
      chunk.pcmInt16,
      clockDriftPpm,
      bufferDepthChunks,
      chunk.is24bit,
    );
    const { left, right, sampleCount } = corrected;

    // --- Chunk boundary: measure discontinuity (informational) then de-click if needed ---
    // The old code applied a 96-sample linear ramp from a single held sample on EVERY
    // non-silence chunk, rewriting 10% of the waveform and creating 50 Hz chunk-rate
    // distortion (~10-14 dB SNR on loud content).  Now we only touch audio on actual
    // discontinuity events (trim, underrun recovery, track transition) using a short
    // fade-in from zero.
    {
      const isSilenceXfade = (chunk.flags & FLAG_SILENCE) !== 0;
      if (left.length > 0) {
        if (!isSilenceXfade) {
          // Measure raw discontinuity for diagnostics (informational only — no rewrite)
          if (!Number.isNaN(this._prevChunkLastSampleL)) {
            const disc = Math.abs(this._prevChunkLastSampleL - left[0]);
            this._boundaryDiscontinuitySum += disc;
            this._boundaryCount++;
            if (disc > this._boundaryMaxDiscontinuity) {
              this._boundaryMaxDiscontinuity = disc;
            }
          }
          // De-click: short fade-in from zero ONLY after a discontinuity event.
          // This fires once after trim / underrun recovery / track transition,
          // then clears the flag so subsequent chunks pass through unmodified.
          if (this._needsDeclick && left.length >= DECLICK_SAMPLES) {
            const n = DECLICK_SAMPLES;
            for (let i = 0; i < n; i++) {
              const t = i / n;        // 0 → 1 linear ramp
              left[i]  *= t;
              right[i] *= t;
            }
            this._crossfadeAppliedCount++;
            this._needsDeclick = false;
          }
          // Store last samples for next chunk (diagnostic tracking)
          this._prevChunkLastSampleL = left[left.length - 1];
          this._prevChunkLastSampleR = right[right.length - 1];
        } else {
          // Silence chunk: apply a short fade-out from last audio level so the
          // transition to silence is smooth, then mark next chunk for de-click.
          if (!Number.isNaN(this._prevChunkLastSampleL) &&
              !Number.isNaN(this._prevChunkLastSampleR) &&
              left.length >= DECLICK_SAMPLES) {
            const n = DECLICK_SAMPLES;
            const prevL = this._prevChunkLastSampleL;
            const prevR = this._prevChunkLastSampleR;
            for (let i = 0; i < n; i++) {
              const t = 1.0 - (i / n);  // 1 → 0 linear ramp
              left[i]  = prevL * t;
              right[i] = prevR * t;
            }
            // Zero the rest (buffer is already zeroed for silence chunks, but be safe)
            for (let i = n; i < left.length; i++) {
              left[i] = 0;
              right[i] = 0;
            }
            this._crossfadeAppliedCount++;
          }
          this._prevChunkLastSampleL = 0.0;
          this._prevChunkLastSampleR = 0.0;
          this._needsDeclick = true;  // next non-silence chunk needs fade-in
        }
      }
    }

    // --- Echo detector: fingerprint comparison ---
    {
      const FP_LEN = 8;
      const isSilenceEcho = (chunk.flags & FLAG_SILENCE) !== 0;
      if (!isSilenceEcho && left.length >= FP_LEN) {
        // Build fingerprint from first 8 left-channel samples
        const fp = new Float32Array(FP_LEN);
        for (let i = 0; i < FP_LEN; i++) fp[i] = left[i];

        // Compute magnitude of current fingerprint
        let magCur = 0;
        for (let i = 0; i < FP_LEN; i++) magCur += fp[i] * fp[i];
        magCur = Math.sqrt(magCur);

        // Compare against ring (skip very quiet chunks to avoid false positives)
        if (magCur > 0.001) {
          const now = performance.now();
          for (let j = 0; j < this._echoFingerprints.length; j++) {
            const prev = this._echoFingerprints[j];
            // Skip comparison with adjacent chunks (±2 seq) — they naturally overlap via crossfade
            if (Math.abs(chunk.sequence - prev.seq) <= 2) continue;

            let dot = 0, magPrev = 0;
            for (let i = 0; i < FP_LEN; i++) {
              dot += fp[i] * prev.fp[i];
              magPrev += prev.fp[i] * prev.fp[i];
            }
            magPrev = Math.sqrt(magPrev);
            if (magPrev < 0.001) continue;

            const similarity = dot / (magCur * magPrev);
            if (similarity > 0.92) {
              const delayMs = now - prev.time;
              this._echoDetections++;
              if (this._echoDetections <= 20 || this._echoDetections % 50 === 0) {
                console.warn(
                  '[SpokeAudio] ECHO_DETECTED #%d: seq %d ≈ seq %d (similarity=%.3f, delay=%.0fms, gap=%d chunks)',
                  this._echoDetections, chunk.sequence, prev.seq, similarity, delayMs,
                  chunk.sequence - prev.seq,
                );
              }
            }
          }

          // Add to ring
          this._echoFingerprints.push({ seq: chunk.sequence, time: now, fp });
          if (this._echoFingerprints.length > this._echoRingMax) {
            this._echoFingerprints.shift();
          }
        }
      }
    }

    // --- Recording tap: capture per-chunk raw vs corrected data ---
    if (this._isRecording && this._recordedChunks) {
      const rawSamples = SAMPLES_PER_CHUNK; // always 960
      const rawLeft = new Float32Array(rawSamples);
      if (chunk.is24bit) {
        const stride24 = CHANNELS * BYTES_PER_SAMPLE_24;
        for (let i = 0; i < rawSamples; i++) {
          const off = i * stride24;
          const b0 = chunk.pcmInt16.getUint8(off);
          const b1 = chunk.pcmInt16.getUint8(off + 1);
          const b2 = chunk.pcmInt16.getUint8(off + 2);
          let val = b0 | (b1 << 8) | (b2 << 16);
          if (val & 0x800000) val |= 0xFF000000;
          rawLeft[i] = val / 8388608;
        }
      } else {
        for (let i = 0; i < rawSamples; i++) {
          rawLeft[i] = chunk.pcmInt16.getInt16((i * CHANNELS) * BYTES_PER_SAMPLE, true) / 32768;
        }
      }
      this._recordedChunks.push({
        seq: chunk.sequence,
        rawLeft,
        corrLeft: new Float32Array(left),
        corrRight: new Float32Array(right),
        sampleCount,
      });
      this._recordingLength += sampleCount;
    }

    // Soft limiter: compress samples above SOFT_LIMIT_THRESHOLD to reduce
    // inter-sample peak overload after the 0.8× gain node + DAC reconstruction.
    // Uses tanh-style soft knee: above threshold, excess is compressed via tanh.
    // At threshold=0.85, post-gain peak ≈ 0.85×0.8 = 0.68, worst-case true
    // peak ≈ 0.68×1.41 = 0.96 — safely below 1.0 FS.
    {
      const thresh = SOFT_LIMIT_THRESHOLD;
      for (let ch = 0; ch < 2; ch++) {
        const buf = ch === 0 ? left : right;
        for (let i = 0; i < buf.length; i++) {
          const v = buf[i];
          if (v > thresh) {
            buf[i] = thresh + (1.0 - thresh) * Math.tanh((v - thresh) / (1.0 - thresh));
          } else if (v < -thresh) {
            buf[i] = -thresh - (1.0 - thresh) * Math.tanh((-v - thresh) / (1.0 - thresh));
          }
        }
      }
    }

    // Create AudioBuffer and schedule playback
    const audioBuffer = ctx.createBuffer(CHANNELS, sampleCount, SAMPLE_RATE);
    audioBuffer.getChannelData(0).set(left);
    audioBuffer.getChannelData(1).set(right);

    // Create source node and route through gain → analyser → destination
    const source = ctx.createBufferSource();
    source.buffer = audioBuffer;
    if (this._gainNode) {
      source.connect(this._gainNode);
    } else if (this._analyser) {
      source.connect(this._analyser);
    } else {
      source.connect(ctx.destination);
    }
    const renderTapChunk = {
      sequence: chunk.sequence,
      scheduledAt: when,
      sampleCount,
      flags: chunk.flags,
    };
    if (typeof window !== 'undefined') {
      window.__spokeRenderTapCurrentChunk = renderTapChunk;
    }
    try {
      source.start(when);
    } finally {
      if (
        typeof window !== 'undefined'
        && window.__spokeRenderTapCurrentChunk === renderTapChunk
      ) {
        window.__spokeRenderTapCurrentChunk = null;
      }
    }

    // Clean up source node after playback to prevent unbounded accumulation.
    // Without this, every source stays connected to the gainNode forever,
    // forcing the audio rendering thread to mix N silent inputs per quantum.
    this._activeSourceNodes++;
    if (this._activeSourceNodes > this._peakActiveSourceNodes) {
      this._peakActiveSourceNodes = this._activeSourceNodes;
    }
    const nodeEntry = { source, when };
    this._pendingSourceNodes.push(nodeEntry);
    const cleanup = () => {
      if (nodeEntry._cleaned) return;
      nodeEntry._cleaned = true;
      try { source.disconnect(); } catch { /* already disconnected */ }
      source.buffer = null;
      this._activeSourceNodes--;
      const idx = this._pendingSourceNodes.indexOf(nodeEntry);
      if (idx >= 0) this._pendingSourceNodes.splice(idx, 1);
    };
    source.onended = cleanup;
    // iOS Safari fallback: onended may not fire reliably on screen lock,
    // app switch, or memory pressure. Schedule a timer-based cleanup
    // slightly after the chunk should have finished playing.
    const chunkDurationSec = sampleCount / 48000;
    const safetyMs = Math.max(0, (when - ctx.currentTime + chunkDurationSec) * 1000) + 100;
    setTimeout(cleanup, safetyMs);

    // Record schedule event for sync measurement (Item 7)
    this._scheduleLog.push({
      seq: chunk.sequence,
      hubPlayAt: chunk.playAt,
      scheduledAt: when,
      playedAt: when + (this._outputLatency || 0),
    });
    if (this._scheduleLog.length > this._scheduleLogMax) {
      this._scheduleLog.shift();
    }

    this._lastScheduledSequence = chunk.sequence;
    this._lastScheduledPlayAt = when;
    this._chunksPlayed++;
    this._lastChunkPlayedTime = performance.now();

    // Advance running counter by actual output duration (Fix 3: not hardcoded 0.02)
    this._nextPlayTime += sampleCount / SAMPLE_RATE;

    // --- Diagnostics (throttled to every 10th chunk) ---
    if (doDiag) {
      const isSilenceChunk = (chunk.flags & FLAG_SILENCE) !== 0;

      // --- Layer 4: Reception diagnostics ---
      {
        const pcm = chunk.pcmInt16;
        let recvSumSq = 0;
        let hasNonZero = false;
        let sc;
        if (chunk.is24bit) {
          sc = Math.min(200, pcm.byteLength / 3);
          for (let i = 0; i < sc; i++) {
            const off = i * 3;
            const b0 = pcm.getUint8(off);
            const b1 = pcm.getUint8(off + 1);
            const b2 = pcm.getUint8(off + 2);
            let val = b0 | (b1 << 8) | (b2 << 16);
            if (val & 0x800000) val |= 0xFF000000;
            recvSumSq += val * val;
            if (val !== 0) hasNonZero = true;
          }
        } else {
          sc = Math.min(200, pcm.byteLength / 2);
          for (let i = 0; i < sc; i++) {
            const s = pcm.getInt16(i * 2, true);
            recvSumSq += s * s;
            if (s !== 0) hasNonZero = true;
          }
        }
        const recvRMS = Math.sqrt(recvSumSq / sc) / (chunk.is24bit ? 8388607 : 32767);
        if (!isSilenceChunk && hasNonZero) {
          this._lastReceivedChunkRMS = recvRMS;
          this._lastReceivedNonSilent = true;
        }
      }

      // --- Layer 5: Conversion diagnostics ---
      {
        let convMin = Infinity, convMax = -Infinity, convSumSq = 0;
        for (let i = 0; i < left.length; i++) {
          const v = left[i];
          convSumSq += v * v;
          if (v < convMin) convMin = v;
          if (v > convMax) convMax = v;
        }
        if (!isSilenceChunk && convSumSq > 0) {
          this._lastConvertedRMS = Math.sqrt(convSumSq / left.length);
          this._lastConvertedMin = convMin;
          this._lastConvertedMax = convMax;
        }
      }

      // --- Clipping detection (pre-gain) ---
      {
        let chunkClips = 0;
        for (let i = 0; i < left.length; i++) {
          if (left[i] > 0.99 || left[i] < -0.99) chunkClips++;
          if (right[i] > 0.99 || right[i] < -0.99) chunkClips++;
        }
        this._clipCount += chunkClips;
        this._clipCountLastChunk = chunkClips;
      }

      // --- Layer 6: Buffer fill diagnostics ---
      {
        const bufData = audioBuffer.getChannelData(0);
        let bufSumSq = 0;
        const bufLen = Math.min(100, bufData.length);
        for (let i = 0; i < bufLen; i++) {
          bufSumSq += bufData[i] * bufData[i];
        }
        if (!isSilenceChunk && bufSumSq > 0) {
          this._lastBufferRMS = Math.sqrt(bufSumSq / bufLen);
        }
        this._lastBufferLength = sampleCount;
      }

      // --- Layer 7: Schedule timing diagnostics ---
      this._lastScheduleDelta = when - ctx.currentTime;

      const rmsLen = Math.min(100, left.length);
      let sumSq = 0;
      for (let i = 0; i < rmsLen; i++) {
        sumSq += left[i] * left[i];
      }
      this._lastChunkRMS = Math.sqrt(sumSq / rmsLen);
      if (this._lastChunkRMS > this._peakChunkRMS) {
        this._peakChunkRMS = this._lastChunkRMS;
      }

      if (!isSilenceChunk && this._lastChunkRMS > 0.001) {
        const rawFirst4Bytes = [
          chunk.pcmInt16.getUint8(0),
          chunk.pcmInt16.getUint8(1),
          chunk.pcmInt16.getUint8(2),
          chunk.pcmInt16.getUint8(3),
        ];
        let leftMin = Infinity, leftMax = -Infinity;
        for (let i = 0; i < left.length; i++) {
          if (left[i] < leftMin) leftMin = left[i];
          if (left[i] > leftMax) leftMax = left[i];
        }
        this._lastAudioDataSample = {
          rawFirst4Bytes,
          leftMin,
          leftMax,
          leftRMS: this._lastChunkRMS,
          outputSamples: sampleCount,
        };
      }

      // Update signal quality cache (every 50th chunk = every 5th diagnostic run)
      if (this._diagCounter % 50 === 0) {
        this._cachedSignalQuality = this.getSignalQuality();
      }

      this._updateDebugObject();
    }
  }

  /**
   * Re-anchor the running counter after AudioContext suspend/resume.
   *
   * Called when the AudioContext transitions from suspended to running
   * during active playback. The previous counter position may reference
   * a stale (frozen) currentTime.
   * @private
   */
  _recomputeTimeMapping() {
    if (!this._audioCtx || this._audioCtx.state !== 'running') return;

    const oldNext = this._nextPlayTime;
    const now = this._audioCtx.currentTime;

    // Advance forward only — never schedule new chunks before already-queued
    // AudioBufferSourceNodes. On iOS, when the AudioContext resumes from
    // interruption (screen lock/unlock, phone call), currentTime resumes from
    // its pre-interruption value. Pre-scheduled nodes are still queued from
    // currentTime+Δ onward. Resetting _nextPlayTime = currentTime + 0.1 would
    // overlap new chunks with those pending nodes → simultaneous audio → static.
    //
    // Math.max preserves the existing counter if it's ahead of now+0.05,
    // preventing overlap. If _nextPlayTime fell behind (e.g., context was
    // fully paused and no new chunks arrived), it advances to now+0.05 to
    // start scheduling promptly.
    this._nextPlayTime = Math.max(this._nextPlayTime, now + 0.05);
    this._desyncAnchorSet = false;

    this._log(
      'Running counter re-anchored: %.3f → %.3f (ctx=%.3f, advanced=%.3f)',
      oldNext, this._nextPlayTime, now, this._nextPlayTime - oldNext,
    );
  }

  // ---------------------------------------------------------------- //
  // Debug metrics                                                     //
  // ---------------------------------------------------------------- //

  /**
   * Update the window.__spokeAudioDebug object for automated testing.
   * @private
   */
  _updateDebugObject() {
    if (typeof window === 'undefined') return;

    const SIGNAL_THRESHOLD = 0.001;

    // Determine first silent layer for diagnostics
    let firstSilentLayer = null;
    if (this._lastReceivedChunkRMS <= SIGNAL_THRESHOLD) {
      firstSilentLayer = 'capture';
    } else if (this._lastConvertedRMS <= SIGNAL_THRESHOLD) {
      firstSilentLayer = 'conversion';
    } else if (this._lastBufferRMS <= SIGNAL_THRESHOLD) {
      firstSilentLayer = 'buffer';
    } else if (this._lastScheduleDelta <= 0 || this._lastScheduleDelta > 5.0) {
      firstSilentLayer = 'schedule';
    } else if (this._analyserRMS <= SIGNAL_THRESHOLD) {
      firstSilentLayer = 'analyser';
    }

    window.__spokeAudioDebug = {
      // Layer 4: Reception
      lastReceivedChunkRMS: this._lastReceivedChunkRMS,
      lastReceivedNonSilent: this._lastReceivedNonSilent,

      // Layer 5: Conversion
      lastConvertedRMS: this._lastConvertedRMS,
      lastConvertedMin: this._lastConvertedMin,
      lastConvertedMax: this._lastConvertedMax,

      // Layer 6: Buffer fill
      lastBufferRMS: this._lastBufferRMS,
      lastBufferLength: this._lastBufferLength,

      // Layer 7: Scheduling
      lastScheduleDelta: this._lastScheduleDelta,
      audioContextState: this._audioCtx ? this._audioCtx.state : 'closed',
      audioContextCurrentTime: this._audioCtx ? this._audioCtx.currentTime : 0,

      // Layer 8: Signal flow (AnalyserNode)
      analyserRMS: this._analyserRMS,
      analyserPeak: this._analyserPeak,

      // Signal quality (cached, recomputed every 50th chunk = ~1 second)
      signalQuality: this._cachedSignalQuality,

      // Drift correction state
      driftCorrectionEnabled: this._driftCorrector.isEnabled(),
      driftDebug: this._driftCorrector.getDebugState(),
      oracleClockSkewPpm: this._getOracleClockSkewPpm(),
      oracleEvents: this._oracleEvents.slice(-50),
      reAnchorCount: this._reAnchorCount || 0,
      lastReAnchorAt: this._lastReAnchorAt || null,
      stallRecoveryCount: this._stallRecoveryCount,
      stallCreditMs: this._stallCreditMs,
      muted: this._muted,
      volume: this._volumeBeforeMute,

      // Chunk boundary continuity
      chunkBoundaryAvgDiscontinuity: this._boundaryCount > 0
        ? this._boundaryDiscontinuitySum / this._boundaryCount : 0,
      chunkBoundaryMaxDiscontinuity: this._boundaryMaxDiscontinuity,
      chunkBoundaryCount: this._boundaryCount,
      crossfadeAppliedCount: this._crossfadeAppliedCount,

      // Pipeline health summary
      pipelineHealthy: this._analyserRMS > SIGNAL_THRESHOLD && firstSilentLayer === null,
      firstSilentLayer: firstSilentLayer,

      // Clipping detection (pre-gain)
      clipCount: this._clipCount,
      clipCountLastChunk: this._clipCountLastChunk,
      clipRate: this._chunksPlayed > 0
        ? this._clipCount / (this._chunksPlayed * SAMPLES_PER_CHUNK * CHANNELS)
        : 0,

      // Echo detection
      echoDetections: this._echoDetections,

      // Source node lifecycle
      activeSourceNodes: this._activeSourceNodes,
      peakActiveSourceNodes: this._peakActiveSourceNodes,

      // Legacy fields (backwards compat with existing tests)
      lastChunkRMS: this._lastChunkRMS,
      peakChunkRMS: this._peakChunkRMS,
      audioDataSample: this._lastAudioDataSample,
    };
  }

  // ---------------------------------------------------------------- //
  // Output latency measurement (metrics only)                         //
  // ---------------------------------------------------------------- //

  /**
   * Read the AudioContext output latency for metrics display.
   *
   * NOTE: This is NOT subtracted from scheduling targets. The A/B test
   * confirmed that output latency compensation was not needed for clean
   * audio (?clean=1&latency=1 was clean). Latency is reported in metrics
   * for multi-spoke sync analysis only.
   *
   * @private
   */
  _updateOutputLatency() {
    if (!this._audioCtx) return;

    let latency = 0;

    // Try getOutputTimestamp() first (most accurate — Chrome reports ~52ms)
    if (typeof this._audioCtx.getOutputTimestamp === 'function') {
      const ts = this._audioCtx.getOutputTimestamp();
      if (ts.contextTime > 0 && ts.performanceTime > 0) {
        if (ts.contextTime > this._audioCtx.currentTime / 100) {
          latency = this._audioCtx.currentTime - ts.contextTime;
          if (latency > 0 && latency < 0.5) {
            this._outputLatency = latency;
            // Accurate path — 15ms floor (no real hardware runs under 15ms)
            if (this._outputLatency < 0.015) {
              this._outputLatency = 0.015;
            }
            return;
          }
        }
      }
    }

    // Fallback: baseLatency + outputLatency properties.
    // Safari/iOS lacks getOutputTimestamp() and outputLatency — baseLatency
    // returns only the render quantum (~2.67ms at 128 samples/48kHz), not
    // the full hardware path (CoreAudio safety offset + OS mixer + DAC).
    // Real iOS output latency is 30–50ms on built-in speaker.
    latency = (this._audioCtx.baseLatency || 0) + (this._audioCtx.outputLatency || 0);
    if (latency > 0 && latency < 0.5) {
      this._outputLatency = latency;
    }

    // Inaccurate-path floor: 50ms.  Browsers that reach here lack
    // getOutputTimestamp() (the only accurate latency API).  Their
    // baseLatency severely underreports real hardware latency — iOS Safari
    // reports 2.67ms when actual output latency is 30–50ms.  With 40ms
    // floor, iPhone was consistently 47-59ms ahead of hub — the floor
    // undercompensated by ~10ms.  50ms aligns with empirical measurements
    // of real iOS CoreAudio output path (~25ms IO buffer + ~5ms DAC +
    // ~10ms OS mixer + safety offset).  Chrome never reaches here.
    if (this._outputLatency < 0.050) {
      this._outputLatency = 0.050;
    }
  }

  // ---------------------------------------------------------------- //
  // Tab visibility                                                    //
  // ---------------------------------------------------------------- //

  /** @private */
  _onVisibilityChange() {
    if (document.visibilityState === 'hidden') {
      this._log('Tab went to background — pausing audio stream');
      this._sendTextMessage({ type: 'pause_audio' });
      this._backgroundSince = performance.now();
      if (this._onBackgroundChange) {
        this._onBackgroundChange(true);
      }
    } else if (document.visibilityState === 'visible') {
      this._log('Tab became visible');
      this._sendTextMessage({ type: 'resume_audio' });
      this._backgroundSince = null;
      if (this._onBackgroundChange) {
        this._onBackgroundChange(false);
      }

      // Resume AudioContext if suspended/interrupted — the onstatechange handler
      // will recompute time mapping when it transitions to 'running'
      if (this._audioCtx
          && (this._audioCtx.state === 'suspended' || this._audioCtx.state === 'interrupted')) {
        this._audioCtx.resume().catch(() => {});
      }

      // Re-sync clock after returning from background
      if (this._clockSync) {
        this._clockSync.synchronize().catch(() => {});
      }
    }
  }

  /**
   * Send a JSON text message over the WebSocket.
   * @param {Object} obj - Message object (will be JSON-stringified)
   * @private
   */
  _sendTextMessage(obj) {
    if (this._ws && this._ws.readyState === WebSocket.OPEN) {
      try {
        this._ws.send(JSON.stringify(obj));
      } catch {
        // WebSocket may have closed between check and send
      }
    }
  }

  // ---------------------------------------------------------------- //
  // State and metrics                                                 //
  // ---------------------------------------------------------------- //

  /** @private */
  _setState(newState) {
    if (this._state !== newState) {
      this._state = newState;
      this._emitMetrics();
    }
  }

  /** @private */
  _emitMetrics() {
    this._onMetrics(this.getMetrics());
  }

  /**
   * Format and emit a debug log message.
   * @param {string} fmt - printf-style format string
   * @param {...*} args - Format arguments
   * @private
   */
  _log(fmt, ...args) {
    let msg = fmt;
    for (const arg of args) {
      msg = msg.replace(/%[sd]|%.?\d*f/, String(arg));
    }
    this._onLog(msg);
  }
}

// ------------------------------------------------------------------ //
// Sync quality                                                        //
// ------------------------------------------------------------------ //

/**
 * Categorise engine metrics into a three-state sync-quality label for
 * the UI indicator.  Pure function — no engine-instance dependencies —
 * so it's trivially testable and the React component can re-derive
 * the label from a metrics snapshot without reaching into internals.
 *
 * States:
 *   "good"      — clock synced, small correction, no recent re-anchor.
 *                  The UI typically fades this out after a beat of
 *                  stability to avoid cluttering the happy path.
 *   "syncing"   — clock not yet synced OR medium correction OR recent
 *                  re-anchor event.  Transient; should be visible so
 *                  users understand what they're hearing.
 *   "degraded"  — drift corrector pinned at its clamp.  Clock sync is
 *                  bad enough that audio artefacts are likely.  Red dot
 *                  with tooltip so the user knows it's not a content
 *                  problem.
 *
 * Thresholds (ppm of drift correction):
 *    |corr| ≤ 200   → good
 *    |corr| ≤ 1500  → syncing (converging)
 *    |corr| >  1500 → degraded (saturated)
 *
 * A re-anchor within ``RECENT_REANCHOR_MS`` bumps the state down to
 * "syncing" regardless of correction magnitude, so the indicator
 * faithfully shows "something just happened" even after a momentary
 * clock blip.
 *
 * @param {object|null} metrics ``SpokeAudioEngine.getMetrics()`` result.
 * @returns {"good"|"syncing"|"degraded"}
 */
export function computeSyncQuality(metrics) {
  if (!metrics) return 'syncing';
  if (!metrics.clockSynced) return 'syncing';

  const RECENT_REANCHOR_MS = 2000;
  if (
    metrics.lastReAnchorMsAgo != null
    && metrics.lastReAnchorMsAgo < RECENT_REANCHOR_MS
  ) {
    return 'syncing';
  }

  const absCorr = Math.abs(metrics.driftCorrectionPpm || 0);
  if (absCorr > 1500) return 'degraded';
  if (absCorr > 200) return 'syncing';
  return 'good';
}

// ------------------------------------------------------------------ //
// Exports                                                             //
// ------------------------------------------------------------------ //

export {
  DEFAULT_BUFFER_TARGET_SEC,
  FRAME_SIZE,
  HEADER_SIZE,
  MAX_BUFFER_CHUNKS,
  MIN_BUFFER_MS,
  PCM_SIZE,
  SAMPLE_RATE,
  SAMPLES_PER_CHUNK,
  SCHEDULER_TICK_MS,
  TRACK_TRANSITION_SEQ_GAP,
};
