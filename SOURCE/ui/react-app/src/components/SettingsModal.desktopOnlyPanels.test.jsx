/**
 * #4226 (C-427): the seven desktop-only panels that rendered live on the cloud
 * SPA.
 *
 * Pre-fix shape this file fails on: `extensions`, `smarthome`, `local_music`,
 * `music_services`, `wake_models`, `weekly_review` and `system_controls` were
 * each declared in DESKTOP_ONLY_FEATURES (featureSurface.js) with upsell copy
 * written for them, but NOTHING ever called the gate with those keys. So the
 * cloud-served /app page rendered a Spotify sign-in tile, a local-music folder
 * picker, a "Find devices on my network" scan button, wake-model Switch/Delete
 * buttons, Run-Now weekly review, an MCP/plugin manager and a whole System tab
 * of tray / start-on-boot / local-port / audio-device controls. Every one of
 * them dials a route group backend/cloud_route_manifest.py deliberately does
 * not register on cloud (LOCAL_ONLY or COMPANION_REQUIRED), so the click was a
 * 404 or a silent no-op.
 *
 * Unlike C-401's `desktop_ai`, none of these carries a Tier-3 secret; the
 * defect is user-facing dishonesty rather than credential custody. The bar is
 * therefore the same in both directions: on cloud the user must read an honest
 * DesktopUpsell and the SPA must fire NO request at the unserved route group;
 * on desktop every one of these surfaces must still work exactly as before.
 *
 * Surface detection follows utils/featureSurface.test.js: a `window.viola` Qt
 * bridge means desktop (everything available); no bridge and no spoke param
 * means the cloud SPA.
 */
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import SettingsModal from './SettingsModal';

const settingsHarness = vi.hoisted(() => ({
  settings: {},
  loading: false,
  saving: false,
  error: null,
  devices: { input: [], output: [] },
  playlists: [],
  updateSettings: vi.fn(() => Promise.resolve(true)),
  syncPlaylists: vi.fn(() => Promise.resolve({ ok: true, synced_count: 0 })),
  renamePlaylist: vi.fn(),
  setDefaultPlaylist: vi.fn(),
  deletePlaylist: vi.fn(),
  clearError: vi.fn(),
  refreshDevices: vi.fn(),
  refreshPlaylists: vi.fn(),
}));

const apiFetchMock = vi.hoisted(() => vi.fn(() => Promise.resolve({})));
const authHarness = vi.hoisted(() => ({ value: null }));

// Preserve the boolean fixture while exposing the hook's exact save receipt.
const saveSettingsWithSnapshot = async values => {
  const ok = await settingsHarness.updateSettings(values);
  if (!ok) return { ok: false };
  const settings = { ...values };
  settingsHarness.settings = settings;
  return { ok: true, settings };
};

vi.mock('../hooks/useSettings', () => ({
  useSettings: () => ({ ...settingsHarness, saveSettingsWithSnapshot }),
}));

vi.mock('../hooks/useAuth', () => ({
  useOptionalAuth: () => authHarness.value,
  useAuth: () => authHarness.value,
  usePlan: () => ({}),
  AuthProvider: ({ children }) => children,
}));

vi.mock('../hooks/useViolaApi', () => ({
  apiFetch: apiFetchMock,
  authFetch: vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({}) })),
}));

vi.mock('./AccountTab', () => ({
  default: () => <div>Account settings body</div>,
  CalendarSettings: () => <div>Calendar settings body</div>,
}));

vi.mock('./MessagingTab', () => ({
  default: () => <div>Telegram settings body</div>,
}));

vi.mock('./ai/CodexAuthCard', () => ({
  default: () => <div>Codex sign-in card</div>,
}));

const openTab = (name) => {
  fireEvent.click(screen.getByRole('button', { name }));
};

const requestedPaths = () => apiFetchMock.mock.calls.map(([path]) => String(path));

// The upsell REASON, not the title: several of these titles collide with the
// enclosing <Section> heading of the same name, and the reason is the sentence
// that actually tells the user why the surface is not here.
const UPSELL_REASON = {
  smarthome: /scans your local network/i,
  local_music: /access to your machine.s disk/i,
  music_services: /opens a secure browser window on your machine/i,
  weekly_review: /generated from memory stored on your machine/i,
  system_controls: /settings for the installed desktop app/i,
};

