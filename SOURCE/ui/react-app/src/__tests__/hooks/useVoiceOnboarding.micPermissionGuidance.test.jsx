import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { useVoiceOnboarding } from '../../hooks/useVoiceOnboarding';
import { apiFetch, authFetch } from '../../hooks/useViolaApi';
import { gotrueClient } from '../../lib/gotrue_client';

// C-078: the mic-permission-denied guidance shown during onboarding used to
// hardcode Windows-only copy (useVoiceOnboarding.js:225) regardless of the
// actual host OS, so a Mac or Linux desktop user hitting a mic-check network
// failure was told to check Windows settings that do not exist on their
// machine. This drives the real hook all the way to the mic_try phase on a
// stubbed Mac host and proves the rendered guidance names macOS, never
// Windows — the actual user-visible regression, not just the pure helper.

vi.mock('../../hooks/useViolaApi', () => ({
  apiFetch: vi.fn(),
  authFetch: vi.fn(),
}));

vi.mock('../../lib/gotrue_client', () => ({
  gotrueClient: { getSession: vi.fn() },
}));

function stubMacHost() {
  vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
  );
  vi.spyOn(navigator, 'platform', 'get').mockReturnValue('MacIntel');
}

describe('useVoiceOnboarding mic-permission guidance (issue C-078)', () => {
  beforeEach(() => {
    apiFetch.mockReset();
    authFetch.mockReset();
    gotrueClient.getSession.mockReset();
    // authFetch (TTS narration) always misses so speakText falls back to the
    // browser speechSynthesis branch, which resolves immediately in jsdom
    // (no 'speechSynthesis' in window) instead of waiting out a real timer.
    authFetch.mockResolvedValue({ ok: false, json: () => Promise.resolve({}) });
    // Desktop surface: device-pairing onboarding is desktop-only.
    window.viola = {};
    window.history.replaceState({}, '', '/');
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
    delete window.viola;
    window.history.replaceState({}, '', '/');
  });

  it('shows macOS guidance, never Windows copy, when the mic check fails on a Mac host', async () => {
    stubMacHost();
    gotrueClient.getSession.mockResolvedValue({ data: { session: { access_token: 'x' } }, error: null });

    apiFetch.mockImplementation((url) => {
      if (url === '/v1/onboarding/status') return Promise.resolve({ completed: false });
      if (url === '/v1/onboarding/check-mic-permission') {
        return Promise.reject(new Error('network unreachable'));
      }
      // /v1/onboarding/save, /v1/settings, etc.
      return Promise.resolve({ ok: true });
    });

    const { result } = renderHook(() => useVoiceOnboarding());

    await waitFor(() => expect(result.current.isOnboarding).toBe(true), { timeout: 2000 });
    expect(result.current.phase).toBe('welcome');

    act(() => result.current.onWelcomeContinue());
    await waitFor(() => expect(result.current.phase).toBe('account_pair'), { timeout: 2000 });

    // The signed-in probe fires immediately and auto-advances once signed in.
    await waitFor(() => expect(result.current.phase).toBe('cloud_consent'), { timeout: 3000 });

    await act(async () => {
      await result.current.onCloudConsentChoice('enable');
    });
    await waitFor(() => expect(result.current.phase).toBe('autonomy_tier'), { timeout: 2000 });

    await act(async () => {
      await result.current.onAutonomyChoice('solo');
    });
    await waitFor(() => expect(result.current.phase).toBe('mic_try'), { timeout: 2000 });

    // The mic_try phase effect runs checkMicPermission, which rejects above.
    await waitFor(() => expect(result.current.micPermission.blocked).toBe(true), { timeout: 2000 });

    expect(result.current.micPermission.message).toMatch(/System Settings/);
    expect(result.current.micPermission.message).not.toMatch(/Windows/);
  });
});
