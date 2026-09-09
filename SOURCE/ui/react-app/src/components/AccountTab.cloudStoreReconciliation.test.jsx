/**
 * Reconciliation-level oracle for candidate C-400 (safety core - Payments).
 *
 * The SPA runs TWO independent GoTrue session stores on the cloud surface:
 * the front door (auth/AuthProvider, which gates entry to /app) and the
 * app-wide store (lib/auth_context via hooks/useAuth), which is where
 * `subscription` - the value that decides the plan pill, the Manage
 * Subscription button, paid-feature gating and Cloud Sync - actually lives.
 *
 * Every other test around this seam mocks BOTH hooks, so none of them can see
 * whether the real stores actually reconcile. This file mocks NOTHING but the
 * network: real auth/AuthProvider, real lib/auth_context, real @supabase/auth-js
 * client, real AccountTab, against a stateful fake GoTrue that enforces real
 * refresh-token rotation with reuse detection.
 *
 * It asserts the reconciliation at the STORE, not at the pill: a component-local
 * shadow derivation inside AccountTab can make the pill look right while every
 * other consumer of `subscription` still reads free.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import PropTypes from 'prop-types';
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react';
import { UiStateProvider } from '../state/uiState';
import { AuthProvider as CloudAccountProvider, __setInMemorySessionForTest } from '../auth/AuthProvider';
import { useAuth as useCloudAuth } from '../auth/useAuth';
import { AuthProvider as AppAuthProvider, useAuth as useAppAuth } from '../hooks/useAuth';
import { gotrueClient } from '../lib/gotrue_client';
import { AccountTab } from './AccountTab';

/** Unsigned three-segment JWT - decodeJWT never verifies the signature. */
function fakeJwt(payload) {
  const seg = (obj) => btoa(JSON.stringify(obj))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  return `${seg({ alg: 'none', typ: 'JWT' })}.${seg(payload)}.${seg({ sig: true })}`;
}

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    text: async () => JSON.stringify(body),
    json: async () => body,
  };
}

const PAID_METADATA = {
  plan_tier: 'pro',
  plan_id: 'pro_monthly',
  plan_family: 'pro',
  subscription_status: 'active',
  has_paid_access: true,
  payment_provider: 'stripe',
};

const FREE_METADATA = {
  plan_tier: 'free',
  plan_id: 'free',
  plan_family: 'free',
  subscription_status: 'free',
  has_paid_access: false,
};

/**
 * Stateful fake GoTrue. `appMetadata` is mutable so a test can simulate the
 * Stripe webhook writing the upgrade between two token grants.
 */
function fakeGoTrue({ appMetadata = PAID_METADATA, expiresIn = 300 } = {}) {
  const state = {
    appMetadata,
    currentRefreshToken: null,
    revoked: false,
    seq: 0,
    refreshGrants: [],
  };

  const user = () => ({
    id: 'cloud-user-1',
    email: 'paying@example.com',
    factors: [],
    app_metadata: { ...state.appMetadata },
    user_metadata: {},
  });

  function issue() {
    state.seq += 1;
    state.currentRefreshToken = `refresh-${state.seq}`;
    const nowS = Math.floor(Date.now() / 1000);
    return {
      access_token: fakeJwt({ sub: 'cloud-user-1', exp: nowS + expiresIn, aal: 'aal1' }),
      refresh_token: state.currentRefreshToken,
      token_type: 'bearer',
      expires_in: expiresIn,
      expires_at: nowS + expiresIn,
      user: user(),
    };
  }

  const fetchMock = vi.fn(async (input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const parsed = new URL(url, 'http://localhost');
    const path = parsed.pathname;
    const grantType = parsed.searchParams.get('grant_type');

    if (path.endsWith('/auth/v1/token') && grantType === 'password') {
      return jsonResponse(issue());
    }

    if (path.endsWith('/auth/v1/token') && grantType === 'refresh_token') {
      let presented = null;
      try {
        presented = JSON.parse(init?.body || '{}').refresh_token;
      } catch { /* malformed -> treated as no token */ }
      state.refreshGrants.push(presented);
      if (state.revoked) {
        return jsonResponse(
          { error: 'invalid_grant', error_description: 'Invalid Refresh Token: session revoked' },
          400,
        );
      }
      if (presented !== state.currentRefreshToken) {
        // GoTrue reuse detection: replaying an already-rotated refresh token
        // revokes the whole family.
        state.revoked = true;
        return jsonResponse(
          { error: 'invalid_grant', error_description: 'Invalid Refresh Token: Already Used' },
          400,
        );
      }
      return jsonResponse(issue());
    }

    if (path.endsWith('/auth/v1/user')) {
      return jsonResponse(user());
    }

    if (path.endsWith('/auth/v1/logout')) {
      return jsonResponse(null, 204);
    }

    // Everything else the account panel touches (/billing/usage, /billing/plans).
    return jsonResponse({}, 200);
  });

  return { state, fetchMock };
}

