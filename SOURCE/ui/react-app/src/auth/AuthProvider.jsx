/**
 * AuthProvider — React context for Viola Cloud GoTrue session auth.
 *
 * Responsibilities:
 *  - Hold the GoTrue session (access + refresh token) in module memory only —
 *    NEVER localStorage (SEC-017: keep tokens off disk so XSS can't scrape the
 *    refresh token). A hard reload / new tab therefore starts signed-out and
 *    the user re-authenticates; that is the accepted security tradeoff.
 *  - Auto-refresh the access token shortly before it expires, transparently.
 *  - Expose a `useAuth()`-shaped context value to the rest of the app.
 *  - Mirror the live session into `config.js` so REST + WebSocket callers can
 *    attach `Authorization: Bearer <token>` without importing React.
 *  - On the cloud surface, mirror the session into the pre-existing app-wide
 *    GoTrue SDK client (`lib/gotrue_client.ts`, consumed via `hooks/useAuth.jsx`
 *    -> `lib/auth_context.tsx`) so SmartDisplay/AccountTab/useUsage/ReviewPage —
 *    which read identity + plan/billing through THAT client, not this one —
 *    see the same signed-in session this provider just established (#3547).
 *
 * Surface awareness: on the desktop app the API-key path
 * (`window.__VIOLA_API_KEY__`) is authoritative and this provider stays in
 * `signedOut` unless a cloud session genuinely exists. The desktop key path
 * keeps working regardless — see `setCloudSession` in config.js.
 */

