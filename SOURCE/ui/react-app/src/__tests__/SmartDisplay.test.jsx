/**
 * Smoke tests for SmartDisplay.
 *
 * SmartDisplay is a 3,600+ line monolith, so these tests verify it can mount
 * without crashing and renders essential UI landmarks. Deep interaction tests
 * live in __tests__/integration/.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '../test/test-utils';

const wsHarness = vi.hoisted(() => ({
  handler: null,
  handlers: [],
  // Stable across re-renders so a test can assert what SmartDisplay sent
  // to the backend (`useWebSocket` is called on every render).
  sent: [],
}));

const playerHarness = vi.hoisted(() => ({
  overlayCallback: null,
  progressCallback: null,
  state: null,
}));

// Records the `enabled` flag the dedicated agent-browser stream hook was last
// called with, so a test can assert SmartDisplay turns it on for the cloud
// browser stage without opening a real WebSocket.
const agentBrowserStreamHarness = vi.hoisted(() => ({ calls: [] }));
const apiHarness = vi.hoisted(() => ({
  submitBugReport: null,
  setShuffle: vi.fn(() => Promise.resolve({})),
  // Transport calls a test needs to fail on demand. Every one of these is
  // `apiFetch` in production, which ALWAYS returns a promise -- so the mocks
  // must too, or a component that legitimately attaches `.catch()` blows up on
  // `undefined.catch` in tests while working fine in the real app.
  //
  // Created eagerly, unlike the `x || (x = vi.fn())` fields above: those only
  // come into existence when `useViolaApi()` is first CALLED, so a test that
  // arms one before anything has rendered reads null and dies -- which happens
  // to any test run in isolation (`vitest -t ...`) rather than after its
  // file-mates.
  skip: vi.fn(() => Promise.resolve({})),
  previous: vi.fn(() => Promise.resolve({})),
  setVolume: vi.fn(() => Promise.resolve({})),
  seek: vi.fn(() => Promise.resolve({})),
  setRating: vi.fn(() => Promise.resolve({})),
}));
const sentryHarness = vi.hoisted(() => ({
  openSentryUserFeedback: vi.fn(() => Promise.resolve(true)),
  syncSentrySettings: vi.fn(),
}));
const onboardingHarness = vi.hoisted(() => ({
  useVoiceOnboarding: vi.fn(),
}));
const settingsHarness = vi.hoisted(() => ({
  useSettings: vi.fn(),
  updateSetting: vi.fn(),
}));

// Mock the hooks that SmartDisplay depends on so we control their return values
// without needing a real backend.
vi.mock('../hooks/usePlayerState', () => ({
  usePlayerState: () => ({
    is_playing: false,
    now_playing: null,
    queue: [],
    volume: 80,
    position: 0,
    duration: 0,
    position_percentage: 0,
    yt_hub_muted: false,
    hub_local_playback_active: false,
    cef_active: false,
    connected: true,
    ...(playerHarness.state || {}),
    // SmartDisplay's `wsSend` is this `send` (see SmartDisplay.jsx:434), so
    // recording here is how a test sees what it told the backend.
    send: vi.fn((message) => { wsHarness.sent.push(message); }),
    connectCount: 0,
    setLocalIsPlaying: vi.fn(),
    setDiagnosticRequestCallback: vi.fn(),
    setErrorCallback: vi.fn(),
    setSpokeMessageCallback: vi.fn(),
    setOverlayCallback: vi.fn((callback) => { playerHarness.overlayCallback = callback; }),
    setAgentProgressCallback: vi.fn((callback) => { playerHarness.progressCallback = callback; }),
    setDisplayPriorityCallback: vi.fn(),
    setAgentFrameCallback: vi.fn(),
    setPlaybackCommandCallback: vi.fn(),
    setChatResponseCallback: vi.fn(),
    setCalendarUpdateCallback: vi.fn(),
    setDisconnectCallback: vi.fn(),
    getWsDebug: vi.fn(() => ({})),
  }),
}));

vi.mock('../hooks/useViolaApi', () => ({
  authFetch: vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({}) })),
  apiFetch: vi.fn((url, options = {}) => {
    // Transport tests model a returning user. An empty response makes the real
    // welcome hook open a first-visit modal after 400 ms and hide the controls.
    if (url === '/v1/cloud-welcome/status') {
      return Promise.resolve({ completed: true });
    }
    if (url === '/v1/chat/models') {
      return Promise.resolve({ current_model: 'gpt-test', provider: 'test', providers: [] });
    }
    if (url.startsWith('/v1/chat/threads') && options.method === 'POST') {
      return Promise.resolve({
        thread: {
          id: 'thread-1',
          title: 'New chat',
          updated_at: '2026-05-11T12:00:00Z',
        },
        messages: [],
      });
    }
    if (url === '/v1/chat/threads' || url.startsWith('/v1/chat/threads?')) {
      return Promise.resolve({ threads: [] });
    }
    if (url.startsWith('/v1/chat/threads/')) {
      return Promise.resolve({
        thread: {
          id: 'thread-1',
          title: 'New chat',
          updated_at: '2026-05-11T12:00:00Z',
        },
        messages: [],
      });
    }
    return Promise.resolve({});
  }),
  useViolaApi: () => ({
    getState: vi.fn(() => Promise.resolve({})),
    play: vi.fn(),
    pause: vi.fn(),
    resume: vi.fn(),
    stop: vi.fn(),
    skip: apiHarness.skip,
    next: vi.fn(() => Promise.resolve({})),
    previous: apiHarness.previous,
    seek: apiHarness.seek,
    setVolume: apiHarness.setVolume,
    setRating: apiHarness.setRating,
    setRepeat: vi.fn(() => Promise.resolve({})),
    getQueue: vi.fn(() => Promise.resolve([])),
    clearQueue: vi.fn(),
    sendCommand: vi.fn(() => Promise.resolve({ message: 'ok' })),
    getWeather: vi.fn(() => Promise.resolve({})),
    submitBugReport: apiHarness.submitBugReport || (apiHarness.submitBugReport = vi.fn(() => Promise.resolve({ bug_ticket_id: 42 }))),
    setShuffle: apiHarness.setShuffle,
    getCalendarEvents: vi.fn(() => Promise.resolve([])),
    getCalendarStatus: vi.fn(() => Promise.resolve({})),
    getCalendarNextEvent: vi.fn(() => Promise.resolve(null)),
  }),
}));

vi.mock('../hooks/useWebSocket', () => ({
  useWebSocket: vi.fn((handler) => {
    if (!wsHarness.handler) wsHarness.handler = handler;
    wsHarness.handlers.push(handler);
    return {
      wsRef: { current: null },
      send: vi.fn((message) => { wsHarness.sent.push(message); }),
      connectCount: 0,
      setBinaryCallback: vi.fn(),
      setDisconnectCallback: vi.fn(),
      getWsDebug: vi.fn(() => ({})),
    };
  }),
}));

vi.mock('../hooks/useAgentBrowserStream', () => ({
  useAgentBrowserStream: vi.fn(({ enabled } = {}) => {
    agentBrowserStreamHarness.calls.push(enabled);
    return {
      frameSrc: null,
      frameFade: null,
      ready: false,
      sessionId: null,
      viewerId: null,
      frameFormat: null,
      status: 'idle',
      streamError: null,
      inputBlocked: false,
      sendInput: vi.fn(),
    };
  }),
}));

vi.mock('../hooks/useVoice', () => ({
  useVoice: () => ({
    isListening: false,
    transcript: '',
    startListening: vi.fn(),
    stopListening: vi.fn(),
  }),
}));

vi.mock('../hooks/useCallAudio', () => ({
  default: vi.fn(() => ({
    isListening: false,
    startListening: vi.fn(),
    stopListening: vi.fn(),
    takeoverActive: false,
    startTakeover: vi.fn(),
    releaseTakeover: vi.fn(),
    transcripts: [],
    latestCostUsd: null,
    recipientState: null,
    sendOperatorMessage: vi.fn(),
    endCall: vi.fn(),
  })),
  fetchCallHistory: vi.fn(() => Promise.resolve({
    count: 1,
    calls: [{
      call_id: 'call-ended',
      phone_number: '+1 555 0100',
      status: 'completed',
      duration_seconds: 154,
      started_at: '2026-05-09T17:00:00Z',
      summary: 'Appointment set for Tuesday.',
    }],
  })),
  getCallTranscript: vi.fn(() => Promise.resolve({
    call_id: 'call-ended',
    transcript: [{ role: 'viola', text: 'The appointment is confirmed.', ts: '1' }],
  })),
  fetchCallQueue: vi.fn(() => Promise.resolve([])),
  fetchActiveCall: vi.fn(() => Promise.resolve(null)),
  removeQueuedCall: vi.fn(() => Promise.resolve({ queue: [] })),
}));

vi.mock('../hooks/useSettings', () => ({
  useSettings: settingsHarness.useSettings,
}));

// SmartDisplay reads the signed-in account identity via useAuth (issue #772);
// stub it signed-out so these render tests don't require an AuthProvider.
vi.mock('../hooks/useAuth', () => ({
  useAuth: () => ({ user: null, status: 'signedOut' }),
}));

function mockSettingsState() {
  return {
    settings: {},
    updateSetting: settingsHarness.updateSetting,
    loading: false,
  };
}

vi.mock('../sentryClient', () => ({
  openSentryUserFeedback: sentryHarness.openSentryUserFeedback,
  syncSentrySettings: sentryHarness.syncSentrySettings,
}));

vi.mock('../hooks/useAvailableRooms', () => ({
  useAvailableRooms: () => ({
    rooms: [],
    loading: false,
  }),
}));

vi.mock('../hooks/useVoiceOnboarding', () => ({
  useVoiceOnboarding: onboardingHarness.useVoiceOnboarding,
}));

function mockOnboardingState() {
  return {
    step: null,
    isActive: false,
    isOnboarding: false,
    phase: 'account_pair',
    phaseIndex: 0,
    totalPhases: 4,
    phaseContent: '',
    isSpeaking: false,
    canSkip: false,
    activeHighlight: null,
    isAccountPairPhase: false,
    accountPair: {},
    isMicPermissionPhase: false,
    micPermission: {},
    isCloudConsentPhase: false,
    cloudConsentStatus: 'idle',
    cloudConsentError: null,
    suggestions: null,
    tryCommandStatus: 'idle',
    advance: vi.fn(),
    dismiss: vi.fn(),
    pauseOnboarding: vi.fn(),
    resumeOnboarding: vi.fn(),
    onCommandExecuted: vi.fn(),
    onMicTestResult: vi.fn(),
    onAccountPairRetry: vi.fn(),
    onMicPermissionRetry: vi.fn(),
    onCloudConsentChoice: vi.fn(),
    onByokSetupDone: vi.fn(),
    skipOnboarding: vi.fn(),
    onSuggestionTap: vi.fn(),
  };
}

// Mock lazy-loaded modals to prevent dynamic import issues in tests
vi.mock('../components/SettingsModal', () => ({
  default: ({ initialTab, initialSection }) => (
    <div data-testid="settings-modal">Settings {initialTab || ''} {initialSection || ''}</div>
  ),
}));
vi.mock('../components/QueueModal', () => ({
  default: () => <div data-testid="queue-modal">Queue</div>,
}));
vi.mock('../components/HistoryModal', () => ({
  default: () => <div data-testid="history-modal">History</div>,
}));
vi.mock('../components/RoomGroupsModal', () => ({
  default: ({ initialTab, prefill }) => (
    <div data-testid="room-groups-modal">
      Room Groups {initialTab || ''} {prefill?.room_name || prefill?.target_room || ''}
    </div>
  ),
}));
vi.mock('../components/HelpModal', () => ({
  default: () => <div data-testid="help-modal">Help</div>,
}));
vi.mock('../components/CalendarView', () => ({
  default: () => <div data-testid="calendar-view">Calendar</div>,
}));

// Dynamically import SmartDisplay AFTER mocks are set up
let SmartDisplay;
let agentContextPillCooldownMs;

beforeEach(async () => {
  wsHarness.handler = null;
  wsHarness.handlers = [];
  wsHarness.sent = [];
  playerHarness.overlayCallback = null;
  playerHarness.progressCallback = null;
  playerHarness.state = null;
  agentBrowserStreamHarness.calls = [];
  apiHarness.submitBugReport?.mockClear?.();
  apiHarness.submitBugReport?.mockResolvedValue?.({ bug_ticket_id: 42 });
  apiHarness.setShuffle.mockReset();
  apiHarness.setShuffle.mockResolvedValue({});
  for (const name of ['skip', 'previous', 'setVolume', 'seek', 'setRating']) {
    apiHarness[name]?.mockClear?.();
    apiHarness[name]?.mockResolvedValue?.({});
  }
  onboardingHarness.useVoiceOnboarding.mockReset();
  onboardingHarness.useVoiceOnboarding.mockImplementation(() => mockOnboardingState());
  settingsHarness.useSettings.mockReset();
  settingsHarness.useSettings.mockImplementation(() => mockSettingsState());
  settingsHarness.updateSetting.mockClear();
  sentryHarness.openSentryUserFeedback.mockReset();
  sentryHarness.openSentryUserFeedback.mockResolvedValue(true);
  sentryHarness.syncSentrySettings.mockClear();
  const callAudioMod = await import('../hooks/useCallAudio');
  callAudioMod.fetchActiveCall.mockReset();
  callAudioMod.fetchActiveCall.mockResolvedValue(null);
  const mod = await import('../SmartDisplay');
  SmartDisplay = mod.default;
  agentContextPillCooldownMs = mod.AGENT_CONTEXT_PILL_COOLDOWN_MS;
});

describe('SmartDisplay', () => {
  it('should render without crashing', () => {
    expect(() => render(<SmartDisplay />)).not.toThrow();
  });

  it('should render the main display container', () => {
    const { container } = render(<SmartDisplay />);
    // SmartDisplay renders at least one div
    expect(container.firstChild).toBeTruthy();
  });

  it('does not pass the spoke surface into data-loading hooks when rendered as a multiroom spoke', () => {
    render(<SmartDisplay isSpoke room="kitchen" />);

    expect(onboardingHarness.useVoiceOnboarding.mock.calls[0]).toEqual([]);
    expect(settingsHarness.useSettings.mock.calls[0]).toEqual([]);
  });

  it('keeps hub stage modes available on the multiroom spoke surface', () => {
    render(<SmartDisplay isSpoke room="kitchen" />);

    expect(screen.getByRole('button', { name: 'Open music mode' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Open chat mode' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Open phone mode/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Open memory and Workbench' })).toBeInTheDocument();
  });

  it('shows the paid-action sign-in modal on a multiroom spoke', async () => {
    render(<SmartDisplay isSpoke room="kitchen" />);

    act(() => {
      window.dispatchEvent(new CustomEvent('viola:paid-action-login-required', {
        detail: {
          error_code: 'login_required_for_paid_action',
          message: 'Sign in to make phone calls.',
        },
      }));
    });

    expect(await screen.findByText('Sign in required')).toBeInTheDocument();
    expect(screen.getByText('Sign in to continue')).toBeInTheDocument();
  });

  it('should show idle state when nothing is playing', async () => {
    render(<SmartDisplay />);
    // When nothing is playing, SmartDisplay shows a clock or idle view.
    // Just verify the component mounted and has content.
    await waitFor(() => {
      expect(document.body.textContent.length).toBeGreaterThan(0);
    });
  });

  it('opens Sentry user feedback from the topbar affordance', async () => {
    const { user } = render(<SmartDisplay />);

    await user.click(screen.getByRole('button', { name: 'Report a bug' }));

    await waitFor(() => expect(sentryHarness.openSentryUserFeedback).toHaveBeenCalledTimes(1));
    expect(apiHarness.submitBugReport).not.toHaveBeenCalled();
    expect(screen.queryByLabelText('What broke?')).not.toBeInTheDocument();
    const [context] = sentryHarness.openSentryUserFeedback.mock.calls[0];
    expect(context).toMatchObject({
      source: 'react_topbar',
      ui_entrypoint: 'react_topbar',
      recent_action: {
        kind: 'ui_stage',
        display_mode: 'now_playing',
        stage_mode: 'music',
      },
      screen_capture_metadata: {
        requested: false,
        provided: false,
        storage: 'metadata_only',
      },
    });
    expect(context.current_url).toEqual(expect.any(String));
    expect(context.viewport).toEqual(expect.objectContaining({
      width: expect.any(Number),
      height: expect.any(Number),
    }));
  });

  it('submits a bug report from the topbar affordance with screen context', async () => {
    sentryHarness.openSentryUserFeedback.mockResolvedValueOnce(false);
    const { user } = render(<SmartDisplay />);

    await user.click(screen.getByRole('button', { name: 'Report a bug' }));
    // fireEvent.change (not userEvent.type): sets the field's value in ONE
    // synthetic event, so it is immune to the per-keystroke dropped-character
    // race userEvent.type hits when the event loop is starved under
    // concurrent-hook CPU contention (#3583 follow-up -- reproduced live on
    // the CI runner: "Music keeplaying" instead of "Music keeps playing",
    // characters dropped mid-word). The component's onChange reads
    // event.target.value normally either way, so this asserts the exact same
    // consented-field contract, just without the flake.
    fireEvent.change(await screen.findByLabelText('What broke?'), {
      target: { value: 'Playback stopped after I clicked play' },
    });
    await user.click(screen.getByRole('button', { name: 'Submit report' }));

    await waitFor(() => expect(apiHarness.submitBugReport).toHaveBeenCalledTimes(1));
    const [message, context] = apiHarness.submitBugReport.mock.calls[0];
    expect(message).toBe('Playback stopped after I clicked play');
    expect(context).toMatchObject({
      source: 'react_topbar',
      ui_entrypoint: 'react_topbar',
      recent_action: {
        kind: 'ui_stage',
        display_mode: 'now_playing',
        stage_mode: 'music',
      },
      screen_capture_metadata: {
        requested: true,
        provided: false,
        storage: 'metadata_only',
      },
    });
  });

  it('carries the user-consented repro/contact/version fields the backend allowlists', async () => {
    // Contract for the enhanced bug-report form: every consented field the user
    // typed or was shown (steps/expected/actual/contact + app_version/os) must
    // reach api.submitBugReport under the closed keys the backend's
    // WebBugReportContext accepts. If a future edit drops a field or renames a
    // key, this fails -- the form silently under-reporting is exactly the gap.
    sentryHarness.openSentryUserFeedback.mockResolvedValueOnce(false);
    const { user } = render(<SmartDisplay />);

    await user.click(screen.getByRole('button', { name: 'Report a bug' }));
    // fireEvent.change, not userEvent.type -- see the comment on the first
    // bug-report test above (dropped-keystroke race under CPU contention).
    fireEvent.change(await screen.findByLabelText('What broke?'), {
      target: { value: 'Music stops after ten seconds every time' },
    });
    fireEvent.change(screen.getByLabelText(/What were you doing/), {
      target: { value: 'Playing a playlist from the topbar' },
    });
    fireEvent.change(screen.getByLabelText(/What did you expect/), {
      target: { value: 'Music keeps playing' },
    });
    fireEvent.change(screen.getByLabelText(/What actually happened/), {
      target: { value: 'It cuts out at 0:10' },
    });
    fireEvent.change(screen.getByLabelText(/Email/), {
      target: { value: 'reporter@example.com' },
    });
    await user.click(screen.getByRole('button', { name: 'Submit report' }));

    await waitFor(() => expect(apiHarness.submitBugReport).toHaveBeenCalledTimes(1));
    const [, context] = apiHarness.submitBugReport.mock.calls[0];
    // The closed, user-consented set the backend WebBugReportContext allowlists.
    expect(context.steps).toBe('Playing a playlist from the topbar');
    expect(context.expected).toBe('Music keeps playing');
    expect(context.actual).toBe('It cuts out at 0:10');
    expect(context.contact).toBe('reporter@example.com');
    // app_version/os are shown-then-sent: string keys always present (may be
    // empty when the browser/build cannot determine them), never omitted.
    expect(typeof context.app_version).toBe('string');
    expect(typeof context.os).toBe('string');
    // surface is the origin the report started on (allowlisted too).
    expect(typeof context.surface).toBe('string');
    expect(context.surface.length).toBeGreaterThan(0);
  });

  it('leaves the optional repro/contact fields empty when the user skips them', async () => {
    // Anonymous-by-default is preserved: skipping the optional fields sends
    // empty strings (not fabricated values), exactly like the pre-enhancement
    // form. The backend treats empty as absent.
    sentryHarness.openSentryUserFeedback.mockResolvedValueOnce(false);
    const { user } = render(<SmartDisplay />);

    await user.click(screen.getByRole('button', { name: 'Report a bug' }));
    // fireEvent.change, not userEvent.type -- see the comment on the first
    // bug-report test above (dropped-keystroke race under CPU contention).
    fireEvent.change(await screen.findByLabelText('What broke?'), {
      target: { value: 'A short but complete bug description' },
    });
    await user.click(screen.getByRole('button', { name: 'Submit report' }));

    await waitFor(() => expect(apiHarness.submitBugReport).toHaveBeenCalledTimes(1));
    const [, context] = apiHarness.submitBugReport.mock.calls[0];
    expect(context.contact).toBe('');
    expect(context.steps).toBe('');
    expect(context.expected).toBe('');
    expect(context.actual).toBe('');
  });

  it('opens settings from a ui_action event', async () => {
    render(<SmartDisplay />);

    act(() => {
      window.dispatchEvent(new CustomEvent('viola:ui-action', {
        detail: { action: 'open_settings', payload: { tab: 'services' } },
      }));
    });

    expect(await screen.findByTestId('settings-modal')).toHaveTextContent('connections');
  });

  // Regression test for #1422: the ui_action-event route above was covered,
  // but the actual user-facing path -- opening the main menu and clicking
  // the Settings item (DropdownMenu -> TopBar -> SmartDisplay) -- had zero
  // coverage, which is exactly why this could regress silently.
  it('opens SettingsModal by clicking Settings in the main menu (#1422)', async () => {
    const { user } = render(<SmartDisplay />);

    expect(screen.queryByTestId('settings-modal')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Open menu' }));
    expect(screen.getByRole('menu', { name: 'Navigation menu' })).toBeInTheDocument();

    await user.click(screen.getByRole('menuitem', { name: 'Settings' }));

    expect(await screen.findByTestId('settings-modal')).toBeInTheDocument();
    // The menu closes once an item is chosen.
    expect(screen.queryByRole('menu', { name: 'Navigation menu' })).not.toBeInTheDocument();
  });

  it('opens Rooms Add Speaker with prefill from a ui_action event', async () => {
    render(<SmartDisplay />);

    act(() => {
      window.dispatchEvent(new CustomEvent('viola:ui-action', {
        detail: {
          action: 'open_rooms_add_speaker',
          payload: { rooms_modal_tab: 'add-speaker', room_name: 'kitchen' },
        },
      }));
    });

    const modal = await screen.findByTestId('room-groups-modal');
    expect(modal).toHaveTextContent('add-speaker');
    expect(modal).toHaveTextContent('kitchen');
  });

  it('opens the calendar view from a ui_action event', async () => {
    render(<SmartDisplay />);

    act(() => {
      window.dispatchEvent(new CustomEvent('viola:ui-action', {
        detail: { action: 'open_calendar', payload: {} },
      }));
    });

    expect(await screen.findByTestId('calendar-view-panel')).toBeInTheDocument();
  });

  it('renders the chat stage pill', () => {
    render(<SmartDisplay />);

    expect(screen.getByTestId('stage-pill-chat')).toHaveAccessibleName('Open chat mode');
  });

  it('leads the pinned stage pills with Music (founder ruling 2026-07-14, #1528)', () => {
    render(<SmartDisplay />);

    const pillBar = screen.getByTestId('stage-pill-bar');
    const pinnedPillIds = within(pillBar)
      .getAllByRole('button')
      .map((button) => button.dataset.testid)
      .filter((testid) => testid && testid.startsWith('stage-pill-'));

    expect(pinnedPillIds).toEqual(['stage-pill-music', 'stage-pill-phone', 'stage-pill-chat']);
  });

  it('switches stage modes from the pinned pills', async () => {
    const { user } = render(<SmartDisplay />);

    await user.click(screen.getByTestId('stage-pill-chat'));
    expect(await screen.findByTestId('chat-mode')).toBeInTheDocument();

    await user.click(screen.getByTestId('stage-pill-music'));
    expect(await screen.findByTestId('now-playing')).toBeInTheDocument();

    await user.click(screen.getByTestId('stage-pill-phone'));
    expect(await screen.findByTestId('call-history-list')).toBeInTheDocument();

    await user.click(screen.getByTestId('stage-pill-phone'));
    expect(await screen.findByTestId('now-playing')).toBeInTheDocument();
  });

  it('shows embedded YouTube playback on the idle music stage when music starts', async () => {
    const { rerender } = render(<SmartDisplay />);

    playerHarness.state = {
      is_playing: true,
      now_playing: {
        id: 'yt-track-1',
        title: 'Embedded YouTube Track',
        artist: 'YouTube',
        provider: 'youtube_iframe',
        source: 'ytsearch1',
        playback_mode: 'embedded_iframe_webview',
        video_id: 'abc123',
      },
      position: 0,
      position_ms: 0,
      duration: 0,
      position_percentage: 0,
    };
    rerender(<SmartDisplay />);

    expect(await screen.findByTestId('now-playing')).toBeInTheDocument();
    expect(screen.getByTitle('YouTube video player')).toBeInTheDocument();
  });

  it('keeps embedded YouTube playback mounted without stealing a selected stage', async () => {
    playerHarness.state = {
      is_playing: true,
      now_playing: {
        id: 'yt-track-1',
        title: 'Embedded YouTube Track',
        artist: 'YouTube',
        provider: 'youtube_iframe',
        source: 'ytsearch1',
        playback_mode: 'embedded_iframe_webview',
        video_id: 'abc123',
      },
      position: 0,
      position_ms: 0,
      duration: 0,
      position_percentage: 0,
    };
    const { user, rerender } = render(<SmartDisplay />);

    expect(await screen.findByTestId('now-playing')).toBeInTheDocument();
    const musicPanel = screen.getByTestId('stage-mode-panel-music');
    const playbackFrame = within(musicPanel).getByTitle('YouTube video player');

    await user.click(screen.getByTestId('stage-pill-chat'));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    await waitFor(() => expect(screen.getByTestId('viola-stage')).toHaveAttribute('data-stage-mode', 'chat'));
    expect(screen.getByTestId('chat-mode')).toBeInTheDocument();
    expect(musicPanel).toHaveAttribute('aria-hidden', 'true');
    expect(within(musicPanel).getByTitle('YouTube video player')).toBe(playbackFrame);

    rerender(<SmartDisplay />);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    await waitFor(() => expect(screen.getByTestId('viola-stage')).toHaveAttribute('data-stage-mode', 'chat'));
    expect(within(musicPanel).getByTitle('YouTube video player')).toBe(playbackFrame);

    await user.click(screen.getByTestId('stage-pill-phone'));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    await waitFor(() => expect(screen.getByTestId('viola-stage')).toHaveAttribute('data-stage-mode', 'phone'));
    expect(screen.getByTestId('call-history-list')).toBeInTheDocument();
    expect(within(musicPanel).getByTitle('YouTube video player')).toBe(playbackFrame);
  });

  it('opens the command palette with Ctrl-K', async () => {
    render(<SmartDisplay />);

    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'k',
        ctrlKey: true,
        bubbles: true,
      }));
    });

    expect(await screen.findByTestId('command-palette')).toBeInTheDocument();
  });

  it('does not open the command palette from focused text input', () => {
    render(<SmartDisplay />);
    const input = document.createElement('input');
    document.body.appendChild(input);
    input.focus();

    act(() => {
      input.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'k',
        ctrlKey: true,
        bubbles: true,
      }));
    });

    expect(screen.queryByTestId('command-palette')).not.toBeInTheDocument();
    input.remove();
  });

  it('captures an initial plain space when space is not the PTT hotkey', () => {
    settingsHarness.useSettings.mockImplementation(() => ({
      ...mockSettingsState(),
      settings: { ptt_hotkey: 'Ctrl+Space' },
    }));
    render(<SmartDisplay />);

    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', {
        key: ' ',
        code: 'Space',
        bubbles: true,
      }));
      document.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'a',
        code: 'KeyA',
        bubbles: true,
      }));
    });

    const responseArea = document.querySelector('.viola-bottom-row [role="status"]');
    expect(responseArea.textContent).toBe(' a');
  });

  it('toggles mic_muted when the mute hotkey (default Ctrl+M) is pressed', () => {
    settingsHarness.useSettings.mockImplementation(() => ({
      ...mockSettingsState(),
      settings: { mic_muted: false },
    }));
    render(<SmartDisplay />);

    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'm',
        code: 'KeyM',
        ctrlKey: true,
        bubbles: true,
      }));
    });

    expect(settingsHarness.updateSetting).toHaveBeenCalledWith('mic_muted', true);
  });

  it('unmutes when the mute hotkey is pressed again while already muted', () => {
    settingsHarness.useSettings.mockImplementation(() => ({
      ...mockSettingsState(),
      settings: { mic_muted: true },
    }));
    render(<SmartDisplay />);

    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'm',
        code: 'KeyM',
        ctrlKey: true,
        bubbles: true,
      }));
    });

    expect(settingsHarness.updateSetting).toHaveBeenCalledWith('mic_muted', false);
  });

  it('does not treat a plain M keypress as the mute hotkey (requires Ctrl)', () => {
    render(<SmartDisplay />);

    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'm',
        code: 'KeyM',
        bubbles: true,
      }));
    });

    expect(settingsHarness.updateSetting).not.toHaveBeenCalledWith('mic_muted', expect.anything());
  });

  it('honors a user-configured mute_hotkey instead of the default', () => {
    settingsHarness.useSettings.mockImplementation(() => ({
      ...mockSettingsState(),
      settings: { mute_hotkey: 'Ctrl+Shift+M', mic_muted: false },
    }));
    render(<SmartDisplay />);

    act(() => {
      // Plain Ctrl+M (the default) must NOT fire once a custom combo is set.
      document.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'm',
        code: 'KeyM',
        ctrlKey: true,
        bubbles: true,
      }));
    });
    expect(settingsHarness.updateSetting).not.toHaveBeenCalledWith('mic_muted', expect.anything());

    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'm',
        code: 'KeyM',
        ctrlKey: true,
        shiftKey: true,
        bubbles: true,
      }));
    });
    expect(settingsHarness.updateSetting).toHaveBeenCalledWith('mic_muted', true);
  });

  it('shows commands contributed through the stage command registry', async () => {
    const { user } = render(<SmartDisplay />);
    const newChatListener = vi.fn();
    window.addEventListener('viola:chat:new', newChatListener);

    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'k',
        ctrlKey: true,
        bubbles: true,
      }));
    });

    expect(await screen.findByText('New Chat')).toBeInTheDocument();
    expect(screen.getByText('Open Agent Browser')).toBeInTheDocument();

    await user.click(screen.getByText('New Chat'));

    expect(newChatListener).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('viola-stage')).toHaveAttribute('data-stage-mode', 'chat');
    window.removeEventListener('viola:chat:new', newChatListener);
  });

  it('announces the live phone pill state', async () => {
    render(<SmartDisplay />);

    await waitFor(() => expect(wsHarness.handler).toBeTypeOf('function'));
    act(() => {
      wsHarness.handler({
        type: 'call_started',
        payload: {
          call_id: 'call-live',
          phone_number: '+1 555 0101',
          started_at: '2026-05-11T12:00:00Z',
        },
      });
    });

    expect(await screen.findByTestId('stage-pill-phone')).toHaveTextContent('Live');
    expect(screen.getByTestId('stage-pill-status')).toHaveTextContent('Phone Live');
  });

  it('does NOT auto-open the phone tab when a call starts', async () => {
    // Founder direction 2026-06-29: placing a call must NOT switch the stage to
    // the phone tab. The user stays on the music stage; only the phone pill +
    // window title signal the live call.
    playerHarness.state = {
      is_playing: true,
      now_playing: { id: 't1', title: 'Track', artist: 'A', provider: 'youtube_iframe' },
    };
    render(<SmartDisplay />);

    await waitFor(() => expect(wsHarness.handler).toBeTypeOf('function'));
    act(() => {
      wsHarness.handler({
        type: 'call_started',
        payload: { call_id: 'call-live', phone_number: '+1 555 0101' },
      });
    });

    // Pill shows Live, but the stage did NOT switch to phone.
    expect(await screen.findByTestId('stage-pill-phone')).toHaveTextContent('Live');
    await waitFor(() => expect(screen.getByTestId('viola-stage')).not.toHaveAttribute('data-stage-mode', 'phone'));
  });

  it('renders the live-call screen when the phone tab is opened mid-call', async () => {
    // The founder-observed symptom: a tab opened mid-call showed the history
    // list because activeCallId was never set (the call_started event had
    // already fired before the tab opened). The fetchActiveCall recovery must
    // reconstruct the live call so the live-call panel renders, not history.
    const { fetchActiveCall } = await import('../hooks/useCallAudio');
    fetchActiveCall.mockResolvedValueOnce({
      call_id: 'call-in-progress',
      phone_number: '+1 555 0202',
      task: 'ask about hours',
      status: 'active',
      started_at: '2026-06-29T15:40:48Z',
    });

    const { user } = render(<SmartDisplay />);

    // Open the phone tab WITHOUT any prior call_started WebSocket event.
    await user.click(await screen.findByTestId('stage-pill-phone'));

    // The live-call panel (transcript + takeover + consult) renders, NOT the
    // history list.
    expect(await screen.findByTestId('phone-call-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('call-history-list')).not.toBeInTheDocument();
  });

  it('keeps the contextual Agent pill through its cooldown and announces it', async () => {
    render(<SmartDisplay />);

    await waitFor(() => expect(playerHarness.overlayCallback).toBeTypeOf('function'));
    act(() => {
      playerHarness.overlayCallback({
        visible: true,
        mode: 'agentic',
        url: 'https://example.test',
        agent_task: {
          status: 'Checking page details',
          phase: 'reading',
        },
      });
    });

    expect(await screen.findByTestId('stage-pill-agent')).toBeInTheDocument();
    expect(screen.getByTestId('stage-pill-status')).toHaveTextContent('Checking page details available');

    vi.useFakeTimers();
    try {
      act(() => {
        playerHarness.overlayCallback({
          visible: false,
          agent_outcome: 'done',
        });
      });
      expect(screen.getByTestId('stage-pill-agent')).toBeInTheDocument();

      act(() => {
        vi.advanceTimersByTime(agentContextPillCooldownMs);
      });

      expect(screen.queryByTestId('stage-pill-agent')).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('enables the dedicated agent-browser stream when an agentic browser task starts', async () => {
    render(<SmartDisplay />);

    // A web client with no browsing task active: the dedicated stream is off.
    await waitFor(() => expect(agentBrowserStreamHarness.calls.length).toBeGreaterThan(0));
    expect(agentBrowserStreamHarness.calls.every((enabled) => enabled === false)).toBe(true);

    // When cloud Viola starts an agentic browser task, the stream turns on.
    await waitFor(() => expect(playerHarness.overlayCallback).toBeTypeOf('function'));
    act(() => {
      playerHarness.overlayCallback({
        visible: true,
        mode: 'agentic',
        url: 'https://example.test',
        agent_task: { status: 'Browsing', phase: 'acting' },
      });
    });

    await waitFor(() => {
      expect(agentBrowserStreamHarness.calls[agentBrowserStreamHarness.calls.length - 1]).toBe(true);
    });
  });

  it('publishes the Stage rect to the Qt browser bridge', async () => {
    const setBrowserOverlayBounds = vi.fn();
    window.viola = { setBrowserOverlayBounds };
    const rectSpy = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      x: 12.4,
      y: 34.5,
      width: 800.2,
      height: 420.8,
      top: 34.5,
      left: 12.4,
      right: 812.6,
      bottom: 455.3,
    });

    render(<SmartDisplay />);
    act(() => {
      window.dispatchEvent(new Event('viola-bridge-ready'));
    });

    await waitFor(() => {
      expect(setBrowserOverlayBounds).toHaveBeenCalledWith(12, 35, 800, 421);
    });
    rectSpy.mockRestore();
    delete window.viola;
  });

  it('publishes the Stage rect when the Qt bridge becomes ready after mount', async () => {
    delete window.viola;
    const setBrowserOverlayBounds = vi.fn();
    const rectSpy = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      x: 12.4,
      y: 34.5,
      width: 800.2,
      height: 420.8,
      top: 34.5,
      left: 12.4,
      right: 812.6,
      bottom: 455.3,
    });

    render(<SmartDisplay />);

    await new Promise((resolve) => window.requestAnimationFrame(resolve));
    window.viola = { setBrowserOverlayBounds };
    act(() => {
      window.dispatchEvent(new Event('viola-bridge-ready'));
    });

    await waitFor(() => {
      expect(setBrowserOverlayBounds).toHaveBeenCalledWith(12, 35, 800, 421);
    });
    rectSpy.mockRestore();
    delete window.viola;
  });

  it('opens the account settings section from a payment ui_action event', async () => {
    render(<SmartDisplay />);

    act(() => {
      window.dispatchEvent(new CustomEvent('viola:ui-action', {
        detail: { action: 'open_payment_methods', payload: {} },
      }));
    });

    expect(await screen.findByTestId('settings-modal')).toHaveTextContent('account');
  });

  it('shows a dismissable call summary from call_ended and opens the transcript view', async () => {
    const { user } = render(<SmartDisplay />);

    await waitFor(() => expect(wsHarness.handler).toBeTypeOf('function'));
    act(() => {
      wsHarness.handler({
        type: 'call_started',
        payload: {
          call_id: 'call-ended',
          phone_number: '+1 555 0100',
          started_at: '2026-05-09T17:00:00Z',
        },
      });
      wsHarness.handler({
        type: 'call_ended',
        payload: {
          call_id: 'call-ended',
          phone_number: '+1 555 0100',
          outcome: 'Booking confirmed',
          summary: 'Appointment set for Tuesday.',
          duration_seconds: 154,
        },
      });
    });

    expect(await screen.findByTestId('call-summary-card')).toHaveTextContent('Booking confirmed');
    expect(screen.getByTestId('call-summary-card')).toHaveTextContent('Appointment set for Tuesday.');

    await user.click(screen.getByRole('button', { name: /view transcript/i }));

    expect(await screen.findByTestId('call-history-list')).toBeInTheDocument();
    expect(await screen.findByText('+1 555 0100')).toBeInTheDocument();
    expect(await screen.findByText('The appointment is confirmed.')).toBeInTheDocument();
  });
});

describe('YouTube iframe playback honesty (#2757)', () => {
  // Measured against the real ui/static/webviews/youtube_iframe_v3.html driven in
  // Chromium: when a video is not embeddable the page emits yt_iframe_ready, then
  // yt_iframe_error{error:150,fatal:true}, then yt_iframe_time{state:-1} forever.
  // It never emits a yt_iframe_state transition, because the player never leaves
  // UNSTARTED. yt_iframe_time only feeds position_update, which does not touch
  // is_playing. So unless the error itself is reported, nothing tells the backend
  // playback did not start, and its optimistic is_playing stands until the embedded
  // watchdog fires - duration + 60s, or a full 10 minutes when duration is 0,
  // which it is when the embed never loaded.
  const iframeMessage = (type, payload) =>
    new MessageEvent('message', { data: { type, payload }, origin: window.location.origin });

  const mountWithTrack = async (videoId = 'vid-123') => {
    playerHarness.state = {
      is_playing: true,
      now_playing: { id: 't1', title: 'Track', artist: 'A', provider: 'youtube_iframe', video_id: videoId },
      queue: [],
    };
    const result = render(<SmartDisplay />);
    await waitFor(() => expect(wsHarness.handler).toBeTypeOf('function'));
    return result;
  };

  const youtubeStateMessages = () => wsHarness.sent.filter((m) => m?.action === 'youtube_state');

  it.each([150, 101, 100, 2])('reports playback did not start on iframe error %i', async (errorCode) => {
    await mountWithTrack();

    act(() => {
      window.dispatchEvent(iframeMessage('yt_iframe_error', { error: errorCode, fatal: true, videoId: 'vid-123' }));
    });

    await waitFor(() => expect(youtubeStateMessages().length).toBeGreaterThan(0));
    const message = youtubeStateMessages().at(-1);
    expect(message.payload.state).toBe('UNSTARTED');
    expect(message.payload.video_id).toBe('vid-123');
  });

  it('reports playback did not start when the player gives up retrying', async () => {
    await mountWithTrack();

    act(() => {
      window.dispatchEvent(iframeMessage('yt_iframe_autoplay_blocked', { reason: 'max_retries', retries: 5 }));
    });

    await waitFor(() => expect(youtubeStateMessages().length).toBeGreaterThan(0));
    expect(youtubeStateMessages().at(-1).payload.state).toBe('UNSTARTED');
  });

  it('ignores an error from a video that is no longer the current track', async () => {
    await mountWithTrack('vid-current');

    act(() => {
      window.dispatchEvent(iframeMessage('yt_iframe_error', { error: 150, fatal: true, videoId: 'vid-stale' }));
    });

    expect(youtubeStateMessages()).toHaveLength(0);
  });

  it('does not claim playback stopped while the track is playing normally', async () => {
    await mountWithTrack();

    act(() => {
      window.dispatchEvent(
        iframeMessage('yt_iframe_time', { currentTime: 12, duration: 200, state: 1, videoId: 'vid-123' }),
      );
    });

    await waitFor(() => expect(wsHarness.sent.length).toBeGreaterThan(0));
    expect(youtubeStateMessages().filter((m) => m.payload.state === 'UNSTARTED')).toHaveLength(0);
  });

  it('reports a stuck player once, not once per retry post', async () => {
    // Measured against the real player page: once retryPlayVideo() exceeds
    // _maxPlayRetries it re-posts yt_iframe_autoplay_blocked on every 1s tick
    // and never clears _wantsToPlay (11 posts in 15s). Without the per-video
    // dedupe this would become a 1 Hz backend message and state broadcast.
    await mountWithTrack();

    act(() => {
      for (let i = 0; i < 12; i += 1) {
        window.dispatchEvent(iframeMessage('yt_iframe_autoplay_blocked', { reason: 'max_retries', retries: 10 }));
      }
    });

    await waitFor(() => expect(youtubeStateMessages().length).toBeGreaterThan(0));
    expect(youtubeStateMessages()).toHaveLength(1);
  });

  it('reports the failure again once the queue moves to a new track', async () => {
    // The dedupe is per video, cleared by the track-change effect - otherwise a
    // single bad embed would permanently silence the signal for the rest of the
    // session.
    const { rerender } = await mountWithTrack('vid-a');

    act(() => {
      window.dispatchEvent(iframeMessage('yt_iframe_error', { error: 150, fatal: true, videoId: 'vid-a' }));
      window.dispatchEvent(iframeMessage('yt_iframe_error', { error: 150, fatal: true, videoId: 'vid-a' }));
    });
    await waitFor(() => expect(youtubeStateMessages()).toHaveLength(1));

    playerHarness.state = {
      is_playing: true,
      now_playing: { id: 't2', title: 'Next', artist: 'B', provider: 'youtube_iframe', video_id: 'vid-b' },
      queue: [],
    };
    await act(async () => { rerender(<SmartDisplay />); });

    act(() => {
      window.dispatchEvent(iframeMessage('yt_iframe_error', { error: 150, fatal: true, videoId: 'vid-b' }));
    });
    await waitFor(() => expect(youtubeStateMessages()).toHaveLength(2));
    expect(youtubeStateMessages().at(-1).payload.video_id).toBe('vid-b');
  });
});

describe('SmartDisplay shuffle toggle', () => {
  // #4214. The button flips optimistically, which is the right feel -- but the
  // pre-fix call was `setShuffleOn(next); api.setShuffle(next);` with the
  // promise neither awaited nor caught. On any failure the button stayed
  // showing the state the user asked for while the server kept the old one,
  // and it could not self-correct: the reconcile effect is keyed on
  // `playerState.shuffle`, so it only re-runs when the server's value CHANGES,
  // which a failed request never does. The divergence lasted until some other
  // actor moved shuffle.
  const mountPlaying = async () => {
    playerHarness.state = {
      is_playing: true,
      now_playing: { id: 't1', title: 'Track', artist: 'A' },
      queue: [],
      shuffle: false,
    };
    const result = render(<SmartDisplay />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Shuffle tracks' })).toBeInTheDocument());
    return result;
  };

  const shuffleButton = () => screen.getByRole('button', { name: 'Shuffle tracks' });
  const isOn = () => shuffleButton().getAttribute('aria-pressed') === 'true';

  it('keeps returning-user transport controls accessible after the welcome delay', async () => {
    vi.useFakeTimers();
    try {
      playerHarness.state = {
        is_playing: true,
        now_playing: { id: 't1', title: 'Track', artist: 'A' },
        queue: [],
        shuffle: false,
      };
      render(<SmartDisplay />);
      // Let the real welcome hook consume the mocked server response, then
      // advance through the first-visit timer instead of racing wall-clock load.
      await act(async () => { await Promise.resolve(); });
      await act(async () => { await vi.advanceTimersByTimeAsync(400); });
      expect(shuffleButton()).toBeVisible();
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('flips the toggle and tells the server when the request succeeds', async () => {
    const { user } = await mountPlaying();
    expect(isOn()).toBe(false);

    await user.click(shuffleButton());

    await waitFor(() => expect(apiHarness.setShuffle).toHaveBeenCalledWith(true));
    expect(isOn()).toBe(true);
  });

  it('rolls the toggle back to the state the server reports when the reorder fails', async () => {
    // `/v1/shuffle` reorders before it records, so a failed reorder means the
    // preference never moved -- and the error envelope says which value is
    // actually in effect. That value, not the optimistic one, is the truth.
    const failure = Object.assign(new Error('nope'), {
      status: 500,
      code: 'shuffle_queue_failed',
      data: { shuffle: false },
    });
    apiHarness.setShuffle.mockRejectedValueOnce(failure);

    const { user } = await mountPlaying();
    await user.click(shuffleButton());

    await waitFor(() => expect(isOn()).toBe(false));
  });

  it('rolls back to the pre-toggle state when the request never reached the route', async () => {
    // A network or auth failure carries no envelope, so there is no server
    // answer to read. The server still holds the old value, which is what the
    // toggle must go back to showing.
    apiHarness.setShuffle.mockRejectedValueOnce(new Error('network down'));

    const { user } = await mountPlaying();
    await user.click(shuffleButton());

    await waitFor(() => expect(isOn()).toBe(false));
  });

  it('surfaces the failure instead of failing silently', async () => {
    apiHarness.setShuffle.mockRejectedValueOnce(new Error('network down'));

    const { user } = await mountPlaying();
    await user.click(shuffleButton());

    expect(await screen.findByText("Couldn't change shuffle. Please try again.")).toBeInTheDocument();
  });
});

describe('SmartDisplay transport controls own their failures', () => {
  // The nightly chaos spec (tests/e2e/web/chaos_resilience.spec.ts) went red on
  // main because Next and Previous fired their request and walked away:
  // `api.skip().finally(...)` -- and `.finally()` re-raises a rejection rather
  // than handling one -- and a bare `api.previous()`. With the network HEALTHY
  // and an empty queue, /v1/skip answered 409 empty_queue and /v1/previous
  // answered "no previous track", and both rejections escaped to
  // `window.onerror` carrying apiFetch's generic
  // "We couldn't complete that request. Please try again.". Nothing renders an
  // uncaught error, so the user clicked and watched the button do nothing at
  // all. Same for the volume slider, which is the third call site the same run
  // caught rejecting unhandled while offline.
  const mountPlaying = async () => {
    playerHarness.state = {
      is_playing: true,
      now_playing: { id: 't1', title: 'Track', artist: 'A' },
      queue: [],
      volume: 50,
    };
    const result = render(<SmartDisplay />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Next track' })).toBeInTheDocument());
    return result;
  };

  /** The exact rejection `apiFetch` throws for a non-2xx: one generic message,
   * with the status and code carried alongside it. */
  const apiRejection = (status, code) => Object.assign(
    new Error("We couldn't complete that request. Please try again."),
    { status, code },
  );

  it('tells the user WHY when Next has nowhere to go', async () => {
    apiHarness.skip.mockRejectedValueOnce(apiRejection(409, 'empty_queue'));

    const { user } = await mountPlaying();
    await user.click(screen.getByRole('button', { name: 'Next track' }));

    expect(await screen.findByText('No more tracks in the queue.')).toBeInTheDocument();
  });

  it('tells the user WHY when Previous has nowhere to go', async () => {
    apiHarness.previous.mockRejectedValueOnce(apiRejection(409, 'previous_failed'));

    const { user } = await mountPlaying();
    await user.click(screen.getByRole('button', { name: 'Previous track' }));

    expect(await screen.findByText("You're already at the first track.")).toBeInTheDocument();
  });

  it('reports a real server fault as an error, not as an end-of-queue', async () => {
    // A 500 is the player actually broken. Saying "no more tracks" there would
    // be a comfortable lie, so the two must not collapse into one message.
    apiHarness.skip.mockRejectedValueOnce(apiRejection(500, 'skip_failed'));

    const { user } = await mountPlaying();
    await user.click(screen.getByRole('button', { name: 'Next track' }));

    expect(
      await screen.findByText("Couldn't skip to the next track. Please try again."),
    ).toBeInTheDocument();
    expect(screen.queryByText('No more tracks in the queue.')).not.toBeInTheDocument();
  });

  it('never shows apiFetch\'s generic placeholder to the user', async () => {
    // THE RATCHET on the copy: passing `err.message` through would render this
    // string, which names no cause and offers no next step. Every transport
    // failure has to be described by the app, from the status/code.
    apiHarness.skip.mockRejectedValueOnce(apiRejection(409, 'empty_queue'));

    const { user } = await mountPlaying();
    await user.click(screen.getByRole('button', { name: 'Next track' }));

    await screen.findByText('No more tracks in the queue.');
    expect(
      screen.queryByText("We couldn't complete that request. Please try again."),
    ).not.toBeInTheDocument();
  });

  it('stays quiet for a request that never reached the server', async () => {
    // Offline: `fetch` rejects with a bare TypeError and no status. The lost
    // connection is already announced once by the disconnect toast and the
    // "Disconnected" label, so a per-click toast would only stack duplicates --
    // but the rejection still has to be HANDLED, which is what the absence of
    // an unhandled-rejection failure in this test proves.
    apiHarness.skip.mockRejectedValueOnce(new TypeError('Failed to fetch'));

    const { user } = await mountPlaying();
    await user.click(screen.getByRole('button', { name: 'Next track' }));

    await waitFor(() => expect(apiHarness.skip).toHaveBeenCalled());
    expect(screen.queryByText('No more tracks in the queue.')).not.toBeInTheDocument();
    expect(
      screen.queryByText("Couldn't skip to the next track. Please try again."),
    ).not.toBeInTheDocument();
  });

  it('leaves the debounce reset working even when the skip fails', async () => {
    // `.catch()` has to come BEFORE `.finally()`. Chained the other way the
    // rejection is still live when `.finally()` re-raises it, which is the
    // pre-fix shape -- and a swallowed reset would wedge Next after one
    // failure.
    apiHarness.skip.mockRejectedValueOnce(apiRejection(409, 'empty_queue'));

    const { user } = await mountPlaying();
    const nextButton = screen.getByRole('button', { name: 'Next track' });
    await user.click(nextButton);
    await screen.findByText('No more tracks in the queue.');

    await waitFor(async () => {
      await user.click(nextButton);
      expect(apiHarness.skip).toHaveBeenCalledTimes(2);
    }, { timeout: 3000 });
  });
});