/** Captured live values from the two stores. */
let cloudAuth = null;
let appAuth = null;

function CloudDriver() {
  cloudAuth = useCloudAuth();
  return <span data-testid="cloud-status">{cloudAuth.status}</span>;
}

/**
 * Probe the APP-WIDE store directly. This is the value AccountTab's
 * ProfileCard, SmartDisplay's gating, and useUsage all read - asserting on it
 * is what distinguishes a real reconciliation from a pill-shaped patch.
 */
function StoreProbe() {
  appAuth = useAppAuth();
  return (
    <div>
      <span data-testid="store-logged-in">{String(appAuth.isLoggedIn)}</span>
      <span data-testid="store-subscription">{JSON.stringify(appAuth.subscription)}</span>
    </div>
  );
}

/** Mirrors App.jsx's `Dashboard()` on the cloud surface: the app store is
 *  mounted INSIDE the front-door gate, exactly as production nests them. */
function CloudGate({ children }) {
  const { status } = useCloudAuth();
  if (status !== 'signedIn') return <span data-testid="gate">gated</span>;
  return children;
}

CloudGate.propTypes = { children: PropTypes.node };

function renderCloudApp() {
  return render(
    <UiStateProvider>
      <CloudAccountProvider>
        <CloudDriver />
        <CloudGate>
          <AppAuthProvider>
            <StoreProbe />
            <AccountTab />
          </AppAuthProvider>
        </CloudGate>
      </CloudAccountProvider>
    </UiStateProvider>,
  );
}

/**
 * The app-wide store mounted on its OWN, with no cloud front door above it -
 * the shape the desktop app, ReviewPage and the spoke tree all use. There is
 * no refresh owner to delegate to there, so `refreshUser` must keep refreshing
 * for itself (on desktop those refreshes are serialized server-side by
 * auth/desktop_gotrue_proxy.py).
 */
function renderStandaloneAppStore() {
  return render(
    <UiStateProvider>
      <AppAuthProvider>
        <StoreProbe />
      </AppAuthProvider>
    </UiStateProvider>,
  );
}

async function signInThroughFrontDoor() {
  await act(async () => {
    await cloudAuth.signIn('paying@example.com', 'pw');
  });
}

