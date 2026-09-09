import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '../test/test-utils';
import { AuthProvider } from '../hooks/useAuth';
import {
  AuthProvider as CloudAuthProvider,
  __setInMemorySessionForTest,
} from '../auth/AuthProvider';
import { AccountTab } from './AccountTab';

// Regression test for issue #2741: a signed-in PAID user was shown as "Free"
// (grey badge, "Upgrade Plan", no "Manage Subscription", "Cloud Sync:
// Disabled") on the cloud SPA. Root cause: AccountTab.jsx's ProfileCard and
// SyncStatus read `subscription` ONLY from the desktop-store useAuth()
// (lib/auth_context's buildSubscription), which stays null for a user
// authenticated solely through the cloud front door (auth/AuthProvider /
// useCloudAuth) -- the exact shape this file seeds, since that IS how a real
// cloud-only user authenticates.
//
// This seeds ONLY the cloud store (auth/AuthProvider's in-memory session, via
// its test seam) and leaves the desktop-store GoTrue session (hooks/useAuth)
// empty, matching the real cloud-surface shape.

function cloudGotrueUser({ id, email, app_metadata = {} }) {
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

function cloudSession(user) {
  return {
    access_token: 'cloud-access-token',
    refresh_token: 'cloud-refresh-token',
    token_type: 'bearer',
    expires_in: 86400,
    expires_at: Math.floor(Date.now() / 1000) + 86400,
    user,
  };
}

function seedCloudOnlySession(user) {
  __setInMemorySessionForTest(cloudSession(user));
}

function buildFetchSpy(usagePayload) {
  return vi.fn(async (input) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.endsWith('/billing/usage')) {
      return { ok: true, status: 200, json: () => Promise.resolve(usagePayload) };
    }
    if (url.endsWith('/v1/calendar/status')) {
      return { ok: true, status: 200, json: () => Promise.resolve({ providers: [] }) };
    }
    return { ok: true, status: 200, json: () => Promise.resolve({}) };
  });
}

function renderCloudOnly(ui) {
  // AccountTab reads BOTH the desktop-store useAuth() (hooks/useAuth) and the
  // cloud front-door useCloudAuth() (auth/useAuth) -- real app usage always
  // wraps both providers, so tests must too.
  return render(
    <CloudAuthProvider>
      <AuthProvider>{ui}</AuthProvider>
    </CloudAuthProvider>,
  );
}

const CLOUD_PRO_USER = cloudGotrueUser({
  id: 'cloud-pro',
  email: 'cloud-pro@example.com',
  app_metadata: {
    plan_tier: 'pro',
    plan_id: 'pro_monthly',
    plan_family: 'pro',
    subscription_status: 'active',
    has_paid_access: true,
    payment_provider: 'stripe',
  },
});

const CLOUD_FREE_USER = cloudGotrueUser({
  id: 'cloud-free',
  email: 'cloud-free@example.com',
  app_metadata: {
    plan_tier: 'free',
  },
});

describe('AccountTab — cloud-only-signed-in plan display (#2741)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    __setInMemorySessionForTest(null);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    __setInMemorySessionForTest(null);
  });

  it('shows a paid cloud-only user as Pro with Manage Subscription and Sync active, never Free', async () => {
    seedCloudOnlySession(CLOUD_PRO_USER);
    vi.stubGlobal('fetch', buildFetchSpy({
      monthly_percent: 20,
      weekly_percent: 0,
      monthly_capped: true,
      weekly_capped: false,
      resets_monthly: '2026-08-01T00:00:00.000Z',
      resets_weekly: '2026-07-28T00:00:00.000Z',
      extra_usage_cents: 0,
    }));

    renderCloudOnly(<AccountTab />);

    await screen.findByText('cloud-pro@example.com');

    expect(screen.getByText(/Pro/)).toBeInTheDocument();
    expect(screen.queryByText(/Free Plan/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Upgrade/i })).not.toBeInTheDocument();

    expect(await screen.findByRole('button', { name: /Manage Subscription/i })).toBeInTheDocument();

    expect(await screen.findByText(/Synced across your devices/i)).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
  });

  it('still shows a free cloud-only user as Free with Upgrade and Sync disabled', async () => {
    seedCloudOnlySession(CLOUD_FREE_USER);
    vi.stubGlobal('fetch', buildFetchSpy({
      monthly_percent: 0,
      weekly_percent: 0,
      monthly_capped: false,
      weekly_capped: false,
      resets_monthly: '2026-08-01T00:00:00.000Z',
      resets_weekly: '2026-07-28T00:00:00.000Z',
      extra_usage_cents: 0,
    }));

    renderCloudOnly(<AccountTab />);

    await screen.findByText('cloud-free@example.com');

    expect(screen.getByText(/Free Plan/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Manage Subscription/i })).not.toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText(/Sign in to sync|Upgrade to sync/i)).toBeInTheDocument();
    });
    expect(screen.getByText('Disabled')).toBeInTheDocument();
  });
});
