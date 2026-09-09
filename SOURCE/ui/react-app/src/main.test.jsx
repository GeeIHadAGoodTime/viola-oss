import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { clearCachedClientApiKey, getClientApiKeySync, setBackendReady } from './config';
import { isCloudSurface } from './components/auth/cloudSurface';
import { setBackendSurface } from './utils/backendSurface';

vi.mock('./App', () => ({
  default: () => null,
}));

import { BackendReadyGate } from './main';

function mockHealthyBackend(surface = {}) {
  global.fetch = vi.fn((url) => {
    if (String(url).endsWith('/health')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => surface,
        text: async () => '',
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  });
}

describe('BackendReadyGate', () => {
  beforeEach(() => {
    cleanup();
    clearCachedClientApiKey();
    setBackendReady(false);
    setBackendSurface(null);
    window.__VIOLA_API_KEY__ = '';
    window.__VIOLA_WS_AUTH_TOKEN__ = '';
    window.history.replaceState({}, '', '/');
    delete window.viola;
    mockHealthyBackend();
  });

  afterEach(() => {
    cleanup();
    delete window.viola;
    setBackendSurface(null);
  });

  // The desktop surface is identified by the Qt bridge (window.viola). On the
  // desktop, a missing __VIOLA_API_KEY__ still shows the "Open Viola Desktop"
  // wall — the desktop behavior is unchanged.
  it('shows the desktop gate when on the desktop surface without an API key', async () => {
    window.viola = {}; // Qt bridge present → desktop surface.

    render(
      <BackendReadyGate apiKeyWaitMs={0}>
        <div>private app mounted</div>
      </BackendReadyGate>
    );

    expect(await screen.findByText('Open Viola Desktop')).toBeInTheDocument();
    expect(screen.queryByText('private app mounted')).not.toBeInTheDocument();
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(String(global.fetch.mock.calls[0][0])).toMatch(/\/health$/);
  });

  it('mounts the app when the desktop shell API key is present', async () => {
    window.viola = {}; // Qt bridge present → desktop surface.
    window.__VIOLA_API_KEY__ = 'desktop-key'; // pragma: allowlist secret

    render(
      <BackendReadyGate apiKeyWaitMs={0}>
        <div>private app mounted</div>
      </BackendReadyGate>
    );

    await waitFor(() => expect(screen.getByText('private app mounted')).toBeInTheDocument());
    expect(screen.queryByText('Open Viola Desktop')).not.toBeInTheDocument();
  });

  // Cloud surface: a plain browser (no Qt bridge) on Viola Cloud has no
  // desktop API key by design. The "Open Viola Desktop" wall must NOT show —
  // the app mounts so CloudAuthGate can render the account sign-in front door.
  it('mounts the app on the cloud surface without a desktop API key', async () => {
    mockHealthyBackend({ app_surface: 'cloud', build_profile: 'personal' });
    // No window.viola, no spoke params → cloud surface.
    render(
      <BackendReadyGate apiKeyWaitMs={0}>
        <div>private app mounted</div>
      </BackendReadyGate>
    );

    await waitFor(() => expect(screen.getByText('private app mounted')).toBeInTheDocument());
    expect(screen.queryByText('Open Viola Desktop')).not.toBeInTheDocument();
    expect(screen.queryByText('Connect to your Viola')).not.toBeInTheDocument();
    expect(isCloudSurface()).toBe(true);
  });

  it('requires an owner key on a backend-declared desktop browser', async () => {
    mockHealthyBackend({ app_surface: 'desktop', build_profile: 'personal' });
    render(<BackendReadyGate apiKeyWaitMs={0}><div>private app mounted</div></BackendReadyGate>);
    expect(await screen.findByLabelText('Local API key')).toBeInTheDocument();
    expect(screen.queryByText('private app mounted')).not.toBeInTheDocument();
    expect(isCloudSurface()).toBe(false);
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it('rejects an invalid browser key without caching it or mounting the dashboard', async () => {
    mockHealthyBackend({ app_surface: 'desktop', build_profile: 'personal' });
    const healthFetch = global.fetch;
    global.fetch = vi.fn((url, options) => String(url).endsWith('/v1/state')
      ? Promise.resolve({ ok: false, status: 401 }) : healthFetch(url, options));
    render(<BackendReadyGate apiKeyWaitMs={0}><div>private app mounted</div></BackendReadyGate>);
    fireEvent.change(await screen.findByLabelText('Local API key'), { target: { value: 'incorrect-fixture' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('That key was not accepted');
    expect(screen.queryByText('private app mounted')).not.toBeInTheDocument();
    expect(getClientApiKeySync()).toBeNull();
    expect(global.fetch).toHaveBeenCalledWith(expect.stringMatching(/\/v1\/state$/), expect.objectContaining({
      credentials: 'omit', redirect: 'error', headers: { 'X-API-Key': 'incorrect-fixture' },
    }));
  });

  it('opens the local dashboard only after API authorization and keeps the key in memory', async () => {
    mockHealthyBackend({ app_surface: 'desktop', build_profile: 'personal' });
    const healthFetch = global.fetch;
    let authorize;
    global.fetch = vi.fn((url, options) => String(url).endsWith('/v1/state')
      ? new Promise((resolve) => { authorize = resolve; }) : healthFetch(url, options));
    const storageWrite = vi.spyOn(Storage.prototype, 'setItem');
    render(<BackendReadyGate apiKeyWaitMs={0}><div>private app mounted</div></BackendReadyGate>);
    fireEvent.change(await screen.findByLabelText('Local API key'), { target: { value: 'accepted-fixture' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    expect(screen.queryByText('private app mounted')).not.toBeInTheDocument();
    expect(getClientApiKeySync()).toBeNull();
    authorize({ ok: true, status: 200 });
    expect(await screen.findByText('private app mounted')).toBeInTheDocument();
    expect(getClientApiKeySync()).toBe('accepted-fixture');
    expect(isCloudSurface()).toBe(false);
    expect(window.__VIOLA_API_KEY__).toBe('');
    expect(storageWrite.mock.calls.some((args) => args.includes('accepted-fixture'))).toBe(false);
  });

  // DEFENSE-IN-DEPTH: BackendReadyGate must NOT latch permanently on a missed
  // key window. On a desktop reload the DocumentCreation script normally has the
  // key present at first render, but if it ever arrives late the gate must
  // recover — show the wall, keep re-checking, and mount the app once the key
  // appears — rather than dead-end into the wall for the whole call (49c7106f).
  it('recovers and mounts the app when the desktop API key arrives after the wall shows', async () => {
    window.viola = {}; // Qt bridge present → desktop surface.
    // No key yet → the gate shows the "Open Viola Desktop" wall first.

    render(
      <BackendReadyGate apiKeyWaitMs={0}>
        <div>private app mounted</div>
      </BackendReadyGate>
    );

    expect(await screen.findByText('Open Viola Desktop')).toBeInTheDocument();
    expect(screen.queryByText('private app mounted')).not.toBeInTheDocument();

    // The injected credential global arrives late (post-render race recovery).
    window.__VIOLA_API_KEY__ = 'late-desktop-key'; // pragma: allowlist secret

    await waitFor(
      () => expect(screen.getByText('private app mounted')).toBeInTheDocument(),
      { timeout: 3000 }
    );
    expect(screen.queryByText('Open Viola Desktop')).not.toBeInTheDocument();
  });

  it('does not block paired spoke routes that authenticate with a scoped spoke token', async () => {
    window.history.replaceState({}, '', '/?room=kitchen&spoke_token=qr-token');

    render(
      <BackendReadyGate apiKeyWaitMs={0}>
        <div>spoke app mounted</div>
      </BackendReadyGate>
    );

    await waitFor(() => expect(screen.getByText('spoke app mounted')).toBeInTheDocument());
    expect(screen.queryByText('Open Viola Desktop')).not.toBeInTheDocument();
  });
});
