import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import SettingsModal from './SettingsModal';

const harness = vi.hoisted(() => ({
  settings: {}, loading: false, saving: false, error: null,
  devices: { input: [], output: [] }, playlists: [],
  updateSettings: vi.fn(() => Promise.resolve(true)),
  syncPlaylists: vi.fn(), renamePlaylist: vi.fn(), setDefaultPlaylist: vi.fn(),
  deletePlaylist: vi.fn(), clearError: vi.fn(), refreshDevices: vi.fn(), refreshPlaylists: vi.fn(),
}));
// Preserve the boolean fixture while exposing the hook's exact save receipt.
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
const token = 'portaudio:{"name":"USB Speaker","hostapi":"Host B"}';
function openSystem() {
  render(<SettingsModal isOpen onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: 'System' }));
}

describe('SettingsModal output-device wiring', () => {
  beforeEach(() => {
    window.viola = {};
    harness.settings = { theme: 'dark', ai_source: 'managed', output_device: '' };
    harness.devices = { input: [], output: [{ index: 9, name: 'USB Speaker', hostapi: 'Host B', selection: token }] };
    harness.updateSettings.mockClear();
  });
  afterEach(() => { delete window.viola; });
  it('saves a stable selection from the Speaker picker', async () => {
    openSystem();
    fireEvent.click(await screen.findByRole('button', { name: 'Speaker: System Default' }));
    fireEvent.click(screen.getByRole('option', { name: 'USB Speaker (Host B)' }));
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));
    await waitFor(() => expect(harness.updateSettings).toHaveBeenCalled());
    expect(harness.updateSettings.mock.calls.at(-1)[0].output_device).toBe(token);
  });
  it('shows legacy numeric selection without silently saving it', async () => {
    harness.settings.output_device = '9';
    openSystem();
    expect(await screen.findByRole('button', { name: 'Speaker: USB Speaker (Host B)' })).toBeInTheDocument();
    expect(harness.updateSettings).not.toHaveBeenCalled();
  });
  it('shows a disconnected saved selection and allows explicit default', async () => {
    harness.settings.output_device = token;
    harness.devices.output = [];
    openSystem();
    fireEvent.click(await screen.findByRole('button', { name: /Speaker: USB Speaker.*unavailable/ }));
    fireEvent.click(screen.getByRole('option', { name: 'System Default' }));
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));
    await waitFor(() => expect(harness.updateSettings).toHaveBeenCalled());
    expect(harness.updateSettings.mock.calls.at(-1)[0].output_device).toBe('');
  });
});
