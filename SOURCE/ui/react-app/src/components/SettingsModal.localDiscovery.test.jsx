import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../test/test-utils';
import SettingsModal from './SettingsModal';

const harness = vi.hoisted(() => ({
  settings: {}, loading: false, saving: false, error: null,
  devices: { input: [], output: [] }, playlists: [],
  saveSettingsWithSnapshot: vi.fn(), syncPlaylists: vi.fn(), renamePlaylist: vi.fn(),
  setDefaultPlaylist: vi.fn(), deletePlaylist: vi.fn(), clearError: vi.fn(),
  refreshDevices: vi.fn(), refreshPlaylists: vi.fn(),
}));
vi.mock('../hooks/useSettings', () => ({ useSettings: () => harness }));
vi.mock('./AccountTab', () => ({default: () => <div>Account fixture</div>, CalendarSettings: () => <div>Calendar fixture</div>}));
vi.mock('./MessagingTab', () => ({default: () => <div>Messaging fixture</div>}));
vi.mock('./ai/CodexAuthCard', () => ({default: () => <div>Codex fixture</div>}));
const response = (data, status = 200) => new Response(JSON.stringify({ok: status === 200, data}), {
  status, headers: {'Content-Type': 'application/json'},
});
const detected = {servers: [{type: 'ollama', name: 'Fixture Ollama', url: 'http://127.0.0.1:11434', models: ['fixture-small'], running: true}]};

