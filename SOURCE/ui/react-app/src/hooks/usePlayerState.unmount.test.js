// Regression for #2775: usePlayerState.js's multiroom hub-buffer delay timer
// (delayTimerRef, ~203-211) had no unmount cleanup effect. A component
// unmount within the hub_buffer_ms window left the timer pending — it fires
// setState after unmount, and a rehydrate fetch that resolved first could be
// overwritten by the stale delayed payload on a later remount.

import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const getStateMock = vi.fn();
let capturedHandleMessage = null;

vi.mock('./useViolaApi', () => ({
  useViolaApi: () => ({
    getState: getStateMock,
  }),
}));

vi.mock('./useWebSocket', () => ({
  useWebSocket: (handleMessage) => {
    capturedHandleMessage = handleMessage;
    return {
      send: vi.fn(),
      connectCount: 0,
      setBinaryCallback: vi.fn(),
      setDisconnectCallback: vi.fn(),
      getWsDebug: () => ({}),
    };
  },
}));

describe('usePlayerState multiroom delay timer cleanup', () => {
  beforeEach(() => {
    getStateMock.mockResolvedValue({});
    capturedHandleMessage = null;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('clears the pending hub-buffer delay timer on unmount', async () => {
    const { usePlayerState } = await import('./usePlayerState');
    const { unmount } = renderHook(() => usePlayerState());

    expect(capturedHandleMessage).toBeInstanceOf(Function);

    // A hub-buffered 'state' broadcast schedules the multiroom visual-delay
    // timer (delayTimerRef) via setTimeout(..., hub_buffer_ms).
    await act(async () => {
      capturedHandleMessage({
        type: 'state',
        payload: {
          hub_local_playback_active: true,
          hub_buffer_ms: 5000,
          now_playing: { id: 'track-1', title: 'Song' },
          volume: 50,
        },
      });
    });

    // usePlayerState has exactly one setTimeout site (the hub-buffer delay
    // timer), so a clearTimeout call on unmount is unambiguously that timer.
    const clearTimeoutSpy = vi.spyOn(global, 'clearTimeout');
    unmount();

    expect(clearTimeoutSpy).toHaveBeenCalledTimes(1);
  });

  it('does not throw or warn when the hub-buffer delay elapses after unmount', async () => {
    vi.useFakeTimers();
    try {
      const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
      const { usePlayerState } = await import('./usePlayerState');
      const { unmount } = renderHook(() => usePlayerState());

      await act(async () => {
        capturedHandleMessage({
          type: 'state',
          payload: {
            hub_local_playback_active: true,
            hub_buffer_ms: 5000,
            now_playing: { id: 'track-1', title: 'Song' },
            volume: 50,
          },
        });
      });

      unmount();

      // If the timer weren't cleared, this would fire setState on the
      // unmounted component (a React warning, if not an outright crash).
      await act(async () => {
        vi.advanceTimersByTime(5100);
      });

      const reactStateWarnings = consoleErrorSpy.mock.calls.filter(([msg]) =>
        typeof msg === 'string' && msg.includes('unmounted component'));
      expect(reactStateWarnings).toHaveLength(0);
    } finally {
      vi.useRealTimers();
    }
  });
});
