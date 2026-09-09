/**
 * useVoiceWs — slow-turn WS-hold + trailing-TTS delivery guard.
 *
 * Proves the client keeps the /ws/voice-stream socket OPEN long enough on a
 * slow browser voice turn to (a) receive the command_result the hub emits after
 * a 25-30s agent turn (browser_navigate / web_search), and (b) receive and play
 * the TTS PCM frame the hub streams back AFTER command_result — instead of
 * tearing the socket down early and dropping the audio.
 *
 * These tests are RED against the pre-fix timers (RESPONSE_TIMEOUT_MS=20000 +
 * a blind ~1500ms post-command_result teardown) and GREEN against the fix
 * (RESPONSE_TIMEOUT_MS=35000; hold up to TTS_WAIT_CAP_MS=10000 for the first
 * TTS frame, play each frame, tear down TTS_IDLE_TEARDOWN_MS=1000 after the
 * last). Proven both ways: see the test file's header commit / the agent report.
 *
 * SCOPE: this exercises the CLIENT timer/WS-hold logic against a mocked socket
 * and a fake-timer timeline. It does NOT cover a real browser over Cloudflare,
 * real WS backpressure, or real TTS synthesis latency — those remain a separate
 * live/human acceptance.
 */

import { renderHook, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// --- ttsPlayback: spy playTtsPcm + decodeTtsFrame, force isTtsFrame true ------
const ttsMocks = vi.hoisted(() => {
  const pcm = new ArrayBuffer(16);
  return {
    MOCK_PCM: pcm,
    playTtsPcm: vi.fn(),
    decodeTtsFrame: vi.fn(() => ({ pcmBuffer: pcm, sampleRate: 16000 })),
    isTtsFrame: vi.fn(() => true),
  };
});
vi.mock('../utils/ttsPlayback', () => ({
  playTtsPcm: ttsMocks.playTtsPcm,
  decodeTtsFrame: ttsMocks.decodeTtsFrame,
  isTtsFrame: ttsMocks.isTtsFrame,
}));

// --- ws-auth fetch: hand back a token synchronously (no real /v1/ws/auth) -----
vi.mock('../lib/ws_auth', () => ({
  getWebSocketAuthToken: vi.fn(async () => 'test-ws-token'),
}));

// --- Import under test AFTER mocks so the hook binds to them -------------------
import useVoiceWs from './useVoiceWs';

const WS_OPEN = 1;

// A WebSocket the test drives by hand: we call onopen/onmessage/onclose and we
// observe close(). Frames are delivered ONLY while the socket is open, mirroring
// reality — a torn-down socket receives nothing (that is what makes the pre-fix
// RED: the dropped frame never reaches onmessage).
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

  close() {
    this.closeCount += 1;
    this.readyState = DriverWebSocket.CLOSED;
  }
}
DriverWebSocket.prototype.CONNECTING = 0;
DriverWebSocket.prototype.OPEN = 1;
DriverWebSocket.prototype.CLOSING = 2;
DriverWebSocket.prototype.CLOSED = 3;

function deliverIfOpen(ws, data) {
  if (ws.readyState === WS_OPEN && typeof ws.onmessage === 'function') {
    ws.onmessage({ data });
  }
}

function commandResultMessage() {
  return JSON.stringify({
    type: 'command_result',
    transcript: 'what is the weather in paris',
    response: 'It is sunny in Paris.',
  });
}

function ttsFrame() {
  // "TTS\0" prefix + a few payload bytes; a real ArrayBuffer so the hook's
  // `data instanceof ArrayBuffer` check passes (isTtsFrame is mocked to true).
  return new Uint8Array([0x54, 0x54, 0x53, 0x00, 0x01, 0x02, 0x03, 0x04]).buffer;
}

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

/**
 * Drive the hook to the "user pressed then released the button" state: recording
 * started, socket open, ptt_stop sent, response timer armed. Returns the hook
 * result and the driven socket.
 *
 * CONFIRM-6 warm-keeping note: startRecording() now awaits the connection
 * actually reaching `onopen` (the same connect path prewarm/reuse share), so
 * it cannot be awaited to completion before the mocked socket's `onopen` is
 * invoked — that would deadlock against the test's own mock, which (like a
 * real WebSocket) only calls `onopen` when the test decides the handshake
 * completed. Instead: kick off startRecording() without awaiting, drain the
 * mic-acquisition + auth-token-fetch microtask hops, grab the constructed
 * socket, fire its `onopen`, THEN await the original promise.
 */
async function startAndStop() {
  const onCommandResult = vi.fn();
  const hook = renderHook(() => useVoiceWs(onCommandResult));

  let startPromise;
  act(() => { startPromise = hook.result.current.startRecording(); });
  // Drain the getUserMedia() -> _connectSocket() -> getWebSocketAuthToken()
  // microtask chain (each a one-tick mocked async function) so the
  // WebSocket is constructed before we read wsInstances.
  await act(async () => {
    for (let i = 0; i < 10; i += 1) await Promise.resolve();
  });

  const ws = wsInstances[0];
  expect(ws).toBeTruthy();

  // Simulate the socket connecting: this is what resolves startRecording(),
  // flips isRecording=true, and wires up the (mocked) audio graph.
  await act(async () => {
    ws.onopen();
    await startPromise;
  });
  expect(hook.result.current.isRecording).toBe(true);

  // Release the push-to-talk button: sends ptt_stop, arms the response timer.
  await act(async () => {
    await hook.result.current.stopRecording();
  });

  return { hook, ws, onCommandResult };
}

