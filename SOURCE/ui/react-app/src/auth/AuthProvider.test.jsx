import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, act, waitFor } from '@testing-library/react';
import { AuthProvider, SESSION_STORAGE_KEY, __setInMemorySessionForTest } from './AuthProvider';
import { useAuth } from './useAuth';

// Mock the network layer; the provider's job is state + persistence + refresh.
// The PURE AAL/MFA helpers (mfaStepUpForSession, decodeJwtPayload,
// verifiedTotpFactor) are kept REAL via importActual so the provider's
// second-factor gating is exercised end to end, not stubbed out.
vi.mock('./authClient', async () => {
  const actual = await vi.importActual('./authClient');
  return {
    ...actual,
    signUp: vi.fn(),
    signInWithPassword: vi.fn(),
    challengeAndVerifyMfaTotp: vi.fn(),
    refresh: vi.fn(),
    signOut: vi.fn(),
    resetPassword: vi.fn(),
    resendVerification: vi.fn(),
  };
});

// #3547 — the pre-existing app-wide GoTrue SDK client that SmartDisplay /
// AccountTab / useUsage / ReviewPage read identity + plan/billing through
// (hooks/useAuth.jsx -> lib/auth_context.tsx). Mocked so the bridge tests
// below can assert it receives the session without a real network client.
vi.mock('../lib/gotrue_client', () => ({
  gotrueClient: {
    setSession: vi.fn().mockResolvedValue({ data: { session: null, user: null }, error: null }),
    signOut: vi.fn().mockResolvedValue({ error: null }),
    // The bridge stops this client's own auto-refresh ticker so exactly one
    // refresher owns the rotating token; the mock has to carry it too.
    stopAutoRefresh: vi.fn().mockResolvedValue(undefined),
  },
}));

import * as authClient from './authClient';
import { gotrueClient } from '../lib/gotrue_client';
import { getCloudAccessToken, getCloudSession } from '../config';

/** Build a session whose token expires `secondsFromNow` from now. */
function makeSession(overrides = {}) {
  return {
    access_token: 'access-jwt',
    refresh_token: 'refresh-jwt',
    token_type: 'bearer',
    expires_in: 3600,
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    user: { id: 'user-1', email: 'a@b.com' },
    ...overrides,
  };
}

/** Test harness — surfaces the useAuth() value as DOM + a captured ref. */
let captured = null;
function Probe() {
  const auth = useAuth();
  captured = auth;
  return (
    <div>
      <span data-testid="status">{auth.status}</span>
      <span data-testid="email">{auth.user?.email || 'none'}</span>
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
  // SEC-017: the provider holds the session in memory, not localStorage. Reset
  // that in-memory store between tests; hydration tests seed it via the seam.
  __setInMemorySessionForTest(null);
  vi.clearAllMocks();
  vi.useRealTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('AuthProvider hydration', () => {
  it('starts signedOut when no stored session exists', async () => {
    renderProvider();
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('signedOut'));
    expect(captured.session).toBeNull();
    expect(captured.user).toBeNull();
  });

  it('hydrates a valid in-memory session to signedIn without a network call', async () => {
    const stored = makeSession();
    __setInMemorySessionForTest(stored);

    renderProvider();
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('signedIn'));
    expect(screen.getByTestId('email').textContent).toBe('a@b.com');
    expect(authClient.refresh).not.toHaveBeenCalled();
  });

  it('refreshes an expired stored session before exposing it', async () => {
    const expired = makeSession({ expires_at: Math.floor(Date.now() / 1000) - 10 });
    __setInMemorySessionForTest(expired);
    const fresh = makeSession({ access_token: 'fresh-jwt' });
    authClient.refresh.mockResolvedValue({ ok: true, session: fresh, error: null });

    renderProvider();
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('signedIn'));
    expect(authClient.refresh).toHaveBeenCalledWith('refresh-jwt');
    expect(captured.session.access_token).toBe('fresh-jwt');
  });

  it('drops to signedOut when refreshing an expired stored session fails', async () => {
    const expired = makeSession({ expires_at: Math.floor(Date.now() / 1000) - 10 });
    __setInMemorySessionForTest(expired);
    authClient.refresh.mockResolvedValue({
      ok: false,
      session: null,
      error: { message: 'expired', code: 'invalid_token_response', status: 401, retryAfter: null },
    });

    renderProvider();
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('signedOut'));
    // SEC-017: tokens never get written to localStorage.
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
  });
});

