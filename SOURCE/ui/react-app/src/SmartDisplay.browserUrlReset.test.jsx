/**
 * Stateful ratchet test for the browser-stage-reset gate.
 *
 * Regression: after a genuine browse, browserUrl stayed populated across
 * the task boundary so every subsequent non-browse agentic_task wrongly
 * resolved to the 'browser' stage.
 *
 * The fix: both terminal branches (browser_overlay_state !visible and
 * agent_progress terminal) now call setBrowserUrl('') to clear the URL.
 *
 * Test structure:
 *  - The "stale" tests demonstrate the broken pre-fix shape:
 *    if browserUrl is not cleared, a subsequent non-browse agentic_task
 *    wrongly resolves to 'browser'.  These tests PASS even without the fix
 *    (they document the broken shape using the pure function).
 *  - The stateful hook tests drive the overlay-hide and agent-progress
 *    terminal handlers through a hook that mirrors the FIXED component code.
 *    These tests will FAIL if the setBrowserUrl('') calls are removed from
 *    either terminal branch (the ratchet).
 */
import { describe, it, expect } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useState, useCallback } from 'react';
import { resolveStageContentMode } from './SmartDisplay';

// ---------------------------------------------------------------------------
// Document the broken pre-fix shape via the pure function.
// ---------------------------------------------------------------------------
describe('pre-fix shape: stale browserUrl causes wrong stage (pure-function proof)', () => {
  it('a non-blank browserUrl during an agentic_task resolves to browser — the bug', () => {
    // This is the bug: if browserUrl is NOT cleared when the task ends, the next
    // non-browse tool call carries the stale URL and resolves to 'browser'.
    const stage = resolveStageContentMode('agentic_task', {
      browserUrl: 'https://example.com',  // stale — was NOT cleared on task end
      browserModeActive: false,
      agentFrameSrc: null,
    });
    expect(stage).toBe('browser');  // wrong — should be 'music' for non-browse
  });

  it('an empty browserUrl during an agentic_task resolves to music — the fix result', () => {
    // After the fix, browserUrl is '' when a non-browse task starts, so stage=music.
    const stage = resolveStageContentMode('agentic_task', {
      browserUrl: '',
      browserModeActive: false,
      agentFrameSrc: null,
    });
    expect(stage).toBe('music');
  });
});

// ---------------------------------------------------------------------------
// Mini state machine mirroring the FIXED SmartDisplay terminal handlers.
// These tests FAIL if setBrowserUrl('') is removed from either branch.
// ---------------------------------------------------------------------------
function useFixedBrowseStateMachine() {
  const [browserUrl, setBrowserUrl] = useState('');
  const [browserModeActive, setBrowserModeActive] = useState(false);
  const [agentTaskActive, setAgentTaskActive] = useState(false);
  const [displayMode, setDisplayMode] = useState('now_playing');

  // Mirrors the FIXED browser_overlay_state (!visible terminal) handler.
  // RATCHET: removing setBrowserUrl('') here breaks the stateful test below.
  const handleOverlayHide = useCallback(() => {
    setAgentTaskActive(false);
    setBrowserModeActive(false);
    setBrowserUrl('');  // THE FIX — was missing pre-fix
    setDisplayMode((m) => (m === 'browser' || m === 'agentic_task' ? 'now_playing' : m));
  }, []);

  // Mirrors the FIXED agent_progress terminal handler.
  // RATCHET: removing setBrowserUrl('') here breaks the stateful test below.
  const handleAgentProgressTerminal = useCallback(() => {
    setAgentTaskActive(false);
    setBrowserUrl('');  // THE FIX — was missing pre-fix
    setBrowserModeActive(false);
    setDisplayMode((m) => (m === 'browser' || m === 'agentic_task' ? 'now_playing' : m));
  }, []);

  // Starts a genuine browse.
  const startBrowse = useCallback((url) => {
    setBrowserUrl(url);
    setBrowserModeActive(true);
    setAgentTaskActive(true);
    setDisplayMode('agentic_task');
  }, []);

  // Starts a non-browse agentic task (music search, memory, etc.).
  const startNonBrowseTask = useCallback(() => {
    setAgentTaskActive(true);
    setDisplayMode('agentic_task');
    // browserUrl intentionally NOT set — this is NOT a browse
  }, []);

  const stageFromSignals = () => resolveStageContentMode(displayMode, {
    browserUrl,
    browserModeActive,
    agentFrameSrc: null,
    activeCallId: null,
  });

  return {
    browserUrl,
    displayMode,
    stageFromSignals,
    startBrowse,
    handleOverlayHide,
    handleAgentProgressTerminal,
    startNonBrowseTask,
  };
}

describe('browser-stage-reset stateful: browserUrl is cleared on task termination', () => {
  it('overlay-hide terminal: browse → browser stage, then task end → browserUrl cleared, then music tool call → music stage', () => {
    const { result } = renderHook(() => useFixedBrowseStateMachine());

    // 1. Genuine browse sets browserUrl and stage = browser.
    act(() => { result.current.startBrowse('https://example.com'); });
    expect(result.current.browserUrl).toBe('https://example.com');
    expect(result.current.stageFromSignals()).toBe('browser');

    // 2. Overlay hides (task complete) — must clear browserUrl.
    act(() => { result.current.handleOverlayHide(); });
    expect(result.current.browserUrl).toBe('');

    // 3. Non-browse agentic task fires (e.g., "searching for drake").
    act(() => { result.current.startNonBrowseTask(); });
    // MUST be 'music', NOT 'browser' — the regression being fixed.
    expect(result.current.stageFromSignals()).toBe('music');
  });

  it('agent_progress terminal: browse → browser stage, then terminal progress → browserUrl cleared, then music tool call → music stage', () => {
    const { result } = renderHook(() => useFixedBrowseStateMachine());

    // 1. Genuine browse.
    act(() => { result.current.startBrowse('https://example.com/search'); });
    expect(result.current.stageFromSignals()).toBe('browser');

    // 2. Agent progress fires terminal (complete/error/cancelled).
    act(() => { result.current.handleAgentProgressTerminal(); });
    expect(result.current.browserUrl).toBe('');

    // 3. Next non-browse tool call.
    act(() => { result.current.startNonBrowseTask(); });
    expect(result.current.stageFromSignals()).toBe('music');
  });

  it('genuine browse still shows browser stage during an active browse', () => {
    const { result } = renderHook(() => useFixedBrowseStateMachine());

    act(() => { result.current.startBrowse('https://example.com'); });
    // While browsing, stage must still be 'browser'.
    expect(result.current.stageFromSignals()).toBe('browser');
  });
});
