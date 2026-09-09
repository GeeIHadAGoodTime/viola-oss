import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import useCloudPhoneEvents from './useCloudPhoneEvents';
import { authFetch } from './useViolaApi';
import { useWebSocket } from './useWebSocket';

vi.mock('./useViolaApi', () => ({
  authFetch: vi.fn(() => Promise.resolve({ ok: true })),
}));

// Capture the message handler the hook registers on the shared local socket so
// tests can feed it cloud-relayed frames.
const wsHarness = { handler: null };
vi.mock('./useWebSocket', () => ({
  useWebSocket: vi.fn((handler) => {
    wsHarness.handler = handler;
    return { send: vi.fn(), wsRef: { current: null } };
  }),
}));

beforeEach(() => {
  wsHarness.handler = null;
  authFetch.mockClear();
  useWebSocket.mockClear();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('useCloudPhoneEvents', () => {
  it('is no longer a no-op: it subscribes to the local socket and drives the relay', () => {
    renderHook(() => useCloudPhoneEvents({ enabled: true }));
    expect(useWebSocket).toHaveBeenCalled();
    expect(typeof wsHarness.handler).toBe('function');
  });

  it('starts the server-side relay when enabled (no browser cloud token)', async () => {
    await act(async () => {
      renderHook(() => useCloudPhoneEvents({ enabled: true }));
    });
    expect(authFetch).toHaveBeenCalledWith('/v1/phone/cloud-events/start', { method: 'POST' });
    // The start call carries no cloud bearer — only authFetch's local api key.
    const call = authFetch.mock.calls.find((c) => String(c[0]).includes('cloud-events/start'));
    expect(JSON.stringify(call[1] || {})).not.toMatch(/[Bb]earer|access_token/);
  });

  it('does not start the relay when disabled', () => {
    renderHook(() => useCloudPhoneEvents({ enabled: false }));
    expect(authFetch).not.toHaveBeenCalled();
  });

  it('stops the relay on teardown', async () => {
    let unmount;
    await act(async () => {
      ({ unmount } = renderHook(() => useCloudPhoneEvents({ enabled: true })));
    });
    authFetch.mockClear();
    await act(async () => {
      unmount();
    });
    expect(authFetch).toHaveBeenCalledWith('/v1/phone/cloud-events/stop', { method: 'POST' });
  });

  it('routes a relayed transcript frame to onTranscript', () => {
    const onTranscript = vi.fn();
    const onMessage = vi.fn();
    renderHook(() => useCloudPhoneEvents({ enabled: true, onMessage, onTranscript }));
    act(() => {
      wsHarness.handler({ type: 'transcript', payload: { role: 'them', text: 'hello', ts: 5 } });
    });
    expect(onTranscript).toHaveBeenCalledWith(
      expect.objectContaining({ role: 'them', text: 'hello', partial: false, ts: 5 }),
    );
    expect(onMessage).not.toHaveBeenCalled();
  });

  it('routes a relayed phone event to onMessage', () => {
    const onMessage = vi.fn();
    renderHook(() => useCloudPhoneEvents({ enabled: true, onMessage, onTranscript: vi.fn() }));
    const frame = { type: 'call_cost_update', payload: { call_id: 'c1', cost_usd: 0.12 } };
    act(() => {
      wsHarness.handler(frame);
    });
    expect(onMessage).toHaveBeenCalledWith(frame);
  });

  it('ignores non-phone frames on the shared socket', () => {
    const onMessage = vi.fn();
    const onTranscript = vi.fn();
    renderHook(() => useCloudPhoneEvents({ enabled: true, onMessage, onTranscript }));
    act(() => {
      wsHarness.handler({ type: 'state', payload: { volume: 50 } });
      wsHarness.handler({ type: 'playback', payload: {} });
    });
    expect(onMessage).not.toHaveBeenCalled();
    expect(onTranscript).not.toHaveBeenCalled();
  });
});
