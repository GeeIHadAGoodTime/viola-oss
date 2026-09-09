/**
 * useVoiceWs — CONFIRM-6 client-side warm-keeping.
 *
 * Pins the behavior that makes the browser voice WS connection warm across
 * turns instead of one-per-turn, and the guardrails that bound it:
 *   1. A second turn reuses the still-open connection from the first —no
 *      second WebSocket construction, no second auth-token mint.
 *   2. The idle-but-open connection sends a cheap keepalive ping on a
 *      cadence well under the server's idle-close window.
 *   3. A hidden tab closes the idle connection immediately (never mid-turn).
 *   4. A battery-saver-like signal (not charging, low level) closes the idle
 *      connection the same way a hidden tab does.
 *   5. `prewarmConnection()` opens the connection ahead of an actual press
 *      (mic-button hover/focus intent signal) WITHOUT touching the mic, and
 *      the next `startRecording()` reuses it.
 *   6. A long-idle warm connection with no new turn self-closes client-side
 *      (bounded lifetime, independent of the hidden/battery signals).
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

const wsAuthMocks = vi.hoisted(() => ({
  getWebSocketAuthToken: vi.fn(async () => 'test-ws-token'),
}));
vi.mock('../lib/ws_auth', () => ({
  getWebSocketAuthToken: wsAuthMocks.getWebSocketAuthToken,
}));

import useVoiceWs from './useVoiceWs';

const WS_OPEN = 1;
const WS_CLOSED = 3;

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

/** Drain the mic-acquisition + auth-token-fetch microtask hops. */
async function drainMicrotasks(times = 10) {
  await act(async () => {
    for (let i = 0; i < times; i += 1) await Promise.resolve();
  });
}

async function pressAndOpen(hook) {
  let startPromise;
  act(() => { startPromise = hook.result.current.startRecording(); });
  await drainMicrotasks();
  const ws = wsInstances[wsInstances.length - 1];
  expect(ws).toBeTruthy();
  await act(async () => {
    ws.onopen();
    await startPromise;
  });
  return ws;
}

async function pressAndRelease(hook) {
  const ws = await pressAndOpen(hook);
  await act(async () => {
    await hook.result.current.stopRecording();
  });
  return ws;
}

function commandResultMessage() {
  return JSON.stringify({ type: 'command_result', transcript: 'what time is it', response: "It's 3pm." });
}

