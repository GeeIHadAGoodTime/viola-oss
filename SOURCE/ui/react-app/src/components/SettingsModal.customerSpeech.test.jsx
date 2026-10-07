import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import SettingsModal from './SettingsModal';

const harness = vi.hoisted(() => ({
  settings: {}, customerSpeech: null, loading: false, saving: false, error: null,
  devices: { input: [], output: [] }, playlists: [],
  updateSettings: vi.fn(() => Promise.resolve(true)),
  syncPlaylists: vi.fn(), renamePlaylist: vi.fn(), setDefaultPlaylist: vi.fn(),
  deletePlaylist: vi.fn(), clearError: vi.fn(), refreshDevices: vi.fn(), refreshPlaylists: vi.fn(),
}));
const saveSettingsWithSnapshot = async values => {
  const ok = await harness.updateSettings(values);
  if (!ok) return { ok: false };
  const settings = { ...values };
  harness.settings = settings;
  return { ok: true, settings };
};
vi.mock('../hooks/useSettings', () => ({ useSettings: () => ({ ...harness, saveSettingsWithSnapshot }) }));
vi.mock('../hooks/useViolaApi', () => ({
  apiFetch: vi.fn(() => Promise.resolve({})),
  authFetch: vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({}) })),
}));
vi.mock('./AccountTab', () => ({ default: () => null, CalendarSettings: () => null }));
vi.mock('./MessagingTab', () => ({ default: () => null }));
vi.mock('./ai/CodexAuthCard', () => ({ default: () => null }));

function openVoice(onClose = vi.fn()) {
  const view = render(<SettingsModal isOpen onClose={onClose} />);
  fireEvent.click(screen.getByRole('button', { name: 'Voice' }));
  return view;
}

describe('qualification-only speech settings', () => {
  beforeEach(() => {
    window.viola = {};
    harness.settings = { theme: 'dark', ai_source: 'managed', tts_enabled: true,
      tts_language: 'en-us', tts_voice: 'default', whisper_language: 'auto', locale: 'en-US' };
    harness.customerSpeech = { selection: { language: 'en-us', voice: 'af_heart' }, locales: [
      { value: 'en-us', label: 'English (US)', voices: ['af_heart'] },
      { value: 'es', label: 'Spanish', voices: ['ef_dora'] },
      { value: 'zh', label: 'Mandarin', voices: ['zf_xiaobei'] },
    ] };
    harness.updateSettings.mockClear();
  });
  afterEach(() => { delete window.viola; });

  it.each([['Spanish', 'es', 'ef_dora'], ['Mandarin', 'zh', 'zf_xiaobei']])(
    'saves %s with its admitted named voice as one settings write', async (label, locale, voice) => {
      openVoice();
      fireEvent.click(await screen.findByRole('button', { name: 'Speech Output Language: English (US)' }));
      fireEvent.click(screen.getByRole('option', { name: label }));
      expect(screen.getByRole('button', { name: `Assistant Voice: ${voice}` })).toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: /save changes/i }));
      await waitFor(() => expect(harness.updateSettings).toHaveBeenCalledTimes(1));
      expect(harness.updateSettings.mock.calls[0][0]).toMatchObject({
        tts_language: locale, tts_voice: voice, whisper_language: 'auto', locale: 'en-US',
      });
    },
  );

  it('keeps ordinary Assistant Voice options and hides the new selector outside qualification', async () => {
    harness.customerSpeech = null;
    openVoice();
    expect(screen.queryByRole('button', { name: /Speech Output Language:/ })).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: 'Assistant Voice: Default' }));
    expect(screen.getByRole('option', { name: 'Alloy' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'ef_dora' })).not.toBeInTheDocument();
  });

  it('does not save a canceled language change and restores the saved pair on reopen', async () => {
    const close = vi.fn();
    const view = openVoice(close);
    fireEvent.click(await screen.findByRole('button', { name: 'Speech Output Language: English (US)' }));
    fireEvent.click(screen.getByRole('option', { name: 'Spanish' }));
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }));
    expect(close).toHaveBeenCalled();
    expect(harness.updateSettings).not.toHaveBeenCalled();
    view.rerender(<SettingsModal isOpen={false} onClose={close} />);
    view.rerender(<SettingsModal isOpen onClose={close} />);
    fireEvent.click(screen.getByRole('button', { name: 'Voice' }));
    expect(await screen.findByRole('button', { name: 'Speech Output Language: English (US)' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Assistant Voice: af_heart' })).toBeInTheDocument();
  });
});
