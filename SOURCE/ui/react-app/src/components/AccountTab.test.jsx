import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '../test/test-utils';
import { AuthProvider } from '../hooks/useAuth';
import { AuthProvider as CloudAuthProvider } from '../auth/AuthProvider';
import { GOTRUE_STORAGE_KEY } from '../lib/gotrue_client';
import { inMemorySessionStorage } from '../lib/sessionStore';
import { AccountTab } from './AccountTab';

// ---------------------------------------------------------------------------
// GoTrue test scaffolding. Since the GoTrue migration (bce028c3c), auth flows
// go through lib/auth_context -> gotrueClient (same-origin /auth/v1/*), not
// the legacy /auth/login-/auth/register REST layer, and the signed-in user +
// subscription derive from the GoTrue session's app_metadata — not /auth/me.
// SEC-017 keeps sessions in the in-memory auth-js storage adapter, so
// logged-in tests seed that store (not localStorage) before rendering.
// ---------------------------------------------------------------------------

function gotrueUser({ id, email, app_metadata = {} }) {
  return {
    id,
    aud: 'authenticated',
    role: 'authenticated',
    email,
    email_confirmed_at: '2026-01-01T00:00:00.000Z',
    created_at: '2026-01-01T00:00:00.000Z',
    updated_at: '2026-01-01T00:00:00.000Z',
    app_metadata: { provider: 'email', ...app_metadata },
    user_metadata: {},
  };
}

function gotrueSession(user) {
  return {
    access_token: 'test-access-token',
    refresh_token: 'test-refresh-token',
    token_type: 'bearer',
    expires_in: 86400,
    expires_at: Math.floor(Date.now() / 1000) + 86400,
    user,
  };
}

function seedGoTrueSession(user) {
  inMemorySessionStorage.setItem(GOTRUE_STORAGE_KEY, JSON.stringify(gotrueSession(user)));
}

// auth-js reads response.headers; plain objects from withAuthDefaults lack it.
function gotrueJsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: () => Promise.resolve(body),
  };
}

afterEach(() => {
  inMemorySessionStorage.removeItem(GOTRUE_STORAGE_KEY);
});

// Default /auth/me response = logged-out (401). Most tests override fetch
// with a per-call mock for the action they care about, then re-stub
// /auth/me afterward (the AuthContext refreshes after login).
function withAuthDefaults(extraImpl) {
  return vi.fn(async (input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.endsWith('/auth/me')) {
      return {
        ok: false,
        status: 401,
        json: () => Promise.resolve({ detail: 'unauthorized' }),
      };
    }
    if (extraImpl) {
      const result = await extraImpl(url, init);
      if (result) return result;
    }
    return { ok: true, status: 200, json: () => Promise.resolve({}) };
  });
}

function renderWithAuth(ui) {
  // AccountTab also reads the separate cloud-front-door auth context
  // (auth/AuthProvider — issue #1067); real app usage always wraps it, so
  // tests must too or useCloudAuth() throws "must be used within
  // AuthProvider".
  return render(
    <CloudAuthProvider>
      <AuthProvider>{ui}</AuthProvider>
    </CloudAuthProvider>,
  );
}

const STRIPE_PRO_USER = gotrueUser({
  id: 'u-pro',
  email: 'pro@example.com',
  app_metadata: {
    plan_tier: 'pro',
    plan_id: 'pro_monthly',
    plan_family: 'pro',
    subscription_status: 'active',
    has_paid_access: true,
    payment_provider: 'stripe',
  },
});

