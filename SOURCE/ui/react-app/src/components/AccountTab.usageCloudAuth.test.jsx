/**
 * Acceptance oracle for issue #1175: useUsage gated its /billing/usage poll
 * on hooks/useAuth's `isLoggedIn` alone — the same disconnected single-store
 * auth problem AccountTab itself was fixed to reconcile for #1067. A user
 * authenticated ONLY through the cloud front door (auth/useAuth `signedIn`,
 * hooks/useAuth `isLoggedIn` false) has AccountTab correctly render their
 * profile (post-#1067), but the nested UsageSummary/useUsage silently never
 * fetched usage, so the progress bar rendered null even with usage data
 * available server-side.
 *
 * The front door now bridges its session into the app-wide store, so the
 * reconciled state is the normal one and is covered first here; the
 * unreconciled first-paint window (the bridge's GET /auth/v1/user round trip
 * has not resolved yet) is kept as its own case, because useUsage must still
 * poll for an already-authenticated user in that window.
 *
 * Mocks both auth hooks directly (AccountTab.authSync.test.jsx's idiom) so
 * each state is deterministic and independent of gotrueClient's async
 * hydration timing.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor } from '../test/test-utils';
import { AccountTab } from './AccountTab';

const hooksAuthMock = vi.hoisted(() => ({ value: null }));
const cloudAuthMock = vi.hoisted(() => ({ value: null }));

vi.mock('../hooks/useAuth', () => ({
  useAuth: () => hooksAuthMock.value,
}));

vi.mock('../auth/useAuth', () => ({
  useAuth: () => cloudAuthMock.value,
}));

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

/** The app-wide store AFTER the front door bridged its session into it. */
function reconciledHooksAuth(overrides = {}) {
  return baseHooksAuth({
    isLoggedIn: true,
    user: { id: 'cloud-1', email: 'cloud-only@example.com' },
    subscription: {
      status: 'active',
      planId: 'pro_monthly',
      planFamily: 'pro',
      hasPaidAccess: true,
      paymentProvider: 'stripe',
    },
    ...overrides,
  });
}

function baseCloudAuth(overrides = {}) {
  return {
    status: 'signedOut',
    user: null,
    session: null,
    signOut: vi.fn(async () => ({ ok: true, error: null })),
    ...overrides,
  };
}

function buildFetchSpy(usagePayload) {
  return vi.fn(async (input) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.endsWith('/billing/usage')) {
      return { ok: true, status: 200, json: () => Promise.resolve(usagePayload) };
    }
    return { ok: true, status: 200, json: () => Promise.resolve({}) };
  });
}

describe('useUsage — reconciled auth state via AccountTab (#1175)', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('renders the usage progress bar for a reconciled cloud sign-in, with the paid plan intact', async () => {
    // The fixed state: the front door bridged its session into hooks/useAuth,
    // so that store carries both the identity and the paid `subscription`.
    hooksAuthMock.value = reconciledHooksAuth();
    cloudAuthMock.value = baseCloudAuth({
      status: 'signedIn',
      user: { id: 'cloud-1', email: 'cloud-only@example.com' },
    });

    const fetchSpy = buildFetchSpy({
      monthly_percent: 55,
      weekly_percent: 0,
      monthly_capped: true,
      weekly_capped: false,
      resets_monthly: '2026-08-01T00:00:00Z',
      resets_weekly: '2026-07-28T00:00:00Z',
      extra_usage_cents: 0,
    });
    vi.stubGlobal('fetch', fetchSpy);

    render(<AccountTab />);

    await waitFor(() => {
      expect(screen.getByText(/55% used this month/i)).toBeInTheDocument();
    });
    // The paying customer is billed as Pro, not shown the free-plan treatment.
    expect(screen.getByText('Pro')).toBeInTheDocument();
    expect(screen.queryByText('Free Plan')).not.toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringMatching(/\/billing\/usage$/),
      expect.anything(),
    );
  });

  it('renders the usage progress bar during the first paint, before the bridge resolves', async () => {
    // The #1175 window: hooks/useAuth (the context useUsage originally read
    // alone) has not caught up yet, but the cloud front door the user actually
    // signed in through already says signedIn.
    hooksAuthMock.value = baseHooksAuth({ isLoggedIn: false, user: null });
    cloudAuthMock.value = baseCloudAuth({
      status: 'signedIn',
      user: { id: 'cloud-1', email: 'cloud-only@example.com' },
    });

    const fetchSpy = buildFetchSpy({
      monthly_percent: 55,
      weekly_percent: 0,
      monthly_capped: true,
      weekly_capped: false,
      resets_monthly: '2026-08-01T00:00:00Z',
      resets_weekly: '2026-07-28T00:00:00Z',
      extra_usage_cents: 0,
    });
    vi.stubGlobal('fetch', fetchSpy);

    render(<AccountTab />);

    // Old bug shape: UsageSummary silently renders null because
    // useUsage's fetchUsage() never fires (isLoggedIn stayed false).
    await waitFor(() => {
      expect(screen.getByText(/55% used this month/i)).toBeInTheDocument();
    });
    const progress = screen.getByRole('progressbar', { name: /month usage/i });
    expect(progress.getAttribute('aria-valuenow')).toBe('55');

    // Confirm the poll actually reached /billing/usage rather than the
    // assertion racing a stale render.
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringMatching(/\/billing\/usage$/),
      expect.anything(),
    );
  });

  it('negative control: no progress bar for a genuinely signed-out user', async () => {
    hooksAuthMock.value = baseHooksAuth({ isLoggedIn: false });
    cloudAuthMock.value = baseCloudAuth({ status: 'signedOut' });
    vi.stubGlobal('fetch', buildFetchSpy({
      monthly_percent: 55,
      weekly_percent: 0,
      monthly_capped: true,
      weekly_capped: false,
    }));

    render(<AccountTab />);

    await screen.findByRole('heading', { name: /^Sign In$/i });
    expect(screen.queryByText(/used this month/i)).not.toBeInTheDocument();
  });
});
