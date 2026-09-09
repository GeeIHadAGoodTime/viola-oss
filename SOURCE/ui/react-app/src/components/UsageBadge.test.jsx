import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '../test/test-utils';
import { AuthProvider } from '../hooks/useAuth';
import { AuthProvider as CloudAuthProvider } from '../auth/AuthProvider';
import { GOTRUE_STORAGE_KEY } from '../lib/gotrue_client';
import { inMemorySessionStorage } from '../lib/sessionStore';
import { AccountTab } from './AccountTab';

// AccountTab also reads the separate cloud-front-door auth context
// (auth/AuthProvider — issue #1067); real app usage always wraps it, so
// tests must too or useCloudAuth() throws "must be used within AuthProvider".
function renderAccountTab(ui) {
  return render(<CloudAuthProvider><AuthProvider>{ui}</AuthProvider></CloudAuthProvider>);
}

// Signed-in state comes from the GoTrue session (lib/auth_context reads the
// session user's app_metadata), not the legacy /auth/me endpoint. SEC-017
// keeps sessions in the in-memory auth-js storage adapter — seed that store
// before rendering. Mirrors AccountTab.test.jsx.
function seedGoTrueSession({ id, email, plan_tier = 'free' }) {
  inMemorySessionStorage.setItem(GOTRUE_STORAGE_KEY, JSON.stringify({
    access_token: 'test-access-token',
    refresh_token: 'test-refresh-token',
    token_type: 'bearer',
    expires_in: 86400,
    expires_at: Math.floor(Date.now() / 1000) + 86400,
    user: {
      id,
      aud: 'authenticated',
      role: 'authenticated',
      email,
      email_confirmed_at: '2026-01-01T00:00:00.000Z',
      created_at: '2026-01-01T00:00:00.000Z',
      updated_at: '2026-01-01T00:00:00.000Z',
      app_metadata: { provider: 'email', plan_tier },
      user_metadata: {},
    },
  }));
}

// Stubs the canonical /billing/usage progress-bar endpoint, then asserts the
// rendered percentage. Per PAY-26: cents never reach the wire, never
// reach the UI.
function buildFetchSpy({ usagePayload, calendarStatus = { providers: [] } }) {
  return vi.fn(async (input) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.endsWith('/billing/usage')) {
      return {
        ok: true,
        status: 200,
        json: () => Promise.resolve(usagePayload),
      };
    }
    if (url.endsWith('/v1/calendar/status')) {
      return {
        ok: true,
        status: 200,
        json: () => Promise.resolve(calendarStatus),
      };
    }
    // Default OK for anything else AccountTab side-effects fire.
    return { ok: true, status: 200, json: () => Promise.resolve({}) };
  });
}

describe('AccountTab — spend usage badge', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    inMemorySessionStorage.removeItem(GOTRUE_STORAGE_KEY);
  });

  it('renders the monthly percentage from the canonical /billing/usage payload', async () => {
    seedGoTrueSession({ id: 'u1', email: 'pro@example.com', plan_tier: 'pro' });
    const fetchSpy = buildFetchSpy({
      usagePayload: {
        monthly_percent: 85,
        weekly_percent: 0,
        percent: 85,
        monthly_capped: true,
        weekly_capped: false,
        resets_monthly: '2026-06-02T03:08:12Z',
        resets_weekly: '2026-05-09T03:08:12Z',
        extra_usage_cents: 0,
      },
    });
    vi.stubGlobal('fetch', fetchSpy);

    renderAccountTab(<AccountTab />);

    // Auth context resolves the logged-in user → ProfileCard mounts → useUsage fires.
    await waitFor(() => {
      expect(screen.getByText(/85% used this month/i)).toBeInTheDocument();
    });

    // Weekly row stays hidden because weekly_capped is false on Pro.
    expect(screen.queryByText(/used this week/i)).not.toBeInTheDocument();

    // Reset-date label is present and rendered separately.
    expect(screen.getByText(/resets/i)).toBeInTheDocument();

    // Progressbar role is wired with the percent so screen readers can pick it up.
    const progress = screen.getByRole('progressbar', { name: /month usage/i });
    expect(progress.getAttribute('aria-valuenow')).toBe('85');
  });

  it('hides the badge entirely when both limits are unlimited (e.g., Max plan)', async () => {
    seedGoTrueSession({ id: 'u2', email: 'max@example.com', plan_tier: 'max' });
    const fetchSpy = buildFetchSpy({
      usagePayload: {
        monthly_percent: 0,
        weekly_percent: 0,
        percent: 0,
        monthly_capped: false,
        weekly_capped: false,
        resets_monthly: '2026-06-02T03:08:12Z',
        resets_weekly: '2026-05-09T03:08:12Z',
        extra_usage_cents: 0,
      },
    });
    vi.stubGlobal('fetch', fetchSpy);

    renderAccountTab(<AccountTab />);

    // Wait for the profile card to mount (logged-in heading renders email).
    await screen.findByText(/max@example.com/);

    // Give the usage poll a tick to land. Even after it does, the badge stays hidden.
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        expect.stringMatching(/\/billing\/usage$/),
        expect.anything(),
      );
    });

    expect(screen.queryByText(/used this month/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/used this week/i)).not.toBeInTheDocument();
  });

  it('also renders the weekly row on Free (weekly_capped true)', async () => {
    seedGoTrueSession({ id: 'u3', email: 'free@example.com', plan_tier: 'free' });
    const fetchSpy = buildFetchSpy({
      usagePayload: {
        monthly_percent: 40,
        weekly_percent: 60,
        percent: 60,
        monthly_capped: true,
        weekly_capped: true,
        resets_monthly: '2026-06-02T03:08:12Z',
        resets_weekly: '2026-05-09T03:08:12Z',
        extra_usage_cents: 0,
      },
    });
    vi.stubGlobal('fetch', fetchSpy);

    renderAccountTab(<AccountTab />);

    await waitFor(() => {
      expect(screen.getByText(/40% used this month/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/60% used this week/i)).toBeInTheDocument();
  });
});
