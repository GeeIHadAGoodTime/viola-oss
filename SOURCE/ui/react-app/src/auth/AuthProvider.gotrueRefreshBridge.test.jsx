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

function bridgeSession(id) {
  const expiresAt = Math.floor(Date.now() / 1000) + 3600;
  return {
    access_token: fakeJwt({ sub: id, exp: expiresAt, aal: 'aal1' }),
    refresh_token: `${id}-refresh`, token_type: 'bearer', expires_in: 3600,
    expires_at: expiresAt, user: { id, factors: [], app_metadata: {}, user_metadata: {} },
  };
}

function deferredBridge() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function withBridgeFixture(check) {
  const previousSurface = window.viola;
  delete window.viola;
  const first = bridgeSession('synthetic-first');
  const second = bridgeSession('synthetic-second');
  const state = { current: first, userReplies: [], sdkLogoutReplies: [], apiLogoutReplies: [], calls: [] };
  const fetchMock = vi.fn(async (input, init) => {
    const url = new URL(typeof input === 'string' ? input : input.url, 'http://localhost');
    const scope = url.searchParams.get('scope');
    state.calls.push({ path: url.pathname, scope });
    if (url.pathname.endsWith('/auth/v1/token')) return jsonResponse(state.current);
    if (url.pathname.endsWith('/auth/v1/user')) {
      if (state.userReplies.length) return state.userReplies.shift()();
      const token = new Headers(init?.headers).get('authorization');
      return jsonResponse(token === `Bearer ${second.access_token}` ? second.user : first.user);
    }
    if (url.pathname.endsWith('/auth/v1/logout')) {
      const replies = scope === 'local' ? state.sdkLogoutReplies : state.apiLogoutReplies;
      return replies.length ? replies.shift()() : jsonResponse({}, 204);
    }
    throw new Error(`Unexpected synthetic route: ${url.pathname}`);
  });
  vi.stubGlobal('fetch', fetchMock);
  await gotrueClient.stopAutoRefresh();
  await gotrueClient.signOut({ scope: 'local' });
  state.calls.length = 0;
  let view;
  const fixture = {
    first, second, state,
    render: () => { view = renderProvider(); return view; },
    signIn: async (session = first) => {
      state.current = session;
      await act(async () => { await captured.signIn('synthetic@example.invalid', 'synthetic-password'); });
    },
  };
  try {
    await check(fixture);
  } finally {
    view?.unmount();
    state.sdkLogoutReplies.length = 0;
    state.apiLogoutReplies.length = 0;
    state.userReplies.length = 0;
    await gotrueClient.signOut({ scope: 'local' });
    await gotrueClient.stopAutoRefresh();
    if (previousSurface === undefined) delete window.viola;
    else window.viola = previousSurface;
  }
}

it('waits for the first owned SDK write and its cleanup before acknowledging logout', async () => {
  await withBridgeFixture(async (f) => {
    const user = deferredBridge();
    const entered = deferredBridge();
    const cleanup = deferredBridge();
    const cleanupEntered = deferredBridge();
    f.state.userReplies.push(() => { entered.resolve(); return user.promise; });
    f.state.sdkLogoutReplies.push(() => { cleanupEntered.resolve(); return cleanup.promise; });
    f.render();
    await f.signIn();
    await entered.promise;
    let acknowledged = false;
    let logout;
    await act(async () => { logout = captured.signOut().then((result) => { acknowledged = true; return result; }); });
    try {
      expect(captured.status).toBe('signedOut');
      expect(acknowledged).toBe(false);
      await act(async () => { user.resolve(jsonResponse(f.first.user)); await cleanupEntered.promise; });
      expect(acknowledged).toBe(false);
    } finally {
      user.resolve(jsonResponse(f.first.user));
      cleanup.resolve(jsonResponse({}, 204));
      await act(async () => { await logout; });
    }
    expect(await logout).toEqual({ ok: true, error: null });
    expect((await gotrueClient.getSession()).data.session).toBeNull();
  });
});

