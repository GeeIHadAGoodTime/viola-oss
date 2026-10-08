/** Source-only J04 controls: synthetic mic, auth, socket and PCM boundaries. */
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const auth = vi.hoisted(() => ({ getWebSocketAuthToken: vi.fn() }));
const tts = vi.hoisted(() => ({
  playTtsPcm: vi.fn(),
  decodeTtsFrame: vi.fn(() => ({ pcmBuffer: new ArrayBuffer(16), sampleRate: 16000 })),
  isTtsFrame: vi.fn(() => true),
}));
vi.mock('../lib/ws_auth', () => auth);
vi.mock('../utils/ttsPlayback', () => tts);
import useVoiceWs from './useVoiceWs';

const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const makeStream = () => {
  const track = { readyState: 'live', stop: vi.fn() };
  return { active: true, track, getTracks: () => [track], getAudioTracks: () => [track] };
};
let sockets, contexts, streams;
class Socket {
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSED = 3;
  constructor() {
    this.readyState = Socket.CONNECTING;
    this.send = vi.fn();
    // Browser close events are asynchronous; tests explicitly deliver queued callbacks.
    this.close = vi.fn(() => { this.readyState = Socket.CLOSED; });
    sockets.push(this);
  }
  open() { this.readyState = Socket.OPEN; this.onopen?.(); }
}
class AudioContext {
  constructor() {
    this.sampleRate = 16000;
    this.destination = {};
    this.source = { connect: vi.fn(), disconnect: vi.fn() };
    this.processor = { connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null };
    this.close = vi.fn(async () => {});
    contexts.push(this);
  }
  createMediaStreamSource() { return this.source; }
  createScriptProcessor() { return this.processor; }
}
const flush = async () => act(async () => {
  for (let i = 0; i < 12; i += 1) await Promise.resolve();
});
function mount(onResult = vi.fn(), options = {}) {
  return renderHook(({ enabled }) => useVoiceWs(onResult, { ...options, enabled }), {
    initialProps: { enabled: true },
  });
}
// Do not await the pending start before delivering its synthetic socket-open event.
async function openTurn(hook) {
  let pending;
  act(() => { pending = hook.result.current.startRecording(); });
  await flush();
  const ws = sockets.at(-1);
  await act(async () => { ws.open(); await pending; });
  return ws;
}
const command = { data: JSON.stringify({ type: 'command_result', transcript: 'synthetic request', response: 'synthetic answer' }) };
const pcm = { inputBuffer: { getChannelData: () => new Float32Array([0.1, 0.2]) } };

beforeEach(() => {
  vi.useFakeTimers();
  delete navigator.getBattery;
  sockets = []; contexts = []; streams = [];
  auth.getWebSocketAuthToken.mockReset().mockResolvedValue('synthetic-token');
  tts.playTtsPcm.mockClear();
  vi.stubGlobal('WebSocket', Socket);
  vi.stubGlobal('AudioContext', AudioContext);
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
    getUserMedia: vi.fn(async () => { const stream = makeStream(); streams.push(stream); return stream; }),
  } });
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
});
afterEach(() => { cleanup(); delete navigator.getBattery; vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); });


function idleRetirementSignal(kind) {
  if (kind === 'hidden-tab') {
    return {
      retire() {
        Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
        document.dispatchEvent(new Event('visibilitychange'));
      },
      restore() {
        Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
      },
    };
  }
  const handlers = {};
  const battery = {
    charging: true, level: 1,
    addEventListener: (name, callback) => { handlers[name] = callback; },
    removeEventListener: name => { delete handlers[name]; },
  };
  navigator.getBattery = vi.fn(async () => battery);
  return {
    retire() { battery.charging = false; battery.level = 0.1; handlers.levelchange(); },
    restore() { battery.charging = true; battery.level = 1; handlers.chargingchange(); },
  };
}

