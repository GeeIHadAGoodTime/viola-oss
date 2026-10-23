import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { useVoiceOnboarding } from '../../hooks/useVoiceOnboarding';
import { apiFetch, authFetch } from '../../hooks/useViolaApi';

vi.mock('../../hooks/useViolaApi', () => ({
  apiFetch: vi.fn(),
  authFetch: vi.fn(),
}));

// Signing in during first run reloads the desktop shell. The phase used to live
// only in React state, so the reload dropped the user back on the welcome
// screen (the 1.0.5 laptop report). It now survives the reload.
describe('useVoiceOnboarding phase survives a page reload', () => {
  beforeEach(() => {
    apiFetch.mockReset();
    authFetch.mockReset();
    authFetch.mockResolvedValue({ ok: false, json: () => Promise.resolve({}) });
    window.sessionStorage.clear();
    window.viola = {};
  });

  afterEach(() => {
    vi.clearAllMocks();
    delete window.viola;
    window.sessionStorage.clear();
  });

  it('resumes on the phase the user had reached instead of restarting at welcome', async () => {
    apiFetch.mockResolvedValue({ completed: false });
    const first = renderHook(() => useVoiceOnboarding());
    await waitFor(() => expect(first.result.current.phase).toBe('welcome'), { timeout: 2000 });
    await waitFor(() => expect(first.result.current.isOnboarding).toBe(true), { timeout: 2000 });
    first.result.current.onWelcomeContinue();
    await waitFor(() => expect(first.result.current.phase).toBe('account_pair'), { timeout: 2000 });
    first.unmount();

    // Same tab, fresh mount: what a location.reload() produces.
    const second = renderHook(() => useVoiceOnboarding());
    await waitFor(() => expect(second.result.current.isOnboarding).toBe(true), { timeout: 2000 });
    expect(second.result.current.phase).toBe('account_pair');
    expect(second.result.current.isWelcomePhase).toBe(false);
  });

  it('ignores an unknown stored phase', async () => {
    window.sessionStorage.setItem('viola_onboarding_phase', 'not_a_phase');
    apiFetch.mockResolvedValue({ completed: false });
    const { result } = renderHook(() => useVoiceOnboarding());
    await waitFor(() => expect(result.current.isOnboarding).toBe(true), { timeout: 2000 });
    expect(result.current.phase).toBe('welcome');
  });

  it('does not reopen onboarding from a stored phase once setup is complete', async () => {
    window.sessionStorage.setItem('viola_onboarding_phase', 'cloud_consent');
    apiFetch.mockResolvedValue({ completed: true });
    const { result } = renderHook(() => useVoiceOnboarding());
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/v1/onboarding/status'));
    await new Promise((resolve) => { setTimeout(resolve, 900); });
    expect(result.current.isOnboarding).toBe(false);
  });
});
