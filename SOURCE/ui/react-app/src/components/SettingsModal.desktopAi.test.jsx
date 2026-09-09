/**
 * C-401 (SAFETY CORE, Tier-3 secrets custody): the AI-provider surface in
 * SettingsModal must be walled behind featureSurface.js's `desktop_ai` gate on
 * the cloud SPA.
 *
 * Pre-fix shape this file fails on: `desktop_ai` was declared in
 * DESKTOP_ONLY_FEATURES (featureSurface.js) with upsell copy written for it,
 * but nothing ever CALLED the gate -- so the cloud-served /app page rendered a
 * live `API Key` password field (placeholder "Paste provider key") inviting the
 * user to paste a Tier-3 BYOK provider secret into a cloud page, and the save
 * then 404'd against the LOCAL_ONLY `connectors` route group. CLAUDE.md Tier 3:
 * BYOK API keys are desktop-only; replicating them to cloud makes us a
 * credential broker.
 *
 * Surface detection follows the same signals as utils/featureSurface.test.js:
 * a `window.viola` Qt bridge means desktop (everything available); no bridge
 * and no spoke param means the cloud SPA.
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

const openAiTab = () => {
  fireEvent.click(screen.getByRole('button', { name: 'AI & Agents' }));
};

const setSource = (source) => {
  settingsHarness.settings = {
    theme: 'dark',
    ai_source: source,
    active_music_provider_id: 'youtube_music',
  };
};

// Verbatim from featureSurface.js DESKTOP_ONLY_FEATURE_COPY.desktop_ai — the
// text a cloud user must read where the key field used to be.
const UPSELL_TITLE = 'AI provider setup';
const UPSELL_REASON = /are set up in the desktop app/i;

describe('SettingsModal AI-provider surface (desktop_ai gate)', () => {
  beforeEach(() => {
    setSource('byok');
    settingsHarness.updateSettings.mockClear();
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue({ json: () => Promise.resolve({ devices: [] }) });
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

    it('renders the desktop upsell instead of the Tier-3 provider-key field', async () => {
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openAiTab();

      await screen.findByText(UPSELL_TITLE);
      expect(screen.getByText(UPSELL_REASON)).toBeInTheDocument();

      // The pre-fix defect itself: a password input for a provider key.
      expect(document.querySelector('input[type="password"]')).toBeNull();
      expect(screen.queryByPlaceholderText('Paste provider key')).toBeNull();
      expect(screen.queryByText('API Key')).toBeNull();
    });

    it('offers no BYOK / local / Codex source choice and no provider-connection panel', async () => {
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openAiTab();

      await screen.findByText(UPSELL_TITLE);
      expect(screen.queryByText('Your Own Key')).toBeNull();
      expect(screen.queryByText('Local Model')).toBeNull();
      expect(screen.queryByText('ChatGPT Plus')).toBeNull();
      expect(screen.queryByText('Provider Connection')).toBeNull();
      expect(screen.queryByText('Save & use profile')).toBeNull();
    });

    it('does not render the Codex sign-in card even when ai_source is codex', async () => {
      setSource('codex');
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openAiTab();

      await screen.findByText(UPSELL_TITLE);
      expect(screen.queryByText('Codex sign-in card')).toBeNull();
    });

    it('fires no request at the LOCAL_ONLY /v1/connectors family', async () => {
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openAiTab();

      await screen.findByText(UPSELL_TITLE);
      await waitFor(() => {
        const paths = apiFetchMock.mock.calls.map(([path]) => String(path));
        // Widened from `/v1/connectors/profiles` to the whole `/v1/connectors`
        // group: the connector CATALOG (`/v1/connectors?category=llm`, fired by
        // its own effect on every settings open) is a fourth call site into the
        // same LOCAL_ONLY group and was still ungated, so the narrower filter
        // walked straight past a dead fetch on every cloud modal open.
        expect(paths.filter((p) => p.startsWith('/v1/connectors'))).toEqual([]);
        expect(paths.filter((p) => p.includes('/v1/settings/detect-local-ai'))).toEqual([]);
      });
    });
  });

  describe('on the desktop app (Qt bridge present)', () => {
    beforeEach(() => {
      window.viola = {};
      window.history.replaceState({}, '', '/');
    });

    it('still renders the real BYOK provider-key field', async () => {
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openAiTab();

      // Provider Connection is an AdvancedSection, collapsed by default for
      // byok (it auto-expands only for `local`), so open it the way a user does.
      fireEvent.click(await screen.findByRole('button', { name: 'Provider Connection' }));
      const keyInput = await screen.findByPlaceholderText('Paste provider key');
      expect(keyInput).toHaveAttribute('type', 'password');
      expect(screen.queryByText(UPSELL_TITLE)).toBeNull();
    });

    it('still offers every AI source and the provider-connection panel', async () => {
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openAiTab();

      await screen.findByText('Your Own Key');
      expect(screen.getByText('Local Model')).toBeInTheDocument();
      expect(screen.getByText('ChatGPT Plus')).toBeInTheDocument();
      expect(screen.getByText('Provider Connection')).toBeInTheDocument();
    });

    it('still loads saved provider profiles from /v1/connectors/profiles', async () => {
      render(<SettingsModal isOpen onClose={vi.fn()} />);

      await waitFor(() => {
        const paths = apiFetchMock.mock.calls.map(([path]) => String(path));
        expect(paths.some((p) => p.includes('/v1/connectors/profiles'))).toBe(true);
      });
    });

    it('still renders the Codex sign-in card when ai_source is codex', async () => {
      setSource('codex');
      render(<SettingsModal isOpen onClose={vi.fn()} />);
      openAiTab();

      expect(await screen.findByText('Codex sign-in card')).toBeInTheDocument();
    });
  });
});