describe('AuthProvider sign-in / sign-up / sign-out', () => {
  it('signIn keeps the session out of localStorage and mirrors it into config.js', async () => {
    const session = makeSession();
    authClient.signInWithPassword.mockResolvedValue({ ok: true, session, error: null });

    renderProvider();
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('signedOut'));

    let result;
    await act(async () => {
      result = await captured.signIn('a@b.com', 'pw');
    });

    expect(result).toEqual({ ok: true, error: null });
    expect(screen.getByTestId('status').textContent).toBe('signedIn');
    // SEC-017: the token (incl. refresh_token) is NEVER written to localStorage.
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
    // Mirrored into config.js for the REST/WS layers.
    expect(getCloudAccessToken()).toBe('access-jwt');
    expect(getCloudSession().refresh_token).toBe('refresh-jwt');
    // #3547 — also mirrored into the OTHER GoTrue client (SmartDisplay /
    // AccountTab / useUsage / ReviewPage's identity + plan/billing source),
    // or those consumers silently keep reporting signed-out / free-plan for a
    // real signed-in cloud visitor.
    expect(gotrueClient.setSession).toHaveBeenCalledWith({
      access_token: 'access-jwt',
      refresh_token: 'refresh-jwt',
    });
  });

  it('signIn surfaces the error and stays signedOut on failure', async () => {
    authClient.signInWithPassword.mockResolvedValue({
      ok: false,
      session: null,
      error: { message: 'bad creds', code: 'invalid_credentials', status: 400, retryAfter: null },
    });

    renderProvider();
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('signedOut'));

    let result;
    await act(async () => {
      result = await captured.signIn('a@b.com', 'wrong');
    });

    expect(result.ok).toBe(false);
    expect(result.error.code).toBe('invalid_credentials');
    expect(screen.getByTestId('status').textContent).toBe('signedOut');
  });

  it('signUp reports needsEmailVerification and stays signedOut', async () => {
    authClient.signUp.mockResolvedValue({
      ok: true,
      session: null,
      needsEmailVerification: true,
      error: null,
    });

    renderProvider();
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('signedOut'));

    let result;
    await act(async () => {
      result = await captured.signUp('new@b.com', 'pw123456', {
        tosAccepted: true,
        legalEligibilityConfirmed: true,
      });
    });

    expect(result).toEqual({ ok: true, needsEmailVerification: true, error: null });
    expect(authClient.signUp).toHaveBeenCalledWith('new@b.com', 'pw123456', {
      tosAccepted: true,
      legalEligibilityConfirmed: true,
    });
    expect(screen.getByTestId('status').textContent).toBe('signedOut');
  });

  it('signOut clears state, the in-memory session, and the config.js mirror', async () => {
    const session = makeSession();
    __setInMemorySessionForTest(session);
    authClient.signOut.mockResolvedValue({ ok: true, error: null });

    renderProvider();
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('signedIn'));

    let result;
    await act(async () => {
      result = await captured.signOut();
    });

    expect(result.ok).toBe(true);
    expect(authClient.signOut).toHaveBeenCalledWith('access-jwt');
    expect(screen.getByTestId('status').textContent).toBe('signedOut');
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
    expect(getCloudAccessToken()).toBe('');
    // #3547 — the other GoTrue client's session is cleared too, so its
    // consumers (AccountTab/useUsage/etc.) drop out of a stale signed-in view.
    expect(gotrueClient.signOut).toHaveBeenCalledWith({ scope: 'local' });
  });

  it('resetPassword and resendVerification pass through the client result', async () => {
    authClient.resetPassword.mockResolvedValue({ ok: true, error: null });
    authClient.resendVerification.mockResolvedValue({ ok: true, error: null });

    renderProvider();
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('signedOut'));

    let reset;
    let resend;
    await act(async () => {
      reset = await captured.resetPassword('a@b.com');
      resend = await captured.resendVerification('a@b.com');
    });

    expect(reset).toEqual({ ok: true, error: null });
    expect(resend).toEqual({ ok: true, error: null });
    expect(authClient.resetPassword).toHaveBeenCalledWith('a@b.com');
    expect(authClient.resendVerification).toHaveBeenCalledWith('a@b.com');
  });
});