import {
  createContext,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import PropTypes from 'prop-types';
import {
  signUp as apiSignUp,
  signInWithPassword as apiSignIn,
  mfaStepUpForSession,
  challengeAndVerifyMfaTotp as apiChallengeAndVerifyMfaTotp,
  refresh as apiRefresh,
  hydrateDesktopSessionFromCookie as apiHydrateDesktopSession,
  signOut as apiSignOut,
  resetPassword as apiResetPassword,
  resendVerification as apiResendVerification,
} from './authClient';
import { setCloudSession } from '../config';
import { purgeLegacyLocalStorageSessions } from '../lib/sessionStore';
import { gotrueClient } from '../lib/gotrue_client';
import { isDesktopApp } from '../utils/runtimeSurface';
import { isCloudSurface } from '../components/auth/cloudSurface';

/**
 * Legacy localStorage key a prior build used to persist the full GoTrue session
 * (incl. refresh_token). SEC-017: we no longer write tokens to localStorage —
 * they live in memory only — but we still purge this key so an upgrading user's
 * stale refresh token doesn't keep sitting on disk for XSS to scrape.
 */
export const SESSION_STORAGE_KEY = 'viola-cloud-session';

/**
 * Refresh this many milliseconds before the access token's `expires_at`.
 * 60s of slack absorbs clock skew and a slow round-trip.
 */
const REFRESH_LEAD_MS = 60_000;

/**
 * Floor for the refresh timer — never schedule sooner than this even if the
 * token is already near expiry, to avoid a tight refresh loop.
 */
const MIN_REFRESH_DELAY_MS = 5_000;

/**
 * How many times to attempt the app-store bridge (`gotrueClient.setSession`)
 * before giving up on this commit. The bridge does a real `GET /auth/v1/user`
 * round trip, so a single network blip at sign-in time would otherwise leave
 * the app-wide store empty — which renders a PAYING customer as "Free Plan"
 * with no route to the billing portal (candidate C-400). One retry is enough
 * to ride out a blip without delaying sign-in perceptibly.
 */
const BRIDGE_ATTEMPTS = 3;

/** Delay between bridge attempts. */
const BRIDGE_RETRY_MS = 750;

/**
 * Retry delay after a TRANSIENT refresh failure (cloud 5xx / network drop —
 * e.g. right as a laptop wakes from sleep and the network stack is still
 * re-establishing). The desktop's DesktopSessionStore preserves the session
 * server-side on a transient failure (issue #340), so the client must retry
 * rather than sign out; 15s gives the cloud/network a moment to recover
 * without hammering it.
 */
const TRANSIENT_REFRESH_RETRY_MS = 15_000;

/**
 * True when a failed refresh attempt's error looks TRANSIENT (network
 * failure or a 5xx from the cloud/proxy) rather than a genuine rejection
 * (4xx — the refresh token itself was rejected/revoked). `status === 0` is
 * authClient's networkError() shape (fetch threw); the desktop GoTrue proxy
 * answers a transient upstream hiccup with 503 (auth/desktop_gotrue_proxy.py)
 * rather than GoTrue's normal 400 invalid_grant.
 *
 * 429 is TRANSIENT too, even though it is a 4xx: it means "ask again later",
 * not "this refresh token is dead". The refresh path is the burstiest auth
 * surface in the product (the post-checkout entitlement poll issues several
 * grants in a few seconds, #2609), and treating a rate-limit as a genuine
 * rejection would sign a paying customer out of BOTH stores over a throttle
 * they can simply wait out. A 400 `invalid_grant` still signs out, which is
 * correct — the token really is gone.
 * @param {{status?: number}|null} error
 * @returns {boolean}
 */
function isTransientRefreshError(error) {
  const status = Number(error?.status);
  return status === 0 || status === 429 || status >= 500;
}

/**
 * Shared auth context. Consumed via `useAuth()` (see ./useAuth.js).
 * @type {React.Context<import('./useAuth').AuthContextValue|null>}
 */
export const AuthContext = createContext(null);

// SEC-017: the session (access + refresh token) is held in module memory, NOT
// localStorage, so an XSS payload cannot read the refresh token off disk and
// mint a persistent takeover. Tradeoff: a hard reload / new tab starts from
// signed-out (memory is per-document) and the user re-authenticates. On load we
// purge any token a prior localStorage-persisting build left behind.
purgeLegacyLocalStorageSessions([SESSION_STORAGE_KEY]);

/** @type {object|null} In-memory session, scoped to this document's JS heap. */
let inMemorySession = null;

/**
 * Read the in-memory session.
 * @returns {object|null}
 */
function loadStoredSession() {
  const session = inMemorySession;
  if (session && typeof session.access_token === 'string' && session.access_token) {
    return session;
  }
  return null;
}

/**
 * Hold (or clear) the session in memory.
 * @param {object|null} session
 */
function storeSession(session) {
  inMemorySession = session || null;
}

/**
 * Test-only seam: seed/clear the in-memory session that hydration reads on
 * mount. Production code never imports this — tests use it instead of
 * localStorage now that tokens never touch disk (SEC-017).
 * @param {object|null} session
 */
export function __setInMemorySessionForTest(session) {
  inMemorySession = session || null;
}

/**
 * Milliseconds until a session should be refreshed.
 * @param {object} session
 * @returns {number}
 */
function refreshDelayMs(session) {
  const expiresAtMs = (Number(session?.expires_at) || 0) * 1000;
  if (!expiresAtMs) return MIN_REFRESH_DELAY_MS;
  return Math.max(MIN_REFRESH_DELAY_MS, expiresAtMs - REFRESH_LEAD_MS - Date.now());
}

/**
 * True when a session's access token is already past (or at) expiry.
 * @param {object} session
 * @returns {boolean}
 */
function isExpired(session) {
  const expiresAtMs = (Number(session?.expires_at) || 0) * 1000;
  return expiresAtMs > 0 && Date.now() >= expiresAtMs;
}

/**
 * AuthProvider component — wrap the app (or the cloud sign-in shell) in this.
 * @param {{ children: React.ReactNode }} props
 */
export function AuthProvider({ children }) {
  // 'loading' until the first hydration + (if needed) refresh resolves.
  const [status, setStatus] = useState('loading');
  const [session, setSession] = useState(null);

  // MFA step-up state. When a password sign-in yields an AAL1 session for an
  // account that has a verified TOTP factor, the second factor is still owed:
  // `mfaPending` gates the UI onto the TOTP prompt and `mfaFactorId` names the
  // factor to challenge. Crucially the half-authenticated AAL1 session is NEVER
  // committed (no `signedIn` status, no config.js mirror, no refresh timer) —
  // it is held privately in `pendingMfaSessionRef` ONLY so verifyMfaTotp can
  // present its bearer for the GoTrue challenge/verify step-up. This is the
  // fail-closed core of #2404: an enrolled account cannot reach the dashboard
  // at AAL1 without entering its code.
  const [mfaPending, setMfaPending] = useState(false);
  const [mfaFactorId, setMfaFactorId] = useState(null);
  const pendingMfaSessionRef = useRef(null);

  // Timer handle for the scheduled auto-refresh.
  const refreshTimer = useRef(null);
  // Latest refresh token, kept in a ref so the refresh callback never goes
  // stale across renders / re-schedules.
  const refreshTokenRef = useRef('');
  const mountedRef = useRef(true);
  // True once this provider has bridged a real session into gotrueClient (see
  // commitSession below). Guards the sign-out side of the bridge: only clear
  // gotrueClient's session in response to OUR OWN transition to signed-out,
  // never on an initial null commit (e.g. this provider hydrating to
  // signed-out while gotrueClient already independently holds a session from
  // somewhere else) — that would blow away a session this provider never set.
  const gotrueBridgeActiveRef = useRef(false);
  // Resolves once the most recent commit has finished mirroring into
  // gotrueClient. `refreshSessionNow` awaits it so a caller that asked this
  // provider to refresh can rely on the OTHER store already carrying the
  // rotated pair (and the fresh plan/entitlement) when the call returns.
  const bridgeSettledRef = useRef(Promise.resolve(true));
  // The refresh currently in flight, if any. This provider owns the ONE
  // rotating GoTrue refresh token on the cloud surface, and owning it is only
  // meaningful if the owner itself redeems it once at a time: two overlapping
  // `runRefresh` calls both read the same `refreshTokenRef.current` and present
  // it, so the second is a replay and GoTrue's reuse detection revokes the
  // whole family. Concurrent askers are real — `refreshSessionNow` is public,
  // and returning to the tab after Stripe checkout fires BOTH `visibilitychange`
  // and `focus` — so they share this one promise instead of each starting a
  // redemption (see `runRefresh`).
  const inFlightRefreshRef = useRef(null);

  const clearRefreshTimer = useCallback(() => {
    if (refreshTimer.current) {
      clearTimeout(refreshTimer.current);
      refreshTimer.current = null;
    }
  }, []);

  // Drop any half-authenticated (AAL1, second-factor-owed) session and clear
  // the pending TOTP prompt state.
  const clearMfaPending = useCallback(() => {
    pendingMfaSessionRef.current = null;
    setMfaPending(false);
    setMfaFactorId(null);
  }, []);

  /**
   * Mirror a live session into the app-wide GoTrue SDK client
   * (lib/gotrue_client.ts) and return whether the OTHER store genuinely holds
   * it now.
   *
   * `setSession` REPORTS failure instead of throwing (auth-js returns
   * `{ error }` when its `GET /auth/v1/user` round trip fails), so a
   * `.catch()`-only bridge cannot see a failure at all: it silently leaves the
   * app-wide store empty while believing it succeeded. That store is where
   * `subscription` lives, so a swallowed failure renders a paying customer as
   * "Free Plan" with no Manage Subscription button — candidate C-400. Inspect
   * the error and retry.
   *
   * The retry is guarded on the access token still being unexpired: auth-js's
   * `setSession` redeems the refresh token itself when the access token has
   * expired, and this provider is the sole owner of that rotating token on the
   * cloud surface (see `refreshSessionNow`). Handing an expired session to the
   * bridge would make it a second redeemer and revoke the token family.
   */
  const bridgeIntoAppStore = useCallback(async (nextSession) => {
    for (let attempt = 1; attempt <= BRIDGE_ATTEMPTS; attempt += 1) {
      if (isExpired(nextSession)) return false;
      // Never let a bridge failure escape as a rejection: this promise is
      // parked in a ref and only sometimes awaited, so a rejection would
      // surface as an unhandled one rather than as the `false` the caller
      // needs.
      const { error } = await (async () => {
        try {
          return await gotrueClient.setSession({
            access_token: nextSession.access_token,
            refresh_token: nextSession.refresh_token,
          });
        } catch (err) {
          return { error: err || new Error('setSession rejected') };
        }
      })();
      if (!error) {
        gotrueBridgeActiveRef.current = true;
        // CRITICAL: gotrueClient (lib/gotrue_client.ts) is constructed with
        // autoRefreshToken: true and starts its own background refresh ticker
        // at module load, independent of whether it ever holds a session. Once
        // setSession above gives it OUR session, that ticker would, on its own
        // schedule, try to refresh the SAME rotating refresh_token this
        // provider's runRefresh/scheduleRefresh already owns. GoTrue's
        // refresh-token rotation means only ONE consumer of a given
        // refresh_token can ever win a refresh; the other replays an
        // already-rotated token, gets invalid_grant, and (past GoTrue's
        // reuse-detection grace window) GoTrue revokes the whole token family —
        // signing the user out a few minutes into every cloud session. This is
        // the client-side sibling of the 2026-07-02 dual-refresher incident
        // serialized server-side for desktop in auth/desktop_gotrue_proxy.py's
        // _serve_desktop_refresh_grant (see its docstring for the same bug
        // class). There is no such serializing proxy on the cloud surface, so
        // the fix is to make THIS provider the sole refresher:
        // stopAutoRefresh() permanently removes gotrueClient's internal
        // visibilitychange-driven ticker for the rest of this tab's lifetime
        // (auth-js: "any managed visibility change callback will be removed").
        // runRefresh's own scheduleRefresh keeps refreshing on schedule, and
        // every successful refresh re-enters commitSession -> this same bridge,
        // re-arming gotrueClient with the newly rotated pair — so the OTHER
        // store never goes stale.
        try {
          await gotrueClient.stopAutoRefresh();
        } catch { /* ticker already stopped / not available */ }
        return true;
      }
      if (attempt < BRIDGE_ATTEMPTS) {
        await new Promise((resolve) => { setTimeout(resolve, BRIDGE_RETRY_MS); });
      }
    }
    return false;
  }, []);

  /**
   * Commit a session (or sign-out) to state, localStorage, and config.js.
   * Centralised so every auth path keeps the three stores consistent.
   */
  const commitSession = useCallback((nextSession) => {
    setSession(nextSession);
    setStatus(nextSession ? 'signedIn' : 'signedOut');
    storeSession(nextSession);
    // Mirror into config.js so non-React REST/WS callers can read the token.
    setCloudSession(nextSession);
    refreshTokenRef.current = nextSession?.refresh_token || '';
    // Bridge into the pre-existing app-wide GoTrue SDK client (lib/gotrue_client.ts,
    // consumed via hooks/useAuth.jsx -> lib/auth_context.tsx's AppAuthProvider).
    // SmartDisplay/AccountTab/useUsage/ReviewPage all read identity + plan/billing
    // through THAT client's useAuth()/usePlan(), not this one. On the desktop app
    // AppAuthProvider hydrates itself from the httpOnly session cookie, so the two
    // stay in sync without help; on the cloud SPA there is no such cookie path
    // (#2604 is desktop-only) and gotrueClient's own in-memory session never gets
    // populated on its own — without this bridge, every browser visitor who signs
    // in through CloudAuthGate reaches SmartDisplay (this provider correctly flips
    // to signedIn) while every consumer of the OTHER auth context still reports
    // signed-out / free-plan, silently breaking the account tab, plan/subscription
    // display, and paid-feature gating (#3547).
    if (isCloudSurface()) {
      if (nextSession?.access_token && nextSession?.refresh_token) {
        bridgeSettledRef.current = bridgeIntoAppStore(nextSession);
      } else if (gotrueBridgeActiveRef.current) {
        // Only clear gotrueClient's session when WE previously put one there —
        // never on the initial signed-out hydration (nothing to undo, and
        // gotrueClient may independently already hold an unrelated session).
        gotrueBridgeActiveRef.current = false;
        bridgeSettledRef.current = gotrueClient
          .signOut({ scope: 'local' })
          .then(() => false)
          .catch(() => false);
      } else {
        bridgeSettledRef.current = Promise.resolve(false);
      }
    }
  }, [bridgeIntoAppStore]);

  // Forward declaration: scheduleRefresh and runRefresh are mutually
  // recursive (a refresh schedules the next one). Use a ref to break the
  // declaration cycle without disabling exhaustive-deps app-wide.
  const scheduleRefreshRef = useRef(() => {});

  /**
   * Perform a token refresh now. On success, commits the new session and
   * re-schedules. On a GENUINE rejection (the refresh token itself was
   * rejected/revoked — a 4xx from GoTrue), signs the user out. On a
   * TRANSIENT failure (cloud 5xx / network drop), the desktop session is
   * preserved server-side (issue #340), so the current session is kept as-is
   * and the refresh is retried shortly instead of signing out — a momentary
   * blip (e.g. right as a laptop wakes from sleep) must not cost the user
   * their session.
   *
   * SINGLE-FLIGHT. A refresh token may be redeemed exactly once — GoTrue
   * rotates it on redemption and revokes the whole family when an
   * already-rotated one is replayed. So while a redemption is in flight every
   * further asker gets THAT promise rather than a second redemption of the same
   * token. Without this, two callers arriving in the same tick (the scheduled
   * timer and a `refreshSessionNow` caller, or the two listeners a single
   * post-checkout tab return fires) both read `refreshTokenRef.current` before
   * either has committed a rotation, and the loser's replay revokes the family
   * — the exact sign-out this provider's ownership of the token exists to
   * prevent. Sharing means a caller can receive a rotation that started
   * marginally before it asked; the post-checkout poll re-asks on a backoff, so
   * a just-written entitlement still lands on the next attempt.
   */
  const runRefresh = useCallback(async () => {
    if (inFlightRefreshRef.current) return inFlightRefreshRef.current;
    const attempt = (async () => {
      const token = refreshTokenRef.current;
      if (!token) {
        commitSession(null);
        return;
      }
      const result = await apiRefresh(token);
      if (!mountedRef.current) return;
      if (result.ok && result.session) {
        commitSession(result.session);
        scheduleRefreshRef.current(result.session);
      } else if (isTransientRefreshError(result.error)) {
        // Preserve the current session/status and retry shortly — do NOT
        // commitSession(null) here, that would sign the user out over a blip
        // the server itself considers recoverable.
        clearRefreshTimer();
        refreshTimer.current = setTimeout(() => {
          void runRefresh();
        }, TRANSIENT_REFRESH_RETRY_MS);
      } else {
        // Genuine rejection — the refresh token is dead (revoked/expired).
        // Drop to signed-out.
        commitSession(null);
      }
    })();
    inFlightRefreshRef.current = attempt;
    try {
      return await attempt;
    } finally {
      // Identity-checked so a slow loser can never clear a NEWER in-flight
      // refresh and re-open the double-redemption window it just closed.
      if (inFlightRefreshRef.current === attempt) inFlightRefreshRef.current = null;
    }
  }, [commitSession, clearRefreshTimer]);

  /**
   * Schedule the next auto-refresh for the given session.
   */
  const scheduleRefresh = useCallback((forSession) => {
    clearRefreshTimer();
    if (!forSession?.refresh_token) return;
    const delay = refreshDelayMs(forSession);
    refreshTimer.current = setTimeout(() => {
      void runRefresh();
    }, delay);
  }, [clearRefreshTimer, runRefresh]);

  // Keep the ref pointing at the latest scheduleRefresh.
  useEffect(() => {
    scheduleRefreshRef.current = scheduleRefresh;
  }, [scheduleRefresh]);

  /**
   * Refresh the session NOW, on behalf of another consumer, and resolve once
   * BOTH stores carry the rotated pair.
   *
   * On the cloud surface this provider is the sole owner of the rotating
   * GoTrue refresh token (see the bridge above). Any other holder that
   * redeems it independently — for instance the app-wide store's
   * `refreshUser()` pulling fresh entitlement when the tab regains focus after
   * Stripe checkout (#2609) — rotates the token out from under this provider,
   * whose next scheduled refresh then replays the stale one. GoTrue's
   * reuse detection revokes the whole family on that replay, so the customer
   * is signed out minutes after paying. Exposing this lets the app-wide store
   * ASK the owner to refresh instead of racing it, which keeps the
   * one-redeemer invariant intact while still surfacing the new plan.
   */
  const refreshSessionNow = useCallback(async () => {
    await runRefresh();
    // The commit above re-armed gotrueClient asynchronously; wait for that so
    // the caller reads the fresh plan rather than the pre-refresh one.
    await bridgeSettledRef.current;
  }, [runRefresh]);

  // Hydration: on mount, load any stored session. If it's expired, try a
  // refresh before deciding signedIn vs signedOut.
  useEffect(() => {
    mountedRef.current = true;
    let cancelled = false;

    // Commit a hydrated session — UNLESS it still owes a TOTP second factor, in
    // which case fail closed: hold it as pending and stay signed-out until the
    // code is entered, exactly as a fresh sign-in would (#2404).
    const applyHydratedSession = (hydrated) => {
      refreshTokenRef.current = hydrated?.refresh_token || '';
      const stepUp = mfaStepUpForSession(hydrated);
      if (stepUp.required && stepUp.factorId) {
        pendingMfaSessionRef.current = hydrated;
        setMfaPending(true);
        setMfaFactorId(stepUp.factorId);
        commitSession(null);
        return;
      }
      commitSession(hydrated);
      scheduleRefresh(hydrated);
    };

    const stored = loadStoredSession();
    if (!stored) {
      // #2604 — persistent desktop sign-in. No in-memory session (SEC-017: the
      // webview never persists tokens to disk), so a fresh desktop launch would
      // otherwise start signed-out. On the desktop app the session survives
      // server-side (encrypted at rest) behind the opaque httpOnly viola_session
      // cookie that outlives the relaunch; hydrate from it before deciding
      // signed-out. On the cloud surface (or when no cookie resolves) this stays
      // signed-out exactly as before — config.js still has the desktop key path.
      if (isDesktopApp()) {
        apiHydrateDesktopSession().then((result) => {
          if (cancelled) return;
          if (result.ok && result.session) {
            applyHydratedSession(result.session);
          } else {
            commitSession(null);
          }
        });
      } else {
        commitSession(null);
      }
      return () => {
        cancelled = true;
        mountedRef.current = false;
        clearRefreshTimer();
      };
    }

    refreshTokenRef.current = stored.refresh_token || '';

    if (isExpired(stored)) {
      // Stored session is stale — refresh before exposing it.
      apiRefresh(stored.refresh_token).then((result) => {
        if (cancelled) return;
        if (result.ok && result.session) {
          applyHydratedSession(result.session);
        } else {
          commitSession(null);
        }
      });
    } else {
      applyHydratedSession(stored);
    }

    return () => {
      cancelled = true;
      mountedRef.current = false;
      clearRefreshTimer();
    };
    // commitSession / scheduleRefresh / clearRefreshTimer are stable.
  }, [commitSession, scheduleRefresh, clearRefreshTimer]);

  // ---- Auth actions exposed through context -----------------------------

  const signIn = useCallback(async (email, password) => {
    const result = await apiSignIn(email, password);
    if (result.ok && result.session) {
      const stepUp = mfaStepUpForSession(result.session);
      if (stepUp.required && stepUp.factorId) {
        // MFA-enrolled account: the password satisfied AAL1 but a verified TOTP
        // second factor is still owed. Do NOT commit this half-authenticated
        // session — hold it privately (bearer only, for the step-up) and
        // surface the pending state so the UI prompts for the code. Returning
        // ok:false keeps the caller out of the "signed in" branch; mfaRequired
        // tells it to show the TOTP prompt rather than a sign-in error.
        pendingMfaSessionRef.current = result.session;
        setMfaPending(true);
        setMfaFactorId(stepUp.factorId);
        return { ok: false, mfaRequired: true, factorId: stepUp.factorId, error: null };
      }
      commitSession(result.session);
      scheduleRefresh(result.session);
      return { ok: true, error: null };
    }
    return { ok: false, error: result.error };
  }, [commitSession, scheduleRefresh]);

  // Complete the TOTP second factor for the pending AAL1 sign-in. Runs GoTrue's
  // challenge -> verify step-up with the held bearer; on success GoTrue mints a
  // NEW, fully-authenticated AAL2 session, which becomes the real committed
  // session. A wrong/expired code returns an error and leaves the prompt up.
  const verifyMfaTotp = useCallback(async (code) => {
    const pending = pendingMfaSessionRef.current;
    const factorId = mfaFactorId;
    if (!pending?.access_token || !factorId) {
      return {
        ok: false,
        error: {
          message: 'Your sign-in expired. Please sign in again.',
          code: 'mfa_no_pending_session',
          status: 0,
          retryAfter: null,
        },
      };
    }
    const result = await apiChallengeAndVerifyMfaTotp(pending.access_token, factorId, code);
    if (!mountedRef.current) return { ok: result.ok, error: result.error || null };
    if (result.ok && result.session) {
      clearMfaPending();
      commitSession(result.session);
      scheduleRefresh(result.session);
      return { ok: true, error: null };
    }
    return { ok: false, error: result.error };
  }, [mfaFactorId, clearMfaPending, commitSession, scheduleRefresh]);

  // Back out of the TOTP prompt: revoke the half-authenticated AAL1 session
  // server-side (the password grant minted a real session) and clear pending
  // state so the login form returns.
  const cancelMfa = useCallback(async () => {
    const pending = pendingMfaSessionRef.current;
    clearMfaPending();
    if (pending?.access_token) {
      await apiSignOut(pending.access_token);
    }
    return { ok: true, error: null };
  }, [clearMfaPending]);

  const signUp = useCallback(async (email, password, consents = {}) => {
    const result = await apiSignUp(email, password, consents);
    if (!result.ok) {
      return { ok: false, needsEmailVerification: false, error: result.error };
    }
    // GoTrue may (rarely, when email confirmation is disabled) return a
    // session directly; honour it. Normally needsEmailVerification is true.
    if (result.session) {
      commitSession(result.session);
      scheduleRefresh(result.session);
    }
    return {
      ok: true,
      needsEmailVerification: result.needsEmailVerification,
      error: null,
    };
  }, [commitSession, scheduleRefresh]);

  const signOut = useCallback(async () => {
    clearRefreshTimer();
    clearMfaPending();
    const token = session?.access_token || '';
    const result = await apiSignOut(token);
    // Always drop local state, even if the server call failed — never trap
    // the user in a signed-in shell.
    commitSession(null);
    return { ok: result.ok, error: result.ok ? null : result.error };
  }, [session, commitSession, clearRefreshTimer, clearMfaPending]);

  const resetPassword = useCallback(async (email) => {
    const result = await apiResetPassword(email);
    return { ok: result.ok, error: result.ok ? null : result.error };
  }, []);

  const resendVerification = useCallback(async (email) => {
    const result = await apiResendVerification(email);
    return { ok: result.ok, error: result.ok ? null : result.error };
  }, []);

  const value = useMemo(() => ({
    status,
    user: session?.user || null,
    session,
    mfaPending,
    mfaFactorId,
    signIn,
    verifyMfaTotp,
    cancelMfa,
    signUp,
    signOut,
    resetPassword,
    resendVerification,
    // Published ONLY on the cloud surface, where this provider owns the single
    // rotating refresh token. Its presence is what tells the app-wide store to
    // delegate rather than redeem the token itself; on the desktop app it is
    // absent, so that store keeps its own refresh path (serialized server-side
    // by auth/desktop_gotrue_proxy.py) exactly as before.
    refreshSessionNow: isCloudSurface() ? refreshSessionNow : null,
  }), [
    status, session, mfaPending, mfaFactorId,
    signIn, verifyMfaTotp, cancelMfa, signUp, signOut, resetPassword, resendVerification,
    refreshSessionNow,
  ]);

  return (
    <AuthContext.Provider value={value}>
      {children}
    </AuthContext.Provider>
  );
}

AuthProvider.propTypes = {
  children: PropTypes.node,
};

export default AuthProvider;
