/**
 * useVoiceWs — mid-turn WebSocket close codes must surface an honest,
 * user-visible error instead of the mic silently stopping (#2606, the
 * frontend half of backend commit 8077f878).
 *
 * 8077f878 taught every cloud WS route (including /ws/voice-stream, see
 * ui/api/routes/voice_stream.py) to accept-then-close via
 * ui.core.security.reject_websocket so the browser's `onclose` actually
 * receives the app's intended close code/reason (expired auth -> 4401,
 * too-many-connections -> 4429, voice subsystem unavailable -> 1013, a
 * graceful server restart -> 1001 "server_shutdown" from
 * backend/cloud_app.py) instead of an opaque abnormal-closure 1006. The
 * frontend half was never built: pre-fix, `ws.onclose` took no event
 * parameter at all, never inspected `code`/`reason`, and tore the turn down
 * via `_teardown()` with no `setError` -- so a real forced close mid-turn
 * left the user staring at a mic that just stopped "processing" with zero
 * explanation.
 *
 * This suite forces each close code mid-turn (recording in flight, before
 * command_result) and asserts `error` becomes a non-empty, code-specific
 * message. It is RED against the pre-fix hook (no event param means `error`
 * stays null on every one of these) and GREEN against the fix.
 */

import { renderHook, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../utils/ttsPlayback', () => ({
  playTtsPcm: vi.fn(),
  decodeTtsFrame: vi.fn(() => ({ pcmBuffer: new ArrayBuffer(16), sampleRate: 16000 })),
  isTtsFrame: vi.fn(() => false),
}));

vi.mock('../lib/ws_auth', () => ({
  getWebSocketAuthToken: vi.fn(async () => 'test-ws-token'),
}));

import useVoiceWs from './useVoiceWs';

let wsInstances;
class DriverWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  constructor(url) {
    this.url = url;
    this.readyState = DriverWebSocket.OPEN;
    this.binaryType = 'blob';
    this.sent = [];
    this.closeCount = 0;
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.onclose = null;
    wsInstances.push(this);
  }

  send(data) { this.sent.push(data); }

  // Client-initiated close (used by _teardown/_armIdleCloseTimer). Real
  // server-forced closes are simulated directly via ws.onclose({code, reason})
  // in the tests below, since that is what the browser actually delivers for
  // a server-sent close frame -- this method models the OTHER direction.
  close() {
    this.closeCount += 1;
    this.readyState = DriverWebSocket.CLOSED;
    if (typeof this.onclose === 'function') this.onclose({ code: 1000, reason: '' });
  }
}
DriverWebSocket.prototype.CONNECTING = 0;
DriverWebSocket.prototype.OPEN = 1;
DriverWebSocket.prototype.CLOSING = 2;
DriverWebSocket.prototype.CLOSED = 3;

class MockAudioContext {
  constructor() {
    this.destination = {};
    this.currentTime = 0;
    this.state = 'running';
    this.sampleRate = 16000;
  }
  createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
  createScriptProcessor() { return { connect() {}, disconnect() {}, onaudioprocess: null }; }
  close() { return Promise.resolve(); }
}

function makeMediaStream() {
  const track = { readyState: 'live', stop: vi.fn() };
  return {
    active: true,
    getTracks: () => [track],
    getAudioTracks: () => [track],
  };
}

/** Press-and-open: starts a turn (recording, mid-turn) and returns the socket. */
async function pressAndOpen(hook) {
  let startPromise;
  act(() => { startPromise = hook.result.current.startRecording(); });
  await act(async () => {
    for (let i = 0; i < 10; i += 1) await Promise.resolve();
  });
  const ws = wsInstances[wsInstances.length - 1];
  expect(ws).toBeTruthy();
  await act(async () => {
    ws.onopen();
    await startPromise;
  });
  expect(hook.result.current.isRecording).toBe(true);
  return ws;
}

describe('useVoiceWs (#2606): mid-turn WS close codes surface an honest error, never a silent stop', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    wsInstances = [];
    global.WebSocket = DriverWebSocket;
    window.AudioContext = MockAudioContext;
    window.webkitAudioContext = MockAudioContext;
    window.__VIOLA_BASE_URL__ = 'http://localhost:8756';
    window.history.replaceState({}, '', '/');
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: vi.fn(async () => makeMediaStream()) },
    });
  });

  afterEach(() => {
    act(() => { vi.runOnlyPendingTimers(); });
    vi.useRealTimers();
  });

  const cases = [
    {
      name: 'expired auth (4401 "User identity required" -- token-refresh race on a warm-kept turn)',
      code: 4401,
      reason: 'User identity required',
      expectedSubstring: 'session expired',
    },
    {
      name: 'too many active voice connections (4429)',
      code: 4429,
      reason: 'Too many active voice connections',
      expectedSubstring: 'Too many active voice connections',
    },
    {
      name: 'voice subsystem unavailable (1013 WS_1013_TRY_AGAIN_LATER)',
      code: 1013,
      reason: 'voice subsystem disabled',
      expectedSubstring: 'temporarily unavailable',
    },
    {
      name: 'backend graceful restart (1001 "server_shutdown", backend/cloud_app.py)',
      code: 1001,
      reason: 'server_shutdown',
      expectedSubstring: 'restarted',
    },
  ];

  for (const { name, code, reason, expectedSubstring } of cases) {
    it(`${name} -> user-visible error, not a silent stop`, async () => {
      const hook = renderHook(() => useVoiceWs(vi.fn()));
      const ws = await pressAndOpen(hook);

      expect(hook.result.current.error).toBeNull();

      // The server force-closes the connection mid-turn (before command_result)
      // with the real close code/reason -- exactly what reject_websocket /
      // the shutdown broadcast now delivers over the wire post-8077f878.
      act(() => { ws.onclose({ code, reason }); });

      // Pre-fix: onclose took no event param at all, so `error` never moved
      // off null here -- this assertion is what makes the test RED pre-fix.
      expect(hook.result.current.error).not.toBeNull();
      expect(hook.result.current.error.toLowerCase()).toContain(expectedSubstring.toLowerCase());
      // The turn must be torn down too (not left dangling) -- an honest error
      // AND a clean state, not just one or the other.
      expect(hook.result.current.isRecording).toBe(false);
      expect(hook.result.current.isProcessing).toBe(false);
    });
  }

  it('does NOT surface an error for a close while no turn is in flight (prewarm / idle warm-keep teardown)', async () => {
    const hook = renderHook(() => useVoiceWs(vi.fn()));

    // prewarmConnection() opens a socket WITHOUT ever setting sessionActiveRef
    // (no turn started) -- this is the idle-warm-keep / server-idle-timeout
    // case the pre-existing `else` branch already handled (stop pinging, drop
    // the stale ref) and must stay untouched by this fix: no turn was in
    // flight, so there is nothing to explain to the user.
    act(() => { hook.result.current.prewarmConnection(); });
    await act(async () => {
      for (let i = 0; i < 10; i += 1) await Promise.resolve();
    });
    const ws = wsInstances[wsInstances.length - 1];
    expect(ws).toBeTruthy();
    act(() => { ws.onopen(); });

    act(() => { ws.onclose({ code: 1000, reason: '' }); });
    expect(hook.result.current.error).toBeNull();
  });
});
