import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { useVoiceOnboarding } from '../../hooks/useVoiceOnboarding';
import { apiFetch } from '../../hooks/useViolaApi';
import { gotrueClient } from '../../lib/gotrue_client';

// The mic step used to take its verdict from a PyAudio stream on the Python
// side, which is not the stack that records: push-to-talk captures through
// Chromium's getUserMedia (useVoice.js). That mismatch was wrong in BOTH
// directions — it blocked first run on devices Chromium opens fine, and it
// passed a privacy-denied microphone because its only denial test was a
// zero-LENGTH read while a denied device returns full buffers of digital
// silence. These tests pin the verdict to the real capture stack.

vi.mock('../../hooks/useViolaApi', () => ({
  apiFetch: vi.fn(),
  authFetch: vi.fn(),
}));

vi.mock('../../lib/gotrue_client', () => ({
  gotrueClient: { getSession: vi.fn() },
}));

/** An AudioContext whose analyser emits either silence or real signal. */
function installAudioContext({ silent }) {
  class FakeAnalyser {
    constructor() {
      this.fftSize = 2048;
    }

    getFloatTimeDomainData(buf) {
      for (let i = 0; i < buf.length; i += 1) {
        // Digital silence is exactly zero. A live mic always carries a noise
        // floor, which is what makes the two distinguishable at all.
        buf[i] = silent ? 0 : (i % 7 === 0 ? 0.02 : -0.01);
      }
    }
  }

  window.AudioContext = class {
    createMediaStreamSource() {
      return { connect: () => {} };
    }

    createAnalyser() {
      return new FakeAnalyser();
    }

    close() {
      return Promise.resolve();
    }
  };
}

function installMicrophone({ result, silent = false, errorName = null }) {
  const stop = vi.fn();
  const getUserMedia = vi.fn(() => {
    if (result === 'reject') {
      const err = new Error('denied');
      err.name = errorName || 'NotAllowedError';
      return Promise.reject(err);
    }
    return Promise.resolve({ getTracks: () => [{ stop }] });
  });
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia },
  });
  installAudioContext({ silent });
  return { getUserMedia, stop };
}

async function walkToMicPhase(result) {
  await waitFor(() => expect(result.current.isOnboarding).toBe(true), { timeout: 2000 });
  act(() => result.current.onWelcomeContinue());
  await waitFor(() => expect(result.current.phase).toBe('cloud_consent'), { timeout: 3000 });
  await act(async () => {
    await result.current.onCloudConsentChoice('enable');
  });
  await waitFor(() => expect(result.current.phase).toBe('autonomy_tier'), { timeout: 2000 });
  await act(async () => {
    await result.current.onAutonomyChoice('solo');
  });
  await waitFor(() => expect(result.current.phase).toBe('mic_try'), { timeout: 2000 });
}