describe('AuthProvider auto-refresh', () => {
  it('refreshes the access token shortly before it expires', async () => {
    vi.useFakeTimers();
    // Session expires in 65s: refresh lead is 60s, so timer fires in ~5s.
    const nearExpiry = makeSession({
      expires_at: Math.floor(Date.now() / 1000) + 65,
    });
    __setInMemorySessionForTest(nearExpiry);
    const refreshed = makeSession({ access_token: 'refreshed-jwt' });
    authClient.refresh.mockResolvedValue({ ok: true, session: refreshed, error: null });

    renderProvider();
    // Hydration is synchronous for a non-expired stored session.
    await act(async () => { await Promise.resolve(); });
    expect(captured.status).toBe('signedIn');
    expect(captured.session.access_token).toBe('access-jwt');

    // Advance past the scheduled refresh.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });

    expect(authClient.refresh).toHaveBeenCalledWith('refresh-jwt');
    expect(captured.session.access_token).toBe('refreshed-jwt');
    expect(getCloudAccessToken()).toBe('refreshed-jwt');
  });

  it('issue #340: a TRANSIENT refresh failure (cloud 5xx) keeps the user signed in and retries', async () => {
    vi.useFakeTimers();
    const nearExpiry = makeSession({
      expires_at: Math.floor(Date.now() / 1000) + 65,
    });
    __setInMemorySessionForTest(nearExpiry);
    // First attempt: the desktop proxy answers 503 (auth/desktop_gotrue_proxy.py
    // on a transient upstream hiccup) — must NOT sign the user out.
    authClient.refresh.mockResolvedValueOnce({
      ok: false,
      session: null,
      error: { message: 'unavailable', code: 'auth_service_unavailable', status: 503, retryAfter: null },
    });
    const refreshed = makeSession({ access_token: 'recovered-jwt' });
    authClient.refresh.mockResolvedValueOnce({ ok: true, session: refreshed, error: null });

    renderProvider();
    await act(async () => { await Promise.resolve(); });
    expect(captured.status).toBe('signedIn');

    // Advance past the scheduled refresh — it fails transiently.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(authClient.refresh).toHaveBeenCalledTimes(1);
    // The blip must NOT sign the user out or drop the current session.
    expect(captured.status).toBe('signedIn');
    expect(captured.session.access_token).toBe('access-jwt');

    // Advance past the transient retry delay — it now succeeds.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(authClient.refresh).toHaveBeenCalledTimes(2);
    expect(captured.status).toBe('signedIn');
    expect(captured.session.access_token).toBe('recovered-jwt');
    expect(getCloudAccessToken()).toBe('recovered-jwt');
  });

  it('a network-transport refresh failure (status 0) also retries instead of signing out', async () => {
    vi.useFakeTimers();
    const nearExpiry = makeSession({
      expires_at: Math.floor(Date.now() / 1000) + 65,
    });
    __setInMemorySessionForTest(nearExpiry);
    authClient.refresh.mockResolvedValueOnce({
      ok: false,
      session: null,
      error: { message: 'Connection failed.', code: 'network_error', status: 0, retryAfter: null },
    });
    const refreshed = makeSession({ access_token: 'recovered-jwt-2' });
    authClient.refresh.mockResolvedValueOnce({ ok: true, session: refreshed, error: null });

    renderProvider();
    await act(async () => { await Promise.resolve(); });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(captured.status).toBe('signedIn');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(captured.session.access_token).toBe('recovered-jwt-2');
  });

  it('a 429 on the refresh path retries instead of signing the paying customer out', async () => {
    // The refresh path is the burstiest auth surface in the product: the
    // post-checkout entitlement poll (#2609) issues several grants within
    // seconds of a customer paying. A rate-limit means "ask again later", not
    // "this token is dead", so treating it like a 400 invalid_grant would sign
    // that customer out of BOTH stores over a throttle they could wait out.
    vi.useFakeTimers();
    __setInMemorySessionForTest(makeSession({
      expires_at: Math.floor(Date.now() / 1000) + 65,
    }));
    authClient.refresh.mockResolvedValueOnce({
      ok: false,
      session: null,
      error: { message: 'Too many requests.', code: 'over_request_rate_limit', status: 429, retryAfter: 5 },
    });
    authClient.refresh.mockResolvedValueOnce({
      ok: true,
      session: makeSession({ access_token: 'after-429-jwt' }),
      error: null,
    });

    renderProvider();
    await act(async () => { await Promise.resolve(); });

    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(authClient.refresh).toHaveBeenCalledTimes(1);
    expect(captured.status).toBe('signedIn');
    expect(captured.session.access_token).toBe('access-jwt');

    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(captured.status).toBe('signedIn');
    expect(captured.session.access_token).toBe('after-429-jwt');
  });

  it('concurrent refresh askers share ONE redemption of the rotating token', async () => {
    // The provider owns the single rotating refresh token on the cloud
    // surface, and `refreshSessionNow` hands that ownership to other callers.
    // Two askers in the same tick both read the same `refreshTokenRef.current`,
    // so without a single-flight guard the loser replays an already-rotated
    // token and GoTrue's reuse detection revokes the whole family.
    __setInMemorySessionForTest(makeSession());
    let release = null;
    authClient.refresh.mockImplementationOnce(
      () => new Promise((resolve) => { release = resolve; }),
    );

    renderProvider();
    await act(async () => { await Promise.resolve(); });

    await act(async () => {
      const both = Promise.all([captured.refreshSessionNow(), captured.refreshSessionNow()]);
      await Promise.resolve();
      release({ ok: true, session: makeSession({ access_token: 'shared-jwt' }), error: null });
      await both;
    });

    expect(authClient.refresh).toHaveBeenCalledTimes(1);
    expect(captured.session.access_token).toBe('shared-jwt');
  });
});

