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
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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

vi.mock('../hooks/useSettings', () => ({
  useSettings: () => settingsHarness,
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
    settingsHarness.updateSettings.mockClear();
    settingsHarness.refreshDevices.mockClear();
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue({});
  });

  afterEach(() => {
    delete window.viola;
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
