import { useState } from 'react';
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import SettingsModal from './SettingsModal';
import { applyTheme, getCurrentThemeMode, setAccent, THEME } from '../config';

const settingsHarness = vi.hoisted(() => ({
  settings: {
    theme: 'dark',
    ai_source: 'managed',
    active_music_provider_id: 'youtube_music',
  },
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

const apiFetchMock = vi.hoisted(() => vi.fn(() => Promise.resolve({
  json: () => Promise.resolve({ devices: [] }),
})));

// Preserve the boolean fixture while exposing the hook's exact save receipt.
const saveSettingsWithSnapshot = async values => {
  const ok = await settingsHarness.updateSettings(values);
  if (!ok) return { ok: false };
  const settings = settingsHarness.acknowledgedSettings?.(values) ?? { ...values };
  settingsHarness.settings = settings;
  return { ok: true, settings };
};

vi.mock('../hooks/useSettings', () => ({
  useSettings: () => ({ ...settingsHarness, saveSettingsWithSnapshot }),
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

vi.mock('./WakeWordSection', () => ({
  default: () => <div>Wake word settings body</div>,
}));

vi.mock('./AccentPicker', () => ({
  default: () => <div>Accent picker body</div>,
}));

describe('SettingsModal sidebar shell', () => {
  beforeEach(() => {
    // C-401: local-model detection and BYOK provider profiles are the
    // `desktop_ai` surface -- Tier-3 credentials that only exist on the desktop
    // app -- and SettingsModal now walls them behind isFeatureHidden. jsdom with
    // no Qt bridge IS the cloud surface (see utils/featureSurface.test.js), so
    // these desktop flows must declare the bridge instead of relying on the
    // pre-fix state where the surface was ungated everywhere. The cloud
    // behaviour of the same surface is covered by SettingsModal.desktopAi.test.jsx.
    window.viola = {};
    settingsHarness.settings = {
      theme: 'dark',
      ai_source: 'managed',
      active_music_provider_id: 'youtube_music',
    };
    settingsHarness.updateSettings.mockClear();
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue({
      json: () => Promise.resolve({ devices: [] }),
    });
  });

  afterEach(() => {
    delete window.viola;
  });

  it.each(['Cancel', 'Escape'])('restores saved theme and accent when dismissing a preview with %s', async (dismiss) => {
    settingsHarness.settings = { ...settingsHarness.settings, theme: 'dark', accent_color: '#123456' };
    applyTheme('dark');
    setAccent('#123456');
    const onClose = vi.fn();
    render(<SettingsModal isOpen onClose={onClose} initialTab="customize" />);
    fireEvent.click(await screen.findByRole('button', { name: /Color Theme:/ }));
    fireEvent.click(await screen.findByRole('option', { name: 'Light' }));
    expect(getCurrentThemeMode()).toBe('light');
    if (dismiss === 'Escape') fireEvent.keyDown(document, { key: 'Escape' });
    else fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalled();
    expect(getCurrentThemeMode()).toBe('dark');
    expect(THEME.colors.accent).toBe('#123456');
    expect(settingsHarness.updateSettings).not.toHaveBeenCalled();
  });

  it('keeps the selected theme after Save succeeds', async () => {
    applyTheme('dark');
    const onClose = vi.fn();
    render(<SettingsModal isOpen onClose={onClose} initialTab="customize" />);
    fireEvent.click(await screen.findByRole('button', { name: /Color Theme:/ }));
    fireEvent.click(await screen.findByRole('option', { name: 'Light' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(getCurrentThemeMode()).toBe('light');
    expect(settingsHarness.updateSettings).toHaveBeenCalledWith(expect.objectContaining({ theme: 'light' }));
  });

  it('renders the seven settings scopes and filters them from search', async () => {
    render(<SettingsModal isOpen onClose={vi.fn()} />);

    const expectedSections = [
      'Account',
      'AI & Agents',
      'Music',
      'Voice',
      'Connections',
      'Customize',
      'System',
    ];

    expectedSections.forEach((section) => {
      expect(screen.getByRole('button', { name: section })).toBeInTheDocument();
    });

    fireEvent.change(screen.getByRole('searchbox', { name: 'Search settings' }), {
      target: { value: 'telegram' },
    });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Connections' })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Account' })).not.toBeInTheDocument();
    });
  });

  it('keeps session replay disabled by default', () => {
    render(<SettingsModal isOpen onClose={vi.fn()} />);

    expect(screen.getByRole('switch', { name: 'Session replay consent' })).toHaveAttribute('aria-checked', 'false');
  });

  it('detects installed local models and saves the selected model', async () => {
    apiFetchMock.mockImplementation((path) => {
      if (path === '/v1/connectors?category=llm') {
        return Promise.resolve({ connectors: [] });
      }
      if (path === '/v1/settings/detect-local-ai') {
        return Promise.resolve({
          servers: [
            {
              type: 'ollama',
              name: 'Ollama',
              url: 'http://localhost:11434',
              models: [
                'viola-qwen25-coder-14b-32k:latest',
                'viola-qwen25-coder-7b-32k:latest',
              ],
            },
          ],
        });
      }
      return Promise.resolve({
        json: () => Promise.resolve({ devices: [] }),
      });
    });

    render(<SettingsModal isOpen onClose={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: 'AI & Agents' }));
    fireEvent.click(screen.getByRole('button', { name: /local model/i }));

    await waitFor(() => {
      expect(screen.getByText(/2 installed models/i)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /viola-qwen25-coder-14b-32k/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /viola-qwen25-coder-14b-32k/i }));
    fireEvent.click(screen.getByRole('option', { name: /viola-qwen25-coder-7b-32k/i }));
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => {
      expect(settingsHarness.updateSettings).toHaveBeenCalledWith(expect.objectContaining({
        ai_source: 'local',
        llm_provider: 'ollama',
        llm_base_url: 'http://localhost:11434',
        llm_model: 'viola-qwen25-coder-7b-32k:latest',
      }));
    });
  });

  it('loads BYOK provider presets and saves the selected preset base URL', async () => {
    settingsHarness.settings = {
      theme: 'dark',
      ai_source: 'byok',
      llm_provider: 'openai',
      llm_model: '',
      llm_base_url: '',
      llm_api_key: 'sk-test', // pragma: allowlist secret
      active_music_provider_id: 'youtube_music',
    };
    apiFetchMock.mockImplementation((path) => {
      if (path === '/v1/connectors?category=llm') {
        return Promise.resolve({
          connectors: [
            {
              id: 'llm.openai',
              category: 'llm',
              display_name: 'OpenAI',
              adapter: 'openai',
              provider_key: 'openai',
              tags: ['byok', 'cloud'],
              setting_hints: { ai_source: 'byok', llm_provider: 'openai' },
            },
            {
              id: 'llm.openrouter',
              category: 'llm',
              display_name: 'OpenRouter',
              adapter: 'openai_compatible',
              provider_key: 'openrouter',
              default_base_url: 'https://openrouter.ai/api/v1',
              default_models: ['~google/gemini-flash-latest'],
              tags: ['byok', 'cloud'],
              setting_hints: {
                ai_source: 'byok',
                llm_provider: 'openai_compatible',
                llm_base_url: 'https://openrouter.ai/api/v1',
              },
            },
          ],
        });
      }
      return Promise.resolve({
        json: () => Promise.resolve({ devices: [] }),
      });
    });

    render(<SettingsModal isOpen onClose={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: 'AI & Agents' }));
    fireEvent.click(screen.getByRole('button', { name: /Provider Connection/i }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Provider \/ preset: OpenAI/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /Provider \/ preset: OpenAI/i }));
    fireEvent.click(await screen.findByRole('option', { name: 'OpenRouter' }));
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => {
      expect(settingsHarness.updateSettings).toHaveBeenCalledWith(expect.objectContaining({
        ai_source: 'byok',
        llm_provider: 'openai_compatible',
        llm_base_url: 'https://openrouter.ai/api/v1',
        llm_model: '~google/gemini-flash-latest',
      }));
    });
  });

  it('saves BYOK providers as selected connector profiles without persisting the API key in settings', async () => {
    settingsHarness.settings = {
      theme: 'dark',
      ai_source: 'byok',
      llm_provider: 'openai',
      llm_model: 'gpt-4.1-mini',
      llm_base_url: '',
      llm_api_key: 'sk-test', // pragma: allowlist secret
      active_music_provider_id: 'youtube_music',
    };
    apiFetchMock.mockImplementation((path, options = {}) => {
      if (path === '/v1/connectors?category=llm') {
        return Promise.resolve({
          connectors: [
            {
              id: 'llm.openai',
              category: 'llm',
              display_name: 'OpenAI',
              adapter: 'openai',
              provider_key: 'openai',
              auth_type: 'api_key',
              requires_api_key: true,
              tags: ['byok', 'cloud'],
              setting_hints: { ai_source: 'byok', llm_provider: 'openai' },
            },
            {
              id: 'llm.openrouter',
              category: 'llm',
              display_name: 'OpenRouter',
              adapter: 'openai_compatible',
              provider_key: 'openrouter',
              auth_type: 'api_key',
              requires_api_key: true,
              default_base_url: 'https://openrouter.ai/api/v1',
              tags: ['byok', 'cloud'],
              setting_hints: {
                ai_source: 'byok',
                llm_provider: 'openai_compatible',
                llm_base_url: 'https://openrouter.ai/api/v1',
              },
            },
          ],
        });
      }
      if (path === '/v1/connectors/profiles?category=llm') {
        return Promise.resolve({
          profiles: [
            {
              profile_id: 'openrouter-main',
              connector_id: 'llm.openrouter',
              category: 'llm',
              display_name: 'OpenRouter',
              adapter: 'openai_compatible',
              auth_type: 'api_key',
              base_url: 'https://openrouter.ai/api/v1',
              model: 'gpt-4.1-mini',
              selected: true,
              secrets: { api_key: true },
              validation: {},
            },
          ],
        });
      }
      if (path === '/v1/connectors/profiles' && options.method === 'POST') {
        return Promise.resolve({
          profile: {
            profile_id: 'openrouter-main',
            connector_id: 'llm.openrouter',
            category: 'llm',
            display_name: 'OpenRouter',
            adapter: 'openai_compatible',
            auth_type: 'api_key',
            base_url: 'https://openrouter.ai/api/v1',
            model: 'gpt-4.1-mini',
            selected: true,
            secrets: { api_key: true },
            validation: {},
          },
        });
      }
      return Promise.resolve({
        json: () => Promise.resolve({ devices: [] }),
      });
    });

    render(<SettingsModal isOpen onClose={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: 'AI & Agents' }));
    fireEvent.click(screen.getByRole('button', { name: /Provider Connection/i }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Provider \/ preset: OpenAI/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /Provider \/ preset: OpenAI/i }));
    fireEvent.click(await screen.findByRole('option', { name: 'OpenRouter' }));
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Provider \/ preset: OpenRouter/i })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: /save & use profile/i }));

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalledWith('/v1/connectors/profiles', expect.objectContaining({
        method: 'POST',
        body: expect.any(String),
      }));
    });
    const saveCall = apiFetchMock.mock.calls.find(([path, options]) => (
      path === '/v1/connectors/profiles' && options?.method === 'POST'
    ));
    expect(JSON.parse(saveCall[1].body)).toEqual(expect.objectContaining({
      connector_id: 'llm.openrouter',
      api_key: 'sk-test', // pragma: allowlist secret
      selected: true,
    }));
    await waitFor(() => {
      expect(settingsHarness.updateSettings).toHaveBeenCalledWith(expect.objectContaining({
        ai_source: 'byok',
        llm_provider: 'openai_compatible',
        llm_base_url: 'https://openrouter.ai/api/v1',
        llm_model: 'gpt-4.1-mini',
        llm_api_key: '',
      }));
    });
  });

  it('surfaces Spotify identifier rejection during live connect polling', async () => {
    let spotifyStatusCalls = 0;
    apiFetchMock.mockImplementation((path, options = {}) => {
      if (path === '/v1/connectors?category=llm') {
        return Promise.resolve({ connectors: [] });
      }
      if (path === '/v1/spotify/status') {
        spotifyStatusCalls += 1;
        return Promise.resolve(
          spotifyStatusCalls === 1
            ? { logged_in: false, connected: false, auth_state: 'login_required' }
            : {
                logged_in: false,
                connected: false,
                auth_state: 'identifier_rejected',
                login_error_code: 'identifier_rejected',
              },
        );
      }
      if (path === '/v1/browser/auth/login/spotify' && options.method === 'POST') {
        return Promise.resolve({ login_url: 'https://accounts.spotify.com/login', provider: 'spotify', controller_attached: true });
      }
      return Promise.resolve({
        json: () => Promise.resolve({ devices: [] }),
      });
    });

    render(<SettingsModal isOpen onClose={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: 'Music' }));
    fireEvent.click(screen.getByRole('button', { name: /Spotify/i }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Sign In with Spotify/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /Sign In with Spotify/i }));

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalledWith('/v1/browser/auth/login/spotify', { method: 'POST' });
    });

    await waitFor(() => {
      expect(screen.getByText(/Spotify rejected that login identifier/i)).toBeInTheDocument();
    }, { timeout: 4000 });
  }, 10000);
  it.each([[1, '100'], [0.8, '80'], [80, '80'], [0, '0']])('displays stored TTS volume %s as percent %s', async (stored, shown) => {
    settingsHarness.settings = {...settingsHarness.settings, tts_volume: stored};
    render(<SettingsModal isOpen onClose={vi.fn()} initialTab="voice" />);
    const label = await screen.findByText('Assistant Volume');
    expect(label.parentElement.textContent).toBe(`Assistant Volume${shown}`);
  });

  it('stores one-percent assistant volume as 0.01 rather than full-volume 1', async () => {
    settingsHarness.settings = {...settingsHarness.settings, tts_volume: 1};
    render(<SettingsModal isOpen onClose={vi.fn()} initialTab="voice" />);
    const label = await screen.findByText('Assistant Volume');
    const track = label.parentElement.parentElement.querySelector('[data-hold-interactive]');
    vi.spyOn(track, 'getBoundingClientRect').mockReturnValue({left:0,width:100,right:100,top:0,bottom:44,height:44});
    fireEvent.mouseDown(track,{clientX:1});
    fireEvent.mouseUp(document);
    fireEvent.click(screen.getByRole('button',{name:'Save Changes'}));
    await waitFor(() => expect(settingsHarness.updateSettings).toHaveBeenCalledWith(expect.objectContaining({tts_volume:0.01})));
  });

  it('shows the shipped English tiny model in the accuracy selector', async () => {
    settingsHarness.settings = {...settingsHarness.settings, whisper_model:'tiny.en', stt_engine:'whisper_local'};
    render(<SettingsModal isOpen onClose={vi.fn()} initialTab="voice" />);
    fireEvent.click(await screen.findByRole('button',{name:/Voice Recognition & Advanced/}));
    expect(await screen.findByRole('button',{name:/Accuracy Level:.*English/})).toBeInTheDocument();
  });

  it('saves captured PTT keyboard metadata together', async () => {
    settingsHarness.settings = {...settingsHarness.settings, ptt_hotkey:'Space', ptt_hotkey_type:'mouse', ptt_hotkey_display:'Space'};
    render(<SettingsModal isOpen onClose={vi.fn()} initialTab="voice" />);
    const input = await screen.findByDisplayValue('Space');
    fireEvent.keyDown(input, {key:'k', code:'KeyK', ctrlKey:true});
    fireEvent.click(screen.getByRole('button',{name:'Save Changes'}));
    await waitFor(() => expect(settingsHarness.updateSettings).toHaveBeenCalledWith(expect.objectContaining({
      ptt_hotkey:'Ctrl+KeyK', ptt_hotkey_type:'keyboard', ptt_hotkey_display:'Ctrl+KeyK',
    })));
  });

  it('resets PTT shortcut metadata to the keyboard default', async () => {
    settingsHarness.settings = {...settingsHarness.settings, ptt_hotkey:'Ctrl+KeyK', ptt_hotkey_type:'mouse', ptt_hotkey_display:'Old shortcut'};
    render(<SettingsModal isOpen onClose={vi.fn()} initialTab="voice" />);
    fireEvent.click(await screen.findByRole('button',{name:'Reset hotkey to default', exact:true}));
    expect(screen.getByRole('textbox',{name:'Push-to-Talk Shortcut'})).toHaveValue('Space');
    fireEvent.click(screen.getByRole('button',{name:'Save Changes'}));
    await waitFor(() => expect(settingsHarness.updateSettings).toHaveBeenCalledWith(expect.objectContaining({
      ptt_hotkey:'Space', ptt_hotkey_type:'keyboard', ptt_hotkey_display:'Space',
    })));
  });

});