it.each([429, 500, 503, 'throw'])('reports SDK cleanup failure and permits an explicit retry (%s)', async (failure) => {
  await withBridgeFixture(async (f) => {
    f.render();
    await f.signIn();
    expect((await gotrueClient.getSession()).data.session?.refresh_token).toBe(f.first.refresh_token);
    f.state.sdkLogoutReplies.push(() => {
      if (failure === 'throw') throw new Error('Synthetic cleanup transport failure');
      return jsonResponse({ message: 'Synthetic cleanup failure' }, failure);
    });
    let result;
    await act(async () => { result = await captured.signOut(); });
    expect(result.ok).toBe(false);
    expect(result.error.code).toBe('app_store_signout_failed');
    expect(captured.status).toBe('signedOut');
    expect((await gotrueClient.getSession()).data.session?.refresh_token).toBe(f.first.refresh_token);
    await act(async () => { result = await captured.signOut(); });
    expect(result).toEqual({ ok: true, error: null });
    expect((await gotrueClient.getSession()).data.session).toBeNull();
  });
});

it.each([204, 401, 403, 404])('keeps successful or already-revoked SDK cleanup successful (%s)', async (status) => {
  await withBridgeFixture(async (f) => {
    f.render();
    await f.signIn();
    expect((await gotrueClient.getSession()).data.session?.refresh_token).toBe(f.first.refresh_token);
    f.state.sdkLogoutReplies.push(() => jsonResponse({}, status));
    let result;
    await act(async () => { result = await captured.signOut(); });
    expect(result).toEqual({ ok: true, error: null });
    expect((await gotrueClient.getSession()).data.session).toBeNull();
  });
});

it('preserves independently owned SDK state during initial signed-out hydration', async () => {
  await withBridgeFixture(async (f) => {
    await gotrueClient.setSession(f.second);
    f.render();
    await act(async () => { await Promise.resolve(); });
    expect(captured.status).toBe('signedOut');
    expect((await gotrueClient.getSession()).data.session?.refresh_token).toBe(f.second.refresh_token);
    expect(f.state.calls.filter((call) => call.scope === 'local')).toHaveLength(0);
  });
});

it('retires an obsolete bridge retry before acknowledging logout', async () => {
  await withBridgeFixture(async (f) => {
    vi.useFakeTimers();
    f.state.userReplies.push(() => jsonResponse({ message: 'Synthetic user read failure' }, 503));
    f.render();
    await f.signIn();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    let result;
    await act(async () => {
      const logout = captured.signOut();
      await vi.advanceTimersByTimeAsync(2251);
      result = await logout;
    });
    expect(result).toEqual({ ok: true, error: null });
    expect(f.state.calls.filter((call) => call.path.endsWith('/user'))).toHaveLength(1);
    expect((await gotrueClient.getSession()).data.session).toBeNull();
  });
});

it.each(['queued', 'started'])('preserves a newer committed session while older SDK cleanup is %s', async (phase) => {
  await withBridgeFixture(async (f) => {
    const held = deferredBridge();
    const entered = deferredBridge();
    if (phase === 'queued') f.state.userReplies.push(() => { entered.resolve(); return held.promise; });
    else f.state.sdkLogoutReplies.push(() => { entered.resolve(); return held.promise; });
    f.render();
    await f.signIn();
    if (phase === 'queued') await entered.promise;
    else expect((await gotrueClient.getSession()).data.session?.refresh_token).toBe(f.first.refresh_token);
    let logout;
    await act(async () => { logout = captured.signOut(); });
    if (phase === 'started') await entered.promise;
    await f.signIn(f.second);
    try {
      expect(captured.user.id).toBe(f.second.user.id);
    } finally {
      held.resolve(phase === 'queued' ? jsonResponse(f.first.user) : jsonResponse({}, 204));
      await act(async () => { await logout; });
    }
    expect((await logout).ok).toBe(false);
    expect((await logout).error?.code).toBe('auth_session_changed');
    expect(captured.user.id).toBe(f.second.user.id);
    expect((await gotrueClient.getSession()).data.session?.refresh_token).toBe(f.second.refresh_token);
  });
});