describe('useAuth guard', () => {
  it('throws when used outside an AuthProvider', () => {
    const Bare = () => {
      useAuth();
      return null;
    };
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    expect(() => render(<Bare />)).toThrow(/within an <AuthProvider>/);
    spy.mockRestore();
  });
});

// ===========================================================================
// TOTP second factor (#2404): a password sign-in for a TOTP-enrolled account
// only reaches AAL1 and MUST NOT be treated as signed in. The provider holds
// the half-authenticated session privately, surfaces mfaPending, and only
// commits the fully-authenticated AAL2 session after verifyMfaTotp succeeds.
// ===========================================================================

/** Unsigned JWT carrying the given payload (only the payload is read). */
function makeJwt(payload) {
  const b64url = (obj) => btoa(JSON.stringify(obj))
    .replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
  return `${b64url({ alg: 'HS256', typ: 'JWT' })}.${b64url(payload)}.sig`;
}

const VERIFIED_TOTP = { id: 'factor-1', factor_type: 'totp', status: 'verified' };

/** An AAL1 password session for an account with a verified TOTP factor. */
function makeAal1EnrolledSession() {
  return {
    access_token: makeJwt({ aal: 'aal1', sub: 'user-1' }),
    refresh_token: 'aal1-refresh',
    token_type: 'bearer',
    expires_in: 3600,
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    user: { id: 'user-1', email: 'mfa@b.com', factors: [VERIFIED_TOTP] },
  };
}

/** The fully-authenticated AAL2 session GoTrue returns after verify. */
function makeAal2Session() {
  return {
    access_token: makeJwt({ aal: 'aal2', sub: 'user-1' }),
    refresh_token: 'aal2-refresh',
    token_type: 'bearer',
    expires_in: 3600,
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    user: { id: 'user-1', email: 'mfa@b.com', factors: [VERIFIED_TOTP] },
  };
}

