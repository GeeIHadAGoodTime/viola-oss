/**
 * #3547 adversarial-review finding: bridging a session into gotrueClient
 * (lib/gotrue_client.ts, autoRefreshToken: true) risks a SECOND, independent
 * refresher racing AuthProvider's own scheduleRefresh/runRefresh on the SAME
 * rotating GoTrue refresh_token. GoTrue revokes the whole token family when a
 * stale (already-rotated) refresh_token is replayed -- the client-side
 * sibling of the 2026-07-02 dual-refresher incident serialized server-side
 * for desktop in auth/desktop_gotrue_proxy.py's _serve_desktop_refresh_grant.
 *
 * Unlike AuthProvider.test.jsx (which mocks gotrueClient entirely and so
 * cannot see this), this file uses the REAL gotrueClient / real
 * @supabase/auth-js GoTrueClient and REAL AuthProvider.jsx, mocking only
 * `fetch` with a stateful fake GoTrue server that enforces real rotation
 * semantics: a refresh_token may be redeemed exactly once; replaying an
 * already-consumed one revokes the whole session (the reuse-detection
 * behavior that turns a benign race into a forced sign-out).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { AuthProvider, __setInMemorySessionForTest } from './AuthProvider';
import { useAuth } from './useAuth';
import { gotrueClient } from '../lib/gotrue_client';

/** Minimal (unsigned, unverified -- decodeJWT never checks the signature)
 * three-segment JWT carrying an `exp` claim, matching what both
 * auth-js's own decodeJWT and this app's decodeJwtPayload expect. */
