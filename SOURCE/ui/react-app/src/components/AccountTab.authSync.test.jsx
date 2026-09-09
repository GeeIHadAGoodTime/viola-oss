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
import { render, screen } from '../test/test-utils';
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
