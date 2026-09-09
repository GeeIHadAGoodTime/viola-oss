/**
 * #4789: the Cloud Sync consent row.
 *
 * Pre-fix shape this file fails on: the row read "Sync settings and playlists
 * across devices" and rendered an always-live toggle that called
 * `updateLocal('consent_cloud_sync', ...)`. Neither half was true. Nothing in the
 * desktop app pushes settings or playlists anywhere (Tier2SyncClient has no
 * production caller), and consent belongs to the ACCOUNT — the cloud row is what
 * every Tier-2 gate reads — so with nobody signed in the switch had nothing to
 * consent on and stored a local flag that meant nothing.
 *
 * The bar in both directions: signed out, the control is inert and says why;
 * signed in, it is live and the copy claims only what the account row buys.
 */
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
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

const authHarness = vi.hoisted(() => ({ value: null }));

vi.mock('../hooks/useSettings', () => ({
  useSettings: () => settingsHarness,
}));

vi.mock('../hooks/useAuth', () => ({
  useOptionalAuth: () => authHarness.value,
  useAuth: () => authHarness.value,
  usePlan: () => ({}),
  AuthProvider: ({ children }) => children,
}));

vi.mock('../hooks/useViolaApi', () => ({
  apiFetch: vi.fn(() => Promise.resolve({})),
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

// The Privacy & Data section lives on the Account tab, which is the default tab.
const cloudSyncToggle = () => screen.getByRole('switch', { name: 'Cloud sync consent' });

describe('Cloud Sync consent row (#4789)', () => {
  beforeEach(() => {
    settingsHarness.settings = { theme: 'dark', consent_cloud_sync: false };
    settingsHarness.error = null;
    settingsHarness.updateSettings.mockClear();
    authHarness.value = null;
    // Desktop surface: the Qt bridge is what isCloudSurface() keys off.
    window.viola = {};
  });

  afterEach(() => {
    delete window.viola;
    window.history.replaceState({}, '', '/');
  });

  it('is inert and explains why when no account is signed in', () => {
    render(<SettingsModal isOpen onClose={vi.fn()} />);

    const toggle = cloudSyncToggle();
    fireEvent.click(toggle);

    expect(screen.getByText(/Sign in to your Viola account to use cloud sync/i)).toBeInTheDocument();
    // A disabled Toggle swallows the click, so the switch never flips and the
    // Save button stays inert -- nothing local is recorded.
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByRole('button', { name: /Save Changes/i })).toBeDisabled();
  });

  it('is live when an account is signed in', () => {
    authHarness.value = { isLoggedIn: true, user: { id: 'u1' } };
    render(<SettingsModal isOpen onClose={vi.fn()} />);

    const toggle = cloudSyncToggle();
    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByRole('button', { name: /Save Changes/i })).toBeEnabled();
  });

  it('does not promise to sync this desktop across devices', () => {
    authHarness.value = { isLoggedIn: true, user: { id: 'u1' } };
    render(<SettingsModal isOpen onClose={vi.fn()} />);

    expect(screen.getByText('Cloud Sync')).toBeInTheDocument();
    expect(screen.queryByText(/across devices/i)).toBeNull();
    expect(screen.getByText(/does not upload its own settings yet/i)).toBeInTheDocument();
  });

  it('shows the refusal reason when the account row could not be written', () => {
    authHarness.value = { isLoggedIn: true, user: { id: 'u1' } };
    settingsHarness.error = "Cloud sync was not changed: Viola couldn't reach your account. Nothing was saved. Try again.";
    render(<SettingsModal isOpen onClose={vi.fn()} />);

    expect(screen.getByText(/Nothing was saved/i)).toBeInTheDocument();
  });
});