it('does not let a slower server logout clear a newer committed session', async () => {
  await withBridgeFixture(async (f) => {
    f.render();
    await f.signIn();
    const response = deferredBridge();
    f.state.apiLogoutReplies.push(() => response.promise);
    let logout;
    await act(async () => { logout = captured.signOut(); });
    await f.signIn(f.second);
    response.resolve(jsonResponse({}, 204));
    await act(async () => { await logout; });
    expect((await logout).error?.code).toBe('auth_session_changed');
    expect(captured.user.id).toBe(f.second.user.id);
    expect((await gotrueClient.getSession()).data.session?.refresh_token).toBe(f.second.refresh_token);
  });
});

it('retains a server revocation failure after successful local SDK cleanup', async () => {
  await withBridgeFixture(async (f) => {
    f.render();
    await f.signIn();
    f.state.apiLogoutReplies.push(() => jsonResponse({ message: 'Synthetic revocation failure' }, 503));
    let result;
    await act(async () => { result = await captured.signOut(); });
    expect(result.ok).toBe(false);
    expect(result.error.status).toBe(503);
    expect(captured.status).toBe('signedOut');
    expect((await gotrueClient.getSession()).data.session).toBeNull();
  });
});

it('cleans an owned SDK write even when a subscriber throws after storage was updated', async () => {
  await withBridgeFixture(async (f) => {
    vi.useFakeTimers();
    const { data: { subscription } } = gotrueClient.onAuthStateChange((event, session) => {
      if (event === 'SIGNED_IN' && session?.refresh_token === f.first.refresh_token) {
        throw new Error('Synthetic post-storage subscriber failure');
      }
    });
    try {
      f.render();
      await f.signIn();
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect((await gotrueClient.getSession()).data.session?.refresh_token).toBe(f.first.refresh_token);
      let result;
      await act(async () => {
        const logout = captured.signOut();
        await vi.advanceTimersByTimeAsync(2251);
        result = await logout;
      });
      expect(result).toEqual({ ok: true, error: null });
      expect((await gotrueClient.getSession()).data.session).toBeNull();
    } finally {
      subscription.unsubscribe();
    }
  });
});

it('a failed first bridge does not claim an unrelated pre-existing SDK session', async () => {
  await withBridgeFixture(async (f) => {
    await gotrueClient.setSession(f.second);
    vi.useFakeTimers();
    f.state.userReplies.push(() => jsonResponse({ message: 'Synthetic bridge failure' }, 503));
    f.render();
    await f.signIn();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    let result;
    await act(async () => {
      const logout = captured.signOut();
      await vi.advanceTimersByTimeAsync(2251);
      result = await logout;
    });
    expect(result).toEqual({ ok: true, error: null });
    expect((await gotrueClient.getSession()).data.session?.refresh_token).toBe(f.second.refresh_token);
    expect(f.state.calls.filter((call) => call.scope === 'local')).toHaveLength(0);
  });
});

it('keeps logout intent while SDK cleanup is pending despite new refresh demand', async () => {
  await withBridgeFixture(async (f) => {
    f.render();
    await f.signIn();
    const cleanup = deferredBridge();
    const entered = deferredBridge();
    f.state.sdkLogoutReplies.push(() => { entered.resolve(); return cleanup.promise; });
    let logout;
    await act(async () => { logout = captured.signOut(); });
    await entered.promise;
    let refresh;
    await act(async () => { refresh = captured.refreshSessionNow(); });
    cleanup.resolve(jsonResponse({}, 204));
    await act(async () => { await Promise.all([logout, refresh]); });
    expect(await logout).toEqual({ ok: true, error: null });
    expect(captured.status).toBe('signedOut');
    expect((await gotrueClient.getSession()).data.session).toBeNull();
  });
});

