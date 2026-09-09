/**
 * Tests for SpokeAudioEngine._cancelPendingNodes() fade-out behaviour.
 *
 * Pre-2026-04-19: source.stop(0) → hard click on every re-anchor.
 * Post-fix: gain ramps 0 → 0 → restore across 40 ms while pending
 * source nodes stop at the bottom of the fade, turning the click into
 * an imperceptible mute-and-restore.
 *
 * Vitest lacks Web Audio so we hand-roll the minimum surface the
 * method touches: an audio context with a currentTime property and a
 * gainNode whose .gain exposes ``cancelAndHoldAtTime``,
 * ``linearRampToValueAtTime``, and a ``value``.  We invoke the method
 * via ``Function.prototype.call`` on a plain object so we don't have
 * to construct the full engine.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { SpokeAudioEngine } from './spokeAudioEngine';

function makeFakeGainParam(initial = 0.8) {
  return {
    value: initial,
    cancelAndHoldAtTime: vi.fn(),
    cancelScheduledValues: vi.fn(),
    setValueAtTime: vi.fn(),
    linearRampToValueAtTime: vi.fn(),
  };
}

function makeFakeCtx(currentTime = 10.0) {
  return { currentTime };
}

function makeFakePendingSource() {
  return {
    source: { stop: vi.fn() },
    when: 20.0,  // some time in the future relative to ctx.currentTime
  };
}

describe('SpokeAudioEngine._cancelPendingNodes fade', () => {
  let self;
  let gainParam;
  let ctx;

  beforeEach(() => {
    gainParam = makeFakeGainParam(0.8);
    ctx = makeFakeCtx(10.0);
    self = {
      _audioCtx: ctx,
      _gainNode: { gain: gainParam },
      _pendingSourceNodes: [],
      _reAnchorCount: 0,
      _lastReAnchorAt: null,
      // The engine records an oracle trace event on cancel (added with the
      // L5 multiroom oracle); the fake needs a stub for it.
      _recordOracleEvent: vi.fn(),
    };
  });

  it('no-op when there are no pending nodes', () => {
    SpokeAudioEngine.prototype._cancelPendingNodes.call(self);
    expect(gainParam.linearRampToValueAtTime).not.toHaveBeenCalled();
    expect(self._reAnchorCount).toBe(0);
  });

  it('fades gain down, stops nodes at fade-bottom, fades back up', () => {
    const p1 = makeFakePendingSource();
    const p2 = makeFakePendingSource();
    self._pendingSourceNodes = [p1, p2];

    SpokeAudioEngine.prototype._cancelPendingNodes.call(self);

    // Hold current gain so the ramp starts from a known value.
    expect(gainParam.cancelAndHoldAtTime).toHaveBeenCalledWith(10.0);

    // Ramp down to 0 at now + 20 ms.
    expect(gainParam.linearRampToValueAtTime).toHaveBeenNthCalledWith(1, 0, 10.02);

    // Ramp back to the prior gain at now + 40 ms.
    expect(gainParam.linearRampToValueAtTime).toHaveBeenNthCalledWith(2, 0.8, 10.04);

    // Each pending node is stopped at the fade-bottom (now + 20 ms).
    expect(p1.source.stop).toHaveBeenCalledWith(10.02);
    expect(p2.source.stop).toHaveBeenCalledWith(10.02);
  });

  it('skips nodes whose start time has already passed', () => {
    const passed = {
      source: { stop: vi.fn() },
      when: 5.0,  // < ctx.currentTime (10) — already playing or done
    };
    const future = {
      source: { stop: vi.fn() },
      when: 15.0,
    };
    self._pendingSourceNodes = [passed, future];

    SpokeAudioEngine.prototype._cancelPendingNodes.call(self);

    expect(passed.source.stop).not.toHaveBeenCalled();
    expect(future.source.stop).toHaveBeenCalledWith(10.02);
  });

  it('increments the re-anchor counter', () => {
    self._pendingSourceNodes = [makeFakePendingSource()];
    SpokeAudioEngine.prototype._cancelPendingNodes.call(self);
    expect(self._reAnchorCount).toBe(1);

    self._pendingSourceNodes = [makeFakePendingSource()];
    SpokeAudioEngine.prototype._cancelPendingNodes.call(self);
    expect(self._reAnchorCount).toBe(2);
  });

  it('falls back to cancelScheduledValues + setValueAtTime when cancelAndHoldAtTime is unavailable', () => {
    // Some browsers ship without cancelAndHoldAtTime; fall-through must
    // still cancel the ramp and seed the new ramp origin.
    delete gainParam.cancelAndHoldAtTime;
    self._pendingSourceNodes = [makeFakePendingSource()];

    SpokeAudioEngine.prototype._cancelPendingNodes.call(self);

    expect(gainParam.cancelScheduledValues).toHaveBeenCalledWith(10.0);
    expect(gainParam.setValueAtTime).toHaveBeenCalledWith(0.8, 10.0);
    expect(gainParam.linearRampToValueAtTime).toHaveBeenCalledWith(0, 10.02);
  });

  it('is resilient to a missing gain node', () => {
    self._gainNode = null;
    self._pendingSourceNodes = [makeFakePendingSource()];

    // Should still stop nodes without throwing.
    expect(() => {
      SpokeAudioEngine.prototype._cancelPendingNodes.call(self);
    }).not.toThrow();
    expect(self._pendingSourceNodes[0].source.stop).toHaveBeenCalled();
  });
});
