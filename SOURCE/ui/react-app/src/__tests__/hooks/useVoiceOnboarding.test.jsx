import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { useVoiceOnboarding } from '../../hooks/useVoiceOnboarding';
import { apiFetch, authFetch } from '../../hooks/useViolaApi';
import { gotrueClient } from '../../lib/gotrue_client';
import en from '../../i18n/en.json';

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
    // The onboarding phase persists across a reload (sessionStorage); start each test fresh.
    window.sessionStorage.clear();
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

  // Steps used to wait for their narration to finish before advancing: the
  // server accepts a line instantly, then the hook sat out an estimated
  // speaking time (230 ms a word, 1.8 s minimum) before moving on, so a click
  // on "Enable cloud AI" or a finished sign-in looked like seconds of input
  // lag. Every assertion below has a 1 s budget, inside that old floor.
  describe('narration never gates a step', () => {
    let speakCalls;
    let stopCalls;
    let settingsImpl;
    let sessionSpy;

    beforeEach(() => {
      speakCalls = [];
      stopCalls = [];
      settingsImpl = () => Promise.resolve({ ok: true });
      apiFetch.mockImplementation((path, options = {}) => {
        if (path === '/v1/onboarding/status') return Promise.resolve({ completed: false });
        if (path === '/v1/tts/speak') {
          const narrationId = speakCalls.length + 1;
          speakCalls.push({ text: JSON.parse(options.body).text, narrationId });
          return Promise.resolve({ ok: true, spoken: true, narration_id: narrationId });
        }
        if (path === '/v1/tts/stop') {
          stopCalls.push(JSON.parse(options.body));
          return Promise.resolve({ ok: true, stopped: true });
        }
        if (path === '/v1/settings') return settingsImpl(options);
        return Promise.resolve({ ok: true });
      });
      sessionSpy = vi.spyOn(gotrueClient, 'getSession').mockResolvedValue({ data: { session: null }, error: null });
    });

    afterEach(() => {
      sessionSpy.mockRestore();
    });

    const fast = { timeout: 1000 };

    async function reachCloudConsentSignedIn() {
      const { result } = renderHook(() => useVoiceOnboarding());
      await waitFor(() => expect(result.current.isWelcomePhase).toBe(true), { timeout: 2000 });
      result.current.onWelcomeContinue();
      await waitFor(() => expect(result.current.phase).toBe('account_pair'), fast);
      // The user finishes signing in through Account settings.
      sessionSpy.mockResolvedValue({ data: { session: { access_token: 'x' } }, error: null });
      await waitFor(() => expect(result.current.phase).toBe('cloud_consent'), { timeout: 4000 });
      return result;
    }

    it('advances the moment sign-in lands, confirming it in the next line', async () => {
      const { result } = renderHook(() => useVoiceOnboarding());
      await waitFor(() => expect(result.current.isWelcomePhase).toBe(true), { timeout: 2000 });
      result.current.onWelcomeContinue();
      await waitFor(() => expect(result.current.phase).toBe('account_pair'), fast);
      await waitFor(() => expect(result.current.signInStatus).toBe('awaiting_user'), fast);

      sessionSpy.mockResolvedValue({ data: { session: { access_token: 'x' } }, error: null });
      // Picked up on the next 2.5 s session poll, then no further wait.
      await waitFor(() => expect(result.current.signInStatus).toBe('signed_in'), { timeout: 4000 });
      await waitFor(() => expect(result.current.phase).toBe('cloud_consent'), fast);
      await waitFor(() => {
        expect(speakCalls.at(-1).text.startsWith('Signed in. Let us finish setting up.')).toBe(true);
      }, fast);
    });

    it('advances as soon as "Enable cloud AI" is saved and cuts the old line', async () => {
      const result = await reachCloudConsentSignedIn();
      await waitFor(() => expect(speakCalls.at(-1).text).toContain('Signed in.'), fast);
      const cloudLine = speakCalls.at(-1);

      const started = Date.now();
      expect(await result.current.onCloudConsentChoice('enable')).toBe(true);
      await waitFor(() => expect(result.current.phase).toBe('autonomy_tier'), fast);
      expect(Date.now() - started).toBeLessThan(1000);

      // The step's own line is cut when the user moves on, by its id, so the
      // stop can never silence the next step's line.
      await waitFor(() => expect(stopCalls).toContainEqual({ narration_id: cloudLine.narrationId }), fast);
      // The confirmation is the opening of the next step's narration.
      await waitFor(() => {
        expect(speakCalls.at(-1).text.startsWith('Cloud AI is enabled.')).toBe(true);
      }, fast);
    });

    it('finishes first run without waiting on the optional attribution save', async () => {
      const result = await reachCloudConsentSignedIn();
      await result.current.onCloudConsentChoice('enable');
      await waitFor(() => expect(result.current.phase).toBe('autonomy_tier'), fast);
      await result.current.onAutonomyChoice('solo');
      await waitFor(() => expect(result.current.phase).toBe('mic_try'), fast);
      result.current.onSkipMicStep();
      await waitFor(() => expect(result.current.phase).toBe('hear_about'), fast);
      await waitFor(() => expect(speakCalls.at(-1).text).toBe(en.onboarding.phases.hear_about.speech), fast);

      // A settings round-trip that never answers must not hold the last screen.
      settingsImpl = () => new Promise(() => {});
      const lineBefore = speakCalls.at(-1);
      expect(await result.current.onAttributionChoice('reddit')).toBe(true);
      await waitFor(() => expect(result.current.isOnboarding).toBe(false), fast);
      expect(apiFetch).toHaveBeenCalledWith('/v1/onboarding/complete', { method: 'POST' });
      // Leaving first run cuts whatever was still being said.
      await waitFor(() => expect(stopCalls).toContainEqual({ narration_id: lineBefore.narrationId }), fast);
    });
  });
});
