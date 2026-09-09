import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { useCloudWelcome } from './useCloudWelcome';
import { apiFetch } from './useViolaApi';

vi.mock('./useViolaApi', () => ({
  apiFetch: vi.fn(),
}));

describe('useCloudWelcome', () => {
  beforeEach(() => {
    apiFetch.mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('starts unknown (null) and resolves to false for a fresh account', async () => {
    apiFetch.mockResolvedValueOnce({ completed: false });

    const { result } = renderHook(() => useCloudWelcome({ enabled: true }));

    expect(result.current.completed).toBeNull();
    await waitFor(() => expect(result.current.completed).toBe(false));
    expect(apiFetch).toHaveBeenCalledWith('/v1/cloud-welcome/status');
  });

  it('resolves to true for a returning completed account', async () => {
    apiFetch.mockResolvedValueOnce({ completed: true });

    const { result } = renderHook(() => useCloudWelcome({ enabled: true }));

    await waitFor(() => expect(result.current.completed).toBe(true));
  });

  it('never fetches when disabled', async () => {
    const { result } = renderHook(() => useCloudWelcome({ enabled: false }));

    await waitMs(10);
    expect(apiFetch).not.toHaveBeenCalled();
    expect(result.current.completed).toBeNull();
  });

  it('complete() posts to the completion endpoint and flips completed to true', async () => {
    apiFetch.mockResolvedValueOnce({ completed: false }); // initial status read
    apiFetch.mockResolvedValueOnce({ completed: true }); // complete write

    const { result } = renderHook(() => useCloudWelcome({ enabled: true }));
    await waitFor(() => expect(result.current.completed).toBe(false));

    await act(async () => {
      const ok = await result.current.complete();
      expect(ok).toBe(true);
    });

    expect(result.current.completed).toBe(true);
    expect(apiFetch).toHaveBeenCalledWith('/v1/cloud-welcome/complete', { method: 'POST' });
  });

  it('complete() still marks locally-done on a network failure (best effort, never traps the user)', async () => {
    apiFetch.mockResolvedValueOnce({ completed: false });
    apiFetch.mockRejectedValueOnce(new Error('network down'));

    const { result } = renderHook(() => useCloudWelcome({ enabled: true }));
    await waitFor(() => expect(result.current.completed).toBe(false));

    await act(async () => {
      const ok = await result.current.complete();
      expect(ok).toBe(false);
    });

    expect(result.current.completed).toBe(true);
    expect(result.current.error).toBeTruthy();
  });
});

function waitMs(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}
