/**
 * The transcript render chain: local socket -> rendered text. (Refs #1529)
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * A live call's transcript reaches the desktop Phone panel through a chain:
 *
 *   speech -> cloud /ws/events hub
 *          -> server-side relay (telephony/phone_cloud_event_relay.py)
 *          -> desktop backend -> LOCAL /ws/events hub
 *          -> browser socket (useWebSocket)
 *          -> useCloudPhoneEvents  (routes `transcript` frames)
 *          -> useCallAudio.pushTranscript -> mergeTranscriptEntry (state merge)
 *          -> PhoneCallPanel                                     (render)
 *
 * Before this file, BOTH ENDS were covered and the JOIN was not:
 *
 *   - tests/unit/telephony/test_phone_cloud_event_relay.py drives a real local
 *     hub and a real socket, but stops at the socket — it never renders.
 *   - hooks/useCloudPhoneEvents.test.js mocks BOTH `useWebSocket` and
 *     `authFetch`, so no socket exists and nothing renders.
 *   - components/PhoneCallPanel.test.jsx is the only real rendering coverage,
 *     but it hands `transcripts` in as a PROP — the hook and the state merge
 *     are never exercised.
 *   - __tests__/SmartDisplay.test.jsx lets the real useCloudPhoneEvents run,
 *     but mocks useCallAudio with `transcripts: []` and NO `pushTranscript`
 *     key, so the handler at SmartDisplay.jsx:1747 is falsy and inert.
 *
 * Net: nothing asserted that a relayed transcript frame arriving on the local
 * socket produces rendered text in the panel. That join is exactly where the
 * two historically-observed failures lived (a principal mismatch between the
 * relay's account scope and the tab socket's device scope, and a socket drop
 * with no backfill).
 *
 * WHAT IS REAL HERE
 * -----------------
 * Deliberately NOT mocked, because they ARE the chain under test:
 *   - useWebSocket        (real shared-socket plumbing; only the global
 *                          `WebSocket` constructor is stubbed, which is the
 *                          local-socket boundary frames are injected at)
 *   - useCloudPhoneEvents (real frame routing)
 *   - useCallAudio        (real `pushTranscript` + `mergeTranscriptEntry`;
 *                          only its REST helpers are stubbed, since fetching
 *                          call history is not part of the render chain)
 *   - PhoneCallPanel      (real rendering)
 *   - SmartDisplay        (real wiring — this is what makes the test cover the
 *                          SmartDisplay.jsx:1747 handoff rather than a
 *                          hand-rolled re-implementation of it)
 *
 * Everything else is stubbed exactly as __tests__/SmartDisplay.test.jsx stubs
 * it, so SmartDisplay can mount without a backend.
 *
 * This test does NOT and CANNOT stand in for #1529's oracle, which is a
 * watched live call showing turns render at camera-acceptable latency. It
 * makes that live proof trustworthy by removing the one stretch of the chain
 * where a regression would go undetected; it does not substitute for it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, render, screen, waitFor, within } from '../../test/test-utils';

const wsHarness = vi.hoisted(() => ({ sockets: [] }));

// ---------------------------------------------------------------------------
// Peripheral stubs — copied from __tests__/SmartDisplay.test.jsx so SmartDisplay
// mounts without a backend. NOTE what is absent: useWebSocket, useCallAudio's
// default export, useCloudPhoneEvents, and PhoneCallPanel all run for real.
// ---------------------------------------------------------------------------
vi.mock('../../hooks/usePlayerState', () => ({
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
    send: vi.fn(),
    connectCount: 0,
    setLocalIsPlaying: vi.fn(),
    setDiagnosticRequestCallback: vi.fn(),
    setErrorCallback: vi.fn(),
    setSpokeMessageCallback: vi.fn(),
    setOverlayCallback: vi.fn(),
    setAgentProgressCallback: vi.fn(),
    setDisplayPriorityCallback: vi.fn(),
    setAgentFrameCallback: vi.fn(),
    setPlaybackCommandCallback: vi.fn(),
    setChatResponseCallback: vi.fn(),
    setCalendarUpdateCallback: vi.fn(),
    setDisconnectCallback: vi.fn(),
    getWsDebug: vi.fn(() => ({})),
  }),
}));

vi.mock('../../hooks/useViolaApi', () => ({
  // useCloudPhoneEvents drives the server-side relay lifecycle through this and
  // carries only the local api key (SEC-017). Stubbed, but asserted on below.
  authFetch: vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({}) })),
  apiFetch: vi.fn(() => Promise.resolve({})),
  useViolaApi: () => ({
    getState: vi.fn(() => Promise.resolve({})),
    play: vi.fn(),
    pause: vi.fn(),
    resume: vi.fn(),
    stop: vi.fn(),
    skip: vi.fn(),
    next: vi.fn(),
    previous: vi.fn(),
    seek: vi.fn(),
    setVolume: vi.fn(),
    setRating: vi.fn(),
    getQueue: vi.fn(() => Promise.resolve([])),
    clearQueue: vi.fn(),
    sendCommand: vi.fn(() => Promise.resolve({ message: 'ok' })),
    getWeather: vi.fn(() => Promise.resolve({})),
    submitBugReport: vi.fn(() => Promise.resolve({ bug_ticket_id: 42 })),
    getCalendarEvents: vi.fn(() => Promise.resolve([])),
    getCalendarStatus: vi.fn(() => Promise.resolve({})),
    getCalendarNextEvent: vi.fn(() => Promise.resolve(null)),
  }),
}));

// useCallAudio is PARTIALLY mocked: the hook itself (default) and the merge rule
// stay real — they are under test. Only the REST helpers SmartDisplay calls for
// history/queue/active-call recovery are stubbed, since none of them are part of
// the socket -> render chain.
vi.mock('../../hooks/useCallAudio', async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    fetchCallHistory: vi.fn(() => Promise.resolve({ count: 0, calls: [] })),
    getCallTranscript: vi.fn(() => Promise.resolve({ call_id: '', transcript: [] })),
    fetchCallQueue: vi.fn(() => Promise.resolve([])),
    removeQueuedCall: vi.fn(() => Promise.resolve({ queue: [] })),
    fetchActiveCall: vi.fn(() => Promise.resolve(null)),
  };
});

vi.mock('../../hooks/useAgentBrowserStream', () => ({
  useAgentBrowserStream: vi.fn(() => ({
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
  })),
}));

vi.mock('../../hooks/useVoice', () => ({
  useVoice: () => ({
    isListening: false,
    transcript: '',
    startListening: vi.fn(),
    stopListening: vi.fn(),
  }),
}));

vi.mock('../../hooks/useSettings', () => ({
  useSettings: () => ({ settings: {}, updateSetting: vi.fn(), loading: false }),
}));

vi.mock('../../hooks/useAuth', () => ({
  useAuth: () => ({ user: null, status: 'signedOut' }),
}));

vi.mock('../../sentryClient', () => ({
  openSentryUserFeedback: vi.fn(() => Promise.resolve(true)),
  syncSentrySettings: vi.fn(),
}));

vi.mock('../../hooks/useAvailableRooms', () => ({
  useAvailableRooms: () => ({ rooms: [], loading: false }),
}));

vi.mock('../../hooks/useVoiceOnboarding', () => ({
  useVoiceOnboarding: () => ({
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
  }),
}));

vi.mock('../../components/SettingsModal', () => ({
  default: () => <div data-testid="settings-modal">Settings</div>,
}));
vi.mock('../../components/QueueModal', () => ({
  default: () => <div data-testid="queue-modal">Queue</div>,
}));
vi.mock('../../components/HistoryModal', () => ({
  default: () => <div data-testid="history-modal">History</div>,
}));
vi.mock('../../components/RoomGroupsModal', () => ({
  default: () => <div data-testid="room-groups-modal">Room Groups</div>,
}));
vi.mock('../../components/HelpModal', () => ({
  default: () => <div data-testid="help-modal">Help</div>,
}));
vi.mock('../../components/CalendarView', () => ({
  default: () => <div data-testid="calendar-view">Calendar</div>,
}));

// ---------------------------------------------------------------------------
// The local-socket boundary.
//
// src/test/setup.js installs a MockWebSocket global with an `_emit` helper.
// Subclassing it and recording every instance gives the test a handle on the
// socket the REAL useWebSocket opened, which is precisely the seam a relayed
// frame arrives on. Nothing between here and the rendered DOM is stubbed.
// ---------------------------------------------------------------------------
const BaseMockWebSocket = global.WebSocket;

class RecordingWebSocket extends BaseMockWebSocket {
  constructor(url) {
    super(url);
    wsHarness.sockets.push(this);
  }
}

let SmartDisplay;
let useCallAudioMod;
let violaApiMod;

beforeEach(async () => {
  wsHarness.sockets = [];
  global.WebSocket = RecordingWebSocket;

  useCallAudioMod = await import('../../hooks/useCallAudio');
  useCallAudioMod.fetchActiveCall.mockReset();
  useCallAudioMod.fetchActiveCall.mockResolvedValue(null);
  useCallAudioMod.fetchCallQueue.mockReset();
  useCallAudioMod.fetchCallQueue.mockResolvedValue([]);
  useCallAudioMod.fetchCallHistory.mockReset();
  useCallAudioMod.fetchCallHistory.mockResolvedValue({ count: 0, calls: [] });

  violaApiMod = await import('../../hooks/useViolaApi');
  violaApiMod.authFetch.mockClear();

  const mod = await import('../../SmartDisplay');
  SmartDisplay = mod.default;
});

afterEach(() => {
  global.WebSocket = BaseMockWebSocket;
});

/** The socket the real useWebSocket most recently opened. */
async function currentSocket() {
  await waitFor(() => expect(wsHarness.sockets.length).toBeGreaterThan(0));
  return wsHarness.sockets[wsHarness.sockets.length - 1];
}

