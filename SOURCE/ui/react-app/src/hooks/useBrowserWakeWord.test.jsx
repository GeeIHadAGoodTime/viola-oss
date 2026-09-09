/**
 * Ratchet gate — browser hands-free wake word capture discipline.
 *
 * Bug class: an "off" hands-free toggle that still captures audio (hot mic
 * without disclosure), or a disabled toggle that leaves the mic/AudioContext
 * alive. The gate fails on any implementation where:
 *   - enabled=false triggers getUserMedia / AudioContext / model loading, or
 *   - flipping enabled true->false leaves mic tracks running.
 *
 * The ONNX runtime and detector are mocked; this gate tests the capture
 * lifecycle contract, not inference (inference parity is proven by the
 * ViolaWake WASM parity oracle + the live Playwright oracle).
 */
import React from 'react';
import { render, act, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('onnxruntime-web/wasm', () => ({
  env: { wasm: {} },
}));

const detectMock = vi.fn(async () => false);
const loadMock = vi.fn(async () => {});
const disposeMock = vi.fn();
const resetMock = vi.fn();

vi.mock('../lib/wake', () => ({
  WakeDetector: class {
    constructor() {
      this.lastScore = 0;
    }

    load = loadMock;

    detect = detectMock;

    reset = resetMock;

    dispose = disposeMock;
  },
}));

import { useBrowserWakeWord } from './useBrowserWakeWord';
import { useHandsFreeWake, HANDS_FREE_WAKE_KEY } from './useHandsFreeWake';

function HookProbe({ enabled, onState }) {
  const state = useBrowserWakeWord({ enabled });
  onState(state);
  return null;
}

describe('hands-free wake capture gate', () => {
  let getUserMedia;
  let audioContextCtor;
  let stopTrack;
  let closeCtx;

  beforeEach(() => {
    stopTrack = vi.fn();
    getUserMedia = vi.fn(async () => ({
      getTracks: () => [{ stop: stopTrack, readyState: 'live' }],
      getAudioTracks: () => [{ stop: stopTrack, readyState: 'live' }],
      active: true,
    }));
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia },
    });

    closeCtx = vi.fn(async () => {});
    audioContextCtor = vi.fn(function AudioContextMock() {
      this.state = 'running';
      this.sampleRate = 16000;
      this.audioWorklet = { addModule: vi.fn(async () => {}) };
      this.createMediaStreamSource = vi.fn(() => ({ connect: vi.fn(), disconnect: vi.fn() }));
      this.resume = vi.fn(async () => {});
      this.close = closeCtx;
    });
    vi.stubGlobal('AudioContext', audioContextCtor);
    vi.stubGlobal('AudioWorkletNode', vi.fn(function AudioWorkletNodeMock() {
      this.port = { onmessage: null, postMessage: vi.fn() };
      this.connect = vi.fn();
      this.disconnect = vi.fn();
    }));
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('toggle OFF means ZERO capture: no getUserMedia, no AudioContext, no model load', async () => {
    let state = null;
    render(<HookProbe enabled={false} onState={(s) => { state = s; }} />);

    // Give any (buggy) async init a chance to run before asserting.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(getUserMedia).not.toHaveBeenCalled();
    expect(audioContextCtor).not.toHaveBeenCalled();
    expect(loadMock).not.toHaveBeenCalled();
    expect(state.status).toBe('off');
  });

  it('enabling starts capture; disabling stops every mic track and closes the AudioContext', async () => {
    let state = null;
    const { rerender } = render(<HookProbe enabled onState={(s) => { state = s; }} />);

    await waitFor(() => expect(state.status).toBe('listening'));
    expect(getUserMedia).toHaveBeenCalledTimes(1);
    expect(loadMock).toHaveBeenCalledTimes(1);

    rerender(<HookProbe enabled={false} onState={(s) => { state = s; }} />);
    await waitFor(() => expect(state.status).toBe('off'));

    expect(stopTrack).toHaveBeenCalled();
    expect(closeCtx).toHaveBeenCalled();
    expect(disposeMock).toHaveBeenCalled();
  });

  it('hands-free preference defaults OFF and is device-local (localStorage)', () => {
    let value = null;
    function ToggleProbe() {
      const [enabled] = useHandsFreeWake();
      value = enabled;
      return null;
    }
    render(<ToggleProbe />);
    expect(value).toBe(false);
    // Stored under a device-local key, never in cloud-synced settings.
    expect(window.localStorage.getItem(HANDS_FREE_WAKE_KEY)).toBeNull();
  });
});
