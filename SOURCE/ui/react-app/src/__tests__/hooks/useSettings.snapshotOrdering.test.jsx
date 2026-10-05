import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useSettings } from '../../hooks/useSettings';

const harness = vi.hoisted(() => ({ api: vi.fn(), receive: null, cloud: false }));
vi.mock('../../hooks/useViolaApi', () => ({ apiFetch: (...args) => harness.api(...args) }));
vi.mock('../../hooks/useWebSocket', () => ({ useWebSocket: receive => { harness.receive = receive; } }));
vi.mock('../../components/auth/cloudSurface', () => ({ isCloudSurface: () => harness.cloud }));

const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const oldSettings = { mic_muted: false, voice_mode: 'wake_word', theme: 'light' };
const savedSettings = { mic_muted: true, voice_mode: 'disabled', theme: 'dark' };
const payload = (settings, voiceStatus = null) => ({ ok: true, settings, voice_status: voiceStatus });
const load = async () => {
  const view = renderHook(() => useSettings());
  await waitFor(() => expect(view.result.current.loading).toBe(false));
  return view;
};
beforeEach(() => {
  harness.api.mockReset().mockResolvedValue(payload(oldSettings));
  harness.receive = null;
  harness.cloud = false;
});

describe('useSettings current snapshot acceptance', () => {
  it.each([false, true])('keeps a newer WebSocket privacy snapshot over an earlier read (cloud=%s)', async cloud => {
    harness.cloud = cloud;
    const read = deferred();
    harness.api.mockReturnValueOnce(read.promise);
    const { result } = renderHook(() => useSettings());
    const voice = { status: 'degraded', degraded: true, reason: 'synthetic-output-unavailable' };
    act(() => harness.receive({ type: 'settings_changed', payload: { settings: savedSettings, voice_status: voice } }));
    expect(result.current.settings).toEqual(savedSettings);
    await act(async () => read.resolve(payload(oldSettings, { status: 'ok', degraded: false, reason: null })));
    expect(result.current.settings).toEqual(savedSettings);
    expect(result.current.voiceStatus).toEqual(voice);
    expect(result.current.loading).toBe(false);
  });

  it('does not replace an acknowledged save with a previously started read', async () => {
    const { result } = await load();
    const read = deferred();
    harness.api.mockReturnValueOnce(read.promise).mockResolvedValueOnce(payload(savedSettings));
    act(() => { void result.current.refreshSettings(); });
    await act(async () => expect(await result.current.updateSettings(savedSettings)).toBe(true));
    expect(result.current.settings).toEqual(savedSettings);
    await act(async () => read.resolve(payload(oldSettings)));
    expect(result.current.settings).toEqual(savedSettings);
  });

  it('does not replace acknowledged reset defaults with a previously started read', async () => {
    const { result } = await load();
    const read = deferred();
    const defaults = { mic_muted: true, voice_mode: 'push_to_talk', theme: 'dark' };
    harness.api.mockReturnValueOnce(read.promise).mockResolvedValueOnce(payload(defaults));
    act(() => { void result.current.refreshSettings(); });
    await act(async () => expect(await result.current.resetSettings()).toBe(true));
    await act(async () => read.resolve(payload(oldSettings)));
    expect(result.current.settings).toEqual(defaults);
  });

  it('keeps loading owned by the latest refresh and never adopts its older predecessor', async () => {
    const { result } = await load();
    const first = deferred(), second = deferred();
    harness.api.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    act(() => { void result.current.refreshSettings(); void result.current.refreshSettings(); });
    await act(async () => first.resolve(payload({ theme: 'stale' })));
    expect(result.current.loading).toBe(true);
    expect(result.current.settings).toEqual(oldSettings);
    await act(async () => second.resolve(payload(savedSettings)));
    expect(result.current.loading).toBe(false);
    expect(result.current.settings).toEqual(savedSettings);
  });

  it('ignores an older read failure after a current snapshot has arrived', async () => {
    const read = deferred();
    harness.api.mockReturnValueOnce(read.promise);
    const { result } = renderHook(() => useSettings());
    act(() => harness.receive({ type: 'settings_changed', payload: { settings: savedSettings } }));
    await act(async () => read.reject(new Error('stale offline response')));
    expect(result.current.settings).toEqual(savedSettings);
    expect(result.current.error).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it('clears a failed load after a successful explicit retry', async () => {
    harness.api.mockRejectedValueOnce(new Error('offline'));
    const { result } = await load();
    expect(result.current.error).toBe('Failed to load settings');
    harness.api.mockResolvedValueOnce(payload(savedSettings));
    await act(async () => result.current.refreshSettings());
    expect(result.current.settings).toEqual(savedSettings);
    expect(result.current.error).toBeNull();
  });

  it('distinguishes an omitted voice status from an explicitly cleared status', async () => {
    harness.api.mockResolvedValueOnce(payload(oldSettings, { status: 'ok', degraded: false, reason: null }));
    const { result } = await load();
    act(() => harness.receive({ type: 'settings_changed', payload: { settings: savedSettings } }));
    expect(result.current.voiceStatus).toEqual({ status: 'ok', degraded: false, reason: null });
    act(() => harness.receive({ type: 'settings_changed', payload: { settings: savedSettings, voice_status: null } }));
    expect(result.current.voiceStatus).toBeNull();
  });

  it('retains a newer persistence error when an older read succeeds', async () => {
    const { result } = await load();
    const read = deferred();
    const error = Object.assign(new Error('synthetic refusal'), { code: 'cloud_sync_consent_not_recorded' });
    harness.api.mockReturnValueOnce(read.promise).mockRejectedValueOnce(error);
    act(() => { void result.current.refreshSettings(); });
    await act(async () => expect(await result.current.updateSettings({ cloud_sync_enabled: false })).toBe(false));
    const failure = result.current.error;
    expect(failure).toContain('Nothing was saved');
    await act(async () => read.resolve(payload(oldSettings)));
    expect(result.current.error).toBe(failure);
    expect(result.current.settings).toEqual(oldSettings);
  });

  it('does not let an older read failure replace a newer persistence error', async () => {
    const { result } = await load();
    const read = deferred();
    harness.api.mockReturnValueOnce(read.promise).mockRejectedValueOnce(new Error('write refused'));
    act(() => { void result.current.refreshSettings(); });
    await act(async () => expect(await result.current.updateSetting('theme', 'dark')).toBe(false));
    await act(async () => read.reject(new Error('older read refused')));
    expect(result.current.error).toBe('Failed to save settings');
  });

  it('allows a genuinely newer read after a pushed snapshot', async () => {
    const { result } = await load();
    act(() => harness.receive({ type: 'settings_changed', payload: { settings: savedSettings } }));
    const next = { ...savedSettings, theme: 'light' };
    harness.api.mockResolvedValueOnce(payload(next));
    await act(async () => result.current.refreshSettings());
    expect(result.current.settings).toEqual(next);
    expect(result.current.loading).toBe(false);
  });

  it('keeps the latest failed refresh visible when its older predecessor succeeds', async () => {
    const { result } = await load();
    const first = deferred(), second = deferred();
    harness.api.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    act(() => { void result.current.refreshSettings(); void result.current.refreshSettings(); });
    await act(async () => second.reject(new Error('latest unavailable')));
    await act(async () => first.resolve(payload(savedSettings)));
    expect(result.current.settings).toEqual(oldSettings);
    expect(result.current.error).toBe('Failed to load settings');
  });

  it('ignores unrelated or incomplete WebSocket messages without dropping a valid read', async () => {
    const read = deferred();
    harness.api.mockReturnValueOnce(read.promise);
    const { result } = renderHook(() => useSettings());
    act(() => {
      harness.receive({ type: 'state', payload: { settings: savedSettings } });
      harness.receive({ type: 'settings_changed', payload: { voice_status: null } });
    });
    await act(async () => read.resolve(payload(oldSettings)));
    expect(result.current.settings).toEqual(oldSettings);
    expect(result.current.loading).toBe(false);
  });

  it('does not leak a prior mounted instance into a fresh settings instance', async () => {
    const oldRead = deferred();
    harness.api.mockReturnValueOnce(oldRead.promise);
    const old = renderHook(() => useSettings());
    const oldReceive = harness.receive;
    old.unmount();
    harness.api.mockResolvedValueOnce(payload(savedSettings));
    const { result } = await load();
    act(() => oldReceive({ type: 'settings_changed', payload: { settings: oldSettings } }));
    await act(async () => oldRead.resolve(payload(oldSettings)));
    expect(result.current.settings).toEqual(savedSettings);
    expect(result.current.error).toBeNull();
  });

  it('keeps StrictMode cleanup and replay from reviving an old read failure', async () => {
    const first = deferred(), second = deferred();
    harness.api.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { result } = renderHook(() => useSettings(), { wrapper: ({ children }) => <React.StrictMode>{children}</React.StrictMode> });
    expect(harness.api).toHaveBeenCalledTimes(2);
    await act(async () => second.resolve(payload(savedSettings)));
    await act(async () => first.reject(new Error('prior effect lifetime')));
    expect(result.current.settings).toEqual(savedSettings);
    expect(result.current.error).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it('preserves cloud per-key save routing and its acknowledged merged snapshot', async () => {
    harness.cloud = true;
    const { result } = await load();
    const oldRead = deferred();
    harness.api.mockReturnValueOnce(oldRead.promise)
      .mockResolvedValueOnce({ ok: true }).mockResolvedValueOnce({ ok: true })
      .mockResolvedValueOnce(payload(savedSettings));
    act(() => { void result.current.refreshSettings(); });
    await act(async () => expect(await result.current.updateSettings({ mic_muted: true, theme: 'dark' })).toBe(true));
    await act(async () => oldRead.resolve(payload(oldSettings)));
    expect(result.current.settings).toEqual(savedSettings);
    expect(harness.api.mock.calls.slice(-3)).toEqual([
      ['/api/v1/cloud/settings/mic_muted', { method: 'PUT', body: JSON.stringify({ value: true }) }],
      ['/api/v1/cloud/settings/theme', { method: 'PUT', body: JSON.stringify({ value: 'dark' }) }],
      ['/api/v1/cloud/settings'],
    ]);
  });

});


describe('useSettings explicit acknowledgement receipts', () => {
  it('returns exact normalized and redacted values for the acknowledged request', async () => {
    const { result } = await load();
    const acknowledged = { ...savedSettings, tts_volume: 1, llm_api_key: '••••••' };
    harness.api.mockResolvedValueOnce(payload(acknowledged));
    let receipt;
    await act(async () => { receipt = await result.current.saveSettingsWithSnapshot({ tts_volume: 2, llm_api_key: 'synthetic input' }); });
    expect(receipt).toEqual({ ok: true, settings: acknowledged });
    expect(result.current.settings).toEqual(acknowledged);
  });

  it('keeps the request receipt separate from a later pushed snapshot', async () => {
    const { result } = await load();
    const response = deferred();
    harness.api.mockReturnValueOnce(response.promise);
    let pendingSave;
    act(() => { pendingSave = result.current.saveSettingsWithSnapshot(savedSettings); });
    let receipt;
    const later = { ...savedSettings, theme: 'system' };
    await act(async () => {
      response.resolve(payload(savedSettings));
      receipt = await pendingSave;
      harness.receive({ type: 'settings_changed', payload: { settings: later } });
    });
    expect(receipt).toEqual({ ok: true, settings: savedSettings });
    expect(result.current.settings).toEqual(later);
  });

  it('returns an explicit refusal without fabricating a settings snapshot', async () => {
    const { result } = await load();
    harness.api.mockRejectedValueOnce(new Error('synthetic write refusal'));
    let receipt;
    await act(async () => { receipt = await result.current.saveSettingsWithSnapshot({ theme: 'light' }); });
    expect(receipt).toEqual({ ok: false });
    expect(result.current.error).toBe('Failed to save settings');
    expect(result.current.saving).toBe(false);
  });

  it('keeps both legacy update methods strictly boolean', async () => {
    const { result } = await load();
    harness.api.mockResolvedValueOnce(payload(savedSettings)).mockRejectedValueOnce(new Error('refused'));
    await act(async () => {
      expect(await result.current.updateSettings(savedSettings)).toBe(true);
      expect(await result.current.updateSetting('theme', 'light')).toBe(false);
    });
  });
});
