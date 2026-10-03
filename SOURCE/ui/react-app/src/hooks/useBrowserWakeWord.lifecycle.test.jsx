import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useBrowserWakeWord } from './useBrowserWakeWord';

const engine = vi.hoisted(() => ({ instances: [], nextLoad: null }));
vi.mock('onnxruntime-web/wasm', () => ({ env: { wasm: {} } }));
vi.mock('../lib/wake', () => ({ WakeDetector: class {
  constructor() {
    this.load = vi.fn(() => engine.nextLoad || Promise.resolve());
    this.detect = vi.fn(async () => false);
    this.dispose = vi.fn();
    this.reset = vi.fn();
    this.lastScore = 0;
    engine.instances.push(this);
  }
} }));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const makeStream = () => {
  const track = { stop: vi.fn() };
  return { track, getTracks: () => [track] };
};
let getUserMedia, streams, contexts, worklets, onWake, onStreamReady;
beforeEach(() => {
  engine.instances = [];
  engine.nextLoad = null;
  streams = []; contexts = []; worklets = [];
  onWake = vi.fn(); onStreamReady = vi.fn();
  getUserMedia = vi.fn(async () => { const stream = makeStream(); streams.push(stream); return stream; });
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } });
  vi.stubGlobal('AudioContext', class {
    constructor() {
      this.state = 'running';
      this.close = vi.fn(async () => {});
      this.audioWorklet = { addModule: vi.fn(async () => {}) };
      this.source = { connect: vi.fn(), disconnect: vi.fn() };
      this.createMediaStreamSource = () => this.source;
      contexts.push(this);
    }
  });
  vi.stubGlobal('AudioWorkletNode', class {
    constructor() {
      this.port = { onmessage: null, postMessage: vi.fn() };
      this.disconnect = vi.fn();
      worklets.push(this);
    }
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const start = (enabled = true) => renderHook(
  (props) => useBrowserWakeWord({ ...props, onWake, onStreamReady }),
  { initialProps: { enabled } },
);
const frame = { data: new Float32Array([0, 0, 0]) };

describe('browser wake cancellation and stale callbacks', () => {
  it('stops late permission tracks without publishing or using them after disable', async () => {
    const permission = deferred();
    getUserMedia.mockReturnValueOnce(permission.promise);
    const { result, rerender } = start();
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1));
    rerender({ enabled: false });
    const late = makeStream();
    await act(async () => { permission.resolve(late); });
    expect(late.track.stop).toHaveBeenCalledTimes(1);
    expect(contexts).toHaveLength(0);
    expect(onStreamReady).not.toHaveBeenCalledWith(late);
    expect(result.current.status).toBe('off');
  });

  it('an old permission response cannot stop a new capture after disable/re-enable', async () => {
    const permission = deferred();
    getUserMedia.mockReturnValueOnce(permission.promise);
    const { result, rerender } = start();
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1));
    rerender({ enabled: false });
    rerender({ enabled: true });
    await waitFor(() => expect(result.current.status).toBe('listening'));
    const late = makeStream();
    await act(async () => { permission.resolve(late); });
    expect(late.track.stop).toHaveBeenCalledTimes(1);
    expect(streams[0].track.stop).not.toHaveBeenCalled();
    expect(result.current.status).toBe('listening');
  });

  it('stops every active capture resource and ignores in-flight inference after disable', async () => {
    const pendingDetect = deferred();
    const { result, rerender } = start();
    await waitFor(() => expect(result.current.status).toBe('listening'));
    engine.instances[0].detect.mockReturnValueOnce(pendingDetect.promise);
    act(() => { worklets[0].port.onmessage(frame); });
    rerender({ enabled: false });
    expect(streams[0].track.stop).toHaveBeenCalledTimes(1);
    expect(contexts[0].close).toHaveBeenCalledTimes(1);
    expect(contexts[0].source.disconnect).toHaveBeenCalledTimes(1);
    expect(worklets[0].disconnect).toHaveBeenCalledTimes(1);
    expect(worklets[0].port.onmessage).toBeNull();
    await act(async () => { pendingDetect.resolve(true); });
    expect(onWake).not.toHaveBeenCalled();
    expect(result.current.status).toBe('off');
  });

  it('disposes a model that finishes loading after disable without acquiring a mic', async () => {
    const loading = deferred(); engine.nextLoad = loading.promise;
    const { result, rerender } = start();
    await waitFor(() => expect(engine.instances).toHaveLength(1));
    rerender({ enabled: false });
    await act(async () => { loading.resolve(); });
    expect(engine.instances[0].dispose).toHaveBeenCalledTimes(1);
    expect(getUserMedia).not.toHaveBeenCalled();
    expect(result.current.status).toBe('off');
  });

  it('ignores a saved old worklet callback after disable/re-enable', async () => {
    const { result, rerender } = start();
    await waitFor(() => expect(result.current.status).toBe('listening'));
    const staleCallback = worklets[0].port.onmessage;
    rerender({ enabled: false });
    rerender({ enabled: true });
    await waitFor(() => expect(worklets).toHaveLength(2));
    await act(async () => { staleCallback(frame); });
    expect(engine.instances[1].detect).not.toHaveBeenCalled();
    expect(onWake).not.toHaveBeenCalled();
  });

  it('does not let old inference completion unlock a newer in-flight inference', async () => {
    const oldDetect = deferred(), newDetect = deferred();
    const { result, rerender } = start();
    await waitFor(() => expect(result.current.status).toBe('listening'));
    engine.instances[0].detect.mockReturnValueOnce(oldDetect.promise);
    act(() => { worklets[0].port.onmessage(frame); });
    rerender({ enabled: false }); rerender({ enabled: true });
    await waitFor(() => expect(worklets).toHaveLength(2));
    engine.instances[1].detect.mockReturnValueOnce(newDetect.promise);
    act(() => { worklets[1].port.onmessage(frame); });
    await act(async () => { oldDetect.resolve(false); });
    act(() => { worklets[1].port.onmessage(frame); });
    expect(engine.instances[1].detect).toHaveBeenCalledTimes(1);
    await act(async () => { newDetect.resolve(false); });
    expect(engine.instances[1].detect).toHaveBeenCalledTimes(2);
  });

  it('a deliberate re-enable clears suppression from an earlier wake', async () => {
    const { result, rerender } = start();
    await waitFor(() => expect(result.current.status).toBe('listening'));
    engine.instances[0].detect.mockResolvedValue(true);
    await act(async () => { worklets[0].port.onmessage(frame); });
    expect(onWake).toHaveBeenCalledTimes(1);
    rerender({ enabled: false }); rerender({ enabled: true });
    await waitFor(() => expect(worklets).toHaveLength(2));
    engine.instances[1].detect.mockResolvedValue(true);
    await act(async () => { worklets[1].port.onmessage(frame); });
    expect(onWake).toHaveBeenCalledTimes(2);
  });
});
