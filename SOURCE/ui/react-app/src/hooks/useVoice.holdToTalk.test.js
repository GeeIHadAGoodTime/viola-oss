/**
 * Touch hold-to-talk on the desktop (1.0.5 build 5): both held clips (~24 KB,
 * about 1.5 s of webm/opus) came back "Transcription returned empty result".
 *
 * The client-side silence auto-stop (1.5 s below the floor) is meant for a
 * quick tap; it also ran during a deliberate hold, and a release that came
 * while the microphone was still opening was dropped, so the take recorded
 * only the silence after the finger lifted. These pin the fixed behaviour.
 */
import { renderHook, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./useViolaApi', () => ({ authFetch: vi.fn() }));

let level;
let track;
let stream;
let recorders;
let authFetch;

beforeEach(async () => {
  vi.resetModules();
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout'] });
  vi.spyOn(window.performance, 'now').mockImplementation(() => Date.now());
  vi.stubGlobal('requestAnimationFrame', (cb) => setTimeout(() => cb(Date.now()), 50));
  vi.stubGlobal('cancelAnimationFrame', (id) => clearTimeout(id));
  level = 0.05;
  track = { stop: vi.fn(), readyState: 'live' };
  stream = { active: true, getTracks: () => [track], getAudioTracks: () => [track] };
  recorders = [];
  vi.stubGlobal('MediaRecorder', class {
    static isTypeSupported() { return true; }
    constructor() { this.state = 'inactive'; this.mimeType = 'audio/webm'; recorders.push(this); }
    start() { this.state = 'recording'; }
    stop() {
      this.state = 'inactive';
      this.ondataavailable?.({ data: new Blob([new Uint8Array(24000)]) });
      this.onstop?.();
    }
  });
  vi.stubGlobal('AudioContext', class {
    constructor() { this.state = 'running'; }
    createMediaStreamSource() { return { connect() {} }; }
    createAnalyser() { return { fftSize: 256, getFloatTimeDomainData(data) { data.fill(level); } }; }
    resume() { return Promise.resolve(); }
    close() { this.state = 'closed'; return Promise.resolve(); }
  });
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: vi.fn(async () => stream) },
  });
  ({ authFetch } = await import('./useViolaApi'));
  authFetch.mockReset();
  authFetch.mockImplementation(async (path) => ({
    json: async () => (path === '/v1/transcribe'
      ? { ok: true, data: { text: 'play drake' } }
      : { ok: true, data: {} }),
  }));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const transcribeCalls = () => authFetch.mock.calls.filter(([path]) => path === '/v1/transcribe');

async function mount() {
  const { useVoice } = await import('./useVoice');
  return renderHook(() => useVoice(vi.fn(), { executeCommand: false }));
}

describe('hold-to-talk', () => {
  it('keeps recording through a pause while the button is held', async () => {
    const { result } = await mount();
    await act(async () => { await result.current.startRecording({ holdToTalk: true }); });
    expect(result.current.isRecording).toBe(true);

    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    level = 0.001; // the user pauses mid-sentence
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(recorders[0].state).toBe('recording');

    level = 0.05;
    await act(async () => { await result.current.stopRecording(); });
    expect(transcribeCalls()).toHaveLength(1);
  });

  it('still auto-stops after silence for a quick tap', async () => {
    const { result } = await mount();
    await act(async () => { await result.current.startRecording({ holdToTalk: true }); });
    act(() => { result.current.armSilenceStop(); }); // released within the tap threshold
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    level = 0.001;
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });

    expect(recorders[0].state).toBe('inactive');
    expect(transcribeCalls()).toHaveLength(1);
  });

  it('auto-stops on silence by default (wake / hotkey turns)', async () => {
    const { result } = await mount();
    await act(async () => { await result.current.startRecording(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    level = 0.001;
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(recorders[0].state).toBe('inactive');
  });
});

describe('release while the microphone is still opening', () => {
  it('does not record the silence after the release', async () => {
    let grant;
    navigator.mediaDevices.getUserMedia.mockImplementation(() => new Promise((resolve) => { grant = resolve; }));
    const { result } = await mount();

    let pending;
    act(() => { pending = result.current.startRecording({ holdToTalk: true }); });
    await act(async () => { await result.current.stopRecording(); }); // finger lifted
    await act(async () => { grant(stream); await pending; });

    expect(recorders).toHaveLength(0);
    expect(track.stop).toHaveBeenCalled();
    expect(result.current.isRecording).toBe(false);
    expect(result.current.error).toMatch(/Hold the mic button until it says recording/);
    expect(transcribeCalls()).toHaveLength(0);
  });
});

describe('a silent microphone', () => {
  it('says the microphone was silent instead of sending an empty clip', async () => {
    level = 0;
    const { result } = await mount();
    await act(async () => { await result.current.startRecording({ holdToTalk: true }); });
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    await act(async () => { await result.current.stopRecording(); });

    expect(transcribeCalls()).toHaveLength(0);
    expect(result.current.error).toMatch(/didn't hear anything from the microphone/);
    expect(result.current.isProcessing).toBe(false);
  });
});