describe('AccountTab — Path C2 short-code flow', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('switches to code mode after the user requests a magic link', async () => {
    const fetchSpy = withAuthDefaults(async (url) => {
      if (url.endsWith('/auth/magic-link/request')) {
        return {
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve({ ok: true, data: { message: 'Magic link sent' } }),
        };
      }
      return null;
    });
    vi.stubGlobal('fetch', fetchSpy);

    const { user } = renderWithAuth(<AccountTab />);

    // Wait for the auth context to settle into logged-out state.
    await screen.findByRole('button', { name: /^Sign In$/i });

    // Switch into magic-link mode → request a link → auto-flips to code mode.
    await user.click(screen.getByRole('button', { name: /Sign in with magic link/i }));
    await user.type(screen.getByLabelText(/^Email$/i), 'rehearsal@example.com');
    await user.click(screen.getByRole('button', { name: /Send Magic Link/i }));

    // The auto-switch is timer-based (800ms in the component); wait it out.
    await waitFor(
      () => {
        expect(screen.getByRole('heading', { name: /Enter Sign-In Code/i })).toBeInTheDocument();
      },
      { timeout: 2000 },
    );
    expect(screen.getByLabelText(/Sign-in code from email/i)).toBeInTheDocument();
  });

  it('posts the typed 8-char code to GoTrue /auth/v1/verify and signs in', async () => {
    const verifyCalls = [];
    const fetchSpy = withAuthDefaults(async (url, init) => {
      if (url.includes('/auth/v1/verify')) {
        verifyCalls.push(JSON.parse(init.body));
        return gotrueJsonResponse(
          gotrueSession(gotrueUser({ id: 'u1', email: 'rehearsal@example.com' })),
        );
      }
      return null;
    });
    vi.stubGlobal('fetch', fetchSpy);

    const { user } = renderWithAuth(<AccountTab />);
    await screen.findByRole('button', { name: /^Sign In$/i });

    // Drive directly into code mode via "I have a code" button under magic mode.
    await user.click(screen.getByRole('button', { name: /Sign in with magic link/i }));
    await user.click(screen.getByRole('button', { name: /I have a code from my email/i }));

    await user.type(screen.getByLabelText(/^Email$/i), 'rehearsal@example.com');
    await user.type(screen.getByLabelText(/Sign-in code from email/i), 'abcd2345');

    await user.click(screen.getByRole('button', { name: /Sign In with Code/i }));

    await waitFor(() => expect(verifyCalls.length).toBe(1));
    expect(verifyCalls[0]).toMatchObject({
      email: 'rehearsal@example.com',
      // Component upper-cases input; gotrueClient.verifyOtp sends it as `token`.
      token: 'ABCD2345',
      type: 'magiclink',
    });

    // The returned session is applied — profile card replaces the sign-in form.
    await screen.findByText('rehearsal@example.com');
  });
});

// The legacy /auth/login -> mfa_required -> /auth/mfa/totp/authenticate REST
// pair was removed in the GoTrue migration (bce028c3c). MFA sign-in is now the
// GoTrue-native AAL1->AAL2 step-up (mfa.challengeAndVerify), driven by
// lib/auth_context.login()'s AAL check + verifyMfaTotp — covered end to end in
// AccountTab.mfa.test.jsx (#2317). These tests lock in the base (no-MFA)
// password sign-in contract.
describe('AccountTab — password sign-in (GoTrue)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('posts credentials to GoTrue token?grant_type=password and signs in', async () => {
    const tokenCalls = [];
    const fetchSpy = withAuthDefaults(async (url, init) => {
      if (url.includes('/auth/v1/token')) {
        tokenCalls.push({ url, body: JSON.parse(init.body) });
        return gotrueJsonResponse(
          gotrueSession(gotrueUser({ id: 'u1', email: 'signin@example.com' })),
        );
      }
      return null;
    });
    vi.stubGlobal('fetch', fetchSpy);

    const { user } = renderWithAuth(<AccountTab />);
    await screen.findByRole('button', { name: /^Sign In$/i });

    await user.type(screen.getByLabelText(/^Email$/i), 'signin@example.com');
    await user.type(screen.getByLabelText(/^Password$/i), 'CorrectHorse123!');
    await user.click(screen.getByRole('button', { name: /^Sign In$/i }));

    await waitFor(() => expect(tokenCalls).toHaveLength(1));
    expect(tokenCalls[0].url).toContain('grant_type=password');
    expect(tokenCalls[0].body).toMatchObject({
      email: 'signin@example.com',
      password: 'CorrectHorse123!', // pragma: allowlist secret
    });

    // Session applied -> profile card replaces the sign-in form.
    await screen.findByText('signin@example.com');
  });
});