describe('useVoiceWs CONFIRM-6 warm-keeping', () => {
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
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
    // getBattery is undefined by default in jsdom — tests that need it define
    // navigator.getBattery explicitly.
    delete navigator.getBattery;
    wsAuthMocks.getWebSocketAuthToken.mockClear();
    ttsMocks.isTtsFrame.mockReturnValue(false);
  });

  afterEach(() => {
    act(() => { vi.runOnlyPendingTimers(); });
    vi.useRealTimers();
    delete navigator.getBattery;
  });

  it('reuses the still-open connection for a second turn: no second WebSocket, no second auth-token fetch', async () => {
    const hook = renderHook(() => useVoiceWs(vi.fn()));

    const ws1 = await pressAndRelease(hook);
    act(() => { ws1.onmessage({ data: commandResultMessage() }); });
    // No TTS frame follows (isTtsFrame mocked false) -> ends the turn on the
    // TTS_WAIT_CAP_MS backstop (10s), warm-keeping the connection.
    await act(async () => { await vi.advanceTimersByTimeAsync(10500); });
    expect(ws1.closeCount).toBe(0);
    expect(ws1.readyState).toBe(WS_OPEN);
    expect(wsInstances.length).toBe(1);
    expect(wsAuthMocks.getWebSocketAuthToken).toHaveBeenCalledTimes(1);

    // Second press: must reuse ws1, not construct a new socket or re-mint a
    // token.
    let startPromise2;
    act(() => { startPromise2 = hook.result.current.startRecording(); });
    await drainMicrotasks();
    expect(wsInstances.length).toBe(1); // still just the one socket
    await act(async () => { await startPromise2; });
    expect(hook.result.current.isRecording).toBe(true);
    expect(wsAuthMocks.getWebSocketAuthToken).toHaveBeenCalledTimes(1); // not re-minted
    // ptt_start was sent on the reused socket for the second turn.
    const pttStarts = ws1.sent.filter((m) => typeof m === 'string' && m.includes('ptt_start'));
    expect(pttStarts.length).toBe(2);
  });

  it('sends a cheap keepalive ping on the idle-warm connection', async () => {
    const hook = renderHook(() => useVoiceWs(vi.fn()));
    const ws = await pressAndRelease(hook);
    act(() => { ws.onmessage({ data: commandResultMessage() }); });
    await act(async () => { await vi.advanceTimersByTimeAsync(10500); }); // end of turn, warm-keep armed

    ws.sent = []; // clear turn-time sends, isolate the idle-ping check
    await act(async () => { await vi.advanceTimersByTimeAsync(20000); });
    const pings = ws.sent.filter((m) => typeof m === 'string' && JSON.parse(m).type === 'ping');
    expect(pings.length).toBeGreaterThanOrEqual(1);
    expect(ws.readyState).toBe(WS_OPEN); // pinging, not closing
  });

  it('closes the idle warm connection the instant the tab is hidden, never mid-turn', async () => {
    const hook = renderHook(() => useVoiceWs(vi.fn()));
    const ws = await pressAndRelease(hook);
    act(() => { ws.onmessage({ data: commandResultMessage() }); });
    await act(async () => { await vi.advanceTimersByTimeAsync(10500); }); // idle warm-keep

    expect(ws.readyState).toBe(WS_OPEN);
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
    act(() => { document.dispatchEvent(new Event('visibilitychange')); });
    expect(ws.closeCount).toBe(1);
    expect(ws.readyState).toBe(WS_CLOSED);
  });

  it('does NOT close the connection on hidden-tab while a turn is actively in flight', async () => {
    const hook = renderHook(() => useVoiceWs(vi.fn()));
    const ws = await pressAndOpen(hook); // mid-turn: recording, not yet released
    expect(hook.result.current.isRecording).toBe(true);

    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
    act(() => { document.dispatchEvent(new Event('visibilitychange')); });
    // The in-flight turn's connection must survive a hidden-tab event.
    expect(ws.closeCount).toBe(0);
    expect(ws.readyState).toBe(WS_OPEN);
  });

  it('closes the idle warm connection when battery-saver-like (not charging, low level)', async () => {
    let changeHandlers = {};
    const battery = {
      charging: true,
      level: 0.9,
      addEventListener: vi.fn((evt, fn) => { changeHandlers[evt] = fn; }),
      removeEventListener: vi.fn(),
    };
    navigator.getBattery = vi.fn(async () => battery);

    const hook = renderHook(() => useVoiceWs(vi.fn()));
    const ws = await pressAndRelease(hook);
    act(() => { ws.onmessage({ data: commandResultMessage() }); });
    await act(async () => { await vi.advanceTimersByTimeAsync(10500); }); // idle warm-keep
    // Let the getBattery() promise resolve and register listeners.
    await drainMicrotasks();
    expect(ws.readyState).toBe(WS_OPEN);

    battery.charging = false;
    battery.level = 0.1; // at/below the 0.2 threshold, not charging
    act(() => { changeHandlers.levelchange(); });

    expect(ws.closeCount).toBe(1);
    expect(ws.readyState).toBe(WS_CLOSED);
  });

  it('prewarmConnection() opens the connection ahead of a press, without touching the mic, and startRecording reuses it', async () => {
    const hook = renderHook(() => useVoiceWs(vi.fn()));

    act(() => { hook.result.current.prewarmConnection(); });
    await drainMicrotasks();
    expect(wsInstances.length).toBe(1);
    const ws = wsInstances[0];
    // No mic access yet — prewarm never calls getUserMedia.
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();

    act(() => { ws.onopen(); });
    expect(hook.result.current.isRecording).toBe(false); // still no turn, no mic

    // A real press now reuses the prewarmed socket: no second WebSocket, no
    // second auth-token mint.
    let startPromise;
    act(() => { startPromise = hook.result.current.startRecording(); });
    await drainMicrotasks();
    expect(wsInstances.length).toBe(1);
    await act(async () => { await startPromise; });
    expect(hook.result.current.isRecording).toBe(true);
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1); // only for the real turn
    expect(wsAuthMocks.getWebSocketAuthToken).toHaveBeenCalledTimes(1);
  });

  it('prewarmConnection() is a no-op when the tab is hidden', async () => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
    const hook = renderHook(() => useVoiceWs(vi.fn()));

    act(() => { hook.result.current.prewarmConnection(); });
    await drainMicrotasks();
    expect(wsInstances.length).toBe(0);
  });

  it('self-closes a long-idle warm connection even on a visible, plugged-in tab (bounded lifetime)', async () => {
    const hook = renderHook(() => useVoiceWs(vi.fn()));
    const ws = await pressAndRelease(hook);
    act(() => { ws.onmessage({ data: commandResultMessage() }); });
    await act(async () => { await vi.advanceTimersByTimeAsync(10500); }); // idle warm-keep armed

    expect(ws.readyState).toBe(WS_OPEN);
    // Comfortably past the bounded idle-keepalive window with no new turn.
    await act(async () => { await vi.advanceTimersByTimeAsync(3 * 60 * 1000 + 1000); });
    expect(ws.closeCount).toBe(1);
    expect(ws.readyState).toBe(WS_CLOSED);
  });
});
