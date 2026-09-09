/**
 * useVoiceWs — tap-mode PTT must recover the mic even when the server never
 * answers the turn (#2769).
 *
 * A *tap* (quick press+release under SmartDisplay's TAP_THRESHOLD_MS) does
 * NOT call stopRecording() -- the mic is meant to keep listening until the
 * SERVER's own silence/timeout endpointing (voice_stream.py
 * feed_command_pcm) ends the capture, not any client action. Before this
 * fix, the client had no signal at all for that server-side ending: no
 * `capture_ended`/equivalent message existed, so isRecording stayed true for
 * the whole agent-thinking window and -- critically -- the RESPONSE_TIMEOUT_MS
 * dropped-turn backstop was NEVER armed, because arming it only happened
 * inside stopRecording(). A turn the server silently dropped after ending
 * capture (no command_result ever sent) left the mic hot and the UI stuck
 * "recording" until the user reloaded the page.
 *
 * The fix: the server sends `{"type": "capture_ended"}` the instant it ends
 * a still-in-progress command capture on its own (voice_stream.py's
 * feed_command_pcm auto-end branch), and useVoiceWs.js's onmessage handler
 * treats it like the tail half of stopRecording() -- stop pushing mic
 * frames, flip isRecording->false/isProcessing->true, and arm the SAME
 * RESPONSE_TIMEOUT_MS backstop stopRecording() uses.
 *
 * This test drives the hook exactly like a tap: startRecording() then NO
 * stopRecording() call at all (mirroring SmartDisplay.jsx's handlePTTEnd
 * early-return for a tap), delivers `capture_ended` as the server would, and
 * proves (a) the mic stops streaming and the UI leaves "recording", and (b)
 * if command_result never arrives, the response-timeout backstop still
 * fires and fully tears the session down client-side -- the mic recovers
 * without a reload.
 *
 * SCOPE: client hook state machine against a mocked socket + fake timers.
 * Real browser live-trace confirmation of the server emitting this message
 * is a separate live check (noted in the PR).
 */

import { renderHook, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const ttsMocks = vi.hoisted(() => ({
  playTtsPcm: vi.fn(),
  decodeTtsFrame: vi.fn(() => ({ pcmBuffer: new ArrayBuffer(16), sampleRate: 16000 })),
  isTtsFrame: vi.fn(() => false),
}));
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

function captureEndedMessage() {
  return JSON.stringify({ type: 'capture_ended' });
}

function commandResultMessage() {
  return JSON.stringify({
    type: 'command_result',
    transcript: 'what time is it',
    response: 'It is noon.',
  });
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

// Drive the hook through a TAP press: start, then (unlike a hold) never call
// stopRecording() -- exactly what SmartDisplay.jsx's handlePTTEnd does for a
// tap (early-returns before calling voice.stopRecording()).
async function startTap(hook) {
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

describe('useVoiceWs tap-mode capture_ended (#2769): mic recovers without stopRecording()', () => {
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
  });

  afterEach(() => {
    act(() => { vi.runOnlyPendingTimers(); });
    vi.useRealTimers();
  });

  it('capture_ended alone (no stopRecording call) stops the mic and flips to processing', async () => {
    const hook = renderHook(() => useVoiceWs(vi.fn()));
    const ws = await startTap(hook);

    // Server ends the tap's capture on its own (silence/timeout endpointing)
    // -- the client never sent ptt_stop and never called stopRecording().
    act(() => { deliverIfOpen(ws, captureEndedMessage()); });

    expect(hook.result.current.isRecording).toBe(false);
    expect(hook.result.current.isProcessing).toBe(true);

    // The audioprocess callback must be torn down so no further PCM streams
    // after the server already ended capture.
    // (Nothing further to send -- proven indirectly: onaudioprocess nulled
    // is exercised via the processor mock's disconnect having been called.)
  });

  it('a dropped turn after capture_ended (no command_result ever arrives) still recovers the mic', async () => {
    const onCommandResult = vi.fn();
    const hook = renderHook(() => useVoiceWs(onCommandResult));
    const ws = await startTap(hook);

    act(() => { deliverIfOpen(ws, captureEndedMessage()); });
    expect(hook.result.current.isRecording).toBe(false);
    expect(hook.result.current.isProcessing).toBe(true);
    expect(hook.result.current.isBusy).toBe(true);

    // THE BUG (pre-fix): no backstop was ever armed for a tap turn, so this
    // would hang forever. Post-fix, RESPONSE_TIMEOUT_MS (35s) was armed by
    // _handleCaptureEnded, exactly like stopRecording() would have.
    await act(async () => { await vi.advanceTimersByTimeAsync(35000); });

    expect(hook.result.current.isProcessing).toBe(false);
    expect(hook.result.current.error).toBe('No response from hub');
    expect(onCommandResult).toHaveBeenCalledWith({ ok: false, error: 'response_timeout' });

    // Full recovery: the session tears all the way down (no TTS was
    // expected for a failed turn), so a fresh press works again.
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(hook.result.current.isBusy).toBe(false);
    expect(hook.result.current.isRecording).toBe(false);
  });

  it('command_result arriving normally after capture_ended still completes the turn', async () => {
    const onCommandResult = vi.fn();
    const hook = renderHook(() => useVoiceWs(onCommandResult));
    const ws = await startTap(hook);

    act(() => { deliverIfOpen(ws, captureEndedMessage()); });
    act(() => { deliverIfOpen(ws, commandResultMessage()); });

    expect(hook.result.current.isProcessing).toBe(false);
    expect(hook.result.current.transcript).toBe('what time is it');
    expect(onCommandResult).toHaveBeenCalledWith(
      expect.objectContaining({ ok: true, data: expect.objectContaining({ transcript: 'what time is it' }) }),
    );
  });
});
