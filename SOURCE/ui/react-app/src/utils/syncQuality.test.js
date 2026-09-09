/**
 * Tests for computeSyncQuality — pure helper that maps engine
 * metrics to a three-state UI label.  Behaviour invariant we want to
 * hold forever:
 *
 *   1. No metrics / no clock sync → "syncing" (never "good").
 *   2. Drift corrector saturated (>1500 ppm) → "degraded".
 *   3. Recent re-anchor (<2 s ago) overrides small-correction "good".
 *   4. Happy path (synced + small correction + no recent event) → "good".
 */
import { describe, it, expect } from 'vitest';
import { computeSyncQuality } from './spokeAudioEngine';

const baseGood = {
  clockSynced: true,
  driftCorrectionPpm: 50,
  lastReAnchorMsAgo: null,
};

describe('computeSyncQuality', () => {
  it('returns "syncing" when metrics are missing', () => {
    expect(computeSyncQuality(null)).toBe('syncing');
    expect(computeSyncQuality(undefined)).toBe('syncing');
  });

  it('returns "syncing" when the clock is not yet synced', () => {
    expect(computeSyncQuality({ ...baseGood, clockSynced: false })).toBe('syncing');
  });

  it('returns "good" for the happy path', () => {
    expect(computeSyncQuality(baseGood)).toBe('good');
  });

  it('stays "good" across small corrections within ± 200 ppm', () => {
    expect(computeSyncQuality({ ...baseGood, driftCorrectionPpm: 150 })).toBe('good');
    expect(computeSyncQuality({ ...baseGood, driftCorrectionPpm: -199 })).toBe('good');
  });

  it('drops to "syncing" for medium corrections (200–1500 ppm)', () => {
    expect(computeSyncQuality({ ...baseGood, driftCorrectionPpm: 500 })).toBe('syncing');
    expect(computeSyncQuality({ ...baseGood, driftCorrectionPpm: -1500 })).toBe('syncing');
  });

  it('escalates to "degraded" when corrector saturates beyond 1500 ppm', () => {
    expect(computeSyncQuality({ ...baseGood, driftCorrectionPpm: 1800 })).toBe('degraded');
    expect(computeSyncQuality({ ...baseGood, driftCorrectionPpm: -2400 })).toBe('degraded');
  });

  it('forces "syncing" for 2 s after a re-anchor event', () => {
    expect(
      computeSyncQuality({ ...baseGood, lastReAnchorMsAgo: 500 })
    ).toBe('syncing');
    expect(
      computeSyncQuality({ ...baseGood, lastReAnchorMsAgo: 1999 })
    ).toBe('syncing');
  });

  it('ignores old re-anchors (> 2 s ago)', () => {
    expect(
      computeSyncQuality({ ...baseGood, lastReAnchorMsAgo: 2500 })
    ).toBe('good');
  });

  it('re-anchor wins over drift-corrector "degraded"', () => {
    // Semantic: the user's immediate experience (a click/fade just
    // happened) is more actionable than a saturated-corrector
    // warning.  We show "syncing" and escalate to "degraded" only
    // after the re-anchor window closes.
    expect(
      computeSyncQuality({
        clockSynced: true,
        driftCorrectionPpm: 2000,
        lastReAnchorMsAgo: 500,
      })
    ).toBe('syncing');
  });
});