const baseSettings = {
  theme: 'dark',
  ai_source: 'managed',
  ai_enabled: true,
  active_music_provider_id: 'youtube_music',
  weekly_review_enabled: true,
  tts_enabled: true,
};

describe('SettingsModal desktop-only panels (#4226)', () => {
  beforeEach(() => {
    settingsHarness.settings = { ...baseSettings };
    authHarness.value = null;
    settingsHarness.updateSettings.mockReset().mockResolvedValue(true);
    settingsHarness.refreshDevices.mockClear();
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue({});
  });

  afterEach(() => {
    delete window.viola;
    delete window.__VIOLA_API_KEY__;
    vi.unstubAllGlobals();
    window.history.replaceState({}, '', '/');
  });

  describe('on the cloud SPA (no Qt bridge)', () => {
    beforeEach(() => {
      delete window.viola;
      window.history.replaceState({}, '', '/app');
    });

    it('offers no Spotify or Local Files music source, only cloud-native YouTube', async () => {
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('Music');

      await screen.findByText('YouTube');
      expect(screen.queryByText('Spotify')).toBeNull();
      expect(screen.queryByText('Local Files')).toBeNull();
    });

    it('fires no request at the COMPANION_REQUIRED spotify_cdp / local_library groups', async () => {
      settingsHarness.settings = {
        ...baseSettings,
        active_music_provider_id: 'local',
        local_music_folder: 'C:/Music',
      };
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('Music');

      await screen.findByText('YouTube');
      // The Spotify-status poll and the local track-count fetch both sit behind
      // MODAL_OPEN_DEFER_MS, so asserting "no calls" straight away would pass
      // vacuously on the PRE-FIX tree too -- it would just be racing the timer
      // rather than testing the gate. Wait until a request has demonstrably had
      // its chance; the desktop twin below ('still polls Spotify status when
      // the Music tab opens') proves a call does land inside this window.
      await new Promise((resolve) => { setTimeout(resolve, 400); });
      const paths = requestedPaths();
      expect(paths.filter((p) => p.startsWith('/v1/spotify'))).toEqual([]);
      expect(paths.filter((p) => p.startsWith('/v1/browser/auth'))).toEqual([]);
      expect(paths.filter((p) => p.startsWith('/v1/local/'))).toEqual([]);
    });

    it('keeps the local-music controls hidden even when the SAVED provider is local', async () => {
      // The pre-fix hole a tile-only fix would leave: a desktop user whose
      // synced setting already says `local` still got the folder picker and
      // the Rescan button rendered into a browser tab.
      settingsHarness.settings = {
        ...baseSettings,
        active_music_provider_id: 'local',
        local_music_folder: 'C:/Music',
      };
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('Music');

      await screen.findByText('YouTube');
      expect(screen.queryByText('Rescan')).toBeNull();
      expect(screen.queryByText('Browse')).toBeNull();
      expect(screen.queryByRole('button', { name: /^(Set|Change) Folder$/ })).toBeNull();
      await waitFor(() => {
        expect(requestedPaths().filter((p) => p.startsWith('/v1/local/'))).toEqual([]);
      });
    });

    it('renders the weekly-review upsell instead of the Run Now / View Last Review controls', async () => {
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('AI & Agents');

      await screen.findByText(UPSELL_REASON.weekly_review);
      expect(screen.queryByText('Run Now')).toBeNull();
      expect(screen.queryByText('View Last Review')).toBeNull();
      expect(screen.queryByLabelText('Enable weekly review')).toBeNull();
      await waitFor(() => {
        expect(requestedPaths().filter((p) => p.includes('/weekly-review'))).toEqual([]);
      });
    });

    it('renders the smart-home upsell instead of the network scan on the Connections tab', async () => {
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('Connections');

      await screen.findByText(UPSELL_REASON.smarthome);
      expect(screen.queryByText('Find devices on my network')).toBeNull();
      await waitFor(() => {
        expect(requestedPaths().filter((p) => p.startsWith('/v1/smarthome'))).toEqual([]);
      });
    });

    it('replaces the whole System tab with the desktop-settings upsell', async () => {
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('System');

      await screen.findByText(UPSELL_REASON.system_controls);
      expect(screen.queryByText('Audio Devices')).toBeNull();
      expect(screen.queryByText('Open Advanced Settings')).toBeNull();
    });

    it('enumerates no audio devices on the System tab', async () => {
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('System');

      await screen.findByText(UPSELL_REASON.system_controls);
      await waitFor(() => {
        expect(settingsHarness.refreshDevices).not.toHaveBeenCalled();
      });
    });
  });

  describe('on the desktop app (Qt bridge present)', () => {
    beforeEach(() => {
      window.viola = {};
      window.history.replaceState({}, '', '/');
    });

    it('still offers Spotify and Local Files as music sources', async () => {
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('Music');

      await screen.findByText('YouTube');
      expect(screen.getByText('Spotify')).toBeInTheDocument();
      expect(screen.getByText('Local Files')).toBeInTheDocument();
    });

    it.each([
      ['', 'Set Folder'],
      ['C:/QA Music', 'Change Folder'],
    ])('opens the existing editor for folder %j without browsing, saving or scanning', async (folder, action) => {
      settingsHarness.settings = { ...baseSettings, active_music_provider_id: 'local', local_music_folder: folder };
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('Music');
      fireEvent.click(await screen.findByRole('button', { name: action }));
      expect(screen.getByRole('textbox', { name: 'Local music folder' })).toHaveValue(folder);
      expect(screen.queryByRole('button', { name: action })).toBeNull();
      expect(settingsHarness.updateSettings).not.toHaveBeenCalled();
      expect(requestedPaths()).not.toContain('/v1/local/browse-folder');
      expect(requestedPaths()).not.toContain('/v1/local/library/rescan');
    });

    it('cancels a configured folder edit without persisting or scanning the draft', async () => {
      const original = 'C:/QA Music';
      settingsHarness.settings = { ...baseSettings, active_music_provider_id: 'local', local_music_folder: original };
      const onClose = vi.fn();
      render(<SettingsModal isOpen onClose={onClose} />);
      openTab('Music');
      fireEvent.click(await screen.findByRole('button', { name: 'Change Folder' }));
      const input = screen.getByRole('textbox', { name: 'Local music folder' });
      fireEvent.change(input, { target: { value: 'D:/Uncommitted' } });
      fireEvent.click(within(input.parentElement.parentElement).getByRole('button', { name: 'Cancel' }));
      expect(screen.queryByRole('textbox', { name: 'Local music folder' })).toBeNull();
      fireEvent.click(screen.getByRole('button', { name: 'Change Folder' }));
      expect(screen.getByRole('textbox', { name: 'Local music folder' })).toHaveValue(original);
      expect(settingsHarness.settings.local_music_folder).toBe(original);
      expect(settingsHarness.updateSettings).not.toHaveBeenCalled();
      expect(requestedPaths()).not.toContain('/v1/local/library/rescan');
      expect(onClose).not.toHaveBeenCalled();
    });

    it('waits for the configured folder save acknowledgement before scanning and reopens the saved value', async () => {
      settingsHarness.settings = { ...baseSettings, active_music_provider_id: 'local', local_music_folder: 'C:/QA Music' };
      let resolveSave;
      settingsHarness.updateSettings.mockImplementationOnce(() => new Promise(resolve => { resolveSave = resolve; }));
      apiFetchMock.mockImplementation(path => Promise.resolve(path === '/v1/local/library/rescan' ? { scanned: 2 } : {}));
      const onClose = vi.fn();
      const view = render(<SettingsModal isOpen onClose={onClose} />);
      openTab('Music');
      fireEvent.click(await screen.findByRole('button', { name: 'Change Folder' }));
      fireEvent.change(screen.getByRole('textbox', { name: 'Local music folder' }), { target: { value: '  D:/QA Music Two  ' } });
      fireEvent.click(screen.getByRole('button', { name: 'Save & Scan' }));
      expect(settingsHarness.updateSettings).toHaveBeenCalledWith(expect.objectContaining({ local_music_folder: 'D:/QA Music Two', active_music_provider_id: 'local' }));
      expect(requestedPaths()).not.toContain('/v1/local/library/rescan');
      await act(async () => resolveSave(true));
      expect(await screen.findByText(/Scan complete.*2 tracks found/)).toBeInTheDocument();
      expect(requestedPaths().filter(path => path === '/v1/local/library/rescan')).toHaveLength(1);
      expect(settingsHarness.settings.local_music_folder).toBe('D:/QA Music Two');
      view.rerender(<SettingsModal isOpen={false} onClose={onClose} />);
      view.rerender(<SettingsModal isOpen onClose={onClose} />);
      openTab('Music');
      fireEvent.click(await screen.findByRole('button', { name: 'Change Folder' }));
      expect(screen.getByRole('textbox', { name: 'Local music folder' })).toHaveValue('D:/QA Music Two');
      expect(onClose).not.toHaveBeenCalled();
    });

    it('keeps a refused folder draft available for correction and never scans it before a successful retry', async () => {
      settingsHarness.settings = { ...baseSettings, active_music_provider_id: 'local', local_music_folder: 'C:/QA Music' };
      settingsHarness.updateSettings.mockResolvedValueOnce(false);
      apiFetchMock.mockImplementation(path => Promise.resolve(path === '/v1/local/library/rescan' ? { scanned: 0 } : {}));
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('Music');
      fireEvent.click(await screen.findByRole('button', { name: 'Change Folder' }));
      fireEvent.change(screen.getByRole('textbox', { name: 'Local music folder' }), { target: { value: 'D:/QA Retry' } });
      await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Save & Scan' })));
      expect(settingsHarness.updateSettings).toHaveBeenCalledTimes(1);
      expect(screen.getByRole('textbox', { name: 'Local music folder' })).toHaveValue('D:/QA Retry');
      expect(settingsHarness.settings.local_music_folder).toBe('C:/QA Music');
      expect(requestedPaths()).not.toContain('/v1/local/library/rescan');
      fireEvent.click(screen.getByRole('button', { name: 'Save & Scan' }));
      expect(await screen.findByText('No audio files found in this folder')).toBeInTheDocument();
      expect(settingsHarness.updateSettings).toHaveBeenCalledTimes(2);
      expect(settingsHarness.settings.local_music_folder).toBe('D:/QA Retry');
      expect(requestedPaths().filter(path => path === '/v1/local/library/rescan')).toHaveLength(1);
    });

    it('retains the configured folder when the native picker is cancelled', async () => {
      settingsHarness.settings = { ...baseSettings, active_music_provider_id: 'local', local_music_folder: 'C:/QA Music' };
      apiFetchMock.mockImplementation(path => Promise.resolve(path === '/v1/local/browse-folder' ? { folder: null } : {}));
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('Music');
      fireEvent.click(await screen.findByRole('button', { name: 'Change Folder' }));
      await act(async () => fireEvent.click(screen.getByRole('button', { name: /^Browse\.\.\.$/ })));
      expect(screen.getByRole('textbox', { name: 'Local music folder' })).toHaveValue('C:/QA Music');
      expect(settingsHarness.updateSettings).not.toHaveBeenCalled();
      expect(requestedPaths()).not.toContain('/v1/local/library/rescan');
      expect(screen.queryByText(/Couldn't open the folder picker/i)).toBeNull();
    });

    it('clears a configured folder through the existing outer Save and restores YouTube without scanning', async () => {
      settingsHarness.settings = { ...baseSettings, active_music_provider_id: 'youtube_music', local_music_folder: 'C:/QA Music' };
      const onClose = vi.fn();
      const view = render(<SettingsModal isOpen onClose={onClose} />);
      openTab('Music');
      fireEvent.click(await screen.findByText('Local Files'));
      fireEvent.click(await screen.findByRole('button', { name: 'Change Folder' }));
      fireEvent.change(screen.getByRole('textbox', { name: 'Local music folder' }), { target: { value: '' } });
      expect(screen.getByRole('button', { name: 'Save & Scan' })).toBeDisabled();
      fireEvent.click(screen.getByText('YouTube'));
      fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
      await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
      expect(settingsHarness.updateSettings).toHaveBeenCalledWith(expect.objectContaining({ local_music_folder: '', active_music_provider_id: 'youtube_music' }));
      expect(settingsHarness.settings.local_music_folder).toBe('');
      expect(settingsHarness.settings.active_music_provider_id).toBe('youtube_music');
      expect(requestedPaths()).not.toContain('/v1/local/library/rescan');
      expect(requestedPaths()).not.toContain('/v1/local/browse-folder');
      view.unmount();
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('Music');
      fireEvent.click(await screen.findByText('Local Files'));
      fireEvent.click(await screen.findByRole('button', { name: 'Set Folder' }));
      expect(screen.getByRole('textbox', { name: 'Local music folder' })).toHaveValue('');
    });

    it('shows an error when the native local folder picker fails', async () => {
      settingsHarness.settings = {
        ...baseSettings,
        active_music_provider_id: 'local',
        local_music_folder: '',
      };
      apiFetchMock.mockImplementation((path) => {
        if (path === '/v1/local/browse-folder') {
          return Promise.reject(
            Object.assign(new Error("We couldn't complete that request. Please try again."), {
              code: 'folder_picker_failed',
              status: 503,
            })
          );
        }
        return Promise.resolve({});
      });
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('Music');

      fireEvent.click(await screen.findByText('Set Folder'));
      fireEvent.click(screen.getByRole('button', { name: /^Browse\.\.\.$/ }));

      expect(await screen.findByText(/Couldn't open the folder picker/i)).toBeInTheDocument();
      expect(apiFetchMock).toHaveBeenCalledWith('/v1/local/browse-folder', { method: 'POST' });
    });

    it('keeps a cancelled native local folder picker quiet', async () => {
      settingsHarness.settings = {
        ...baseSettings,
        active_music_provider_id: 'local',
        local_music_folder: '',
      };
      apiFetchMock.mockImplementation((path) => {
        if (path === '/v1/local/browse-folder') {
          return Promise.resolve({ folder: null });
        }
        return Promise.resolve({});
      });
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('Music');

      fireEvent.click(await screen.findByText('Set Folder'));
      fireEvent.click(screen.getByRole('button', { name: /^Browse\.\.\.$/ }));

      await waitFor(() => {
        expect(apiFetchMock).toHaveBeenCalledWith('/v1/local/browse-folder', { method: 'POST' });
      });
      expect(screen.queryByText(/Couldn't open the folder picker/i)).toBeNull();
    });

    it('still polls Spotify status when the Music tab opens', async () => {
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('Music');

      await waitFor(() => {
        expect(requestedPaths().some((p) => p.startsWith('/v1/spotify/status'))).toBe(true);
      });
    });

    it('still renders the weekly-review controls', async () => {
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('AI & Agents');

      expect(await screen.findByText('Run Now')).toBeInTheDocument();
      expect(screen.getByText('View Last Review')).toBeInTheDocument();
      expect(screen.queryByText(UPSELL_REASON.weekly_review)).toBeNull();
    });

    it.each([
      [{ has_analysis: true, summary: 'Saved synthetic weekly review.' }, 'Saved synthetic weekly review.'],
      [{ has_analysis: false, analysis: null, summary: 'No meta-analysis has been run yet.' }, 'No meta-analysis has been run yet.'],
      [{ has_analysis: false, analysis: null }, 'No review yet.'],
    ])('reads the parsed latest weekly-review payload %j', async (payload, expected) => {
      apiFetchMock.mockImplementation(path => Promise.resolve(path.endsWith('/weekly-review/latest') ? payload : {}));
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('AI & Agents');
      fireEvent.click(screen.getByRole('button', { name: 'View Last Review' }));
      expect(await screen.findByText(expected)).toBeInTheDocument();
      expect(requestedPaths().filter(path => path.includes('/weekly-review'))).toEqual(['/v1/ai/weekly-review/latest']);
      expect(settingsHarness.updateSettings).not.toHaveBeenCalled();
    });

    it('displays the parsed manual weekly-review completion', async () => {
      apiFetchMock.mockImplementation(path => Promise.resolve(path.endsWith('/weekly-review/trigger')
        ? { triggered: true, analysis: { summary: 'Completed synthetic weekly review.' } } : {}));
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('AI & Agents');
      fireEvent.click(screen.getByRole('button', { name: 'Run Now' }));
      expect(await screen.findByText('Completed synthetic weekly review.')).toBeInTheDocument();
      expect(apiFetchMock).toHaveBeenCalledWith('/v1/ai/weekly-review/trigger', { method: 'POST' });
      expect(settingsHarness.updateSettings).not.toHaveBeenCalled();
    });

    it.each([
      ['View Last Review', { ok: true, data: { summary: 'Saved through the real API helper.' } }, 'Saved through the real API helper.'],
      ['Run Now', { ok: true, data: { triggered: true, analysis: { summary: 'Run through the real API helper.' } } }, 'Run through the real API helper.'],
    ])('uses the real apiFetch unwrapping contract for %s', async (button, envelope, expected) => {
      const { apiFetch } = await vi.importActual('../hooks/useViolaApi');
      window.__VIOLA_API_KEY__ = 'synthetic-weekly-review-test';
      const json = vi.fn().mockResolvedValue(envelope);
      const fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json });
      vi.stubGlobal('fetch', fetch);
      apiFetchMock.mockImplementation((path, options) => path.includes('/weekly-review')
        ? apiFetch(path, options) : Promise.resolve({}));
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('AI & Agents');
      fireEvent.click(screen.getByRole('button', { name: button }));
      expect(await screen.findByText(expected)).toBeInTheDocument();
      expect(json).toHaveBeenCalledTimes(1);
      expect(fetch).toHaveBeenCalledTimes(1);
    });

    it.each([
      [{ triggered: false, analysis: null }, 'Analysis returned no result.'],
      [{ triggered: true, analysis: { summary: '' } }, 'Analysis complete.'],
      [{ ok: false, error: { code: 'review_failed' } }, "Trigger failed: We couldn't complete that request. Please try again."],
    ])('reports the manual review outcome truthfully for %j', async (payload, expected) => {
      apiFetchMock.mockImplementation(path => Promise.resolve(path.endsWith('/weekly-review/trigger') ? payload : {}));
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('AI & Agents');
      fireEvent.click(screen.getByRole('button', { name: 'Run Now' }));
      expect(await screen.findByText(expected)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Run Now' })).toBeEnabled();
    });

    it.each([
      ['View Last Review', '/latest', { summary: 'Saved after retry.' }, 'Saved after retry.', 'Failed to load review: Offline'],
      ['Run Now', '/trigger', { triggered: true, analysis: { summary: 'Completed after retry.' } }, 'Completed after retry.', 'Trigger failed: Offline'],
    ])('recovers from a %s error using the same control', async (button, suffix, payload, expected, error) => {
      let attempt = 0;
      apiFetchMock.mockImplementation(path => {
        if (!path.endsWith('/weekly-review' + suffix)) return Promise.resolve({});
        return ++attempt === 1 ? Promise.reject(new Error('Offline')) : Promise.resolve(payload);
      });
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('AI & Agents');
      fireEvent.click(screen.getByRole('button', { name: button }));
      expect(await screen.findByText(error)).toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: button }));
      expect(await screen.findByText(expected)).toBeInTheDocument();
      expect(attempt).toBe(2);
      expect(screen.queryByText(error)).toBeNull();
    });

    it.each(['View Last Review', 'Run Now'])('prevents overlapping requests while %s is pending', async button => {
      let finish;
      apiFetchMock.mockImplementation(path => path.includes('/weekly-review')
        ? new Promise(resolve => { finish = resolve; }) : Promise.resolve({}));
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('AI & Agents');
      const read = screen.getByRole('button', { name: 'View Last Review' });
      const run = screen.getByRole('button', { name: 'Run Now' });
      fireEvent.click(screen.getByRole('button', { name: button }));
      fireEvent.click(run);
      fireEvent.click(read);
      expect(run).toBeDisabled();
      expect(read).toBeDisabled();
      expect(requestedPaths().filter(path => path.includes('/weekly-review'))).toHaveLength(1);
      await act(async () => finish({ triggered: true, analysis: { summary: 'Complete.' }, summary: 'Complete.' }));
      expect(await screen.findByText('Complete.')).toBeInTheDocument();
      expect(run).toBeEnabled();
      expect(read).toBeEnabled();
    });

    it.each(['tab', 'close', 'cancel', 'disable', 'account'])('ignores a retired review completion after %s and permits a fresh read', async interruption => {
      let finish;
      let attempt = 0;
      apiFetchMock.mockImplementation(path => {
        if (!path.endsWith('/weekly-review/latest')) return Promise.resolve({});
        return ++attempt === 1 ? new Promise(resolve => { finish = resolve; }) : Promise.resolve({ summary: 'Current review.' });
      });
      if (interruption === 'account') authHarness.value = { isLoggedIn: true, user: { id: 'synthetic-original-owner' } };
      const onClose = vi.fn();
      const view = render(<SettingsModal isOpen onClose={onClose} />);
      openTab('AI & Agents');
      fireEvent.click(screen.getByRole('button', { name: 'View Last Review' }));
      if (interruption === 'tab') {
        openTab('Music');
        openTab('AI & Agents');
      } else if (interruption === 'close') {
        view.rerender(<SettingsModal isOpen={false} onClose={onClose} />);
        view.rerender(<SettingsModal isOpen onClose={onClose} />);
      } else if (interruption === 'cancel') {
        fireEvent.keyDown(document, { key: 'Escape' });
        expect(onClose).toHaveBeenCalledTimes(1);
      } else if (interruption === 'disable') {
        fireEvent.click(screen.getByRole('switch', { name: 'Enable weekly review' }));
        fireEvent.click(screen.getByRole('switch', { name: 'Enable weekly review' }));
      } else {
        authHarness.value = { isLoggedIn: true, user: { id: 'synthetic-next-owner' } };
        // Change a prop as well so React.memo re-renders the hook fixture.
        view.rerender(<SettingsModal isOpen onClose={vi.fn()} />);
      }
      fireEvent.click(screen.getByRole('button', { name: 'View Last Review' }));
      expect(await screen.findByText('Current review.')).toBeInTheDocument();
      await act(async () => finish({ summary: 'Retired owner review.' }));
      expect(screen.queryByText('Retired owner review.')).toBeNull();
      expect(screen.getByText('Current review.')).toBeInTheDocument();
    });

    it('does not let a retired error replace a newer completion', async () => {
      let fail;
      let attempt = 0;
      apiFetchMock.mockImplementation(path => {
        if (!path.endsWith('/weekly-review/latest')) return Promise.resolve({});
        return ++attempt === 1 ? new Promise((_, reject) => { fail = reject; }) : Promise.resolve({ summary: 'New review.' });
      });
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('AI & Agents');
      fireEvent.click(screen.getByRole('button', { name: 'View Last Review' }));
      openTab('Music');
      openTab('AI & Agents');
      fireEvent.click(screen.getByRole('button', { name: 'View Last Review' }));
      expect(await screen.findByText('New review.')).toBeInTheDocument();
      await act(async () => fail(new Error('Retired failure')));
      expect(screen.queryByText(/Retired failure/)).toBeNull();
      expect(screen.getByText('New review.')).toBeInTheDocument();
    });

    it('requires saving the opt-in before Run Now, preserves it on reopen, and can save opt-out', async () => {
      settingsHarness.settings = { ...baseSettings, weekly_review_enabled: false };
      const onClose = vi.fn();
      const view = render(<SettingsModal isOpen onClose={onClose} />);
      openTab('AI & Agents');
      fireEvent.click(screen.getByRole('switch', { name: 'Enable weekly review' }));
      expect(screen.getByRole('button', { name: 'Run Now' })).toBeDisabled();
      expect(screen.getByText('Save Changes to enable Weekly Review before running an analysis.')).toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: 'Run Now' }));
      expect(requestedPaths().filter(path => path.endsWith('/weekly-review/trigger'))).toEqual([]);
      expect(settingsHarness.updateSettings).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
      await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
      expect(settingsHarness.settings.weekly_review_enabled).toBe(true);
      view.unmount();
      render(<SettingsModal isOpen onClose={onClose} />);
      openTab('AI & Agents');
      expect(screen.getByRole('switch', { name: 'Enable weekly review' })).toBeChecked();
      expect(screen.getByRole('button', { name: 'Run Now' })).toBeEnabled();
      fireEvent.click(screen.getByRole('switch', { name: 'Enable weekly review' }));
      expect(screen.queryByRole('button', { name: 'Run Now' })).toBeNull();
      fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
      await waitFor(() => expect(onClose).toHaveBeenCalledTimes(2));
      expect(settingsHarness.settings.weekly_review_enabled).toBe(false);
      expect(requestedPaths().filter(path => path.endsWith('/weekly-review/trigger'))).toEqual([]);
    });

    it('still renders the smart-home network scan', async () => {
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('Connections');

      await waitFor(() => {
        expect(screen.getAllByText('Find devices on my network').length).toBeGreaterThan(0);
      });
      expect(screen.queryByText(UPSELL_REASON.smarthome)).toBeNull();
    });

    it('still renders the real System tab and enumerates audio devices', async () => {
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab('System');

      expect(await screen.findByText('Audio Devices')).toBeInTheDocument();
      expect(screen.getByText('Open Advanced Settings')).toBeInTheDocument();
      await waitFor(() => {
        expect(settingsHarness.refreshDevices).toHaveBeenCalled();
      });
    });
  });
});
