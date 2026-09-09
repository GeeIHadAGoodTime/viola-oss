/**
 * Which provider buttons the sign-in screen offers.
 *
 * "Continue with Apple" shipped on this screen while production GoTrue
 * answered `400 Unsupported provider: provider is not enabled` to every press,
 * and "Continue with Google" shipped on surfaces where the flow cannot finish
 * at all. Both are the same bug: an offered button that cannot work.
 *
 * So the screen asks the auth service what it accepts (GoTrue publishes it at
 * /auth/v1/settings) and only offers a provider on the surface that can carry
 * the flow to a session. Fail closed — an unreadable answer offers nothing,
 * because email and password still works.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '../test/test-utils';
import { AccountTab } from './AccountTab';
import { resetEnabledAuthProvidersCache } from '../auth/authProviders';

const hooksAuthMock = vi.hoisted(() => ({ value: null }));
const cloudAuthMock = vi.hoisted(() => ({ value: null }));

vi.mock('../hooks/useAuth', () => ({ useAuth: () => hooksAuthMock.value }));
vi.mock('../auth/useAuth', () => ({ useAuth: () => cloudAuthMock.value }));

function signedOutStores() {
  hooksAuthMock.value = {
    user: null,
    subscription: null,
    loading: false,
    isLoggedIn: false,
    logout: vi.fn(async () => ({ success: true })),
    passwordRecovery: false,
  };
  cloudAuthMock.value = {
    status: 'signedOut',
    user: null,
    signOut: vi.fn(async () => {}),
  };
}

/** GoTrue's own capability document, plus whatever else the tab polls. */
function stubFetch(externalProviders) {
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    if (String(url).includes('/auth/v1/settings')) {
      if (externalProviders === 'unreachable') {
        return { ok: false, status: 503, json: async () => ({}) };
      }
      return { ok: true, status: 200, json: async () => ({ external: externalProviders }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  }));
}

/** The Qt bridge is the desktop signal (utils/runtimeSurface). */
function onDesktop() {
  window.viola = { openExternalUrl: vi.fn() };
}

beforeEach(() => {
  resetEnabledAuthProvidersCache();
  signedOutStores();
});

afterEach(() => {
  delete window.viola;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetEnabledAuthProvidersCache();
});

describe('provider buttons on the desktop surface', () => {
  it('offers Google when GoTrue says Google is enabled', async () => {
    onDesktop();
    stubFetch({ google: true, apple: false });

    render(<AccountTab />);

    expect(await screen.findByRole('button', { name: /Continue with Google/i })).toBeInTheDocument();
  });

  it('does NOT offer Apple while GoTrue has it disabled — production says 400', async () => {
    onDesktop();
    stubFetch({ google: true, apple: false });

    render(<AccountTab />);

    await screen.findByRole('button', { name: /Continue with Google/i });
    expect(screen.queryByRole('button', { name: /Continue with Apple/i })).not.toBeInTheDocument();
  });

  it('offers Apple by itself the day it is turned on, with no code change', async () => {
    onDesktop();
    stubFetch({ google: true, apple: true });

    render(<AccountTab />);

    expect(await screen.findByRole('button', { name: /Continue with Apple/i })).toBeInTheDocument();
  });

  it('offers nothing when the auth service cannot be asked', async () => {
    onDesktop();
    stubFetch('unreachable');

    render(<AccountTab />);

    await screen.findByRole('heading', { name: /^Sign In$/i });
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /Continue with/i })).not.toBeInTheDocument();
    });
    // The path that always works is still right there.
    expect(screen.getByLabelText(/^Email$/i)).toBeInTheDocument();
  });
});

describe('provider buttons on a browser surface', () => {
  it('offers none, because a top-level redirect destroys the PKCE verifier', async () => {
    // No window.viola: SEC-017 keeps auth-js storage in this page's heap, so a
    // flow that navigates away cannot come back with anything to exchange.
    stubFetch({ google: true, apple: true });

    render(<AccountTab />);

    await screen.findByRole('heading', { name: /^Sign In$/i });
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /Continue with/i })).not.toBeInTheDocument();
    });
    expect(screen.getByLabelText(/^Email$/i)).toBeInTheDocument();
  });

  it('never asks the auth service at all — there is nothing it could offer', async () => {
    const fetchSpy = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) }));
    vi.stubGlobal('fetch', fetchSpy);

    render(<AccountTab />);

    await screen.findByRole('heading', { name: /^Sign In$/i });
    const settingsCalls = fetchSpy.mock.calls.filter(
      (call) => String(call[0]).includes('/auth/v1/settings'),
    );
    expect(settingsCalls).toHaveLength(0);
  });
});