describe('voice WebSocket capture ownership', () => {
  it('does not create a socket when pending auth resolves after disable', async () => {
    const token = deferred(); auth.getWebSocketAuthToken.mockReturnValueOnce(token.promise);
    const hook = mount();
    act(() => { void hook.result.current.startRecording(); });
    await flush();
    expect(auth.getWebSocketAuthToken).toHaveBeenCalledTimes(1);
    hook.rerender({ enabled: false });
    await act(async () => { token.resolve('late-token'); });
    expect(sockets).toHaveLength(0);
    expect(streams[0].track.stop).toHaveBeenCalledTimes(1);
    expect(contexts[0].close).toHaveBeenCalledTimes(1);
  });

  it('a new enabled turn does not wait for or reuse a cancelled auth attempt', async () => {
    const oldToken = deferred(); auth.getWebSocketAuthToken.mockReturnValueOnce(oldToken.promise);
    const hook = mount();
    act(() => { void hook.result.current.startRecording(); });
    await flush();
    hook.rerender({ enabled: false }); hook.rerender({ enabled: true });
    let pending;
    act(() => { pending = hook.result.current.startRecording(); });
    await flush();
    expect(auth.getWebSocketAuthToken).toHaveBeenCalledTimes(2);
    expect(sockets).toHaveLength(1);
    await act(async () => { oldToken.resolve('retired-token'); });
    expect(sockets).toHaveLength(1);
    await act(async () => { sockets[0].open(); await pending; });
    expect(hook.result.current.isRecording).toBe(true);
    expect(streams[1].track.stop).not.toHaveBeenCalled();
  });

  it.each(['disabled', 're-enabled'])('ignores queued old result, TTS and error callbacks while %s', async state => {
    const onResult = vi.fn(); const hook = mount(onResult);
    const oldSocket = await openTurn(hook);
    const callback = oldSocket.onmessage;
    hook.rerender({ enabled: false });
    if (state === 're-enabled') { hook.rerender({ enabled: true }); await openTurn(hook); }
    act(() => {
      callback(command);
      callback({ data: new ArrayBuffer(16) });
      callback({ data: JSON.stringify({ type: 'error', message: 'retired failure' }) });
    });
    expect(onResult).not.toHaveBeenCalled();
    expect(tts.playTtsPcm).not.toHaveBeenCalled();
    expect(hook.result.current.transcript).toBe('');
    expect(hook.result.current.error).toBeNull();
  });

  it('an asynchronous close from the cancelled socket cannot tear down the new turn', async () => {
    const hook = mount(); const oldSocket = await openTurn(hook);
    const oldClose = oldSocket.onclose;
    hook.rerender({ enabled: false }); hook.rerender({ enabled: true });
    const current = await openTurn(hook);
    act(() => { oldClose({ code: 1000 }); });
    expect(current.close).not.toHaveBeenCalled();
    expect(hook.result.current.isRecording).toBe(true);
    expect(hook.result.current.error).toBeNull();
  });

  it('a saved old audio callback cannot submit PCM after cancel and re-enable', async () => {
    const hook = mount(); const oldSocket = await openTurn(hook);
    const callback = contexts[0].processor.onaudioprocess;
    hook.rerender({ enabled: false }); hook.rerender({ enabled: true });
    await openTurn(hook);
    // A browser can remain OPEN until its asynchronous closing handshake begins.
    oldSocket.readyState = Socket.OPEN;
    const before = oldSocket.send.mock.calls.length;
    act(() => { callback(pcm); });
    expect(oldSocket.send).toHaveBeenCalledTimes(before);
    expect(oldSocket.send.mock.calls.some(([data]) => typeof data === 'string' && JSON.parse(data).type === 'ptt_stop')).toBe(false);
  });

  it('releases a permission grant delivered after unmount without opening a socket', async () => {
    const permission = deferred(); navigator.mediaDevices.getUserMedia.mockReturnValueOnce(permission.promise);
    const hook = mount();
    act(() => { void hook.result.current.startRecording(); });
    hook.unmount();
    const late = makeStream();
    await act(async () => { permission.resolve(late); });
    expect(late.track.stop).toHaveBeenCalledTimes(1);
    expect(contexts).toHaveLength(0);
    expect(sockets).toHaveLength(0);
  });

  it('does not revive a connection when auth resolves after unmount', async () => {
    const token = deferred(); auth.getWebSocketAuthToken.mockReturnValueOnce(token.promise);
    const hook = mount(); act(() => { void hook.result.current.startRecording(); });
    await flush(); hook.unmount();
    await act(async () => { token.resolve('late-token'); });
    expect(sockets).toHaveLength(0);
  });

  it('does not prewarm while disabled', async () => {
    const hook = mount(); hook.rerender({ enabled: false });
    act(() => { hook.result.current.prewarmConnection(); }); await flush();
    expect(auth.getWebSocketAuthToken).not.toHaveBeenCalled();
    expect(sockets).toHaveLength(0);
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
  });

  it('retires a pending prewarm when disabled before any capture has started', async () => {
    const token = deferred(); auth.getWebSocketAuthToken.mockReturnValueOnce(token.promise);
    const hook = mount(); act(() => { hook.result.current.prewarmConnection(); });
    await flush(); hook.rerender({ enabled: false });
    await act(async () => { token.resolve('late-token'); });
    expect(sockets).toHaveLength(0);
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
  });


  it('settles a cancelled pending connection before its socket-open callback arrives', async () => {
    const hook = mount();
    let pending;
    act(() => { pending = hook.result.current.startRecording(); });
    await flush();
    const ws = sockets[0];
    const lateOpen = ws.onopen;
    hook.rerender({ enabled: false });
    await act(async () => { await pending; });
    act(() => { lateOpen(); });
    expect(ws.send).not.toHaveBeenCalled();
    expect(hook.result.current.isBusy).toBe(false);
    expect(hook.result.current.isRecording).toBe(false);
  });

  it('an old socket error or capture-ended event cannot alter the new turn', async () => {
    const hook = mount(); const oldSocket = await openTurn(hook);
    const oldError = oldSocket.onerror, oldMessage = oldSocket.onmessage;
    hook.rerender({ enabled: false }); hook.rerender({ enabled: true });
    await openTurn(hook);
    act(() => {
      oldError();
      oldMessage({ data: JSON.stringify({ type: 'capture_ended' }) });
    });
    expect(hook.result.current.isRecording).toBe(true);
    expect(hook.result.current.isProcessing).toBe(false);
    expect(hook.result.current.error).toBeNull();
    expect(contexts[1].processor.disconnect).not.toHaveBeenCalled();
  });

  it('auth failure releases capture, settles the attempt and permits a fresh retry', async () => {
    auth.getWebSocketAuthToken.mockRejectedValueOnce(new Error('synthetic auth failure'));
    const hook = mount();
    await act(async () => { await hook.result.current.startRecording(); });
    expect(streams[0].track.stop).toHaveBeenCalledTimes(1);
    expect(contexts[0].close).toHaveBeenCalledTimes(1);
    expect(hook.result.current.isBusy).toBe(false);
    expect(hook.result.current.error).toBeTruthy();
    await openTurn(hook);
    expect(hook.result.current.error).toBeNull();
    expect(hook.result.current.isRecording).toBe(true);
  });

  it('a cancelled auth rejection cannot fail the newer active turn', async () => {
    const token = deferred(); auth.getWebSocketAuthToken.mockReturnValueOnce(token.promise);
    const hook = mount(); act(() => { void hook.result.current.startRecording(); });
    await flush(); hook.rerender({ enabled: false }); hook.rerender({ enabled: true });
    const current = await openTurn(hook);
    await act(async () => { token.reject(new Error('retired auth error')); });
    expect(current.close).not.toHaveBeenCalled();
    expect(hook.result.current.error).toBeNull();
    expect(hook.result.current.isRecording).toBe(true);
  });

  it.each([false, true])('cancels active capture without submission and preserves borrowed ownership (shared=%s)', async shared => {
    const stream = makeStream();
    navigator.mediaDevices.getUserMedia.mockResolvedValue(stream);
    const hook = mount(vi.fn(), shared ? { existingStream: stream } : {});
    const ws = await openTurn(hook);
    hook.rerender({ enabled: false });
    expect(stream.track.stop).toHaveBeenCalledTimes(shared ? 0 : 1);
    expect(contexts[0].processor.disconnect).toHaveBeenCalledTimes(1);
    expect(contexts[0].close).toHaveBeenCalledTimes(1);
    expect(ws.close).toHaveBeenCalledTimes(1);
    expect(ws.send.mock.calls.map(([data]) => JSON.parse(data).type)).toEqual(['ptt_start']);
  });
  it.each(['hidden-tab', 'low-battery'])('retires a connecting prewarm on %s and starts an independent live turn', async kind => {
    const signal = idleRetirementSignal(kind);
    const hook = mount();
    act(() => { hook.result.current.prewarmConnection(); }); await flush();
    const old = sockets[0];
    act(() => { signal.retire(); signal.restore(); });
    expect(old.close).toHaveBeenCalledTimes(1);
    let pending;
    act(() => { pending = hook.result.current.startRecording(); }); await flush();
    expect(sockets).toHaveLength(2);
    const current = sockets[1];
    // Retired close events cannot cancel the new connection's microphone.
    act(() => { old.onclose({ code: 1000 }); }); await flush();
    expect(current.close).not.toHaveBeenCalled();
    expect(streams[0].track.stop).not.toHaveBeenCalled();
    await act(async () => { current.open(); await pending; });
    expect(hook.result.current.isRecording).toBe(true);
    expect(hook.result.current.isBusy).toBe(true);
    act(() => { hook.result.current.cancelRecording(); });
    expect(streams[0].track.stop).toHaveBeenCalledTimes(1);
    expect(contexts[0].close).toHaveBeenCalledTimes(1);
  });

  it.each(['hidden-tab', 'low-battery'])('releases capture if the replacement for a %s prewarm also closes', async kind => {
    const signal = idleRetirementSignal(kind);
    const hook = mount();
    act(() => { hook.result.current.prewarmConnection(); }); await flush();
    const old = sockets[0];
    act(() => { signal.retire(); signal.restore(); });
    let pending;
    act(() => { pending = hook.result.current.startRecording(); }); await flush();
    expect(sockets).toHaveLength(2);
    act(() => { old.onclose({ code: 1000 }); sockets[1].onclose({ code: 1006 }); });
    await act(async () => { await pending; });
    expect({
      stoppedTracks: streams[0].track.stop.mock.calls.length,
      closedContexts: contexts[0].close.mock.calls.length,
      isBusy: hook.result.current.isBusy,
      isRecording: hook.result.current.isRecording,
    }).toEqual({ stoppedTracks: 1, closedContexts: 1, isBusy: false, isRecording: false });
  });

  it.each(['hidden-tab', 'low-battery'])('retires pending prewarm auth on %s before it can construct a socket', async kind => {
    const signal = idleRetirementSignal(kind);
    const oldToken = deferred(); auth.getWebSocketAuthToken.mockReturnValueOnce(oldToken.promise);
    const hook = mount();
    act(() => { hook.result.current.prewarmConnection(); }); await flush();
    act(() => { signal.retire(); signal.restore(); });
    const current = await openTurn(hook);
    await act(async () => { oldToken.resolve('retired-prewarm'); });
    expect(auth.getWebSocketAuthToken).toHaveBeenCalledTimes(2);
    expect(sockets).toHaveLength(1);
    expect(current.close).not.toHaveBeenCalled();
    expect(hook.result.current.isRecording).toBe(true);
  });

  it('retires a late permission grant when the prewarmed socket drops, and permits explicit retry', async () => {
    const hook = mount();
    act(() => { hook.result.current.prewarmConnection(); }); await flush();
    act(() => { sockets[0].open(); }); await flush();
    const permission = deferred(); navigator.mediaDevices.getUserMedia.mockReturnValueOnce(permission.promise);
    let pending;
    act(() => { pending = hook.result.current.startRecording(); });
    act(() => { sockets[0].onclose({ code: 1006 }); });
    const late = makeStream();
    await act(async () => { permission.resolve(late); await pending; });
    expect(late.track.stop).toHaveBeenCalledTimes(1);
    expect(sockets).toHaveLength(1);
    expect(contexts).toHaveLength(0);
    expect(hook.result.current.isBusy).toBe(false);
    await openTurn(hook);
    expect(sockets).toHaveLength(2);
    expect(hook.result.current.isRecording).toBe(true);
  });

  it('a dropped owner releases its late permission without stopping a newer turn', async () => {
    const hook = mount();
    act(() => { hook.result.current.prewarmConnection(); }); await flush();
    act(() => { sockets[0].open(); }); await flush();
    const permission = deferred(); navigator.mediaDevices.getUserMedia.mockReturnValueOnce(permission.promise);
    let oldPending;
    act(() => { oldPending = hook.result.current.startRecording(); });
    act(() => { sockets[0].onclose({ code: 1006 }); });
    const current = await openTurn(hook);
    const late = makeStream();
    await act(async () => { permission.resolve(late); await oldPending; });
    expect(late.track.stop).toHaveBeenCalledTimes(1);
    expect(streams[0].track.stop).not.toHaveBeenCalled();
    expect(contexts[0].close).not.toHaveBeenCalled();
    expect(current.close).not.toHaveBeenCalled();
    expect(hook.result.current.isRecording).toBe(true);
  });

  it('cleans its current capture if the opened socket is already closed before continuation', async () => {
    const hook = mount();
    let pending;
    act(() => { pending = hook.result.current.startRecording(); }); await flush();
    await act(async () => {
      sockets[0].open();
      sockets[0].readyState = Socket.CLOSED;
      await pending;
    });
    expect(sockets[0].send).not.toHaveBeenCalled();
    expect(streams[0].track.stop).toHaveBeenCalledTimes(1);
    expect(contexts[0].close).toHaveBeenCalledTimes(1);
    expect(hook.result.current.isBusy).toBe(false);
    expect(hook.result.current.isRecording).toBe(false);
  });

  it.each(['hidden-tab', 'low-battery'])('cancel and re-enable stay independent after %s retires a prewarm', async kind => {
    const signal = idleRetirementSignal(kind);
    const hook = mount();
    act(() => { hook.result.current.prewarmConnection(); }); await flush();
    const old = sockets[0];
    act(() => { signal.retire(); signal.restore(); });
    let pending;
    act(() => { pending = hook.result.current.startRecording(); }); await flush();
    const cancelled = sockets[1];
    expect(cancelled).toBeTruthy();
    hook.rerender({ enabled: false }); hook.rerender({ enabled: true });
    const current = await openTurn(hook);
    await act(async () => {
      old.onclose({ code: 1000 }); cancelled.onclose({ code: 1000 }); await pending;
    });
    expect(streams[0].track.stop).toHaveBeenCalledTimes(1);
    expect(contexts[0].close).toHaveBeenCalledTimes(1);
    expect(streams[1].track.stop).not.toHaveBeenCalled();
    expect(contexts[1].close).not.toHaveBeenCalled();
    expect(current.close).not.toHaveBeenCalled();
    expect(hook.result.current.isRecording).toBe(true);
  });

});
