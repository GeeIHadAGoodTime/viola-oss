/**
 * Golden tests for DriftCorrector sign-convention invariant.
 *
 * These are regression-shields, not behavioural coverage.  The drift
 * corrector's sign convention is load-bearing: a prior refactor that
 * negated correctionTerm caused death spirals where a low buffer
 * drained further instead of filling.  The gate against that mistake
 * was a 2026-04-12 incident post-mortem — these tests make the
 * invariant executable so a future refactor can't silently re-break it.
 *
 * The invariant (documented at driftCorrector.js:17-21):
 *   - Buffer LOW  → effectivePpm negative → ADD sample (961) → slower → FILLS
 *   - Buffer HIGH → effectivePpm positive → REMOVE sample (959) → faster → DRAINS
 *
 * Tests drive the controller with no clock drift so all corrections come
 * from the buffer depth term, then assert the direction of the majority
 * correction.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { DriftCorrector } from './driftCorrector';

const SAMPLES_PER_CHUNK = 960;
const CHANNELS = 2;
const BYTES_PER_SAMPLE = 2;
const CHUNK_BYTES = SAMPLES_PER_CHUNK * CHANNELS * BYTES_PER_SAMPLE;

/** Empty PCM chunk — content is irrelevant for sign-convention behaviour. */
function makeSilentChunk() {
  return new DataView(new ArrayBuffer(CHUNK_BYTES));
}

/** Run `count` process() calls with fixed buffer depth & zero clock drift.
 *  Returns a tally of +1 (add) / -1 (remove) / 0 (no correction) outcomes.
 */
function runWithDepth(corrector, bufferDepthChunks, count) {
  let add = 0;
  let remove = 0;
  let none = 0;
  for (let i = 0; i < count; i++) {
    const { correctionApplied } = corrector.process(
      makeSilentChunk(),
      0,           // zero clock drift — isolate the buffer term
      bufferDepthChunks,
      false,
    );
    if (correctionApplied === 1) add++;
    else if (correctionApplied === -1) remove++;
    else none++;
  }
  return { add, remove, none };
}

describe('DriftCorrector sign-convention invariant', () => {
  beforeEach(() => {
    // Fake time so we can fast-forward past the 5 s warm-up window
    // deterministically; the corrector reads Date.now() to decide
    // whether to suppress buffer-derived corrections.
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-04-19T00:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('buffer LOW → majority correction is ADD sample (+1, fills buffer)', () => {
    const corrector = new DriftCorrector();  // default target 14 chunks

    // Seed EMA + clear warm-up freeze.  500 chunks of process() while
    // still in warm-up will populate the smoothed buffer depth via the
    // EMA window mechanism (the EMA updates regardless of warm-up; only
    // correction is gated).
    runWithDepth(corrector, 8, 300);

    vi.advanceTimersByTime(6000);  // past WARMUP_DURATION_MS (5 s)

    // Now measure.  Buffer = 8 chunks, target = 14, error = -6.
    // After dead zone (1 chunk for shrink): effective = -5.
    // correctionTerm = -5 * 250 ppm/chunk = -1250 ppm (rate-limited ramp).
    // Negative effectivePpm → accumulator ↓ → crosses −1.0 → ADD sample.
    const tally = runWithDepth(corrector, 8, 500);

    // If someone negates correctionTerm in a refactor, remove will
    // dominate instead — which is the death-spiral failure mode.
    expect(tally.add).toBeGreaterThan(tally.remove);
    expect(tally.add).toBeGreaterThan(100);
  });

  it('buffer HIGH → majority correction is REMOVE sample (-1, drains buffer)', () => {
    const corrector = new DriftCorrector();

    runWithDepth(corrector, 20, 300);
    vi.advanceTimersByTime(6000);

    const tally = runWithDepth(corrector, 20, 500);

    expect(tally.remove).toBeGreaterThan(tally.add);
    expect(tally.remove).toBeGreaterThan(100);
  });

  it('buffer AT target with zero clock drift → near-zero corrections', () => {
    const corrector = new DriftCorrector();

    runWithDepth(corrector, 14, 300);
    vi.advanceTimersByTime(6000);

    const tally = runWithDepth(corrector, 14, 500);

    // With error = 0, dead zone absorbs it, correctionTerm = 0,
    // basePpm = 0 (clock drift = 0) → accumulator stays near zero.
    // A handful of corrections near the boundary are fine.
    const corrections = tally.add + tally.remove;
    expect(corrections).toBeLessThan(25);
  });

  it('normal scheduler-low buffer inside dead zone → near-zero corrections', () => {
    const corrector = new DriftCorrector();

    runWithDepth(corrector, 11, 300);
    vi.advanceTimersByTime(6000);

    const tally = runWithDepth(corrector, 11, 500);

    // A browser spoke can hover a few queued chunks below the target while
    // WebAudio already has audio scheduled ahead. That should not become
    // sample-rate correction when measured clock drift is zero.
    const corrections = tally.add + tally.remove;
    expect(corrections).toBeLessThan(25);
  });

  it('disabled corrector bypasses sign logic entirely', () => {
    const corrector = new DriftCorrector();
    corrector.setEnabled(false);

    const tally = runWithDepth(corrector, 8, 100);

    // Bypass path returns correctionApplied = 0 for every chunk.
    expect(tally.add).toBe(0);
    expect(tally.remove).toBe(0);
    expect(tally.none).toBe(100);
  });
});
