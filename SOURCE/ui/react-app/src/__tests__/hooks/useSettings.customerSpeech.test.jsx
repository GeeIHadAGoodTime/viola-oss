import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useSettings } from '../../hooks/useSettings';

const harness = vi.hoisted(() => ({ api: vi.fn(), receive: null }));
vi.mock('../../hooks/useViolaApi', () => ({ apiFetch: (...args) => harness.api(...args) }));
vi.mock('../../hooks/useWebSocket', () => ({ useWebSocket: receive => { harness.receive = receive; } }));
vi.mock('../../components/auth/cloudSurface', () => ({ isCloudSurface: () => false }));
const settings = { tts_language: 'es', tts_voice: 'ef_dora' };
const profile = { selection: { language: 'es', voice: 'ef_dora' }, locales: [{ value: 'es', voices: ['ef_dora'] }] };
const payload = { ok: true, settings, customer_speech: profile };
async function load() {
  const view = renderHook(() => useSettings());
  await waitFor(() => expect(view.result.current.loading).toBe(false));
  return view;
}
beforeEach(() => { harness.api.mockReset().mockResolvedValue(payload); });

describe('useSettings customer speech outcomes', () => {
  it('surfaces a failed account-refresh binding without claiming speech is active', async () => {
    harness.api.mockResolvedValueOnce({ ...payload, customer_speech_effect: { outcome: 'failed' } });
    const { result } = await load();
    expect(result.current.error).toContain('could not be applied');
    expect(result.current.settings).toEqual(settings);
  });
  it('keeps profile metadata outside the settings submitted for persistence', async () => {
    const { result } = await load();
    expect(result.current.customerSpeech).toEqual(profile);
    await act(async () => expect(await result.current.updateSettings(settings)).toBe(true));
    expect(harness.api).toHaveBeenLastCalledWith('/v1/settings', {
      method: 'POST', body: JSON.stringify({ settings }),
    });
  });
  it('adopts persisted values but reports failed runtime application without a success receipt', async () => {
    const { result } = await load();
    harness.api.mockResolvedValueOnce({ ...payload, customer_speech_effect: { outcome: 'failed' } });
    await act(async () => expect(await result.current.saveSettingsWithSnapshot(settings)).toEqual({ ok: false }));
    expect(result.current.settings).toEqual(settings);
    expect(result.current.error).toContain('was saved, but could not be applied');
  });
  it('reports failed reset application while adopting the actual reset settings', async () => {
    const { result } = await load();
    harness.api.mockResolvedValueOnce({ ...payload, settings: { tts_language: 'en-us', tts_voice: 'default' },
      customer_speech_effect: { outcome: 'failed' } });
    await act(async () => expect(await result.current.resetSettings()).toBe(false));
    expect(result.current.settings.tts_language).toBe('en-us');
    expect(result.current.error).toContain('Settings were reset');
  });
  it('never reports a rejected pair as saved', async () => {
    const { result } = await load();
    harness.api.mockRejectedValueOnce(Object.assign(new Error('invalid pair'), {
      status: 422, code: 'validation_error', data: { validation_reason: 'speech_selection' },
    }));
    await act(async () => expect(await result.current.updateSettings({ tts_language: 'fr' })).toBe(false));
    expect(result.current.error).toContain('Nothing was saved');
    expect(result.current.settings).toEqual(settings);
  });
  it('retains newer selected-inventory metadata when an older read completes', async () => {
    let finish;
    harness.api.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const { result } = renderHook(() => useSettings());
    act(() => harness.receive({ type: 'settings_changed', payload }));
    await act(async () => finish({ ok: true, settings: {} }));
    expect(result.current.customerSpeech).toEqual(profile);
    expect(result.current.settings).toEqual(settings);
  });
});