describe('AccountTab — auth error details', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('shows rate-limit copy and support details for register failures', async () => {
    const fetchSpy = withAuthDefaults(async (url) => {
      if (url.includes('/auth/v1/signup')) {
        // GoTrue-shaped rate-limit error; auth-js surfaces status + error_code.
        return gotrueJsonResponse(
          {
            code: 429,
            error_code: 'over_request_rate_limit',
            msg: 'Too many registration attempts. Wait a moment and try again.',
          },
          429,
        );
      }
      return null;
    });
    vi.stubGlobal('fetch', fetchSpy);

    const { user } = renderWithAuth(<AccountTab />);
    await screen.findByRole('button', { name: /^Sign In$/i });

    await user.click(screen.getByRole('button', { name: /Sign up/i }));
    await user.type(screen.getByLabelText(/^Email$/i), 'retry@example.com');
    await user.type(screen.getByLabelText(/^Password$/i), 'CorrectHorse123!');
    // Both consents gate the submit: age/guardian eligibility + ToS.
    await user.click(screen.getByLabelText(/I confirm that I meet the age/i));
    await user.click(screen.getByLabelText(/I agree to the/i));
    await user.click(screen.getByRole('button', { name: /^Create Account$/i }));

    // friendlyAuthError has no Retry-After plumbing post-GoTrue (retryAfter is
    // always null in authErrorDetails), so the copy uses the generic label.
    await screen.findByText(/Too many attempts - wait a few minutes before trying again\./i);
    await user.click(screen.getByRole('button', { name: /^Details$/i }));
    expect(screen.getByText(/code: over_request_rate_limit; status: 429/i)).toBeInTheDocument();
  });
});

const FREE_USER = gotrueUser({
  id: 'u-free-upgrade',
  email: 'freeupgrade@example.com',
  app_metadata: {
    plan_tier: 'free',
    plan_id: 'free',
    plan_family: 'free',
    subscription_status: 'free',
    has_paid_access: false,
  },
});

const PAID_PLANS = {
  plans: [
    { id: 'pro_monthly', plan_family: 'pro', name: 'Viola Pro', description: '', price_cents: 1200, interval: 'month', limits: {}, capabilities: [] },
    { id: 'max_monthly', plan_family: 'max', name: 'Viola Max', description: '', price_cents: 2000, interval: 'month', limits: {}, capabilities: [] },
    { id: 'free', plan_family: 'free', name: 'Free', description: '', price_cents: 0, interval: 'month', limits: {}, capabilities: [] },
  ],
};

describe('AccountTab - Upgrade Plan (in-app checkout, #2609)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  function upgradeFetch(extra) {
    return withAuthDefaults(async (url, init) => {
      if (url.endsWith('/billing/plans')) {
        return { ok: true, status: 200, json: () => Promise.resolve(PAID_PLANS) };
      }
      if (extra) {
        const result = await extra(url, init);
        if (result) return result;
      }
      return null;
    });
  }

  it('renders the in-app upgrade panel with plans from /billing/plans for free users', async () => {
    seedGoTrueSession(FREE_USER);
    vi.stubGlobal('fetch', upgradeFetch());

    renderWithAuth(<AccountTab />);

    expect(await screen.findByTestId('upgrade-panel')).toBeInTheDocument();
    // Paid plan options are loaded from the backend catalog (never hardcoded);
    // the price-0 "free" plan is filtered out.
    expect(await screen.findByRole('button', { name: /Viola Pro/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Viola Max/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Free/i })).not.toBeInTheDocument();
  });

  it('POSTs /billing/checkout with consent + legal versions and opens the returned Stripe URL', async () => {
    seedGoTrueSession(FREE_USER);
    const checkoutCalls = [];
    const fetchSpy = upgradeFetch(async (url, init) => {
      if (url.endsWith('/billing/checkout')) {
        checkoutCalls.push({ url, body: JSON.parse(init.body) });
        return {
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve({
              checkout_url: 'https://checkout.stripe.com/c/pay/cs_test_123',
              session_id: 'cs_test_123',
              provider: 'stripe',
            }),
        };
      }
      return null;
    });
    vi.stubGlobal('fetch', fetchSpy);
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);

    const { user } = renderWithAuth(<AccountTab />);

    // ROSCA affirmative consent gates the charge — check the box first.
    await user.click(await screen.findByLabelText(/consent to the recurring charge/i));
    await user.click(await screen.findByRole('button', { name: /^Upgrade Plan$/i }));

    await waitFor(() => expect(checkoutCalls).toHaveLength(1));
    expect(checkoutCalls[0].url).toMatch(/\/billing\/checkout$/);
    expect(checkoutCalls[0].body).toMatchObject({
      plan_id: 'pro_monthly',
      provider: 'stripe',
      terms_accepted: true,
      terms_version: expect.any(String),
      privacy_version: expect.any(String),
    });
    expect(openSpy).toHaveBeenCalledWith(
      'https://checkout.stripe.com/c/pay/cs_test_123',
      '_blank',
      'noopener,noreferrer',
    );
  });

  it('keeps the Upgrade button disabled and fires no checkout until consent is given', async () => {
    seedGoTrueSession(FREE_USER);
    const checkoutCalls = [];
    const fetchSpy = upgradeFetch(async (url) => {
      if (url.endsWith('/billing/checkout')) {
        checkoutCalls.push(url);
        return { ok: true, status: 200, json: () => Promise.resolve({ checkout_url: 'x' }) };
      }
      return null;
    });
    vi.stubGlobal('fetch', fetchSpy);

    renderWithAuth(<AccountTab />);

    const upgradeButton = await screen.findByRole('button', { name: /^Upgrade Plan$/i });
    expect(upgradeButton).toBeDisabled();
    expect(checkoutCalls).toHaveLength(0);
  });
});

