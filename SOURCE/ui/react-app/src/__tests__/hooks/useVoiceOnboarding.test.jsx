import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { useVoiceOnboarding } from '../../hooks/useVoiceOnboarding';
import { apiFetch, authFetch } from '../../hooks/useViolaApi';

vi.mock('../../hooks/useViolaApi', () => ({
  apiFetch: vi.fn(),
  authFetch: vi.fn(),
}));

function mockAuthFetch() {
  authFetch.mockImplementation(() =>
    Promise.resolve({
      ok: false,
      json: () => Promise.resolve({}),
    }),
  );
}

function waitMs(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

describe('useVoiceOnboarding first-run trigger', () => {
  beforeEach(() => {
    apiFetch.mockReset();
    authFetch.mockReset();
    mockAuthFetch();
    // Device-pairing onboarding is a DESKTOP-app flow. The Qt shell injects
    // window.viola, which is the signal featureSurface keys off. Establish the
    // desktop surface so the guided flow runs (the cloud web build suppresses
    // it — covered by its own test below).
    window.viola = {};
    window.history.replaceState({}, '', '/');
  });

  afterEach(() => {
    vi.clearAllMocks();
    delete window.viola;
    window.history.replaceState({}, '', '/');
  });

  it('opens on the welcome phase, then advances to account pairing', async () => {
    apiFetch.mockResolvedValueOnce({ completed: false });

    const { result } = renderHook(() => useVoiceOnboarding());

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith('/v1/onboarding/status');
    });

    // The first screen a new user sees is the warm beta welcome, not sign-in.
    await waitFor(() => {
      expect(result.current.isOnboarding).toBe(true);
      expect(result.current.phase).toBe('welcome');
      expect(result.current.isWelcomePhase).toBe(true);
      expect(result.current.isAccountPairPhase).toBe(false);
    }, { timeout: 2000 });

    // Tapping "Begin setup" moves on to the account-pairing phase.
    result.current.onWelcomeContinue();

    await waitFor(() => {
      expect(result.current.phase).toBe('account_pair');
      expect(result.current.isAccountPairPhase).toBe(true);
    }, { timeout: 2000 });
  });

  it('does not start onboarding when the persisted signal is complete', async () => {
    apiFetch.mockResolvedValueOnce({ completed: true });

    const { result } = renderHook(() => useVoiceOnboarding());

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith('/v1/onboarding/status');
    });

    await waitMs(900);
    expect(result.current.isOnboarding).toBe(false);
  });

  it('never starts device-pairing onboarding on the cloud web build', async () => {
    // No window.viola bridge -> cloud surface. The guided flow must not run and
    // must not touch the desktop-only /v1/onboarding/* route.
    delete window.viola;
    apiFetch.mockResolvedValue({ completed: false });

    const { result } = renderHook(() => useVoiceOnboarding());

    await waitMs(900);
    expect(result.current.isOnboarding).toBe(false);
    expect(apiFetch).not.toHaveBeenCalledWith('/v1/onboarding/status');
  });

  it('runs the lean six-phase flow: welcome, account, cloud, autonomy, mic+try, attribution (telemetry is not a step)', async () => {
    apiFetch.mockResolvedValueOnce({ completed: false });

    const { result } = renderHook(() => useVoiceOnboarding());

    await waitFor(() => {
      expect(result.current.isOnboarding).toBe(true);
    }, { timeout: 2000 });

    expect(result.current.totalPhases).toBe(6);
    expect(result.current.isAttributionPhase).toBe(false);
    expect(result.current.isAutonomyPhase).toBe(false);
    // Telemetry is no longer an onboarding phase (moved to Settings, default off).
    expect(result.current.isTelemetryPhase).toBeUndefined();
    expect(result.current.onTelemetryChoice).toBeUndefined();
  });

  it('ignores attribution/autonomy answers outside their phases (no settings write)', async () => {
    apiFetch.mockResolvedValueOnce({ completed: false });

    const { result } = renderHook(() => useVoiceOnboarding());

    await waitFor(() => {
      expect(result.current.isOnboarding).toBe(true);
    }, { timeout: 2000 });

    // Still on the welcome phase: neither handler may fire or write settings.
    expect(await result.current.onAttributionChoice('reddit')).toBe(false);
    expect(await result.current.onAutonomyChoice('ensemble')).toBe(false);
    expect(apiFetch).not.toHaveBeenCalledWith('/v1/settings', expect.anything());
  });

  // #4225: the account step was index 1 of 6 with canSkip:false and a single
  // sign-in action, so first run could not be finished, or left, without a
  // cloud account. Viola's runtime never required that: core/account_gate.py
  // exempts BYOK / Codex / local, and the gate fires at managed-provider
  // construction, not at launch. These cover the guest path and the exit.
  async function reachAccountPhase() {
    apiFetch.mockResolvedValueOnce({ completed: false });
    const { result } = renderHook(() => useVoiceOnboarding());

    await waitFor(() => {
      expect(result.current.isOnboarding).toBe(true);
      expect(result.current.phase).toBe('welcome');
    }, { timeout: 2000 });

    result.current.onWelcomeContinue();

    await waitFor(() => {
      expect(result.current.phase).toBe('account_pair');
    }, { timeout: 2000 });

    return result;
  }

  it('continues past the account step with no session, and records the choice', async () => {
    const result = await reachAccountPhase();

    expect(result.current.accountSkipped).toBe(false);
    expect(result.current.onContinueWithoutAccount()).toBe(true);

    // No GoTrue session exists in this test, and none is created: the user
    // simply moves on to the next step.
    await waitFor(() => {
      expect(result.current.phase).toBe('cloud_consent');
    }, { timeout: 2000 });
    expect(result.current.accountSkipped).toBe(true);
    expect(result.current.signInStatus).toBe('no_account');

    expect(apiFetch).toHaveBeenCalledWith(
      '/v1/onboarding/save',
      expect.objectContaining({
        body: JSON.stringify({
          step_id: 'api_access',
          data: { account_signed_in: false, continued_without_account: true },
        }),
      }),
    );
  });

  it('routes the no-account user to their own key, never to managed AI', async () => {
    const result = await reachAccountPhase();
    result.current.onContinueWithoutAccount();
    await waitFor(() => {
      expect(result.current.phase).toBe('cloud_consent');
    }, { timeout: 2000 });

    // Managed AI is metered per account, so it must not be selectable here.
    expect(await result.current.onCloudConsentChoice('enable')).toBe(false);
    expect(apiFetch).not.toHaveBeenCalledWith(
      '/v1/settings',
      expect.objectContaining({ body: JSON.stringify({ settings: { ai_source: 'managed' } }) }),
    );

    // The BYOK path they do have still works.
    expect(await result.current.onCloudConsentChoice('decline')).toBe(true);
    expect(apiFetch).toHaveBeenCalledWith(
      '/v1/settings',
      expect.objectContaining({ body: JSON.stringify({ settings: { ai_source: 'byok' } }) }),
    );
    await waitFor(() => {
      expect(result.current.cloudConsentStatus).toBe('declined');
    }, { timeout: 2000 });
  });

  it('lets a no-account user change their mind and go back to signing in', async () => {
    const result = await reachAccountPhase();
    result.current.onContinueWithoutAccount();
    await waitFor(() => {
      expect(result.current.phase).toBe('cloud_consent');
    }, { timeout: 2000 });

    expect(result.current.onReturnToSignIn()).toBe(true);

    await waitFor(() => {
      expect(result.current.phase).toBe('account_pair');
      expect(result.current.accountSkipped).toBe(false);
    }, { timeout: 2000 });
  });

  it('dismisses first run from the phases that used to hold the user', async () => {
    // welcome: the very first screen, previously canSkip:false.
    apiFetch.mockResolvedValueOnce({ completed: false });
    const { result: fromWelcome } = renderHook(() => useVoiceOnboarding());
    await waitFor(() => {
      expect(fromWelcome.current.isOnboarding).toBe(true);
    }, { timeout: 2000 });
    fromWelcome.current.skipOnboarding();
    await waitFor(() => {
      expect(fromWelcome.current.isOnboarding).toBe(false);
    }, { timeout: 2000 });
    expect(apiFetch).toHaveBeenCalledWith('/v1/onboarding/complete', { method: 'POST' });

    // account_pair: the step this ticket is about.
    const atAccount = await reachAccountPhase();
    atAccount.current.skipOnboarding();
    await waitFor(() => {
      expect(atAccount.current.isOnboarding).toBe(false);
    }, { timeout: 2000 });

    // cloud_consent: reached through the guest path.
    const atCloud = await reachAccountPhase();
    atCloud.current.onContinueWithoutAccount();
    await waitFor(() => {
      expect(atCloud.current.phase).toBe('cloud_consent');
    }, { timeout: 2000 });
    atCloud.current.skipOnboarding();
    await waitFor(() => {
      expect(atCloud.current.isOnboarding).toBe(false);
    }, { timeout: 2000 });
  });

  it('ignores the guest path outside the account step', async () => {
    apiFetch.mockResolvedValueOnce({ completed: false });
    const { result } = renderHook(() => useVoiceOnboarding());
    await waitFor(() => {
      expect(result.current.phase).toBe('welcome');
    }, { timeout: 2000 });

    expect(result.current.onContinueWithoutAccount()).toBe(false);
    expect(result.current.onReturnToSignIn()).toBe(false);
    expect(result.current.accountSkipped).toBe(false);
    expect(result.current.phase).toBe('welcome');
  });

  it('follows the hub onboarding state on a multiroom spoke', async () => {
    delete window.viola;
    window.history.replaceState({}, '', '/?room=kitchen&spoke_token=qr-token');
    apiFetch.mockResolvedValueOnce({ completed: false });

    const { result } = renderHook(() => useVoiceOnboarding({ isSpoke: true }));

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith('/v1/onboarding/status');
    });

    await waitFor(() => {
      expect(result.current.isOnboarding).toBe(true);
      expect(result.current.phase).toBe('welcome');
    }, { timeout: 2000 });
  });
});