it.each([
  ['write', 'new-user'], ['write', 'same-token-aba'], ['write', 'signed-out'],
  ['cleanup', 'new-user'], ['cleanup', 'same-token-aba'], ['cleanup', 'signed-out'],
])('document SDK ownership survives provider remount during %s (%s)', async (phase, replacement) => {
  await withBridgeFixture(async (f) => {
    const admittedWrites = vi.spyOn(gotrueClient, 'setSession');
    const held = deferredBridge();
    const entered = deferredBridge();
    if (phase === 'write') f.state.userReplies.push(() => { entered.resolve(); return held.promise; });
    else f.state.sdkLogoutReplies.push(() => { entered.resolve(); return held.promise; });
    const oldView = f.render();
    await f.signIn();
    if (phase === 'write') await entered.promise;
    let logout;
    await act(async () => { logout = captured.signOut(); });
    if (phase === 'cleanup') await entered.promise;
    oldView.unmount();
    f.render();
    await act(async () => { await Promise.resolve(); });
    const target = replacement === 'new-user' ? f.second : f.first;
    if (replacement !== 'signed-out') await f.signIn(target);
    const writesBeforeDrain = admittedWrites.mock.calls.length;
    held.resolve(phase === 'write' ? jsonResponse(f.first.user) : jsonResponse({}, 204));
    await act(async () => { await logout; });
    expect(writesBeforeDrain).toBe(1);
    expect((await gotrueClient.getSession()).data.session?.refresh_token || null)
      .toBe(replacement === 'signed-out' ? null : target.refresh_token);
    expect(captured.status).toBe(replacement === 'signed-out' ? 'signedOut' : 'signedIn');
    expect((await logout).ok).toBe(replacement === 'signed-out');
    if (phase === 'write' && replacement !== 'signed-out') {
      expect(f.state.calls.filter((call) => call.scope === 'local')).toHaveLength(0);
    }
  });
});

it('a failed replacement bridge still cleans the document-owned prior write on logout', async () => {
  await withBridgeFixture(async (f) => {
    vi.useFakeTimers();
    const held = deferredBridge();
    const entered = deferredBridge();
    f.state.userReplies.push(() => { entered.resolve(); return held.promise; });
    const oldView = f.render();
    await f.signIn();
    await entered.promise;
    let oldLogout;
    await act(async () => { oldLogout = captured.signOut(); });
    oldView.unmount();
    f.render();
    for (let index = 0; index < 3; index += 1) {
      f.state.userReplies.push(() => jsonResponse({ message: 'Synthetic replacement bridge failure' }, 503));
    }
    await f.signIn(f.second);
    await act(async () => {
      held.resolve(jsonResponse(f.first.user));
      await vi.advanceTimersByTimeAsync(2251);
      await oldLogout;
    });
    expect((await oldLogout).ok).toBe(false);
    expect((await gotrueClient.getSession()).data.session?.refresh_token).toBe(f.first.refresh_token);
    let result;
    await act(async () => { result = await captured.signOut(); });
    expect(result).toEqual({ ok: true, error: null });
    expect((await gotrueClient.getSession()).data.session).toBeNull();
  });
});

it('a former provider server logout cannot clear a replacement provider session', async () => {
  await withBridgeFixture(async (f) => {
    const oldView = f.render();
    await f.signIn();
    const held = deferredBridge();
    f.state.apiLogoutReplies.push(() => held.promise);
    let logout;
    await act(async () => { logout = captured.signOut(); });
    oldView.unmount();
    f.render();
    await f.signIn(f.second);
    held.resolve(jsonResponse({}, 204));
    await act(async () => { await logout; });
    expect((await logout).ok).toBe(false);
    expect(captured.user.id).toBe(f.second.user.id);
    expect((await gotrueClient.getSession()).data.session?.refresh_token).toBe(f.second.refresh_token);
  });
});

it('cleanup preserves SDK state independently replaced after the provider owned a different pair', async () => {
  await withBridgeFixture(async (f) => {
    f.render();
    await f.signIn();
    await gotrueClient.setSession(f.second);
    let result;
    await act(async () => { result = await captured.signOut(); });
    expect(result).toEqual({ ok: true, error: null });
    expect((await gotrueClient.getSession()).data.session?.refresh_token).toBe(f.second.refresh_token);
  });
});
