/**
 * Regression coverage for issue #1067: on the cloud surface (api.useviola.com/app)
 * the app authenticates through TWO independent GoTrue session stores —
 * CloudAuthGate/auth/AuthProvider (gates entry to the dashboard) and this
 * AccountTab's own hooks/useAuth (lib/auth_context) — see App.jsx `Dashboard()`.
 * Signing in through the cloud front door never populated the second store, so
 * AccountTab rendered the full Google/Apple/email Sign In section to a user who
 * was already authenticated per the cloud gate.
 *
 * The front door now BRIDGES its session into that second store, so the
 * reconciled state is the normal one: a cloud sign-in leaves hooks/useAuth
 * signed in, with a real `subscription`. These tests mock both auth hooks
 * directly (App.test.jsx's idiom) so each state can be asserted
 * deterministically, without depending on gotrueClient's async hydration
 * timing:
 *
 *  - the RECONCILED state (what the fixed system produces), including a paying
 *    customer who must never be shown the free-plan treatment; and
 *  - the transient first-paint window before the bridge's `setSession` round
 *    trip resolves, where AccountTab must still not throw a login form at an
 *    already-authenticated user.
 *
 * Whether the stores really do reconcile is proved against the REAL providers
 * in AccountTab.cloudStoreReconciliation.test.jsx — mocked hooks cannot see it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '../test/test-utils';
import { AccountTab } from './AccountTab';

const hooksAuthMock = vi.hoisted(() => ({ value: null }));
const cloudAuthMock = vi.hoisted(() => ({ value: null }));

vi.mock('../hooks/useAuth', () => ({
  useAuth: () => hooksAuthMock.value,
}));

vi.mock('../auth/useAuth', () => ({
  useAuth: () => cloudAuthMock.value,
}));

// AccountTab side-effects (useUsage's /billing/usage poll) shouldn't blow up
// tests that don't care about it.
function stubFetch() {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    status: 200,
    json: () => Promise.resolve({}),
  })));
}

function baseHooksAuth(overrides = {}) {
  return {
    user: null,
    subscription: null,
    loading: false,
    isLoggedIn: false,
    logout: vi.fn(async () => ({ success: true })),
    refreshUser: vi.fn(async () => {}),
    billingStatus: 'ready',
    passwordRecovery: false,
    ...overrides,
  };
}

/**
 * The app-wide store AFTER the front door bridged its session into it — the
 * state a cloud sign-in actually produces now. `subscription` is present
 * because that is what the bridge exists to deliver: it is the value the plan
 * pill, the Manage Subscription button and paid-feature gating all read.
 */
function reconciledHooksAuth({ email = 'cloud-only@example.com', subscription, ...overrides } = {}) {
  return baseHooksAuth({
    isLoggedIn: true,
    user: { id: 'cloud-1', email },
    subscription: subscription || {
      status: 'free',
      planId: 'free',
      planFamily: 'free',
      hasPaidAccess: false,
      paymentProvider: null,
    },
    ...overrides,
  });
}

const PAID_SUBSCRIPTION = {
  status: 'active',
  planId: 'pro_monthly',
  planFamily: 'pro',
  hasPaidAccess: true,
  paymentProvider: 'stripe',
};

function baseCloudAuth(overrides = {}) {
  return {
    status: 'signedOut',
    user: null,
    session: null,
    signOut: vi.fn(async () => ({ ok: true, error: null })),
    ...overrides,
  };
}

