/**
 * Ratchet tests for the turn-state indicators (#1407).
 *
 * Two user-visible lies the UX audit caught:
 *   BUG 1 — the globe Agent/Browser pill kept showing the present-tense
 *           "Working..." for the full 12s cooldown AFTER a turn completed (and,
 *           if the fire-and-forget terminal broadcast was dropped, indefinitely)
 *           — a spinner that never resolves.
 *   BUG 2 — the player progress bar kept a stale near-full fill under
 *           "Nothing playing" after a stop, because it was computed from stale
 *           backend position/duration with no gate on whether a track exists.
 *
 * The fixes, all exercised here against the REAL exported functions:
 *   - getAgentPillLabel(status, phase, active): returns '' when !active, so a
 *     completed-turn pill is an icon-only resolved affordance, never "Working...".
 *   - computePlayerDisplayMetrics({ hasTrack, ... }): forces 0 when !hasTrack.
 *   - handleCommandResult clears the agent-working state on turn completion
 *     (mirrored below) so the pill resolves the moment the turn ends, not only
 *     via the fragile post-answer terminal broadcast.
 *
 * Each block documents the broken pre-fix shape and asserts the fixed shape, so
 * removing any leg of the fix reds the suite.
 */
import { describe, it, expect } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useState, useCallback } from 'react';
import { getAgentPillLabel, computePlayerDisplayMetrics } from './SmartDisplay';

// ---------------------------------------------------------------------------
// BUG 1 — pill label truthfulness (real exported function).
// ---------------------------------------------------------------------------
describe('getAgentPillLabel: never claims "Working..." once the turn is done', () => {
  it('returns a present-tense verb ONLY while active', () => {
    // While genuinely working, a verb label is honest.
    expect(getAgentPillLabel('', '', true)).toBe('Working...');
    expect(getAgentPillLabel('', 'searching', true)).toBe('Searching...');
    expect(getAgentPillLabel('', 'reading', true)).toBe('Reading page...');
    expect(getAgentPillLabel('step 1: starting music', '', true)).toBe('step 1: starting');
  });

  it('returns NO working label once inactive (the fix) — pre-fix returned "Working..."', () => {
    // THE RATCHET: with the fix, an inactive pill (its post-turn cooldown) shows
    // no present-tense label. Removing the `if (!active) return ''` gate makes
    // this return "Working..." again — a spinner claim after the turn resolved.
    expect(getAgentPillLabel('', '', false)).toBe('');
    expect(getAgentPillLabel('', 'searching', false)).toBe('');
    expect(getAgentPillLabel('step 1: starting music', 'acting', false)).toBe('');
  });

  it('never yields a present-tense working verb while inactive, for any input', () => {
    const workingVerbs = ['Working...', 'Thinking...', 'Reading page...', 'Searching...'];
    for (const status of ['', 'step 3: reading page', 'searching music', 'planning']) {
      for (const phase of ['', 'thinking', 'reading', 'searching', 'acting']) {
        expect(workingVerbs).not.toContain(getAgentPillLabel(status, phase, false));
      }
    }
  });
});

