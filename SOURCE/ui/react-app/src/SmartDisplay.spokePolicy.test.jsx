import { act, render, waitFor, cleanup } from './test/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SmartDisplay from './SmartDisplay';

const harness = vi.hoisted(() => ({ settings: null }));
vi.mock('./hooks/useSettings', async (importOriginal) => {
  const actual = await importOriginal();
  return { useSettings: options => {
    const result = actual.useSettings(options);
    harness.settings = result;
    return result;
  } };
});
vi.mock('./components/auth/cloudSurface', () => ({ isCloudSurface: () => false, isSpokeRoute: () => false }));
vi.mock('./hooks/useAuth', () => ({ useAuth: () => ({ user: null, status: 'signedOut' }) }));
vi.mock('./hooks/useVoiceOnboarding', () => ({ useVoiceOnboarding: () => ({ isActive: false }) }));
vi.mock('./hooks/useHandsFreeWake', () => ({ useHandsFreeWake: () => [false, vi.fn()] }));
vi.mock('./hooks/useBrowserWakeWord', () => ({ useBrowserWakeWord: () => ({ status: 'off' }) }));
vi.mock('./sentryClient', () => ({ syncSentrySettings: vi.fn(), openSentryUserFeedback: vi.fn() }));
beforeEach(() => {
  window.__VIOLA_CLOUD__ = false;
  localStorage.clear();
  vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve({
    ok: true, json: async () => ({ completed: true, settings: {}, data: {}, threads: [] }),
  }));
});
afterEach(() => { cleanup(); delete window.__VIOLA_CLOUD__; });

it('independent review: initial settings failure keeps the spoke capture gate closed', async () => {
  const currentFetch = globalThis.fetch.getMockImplementation();
  let rejectSettings;
  globalThis.fetch.mockImplementation((url, options) => {
    if (String(url).endsWith('/v1/settings')) return new Promise((_, reject) => { rejectSettings = reject; });
    return currentFetch(url, options);
  });
  const onVoiceCaptureEnabledChange = vi.fn();
  render(<SmartDisplay isSpoke onVoiceCaptureEnabledChange={onVoiceCaptureEnabledChange} />);
  await waitFor(() => expect(rejectSettings).toBeTypeOf('function'));
  expect(onVoiceCaptureEnabledChange).toHaveBeenLastCalledWith(false);
  await act(async () => { rejectSettings(new Error('synthetic settings unavailable')); });
  expect(onVoiceCaptureEnabledChange).toHaveBeenLastCalledWith(false);
});

describe('real accepted-settings spoke policy', () => {
  it('recovers after a valid retry and preserves known policy across unrelated persistence errors', async () => {
    const currentFetch = globalThis.fetch.getMockImplementation();
    let settings = null;
    globalThis.fetch.mockImplementation((url, options) => {
      if (String(url).endsWith('/v1/settings')) {
        if (!settings || options?.method === 'POST') return Promise.reject(new Error('synthetic unavailable'));
        return Promise.resolve({ ok: true, json: async () => ({ settings }) });
      }
      return currentFetch(url, options);
    });
    const onVoiceCaptureEnabledChange = vi.fn();
    render(<SmartDisplay isSpoke onVoiceCaptureEnabledChange={onVoiceCaptureEnabledChange} />);
    await waitFor(() => expect(harness.settings.loading).toBe(false));
    expect(onVoiceCaptureEnabledChange).toHaveBeenLastCalledWith(false);
    settings = { voice_mode: 'disabled', mic_muted: true };
    await act(async () => harness.settings.refreshSettings());
    expect(onVoiceCaptureEnabledChange).toHaveBeenLastCalledWith(false);
    settings = { voice_mode: 'wake_word', mic_muted: false };
    await act(async () => harness.settings.refreshSettings());
    expect(onVoiceCaptureEnabledChange).toHaveBeenLastCalledWith(true);
    await act(async () => harness.settings.updateSetting('theme', 'dark'));
    expect(harness.settings.error).toBe('Failed to save settings');
    expect(onVoiceCaptureEnabledChange).toHaveBeenLastCalledWith(true);
  });
});
