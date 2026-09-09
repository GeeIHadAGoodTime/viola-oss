// Proof for LEVERAGE_RANKING quick-kill #4 (source: docs/audit/silent_failures.md F4):
// usePlayerState.js:156 - api.getState() rehydrate fetch had `.catch(() => {})`,
// so a failed rehydrate left the playback UI silently showing a stale
// track/position with zero diagnostic trail. This proves the failure is now
// logged via console.error instead of being silently dropped.

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

describe('usePlayerState rehydrate failure logging', () => {
  beforeEach(() => {
    getStateMock.mockReset();
    capturedHandleMessage = null;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('logs to console when the embedded-track rehydrate fetch fails, instead of silently dropping it', async () => {
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    getStateMock.mockRejectedValue(new Error('simulated /v1/state fetch failure'));

    const { usePlayerState } = await import('./usePlayerState');
    renderHook(() => usePlayerState());

    expect(capturedHandleMessage).toBeInstanceOf(Function);

    // A 'state' broadcast whose now_playing lacks video_id but looks like an
    // embeddable YouTube track triggers maybeRehydrateEmbeddedState(), which
    // calls api.getState() and (pre-fix) swallowed any failure silently.
    await act(async () => {
      capturedHandleMessage({
        type: 'state',
        payload: {
          now_playing: {
            id: 'track-1',
            title: 'Some Song',
            provider: 'youtube_iframe',
            playback_mode: 'embedded_iframe_webview',
            capabilities: { requires_embedded_player: true },
          },
          is_playing: true,
        },
      });
      // Let the rejected getState() promise settle.
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(getStateMock).toHaveBeenCalled();
    expect(consoleErrorSpy).toHaveBeenCalledWith(
      '[usePlayerState] Player-state rehydrate fetch failed; UI may show a stale track/position:',
      expect.any(Error)
    );
  });
});