describe('cloud browser playback needs the user gesture (#3552)', () => {
  // Measured against the DEPLOYED webview in real Chromium under the default
  // autoplay policy: `youtube_iframe_v3.html?...&autoplay=1&mute=0` reports
  // UNSTARTED, burns three `Retry playVideo()` attempts, falls into
  // "Trying muted start workaround" and settles at playerState 2 (PAUSED),
  // currentTime 0.46 -- nothing audible. The same URL with `mute=1` reaches
  // playerState 1 (PLAYING) at 14.6s. A plain browser will not start unmuted
  // media that no user gesture asked for; the desktop Qt WebEngine shell will.
  // So the cloud surface must CUE the video and let the user's click on
  // YouTube's own controls be the gesture -- exactly the
  // `requires_user_gesture` the cloud playback plan already declares
  // (services/cloud_music/playback_plan.py).
  const mountWithTrack = async (videoId = 'fJ9rUzIMcZQ') => {
    playerHarness.state = {
      is_playing: false,
      now_playing: { id: 't1', title: 'Track', artist: 'A', provider: 'youtube_iframe', video_id: videoId },
      queue: [],
    };
    const result = render(<SmartDisplay />);
    await waitFor(() => expect(wsHarness.handler).toBeTypeOf('function'));
    return result;
  };

  const embedSrc = () => screen.getByTitle('YouTube video player').getAttribute('src');

  it('cues the video instead of requesting an autoplay the browser blocks', async () => {
    await mountWithTrack();

    const src = embedSrc();
    // Pre-fix this read `autoplay=1`, which a browser blocks every single time.
    expect(src).toContain('autoplay=0');
    expect(src).not.toContain('autoplay=1');
    expect(src).toContain('video=fJ9rUzIMcZQ');
  });

  it('keeps autoplay on the desktop shell, where Qt WebEngine honours it', async () => {
    window.viola = { isDesktop: true };
    try {
      await mountWithTrack('desktopVid1');
      expect(embedSrc()).toContain('autoplay=1');
    } finally {
      delete window.viola;
    }
  });

  it('still mounts a real player element the user can press play on', async () => {
    await mountWithTrack();

    const iframe = screen.getByTitle('YouTube video player');
    expect(iframe.tagName).toBe('IFRAME');
    expect(iframe.getAttribute('src')).toContain('/static/webviews/youtube_iframe_v3.html');
  });
});
