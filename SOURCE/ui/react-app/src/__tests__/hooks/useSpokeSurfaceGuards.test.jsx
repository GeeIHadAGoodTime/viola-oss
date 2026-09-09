import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import { apiFetch, authFetch } from '../../hooks/useViolaApi';
import { useAgentRegistry } from '../../hooks/useAgentRegistry';
import { useAvailableRooms } from '../../hooks/useAvailableRooms';
import { useCalendarEvents } from '../../hooks/useCalendarEvents';
import { useSettings } from '../../hooks/useSettings';

vi.mock('../../hooks/useViolaApi', () => ({
  apiFetch: vi.fn(),
  authFetch: vi.fn(),
  buildStreamUrl: vi.fn((streamId) => Promise.resolve(`/v1/agents/stream/${streamId}`)),
}));

vi.mock('../../hooks/useWebSocket', () => ({
  useWebSocket: vi.fn(() => ({
    send: vi.fn(),
    connectCount: 0,
    setBinaryCallback: vi.fn(),
    setDisconnectCallback: vi.fn(),
    getWsDebug: () => ({}),
  })),
}));

describe('multiroom spoke hub-surface parity guards', () => {
  beforeEach(() => {
    apiFetch.mockReset();
    authFetch.mockReset();
    apiFetch.mockResolvedValue({});
    authFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({}),
    });
    delete window.viola;
    window.history.replaceState({}, '', '/?room=kitchen&spoke_token=qr-token');
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    delete window.viola;
    window.history.replaceState({}, '', '/');
  });

  it('fetches room management routes from a hub-backed spoke', async () => {
    const { result } = renderHook(() => useAvailableRooms({ isSpoke: true }));

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });
    expect(result.current.rooms).toEqual([]);
    expect(apiFetch).toHaveBeenCalledWith('/api/v1/rooms');
  });

  it('polls the desktop agent registry from a hub-backed spoke', async () => {
    renderHook(() => useAgentRegistry({ isSpoke: true }));

    await waitFor(() => {
      expect(authFetch).toHaveBeenCalledWith('/v1/agents', expect.anything());
    });
  });

  it('fetches desktop calendar routes from a hub-backed spoke', async () => {
    const { result } = renderHook(() => useCalendarEvents({ isSpoke: true }));

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith(expect.stringMatching(/^\/v1\/calendar\/events/));
    });
    expect(result.current.calendarStatus).not.toBe('unavailable_web');
  });

  it('fetches desktop settings routes from a hub-backed spoke', async () => {
    const { result } = renderHook(() => useSettings({ isSpoke: true }));

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });
    expect(result.current.settings).toEqual({});
    expect(apiFetch).toHaveBeenCalledWith('/v1/settings');
  });
});
