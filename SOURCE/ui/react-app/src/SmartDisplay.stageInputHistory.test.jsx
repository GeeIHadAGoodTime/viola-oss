/**
 * 1.0.5 build-7 laptop report:
 *  - Ctrl+V into the stage text box was ignored (the type-anywhere box has no
 *    real text field, so the paste went nowhere).
 *  - Chat History lost earlier turns across a restart (A10.4, A11.6): the
 *    list was only what this launch had added.
 *  - The multiroom speaker page kept an old reply on screen (A7.3-A7.6).
 */
import { act, render, screen, waitFor, cleanup } from './test/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SmartDisplay, { IDLE_RESPONSE_LINE, SPOKE_REPLY_HOLD_MS, stagePasteText } from './SmartDisplay';

const harness = vi.hoisted(() => ({ player: null, chatResponse: null }));

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
    is_playing: false,
    now_playing: null,
    queue: [],
    hasQueueSnapshot: true,
    volume: 80,
    position: 0,
    duration: 0,
    position_percentage: 0,
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
    setOverlayCallback: noop,
    setAgentProgressCallback: noop,
    setDisplayPriorityCallback: noop,
    setAgentFrameCallback: noop,
    setPlaybackCommandCallback: noop,
    setChatResponseCallback: (cb) => { harness.chatResponse = cb; },
    setCalendarUpdateCallback: noop,
    setDisconnectCallback: noop,
    getWsDebug: () => ({}),
  };
}

const requested = [];

beforeEach(() => {
  harness.player = makePlayer();
  requested.length = 0;
  localStorage.clear();
  vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
    requested.push(String(url));
    const body = String(url).includes('/v1/chat/recent-activity')
      ? { ok: true, data: { items: [
        { role: 'user', content: 'what is my favorite color', timestamp: '2026-10-30T14:59:00Z' },
        { role: 'assistant', content: 'Your favorite color is teal.', timestamp: '2026-10-30T14:59:04Z' },
      ] } }
      : { completed: true, settings: {}, data: {}, threads: [] };
    return Promise.resolve({ ok: true, status: 200, json: async () => body, headers: new Headers() });
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function pasteOnStage(text) {
  const event = new Event('paste', { bubbles: true, cancelable: true });
  event.clipboardData = { getData: (type) => (type === 'text/plain' || type === 'text' ? text : '') };
  act(() => { document.body.dispatchEvent(event); });
  return event;
}

describe('stage paste', () => {
  it('puts pasted text into the stage text box', async () => {
    render(<SmartDisplay />);
    const event = pasteOnStage('play some jazz');
    expect(event.defaultPrevented).toBe(true);
    await waitFor(() => expect(screen.getByText('play some jazz')).toBeInTheDocument());
  });

  it('turns pasted line breaks into spaces', () => {
    expect(stagePasteText({ getData: () => 'play\r\nsome\tjazz' })).toBe('play some jazz');
    expect(stagePasteText({ getData: () => '' })).toBe('');
    expect(stagePasteText(null)).toBe('');
  });
});

describe('Chat History across a restart', () => {
  it('reads the saved recent turns when the stage starts', async () => {
    render(<SmartDisplay />);
    await waitFor(() => expect(requested.some((url) => url.includes('/v1/chat/recent-activity'))).toBe(true));
  });

  it('does not read them on a speaker page', async () => {
    render(<SmartDisplay isSpoke />);
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)); });
    expect(requested.some((url) => url.includes('/v1/chat/recent-activity'))).toBe(false);
  });
});

describe('speaker page reply line (A7.3-A7.6)', () => {
  it('goes back to the idle line after a while instead of keeping an old reply', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<SmartDisplay isSpoke />);
      await waitFor(() => expect(harness.chatResponse).toBeTypeOf('function'));
      await act(async () => { harness.chatResponse({ text: 'Playing relaxing jazz.' }); });
      expect(screen.getByText(/Playing relaxing jazz\./)).toBeInTheDocument();

      await act(async () => { await vi.advanceTimersByTimeAsync(SPOKE_REPLY_HOLD_MS + 100); });

      expect(screen.queryByText(/Playing relaxing jazz\./)).not.toBeInTheDocument();
      expect(screen.getByText(IDLE_RESPONSE_LINE.replaceAll('"', ''), { exact: false })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('drops the shown reply when its socket reconnects (replies sent meanwhile were missed)', async () => {
    const { rerender } = render(<SmartDisplay isSpoke />);
    await waitFor(() => expect(harness.chatResponse).toBeTypeOf('function'));
    await act(async () => { harness.chatResponse({ text: 'Account required.' }); });
    expect(screen.getByText(/Account required\./)).toBeInTheDocument();

    harness.player = { ...harness.player, connectCount: 2 };
    rerender(<SmartDisplay isSpoke />);

    await waitFor(() => expect(screen.queryByText(/Account required\./)).not.toBeInTheDocument());
  });

  it('keeps the reply on the main stage (not a speaker page)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<SmartDisplay />);
      await waitFor(() => expect(harness.chatResponse).toBeTypeOf('function'));
      await act(async () => { harness.chatResponse({ text: 'Heads.' }); });
      await act(async () => { await vi.advanceTimersByTimeAsync(SPOKE_REPLY_HOLD_MS + 100); });
      expect(screen.getByText(/Heads\./)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});