/**
 * Deliver one JSON frame on the local /ws/events socket, exactly as the
 * server-side relay's republished payload arrives in the browser.
 */
function deliver(socket, frame) {
  act(() => {
    socket._emit('message', { data: JSON.stringify(frame) });
  });
}

/**
 * A relayed transcript frame. `callId` is omitted by default so the existing
 * cases keep exercising the no-call_id shape (the per-call /ws/call-listen
 * frame, and any older cloud frame), which must still render.
 */
function transcriptFrame({ role, text, partial = false, ts, callId }) {
  const payload = { role, text, partial, ts };
  if (callId !== undefined) payload.call_id = callId;
  return { type: 'transcript', payload };
}

/**
 * Mount SmartDisplay with a call already in progress and open the phone tab,
 * so the real PhoneCallPanel is on screen fed by the real useCallAudio state.
 * Returns the live socket to inject frames on.
 */
async function openLiveCallPanel(user) {
  useCallAudioMod.fetchActiveCall.mockResolvedValue({
    call_id: 'call-chain-1529',
    phone_number: '+1 555 0143',
    business_name: 'Tony Pepperoni Pizza',
    task: 'order a large pepperoni',
    status: 'active',
    started_at: '2026-07-24T18:00:00Z',
  });

  await user.click(await screen.findByTestId('stage-pill-phone'));
  expect(await screen.findByTestId('phone-call-panel')).toBeInTheDocument();

  const socket = await currentSocket();
  act(() => socket._emit('open'));
  return socket;
}

