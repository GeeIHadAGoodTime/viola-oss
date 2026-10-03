import { act, render, screen, waitFor, cleanup } from './test/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SmartDisplay from './SmartDisplay';

const state = vi.hoisted(() => ({
  settings: { voice_mode: 'wake_word' }, loading: false, voiceStatus: null,
  browserWake: { status: 'off' }, browserOptions: null, handsFree: false,
  user: null,
}));
vi.mock('./hooks/useSettings', () => ({ useSettings: () => ({
  settings: state.settings, loading: state.loading, voiceStatus: state.voiceStatus,
  updateSetting: vi.fn(), refreshSettings: vi.fn(),
}) }));
vi.mock('./components/auth/cloudSurface', () => ({ isCloudSurface: () => false, isSpokeRoute: () => false }));
vi.mock('./hooks/useAuth', () => ({ useAuth: () => ({ user: state.user, status: state.user ? 'signedIn' : 'signedOut' }) }));
vi.mock('./hooks/useVoiceOnboarding', () => ({ useVoiceOnboarding: () => ({ isActive: false }) }));
vi.mock('./hooks/useHandsFreeWake', () => ({ useHandsFreeWake: () => [state.handsFree, vi.fn()] }));
vi.mock('./hooks/useBrowserWakeWord', () => ({ useBrowserWakeWord: (options) => {
  state.browserOptions = options;
  return state.browserWake;
} }));
vi.mock('./sentryClient', () => ({ syncSentrySettings: vi.fn(), openSentryUserFeedback: vi.fn() }));

const health = (wake) => ({ ok: true, json: async () => ({ dependencies: { wake_detector: wake } }) });
const working = { status: 'ok', is_running: true };
const pttLabel = () => screen.getByRole('button', { name: 'Push to talk' }).parentElement;
let healthRequest;
let polls;

beforeEach(() => {
  state.settings = { voice_mode: 'wake_word' };
  state.loading = false;
  state.voiceStatus = null;
  state.browserWake = { status: 'off' };
  state.handsFree = false;
  state.user = null;
  window.__VIOLA_CLOUD__ = false;
  localStorage.clear();
  healthRequest = vi.fn().mockResolvedValue(health(working));
  vi.spyOn(globalThis, 'fetch').mockImplementation((url, options) => {
    if (String(url).endsWith('/health')) return healthRequest(url, options);
    return Promise.resolve({ ok: true, json: async () => ({ completed: true, settings: {}, data: {}, threads: [] }) });
  });
  polls = [];
  const originalInterval = globalThis.setInterval;
  vi.spyOn(globalThis, 'setInterval').mockImplementation((callback, delay, ...args) => {
    if (delay === 3000) polls.push(callback);
    return originalInterval(callback, delay, ...args);
  });
});
afterEach(() => { cleanup(); delete window.__VIOLA_CLOUD__; });
const poll = async () => { await act(async () => { await polls[0](); }); };

