/**
 * Sign-in page on the desktop (1.0.5 build 5 laptop report).
 *
 * - "Continue with Google" rendered about a second late and pushed the email
 *   field down, so a tap aimed at the email box landed on Google. The form now
 *   reserves the buttons' space while the provider list loads.
 * - The password field had no show/hide toggle.
 * - After an upgrade the page showed the Sign In form while the backend was
 *   signed in (A2.7). The page now re-reads the desktop session and, if that
 *   still fails, says the computer is signed in.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../test/test-utils';
import { AccountTab } from './AccountTab';
import { resetEnabledAuthProvidersCache } from '../auth/authProviders';

const hooksAuthMock = vi.hoisted(() => ({ value: null }));
const cloudAuthMock = vi.hoisted(() => ({ value: null }));

vi.mock('../hooks/useAuth', () => ({ useAuth: () => hooksAuthMock.value }));
vi.mock('../auth/useAuth', () => ({ useAuth: () => cloudAuthMock.value }));

function signedOutStores(extra = {}) {
  hooksAuthMock.value = {
    user: null,
    subscription: null,
    loading: false,
    isLoggedIn: false,
    logout: vi.fn(async () => ({ success: true })),
    passwordRecovery: false,
    restoreDesktopSession: vi.fn(async () => false),
    ...extra,
  };
  cloudAuthMock.value = { status: 'signedOut', user: null, signOut: vi.fn(async () => {}) };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

let settings;
let backendAccount;

function stubFetch() {
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    const path = String(url);
    if (path.includes('/auth/v1/settings')) {
      const external = await settings.promise;
      return { ok: true, status: 200, json: async () => ({ external }) };
    }
    if (path.includes('/v1/onboarding/account')) {
      return { ok: true, status: 200, json: async () => backendAccount };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  }));
}

beforeEach(() => {
  resetEnabledAuthProvidersCache();
  try { localStorage.clear(); } catch { /* jsdom */ }
  settings = deferred();
  backendAccount = { ok: true, signed_in: false, email: null };
  signedOutStores();
  stubFetch();
  window.viola = { openExternalUrl: vi.fn() };
});

afterEach(() => {
  delete window.viola;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetEnabledAuthProvidersCache();
});

describe('provider buttons never push the email field', () => {
  it('reserves the Google button space before the provider list arrives', async () => {
    render(<AccountTab />);

    // Still loading: an invisible stand-in holds the space above the email box.
    const reservation = screen.getByTestId('oauth-provider-reservation');
    expect(reservation).toHaveAttribute('aria-hidden', 'true');
    expect(reservation.style.visibility).toBe('hidden');
    expect(screen.queryByRole('button', { name: /Continue with Google/i })).not.toBeInTheDocument();
    const email = screen.getByLabelText('Email');
    expect(reservation.compareDocumentPosition(email) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    await act(async () => { settings.resolve({ google: true, apple: false }); });

    expect(await screen.findByRole('button', { name: /Continue with Google/i })).toBeInTheDocument();
    expect(screen.queryByTestId('oauth-provider-reservation')).not.toBeInTheDocument();
  });

  it('remembers the answer so the next form renders the buttons at once', async () => {
    settings.resolve({ google: true, apple: false });
    const first = render(<AccountTab />);
    await screen.findByRole('button', { name: /Continue with Google/i });
    first.unmount();

    render(<AccountTab />);
    expect(screen.getByRole('button', { name: /Continue with Google/i })).toBeInTheDocument();
    expect(screen.queryByTestId('oauth-provider-reservation')).not.toBeInTheDocument();
  });
});

describe('password show/hide toggle', () => {
  it('reveals and hides the password without changing the field', async () => {
    settings.resolve({});
    render(<AccountTab />);

    const password = screen.getByLabelText('Password');
    expect(password).toHaveAttribute('type', 'password');
    fireEvent.change(password, { target: { value: 'hunter22' } });

    const toggle = screen.getByRole('button', { name: 'Show password' });
    expect(toggle).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(toggle);

    expect(screen.getByLabelText('Password')).toHaveAttribute('type', 'text');
    expect(screen.getByLabelText('Password')).toHaveValue('hunter22');
    fireEvent.click(screen.getByRole('button', { name: 'Hide password' }));
    expect(screen.getByLabelText('Password')).toHaveAttribute('type', 'password');
  });
});

describe('the page follows the backend sign-in state', () => {
  it('re-reads the desktop session when the backend is signed in', async () => {
    settings.resolve({});
    backendAccount = { ok: true, signed_in: true, email: 'jay@example.com' };
    const restoreDesktopSession = vi.fn(async () => true);
    signedOutStores({ restoreDesktopSession });

    render(<AccountTab />);

    await waitFor(() => expect(restoreDesktopSession).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId('backend-account-notice')).not.toBeInTheDocument();
  });

  it('says the computer is signed in when the page cannot load that sign-in', async () => {
    settings.resolve({});
    backendAccount = { ok: true, signed_in: true, email: 'jay@example.com' };
    signedOutStores({ restoreDesktopSession: vi.fn(async () => false) });

    render(<AccountTab />);

    const notice = await screen.findByTestId('backend-account-notice');
    expect(notice).toHaveTextContent('Viola is signed in on this computer as jay@example.com');
  });

  it('shows the plain Sign In form when the backend is signed out too', async () => {
    settings.resolve({});
    const restoreDesktopSession = vi.fn(async () => false);
    signedOutStores({ restoreDesktopSession });

    render(<AccountTab />);

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/v1/onboarding/account'), expect.anything()));
    expect(restoreDesktopSession).not.toHaveBeenCalled();
    expect(screen.queryByTestId('backend-account-notice')).not.toBeInTheDocument();
  });
});
