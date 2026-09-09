import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { useUsage } from './useUsage';
import { apiFetch } from './useViolaApi';

/**
 * Direct hook-level coverage for issue #1175, complementing
 * AccountTab.usageCloudAuth.test.jsx's integration-level acceptance oracle.
 *
 * useUsage() used to gate its /billing/usage poll on hooks/useAuth's
 * `isLoggedIn` alone, so a user authenticated ONLY through the cloud front
 * door (auth/useAuth `status === 'signedIn'`) never triggered a fetch. The
 * fix reconciles both auth stores: `isLoggedIn = appIsLoggedIn ||
 * cloudStatus === 'signedIn'`.
 *
 * These tests exercise the hook directly (no AccountTab/DOM in the way) and
 * add the transition case: a cloud-only session that later signs out must
 * clear usage and stop polling, exactly as a hooks/useAuth-only session
 * always did - the reconciliation must not regress the negative/closed
 * path while fixing the positive one.
 */
const hooksAuthMock = vi.hoisted(() => ({ value: null }));
const cloudAuthMock = vi.hoisted(() => ({ value: null }));

vi.mock('./useAuth', () => ({
  useAuth: () => hooksAuthMock.value,
}));

vi.mock('../auth/useAuth', () => ({
  useAuth: () => cloudAuthMock.value,
}));

vi.mock('./useViolaApi', () => ({
  apiFetch: vi.fn(),
}));

function setAuth({ appIsLoggedIn, cloudStatus }) {
  hooksAuthMock.value = { isLoggedIn: appIsLoggedIn };
  cloudAuthMock.value = { status: cloudStatus };
}

const USAGE_PAYLOAD = {
  monthly_percent: 55,
  weekly_percent: 0,
  monthly_capped: true,
  weekly_capped: false,
  resets_monthly: '2026-08-01T00:00:00Z',
  resets_weekly: '2026-07-28T00:00:00Z',
};

describe('useUsage - auth-store reconciliation (#1175)', () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it('never fetches for a genuinely signed-out user (both stores false)', async () => {
    setAuth({ appIsLoggedIn: false, cloudStatus: 'signedOut' });
    apiFetch.mockResolvedValue(USAGE_PAYLOAD);

    const { result } = renderHook(() => useUsage());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(apiFetch).not.toHaveBeenCalled();
    expect(result.current.usage).toBeNull();
  });

  it('fetches for a user authenticated ONLY via the cloud auth context - the old bug shape', async () => {
    // hooks/useAuth (appIsLoggedIn) stays false the whole time - this is
    // exactly the desync #1175 fixed: the cloud front door is the only
    // store that ever says signed in.
    setAuth({ appIsLoggedIn: false, cloudStatus: 'signedIn' });
    apiFetch.mockResolvedValue(USAGE_PAYLOAD);

    const { result } = renderHook(() => useUsage());

    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/billing/usage'));
    await waitFor(() => expect(result.current.usage).toEqual({
      monthlyPercent: 55,
      weeklyPercent: null,
      monthlyResetsAt: '2026-08-01T00:00:00Z',
      weeklyResetsAt: '2026-07-28T00:00:00Z',
    }));
  });

  it('fetches for a user authenticated ONLY via the legacy app auth store', async () => {
    setAuth({ appIsLoggedIn: true, cloudStatus: 'signedOut' });
    apiFetch.mockResolvedValue(USAGE_PAYLOAD);

    const { result } = renderHook(() => useUsage());

    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/billing/usage'));
    await waitFor(() => expect(result.current.usage).not.toBeNull());
  });

  it('clears usage on the cloud-only signed-in -> signed-out transition (the closed/logged-out path)', async () => {
    setAuth({ appIsLoggedIn: false, cloudStatus: 'signedIn' });
    apiFetch.mockResolvedValue(USAGE_PAYLOAD);

    const { result, rerender } = renderHook(() => useUsage());

    await waitFor(() => expect(result.current.usage).not.toBeNull());
    apiFetch.mockClear();

    // The user signs out through the cloud front door - both stores now
    // agree the session is closed.
    setAuth({ appIsLoggedIn: false, cloudStatus: 'signedOut' });
    rerender();

    await waitFor(() => expect(result.current.usage).toBeNull());
    // No further poll should fire once signed out.
    expect(apiFetch).not.toHaveBeenCalled();
  });
});
