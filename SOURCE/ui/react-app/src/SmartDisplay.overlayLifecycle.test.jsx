/**
 * Native browser view lifecycle (1.0.5 build 7, A5.2 / A8 / A8.1).
 *
 * On the desktop the agent's browser and the embedded sign-in page are a Qt
 * view laid over this page. Build 7: no Take over / Stop / Exit controls, Esc,
 * the pills and Ctrl+K "Open Music" all left the view over the stage, it drew
 * over the Ctrl+K palette, and the sign-in view's Done did nothing; only a
 * restart removed it. These tests drive SmartDisplay the way the backend
 * broadcasts drive it and check what React tells the desktop shell.
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
// A light Settings with the real modal's keyboard contract: a document-level
// Escape listener that closes it (SettingsModal.jsx "Escape key closes the modal").
vi.mock('./components/SettingsModal', async () => {
  const { useEffect } = await import('react');
  function SettingsStub({ isOpen, onClose }) {
    useEffect(() => {
      if (!isOpen) return undefined;
      const onKey = (event) => { if (event.key === 'Escape') onClose(); };
      document.addEventListener('keydown', onKey);
      return () => document.removeEventListener('keydown', onKey);
    }, [isOpen, onClose]);
    return isOpen ? <div role="dialog" aria-label="Settings" data-testid="settings-modal" /> : null;
  }
  return { default: SettingsStub };
});

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
    setBrowserSurfaceActive: vi.fn(),
    openExternalUrl: vi.fn(),
  };
  localStorage.clear();
  vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve({
    ok: true, json: async () => ({ completed: true, settings: {}, data: {}, threads: [] }),
  }));
});
afterEach(() => { cleanup(); delete window.viola; vi.restoreAllMocks(); });

const WIKI = 'https://en.wikipedia.org/wiki/Main_Page';
const agentic = (url, extra = {}) => ({
  type: 'browser_overlay_state', visible: true, mode: 'agentic', url, agent_busy: true,
  agent_task: { description: "today's featured article", status: 'working', phase: 'acting' },
  ...extra,
});
const hidden = (extra = {}) => ({ type: 'browser_overlay_state', visible: false, mode: 'hidden', url: '', ...extra });
const login = (provider = 'spotify') => ({ type: 'browser_overlay_state', visible: true, mode: 'login', url: provider });

const lastSurfaceActive = () => {
  const calls = window.viola.setBrowserSurfaceActive.mock.calls;
  return calls.length ? calls[calls.length - 1][0] : undefined;
};

async function mount() {
  render(<SmartDisplay />);
  await waitFor(() => expect(harness.overlay).toBeTypeOf('function'));
}

async function send(msg) {
  await act(async () => { harness.overlay(msg); });
}

// The agent browsed, finished (the backend hides the view), then the user
// reopened its page from the globe pill; the backend confirms the reopen.
async function reopenFinishedAgentPage() {
  await send(agentic(WIKI));
  await send(hidden({ agent_outcome: 'done' }));
  fireEvent.click(screen.getByRole('button', { name: /Open agent browser mode/i }));
  expect(window.viola.showBrowserOverlay).toHaveBeenCalledTimes(1);
  await send(agentic(WIKI, { agent_busy: false, agent_task: undefined }));
  return screen.findByRole('button', { name: 'Exit browser mode' });
}

describe('agent browser on the stage', () => {
  it('shows Take over, Stop and Close above the view while the agent drives it', async () => {
    await mount();
    await send(agentic(WIKI));

    expect(await screen.findByRole('button', { name: 'Take over browser' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Stop agent task' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Exit browser mode' })).toBeInTheDocument();
    expect(screen.getByText('Take over')).toBeInTheDocument();
    await waitFor(() => expect(lastSurfaceActive()).toBe(true));
  });

  it('parks the view while the Ctrl+K palette is open, and brings it back after', async () => {
    await mount();
    await send(agentic(WIKI));
    await waitFor(() => expect(lastSurfaceActive()).toBe(true));

    await act(async () => { fireEvent.keyDown(document, { key: 'k', ctrlKey: true }); });
    expect(await screen.findByRole('dialog', { name: 'Command palette' })).toBeInTheDocument();
    await waitFor(() => expect(lastSurfaceActive()).toBe(false));

    await act(async () => { fireEvent.keyDown(screen.getByRole('dialog', { name: 'Command palette' }), { key: 'Escape' }); });
    await waitFor(() => expect(lastSurfaceActive()).toBe(true));
    // Escape went to the palette, not to the browser.
    expect(window.viola.hideBrowserOverlay).not.toHaveBeenCalled();
  });

  it('opens the palette when the desktop forwards Ctrl+K from inside the view', async () => {
    await mount();
    await send(agentic(WIKI));
    await act(async () => { window.dispatchEvent(new CustomEvent('viola:open-command-palette')); });
    expect(await screen.findByRole('dialog', { name: 'Command palette' })).toBeInTheDocument();
    await waitFor(() => expect(lastSurfaceActive()).toBe(false));
  });

  it('Music pill while the agent works parks the view (agent keeps going)', async () => {
    await mount();
    await send(agentic(WIKI));
    await waitFor(() => expect(lastSurfaceActive()).toBe(true));

    fireEvent.click(screen.getByRole('button', { name: /Open music mode/i }));

    await waitFor(() => expect(lastSurfaceActive()).toBe(false));
    expect(window.viola.hideBrowserOverlay).not.toHaveBeenCalled();
  });

  it('a reopened page (agent finished) has no Stop and is closed by the Music pill', async () => {
    await mount();
    expect(await reopenFinishedAgentPage()).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Stop agent task' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Open music mode/i }));
    await waitFor(() => expect(window.viola.hideBrowserOverlay).toHaveBeenCalledTimes(1));
  });

  it('Close closes a reopened page', async () => {
    await mount();
    fireEvent.click(await reopenFinishedAgentPage());
    await waitFor(() => expect(window.viola.hideBrowserOverlay).toHaveBeenCalledTimes(1));
  });

  it('Esc closes a reopened page', async () => {
    await mount();
    await reopenFinishedAgentPage();

    await act(async () => { fireEvent.keyDown(document.body, { key: 'Escape' }); });
    await waitFor(() => expect(window.viola.hideBrowserOverlay).toHaveBeenCalledTimes(1));
  });

  it('Esc forwarded from inside the view leaves the stage', async () => {
    await mount();
    await reopenFinishedAgentPage();

    await act(async () => { window.dispatchEvent(new CustomEvent('viola:browser-exit')); });
    await waitFor(() => expect(window.viola.hideBrowserOverlay).toHaveBeenCalledTimes(1));
  });

  it('the palette lists Close browser while the browser stage is up', async () => {
    await mount();
    await send(agentic(WIKI));
    await screen.findByRole('button', { name: 'Exit browser mode' });

    await act(async () => { fireEvent.keyDown(document, { key: 'k', ctrlKey: true }); });
    expect(await screen.findByText('Close browser')).toBeInTheDocument();
  });
});

describe('embedded sign-in page', () => {
  it('gets the browser stage with a Done button that closes it', async () => {
    await mount();
    await send(login('spotify'));

    const done = await screen.findByRole('button', { name: 'Done signing in to Spotify' });
    expect(screen.getByText('Sign in to Spotify')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Take over browser' })).not.toBeInTheDocument();
    await waitFor(() => expect(lastSurfaceActive()).toBe(true));

    fireEvent.click(done);
    await waitFor(() => expect(window.viola.hideBrowserOverlay).toHaveBeenCalledTimes(1));
  });

  it('is not closed because a video is loaded (it never covers the video)', async () => {
    await mount();
    await send(login('spotify'));
    await screen.findByRole('button', { name: 'Done signing in to Spotify' });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    expect(window.viola.hideBrowserOverlay).not.toHaveBeenCalled();
  });

  it('Esc closes it', async () => {
    await mount();
    await send(login('spotify'));
    await screen.findByRole('button', { name: 'Done signing in to Spotify' });

    await act(async () => { fireEvent.keyDown(document.body, { key: 'Escape' }); });
    await waitFor(() => expect(window.viola.hideBrowserOverlay).toHaveBeenCalledTimes(1));
  });

  it('another pill closes it', async () => {
    await mount();
    await send(login('spotify'));
    await screen.findByRole('button', { name: 'Done signing in to Spotify' });

    fireEvent.click(screen.getByRole('button', { name: /Open chat mode/i }));
    await waitFor(() => expect(window.viola.hideBrowserOverlay).toHaveBeenCalledTimes(1));
  });

  it('leaves the stage when the backend says the view is gone', async () => {
    await mount();
    await send(login('spotify'));
    await screen.findByRole('button', { name: 'Done signing in to Spotify' });

    await send(hidden());
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Done signing in to Spotify' })).not.toBeInTheDocument());
    await waitFor(() => expect(lastSurfaceActive()).toBe(false));
  });
});

describe('sign-in started from Settings (Settings steps aside)', () => {
  async function openSettingsThenSignIn() {
    await mount();
    await act(async () => {
      window.dispatchEvent(new CustomEvent('viola:ui-action', { detail: { action: 'open_settings', payload: {} } }));
    });
    await screen.findByTestId('settings-modal-host', {}, { timeout: 10000 });
    await send(login('spotify'));
    await screen.findByRole('button', { name: 'Done signing in to Spotify' });
    await waitFor(() => expect(screen.getByTestId('settings-modal-host')).toHaveAttribute('data-suspended', 'true'));
  }

  it('Ctrl+K opens the palette over the sign-in page', async () => {
    await openSettingsThenSignIn();

    await act(async () => { fireEvent.keyDown(document.body, { key: 'k', ctrlKey: true }); });

    expect(await screen.findByRole('dialog', { name: 'Command palette' })).toBeInTheDocument();
  });

  it('Esc closes only the palette; Settings stays for after the sign-in', async () => {
    await openSettingsThenSignIn();
    await act(async () => { fireEvent.keyDown(document.body, { key: 'k', ctrlKey: true }); });
    const palette = await screen.findByRole('dialog', { name: 'Command palette' });

    await act(async () => { fireEvent.keyDown(palette, { key: 'Escape' }); });

    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Command palette' })).not.toBeInTheDocument());
    expect(screen.getByTestId('settings-modal-host')).toHaveAttribute('data-suspended', 'true');
    expect(window.viola.hideBrowserOverlay).not.toHaveBeenCalled();

    // The sign-in closes: the user is back in Settings, not on the music stage.
    await send(hidden());
    await waitFor(() => expect(screen.getByTestId('settings-modal-host')).not.toHaveAttribute('data-suspended'));
    expect(screen.getByTestId('settings-modal')).toBeInTheDocument();
  });
});