describe('AuthProvider TOTP second factor (#2404)', () => {
  it('does NOT sign in an enrolled account at AAL1 — surfaces mfaPending, no config mirror', async () => {
    authClient.signInWithPassword.mockResolvedValue({
      ok: true, session: makeAal1EnrolledSession(), error: null,
    });

    renderProvider();
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('signedOut'));

    let result;
    await act(async () => {
      result = await captured.signIn('mfa@b.com', 'pw');
    });

    expect(result.ok).toBe(false);
    expect(result.mfaRequired).toBe(true);
    expect(result.factorId).toBe('factor-1');
    // The half-authenticated session is NOT signed in.
    expect(screen.getByTestId('status').textContent).not.toBe('signedIn');
    expect(captured.mfaPending).toBe(true);
    expect(captured.mfaFactorId).toBe('factor-1');
    // The AAL1 bearer never leaks into config.js (REST/WS callers).
    expect(getCloudAccessToken()).toBe('');
    expect(getCloudSession()).toBeNull();
    // And nothing on disk, per SEC-017.
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
  });

  it('verifyMfaTotp completes the step-up and commits the AAL2 session as signed in', async () => {
    authClient.signInWithPassword.mockResolvedValue({
      ok: true, session: makeAal1EnrolledSession(), error: null,
    });
    authClient.challengeAndVerifyMfaTotp.mockResolvedValue({
      ok: true, session: makeAal2Session(), error: null,
    });

    renderProvider();
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('signedOut'));
    await act(async () => { await captured.signIn('mfa@b.com', 'pw'); });
    expect(captured.mfaPending).toBe(true);

    let verifyResult;
    await act(async () => {
      verifyResult = await captured.verifyMfaTotp('123456');
    });

    // The step-up ran with the held AAL1 bearer + the entered code.
    expect(authClient.challengeAndVerifyMfaTotp).toHaveBeenCalledWith(
      expect.stringContaining('.'), 'factor-1', '123456',
    );
    expect(verifyResult.ok).toBe(true);
    expect(screen.getByTestId('status').textContent).toBe('signedIn');
    expect(captured.mfaPending).toBe(false);
    // NOW the fully-authenticated AAL2 session is mirrored for REST/WS.
    expect(getCloudAccessToken()).toBe(makeAal2Session().access_token);
  });

  it('verifyMfaTotp with a wrong code keeps the session out (still not signed in)', async () => {
    authClient.signInWithPassword.mockResolvedValue({
      ok: true, session: makeAal1EnrolledSession(), error: null,
    });
    authClient.challengeAndVerifyMfaTotp.mockResolvedValue({
      ok: false, session: null,
      error: { message: 'That code is not valid.', code: 'mfa_verification_failed', status: 400, retryAfter: null },
    });

    renderProvider();
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('signedOut'));
    await act(async () => { await captured.signIn('mfa@b.com', 'pw'); });

    let verifyResult;
    await act(async () => {
      verifyResult = await captured.verifyMfaTotp('000000');
    });

    expect(verifyResult.ok).toBe(false);
    expect(verifyResult.error.code).toBe('mfa_verification_failed');
    // Still pending, still not signed in, still no config leak.
    expect(captured.mfaPending).toBe(true);
    expect(screen.getByTestId('status').textContent).not.toBe('signedIn');
    expect(getCloudAccessToken()).toBe('');
  });

  it('cancelMfa revokes the half-session and clears pending', async () => {
    authClient.signInWithPassword.mockResolvedValue({
      ok: true, session: makeAal1EnrolledSession(), error: null,
    });
    authClient.signOut.mockResolvedValue({ ok: true, error: null });

    renderProvider();
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('signedOut'));
    await act(async () => { await captured.signIn('mfa@b.com', 'pw'); });
    expect(captured.mfaPending).toBe(true);

    await act(async () => { await captured.cancelMfa(); });

    // The AAL1 session was revoked server-side and pending state cleared.
    expect(authClient.signOut).toHaveBeenCalledWith(expect.stringContaining('.'));
    expect(captured.mfaPending).toBe(false);
    expect(captured.mfaFactorId).toBeNull();
    expect(screen.getByTestId('status').textContent).toBe('signedOut');
  });

  it('a non-enrolled account still signs in immediately (unchanged behavior)', async () => {
    // makeSession() has a plain user with no factors -> no step-up.
    authClient.signInWithPassword.mockResolvedValue({ ok: true, session: makeSession(), error: null });

    renderProvider();
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('signedOut'));

    let result;
    await act(async () => { result = await captured.signIn('a@b.com', 'pw'); });

    expect(result).toEqual({ ok: true, error: null });
    expect(screen.getByTestId('status').textContent).toBe('signedIn');
    expect(captured.mfaPending).toBe(false);
    expect(getCloudAccessToken()).toBe('access-jwt');
  });
});