describe('AccountTab — reconciled auth state (#1067)', () => {
  beforeEach(() => {
    stubFetch();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('renders the reconciled account for a cloud sign-in, with the store as the source of truth', async () => {
    // The fixed state: the front door bridged its session into hooks/useAuth,
    // so that store — the one ProfileCard reads `subscription` from — is
    // populated rather than empty.
    hooksAuthMock.value = reconciledHooksAuth();
    cloudAuthMock.value = baseCloudAuth({
      status: 'signedIn',
      user: { id: 'cloud-1', email: 'cloud-only@example.com' },
    });

    render(<AccountTab />);

    expect(screen.queryByRole('heading', { name: /^Sign In$/i })).not.toBeInTheDocument();
    expect(await screen.findByText('cloud-only@example.com')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Sign Out$/i })).toBeInTheDocument();
  });

  it('a PAYING cloud customer is never shown the free-plan treatment', async () => {
    // C-400: a customer on Pro was rendered "Free Plan" with an upgrade prompt
    // for the plan they already bought, and no route to the billing portal.
    hooksAuthMock.value = reconciledHooksAuth({
      email: 'paying@example.com',
      subscription: PAID_SUBSCRIPTION,
    });
    cloudAuthMock.value = baseCloudAuth({
      status: 'signedIn',
      user: { id: 'cloud-1', email: 'paying@example.com' },
    });

    render(<AccountTab />);

    expect(await screen.findByText('paying@example.com')).toBeInTheDocument();
    expect(screen.getByText('Pro')).toBeInTheDocument();
    expect(screen.queryByText('Free Plan')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Manage Subscription/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Upgrade Plan/i })).not.toBeInTheDocument();
  });

  it('does NOT render the Sign In section during the first paint, before the bridge resolves', async () => {
    // The bridge's setSession does a real GET /auth/v1/user round trip, so
    // there is a brief window where the cloud gate already says signedIn and
    // the app-wide store has not caught up. An already-authenticated user must
    // not be shown a login form in that window (the #1067 shape).
    hooksAuthMock.value = baseHooksAuth({ isLoggedIn: false, user: null });
    cloudAuthMock.value = baseCloudAuth({
      status: 'signedIn',
      user: { id: 'cloud-1', email: 'cloud-only@example.com' },
    });

    render(<AccountTab />);

    // Old bug shape: full Google/Apple/email Sign In section.
    expect(screen.queryByRole('heading', { name: /^Sign In$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Continue with Google/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Continue with Apple/i })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/^Email$/i)).not.toBeInTheDocument();

    // Correct authenticated state: account info + sign out, not a login form.
    expect(await screen.findByText('cloud-only@example.com')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Sign Out$/i })).toBeInTheDocument();
  });

  it('still renders the Sign In section for a genuinely signed-out user (negative control)', async () => {
    hooksAuthMock.value = baseHooksAuth({ isLoggedIn: false });
    cloudAuthMock.value = baseCloudAuth({ status: 'signedOut' });

    render(<AccountTab />);

    expect(await screen.findByRole('heading', { name: /^Sign In$/i })).toBeInTheDocument();
    // The email form is the part that is always there. Provider buttons are
    // conditional now — they render only on the surface where the flow can
    // finish, and only for providers GoTrue reports enabled. jsdom has no Qt
    // bridge, so this is a browser surface and there are none. That contract
    // has its own coverage in AccountTab.providerButtons.test.jsx.
    expect(screen.getByLabelText(/^Email$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^Password$/i)).toBeInTheDocument();
  });

  it('renders the profile when only the legacy hooks/useAuth session is authenticated (unchanged desktop path)', async () => {
    hooksAuthMock.value = baseHooksAuth({
      isLoggedIn: true,
      user: { email: 'desktop@example.com' },
      subscription: { hasPaidAccess: false },
    });
    cloudAuthMock.value = baseCloudAuth({ status: 'signedOut' });

    render(<AccountTab />);

    expect(await screen.findByText('desktop@example.com')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /^Sign In$/i })).not.toBeInTheDocument();
  });

  it('Sign Out clears whichever session(s) are actually live', async () => {
    const logout = vi.fn(async () => ({ success: true }));
    const cloudSignOut = vi.fn(async () => ({ ok: true, error: null }));
    hooksAuthMock.value = baseHooksAuth({ isLoggedIn: false, logout });
    cloudAuthMock.value = baseCloudAuth({
      status: 'signedIn',
      user: { id: 'cloud-1', email: 'cloud-only@example.com' },
      signOut: cloudSignOut,
    });

    const { user } = render(<AccountTab />);
    await user.click(await screen.findByRole('button', { name: /^Sign Out$/i }));

    // The cloud session is the one actually authenticated — it must be
    // cleared. The disconnected hooks/useAuth session was never signed in,
    // so its logout() is skipped rather than fired needlessly.
    expect(cloudSignOut).toHaveBeenCalledTimes(1);
    expect(logout).not.toHaveBeenCalled();
  });
});

