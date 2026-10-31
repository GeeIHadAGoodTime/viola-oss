/**
 * useVoiceWs: a hub reply spoken one sentence per TTS frame.
 *
 * The hub sends a spoke turn's reply sentence by sentence while the model is
 * still writing when the client declares `tts_stream` on ptt_start. Sentence
 * frames can arrive BEFORE command_result, the unspoken remainder after it,
 * and a `tts_end` message marks the last frame. The hook must declare the
 * capability, play every frame in arrival order, and end the turn on
 * `tts_end` instead of holding the press guard for TTS_WAIT_CAP_MS waiting
 * for a frame that already came.
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

function send(ws, message) {
  act(() => { deliverIfOpen(ws, JSON.stringify(message)); });
}

describe('useVoiceWs per-sentence TTS frames', () => {
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

  it('declares tts_stream on ptt_start', async () => {
    const { ws } = await startAndStop();
    const pttStart = ws.sent
      .filter((m) => typeof m === 'string')
      .map((m) => JSON.parse(m))
      .find((m) => m.type === 'ptt_start');
    expect(pttStart).toEqual({ type: 'ptt_start', tts_stream: true });
  });

  it('plays sentence frames that arrive before command_result, then ends the turn on tts_end', async () => {
    const { hook, ws, onCommandResult } = await startAndStop();

    act(() => { deliverIfOpen(ws, ttsFrame()); });
    await act(async () => { await vi.advanceTimersByTimeAsync(600); });
    act(() => { deliverIfOpen(ws, ttsFrame()); });
    expect(ttsMocks.playTtsPcm).toHaveBeenCalledTimes(2);
    expect(hook.result.current.isBusy).toBe(true);

    act(() => { deliverIfOpen(ws, commandResultMessage()); });
    expect(onCommandResult).toHaveBeenCalledTimes(1);
    act(() => { deliverIfOpen(ws, ttsFrame()); });
    expect(ttsMocks.playTtsPcm).toHaveBeenCalledTimes(3);

    send(ws, { type: 'tts_end' });
    // Ended at once, socket kept warm for the next turn.
    expect(hook.result.current.isBusy).toBe(false);
    expect(ws.closeCount).toBe(0);
  });

  it('does not hold the press guard for TTS_WAIT_CAP_MS when every frame came before command_result', async () => {
    const { hook, ws } = await startAndStop();

    act(() => { deliverIfOpen(ws, ttsFrame()); });
    act(() => { deliverIfOpen(ws, commandResultMessage()); });
    send(ws, { type: 'tts_end' });

    expect(hook.result.current.isBusy).toBe(false);
    expect(ttsMocks.playTtsPcm).toHaveBeenCalledTimes(1);
  });
});