describe('SettingsModal dismissed save ownership', () => {
  const deferredSave = () => {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return { promise, resolve };
  };
  const editAndSave = async () => {
    fireEvent.click(await screen.findByRole('button', { name: /Color Theme:/ }));
    fireEvent.click(await screen.findByRole('option', { name: 'Light' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
  };
  beforeEach(() => {
    window.viola = {};
    settingsHarness.settings = { theme: 'dark', ai_source: 'managed', active_music_provider_id: 'youtube_music' };
    settingsHarness.saving = false;
    settingsHarness.error = null;
    settingsHarness.updateSettings.mockReset().mockResolvedValue(true);
  });
  afterEach(() => { delete window.viola; });

  it.each(['Cancel', 'Escape', 'Close', 'Backdrop'])('does not let an old save close reopened settings after %s', async dismiss => {
    const save = deferredSave();
    settingsHarness.updateSettings.mockReturnValueOnce(save.promise);
    const closed = vi.fn();
    function Host() {
      const [open, setOpen] = useState(true);
      return <>
        <button onClick={() => setOpen(true)}>Reopen settings</button>
        {open && <SettingsModal isOpen initialTab="customize" onClose={() => { closed(); setOpen(false); }} />}
      </>;
    }
    render(<Host />);
    await editAndSave();
    if (dismiss === 'Escape') fireEvent.keyDown(document, { key: 'Escape' });
    else if (dismiss === 'Backdrop') fireEvent.click(screen.getByRole('dialog', { name: 'Settings' }).parentElement);
    else fireEvent.click(screen.getByRole('button', { name: dismiss }));
    expect(closed).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Reopen settings' }));
    expect(await screen.findByRole('button', { name: 'Save Changes' })).toBeInTheDocument();
    await act(async () => save.resolve(true));
    expect(closed).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('dialog', { name: 'Settings' })).toBeInTheDocument();
  });

  it('drops a successful completion after parent-driven unmount', async () => {
    const save = deferredSave();
    settingsHarness.updateSettings.mockReturnValueOnce(save.promise);
    const closed = vi.fn();
    const view = render(<SettingsModal isOpen initialTab="customize" onClose={closed} />);
    await editAndSave();
    view.unmount();
    await act(async () => save.resolve(true));
    expect(closed).not.toHaveBeenCalled();
  });

  it('invalidates dismissal before parent unmount and preserves a new draft', async () => {
    const save = deferredSave();
    settingsHarness.updateSettings.mockReturnValueOnce(save.promise);
    const closed = vi.fn();
    render(<SettingsModal isOpen initialTab="customize" onClose={closed} />);
    await editAndSave();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    fireEvent.click(screen.getByRole('button', { name: /Color Theme:/ }));
    fireEvent.click(screen.getByRole('option', { name: 'Light' }));
    await act(async () => save.resolve(true));
    expect(closed).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Save Changes' })));
    expect(closed).toHaveBeenCalledTimes(2);
  });

  it('does not close for an older success after a newer submission was refused', async () => {
    const first = deferredSave(), second = deferredSave();
    settingsHarness.updateSettings.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const closed = vi.fn();
    render(<SettingsModal isOpen initialTab="customize" onClose={closed} />);
    await editAndSave();
    // The structured fixture publishes accepted snapshots like the real hook.
    // Stage a genuinely newer draft, rather than resubmitting the same value.
    fireEvent.click(screen.getByRole('button', { name: 'Voice' }));
    fireEvent.click(await screen.findByRole('button', { name: /Voice Input Mode:/ }));
    fireEvent.click(screen.getByRole('option', { name: 'Disabled' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
    expect(settingsHarness.updateSettings).toHaveBeenCalledTimes(2);
    expect(settingsHarness.updateSettings.mock.calls[0][0].theme).toBe('light');
    expect(settingsHarness.updateSettings.mock.calls[1][0].voice_mode).toBe('disabled');
    await act(async () => second.resolve(false));
    await act(async () => first.resolve(true));
    expect(closed).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled();
  });

  it('keeps a refused save open and closes only after an accepted retry', async () => {
    settingsHarness.updateSettings.mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const closed = vi.fn();
    render(<SettingsModal isOpen initialTab="customize" onClose={closed} />);
    await editAndSave();
    await act(async () => {});
    expect(closed).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Save Changes' })));
    expect(closed).toHaveBeenCalledTimes(1);
  });
});


describe('SettingsModal unsaved privacy draft acceptance', () => {
  beforeEach(() => {
    window.viola = {};
    settingsHarness.settings = { theme: 'dark', ai_source: 'managed', voice_mode: 'wake_word', mic_muted: false, tts_volume: 1 };
    settingsHarness.updateSettings.mockReset().mockResolvedValue(true);
  });
  afterEach(() => { delete window.viola; });

  it('preserves an unsaved Disabled choice while adopting unrelated server changes', async () => {
    const onClose = vi.fn();
    const view = render(<SettingsModal isOpen initialTab="voice" onClose={onClose} />);
    fireEvent.click(await screen.findByRole('button', { name: /Voice Input Mode:/ }));
    fireEvent.click(screen.getByRole('option', { name: 'Disabled' }));
    expect(screen.getByRole('button', { name: 'Voice Input Mode: Disabled' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled();
    settingsHarness.settings = { ...settingsHarness.settings, tts_volume: 0.42 };
    // A real hook snapshot updates through memo; this fixture forces that same render.
    view.rerender(<SettingsModal isOpen initialTab="voice" onClose={() => onClose()} />);
    expect(screen.getByRole('button', { name: 'Voice Input Mode: Disabled' })).toBeInTheDocument();
    expect((await screen.findByText('Assistant Volume')).parentElement.textContent).toBe('Assistant Volume42');
    expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled();
  });

  it('preserves an unsaved mute choice through a newer unrelated settings snapshot', async () => {
    const onClose = vi.fn();
    const view = render(<SettingsModal isOpen initialTab="voice" onClose={onClose} />);
    const mute = await screen.findByRole('switch', { name: 'Mute microphone' });
    fireEvent.click(mute);
    expect(mute).toBeChecked();
    settingsHarness.settings = { ...settingsHarness.settings, show_notifications: false };
    // A real hook snapshot updates through memo; this fixture forces that same render.
    view.rerender(<SettingsModal isOpen initialTab="voice" onClose={() => onClose()} />);
    expect(screen.getByRole('switch', { name: 'Mute microphone' })).toBeChecked();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Save Changes' })));
    expect(settingsHarness.updateSettings).toHaveBeenCalledWith(expect.objectContaining({ mic_muted: true, show_notifications: false }));
  });
});

describe('Independent open-draft source probes', () => {
  const pending = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };
  beforeEach(() => {
    window.viola = {};
    settingsHarness.settings = {theme:'dark', voice_mode:'wake_word', mic_muted:false, ai_source:'managed', active_music_provider_id:'youtube_music'};
    settingsHarness.saving = false;
    settingsHarness.error = null;
    settingsHarness.updateSettings.mockReset().mockResolvedValue(true);
  });
  afterEach(() => { delete window.viola; });
  const saveMuteAThenDraftDisabledB = async () => {
    fireEvent.click(await screen.findByRole('switch', {name:'Mute microphone'}));
    fireEvent.click(screen.getByRole('button', {name:'Save Changes'}));
    expect(settingsHarness.updateSettings).toHaveBeenCalledWith(expect.objectContaining({mic_muted:true,voice_mode:'wake_word'}));
    fireEvent.click(screen.getByRole('button', {name:/Voice Input Mode:/}));
    fireEvent.click(screen.getByRole('option', {name:'Disabled'}));
    expect(screen.getByRole('button', {name:/Voice Input Mode:/})).toHaveTextContent('Disabled');
  };
  it('save-A completion must not close a newer unsubmitted Disabled draft-B', async () => {
    const save = pending();
    settingsHarness.updateSettings.mockReturnValueOnce(save.promise);
    const closed = vi.fn();
    render(<SettingsModal isOpen initialTab="voice" onClose={closed} />);
    await saveMuteAThenDraftDisabledB();
    await act(async () => save.resolve(true));
    expect(closed).not.toHaveBeenCalled();
    expect(screen.getByRole('button', {name:'Save Changes'})).toBeEnabled();
  });
  it('save-A server acknowledgement must not replace a newer Disabled draft-B', async () => {
    const save = pending();
    settingsHarness.updateSettings.mockReturnValueOnce(save.promise);
    const closed = vi.fn();
    const view = render(<SettingsModal isOpen initialTab="voice" onClose={closed} />);
    await saveMuteAThenDraftDisabledB();
    settingsHarness.settings = {...settingsHarness.settings, mic_muted:true};
    view.rerender(<SettingsModal isOpen initialTab="voice" onClose={() => closed()} />);
    await act(async () => save.resolve(true));
    expect(screen.getByRole('button', {name:/Voice Input Mode:/})).toHaveTextContent('Disabled');
    expect(screen.getByRole('button', {name:'Save Changes'})).toBeEnabled();
  });
  it('Cancel returns previews to the newest backend snapshot rather than the opening snapshot', async () => {
    const closed = vi.fn();
    applyTheme('dark');
    const view = render(<SettingsModal isOpen initialTab="customize" onClose={closed} />);
    fireEvent.click(await screen.findByRole('button', {name:/Color Theme:/}));
    fireEvent.click(screen.getByRole('option', {name:'Light'}));
    expect(getCurrentThemeMode()).toBe('light');
    settingsHarness.settings = {...settingsHarness.settings, theme:'system'};
    view.rerender(<SettingsModal isOpen initialTab="customize" onClose={() => closed()} />);
    fireEvent.click(screen.getByRole('button', {name:'Cancel'}));
    expect(getCurrentThemeMode()).toBe('system');
    expect(closed).toHaveBeenCalledTimes(1);
  });
});


describe('SettingsModal acknowledged draft values', () => {
  beforeEach(() => {
    window.viola = {};
    settingsHarness.settings = { theme: 'dark', ai_source: 'managed', voice_mode: 'wake_word', mic_muted: false, tts_volume: 1 };
    settingsHarness.updateSettings.mockReset().mockResolvedValue(true);
    settingsHarness.acknowledgedSettings = null;
  });
  afterEach(() => { delete window.viola; delete settingsHarness.acknowledgedSettings; });
  it('adopts exact acknowledged values while retaining a later Disabled choice', async () => {
    let resolve;
    settingsHarness.updateSettings.mockReturnValueOnce(new Promise(done => { resolve = done; }));
    settingsHarness.acknowledgedSettings = values => ({ ...values, tts_volume: 0.42 });
    const closed = vi.fn();
    render(<SettingsModal isOpen initialTab="voice" onClose={closed} />);
    fireEvent.click(await screen.findByRole('switch', { name: 'Mute microphone' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
    fireEvent.click(screen.getByRole('button', { name: /Voice Input Mode:/ }));
    fireEvent.click(screen.getByRole('option', { name: 'Disabled' }));
    await act(async () => resolve(true));
    expect(closed).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: /Voice Input Mode:/ })).toHaveTextContent('Disabled');
    expect((await screen.findByText('Assistant Volume')).parentElement.textContent).toBe('Assistant Volume42');
    expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled();
  });
});