describe('AccountTab - Manage Subscription', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the Manage Subscription button for paid Stripe users', async () => {
    seedGoTrueSession(STRIPE_PRO_USER);
    vi.stubGlobal('fetch', withAuthDefaults());

    renderWithAuth(<AccountTab />);

    expect(await screen.findByRole('button', { name: /Manage Subscription/i })).toBeInTheDocument();
  });

  it('does not render the Manage Subscription button for free users', async () => {
    seedGoTrueSession(gotrueUser({
      id: 'u-free',
      email: 'free@example.com',
      app_metadata: {
        plan_tier: 'free',
        plan_id: 'free',
        plan_family: 'free',
        subscription_status: 'free',
        has_paid_access: false,
      },
    }));
    vi.stubGlobal('fetch', withAuthDefaults());

    renderWithAuth(<AccountTab />);

    await screen.findByText('free@example.com');
    expect(screen.queryByRole('button', { name: /Manage Subscription/i })).not.toBeInTheDocument();
  });

  it('fetches a portal session on click and opens the returned URL in a new tab', async () => {
    seedGoTrueSession(STRIPE_PRO_USER);
    const portalCalls = [];
    const fetchSpy = withAuthDefaults(async (url, init) => {
      if (url.endsWith('/v1/billing/portal/session')) {
        portalCalls.push({ url, init });
        return {
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve({
              ok: true,
              url: 'https://billing.stripe.com/p/session_test',
            }),
        };
      }
      return null;
    });
    vi.stubGlobal('fetch', fetchSpy);
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);

    const { user } = renderWithAuth(<AccountTab />);
    await user.click(await screen.findByRole('button', { name: /Manage Subscription/i }));

    await waitFor(() => expect(portalCalls).toHaveLength(1));
    expect(portalCalls[0]).toMatchObject({
      url: '/v1/billing/portal/session',
      init: {
        method: 'POST',
      },
    });
    expect(openSpy).toHaveBeenCalledWith(
      'https://billing.stripe.com/p/session_test',
      '_blank',
      'noopener,noreferrer',
    );
  });
});

describe('AccountTab - Cloud Sync status copy', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // Regression for #2775: a signed-in free user saw "Sign in to sync across
  // your devices" (the signed-out copy) because the disabled branch never
  // distinguished signed-in-but-not-paid from actually signed out.
  it('tells an already-signed-in free user to upgrade, not to sign in', async () => {
    seedGoTrueSession(gotrueUser({
      id: 'u-free',
      email: 'free@example.com',
      app_metadata: {
        plan_tier: 'free',
        plan_id: 'free',
        plan_family: 'free',
        subscription_status: 'free',
        has_paid_access: false,
      },
    }));
    vi.stubGlobal('fetch', withAuthDefaults());

    renderWithAuth(<AccountTab />);

    await screen.findByText('free@example.com');
    expect(screen.getByText('Upgrade to sync across your devices')).toBeInTheDocument();
    expect(screen.queryByText('Sign in to sync across your devices')).not.toBeInTheDocument();
  });

  it('still shows Active sync copy for a signed-in paid user', async () => {
    seedGoTrueSession(STRIPE_PRO_USER);
    vi.stubGlobal('fetch', withAuthDefaults());

    renderWithAuth(<AccountTab />);

    expect(await screen.findByText('Synced across your devices')).toBeInTheDocument();
  });
});
