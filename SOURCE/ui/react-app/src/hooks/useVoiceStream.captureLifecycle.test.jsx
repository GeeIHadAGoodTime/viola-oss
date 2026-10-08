import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useVoiceStream } from './useVoiceStream';
import { getWebSocketAuthToken } from '../lib/ws_auth';
import { playTtsPcm } from '../utils/ttsPlayback';

vi.mock('../lib/ws_auth', () => ({ getWebSocketAuthToken: vi.fn() }));
vi.mock('../utils/ttsPlayback', () => ({
  isTtsFrame: () => true,
  decodeTtsFrame: () => ({ pcmBuffer: new ArrayBuffer(4), sampleRate: 16000 }),
  playTtsPcm: vi.fn(),
}));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
let streams, contexts, sockets;
const makeStream = () => {
  const stream = { active: true };
  stream.stop = vi.fn(() => { stream.active = false; });
  stream.getTracks = () => [{ stop: stream.stop }];
  streams.push(stream);
  return stream;
};
class Context {
  constructor() {
    this.sampleRate = 16000;
    this.destination = {};
    this.source = { connect: vi.fn(), disconnect: vi.fn() };
    this.processor = { connect: vi.fn(), disconnect: vi.fn() };
    this.close = vi.fn(async () => {});
    contexts.push(this);
  }
  createMediaStreamSource() { return this.source; }
  createScriptProcessor() { return this.processor; }
}
class Socket {
  static OPEN = 1;
  constructor(url) { this.url = url; this.readyState = 0; this.send = vi.fn(); sockets.push(this); }
  open() { this.readyState = 1; this.onopen?.(); }
  close() { this.readyState = 3; }
}
const start = async (result) => { await act(async () => { await result.current.startStreaming(); }); };
const open = async (socket = sockets.at(-1)) => { await act(async () => { socket.open(); }); };

beforeEach(() => {
  streams = []; contexts = []; sockets = [];
  getWebSocketAuthToken.mockReset().mockResolvedValue('synthetic-token');
  playTtsPcm.mockClear();
  vi.stubGlobal('AudioContext', Context);
  vi.stubGlobal('WebSocket', Socket);
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: vi.fn(async () => makeStream()) } });
});
afterEach(() => { vi.unstubAllGlobals(); delete navigator.mediaDevices; });