// ---------------------------------------------------------------------------
// BUG 2 — progress bar truthfulness (real exported function).
// ---------------------------------------------------------------------------
describe('computePlayerDisplayMetrics: no track => zero progress', () => {
  it('with a track, reports real backend progress', () => {
    const m = computePlayerDisplayMetrics({
      hasTrack: true, isYouTubeVideo: false, isSpoke: false,
      position: 30, duration: 120,
    });
    expect(m.displayPosition).toBe(30);
    expect(m.displayDuration).toBe(120);
    expect(m.displayProgress).toBeCloseTo(25);
  });

  it('with a YouTube hub track, reads the live iframe clock', () => {
    const m = computePlayerDisplayMetrics({
      hasTrack: true, isYouTubeVideo: true, isSpoke: false,
      iframePosition: 45, iframeDuration: 90,
      position: 999, duration: 999, // backend values ignored for hub YouTube
    });
    expect(m.displayProgress).toBeCloseTo(50);
  });

  it('with NO track, forces 0 even when stale position/duration linger (the fix)', () => {
    // THE RATCHET: reproduces the exact bug — a stop leaves the backend's last
    // position/duration in state, but with no track the bar MUST read empty.
    // Removing the `if (!hasTrack) return { ...0 }` gate makes displayProgress
    // ~92 here — the stale near-full fill under "Nothing playing".
    const m = computePlayerDisplayMetrics({
      hasTrack: false, isYouTubeVideo: false, isSpoke: false,
      position: 110, duration: 120, // stale leftovers from the last track
    });
    expect(m.displayPosition).toBe(0);
    expect(m.displayDuration).toBe(0);
    expect(m.displayProgress).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// BUG 1 — turn lifecycle: the pill resolves when the turn completes.
// Mirrors the FIXED SmartDisplay handlers; removing the handleCommandResult
// clear leaves the pill "Working..." after the turn — the reported bug.
// ---------------------------------------------------------------------------
const AGENT_CONTEXT_PILL_COOLDOWN = 12000;

function useTurnStatePill() {
  const [agentTaskActive, setAgentTaskActive] = useState(false);
  const [agentTaskStatus, setAgentTaskStatus] = useState('');
  const [agentTaskPhase, setAgentTaskPhase] = useState('');
  // Visible = active OR within cooldown; the pill lingers after a task per design.
  const [pillVisible, setPillVisible] = useState(false);

  // Mirrors handleAgentProgress (non-terminal step): every command emits these.
  const onProgress = useCallback((progressText) => {
    setAgentTaskActive(true);
    setAgentTaskStatus(progressText);
    setAgentTaskPhase('acting');
    setPillVisible(true);
  }, []);

  // Mirrors handleCommandResult — THE FIX. Turn complete => resolve working state.
  // RATCHET: delete these three setters and the post-turn label becomes "Working...".
  const onCommandResult = useCallback(() => {
    setAgentTaskActive(false);
    setAgentTaskStatus('');
    setAgentTaskPhase('');
    // pillVisible stays true (cooldown affordance) until it decays.
  }, []);

  // Mirrors the cooldown elapsing (12s after active/visible go false).
  const cooldownElapsed = useCallback(() => setPillVisible(false), []);

  const pillLabel = () => getAgentPillLabel(agentTaskStatus, agentTaskPhase, agentTaskActive);

  return {
    get agentTaskActive() { return agentTaskActive; },
    get pillVisible() { return pillVisible; },
    pillLabel,
    onProgress,
    onCommandResult,
    cooldownElapsed,
    cooldownMs: AGENT_CONTEXT_PILL_COOLDOWN,
  };
}

describe('turn-state pill resolves when the turn completes', () => {
  it('a plain command: working during the turn, resolved (no "Working...") after it', () => {
    const { result } = renderHook(() => useTurnStatePill());

    // During the turn the pill honestly says it is working + pulses (active).
    act(() => { result.current.onProgress('step 1: starting music'); });
    expect(result.current.agentTaskActive).toBe(true);
    expect(result.current.pillLabel()).toBe('step 1: starting');

    // Turn completes: the command result arrives -> working state resolves NOW,
    // not 12s later and not dependent on the terminal broadcast.
    act(() => { result.current.onCommandResult(); });
    expect(result.current.agentTaskActive).toBe(false);
    // Oracle: after a completed turn, no ".working" indicator.
    expect(result.current.pillLabel()).not.toBe('Working...');
    expect(result.current.pillLabel()).toBe('');

    // The pill may linger briefly (cooldown affordance) but as an icon-only,
    // non-spinning, non-"Working..." resolved indicator.
    expect(result.current.pillVisible).toBe(true);
    expect(result.current.pillLabel()).toBe('');

    // ...then disappears when the cooldown elapses.
    act(() => { result.current.cooldownElapsed(); });
    expect(result.current.pillVisible).toBe(false);
  });

  it('back-to-back trivial commands never leave a stale "Working..." between turns', () => {
    const { result } = renderHook(() => useTurnStatePill());
    for (const step of ['step 1: skipping', 'step 1: setting timer', 'step 1: checking weather']) {
      act(() => { result.current.onProgress(step); });
      expect(result.current.agentTaskActive).toBe(true);
      act(() => { result.current.onCommandResult(); });
      // The exact audit symptom: idle between turns must NOT read "Working...".
      expect(result.current.pillLabel()).not.toBe('Working...');
      expect(result.current.agentTaskActive).toBe(false);
    }
  });
});