beforeEach(async () => {
  cloudAuth = null;
  appAuth = null;
  localStorage.clear();
  // Both stores are module-level singletons: the front door keeps its session
  // in module memory (SEC-017) and gotrueClient is a module singleton. Clear
  // both, or one test's session hydrates the next one's provider and the
  // suite's result depends on file order.
  __setInMemorySessionForTest(null);
  await gotrueClient.signOut({ scope: 'local' }).catch(() => {});
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('C-400 - the app-wide store is reconciled for a cloud front-door sign-in', () => {
  it('a PAID cloud user has paid `subscription` in the app store, and sees the Pro pill + Manage Subscription', async () => {
    const { fetchMock } = fakeGoTrue({ appMetadata: PAID_METADATA });
    vi.stubGlobal('fetch', fetchMock);

    renderCloudApp();
    await waitFor(() => expect(screen.getByTestId('cloud-status').textContent).toBe('signedOut'));

    await signInThroughFrontDoor();

    // The STORE - not the pill - carries the paid entitlement.
    await waitFor(() => {
      expect(screen.getByTestId('store-logged-in').textContent).toBe('true');
    });
    await waitFor(() => {
      const subscription = JSON.parse(screen.getByTestId('store-subscription').textContent);
      expect(subscription).not.toBeNull();
      expect(subscription.hasPaidAccess).toBe(true);
      expect(subscription.planFamily).toBe('pro');
      expect(subscription.paymentProvider).toBe('stripe');
    });

    // ...and the panel the customer actually looks at agrees.
    expect(await screen.findByText('paying@example.com')).toBeInTheDocument();
    expect(screen.getByText('Pro')).toBeInTheDocument();
    expect(screen.queryByText('Free Plan')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Manage Subscription/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Upgrade Plan/i })).not.toBeInTheDocument();
  });

  it('the post-checkout entitlement refresh surfaces the new plan AND leaves the session alive', async () => {
    // The #2609 path: UpgradePanel's visibilitychange handler calls the app
    // store's refreshUser() when the tab regains focus after Stripe checkout.
    // On the cloud surface the front door owns the rotating refresh token, so
    // an independent refresh here rotates it out from under that owner - and
    // GoTrue's reuse detection revokes the whole family at the front door's
    // next scheduled refresh, signing the customer out minutes after they paid.
    vi.useFakeTimers();
    const { state, fetchMock } = fakeGoTrue({ appMetadata: FREE_METADATA, expiresIn: 300 });
    vi.stubGlobal('fetch', fetchMock);

    renderCloudApp();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });

    await act(async () => {
      await cloudAuth.signIn('paying@example.com', 'pw');
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(screen.getByTestId('cloud-status').textContent).toBe('signedIn');
    expect(screen.getByText('Free Plan')).toBeInTheDocument();

    // Stripe's webhook writes the upgrade onto the GoTrue user.
    state.appMetadata = PAID_METADATA;

    await act(async () => {
      await appAuth.refreshUser();
      await vi.advanceTimersByTimeAsync(50);
    });

    // The upgrade is visible without a re-login.
    const afterRefresh = JSON.parse(screen.getByTestId('store-subscription').textContent);
    expect(afterRefresh?.hasPaidAccess).toBe(true);
    expect(screen.getByText('Pro')).toBeInTheDocument();

    // ...and the session survives the front door's next scheduled refresh
    // (T+240s: 300s token life minus the 60s refresh lead).
    await act(async () => { await vi.advanceTimersByTimeAsync(250_000); });
    expect(state.revoked).toBe(false);
    expect(screen.getByTestId('cloud-status').textContent).toBe('signedIn');
    expect(screen.queryByRole('heading', { name: /^Sign In$/i })).not.toBeInTheDocument();

    // And a second cycle stays clean, so the fix is not a one-shot.
    await act(async () => { await vi.advanceTimersByTimeAsync(250_000); });
    expect(state.revoked).toBe(false);
    expect(screen.getByTestId('cloud-status').textContent).toBe('signedIn');
  });

  it('two concurrent entitlement refreshes collapse into ONE redemption of the shared token', async () => {
    // Delegating to the owner is only half the invariant: the owner must also
    // redeem once at a time. Two askers arriving in the same tick both read the
    // owner's `refreshTokenRef.current` before either has committed a rotation,
    // so the loser replays an already-rotated token and GoTrue's reuse
    // detection revokes the whole family - the same sign-out the delegation
    // exists to prevent, reintroduced inside the owner. Concurrency here is not
    // hypothetical: `refreshSessionNow` is public, and one return to the tab
    // after Stripe checkout raises BOTH `visibilitychange` and `focus`.
    const { state, fetchMock } = fakeGoTrue({ appMetadata: FREE_METADATA, expiresIn: 300 });
    vi.stubGlobal('fetch', fetchMock);

    renderCloudApp();
    await signInThroughFrontDoor();
    await waitFor(() => expect(screen.getByTestId('store-logged-in').textContent).toBe('true'));

    state.appMetadata = PAID_METADATA;
    const grantsBefore = state.refreshGrants.length;

    await act(async () => {
      await Promise.all([appAuth.refreshUser(), appAuth.refreshUser()]);
    });

    // ONE grant for two askers - and every presented token was the live one.
    expect(state.refreshGrants.length - grantsBefore).toBe(1);
    expect(state.revoked).toBe(false);
    expect(screen.getByTestId('cloud-status').textContent).toBe('signedIn');

    // The sharing asker still gets the fresh entitlement, not a stale read.
    await waitFor(() => {
      const subscription = JSON.parse(screen.getByTestId('store-subscription').textContent);
      expect(subscription?.hasPaidAccess).toBe(true);
    });
  });

  it('one post-checkout tab return fires both listeners and still redeems the token once', async () => {
    // The same invariant through the real product path rather than by calling
    // refreshUser twice by hand: UpgradePanel registers its return handler on
    // BOTH `document.visibilitychange` and `window.focus`, and a real return to
    // the tab raises both.
    vi.useFakeTimers();
    const { state, fetchMock } = fakeGoTrue({ appMetadata: FREE_METADATA, expiresIn: 300 });
    vi.stubGlobal('fetch', fetchMock);
    // jsdom has no real window.open; the checkout hand-off only needs the call.
    vi.stubGlobal('open', vi.fn());

    renderCloudApp();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => {
      await cloudAuth.signIn('paying@example.com', 'pw');
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(screen.getByText('Free Plan')).toBeInTheDocument();

    // Start a real checkout, which is what arms the return handler.
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Upgrade Plan/i }));
      await vi.advanceTimersByTimeAsync(0);
    });

    // Stripe's webhook writes the upgrade while the customer is on Stripe.
    state.appMetadata = PAID_METADATA;
    const grantsBefore = state.refreshGrants.length;

    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new Event('focus'));
      await vi.advanceTimersByTimeAsync(100);
    });

    expect(state.refreshGrants.length - grantsBefore).toBe(1);
    expect(state.revoked).toBe(false);
    expect(screen.getByText('Pro')).toBeInTheDocument();

    // ...and the front door's next scheduled refresh still succeeds.
    await act(async () => { await vi.advanceTimersByTimeAsync(250_000); });
    expect(state.revoked).toBe(false);
    expect(screen.getByTestId('cloud-status').textContent).toBe('signedIn');
  });

  it('with no front door above it, the app store still refreshes for itself (desktop / ReviewPage path)', async () => {
    // The delegation must not become a hard dependency: where no owner exists,
    // refreshUser keeps its own refresh path, so the desktop app and the
    // standalone ReviewPage/spoke trees are untouched by this change.
    const { state, fetchMock } = fakeGoTrue({ appMetadata: FREE_METADATA });
    vi.stubGlobal('fetch', fetchMock);

    // Seed the store's client the way those surfaces do (desktop: the httpOnly
    // session cookie; here: a direct session hand-off).
    const seeded = await fetch('/auth/v1/token?grant_type=password', { method: 'POST' }).then((r) => r.json());
    await act(async () => {
      await gotrueClient.setSession({
        access_token: seeded.access_token,
        refresh_token: seeded.refresh_token,
      });
    });

    renderStandaloneAppStore();
    await waitFor(() => expect(screen.getByTestId('store-logged-in').textContent).toBe('true'));

    state.appMetadata = PAID_METADATA;
    await act(async () => { await appAuth.refreshUser(); });

    await waitFor(() => {
      const subscription = JSON.parse(screen.getByTestId('store-subscription').textContent);
      expect(subscription?.hasPaidAccess).toBe(true);
    });
    // It really redeemed the token itself - there was no owner to ask.
    expect(state.refreshGrants.length).toBeGreaterThanOrEqual(1);
    expect(state.revoked).toBe(false);
  });

  it('negative control: a FREE cloud user still gets Free Plan, no portal button, and the upgrade panel', async () => {
    const { fetchMock } = fakeGoTrue({ appMetadata: FREE_METADATA });
    vi.stubGlobal('fetch', fetchMock);

    renderCloudApp();
    await signInThroughFrontDoor();

    await waitFor(() => {
      const subscription = JSON.parse(screen.getByTestId('store-subscription').textContent);
      expect(subscription).not.toBeNull();
      expect(subscription.hasPaidAccess).toBe(false);
    });

    expect(await screen.findByText('Free Plan')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Manage Subscription/i })).not.toBeInTheDocument();
  });
});