describe('useVoiceWs slow-turn WS-hold + trailing TTS delivery', () => {
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
    ttsMocks.playTtsPcm.mockClear();
    ttsMocks.decodeTtsFrame.mockClear();
    ttsMocks.decodeTtsFrame.mockReturnValue({ pcmBuffer: ttsMocks.MOCK_PCM, sampleRate: 16000 });
    ttsMocks.isTtsFrame.mockReturnValue(true);
  });

  afterEach(() => {
    act(() => { vi.runOnlyPendingTimers(); });
    vi.useRealTimers();
  });

  it('assertion 1+2: holds the socket open through a ~30s turn, then plays the trailing TTS frame', async () => {
    const { ws, onCommandResult } = await startAndStop();

    // ~30s of silence while the agent runs a slow browser_navigate / web_search.
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });

    // ASSERTION 1 — the socket must still be OPEN so it can receive the
    // command_result the hub is about to send. (Pre-fix RESPONSE_TIMEOUT_MS=20000
    // tears it down ~21.5s in -> RED here.)
    expect(ws.closeCount).toBe(0);
    expect(ws.readyState).toBe(WS_OPEN);

    // The hub emits command_result at ~30s.
    act(() => { deliverIfOpen(ws, commandResultMessage()); });
    expect(onCommandResult).toHaveBeenCalledTimes(1);

    // TTS synthesis takes a couple seconds; socket must stay open to receive it.
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(ws.closeCount).toBe(0);
    expect(ws.readyState).toBe(WS_OPEN);

    // The hub streams the TTS PCM frame ~2s after command_result.
    const frame = ttsFrame();
    act(() => { deliverIfOpen(ws, frame); });

    // ASSERTION 2 — the frame reached the client and was played. (Pre-fix blind
    // ~1500ms teardown closes the socket first, so the frame is dropped and
    // playTtsPcm is never called -> RED here.)
    expect(ttsMocks.playTtsPcm).toHaveBeenCalledTimes(1);
    expect(ttsMocks.playTtsPcm).toHaveBeenCalledWith(ttsMocks.MOCK_PCM, 16000);
  });

  it('assertion 2 isolated: holds the socket for a TTS frame trailing command_result by more than the old 1500ms blind teardown', async () => {
    // command_result arrives quickly (5s) — before ANY response timeout in either
    // the fixed or pre-fix constants — so this test isolates the post-result hold
    // from the response-timeout raise. The TTS frame trails by 2s (> pre-fix
    // 1500ms blind teardown), so only the fix keeps the socket open for it.
    const { ws } = await startAndStop();

    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    act(() => { deliverIfOpen(ws, commandResultMessage()); });

    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    // Pre-fix: blind 1500ms teardown already closed it -> frame dropped -> RED.
    expect(ws.readyState).toBe(WS_OPEN);
    act(() => { deliverIfOpen(ws, ttsFrame()); });
    expect(ttsMocks.playTtsPcm).toHaveBeenCalledTimes(1);
  });

  it('assertion 3 (updated for CONFIRM-6 warm-keeping): a FAST turn ends promptly but keeps the WS warm for reuse, instead of closing it', async () => {
    // Pre-fix (and pre-CONFIRM-6) behavior closed the socket ~1s after the
    // last TTS frame on every turn, forcing the NEXT push-to-talk to pay a
    // fresh ws-auth-token mint + WS handshake. CONFIRM-6 keeps an idle,
    // just-finished-turn connection open (with a cheap keepalive ping and a
    // bounded auto-close — see useVoiceWs.warmKeeping.test.jsx) so the next
    // turn can reuse it instead.
    const { ws } = await startAndStop();

    // Fast turn: command_result almost immediately.
    act(() => { deliverIfOpen(ws, commandResultMessage()); });

    // TTS frame ~200ms later.
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    expect(ws.readyState).toBe(WS_OPEN); // did not tear down instantly
    act(() => { deliverIfOpen(ws, ttsFrame()); });
    expect(ttsMocks.playTtsPcm).toHaveBeenCalledTimes(1);

    // Still open immediately after the frame.
    expect(ws.readyState).toBe(WS_OPEN);

    // Well past the old ~1s idle-teardown window: the connection must still
    // be open and never closed — it is being kept warm for the next turn,
    // not torn down (the bounded warm-keep auto-close is on the order of
    // minutes, covered separately in useVoiceWs.warmKeeping.test.jsx).
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(ws.closeCount).toBe(0);
    expect(ws.readyState).toBe(WS_OPEN);
  });
});