describe('real SmartDisplay wake status', () => {
  it('does not call an unknown detector on or listening before the first health response', async () => {
    healthRequest.mockReturnValue(new Promise(() => {}));
    render(<SmartDisplay />);
    expect(pttLabel()).toHaveTextContent('status unknown');
  });

  it.each([
    [{ voice_mode: 'disabled' }, 'off'],
    [{ voice_mode: 'wake_word', mic_muted: true }, 'muted'],
    [{ voice_mode: 'push_to_talk' }, 'off'],
  ])('gives voice settings %j precedence over a stale running detector', async (settings, label) => {
    const { rerender } = render(<SmartDisplay />);
    await waitFor(() => expect(pttLabel()).toHaveTextContent('listening'));
    state.settings = settings;
    rerender(<SmartDisplay />);
    expect(pttLabel()).toHaveTextContent(label);
    expect(pttLabel()).not.toHaveTextContent('listening');
  });

  it.each([
    [{ status: 'degraded', reason: 'not_initialized', is_running: false }, 'starting'],
    [{ status: 'degraded', reason: 'stalled', is_running: false }, 'unavailable'],
    [{ status: 'degraded', reason: 'circuit_open', is_running: false }, 'unavailable'],
    [{ status: 'error', is_running: false }, 'unavailable'],
    [{ status: 'ok', reason: 'stopped', is_running: false }, 'not listening'],
  ])('renders health transitions %j and recovers after a later working report', async (next, label) => {
    render(<SmartDisplay />);
    await waitFor(() => expect(pttLabel()).toHaveTextContent('listening'));
    healthRequest.mockResolvedValue(health(next));
    await poll();
    expect(pttLabel()).toHaveTextContent(label);
    healthRequest.mockResolvedValue(health(working));
    await poll();
    expect(pttLabel()).toHaveTextContent('listening');
  });

  it.each(['network', 'http', 'missing', 'invalid'])('clears a previous listening claim after %s health failure', async (failure) => {
    render(<SmartDisplay />);
    await waitFor(() => expect(pttLabel()).toHaveTextContent('listening'));
    if (failure === 'network') healthRequest.mockRejectedValue(new Error('offline'));
    if (failure === 'http') healthRequest.mockResolvedValue({ ok: false });
    if (failure === 'missing') healthRequest.mockResolvedValue(health(undefined));
    if (failure === 'invalid') healthRequest.mockResolvedValue({ ok: true, json: async () => { throw new Error('bad json'); } });
    await poll();
    expect(pttLabel()).toHaveTextContent('status unknown');
    healthRequest.mockResolvedValue(health(working));
    await poll();
    expect(pttLabel()).toHaveTextContent('listening');
  });

  it('an older pending response cannot restore listening after a newer failure', async () => {
    let resolveOld;
    healthRequest.mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; }));
    render(<SmartDisplay />);
    healthRequest.mockResolvedValue({ ok: false });
    await poll();
    await act(async () => { resolveOld(health(working)); });
    expect(pttLabel()).toHaveTextContent('status unknown');
  });

  it.each([{ id: 'account-b' }, null])('retires old health on a principal switch to %j', async (nextUser) => {
    state.user = { id: 'account-a' };
    const { rerender } = render(<SmartDisplay />);
    await waitFor(() => expect(pttLabel()).toHaveTextContent('listening'));
    let resolveOld;
    healthRequest.mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; }));
    act(() => { void polls[0](); });
    const oldSignal = healthRequest.mock.calls.at(-1)[1].signal;
    healthRequest.mockReturnValue(new Promise(() => {}));
    state.user = nextUser;
    rerender(<SmartDisplay />);
    expect(oldSignal.aborted).toBe(true);
    expect(pttLabel()).toHaveTextContent('status unknown');
    await act(async () => { resolveOld(health(working)); });
    expect(pttLabel()).toHaveTextContent('status unknown');
    healthRequest.mockResolvedValue(health(working));
    await act(async () => { await polls.at(-1)(); });
    expect(pttLabel()).toHaveTextContent('listening');
  });

  it('preserves live health and its poll across same-account metadata refreshes', async () => {
    state.user = { id: 'account-a', user_metadata: { name: 'Before' } };
    const { rerender } = render(<SmartDisplay />);
    await waitFor(() => expect(pttLabel()).toHaveTextContent('listening'));
    const requestCount = healthRequest.mock.calls.length;
    const pollCount = polls.length;
    state.user = { id: 'account-a', user_metadata: { name: 'After' } };
    rerender(<SmartDisplay />);
    expect(pttLabel()).toHaveTextContent('listening');
    expect(healthRequest).toHaveBeenCalledTimes(requestCount);
    expect(polls).toHaveLength(pollCount);
  });

  it('drops old listening health when a poll remains pending into the next interval', async () => {
    render(<SmartDisplay />);
    await waitFor(() => expect(pttLabel()).toHaveTextContent('listening'));
    healthRequest.mockReturnValue(new Promise(() => {}));
    act(() => { void polls[0](); });
    const pendingSignal = healthRequest.mock.calls.at(-1)[1].signal;
    act(() => { void polls[0](); });
    expect(pendingSignal.aborted).toBe(true);
    expect(pttLabel()).toHaveTextContent('status unknown');
  });

  it.each([{ voice_mode: 'disabled' }, { voice_mode: 'wake_word', mic_muted: true }])(
    'does not enable browser mic acquisition for %j, and resumes only after re-enable', async (settings) => {
      state.handsFree = true;
      state.settings = settings;
      const { rerender } = render(<SmartDisplay isSpoke />);
      expect(state.browserOptions.enabled).toBe(false);
      state.settings = { voice_mode: 'wake_word', mic_muted: false };
      rerender(<SmartDisplay isSpoke />);
      expect(state.browserOptions.enabled).toBe(true);
      state.settings = settings;
      rerender(<SmartDisplay isSpoke />);
      expect(state.browserOptions.enabled).toBe(false);
    },
  );

  it('waits for voice settings before enabling opted-in browser capture', () => {
    state.handsFree = true;
    state.loading = true;
    const { rerender } = render(<SmartDisplay isSpoke />);
    expect(state.browserOptions.enabled).toBe(false);
    state.loading = false;
    rerender(<SmartDisplay isSpoke />);
    expect(state.browserOptions.enabled).toBe(true);
  });

  it('associates the live microphone status with the push-to-talk button', async () => {
    render(<SmartDisplay />);
    await waitFor(() => expect(pttLabel()).toHaveTextContent('listening'));
    expect(screen.getByRole('button', { name: 'Push to talk' })).toHaveAccessibleDescription('listening');
    expect(screen.getByRole('status', { name: 'Microphone status' })).toHaveAttribute('aria-live', 'polite');
  });

  it('uses the browser detector signal on a spoke, not the hub microphone health', async () => {
    state.handsFree = true;
    state.browserWake = { status: 'loading' };
    const { rerender } = render(<SmartDisplay isSpoke />);
    await act(async () => {});
    expect(pttLabel()).toHaveTextContent('starting');
    state.browserWake = { status: 'error', error: 'Permission denied' };
    rerender(<SmartDisplay isSpoke />);
    expect(pttLabel()).toHaveTextContent('unavailable');
    state.browserWake = { status: 'listening' };
    rerender(<SmartDisplay isSpoke />);
    expect(pttLabel()).toHaveTextContent('listening');
  });
});