function fakeJwt(payload) {
  const seg = (obj) => btoa(JSON.stringify(obj)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
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

/** Test harness. */
let captured = null;
function Probe() {
  const auth = useAuth();
  captured = auth;
  return (
    <div>
      <span data-testid="status">{auth.status}</span>
    </div>
  );
}

function renderProvider() {
  return render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  );
}

beforeEach(() => {
  captured = null;
  localStorage.clear();
  __setInMemorySessionForTest(null);
  vi.restoreAllMocks();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('AuthProvider + real gotrueClient: single-refresher bridge (#3547 adversarial finding)', () => {
  it('a real refresh cycle produces exactly ONE refresh_token grant, keeps the bridged store current, and never signs the user out', async () => {
    vi.useFakeTimers();

    // gotrueClient is a module-level singleton (`export const gotrueClient =
    // createGoTrueClient()` in lib/gotrue_client.ts) constructed once when the
    // test FILE's imports resolve -- BEFORE vi.useFakeTimers() above ever
    // runs. Its constructor already started auto-refresh's internal ticker on
    // the REAL setInterval, which fake timers cannot see or advance (vitest's
    // fake timers only govern timers created AFTER they're installed). To
    // actually exercise the race under fake-timer control, re-arm the ticker
    // now that fake timers are active -- equivalent to "the ticker has been
    // live since some earlier point in time" in a real browser tab, which is
    // exactly the production shape (the client is constructed once at app
    // bundle load, long before any particular sign-in).
    await gotrueClient.stopAutoRefresh();
    await gotrueClient.startAutoRefresh();

    // 300s token life (matches the reviewer's GOTRUE_JWT_EXP=300s scenario).
    // gotrueClient's own ticker (if not stopped) would try to refresh at
    // T+210s (90s lead: 3 ticks * 30s AUTO_REFRESH_TICK_DURATION_MS);
    // AuthProvider's own scheduleRefresh fires at T+240s (60s REFRESH_LEAD_MS).
    const EXPIRES_IN = 300;
    let currentRefreshToken = null;
    let revoked = false;
    let seq = 0;
    const refreshGrantBodies = [];

    function issue() {
      seq += 1;
      currentRefreshToken = `refresh-${seq}`;
      const nowS = Math.floor(Date.now() / 1000);
      const accessToken = fakeJwt({ sub: 'user-1', exp: nowS + EXPIRES_IN, aal: 'aal1' });
      return {
        access_token: accessToken,
        refresh_token: currentRefreshToken,
        token_type: 'bearer',
        expires_in: EXPIRES_IN,
        expires_at: nowS + EXPIRES_IN,
        user: { id: 'user-1', email: 'a@b.com', factors: [], app_metadata: {}, user_metadata: {} },
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
        } catch { /* malformed body -> treated as no token below */ }
        refreshGrantBodies.push(presented);

        if (revoked) {
          return jsonResponse(
            { error: 'invalid_grant', error_description: 'Invalid Refresh Token: session revoked' },
            400,
          );
        }
        if (presented !== currentRefreshToken) {
          // Reuse of an already-rotated refresh_token -- GoTrue's real
          // reuse-detection response: revoke the whole family.
          revoked = true;
          return jsonResponse(
            { error: 'invalid_grant', error_description: 'Invalid Refresh Token: Already Used' },
            400,
          );
        }
        return jsonResponse(issue());
      }

      if (path.endsWith('/auth/v1/user')) {
        return jsonResponse({ id: 'user-1', email: 'a@b.com', factors: [], app_metadata: {}, user_metadata: {} });
      }

      if (path.endsWith('/auth/v1/logout')) {
        return jsonResponse(null, 204);
      }

      return jsonResponse({}, 200);
    });

    vi.stubGlobal('fetch', fetchMock);

    renderProvider();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByTestId('status').textContent).toBe('signedOut');

    // Sign in through the REAL front-door path (authClient -> commitSession
    // -> the #3547 bridge -> gotrueClient.setSession -> stopAutoRefresh).
    await act(async () => {
      await captured.signIn('a@b.com', 'pw');
      // Let the fire-and-forget bridge (setSession's GET /auth/v1/user round
      // trip, then stopAutoRefresh) actually settle before advancing time.
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(screen.getByTestId('status').textContent).toBe('signedIn');

    // The bridge populated gotrueClient with the SAME session.
    const bridgedAfterSignIn = await gotrueClient.getSession();
    expect(bridgedAfterSignIn.data.session?.refresh_token).toBe(currentRefreshToken);

    // Advance to T+215s: past where gotrueClient's OWN ticker would have
    // tried to refresh (T+210s) if its auto-refresh were still armed, but
    // BEFORE AuthProvider's own scheduled refresh (T+240s). If the bridge
    // failed to stop gotrueClient's ticker, this is where the SECOND
    // refresher fires first and rotates the token out from under
    // AuthProvider -- the exact race the reviewer found.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(215_000);
    });
    expect(refreshGrantBodies.length).toBe(0);
    expect(screen.getByTestId('status').textContent).toBe('signedIn');

    // Advance past AuthProvider's own scheduled refresh (T+240s).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    // Exactly ONE refresh grant for this cycle -- AuthProvider's own, not a
    // second one from gotrueClient's ticker.
    expect(refreshGrantBodies.length).toBe(1);
    expect(refreshGrantBodies[0]).toBe('refresh-1');
    // The family was never revoked (no replay of a stale token occurred).
    expect(revoked).toBe(false);
    expect(screen.getByTestId('status').textContent).toBe('signedIn');

    // The bridge re-armed gotrueClient with the freshly rotated pair, so the
    // OTHER store (SmartDisplay/AccountTab/useUsage/ReviewPage) stays current
    // rather than going stale after the first refresh cycle.
    await act(async () => { await vi.advanceTimersByTimeAsync(50); });
    const bridgedAfterRefresh = await gotrueClient.getSession();
    expect(bridgedAfterRefresh.data.session?.refresh_token).toBe(currentRefreshToken);
    expect(bridgedAfterRefresh.data.session?.refresh_token).not.toBe('refresh-1');

    // A SECOND full cycle stays clean too (proves the fix is durable across
    // repeated refresh -> re-bridge cycles, not a one-shot stop that a later
    // visibilitychange or re-arm could undo).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(240_000);
    });
    expect(revoked).toBe(false);
    expect(screen.getByTestId('status').textContent).toBe('signedIn');
    expect(refreshGrantBodies.length).toBeGreaterThanOrEqual(2);
  });
});