describe('AccountTab logout outcome witnesses', () => {
  beforeEach(() => { stubFetch(); });
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it.each(['cloud', 'legacy', 'rejected'])('shows a usable failure when logout does not complete (%s)', async (source) => {
    const message = 'Sign-out could not clear the app session. Please retry.';
    const logout = source === 'rejected'
      ? vi.fn(async () => { throw new Error(message); })
      : vi.fn(async () => ({ success: false, error: message }));
    const cloudSignOut = vi.fn(async () => ({ ok: false, error: { code: 'app_store_signout_failed', message } }));
    hooksAuthMock.value = baseHooksAuth({
      isLoggedIn: source !== 'cloud', user: source === 'cloud' ? null : { id: 'synthetic-user', email: 'synthetic@example.invalid' }, logout,
    });
    cloudAuthMock.value = baseCloudAuth({
      status: source === 'cloud' ? 'signedIn' : 'signedOut',
      user: { id: 'synthetic-user', email: 'synthetic@example.invalid' }, signOut: cloudSignOut,
    });
    const { user } = render(<AccountTab />);
    await user.click(screen.getByRole('button', { name: /^Sign Out$/i }));
    expect(source === 'cloud' ? cloudSignOut : logout).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByText(source === 'rejected' ? 'Sign-out could not be completed. Please retry.' : message)).toBeInTheDocument());
  });

  it('does not submit a second logout while the first user action is pending', async () => {
    const releases = [];
    const cloudSignOut = vi.fn(() => new Promise((resolve) => { releases.push(resolve); }));
    hooksAuthMock.value = baseHooksAuth();
    cloudAuthMock.value = baseCloudAuth({
      status: 'signedIn', user: { id: 'synthetic-user', email: 'synthetic@example.invalid' }, signOut: cloudSignOut,
    });
    const { user } = render(<AccountTab />);
    const button = screen.getByRole('button', { name: /^Sign Out$/i });
    await user.click(button);
    await user.click(button);
    const calls = cloudSignOut.mock.calls.length;
    releases.forEach((release) => release({ ok: true, error: null }));
    expect(calls).toBe(1);
  });
});