describe('continuous spoke capture ownership', () => {
  it('keeps a deliberate enabled turn working with PCM, wake, transcript and TTS', async () => {
    const onWakeDetected = vi.fn(), onTranscription = vi.fn();
    const { result } = renderHook(() => useVoiceStream({ onWakeDetected, onTranscription }));
    await start(result); await open();
    const socket = sockets[0];
    expect(result.current.isStreaming).toBe(true);
    contexts[0].processor.onaudioprocess({ inputBuffer: { getChannelData: () => new Float32Array([0.5, -0.5]) } });
    socket.onmessage({ data: JSON.stringify({ type: 'wake_detected', payload: { wake: true } }) });
    socket.onmessage({ data: JSON.stringify({ type: 'command_result', payload: { transcript: 'hello' } }) });
    socket.onmessage({ data: new ArrayBuffer(8) });
    expect(socket.send.mock.calls[1][0]).toBeInstanceOf(ArrayBuffer);
    expect(onWakeDetected).toHaveBeenCalledOnce(); expect(onTranscription).toHaveBeenCalledOnce(); expect(playTtsPcm).toHaveBeenCalledOnce();
    act(() => result.current.stopStreaming());
    expect(streams[0].stop).toHaveBeenCalledOnce(); expect(contexts[0].close).toHaveBeenCalledOnce();
  });

  it.each(['stop', 'unmount', 'disable'])('releases a late permission grant after %s without creating audio or transport', async (action) => {
    const permission = deferred(); navigator.mediaDevices.getUserMedia.mockReturnValueOnce(permission.promise);
    const acquired = vi.fn();
    const { result, rerender, unmount } = renderHook(({ enabled }) => useVoiceStream({ enabled, onStreamAcquired: acquired }), { initialProps: { enabled: true } });
    let pending; act(() => { pending = result.current.startStreaming(); });
    if (action === 'stop') act(() => result.current.stopStreaming());
    if (action === 'unmount') unmount();
    if (action === 'disable') rerender({ enabled: false });
    const late = makeStream(); await act(async () => { permission.resolve(late); await pending; });
    expect(late.stop).toHaveBeenCalledOnce(); expect(contexts).toHaveLength(0); expect(sockets).toHaveLength(0); expect(acquired).not.toHaveBeenCalled();
  });

  it('disabled capture cannot ask for permission', async () => {
    const { result } = renderHook(() => useVoiceStream({ enabled: false }));
    await start(result); expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
  });

  it('does not open a socket from an auth completion after stop', async () => {
    const auth = deferred(); getWebSocketAuthToken.mockReturnValueOnce(auth.promise);
    const { result } = renderHook(() => useVoiceStream());
    let pending; await act(async () => { pending = result.current.startStreaming(); });
    act(() => result.current.stopStreaming());
    await act(async () => { auth.resolve('late-token'); await pending; });
    expect(streams[0].stop).toHaveBeenCalledOnce(); expect(contexts[0].close).toHaveBeenCalledOnce(); expect(sockets).toHaveLength(0);
  });

  it('coalesces repeated starts while permission is pending', async () => {
    const permission = deferred(); navigator.mediaDevices.getUserMedia.mockReturnValue(permission.promise);
    const { result } = renderHook(() => useVoiceStream());
    let first, second; act(() => { first = result.current.startStreaming(); second = result.current.startStreaming(); });
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledOnce();
    await act(async () => { permission.resolve(makeStream()); await Promise.all([first, second]); });
    expect(sockets).toHaveLength(1);
  });

  it('does not relinquish ownership when the caller echoes an acquired stream back', async () => {
    const acquired = vi.fn();
    const { result, rerender } = renderHook(({ existingStream }) => useVoiceStream({ existingStream, onStreamAcquired: acquired }), { initialProps: {} });
    await start(result); await open();
    rerender({ existingStream: acquired.mock.calls[0][0] });
    act(() => result.current.stopStreaming());
    expect(streams[0].stop).toHaveBeenCalledOnce();
  });

  it('preserves a stream borrowed from the caller', async () => {
    const shared = makeStream();
    const { result } = renderHook(() => useVoiceStream({ existingStream: shared }));
    await start(result); await open(); act(() => result.current.stopStreaming());
    expect(shared.stop).not.toHaveBeenCalled(); expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
  });

  it('ignores retired socket/PCM callbacks and lets a newer stream remain active', async () => {
    const onWakeDetected = vi.fn(), onTranscription = vi.fn();
    const { result } = renderHook(() => useVoiceStream({ onWakeDetected, onTranscription }));
    await start(result); await open();
    const oldSocket = sockets[0], oldContext = contexts[0];
    const callbacks = { open: oldSocket.onopen, close: oldSocket.onclose, error: oldSocket.onerror, message: oldSocket.onmessage, pcm: oldContext.processor.onaudioprocess };
    act(() => result.current.stopStreaming());
    await start(result); await open();
    const before = oldSocket.send.mock.calls.length;
    await act(async () => {
      // Simulate already-queued callbacks, including a transport that still reports OPEN.
      oldSocket.readyState = Socket.OPEN;
      callbacks.open(); callbacks.pcm({ inputBuffer: { getChannelData: () => new Float32Array([0.5]) } });
      callbacks.message({ data: JSON.stringify({ type: 'wake_detected' }) });
      callbacks.message({ data: JSON.stringify({ type: 'command_result' }) });
      callbacks.message({ data: new ArrayBuffer(8) }); callbacks.close(); callbacks.error();
    });
    expect(oldSocket.send).toHaveBeenCalledTimes(before);
    expect(onWakeDetected).not.toHaveBeenCalled(); expect(onTranscription).not.toHaveBeenCalled(); expect(playTtsPcm).not.toHaveBeenCalled();
    expect(result.current.isStreaming).toBe(true); expect(streams[1].stop).not.toHaveBeenCalled();
  });

  it('ignores an old permission rejection without stopping a newer live turn', async () => {
    const permission = deferred(); navigator.mediaDevices.getUserMedia.mockReturnValueOnce(permission.promise);
    const { result } = renderHook(() => useVoiceStream());
    let pending; act(() => { pending = result.current.startStreaming(); }); act(() => result.current.stopStreaming());
    await start(result); await open();
    await act(async () => { permission.reject(new Error('old denial')); await pending; });
    expect(result.current.isStreaming).toBe(true); expect(streams[0].stop).not.toHaveBeenCalled();
  });
  it('late permission from a cancelled turn cannot replace a newer active microphone', async () => {
    const permission = deferred(); navigator.mediaDevices.getUserMedia.mockReturnValueOnce(permission.promise);
    const { result } = renderHook(() => useVoiceStream());
    let old; act(() => { old = result.current.startStreaming(); });
    act(() => result.current.stopStreaming());
    await start(result); await open();
    const active = streams[0], late = makeStream();
    await act(async () => { permission.resolve(late); await old; });
    expect(late.stop).toHaveBeenCalledOnce(); expect(active.stop).not.toHaveBeenCalled();
    expect(contexts).toHaveLength(1); expect(sockets).toHaveLength(1); expect(result.current.isStreaming).toBe(true);
  });

  it('releases active capture on disable and permits a fresh explicit turn after re-enable', async () => {
    const { result, rerender } = renderHook(({ enabled }) => useVoiceStream({ enabled }), { initialProps: { enabled: true } });
    await start(result); await open();
    rerender({ enabled: false });
    expect(streams[0].stop).toHaveBeenCalledOnce(); expect(contexts[0].close).toHaveBeenCalledOnce(); expect(sockets[0].readyState).toBe(3);
    await start(result); expect(streams).toHaveLength(1);
    rerender({ enabled: true }); await start(result); await open();
    expect(result.current.isStreaming).toBe(true); expect(streams).toHaveLength(2); expect(streams[1].stop).not.toHaveBeenCalled();
  });

  it('late authentication after unmount cannot create transport', async () => {
    const auth = deferred(); getWebSocketAuthToken.mockReturnValueOnce(auth.promise);
    const { result, unmount } = renderHook(() => useVoiceStream());
    let pending; await act(async () => { pending = result.current.startStreaming(); }); unmount();
    await act(async () => { auth.resolve('late-token'); await pending; });
    expect(sockets).toHaveLength(0); expect(streams[0].stop).toHaveBeenCalledOnce(); expect(contexts[0].close).toHaveBeenCalledOnce();
  });

  it('keeps borrowed ownership stable if the caller replaces its stream prop', async () => {
    const shared = makeStream();
    const { result, rerender } = renderHook(({ existingStream }) => useVoiceStream({ existingStream }), { initialProps: { existingStream: shared } });
    await start(result); await open(); rerender({ existingStream: null }); act(() => result.current.stopStreaming());
    expect(shared.stop).not.toHaveBeenCalled();
  });

  it.each(['context', 'auth', 'socket'])('releases owned microphone resources on %s startup failure', async (stage) => {
    const error = new Error('synthetic startup failure');
    const log = vi.spyOn(console, 'error').mockImplementation(() => {});
    if (stage === 'context') vi.stubGlobal('AudioContext', class { constructor() { throw error; } });
    if (stage === 'auth') getWebSocketAuthToken.mockRejectedValueOnce(error);
    if (stage === 'socket') vi.stubGlobal('WebSocket', class { constructor() { throw error; } });
    const { result } = renderHook(() => useVoiceStream());
    await start(result);
    expect(streams[0].stop).toHaveBeenCalledOnce(); expect(result.current.isStreaming).toBe(false);
    if (contexts[0]) expect(contexts[0].close).toHaveBeenCalledOnce();
    log.mockRestore();
  });

});
