/**
 * Tests for ClockSync — focus on the adaptive RTT filter.
 *
 * Pre-2026-04-19 behaviour: hard ``MAX_RTT_MS = 200`` cap + require
 * ≥ 3 valid probes.  On cloud / cellular links (RTT 200–500 ms) every
 * probe was rejected, ``synchronize()`` threw, and the spoke fell back
 * to offset=0 — producing drops and gaps until the periodic retry
 * eventually succeeded (which on bad networks it never did).
 *
 * Post-fix behaviour:
 *   * Startup phase accepts any probe up to ``STARTUP_MAX_RTT_MS``
 *     (2 s), so cloud spokes converge.
 *   * After ``BASELINE_SAMPLE_COUNT`` kept probes, the cap tightens
 *     to ``max(STEADY_FLOOR_MS, baseline × STEADY_BASELINE_MULT)``.
 *   * ``MIN_VALID_PROBES`` lowered to 1 — 1-2 probes give a
 *     "low confidence" offset instead of throwing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  ClockSync,
  STARTUP_MAX_RTT_MS,
  STEADY_FLOOR_MS,
  STEADY_BASELINE_MULT,
  BASELINE_SAMPLE_COUNT,
} from './clockSync';

/** Minimal WebSocket stub — satisfies ClockSync's ``addEventListener`` call. */
function makeWsStub() {
  return {
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    send: vi.fn(),
  };
}

/** Install a synthetic ``_sendProbe`` that returns a fixed RTT per call.
 *  The returned offset is a constant; only the RTT pattern matters for
 *  these tests. */
function stubProbes(sync, rttSequenceSec, offset = 0.1) {
  const iter = [...rttSequenceSec];
  sync._sendProbe = vi.fn(async () => {
    const rtt = iter.shift();
    if (rtt === undefined) {
      throw new Error('stub exhausted');
    }
    return {
      t1: 0, t2: 0, t3: 0, t4: 0,
      rtt,
      offset,
    };
  });
  // Also skip the 100 ms inter-probe delay.
  sync._sleep = vi.fn(async () => {});
}

describe('ClockSync adaptive RTT filter', () => {
  let ws;
  let sync;

  beforeEach(() => {
    ws = makeWsStub();
    sync = new ClockSync(ws);
    // Patch the module-internal _sleep used in synchronize() too.
    // ClockSync references a file-scoped _sleep via its own import, so
    // we fake timers to make setInterval / setTimeout synchronous.
    vi.useFakeTimers();
  });

  it('startup accepts probes up to STARTUP_MAX_RTT_MS', async () => {
    // Seven probes all with RTT = 1500 ms (inside the 2 s startup cap,
    // way outside the old 200 ms cap).
    stubProbes(sync, Array(7).fill(1.5));

    const p = sync.synchronize();
    await vi.runAllTimersAsync();
    const result = await p;

    // All 7 probes should be kept.
    expect(result.probes).toBe(7);
    expect(result.offset).toBeCloseTo(0.1, 6);
  });

  it('rejects probes above STARTUP_MAX_RTT_MS even during startup', async () => {
    // Five probes: two above cap, three below.
    const tooHigh = STARTUP_MAX_RTT_MS / 1000 + 0.5;
    stubProbes(sync, [tooHigh, 0.1, tooHigh, 0.15, 0.2]);

    const p = sync.synchronize(5);
    await vi.runAllTimersAsync();
    const result = await p;

    // 3 kept probes (the ones ≤ 2 s).
    expect(result.probes).toBe(3);
  });

  it('succeeds with one probe (MIN_VALID_PROBES=1)', async () => {
    // One probe succeeds, rest throw.
    let count = 0;
    sync._sendProbe = vi.fn(async () => {
      count++;
      if (count === 1) {
        return { t1: 0, t2: 0, t3: 0, t4: 0, rtt: 0.05, offset: 0.123 };
      }
      throw new Error('probe failed');
    });

    const p = sync.synchronize(5);
    await vi.runAllTimersAsync();
    const result = await p;

    expect(result.probes).toBe(1);
    expect(result.offset).toBeCloseTo(0.123, 6);
  });

  it('throws only when zero probes qualify', async () => {
    sync._sendProbe = vi.fn(async () => {
      throw new Error('WS closed');
    });

    const p = sync.synchronize(5);
    const expectation = expect(p).rejects.toThrow(/0\/5 valid probes/);
    await vi.runAllTimersAsync();
    await expectation;
  });

  it('locks baseline after BASELINE_SAMPLE_COUNT kept probes', async () => {
    // Ten probes all at 50 ms.
    stubProbes(sync, Array(BASELINE_SAMPLE_COUNT).fill(0.05));

    const p = sync.synchronize(BASELINE_SAMPLE_COUNT);
    await vi.runAllTimersAsync();
    await p;

    expect(sync.getBaselineRttSec()).toBeCloseTo(0.05, 6);
  });

  it('tightens to max(STEADY_FLOOR_MS, baseline × MULT) on LAN', async () => {
    // Pristine LAN: 10 probes at 10 ms baseline.
    stubProbes(sync, Array(BASELINE_SAMPLE_COUNT).fill(0.01));

    const p = sync.synchronize(BASELINE_SAMPLE_COUNT);
    await vi.runAllTimersAsync();
    await p;

    // Baseline × 2 = 20 ms, floor = 200 ms → cap = 200 ms.
    const nextMaxSec = sync._getMaxRttSec();
    expect(nextMaxSec * 1000).toBeCloseTo(STEADY_FLOOR_MS, 0);
  });

  it('scales cap to 2× baseline on high-latency links', async () => {
    // Cloud link: 10 probes at 400 ms baseline.
    stubProbes(sync, Array(BASELINE_SAMPLE_COUNT).fill(0.4));

    const p = sync.synchronize(BASELINE_SAMPLE_COUNT);
    await vi.runAllTimersAsync();
    await p;

    // Baseline × 2 = 800 ms (well above the floor).
    const nextMaxSec = sync._getMaxRttSec();
    expect(nextMaxSec * 1000).toBeCloseTo(0.4 * 1000 * STEADY_BASELINE_MULT, 0);
  });

  it('steady-state cap rejects probes above the tightened threshold', async () => {
    // Warm up with 10 probes at 50 ms (baseline = 50 ms, cap = 200 ms
    // floor).
    stubProbes(sync, Array(BASELINE_SAMPLE_COUNT).fill(0.05));
    let p = sync.synchronize(BASELINE_SAMPLE_COUNT);
    await vi.runAllTimersAsync();
    await p;

    // Follow-up round: 5 probes — three within cap (50 ms), two at
    // 500 ms (way above the 200 ms tightened cap).
    stubProbes(sync, [0.05, 0.5, 0.05, 0.5, 0.05]);
    p = sync.synchronize(5);
    await vi.runAllTimersAsync();
    const result = await p;

    expect(result.probes).toBe(3);
  });
});
