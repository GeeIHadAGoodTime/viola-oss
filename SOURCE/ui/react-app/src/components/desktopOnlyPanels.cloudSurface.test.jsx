/**
 * #4226 (C-427), the three desktop-only panels that do NOT live inside
 * SettingsModal's own tab switch: the extension host, the wake-model manager
 * and the Advanced Settings window.
 *
 * Same defect as the four covered in SettingsModal.desktopOnlyPanels.test.jsx:
 * each was declared desktop-only in featureSurface.js but never consulted the
 * gate, so all three rendered live on the cloud SPA over a route group
 * backend/cloud_route_manifest.py does not register there --
 * `/v1/extensions/*` (LOCAL_ONLY, mutates local MCP/plugin process state),
 * `/v1/wake/*` (LOCAL_ONLY, switches and deletes on-disk model files) and the
 * window's own `/v1/diagnostics/*` + `/v1/settings/reset` (LOCAL_ONLY).
 *
 * The bar in both directions: on cloud the user reads a DesktopUpsell and the
 * SPA fires nothing at the unserved group; on desktop each panel still loads
 * and still works.
 */
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import ExtensionsSection from './extensions/ExtensionsSection';
import WakeWordSection from './WakeWordSection';
import AdvancedSettingsWindow from './advanced/AdvancedSettingsWindow';

const apiFetchMock = vi.hoisted(() => vi.fn(() => Promise.resolve({})));
const authFetchMock = vi.hoisted(() => vi.fn(() => Promise.resolve({
  ok: true,
  json: () => Promise.resolve({ ok: true, models: [], active_model_path: '' }),
})));

vi.mock('../hooks/useViolaApi', () => ({
  apiFetch: apiFetchMock,
  authFetch: authFetchMock,
}));

const requestedPaths = () => [
  ...apiFetchMock.mock.calls.map(([path]) => String(path)),
  ...authFetchMock.mock.calls.map(([path]) => String(path)),
];

const advancedSettings = {
  ai_source: 'byok',
  api_port: 8756,
  log_level: 'INFO',
  routing_reasoning_effort: 'low',
  agent_reasoning_effort: 'medium',
  codex_reasoning_effort: 'medium',
};

const renderAdvanced = () => render(
  <AdvancedSettingsWindow
    isOpen
    onClose={vi.fn()}
    settings={advancedSettings}
    onSettingChange={vi.fn()}
    onSettingsChange={vi.fn()}
  />,
);

describe('desktop-only panels outside the SettingsModal tab switch (#4226)', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue({});
    authFetchMock.mockReset();
    authFetchMock.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ ok: true, models: [], active_model_path: '' }),
    });
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

    it('walls the extension host and dials none of /v1/extensions', async () => {
      render(<ExtensionsSection />);

      await screen.findByText(/MCP servers and plugins run inside the desktop app/i);
      expect(screen.queryByText('Loading extensions...')).toBeNull();
      await waitFor(() => {
        expect(requestedPaths().filter((p) => p.startsWith('/v1/extensions'))).toEqual([]);
      });
    });

    it('walls the wake-model manager and dials none of /v1/wake', async () => {
      render(<WakeWordSection />);

      await screen.findByText(/Custom wake-word models are stored and switched on your machine/i);
      await waitFor(() => {
        expect(requestedPaths().filter((p) => p.startsWith('/v1/wake'))).toEqual([]);
      });
    });

    it('collapses the Advanced Settings window to one desktop-settings card', async () => {
      renderAdvanced();

      await screen.findByText(/settings for the installed desktop app/i);
      // The pre-fix window offered a live reset button and local-log probes.
      expect(screen.queryByText('Reset to Defaults')).toBeNull();
      expect(screen.queryByText('Open Diagnostics')).toBeNull();
    });
  });

  describe('on the desktop app (Qt bridge present)', () => {
    beforeEach(() => {
      window.viola = {};
      window.history.replaceState({}, '', '/');
    });

    it('still loads the extension host from /v1/extensions', async () => {
      render(<ExtensionsSection />);

      await waitFor(() => {
        expect(requestedPaths().some((p) => p.startsWith('/v1/extensions'))).toBe(true);
      });
      expect(screen.queryByText(/MCP servers and plugins run inside the desktop app/i)).toBeNull();
    });

    it('still loads wake models from /v1/wake/models', async () => {
      render(<WakeWordSection />);

      await waitFor(() => {
        expect(requestedPaths().some((p) => p.startsWith('/v1/wake/models'))).toBe(true);
      });
    });

    it('still renders the real Advanced Settings sections', async () => {
      renderAdvanced();

      expect(await screen.findByText('Advanced Settings')).toBeInTheDocument();
      expect(screen.queryByText(/settings for the installed desktop app/i)).toBeNull();
    });
  });
});
