/**
 * useVoiceWs — rapid-succession PTT press must never be a SILENT drop (#385).
 *
 * Residual failure mode (post-CONFIRM-6 warm-keeping, #372): a real signed-in
 * browser user who asks two voice questions in quick succession can have the
 * second push-to-talk press silently swallowed. Root cause:
 *   - `_finishWithResult` sets isProcessing=false the instant `command_result`
 *     lands, but `sessionActiveRef.current` stays TRUE for the whole
 *     TTS-playback / teardown window (up to TTS_WAIT_CAP_MS=10s, or 1s after the
 *     last TTS frame).
 *   - In that window `isRecording===false && isProcessing===false`, so EVERY
 *     external observer (the PTT button `disabled={isProcessing}`, SmartDisplay's
 *     start path, the keyboard hotkey, the latency harness) believes the session
 *     is idle and available.
 *   - `startRecording()`'s `if (sessionActiveRef.current) return;` guard then
 *     drops the press with no socket, no error, no state change.
 *
 * The fix keeps the anti-overlap guard but makes the guard window OBSERVABLE:
 * the hook exposes `isBusy`, true for the exact guard window, so a press during
 * teardown yields a visible busy state instead of a silent no-op. This test is
 * RED against the pre-fix hook (no isBusy; the window reads as all-idle) and
 * GREEN against the fix.
 *
 * SCOPE: client hook state machine against a mocked socket + fake timers. Real
 * browser rapid-succession reliability is proven separately by the live
 * feature-drive / latency oracle voice legs.
 */

import { renderHook, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

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

vi.mock('../lib/ws_auth', () => ({
  getWebSocketAuthToken: vi.fn(async () => 'test-ws-token'),
}));

import useVoiceWs from './useVoiceWs';

const WS_OPEN = 1;

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
    if (typeof this.onclose === 'function') this.onclose();
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
    transcript: 'what time is it',
    response: 'It is noon.',
  });
}

function ttsFrame() {
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

// Drive the hook through one full press/release and return the hook + socket.
async function startAndStop(hook) {
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
  await act(async () => {
    await hook.result.current.stopRecording();
  });
  return ws;
}

describe('useVoiceWs rapid-succession PTT (#385): no silent drop during teardown', () => {
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

  it('exposes a busy state for the ENTIRE guard window, incl. after command_result while TTS is still expected', async () => {
    const hook = renderHook(() => useVoiceWs(vi.fn()));
    const ws = await startAndStop(hook);

    // command_result arrives: the answer text is ready, so isProcessing flips
    // false and isRecording is already false. To every external observer this
    // looks idle -- but the session is NOT torn down yet (still awaiting TTS).
    act(() => { deliverIfOpen(ws, commandResultMessage()); });
    expect(hook.result.current.isRecording).toBe(false);
    expect(hook.result.current.isProcessing).toBe(false);

    // THE INVARIANT: because a startRecording() right now would be dropped by
    // the sessionActiveRef guard, the hook MUST expose that it is busy. Pre-fix
    // there is no such signal (isBusy is undefined) -> RED.
    expect(hook.result.current.isBusy).toBe(true);
  });

  it('a second press during teardown never overlaps AND never vanishes silently', async () => {
    const hook = renderHook(() => useVoiceWs(vi.fn()));
    const ws1 = await startAndStop(hook);
    act(() => { deliverIfOpen(ws1, commandResultMessage()); });

    const socketsBefore = wsInstances.length;

    // Rapid second press while turn 1 is still tearing down.
    await act(async () => {
      await hook.result.current.startRecording();
      for (let i = 0; i < 10; i += 1) await Promise.resolve();
    });

    // Must NOT start an overlapping capture (the guard's real job).
    expect(hook.result.current.isRecording).toBe(false);
    expect(wsInstances.length).toBe(socketsBefore);
    // Must NOT be a silent drop: the busy state is visible.
    expect(hook.result.current.isBusy).toBe(true);
  });

  it('after the turn fully tears down, isBusy clears and the NEXT press starts a real turn', async () => {
    const hook = renderHook(() => useVoiceWs(vi.fn()));
    const ws1 = await startAndStop(hook);
    act(() => { deliverIfOpen(ws1, commandResultMessage()); });
    act(() => { deliverIfOpen(ws1, ttsFrame()); });

    // Let the post-TTS idle teardown fire (1s) plus slack.
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(hook.result.current.isBusy).toBe(false);

    // A fresh press now genuinely starts recording (warm socket reused).
    let startPromise;
    act(() => { startPromise = hook.result.current.startRecording(); });
    await act(async () => {
      for (let i = 0; i < 10; i += 1) await Promise.resolve();
      await startPromise;
    });
    expect(hook.result.current.isRecording).toBe(true);
    expect(hook.result.current.isBusy).toBe(true);
  });
});