describe('useVoiceOnboarding microphone capture probe', () => {
  beforeEach(() => {
    apiFetch.mockReset();
    gotrueClient.getSession.mockReset();
    gotrueClient.getSession.mockResolvedValue({
      data: { session: { access_token: 'x' } },
      error: null,
    });
    window.viola = {};
    window.history.replaceState({}, '', '/');
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
    delete window.viola;
    delete window.AudioContext;
    delete navigator.mediaDevices;
    window.history.replaceState({}, '', '/');
  });

  it('does not block first run when the real recorder can capture, even if the desktop probe says denied', async () => {
    // The exact false-block that walled users out: PortAudio cannot open the
    // device, Chromium can. The recorder wins.
    installMicrophone({ result: 'resolve', silent: false });
    apiFetch.mockImplementation((url) => {
      if (url === '/v1/onboarding/status') return Promise.resolve({ completed: false });
      if (url === '/v1/onboarding/check-mic-permission') {
        return Promise.resolve({
          permission_granted: false,
          permission_state: 'blocked',
          blocked: true,
          error_message: 'Your microphone is blocked by Windows settings.',
        });
      }
      if (url === '/v1/tts/speak') return Promise.resolve({ spoken: false });
      return Promise.resolve({ ok: true });
    });

    const { result } = renderHook(() => useVoiceOnboarding());
    await walkToMicPhase(result);

    await waitFor(() => expect(result.current.micPermission.checking).toBe(false), {
      timeout: 3000,
    });
    expect(result.current.micPermission.granted).toBe(true);
    expect(result.current.micPermission.blocked).toBe(false);
  });

  it('reports blocked when a granted-looking stream carries only digital silence', async () => {
    // The false-pass, isolated: the desktop probe is happy (exactly what it
    // reported for a privacy-denied mic, since its only denial test was a
    // zero-LENGTH read) while the real track carries nothing but zeroes.
    // Anything that trusts the backend here waves the user through to a mic
    // test that can never hear them.
    installMicrophone({ result: 'resolve', silent: true });
    apiFetch.mockImplementation((url) => {
      if (url === '/v1/onboarding/status') return Promise.resolve({ completed: false });
      if (url === '/v1/onboarding/check-mic-permission') {
        return Promise.resolve({
          permission_granted: true,
          permission_state: 'granted',
          blocked: false,
          error_message: null,
        });
      }
      if (url === '/v1/tts/speak') return Promise.resolve({ spoken: false });
      return Promise.resolve({ ok: true });
    });

    const { result } = renderHook(() => useVoiceOnboarding());
    await walkToMicPhase(result);

    await waitFor(() => expect(result.current.micPermission.blocked).toBe(true), {
      timeout: 4000,
    });
    expect(result.current.micPermission.granted).toBe(false);
    expect(result.current.micPermission.message).toBeTruthy();
  });

  it('offers a way forward instead of trapping the user on a blocked microphone', async () => {
    installMicrophone({ result: 'reject', errorName: 'NotAllowedError' });
    apiFetch.mockImplementation((url) => {
      if (url === '/v1/onboarding/status') return Promise.resolve({ completed: false });
      if (url === '/v1/onboarding/check-mic-permission') {
        return Promise.resolve({
          permission_granted: false,
          permission_state: 'blocked',
          blocked: true,
          error_message: 'Your microphone is blocked.',
        });
      }
      if (url === '/v1/tts/speak') return Promise.resolve({ spoken: false });
      return Promise.resolve({ ok: true });
    });

    const { result } = renderHook(() => useVoiceOnboarding());
    await walkToMicPhase(result);
    await waitFor(() => expect(result.current.micPermission.blocked).toBe(true), {
      timeout: 3000,
    });

    // Before this existed, "Check again" was the only control on the panel, so
    // a wrong verdict had no exit except abandoning first run entirely.
    expect(typeof result.current.onSkipMicStep).toBe('function');
    act(() => {
      result.current.onSkipMicStep();
    });
    await waitFor(() => expect(result.current.phase).toBe('hear_about'), { timeout: 2000 });
  });
  function configureHappyTransport(overrides = {}) {
    apiFetch.mockImplementation((url, options) => {
      if (overrides[url]) return overrides[url](options);
      if (url === '/v1/onboarding/status') return Promise.resolve({ completed: false });
      if (url === '/v1/tts/speak') return Promise.resolve({ spoken: false });
      return Promise.resolve({ ok: true });
    });
  }

  async function walkToAiChoice(result) {
    await waitFor(() => expect(result.current.isOnboarding).toBe(true));
    act(() => result.current.onWelcomeContinue());
    await waitFor(() => expect(result.current.phase).toBe('cloud_consent'), {timeout:3000});
  }

  it('keeps BYOK setup open when no configured selected profile exists', async () => {
    configureHappyTransport({'/v1/connectors/status?category=llm': () => Promise.resolve({connectors:[]})});
    const {result} = renderHook(() => useVoiceOnboarding());
    await walkToAiChoice(result);
    await act(async () => { await result.current.onCloudConsentChoice('decline'); });
    await act(async () => { await result.current.onByokSetupDone(); });
    expect(result.current.phase).toBe('cloud_consent');
    expect(result.current.cloudConsentError).toBeTruthy();
  });

  it.each([
    {id:'llm.openai',category:'llm',selected:true,ready:true,status_source:'connection_profiles+settings_manager+local_probe'},
    {id:'llm.ollama',category:'llm',selected:true,ready:true,status_source:'settings_manager+local_probe'},
    {id:'llm.openai',category:'llm',selected:true,ready:true,status_source:'single_active_settings_key'},
  ])('allows the server-ready selected provider, including legacy settings', async (profile) => {
    configureHappyTransport({'/v1/connectors/status?category=llm': () => Promise.resolve({connectors:[profile]})});
    const {result} = renderHook(() => useVoiceOnboarding());
    await walkToAiChoice(result);
    await act(async () => { await result.current.onCloudConsentChoice('decline'); });
    await act(async () => { await result.current.onByokSetupDone(); });
    await waitFor(() => expect(result.current.phase).toBe('autonomy_tier'));
  });

  it('does not advance after autonomy persistence fails', async () => {
    configureHappyTransport({'/v1/onboarding/save': (options) => {
      if (JSON.parse(options.body).step_id === 'autonomy_tier') return Promise.reject(new Error('save failed'));
      return Promise.resolve({ok:true});
    }});
    const {result} = renderHook(() => useVoiceOnboarding());
    await walkToAiChoice(result);
    await act(async () => { await result.current.onCloudConsentChoice('enable'); });
    await waitFor(() => expect(result.current.phase).toBe('autonomy_tier'));
    await act(async () => { await result.current.onAutonomyChoice('solo'); });
    expect(result.current.phase).toBe('autonomy_tier');
    expect(result.current.autonomyError).toBeTruthy();
  });

  it.each([{ok:false,error:{message:'No handler'}},{success:false,message:'No handler'},{data:{ok:false}},undefined])(
    'does not record a failed tutorial command as completed: %j', async (response) => {
      configureHappyTransport();
      installMicrophone({result:'resolve',silent:false});
      const {result}=renderHook(() => useVoiceOnboarding());
      await walkToMicPhase(result);
      await act(async () => { await result.current.onSuggestionTap('test command', async () => response); });
      expect(result.current.phase).toBe('mic_try');
      expect(result.current.tryCommandStatus).toBe('error');
      expect(apiFetch.mock.calls.filter(([url, options]) => url === '/v1/onboarding/save' && JSON.parse(options.body).step_id === 'quick_tutorial')).toHaveLength(0);
    },
  );

  it.each([
    {id:'llm.openai',category:'llm',selected:true,ready:false},
    {id:'llm.openai',category:'llm',selected:false,ready:true},
    {id:'llm.managed',category:'llm',selected:true,ready:true},
  ])('does not treat an unavailable, unselected or managed provider as BYOK readiness', async (connector) => {
    configureHappyTransport({'/v1/connectors/status?category=llm': () => Promise.resolve({connectors:[connector]})});
    const {result}=renderHook(() => useVoiceOnboarding());
    await walkToAiChoice(result);
    await act(async () => { await result.current.onCloudConsentChoice('decline'); });
    await act(async () => { await result.current.onByokSetupDone(); });
    expect(result.current.phase).toBe('cloud_consent');
    expect(result.current.cloudConsentError).toBeTruthy();
  });

});
