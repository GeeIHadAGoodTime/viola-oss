// Proof for LEVERAGE_RANKING quick-kill #4 (source: docs/audit/silent_failures.md F4):
// useVoice.js:386 - cancelRecording()'s unduck POST had `.catch(() => {})`, so a
// failed unduck request left audio silently stuck ducked (quiet) with zero
// diagnostic trail. This proves the failure is now logged via console.error
// instead of being silently dropped.

import { renderHook, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./useViolaApi', () => ({
  authFetch: vi.fn(),
}));

describe('useVoice cancelRecording unduck failure logging', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('logs to console when the post-cancel unduck request fails, instead of silently dropping it', async () => {
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { authFetch } = await import('./useViolaApi');
    authFetch.mockRejectedValue(new Error('simulated /v1/audio/unduck failure'));

    const { useVoice } = await import('./useVoice');
    const { result } = renderHook(() => useVoice());

    result.current.cancelRecording();

    // Flush the microtask queue so the rejected authFetch promise's .catch runs.
    await new Promise((resolve) => setTimeout(resolve, 0));
    await Promise.resolve();
    await Promise.resolve();

    expect(authFetch).toHaveBeenCalledWith('/v1/audio/unduck', { method: 'POST' });
    expect(consoleErrorSpy).toHaveBeenCalledWith(
      '[useVoice] Unduck request failed; audio may stay ducked (quiet):',
      expect.any(Error)
    );
  });
});


describe('voice capture enablement', () => {
  let track;
  let stream;
  let recorderInstances;
  beforeEach(async () => {
    vi.resetModules();
    track = { stop: vi.fn(), readyState: 'live' };
    stream = { active: true, getTracks: () => [track], getAudioTracks: () => [track] };
    recorderInstances = [];
    vi.stubGlobal('MediaRecorder', class {
      static isTypeSupported() { return true; }
      constructor() { this.state = 'inactive'; recorderInstances.push(this); }
      start() { this.state = 'recording'; }
      stop() { this.state = 'inactive'; this.onstop?.(); }
    });
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: vi.fn(async () => stream) } });
    const { authFetch } = await import('./useViolaApi');
    authFetch.mockReset();
    authFetch.mockResolvedValue({ json: async () => ({ ok: true, data: { text: 'synthetic' } }) });
  });
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it('does not request microphone access while disabled', async () => {
    const { useVoice } = await import('./useVoice');
    const { result } = renderHook(() => useVoice(undefined, { enabled: false }));
    await act(async () => { await result.current.startRecording(); });
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
    expect(recorderInstances).toHaveLength(0);
  });

  it('releases a late microphone grant after disable and never creates a recorder', async () => {
    let release;
    navigator.mediaDevices.getUserMedia.mockImplementation(() => new Promise(resolve => { release = resolve; }));
    const { useVoice } = await import('./useVoice');
    const { result, rerender } = renderHook(({ enabled }) => useVoice(undefined, { enabled }), { initialProps: { enabled: true } });
    let pending;
    act(() => { pending = result.current.startRecording(); });
    rerender({ enabled: false });
    await act(async () => { release(stream); await pending; });
    expect(track.stop).toHaveBeenCalled();
    expect(recorderInstances).toHaveLength(0);
    expect(result.current.isRecording).toBe(false);
  });

  it('cancels active recording without transcription and can resume only after re-enabled', async () => {
    const { useVoice } = await import('./useVoice');
    const { authFetch } = await import('./useViolaApi');
    const { result, rerender } = renderHook(({ enabled }) => useVoice(undefined, { enabled }), { initialProps: { enabled: true } });
    await act(async () => { await result.current.startRecording(); });
    expect(result.current.isRecording).toBe(true);
    rerender({ enabled: false });
    expect(result.current.isRecording).toBe(false);
    expect(track.stop).toHaveBeenCalled();
    expect(authFetch.mock.calls.some(([url]) => url === '/v1/transcribe')).toBe(false);
    await act(async () => { await result.current.startRecording(); });
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1);
    rerender({ enabled: true });
    await act(async () => { await result.current.startRecording(); });
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2);
    act(() => result.current.cancelRecording());
  });
});