describe('AccountTab logout action ownership', () => {
  beforeEach(() => { stubFetch(); });
  afterEach(() => { delete window.viola; vi.restoreAllMocks(); vi.unstubAllGlobals(); });
  function signedIn(logout, id = 'synthetic-user') {
    return baseHooksAuth({ isLoggedIn: true, user: { id, email: `${id}@example.invalid` }, logout });
  }

  it.each(['cloud', 'desktop'])('uses the correct store owners when both stores are live (%s)', async (surface) => {
    if (surface === 'desktop') window.viola = {};
    const logout = vi.fn(async () => ({ success: true }));
    const cloudSignOut = vi.fn(async () => ({ ok: true, error: null }));
    hooksAuthMock.value = signedIn(logout);
    cloudAuthMock.value = baseCloudAuth({ status: 'signedIn', user: { id: 'synthetic-user' }, signOut: cloudSignOut });
    const { user } = render(<AccountTab />);
    await user.click(screen.getByRole('button', { name: 'Sign Out' }));
    expect(cloudSignOut).toHaveBeenCalledTimes(1);
    expect(logout).toHaveBeenCalledTimes(surface === 'cloud' ? 0 : 1);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('explicitly retries a returned failure and clears it only after success', async () => {
    const logout = vi.fn().mockResolvedValueOnce({ success: false, error: 'Synthetic logout refused' }).mockResolvedValueOnce({ success: true });
    hooksAuthMock.value = signedIn(logout);
    cloudAuthMock.value = baseCloudAuth();
    const { user } = render(<AccountTab />);
    await user.click(screen.getByRole('button', { name: 'Sign Out' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Synthetic logout refused');
    await user.click(screen.getByRole('button', { name: 'Retry sign-out' }));
    expect(logout).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('keeps a late failure visible across its own local signed-out transition and retries the captured action', async () => {
    let release;
    const logout = vi.fn().mockImplementationOnce(() => new Promise((resolve) => { release = resolve; })).mockResolvedValueOnce({ success: true });
    hooksAuthMock.value = signedIn(logout);
    cloudAuthMock.value = baseCloudAuth();
    const view = render(<AccountTab />);
    await view.user.click(screen.getByRole('button', { name: 'Sign Out' }));
    hooksAuthMock.value = baseHooksAuth({ logout });
    view.rerender(<AccountTab />);
    await act(async () => { release({ success: false, error: 'Synthetic late failure' }); });
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Synthetic late failure'));
    await view.user.click(screen.getByRole('button', { name: 'Retry sign-out' }));
    expect(logout).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it.each(['new-user', 'same-user-aba'])('a late previous action cannot publish over a newer principal (%s)', async (transition) => {
    let release;
    const oldLogout = vi.fn(() => new Promise((resolve) => { release = resolve; }));
    const nextLogout = vi.fn(async () => ({ success: true }));
    hooksAuthMock.value = signedIn(oldLogout);
    cloudAuthMock.value = baseCloudAuth();
    const view = render(<AccountTab />);
    await view.user.click(screen.getByRole('button', { name: 'Sign Out' }));
    if (transition === 'same-user-aba') {
      hooksAuthMock.value = baseHooksAuth();
      view.rerender(<AccountTab />);
    }
    hooksAuthMock.value = signedIn(nextLogout, transition === 'new-user' ? 'synthetic-next' : 'synthetic-user');
    view.rerender(<AccountTab />);
    await act(async () => { release({ success: false, error: 'Obsolete logout refusal' }); });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sign Out' })).not.toBeDisabled());
    expect(screen.queryByText('Obsolete logout refusal')).not.toBeInTheDocument();
    await view.user.click(screen.getByRole('button', { name: 'Sign Out' }));
    expect(nextLogout).toHaveBeenCalledTimes(1);
  });

  it.each([undefined, { success: false, error: { message: {} } }])('unknown outcomes show a safe generic failure (%j)', async (result) => {
    hooksAuthMock.value = signedIn(vi.fn(async () => result));
    cloudAuthMock.value = baseCloudAuth();
    const { user } = render(<AccountTab />);
    await user.click(screen.getByRole('button', { name: 'Sign Out' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Sign-out could not be completed. Please retry.');
  });

  it('does not show a retired cloud action as a current failure', async () => {
    hooksAuthMock.value = baseHooksAuth();
    cloudAuthMock.value = baseCloudAuth({ status: 'signedIn', user: { id: 'synthetic-user' }, signOut: vi.fn(async () => ({ ok: false, error: { code: 'auth_session_changed' } })) });
    const { user } = render(<AccountTab />);
    await user.click(screen.getByRole('button', { name: 'Sign Out' }));
    expect(screen.queryByText('Sign-out could not be completed. Please retry.')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry sign-out' })).not.toBeInTheDocument();
  });
});

it('an older completion cannot release a newer account logout action', async () => {
  stubFetch();
  const releases = [];
  const first = vi.fn(() => new Promise((resolve) => { releases.push(resolve); }));
  const second = vi.fn(() => new Promise((resolve) => { releases.push(resolve); }));
  hooksAuthMock.value = baseHooksAuth({ isLoggedIn: true, user: { id: 'synthetic-first', email: 'first@example.invalid' }, logout: first });
  cloudAuthMock.value = baseCloudAuth();
  const view = render(<AccountTab />);
  try {
    await view.user.click(screen.getByRole('button', { name: 'Sign Out' }));
    hooksAuthMock.value = baseHooksAuth({ isLoggedIn: true, user: { id: 'synthetic-second', email: 'second@example.invalid' }, logout: second });
    view.rerender(<AccountTab />);
    await view.user.click(screen.getByRole('button', { name: 'Sign Out' }));
    await act(async () => { releases[0]({ success: false, error: 'Obsolete first failure' }); });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Signing out…' })).toBeDisabled());
    expect(screen.queryByText('Obsolete first failure')).not.toBeInTheDocument();
    expect(second).toHaveBeenCalledTimes(1);
  } finally {
    releases.forEach((release) => release({ success: true }));
    view.unmount();
    vi.unstubAllGlobals();
  }
});