/** The rendered transcript bubbles, in order. */
function renderedTranscriptLines() {
  const pane = screen.getByTestId('phone-call-transcript');
  // Bubbles are the leaf divs holding the entry text; the placeholder is the
  // italic "Live transcript" node shown only when there are zero entries.
  return within(pane)
    .queryAllByTestId('phone-call-transcript-line')
    .map((node) => node.textContent);
}

describe('transcript render chain: local socket -> PhoneCallPanel (Refs #1529)', () => {
  it('renders a relayed transcript frame that arrives on the local socket', async () => {
    const { user } = render(<SmartDisplay />);
    const socket = await openLiveCallPanel(user);

    // Nothing yet: the panel shows its empty-state placeholder.
    expect(screen.getByText('Live transcript')).toBeInTheDocument();

    deliver(socket, transcriptFrame({
      role: 'them',
      text: 'Tony Pepperoni, what can I get you?',
      ts: 1,
    }));

    // THE JOIN: a frame on the socket became text in the DOM, through the real
    // useCloudPhoneEvents routing and the real useCallAudio state merge.
    const pane = await screen.findByTestId('phone-call-transcript');
    await waitFor(() => {
      expect(within(pane).getByText('Tony Pepperoni, what can I get you?')).toBeInTheDocument();
    });
    expect(screen.queryByText('Live transcript')).not.toBeInTheDocument();
  });

  it('drives the server-side relay lifecycle with no cloud bearer in the browser', async () => {
    // The desktop cannot receive ANY frame unless the local backend is asked to
    // run the cloud->local relay. That request must carry only the local api key.
    const { user } = render(<SmartDisplay />);
    await openLiveCallPanel(user);

    await waitFor(() => {
      expect(violaApiMod.authFetch).toHaveBeenCalledWith(
        '/v1/phone/cloud-events/start',
        { method: 'POST' },
      );
    });
    const startCall = violaApiMod.authFetch.mock.calls.find(
      (c) => String(c[0]).includes('cloud-events/start'),
    );
    expect(JSON.stringify(startCall)).not.toMatch(/bearer|authorization/i);
  });

  it('collapses Viola partials in place and lets the final replace the partial', async () => {
    // Viola STREAMS: successive partials for one turn must overwrite each other
    // rather than stack, and the final must replace the last partial.
    // (useCallAudio.js mergeTranscriptEntry, the `shouldReplaceLast` branch.)
    const { user } = render(<SmartDisplay />);
    const socket = await openLiveCallPanel(user);
    const pane = screen.getByTestId('phone-call-transcript');

    deliver(socket, transcriptFrame({ role: 'viola', text: "Hi, I'd like", partial: true, ts: 10 }));
    await waitFor(() => expect(within(pane).getByText("Hi, I'd like")).toBeInTheDocument());

    deliver(socket, transcriptFrame({ role: 'viola', text: "Hi, I'd like to order", partial: true, ts: 11 }));
    await waitFor(() => expect(within(pane).getByText("Hi, I'd like to order")).toBeInTheDocument());
    // The superseded partial is GONE, not stacked above it.
    expect(within(pane).queryByText("Hi, I'd like")).not.toBeInTheDocument();
    expect(renderedTranscriptLines()).toEqual(["Hi, I'd like to order"]);

    deliver(socket, transcriptFrame({
      role: 'viola',
      text: "Hi, I'd like to order a large pepperoni",
      partial: false,
      ts: 12,
    }));
    await waitFor(() => {
      expect(renderedTranscriptLines()).toEqual(["Hi, I'd like to order a large pepperoni"]);
    });
    // A final over a partial replaces it: one turn, one bubble.
    expect(within(pane).queryByText("Hi, I'd like to order")).not.toBeInTheDocument();
  });

  it('accumulates recipient finals instead of collapsing them', async () => {
    // The asymmetry the shoot depends on: Viola streams partials (collapsed
    // above) while the recipient side emits ONLY finals, which must each stand
    // as their own turn. Collapsing these would eat the other half of the call.
    const { user } = render(<SmartDisplay />);
    const socket = await openLiveCallPanel(user);

    deliver(socket, transcriptFrame({ role: 'them', text: 'Sure, large pepperoni.', ts: 20 }));
    deliver(socket, transcriptFrame({ role: 'them', text: 'That will be twenty minutes.', ts: 21 }));

    await waitFor(() => {
      expect(renderedTranscriptLines()).toEqual([
        'Sure, large pepperoni.',
        'That will be twenty minutes.',
      ]);
    });
  });

  it('keeps a finalised turn when the other speaker starts streaming', async () => {
    // A partial only ever replaces the last entry when the ROLE matches. A
    // recipient final followed by a Viola partial must produce two bubbles, not
    // an overwrite — the interleaving a real call is made of.
    const { user } = render(<SmartDisplay />);
    const socket = await openLiveCallPanel(user);

    deliver(socket, transcriptFrame({ role: 'them', text: 'Anything else?', ts: 30 }));
    deliver(socket, transcriptFrame({ role: 'viola', text: 'Just', partial: true, ts: 31 }));
    deliver(socket, transcriptFrame({ role: 'viola', text: 'Just the pizza', partial: true, ts: 32 }));

    await waitFor(() => {
      expect(renderedTranscriptLines()).toEqual(['Anything else?', 'Just the pizza']);
    });
  });

  it('ignores non-transcript and empty frames on the same socket', async () => {
    // The local /ws/events socket is SHARED with the rest of the app. Player
    // state, playback commands, and malformed frames must not reach the panel.
    const { user } = render(<SmartDisplay />);
    const socket = await openLiveCallPanel(user);

    deliver(socket, { type: 'state', payload: { volume: 40 } });
    deliver(socket, { type: 'transcript', payload: { role: 'them', text: '' } });
    deliver(socket, { type: 'transcript', payload: { role: 'them', text: 42 } });

    // Still the empty-state placeholder: none of those produced a bubble.
    await waitFor(() => expect(screen.getByText('Live transcript')).toBeInTheDocument());
    expect(renderedTranscriptLines()).toEqual([]);

    deliver(socket, transcriptFrame({ role: 'them', text: 'Real line.', ts: 40 }));
    await waitFor(() => expect(renderedTranscriptLines()).toEqual(['Real line.']));
  });

  it('renders only the displayed call when another call of the same account is also live', async () => {
    // The cloud /ws/events hub is scoped to the USER, not to one call, and an
    // account may have several calls live at once (up to
    // config.max_concurrent_calls, default 25 — telephony/call_manager.py:4523).
    // The relay forwards every one of them onto the local socket, so the panel
    // showing call A receives call B's transcript too. Only the call_id tells
    // them apart, and before this was fixed useCloudPhoneEvents dropped that
    // field, so a second call's speech rendered into this call's panel.
    const { user } = render(<SmartDisplay />);
    const socket = await openLiveCallPanel(user);

    deliver(socket, transcriptFrame({
      callId: 'call-chain-1529',
      role: 'them',
      text: 'Tony Pepperoni, what can I get you?',
      ts: 60,
    }));
    await waitFor(() => {
      expect(renderedTranscriptLines()).toEqual(['Tony Pepperoni, what can I get you?']);
    });

    // A DIFFERENT live call of the same account. This must not reach the panel.
    deliver(socket, transcriptFrame({
      callId: 'call-some-other-1529',
      role: 'them',
      text: 'Dentist office, do you want the Tuesday slot?',
      ts: 61,
    }));
    deliver(socket, transcriptFrame({
      callId: 'call-some-other-1529',
      role: 'viola',
      text: 'Yes, Tuesday works.',
      ts: 62,
    }));

    // The displayed call keeps streaming, which also pins down that the foreign
    // frames were dropped rather than merely arriving late.
    deliver(socket, transcriptFrame({
      callId: 'call-chain-1529',
      role: 'viola',
      text: "Hi, I'd like a large pepperoni.",
      ts: 63,
    }));

    await waitFor(() => {
      expect(renderedTranscriptLines()).toEqual([
        'Tony Pepperoni, what can I get you?',
        "Hi, I'd like a large pepperoni.",
      ]);
    });
  });

  it('survives a socket drop: rendered turns persist and the new socket still routes', async () => {
    // One of the two historically-observed failures at this join was a socket
    // drop with no backfill. The browser half of the contract: an already
    // rendered turn must NOT be wiped by the drop, and once useWebSocket's
    // backoff reconnects, frames on the REPLACEMENT socket must still reach the
    // panel (the subscriber's handler refs have to survive the reconnect).
    const { user } = render(<SmartDisplay />);
    const socket = await openLiveCallPanel(user);

    deliver(socket, transcriptFrame({ role: 'them', text: 'Before the drop.', ts: 50 }));
    await waitFor(() => expect(renderedTranscriptLines()).toEqual(['Before the drop.']));

    const socketCountBefore = wsHarness.sockets.length;

    vi.useFakeTimers();
    try {
      act(() => socket._emit('close', { code: 1006, reason: 'transport lost' }));
      // Already-rendered turns are state, not socket-derived: the drop alone
      // must not clear them.
      expect(renderedTranscriptLines()).toEqual(['Before the drop.']);

      // useWebSocket's capped backoff starts at 2s.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500);
      });
    } finally {
      vi.useRealTimers();
    }

    expect(wsHarness.sockets.length).toBeGreaterThan(socketCountBefore);
    const reconnected = wsHarness.sockets[wsHarness.sockets.length - 1];
    expect(reconnected).not.toBe(socket);

    act(() => reconnected._emit('open'));
    deliver(reconnected, transcriptFrame({ role: 'them', text: 'After the drop.', ts: 51 }));

    await waitFor(() => {
      expect(renderedTranscriptLines()).toEqual(['Before the drop.', 'After the drop.']);
    });
  });

  it('renders the SECOND call of a session the same as the first', async () => {
    // The feature used to work exactly once per visit. The relay that carries
    // these frames had one owner, the browser, arming it on
    // `isPhoneMode || Boolean(activeCallId)` — and activeCallId is only ever
    // learned from a call_started that can only arrive over that same relay, so
    // once it went down nothing could bring it back and every later call
    // rendered nothing. The relay side of that is fixed where it lives
    // (telephony/phone_cloud_event_relay.py, leases). This holds the BROWSER
    // half: given the frames do keep arriving, a second call must render like
    // the first, with the finished call's turns gone rather than stacked under
    // the new one.
    const { user } = render(<SmartDisplay />);
    await user.click(await screen.findByTestId('stage-pill-phone'));
    const socket = await currentSocket();
    act(() => socket._emit('open'));

    deliver(socket, { type: 'call_started', payload: { call_id: 'call-one' } });
    expect(await screen.findByTestId('phone-call-panel')).toBeInTheDocument();
    deliver(socket, transcriptFrame({
      callId: 'call-one',
      role: 'them',
      text: 'First call speaking.',
      ts: 70,
    }));
    await waitFor(() => expect(renderedTranscriptLines()).toEqual(['First call speaking.']));

    deliver(socket, { type: 'call_ended', payload: { call_id: 'call-one' } });

    // A brand new call, with the user having touched nothing.
    deliver(socket, { type: 'call_started', payload: { call_id: 'call-two' } });
    expect(await screen.findByTestId('phone-call-panel')).toBeInTheDocument();
    deliver(socket, transcriptFrame({
      callId: 'call-two',
      role: 'them',
      text: 'Second call speaking.',
      ts: 71,
    }));

    await waitFor(() => {
      expect(renderedTranscriptLines()).toEqual(['Second call speaking.']);
    });
    // The finished call's transcript does not bleed into the new one's panel.
    expect(screen.queryByText('First call speaking.')).not.toBeInTheDocument();
  });
});
