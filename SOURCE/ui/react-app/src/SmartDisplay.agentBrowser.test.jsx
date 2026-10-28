/**
 * Agent browser view (1.0.5 build 5, A5.2): the view showed for about a
 * second, then React itself called hideBrowserOverlay because a YouTube video
 * was loaded, so Take over / Hand back / Stop were unreachable. Once the task
 * ended the globe pill vanished and Ctrl+K "Open Agent Browser" showed nothing.
 */
import { act, render, screen, waitFor, cleanup, fireEvent } from './test/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SmartDisplay from './SmartDisplay';

const harness = vi.hoisted(() => ({ overlay: null, progress: null, player: null }));

vi.mock('./hooks/usePlayerState', () => ({
  usePlayerState: () => harness.player,
}));
vi.mock('./hooks/useSettings', () => ({ useSettings: () => ({
  settings: { voice_mode: 'push_to_talk' }, loading: false, hasSettingsSnapshot: true, voiceStatus: null,
  updateSetting: vi.fn(), refreshSettings: vi.fn(),
}) }));
vi.mock('./components/auth/cloudSurface', () => ({ isCloudSurface: () => false, isSpokeRoute: () => false }));
vi.mock('./hooks/useAuth', () => ({ useAuth: () => ({ user: null, status: 'signedOut' }) }));
vi.mock('./hooks/useVoiceOnboarding', () => ({ useVoiceOnboarding: () => ({ isActive: false }) }));
vi.mock('./hooks/useHandsFreeWake', () => ({ useHandsFreeWake: () => [false, vi.fn()] }));
vi.mock('./hooks/useBrowserWakeWord', () => ({ useBrowserWakeWord: () => ({ status: 'off' }) }));
vi.mock('./sentryClient', () => ({ syncSentrySettings: vi.fn(), openSentryUserFeedback: vi.fn() }));

function makePlayer() {
  const noop = () => {};
  return {
    is_playing: true,
    now_playing: { title: 'November', artist: 'Tyler', video_id: 'OqqCctAhISQ', provider: 'youtube' },
    queue: [],
    hasQueueSnapshot: true,
    volume: 80,
    position: 10,
    duration: 200,
    position_percentage: 5,
    yt_hub_muted: false,
    hub_local_playback_active: false,
    cef_active: false,
    connected: true,
    connectCount: 1,
    send: vi.fn(),
    setLocalIsPlaying: noop,
    setDiagnosticRequestCallback: noop,
    setErrorCallback: noop,
    setSpokeMessageCallback: noop,
    setOverlayCallback: (cb) => { harness.overlay = cb; },
    setAgentProgressCallback: (cb) => { harness.progress = cb; },
    setDisplayPriorityCallback: noop,
    setAgentFrameCallback: noop,
    setPlaybackCommandCallback: noop,
    setChatResponseCallback: noop,
    setCalendarUpdateCallback: noop,
    setDisconnectCallback: noop,
    getWsDebug: () => ({}),
  };
}

beforeEach(() => {
  harness.player = makePlayer();
  harness.overlay = null;
  window.viola = {
    hideBrowserOverlay: vi.fn(),
    showBrowserOverlay: vi.fn(),
    setBrowserOverlayBounds: vi.fn(),
    openExternalUrl: vi.fn(),
  };
  localStorage.clear();
  vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve({
    ok: true, json: async () => ({ completed: true, settings: {}, data: {}, threads: [] }),
  }));
});
afterEach(() => { cleanup(); delete window.viola; vi.restoreAllMocks(); });

const agentic = (url) => ({
  type: 'browser_overlay_state', visible: true, mode: 'agentic', url,
  agent_task: { description: "today's featured article", status: 'working', phase: 'acting' },
});

describe('agent browser stays up while the agent works', () => {
  it('does not hide the agent view because a video is loaded', async () => {
    render(<SmartDisplay />);
    await waitFor(() => expect(harness.overlay).toBeTypeOf('function'));

    await act(async () => { harness.overlay(agentic('https://en.wikipedia.org/wiki/Main_Page')); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });

    expect(window.viola.hideBrowserOverlay).not.toHaveBeenCalled();
  });

  it('still clears a stale login overlay from over the video', async () => {
    render(<SmartDisplay />);
    await waitFor(() => expect(harness.overlay).toBeTypeOf('function'));

    await act(async () => { harness.overlay({ type: 'browser_overlay_state', visible: true, mode: 'login', url: '' }); });

    await waitFor(() => expect(window.viola.hideBrowserOverlay).toHaveBeenCalled());
  });
});

describe('the agent page can be reopened after the task ends', () => {
  it('keeps the globe pill and shows the native view again when tapped', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<SmartDisplay />);
      await waitFor(() => expect(harness.overlay).toBeTypeOf('function'));
      await act(async () => { harness.overlay(agentic('https://en.wikipedia.org/wiki/Main_Page')); });
      await act(async () => {
        harness.overlay({ type: 'browser_overlay_state', visible: false, mode: 'hidden', url: '', agent_outcome: 'done' });
      });

      // Long after the old 12 s pill cooldown.
      await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
      const pill = screen.getByRole('button', { name: /Open agent browser mode/i });
      fireEvent.click(pill);

      expect(window.viola.showBrowserOverlay).toHaveBeenCalledTimes(1);
      await waitFor(() => expect(screen.getByText('en.wikipedia.org')).toBeInTheDocument());
    } finally {
      vi.useRealTimers();
    }
  });
});