describe('Local model discovery owns only the user choice which started it', () => {
  let reads, requests, unexpected, profiles, profilePick;
  beforeEach(() => {
    harness.settings = {theme: 'dark', ai_source: 'byok', ai_enabled: true, active_music_provider_id: 'youtube_music'};
    harness.saveSettingsWithSnapshot.mockReset();
    harness.saveSettingsWithSnapshot.mockImplementation(async settings => {
      harness.settings = settings;
      return {ok: true, settings};
    });
    reads = []; requests = []; unexpected = []; profiles = [];
    profilePick = vi.fn(async () => {
      profiles = profiles.map(profile => ({...profile, selected: true}));
      return response({profile: profiles[0]});
    });
    window.viola = {};
    window.__VIOLA_API_KEY__ = 'synthetic-settings-discovery';
    vi.stubGlobal('fetch', async (path, options) => {
      requests.push({path, method: options?.method || 'GET'});
      if (path === '/v1/connectors?category=llm') return response({connectors: []});
      if (path === '/v1/connectors/profiles?category=llm') return response({profiles});
      if (path === '/v1/connectors/profiles/saved-local/select' || path === '/v1/connectors/profiles') return profilePick();
      if (path === '/v1/settings/detect-local-ai') {
        let resolve;
        const promise = new Promise(accept => { resolve = accept; });
        reads.push({ promise, resolve });
        return promise;
      }
      unexpected.push(path);
      throw new Error('Unexpected synthetic request');
    });
  });
  afterEach(() => {
    expect(unexpected).toEqual([]);
    vi.restoreAllMocks(); vi.unstubAllGlobals();
    delete window.viola; delete window.__VIOLA_API_KEY__;
  });
  async function start() {
    let view;
    await act(async () => { view = render(<SettingsModal isOpen onClose={vi.fn()} initialTab="ai" />); });
    fireEvent.click(screen.getByRole('button', {name: /Local Model Run on this machine/}));
    await waitFor(() => expect(reads).toHaveLength(1));
    return view;
  }
  async function saveAndRead() {
    await act(async () => fireEvent.click(screen.getByRole('button', {name: /^Save Changes$/})));
    expect(harness.saveSettingsWithSnapshot).toHaveBeenCalledTimes(1);
    return harness.saveSettingsWithSnapshot.mock.calls[0][0];
  }
  it('adopts healthy discovery while the initiating Local Model choice remains current', async () => {
    await start();
    await act(async () => reads[0].resolve(response(detected)));
    const saved = await saveAndRead();
    expect(saved.ai_source).toBe('local');
    expect(saved.llm_model).toBe('fixture-small');
    expect(saved.llm_base_url).toBe('http://127.0.0.1:11434');
  });
  it.each(['healthy', 'empty', 'failed'])('preserves newer Your Own Key choice when older discovery is %s', async outcome => {
    await start();
    fireEvent.click(screen.getByRole('button', {name: /Your Own Key Bring your own API key/}));
    await act(async () => reads[0].resolve(outcome === 'healthy' ? response(detected) : outcome === 'empty' ? response({servers: []}) : response(null, 503)));
    expect((await saveAndRead()).ai_source).toBe('byok');
  });
  const answer = (outcome, model = 'fixture-new') => outcome === 'healthy'
    ? response({servers: [{...detected.servers[0], models: [model]}]})
    : outcome === 'empty' ? response({servers: []}) : response(null, 503);

  it.each([
    ['Viola Managed Built-in defaults', 'managed'],
    ['ChatGPT Plus Use your subscription', 'codex'],
  ])('preserves a newer %s selection', async (name, value) => {
    await start();
    fireEvent.click(screen.getByRole('button', {name}));
    await act(async () => reads[0].resolve(response(detected)));
    expect((await saveAndRead()).ai_source).toBe(value);
  });

  it.each(['healthy', 'empty', 'failed'])('retires Local → BYOK → Local generations before an older %s response', async outcome => {
    await start();
    fireEvent.click(screen.getByRole('button', {name: /Your Own Key Bring your own API key/}));
    fireEvent.click(screen.getByRole('button', {name: /Local Model Run on this machine/}));
    await waitFor(() => expect(reads).toHaveLength(2));
    await act(async () => reads[0].resolve(answer(outcome, 'stale-model')));
    expect(screen.getByRole('button', {name: 'Detecting...'})).toBeDisabled();
    expect(screen.queryByText('No local servers found.')).not.toBeInTheDocument();
    expect(screen.queryByText('Local server detection failed.')).not.toBeInTheDocument();
    await act(async () => reads[1].resolve(answer('healthy', 'current-model')));
    expect((await saveAndRead()).llm_model).toBe('current-model');
  });

  it.each(['healthy', 'empty', 'failed'])('does not overwrite a settled newer generation when old discovery is %s', async outcome => {
    await start();
    fireEvent.click(screen.getByRole('button', {name: /Your Own Key Bring your own API key/}));
    fireEvent.click(screen.getByRole('button', {name: /Local Model Run on this machine/}));
    await waitFor(() => expect(reads).toHaveLength(2));
    await act(async () => reads[1].resolve(answer('healthy', 'current-model')));
    await act(async () => reads[0].resolve(answer(outcome, 'stale-model')));
    expect((await saveAndRead()).llm_model).toBe('current-model');
    expect(screen.queryByText('No local servers found.')).not.toBeInTheDocument();
    expect(screen.queryByText('Local server detection failed.')).not.toBeInTheDocument();
  });

  it.each(['model', 'baseUrl'])('retains a newer manual %s edit', async field => {
    await start();
    if (field === 'model') {
      fireEvent.change(screen.getByPlaceholderText('Click "Find installed models" or type one'), {target: {value: 'my-selected-model'}});
    } else {
      fireEvent.change(screen.getByDisplayValue('http://localhost:11434'), {target: {value: 'http://localhost:12345'}});
    }
    await act(async () => reads[0].resolve(response(detected)));
    const saved = await saveAndRead();
    expect(saved.ai_source).toBe('local');
    expect(saved[field === 'model' ? 'llm_model' : 'llm_base_url']).toBe(field === 'model' ? 'my-selected-model' : 'http://localhost:12345');
  });

  it.each(['empty', 'failed'])('allows current %s discovery to finish and manually retry', async outcome => {
    await start();
    await act(async () => reads[0].resolve(answer(outcome)));
    expect(screen.getByText(outcome === 'empty' ? 'No local servers found.' : 'Local server detection failed.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', {name: 'Find installed models'}));
    await waitFor(() => expect(reads).toHaveLength(2));
    await act(async () => reads[1].resolve(answer('healthy', 'retry-model')));
    expect((await saveAndRead()).llm_model).toBe('retry-model');
  });

  it.each(['healthy', 'empty', 'failed'])('retires %s discovery across isOpen close/reopen', async outcome => {
    const view = await start();
    await act(async () => view.rerender(<SettingsModal isOpen={false} onClose={vi.fn()} initialTab="ai" />));
    await act(async () => view.rerender(<SettingsModal isOpen onClose={vi.fn()} initialTab="ai" />));
    fireEvent.click(screen.getByRole('button', {name: /ChatGPT Plus Use your subscription/}));
    await act(async () => reads[0].resolve(answer(outcome)));
    expect((await saveAndRead()).ai_source).toBe('codex');
  });

  it('does not affect a fresh mounted modal after the original modal unmounts', async () => {
    const view = await start();
    view.unmount();
    await act(async () => render(<SettingsModal isOpen onClose={vi.fn()} initialTab="ai" />));
    fireEvent.click(screen.getByRole('button', {name: /ChatGPT Plus Use your subscription/}));
    await act(async () => reads[0].resolve(response(detected)));
    expect((await saveAndRead()).ai_source).toBe('codex');
  });

  it('keeps a pending current discovery single-flight through the disabled button', async () => {
    await start();
    const button = screen.getByRole('button', {name: 'Detecting...'});
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(reads).toHaveLength(1);
    await act(async () => reads[0].resolve(response(detected)));
    expect((await saveAndRead()).llm_model).toBe('fixture-small');
  });

  it('keeps a reopened unedited draft unchanged after an old healthy result', async () => {
    const view = await start();
    await act(async () => view.rerender(<SettingsModal isOpen={false} onClose={vi.fn()} initialTab="ai" />));
    await act(async () => view.rerender(<SettingsModal isOpen onClose={vi.fn()} initialTab="ai" />));
    expect(screen.getByRole('button', {name: 'Save Changes'})).toBeDisabled();
    await act(async () => reads[0].resolve(response(detected)));
    expect(screen.getByRole('button', {name: 'Save Changes'})).toBeDisabled();
    expect(harness.saveSettingsWithSnapshot).not.toHaveBeenCalled();
  });

  it.each(['Use', 'Save & use profile'])('keeps an acknowledged %s profile when earlier discovery finishes', async buttonName => {
    profiles = [{profile_id: 'saved-local', connector_id: 'ollama', category: 'llm', display_name: 'Saved local fixture',
      adapter: 'ollama', auth_type: 'none', base_url: 'http://localhost:12345', model: 'saved-model', enabled: true, selected: false}];
    await start();
    fireEvent.click(screen.getByRole('button', {name: buttonName}));
    await waitFor(() => expect(harness.saveSettingsWithSnapshot).toHaveBeenCalledTimes(1));
    expect(harness.saveSettingsWithSnapshot.mock.calls[0][0].llm_model).toBe('saved-model');
    await act(async () => reads[0].resolve(response(detected)));
    expect(screen.getByText('saved-model (typed)')).toBeInTheDocument();
    expect(screen.getByRole('button', {name: 'Save Changes'})).toBeDisabled();
  });

  function savedProfile() {
    return {profile_id: 'saved-local', connector_id: 'ollama', category: 'llm', display_name: 'Saved local fixture',
      adapter: 'ollama', auth_type: 'none', base_url: 'http://localhost:12345', model: 'saved-model', enabled: true, selected: false};
  }
  async function renderProfiles() {
    harness.settings = {...harness.settings, ai_source: 'local', llm_provider: 'ollama', llm_model: 'prior-stored-model'};
    profiles = [savedProfile()];
    await act(async () => render(<SettingsModal isOpen onClose={vi.fn()} initialTab="ai" />));
    await screen.findByRole('button', {name: 'Use'});
  }
  it.each(['Use', 'Save & use profile'])('keeps discovery begun after an older %s request', async buttonName => {
    await renderProfiles();
    let settleProfile;const profileReply = new Promise(resolve => { settleProfile = resolve; });
    profilePick.mockReturnValueOnce(profileReply);
    await act(async () => fireEvent.click(screen.getByRole('button', {name: buttonName})));
    expect(profilePick).toHaveBeenCalledTimes(1);
    const discover = screen.getByRole('button', {name: 'Find installed models'});
    expect(discover).toBeEnabled();
    fireEvent.click(discover);
    await waitFor(() => expect(reads).toHaveLength(1));
    profiles = [{...savedProfile(), selected: true}];
    await act(async () => settleProfile(response({profile: profiles[0]})));
    await waitFor(() => expect(harness.saveSettingsWithSnapshot).toHaveBeenCalledTimes(1));
    await act(async () => reads[0].resolve(answer('healthy', 'newer-discovered-model')));
    const save = screen.getByRole('button', {name: /^Save Changes$/});
    expect(save).toBeEnabled();
    await act(async () => fireEvent.click(save));
    expect(harness.saveSettingsWithSnapshot).toHaveBeenCalledTimes(2);
    expect(harness.saveSettingsWithSnapshot.mock.calls[1][0].llm_model).toBe('newer-discovered-model');
  });
  it('preserves normal saved-profile selection with no discovery', async () => {
    await renderProfiles();
    await act(async () => fireEvent.click(screen.getByRole('button', {name: 'Use'})));
    await waitFor(() => expect(harness.saveSettingsWithSnapshot).toHaveBeenCalledTimes(1));
    expect(harness.saveSettingsWithSnapshot.mock.calls[0][0].llm_model).toBe('saved-model');
    expect(screen.getByText('saved-model (typed)')).toBeInTheDocument();
    expect(screen.getByRole('button', {name: /^Save Changes$/})).toBeDisabled();
  });

});
