/**
 * Owner-switched "Coming soon" (build 12). The owner can switch a feature off
 * from the cloud; its Settings option then shows "Coming soon" and its controls
 * are disabled, not hidden. With nothing switched off every option renders
 * exactly as before.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import SettingsModal from './SettingsModal';
import WakeWordSection from './WakeWordSection';
import { AccountTab } from './AccountTab';
import { ComingSoon, ComingSoonContext } from './settings/ComingSoon';

const settingsHarness = vi.hoisted(() => ({
  settings: {},
  comingSoon: undefined,
  loading: false,
  saving: false,
  error: null,
  devices: { input: [], output: [] },
  playlists: [],
  updateSettings: vi.fn(() => Promise.resolve(true)),
  saveSettingsWithSnapshot: vi.fn(() => Promise.resolve({ ok: true, settings: {} })),
  syncPlaylists: vi.fn(() => Promise.resolve({ ok: true, synced_count: 0 })),
  renamePlaylist: vi.fn(),
  setDefaultPlaylist: vi.fn(),
  deletePlaylist: vi.fn(),
  clearError: vi.fn(),
  refreshDevices: vi.fn(),
  refreshPlaylists: vi.fn(),
}));

const apiFetchMock = vi.hoisted(() => vi.fn(() => Promise.resolve({})));
const authFetchMock = vi.hoisted(() => vi.fn());

vi.mock('../hooks/useSettings', () => ({
  useSettings: () => {
    const { comingSoon, ...rest } = settingsHarness;
    return comingSoon === undefined ? rest : { ...rest, comingSoon };
  },
}));

vi.mock('../hooks/useAuth', () => ({
  useOptionalAuth: () => null,
  useAuth: () => ({
    user: null,
    subscription: null,
    loading: false,
    isLoggedIn: false,
    logout: vi.fn(async () => ({ success: true })),
    passwordRecovery: false,
    restoreDesktopSession: vi.fn(async () => false),
  }),
  usePlan: () => ({}),
  AuthProvider: ({ children }) => children,
}));

vi.mock('../auth/useAuth', () => ({
  useAuth: () => ({ status: 'signedOut', user: null, signOut: vi.fn(async () => {}) }),
}));

vi.mock('../hooks/useViolaApi', () => ({
  apiFetch: apiFetchMock,
  authFetch: authFetchMock,
}));

vi.mock('./ai/CodexAuthCard', () => ({
  default: () => <div>Codex sign-in card</div>,
}));

const baseSettings = {
  theme: 'dark',
  ai_source: 'managed',
  ai_enabled: true,
  agent_enabled: true,
  agent_autonomy: 'symphony',
  active_music_provider_id: 'youtube_music',
  tts_enabled: true,
};

// React useId values (":r0:") differ between two renders; nothing else may.
const normalized = (html) => html.replace(/:r[0-9a-z]+:/g, ':id:');

const openTab = (name) => {
  fireEvent.click(screen.getByRole('button', { name }));
};

beforeEach(() => {
  window.viola = { openExternalUrl: vi.fn() };
  settingsHarness.settings = { ...baseSettings };
  settingsHarness.comingSoon = undefined;
  apiFetchMock.mockReset();
  apiFetchMock.mockResolvedValue({});
  authFetchMock.mockReset();
  authFetchMock.mockResolvedValue({ ok: true, json: async () => ({ ok: true, data: {} }) });
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) })));
});

afterEach(() => {
  delete window.viola;
  vi.unstubAllGlobals();
});

describe('ComingSoon wrapper', () => {
  const Option = () => <button type="button">Connect</button>;

  it('renders the option unchanged when nothing is switched off', () => {
    const bare = render(<Option />).container.innerHTML;
    const wrapped = render(<ComingSoon option="connections.smart_home"><Option /></ComingSoon>).container.innerHTML;
    const otherOff = render(
      <ComingSoonContext.Provider value={['music.source.spotify']}>
        <ComingSoon option="connections.smart_home"><Option /></ComingSoon>
      </ComingSoonContext.Provider>,
    ).container.innerHTML;
    expect(wrapped).toBe(bare);
    expect(otherOff).toBe(bare);
  });

  it('labels the option and disables its controls when it is switched off', () => {
    render(
      <ComingSoonContext.Provider value={['connections.smart_home']}>
        <ComingSoon option="connections.smart_home"><Option /></ComingSoon>
      </ComingSoonContext.Provider>,
    );
    expect(screen.getByText('Coming soon')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Connect' })).toBeDisabled();
  });
});

describe('Settings with nothing switched off', () => {
  it.each(['Music', 'Connections', 'AI & Agents', 'Account', 'System'])(
    'the %s tab renders the same and says nothing about coming soon',
    async (tab) => {
      const first = render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab(tab);
      await new Promise((resolve) => { setTimeout(resolve, 100); });
      const withoutKey = normalized(first.container.innerHTML);
      expect(screen.queryByText('Coming soon')).toBeNull();
      first.unmount();

      settingsHarness.comingSoon = [];
      const second = render(<SettingsModal isOpen onClose={vi.fn()} />);
      openTab(tab);
      await new Promise((resolve) => { setTimeout(resolve, 100); });
      expect(normalized(second.container.innerHTML)).toBe(withoutKey);
    },
  );
});

describe('Settings with a feature switched off', () => {
  it('Spotify: the music source shows Coming soon and cannot be picked; YouTube still can', async () => {
    settingsHarness.comingSoon = ['music.source.spotify'];
    render(<SettingsModal isOpen onClose={vi.fn()} />);
    openTab('Music');
    const spotify = (await screen.findByText('Spotify')).closest('button');
    expect(spotify).toBeDisabled();
    expect(within(spotify).getByText('Coming soon')).toBeInTheDocument();
    expect(screen.getByText('YouTube').closest('button')).not.toBeDisabled();
  });

  it('smart home: the Home Assistant setup is labelled and disabled; Calendar is not', async () => {
    settingsHarness.comingSoon = ['connections.smart_home'];
    const { container } = render(<SettingsModal isOpen onClose={vi.fn()} />);
    openTab('Connections');
    const marked = container.querySelector('[data-coming-soon="connections.smart_home"]');
    expect(marked).not.toBeNull();
    expect(within(marked).getByText('Coming soon')).toBeInTheDocument();
    expect(marked.querySelector('fieldset')).toBeDisabled();
    expect(within(marked).queryByText('Calendar')).toBeNull();
    expect(screen.getAllByText('Coming soon')).toHaveLength(1);
  });

  it('payment methods: the saved-card section is labelled and disabled', async () => {
    settingsHarness.comingSoon = ['account.payment_methods'];
    const { container } = render(<SettingsModal isOpen onClose={vi.fn()} />);
    openTab('Account');
    const marked = container.querySelector('[data-coming-soon="account.payment_methods"]');
    expect(marked).not.toBeNull();
    expect(marked.querySelector('fieldset')).toBeDisabled();
  });

  it('agent capabilities: Browser, Email and Desktop control show Coming soon', async () => {
    settingsHarness.comingSoon = [
      'ai_agents.capability.browser',
      'ai_agents.capability.email',
      'ai_agents.capability.desktop_control',
    ];
    render(<SettingsModal isOpen onClose={vi.fn()} />);
    openTab('AI & Agents');
    for (const name of ['Browser', 'Email', 'Desktop control']) {
      const row = (await screen.findByText(name)).parentElement;
      expect(within(row).getByText('Coming soon')).toBeInTheDocument();
    }
    expect(within(screen.getByText('Memory').parentElement).queryByText('Coming soon')).toBeNull();
  });
});

describe('Options outside SettingsModal', () => {
  it('phone calls: the phone settings are labelled and their switches disabled', async () => {
    const onSettingChange = vi.fn();
    render(
      <ComingSoonContext.Provider value={['account.phone_calls']}>
        <AccountTab settings={{}} onSettingChange={onSettingChange} />
      </ComingSoonContext.Provider>,
    );
    const toggle = await screen.findByRole('switch', { name: 'Record phone calls' });
    expect(toggle).toBeDisabled();
    fireEvent.click(toggle);
    expect(onSettingChange).not.toHaveBeenCalled();
    expect(screen.getByText('Coming soon')).toBeInTheDocument();
  });

  it('custom wake word: Create is disabled with Coming soon; picking an existing word still works', async () => {
    const models = [
      { model_id: 'default', name: 'Viola', is_default: true, is_active: true },
      { model_id: 'athena', name: 'Athena', is_active: false },
    ];
    authFetchMock.mockImplementation(async (path) => ({
      ok: true,
      json: async () => ({
        ok: true,
        data: String(path).includes('/v1/wake/training/status') ? { available: true } : { models },
      }),
    }));
    render(
      <ComingSoonContext.Provider value={['customize.wake_word_training']}>
        <WakeWordSection />
      </ComingSoonContext.Provider>,
    );
    await screen.findByText('Athena');
    expect(screen.getByText('Coming soon')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled();
    const useWord = screen.getAllByRole('button', { name: 'Use this word' }).find((button) => !button.disabled);
    expect(useWord).toBeDefined();
  });
});
