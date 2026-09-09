/**
 * authClient — raw-fetch GoTrue (Supabase Auth) session client for Viola Cloud.
 *
 * Viola Cloud's browser SPA authenticates against the cloud backend's GoTrue
 * proxy, which is served same-origin as the SPA under `/auth/v1/*`. This module
 * is a thin, dependency-free wrapper over those endpoints (verified against
 * `auth/gotrue_proxy.py`):
 *
 *   POST /auth/v1/signup                       — {email, password, data} -> 200
 *   POST /auth/v1/token?grant_type=password    — {email, password} -> tokens
 *   POST /auth/v1/token?grant_type=refresh_token — {refresh_token} -> tokens
 *   POST /auth/v1/logout                       — Bearer <token>    -> 204
 *   POST /auth/v1/recover                      — {email}           -> 200
 *   POST /auth/v1/resend                       — {email, type}     -> 200
 *
 * Important proxy behaviours baked into this client:
 *  - `/signup`, `/recover`, and `/resend` are enumeration-safe: the proxy
 *    reshapes them to a generic 200 `{message}` even on duplicate/error/
 *    nonexistent-account (#2153 closed the gap where `/resend` alone still
 *    leaked GoTrue's real per-account status). A signup that returns no
 *    `access_token` therefore means "email verification required" rather
 *    than failure — callers infer `needsEmailVerification` from that; a
 *    `resendVerification()` call always resolves `{ ok: true }` on any
 *    reachable-service outcome, never revealing whether the email had an
 *    account.
 *  - 5xx upstream errors are sanitised to 502 `{code, message}` by the proxy.
 *  - 429 carries a `Retry-After` header.
 *
 * The desktop app uses a separate API-key path (`window.__VIOLA_API_KEY__`);
 * this client is only consulted when running as a cloud/LAN web client.
 */

import { withCsrfHeader } from '../lib/csrf';
import { ACCEPTED_PRIVACY_VERSION, ACCEPTED_TERMS_VERSION } from './legalVersions';

/** Default GoTrue base path - same-origin proxy mount. */
export const GOTRUE_BASE = '/auth/v1';

/**
 * Resolve the origin the GoTrue proxy is served from. Desktop injects
 * `window.__VIOLA_BASE_URL__`; the cloud SPA is served same-origin.
 * @returns {string}
 */
function resolveOrigin() {
  if (typeof window === 'undefined') return '';
  return window.__VIOLA_BASE_URL__ || window.location.origin || '';
}

/**
 * Build a fully-qualified GoTrue endpoint URL.
 * @param {string} path - path relative to `/auth/v1` (e.g. '/token').
 * @param {Record<string, string>} [query] - query parameters.
 * @returns {string}
 */
function gotrueUrl(path, query) {
  const base = `${resolveOrigin()}${GOTRUE_BASE}${path}`;
  if (!query) return base;
  const qs = new URLSearchParams(query).toString();
  return qs ? `${base}?${qs}` : base;
}

/**
 * Normalised error returned by every authClient call. `message` is safe to
 * surface to users; `code` and `status` support programmatic handling.
 * @typedef {{ message: string, code: string, status: number, retryAfter: number|null }} AuthClientError
 */

/**
 * Turn a GoTrue / proxy error body into a user-safe message.
 * GoTrue errors arrive as `{error, error_description}`, `{msg, error_code}`,
 * or `{message, code}` depending on which layer produced them.
 * @param {object} body
 * @param {number} status
 * @returns {string}
 */
function friendlyMessage(body, status) {
  if (isCsrfFailure(body, status)) {
    // Deliberately NOT auto-retried. A silent retry would re-hide exactly the
    // class the spa-auth-csrf-double-submit gate now guards, and the header is
    // attached on every attempt anyway, so a retry that would succeed is a
    // retry that never needed to happen. Tell the user the one thing that
    // actually clears it instead.
    return 'Your browser session is out of date. Refresh the page and try again.';
  }
  return _friendlyMessageBody(body, status);
}

/**
 * True when a response is the cloud CSRF layer's refusal (`auth/csrf.py`).
 *
 * Two distinct shapes reach here: the middleware answers a bare
 * `{"detail": "CSRF token validation failed"}`, while the `csrf_required`
 * route dependency answers `{"detail": {"error": "csrf_mismatch", ...}}`.
 * Neither uses any of the keys `friendlyMessage` reads, which is precisely why
 * this failure spent its whole life collapsed into the generic
 * "Something went wrong" (#362 / #3547) and stayed invisible in prod.
 *
 * @param {object} body
 * @param {number} status
 * @returns {boolean}
 */
function isCsrfFailure(body, status) {
  if (status !== 403 || !body) return false;
  const detail = body.detail;
  if (typeof detail === 'string') return detail.toLowerCase().includes('csrf');
  if (detail && typeof detail === 'object') {
    return String(detail.error || '').toLowerCase().includes('csrf');
  }
  return false;
}

/**
 * The pre-existing GoTrue/proxy message mapping, unchanged.
 * @param {object} body
 * @param {number} status
 * @returns {string}
 */
function _friendlyMessageBody(body, status) {
  const raw = (
    body?.error_description
    || body?.msg
    || body?.message
    || body?.error
    || ''
  ).toString();
  const code = (body?.error_code || body?.code || '').toString().toLowerCase();
  const lower = raw.toLowerCase();

  if (status === 429 || code === 'rate_limited' || code === 'over_request_rate_limit') {
    return 'Too many attempts. Please wait a moment before trying again.';
  }
  if (code === 'invalid_credentials' || lower.includes('invalid login credentials')) {
    return "We couldn't sign you in. Check your email and password.";
  }
  if (code === 'email_not_confirmed' || lower.includes('email not confirmed')) {
    return 'Verify your email address before signing in.';
  }
  if (
    code === 'weak_password'
    || code === 'invalid_password'
    || (lower.includes('password') && lower.length < 160)
  ) {
    return raw || 'Choose a stronger password.';
  }
  if (code === 'user_already_exists' || lower.includes('already registered')) {
    return 'That email is already registered. Try signing in instead.';
  }
  if (
    code === 'mfa_verification_failed'
    || code === 'invalid_code'
    || lower.includes('invalid totp')
    || lower.includes('invalid mfa')
  ) {
    return 'That code is not valid. Check your authenticator app and try again.';
  }
  if (status >= 500 || code === 'auth_service_error' || code === 'auth_service_unavailable') {
    return 'The account service is temporarily unavailable. Try again shortly.';
  }
  // GoTrue messages are short and user-facing; pass through when sane.
  if (raw && raw.length < 200 && !raw.includes('\n')) return raw;
  return 'Something went wrong. Please try again.';
}

/**
 * Build a normalised error object from an HTTP response + parsed body.
 * @param {Response} response
 * @param {object} body
 * @returns {AuthClientError}
 */
function errorFromResponse(response, body) {
  const retryHeader = response.headers?.get?.('Retry-After');
  const retryAfter = retryHeader ? Number(retryHeader) || null : null;
  const code = isCsrfFailure(body, response.status)
    ? 'csrf_token_invalid'
    : (body?.error_code || body?.code || body?.error || 'auth_error').toString();
  return {
    message: friendlyMessage(body, response.status),
    code,
    status: response.status,
    retryAfter,
  };
}

/**
 * Error object for transport-level (network) failures.
 * @returns {AuthClientError}
 */
function networkError() {
  return {
    message: 'Connection failed. Check your internet connection and try again.',
    code: 'network_error',
    status: 0,
    retryAfter: null,
  };
}

/**
 * POST JSON to a GoTrue endpoint.
 *
 * Returns `{ ok, status, body, error }`. `ok` is true for 2xx responses.
 * Never throws — transport failures resolve to `{ ok:false, error }`.
 *
 * @param {string} path - path relative to `/auth/v1`.
 * @param {object} options
 * @param {object} [options.body] - JSON request body.
 * @param {Record<string,string>} [options.query] - query parameters.
 * @param {string} [options.token] - bearer access token for the Authorization header.
 * @returns {Promise<{ ok: boolean, status: number, body: object, error: AuthClientError|null }>}
 */
async function requestGoTrue(method, path, { body, query, token } = {}) {
  let headers = { 'Content-Type': 'application/json' };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  const url = gotrueUrl(path, query);
  // Double-submit CSRF token (#362 / #3547). These requests ride
  // `credentials: 'same-origin'`, so once the browser holds a `viola_session`
  // cookie the cloud CSRF middleware (auth/csrf.py) enforces the
  // cookie/header pairing on every one of them. Without this the RETURNING
  // visitor's sign-in and the reload-time `grant_type=refresh_token`
  // hydration both hard-fail 403 with no in-page recovery — the first-ever
  // visit works only because no session cookie exists yet to arm the check.
  // Attached on every method (not just when `token` is absent) so it stays
  // correct if the server ever stops treating a Bearer as a CSRF exemption;
  // `withCsrfHeader` is a no-op when the request is cross-origin or the
  // cookie is unreadable.
  headers = withCsrfHeader(headers, url);

  let response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: JSON.stringify(body || {}),
      credentials: 'same-origin',
    });
  } catch {
    return { ok: false, status: 0, body: {}, error: networkError() };
  }

  let parsed = {};
  if (response.status !== 204) {
    try {
      const text = await response.text();
      parsed = text ? JSON.parse(text) : {};
    } catch {
      parsed = {};
    }
  }

  if (!response.ok) {
    return { ok: false, status: response.status, body: parsed, error: errorFromResponse(response, parsed) };
  }
  return { ok: true, status: response.status, body: parsed, error: null };
}

/**
 * POST JSON to a GoTrue endpoint. Thin wrapper over {@link requestGoTrue}.
 * @param {string} path
 * @param {object} [options]
 * @returns {Promise<{ ok: boolean, status: number, body: object, error: AuthClientError|null }>}
 */
function postGoTrue(path, options = {}) {
  return requestGoTrue('POST', path, options);
}

/**
 * PUT JSON to a GoTrue endpoint. Thin wrapper over {@link requestGoTrue}.
 * @param {string} path
 * @param {object} [options]
 * @returns {Promise<{ ok: boolean, status: number, body: object, error: AuthClientError|null }>}
 */
function putGoTrue(path, options = {}) {
  return requestGoTrue('PUT', path, options);
}

/**
 * Shape a successful GoTrue token grant into a session object. The proxy
 * returns `expires_in` (seconds); we also compute an absolute `expires_at`
 * (epoch seconds) so callers can schedule refresh without clock drift.
 * @param {object} body - raw token-grant response body.
 * @returns {object|null} session, or null when no access token was issued.
 */
export function toSession(body) {
  if (!body || typeof body.access_token !== 'string' || !body.access_token) {
    return null;
  }
  const expiresIn = Number(body.expires_in) || 3600;
  const nowSeconds = Math.floor(Date.now() / 1000);
  const expiresAt = Number(body.expires_at) || nowSeconds + expiresIn;
  return {
    access_token: body.access_token,
    refresh_token: body.refresh_token || '',
    token_type: body.token_type || 'bearer',
    expires_in: expiresIn,
    expires_at: expiresAt,
    user: body.user || null,
  };
}

/**
 * Sign up a new cloud account.
 *
 * The GoTrue proxy is enumeration-safe: it returns a generic 200 even for
 * duplicate emails, and never returns a session on signup (email verification
 * is always required). Callers therefore treat any 2xx as success and surface
 * the "check your email" message.
 *
 * @param {string} email
 * @param {string} password
 * @param {{ tosAccepted?: boolean, legalEligibilityConfirmed?: boolean, coppaAgeConfirmed?: boolean, termsVersion?: string, privacyVersion?: string }} consents
 * @returns {Promise<{ ok: boolean, session: object|null, needsEmailVerification: boolean, error: AuthClientError|null }>}
 */
export async function signUp(email, password, consents = {}) {
  const eligibilityConfirmed = consents.legalEligibilityConfirmed === true
    || consents.coppaAgeConfirmed === true;
  const termsVersion = String(consents.termsVersion || ACCEPTED_TERMS_VERSION);
  const privacyVersion = String(consents.privacyVersion || ACCEPTED_PRIVACY_VERSION);
  const result = await postGoTrue('/signup', {
    body: {
      email: String(email || '').trim(),
      password,
      data: {
        tos_accepted: consents.tosAccepted === true,
        coppa_age_confirmed: eligibilityConfirmed,
        legal_eligibility_confirmed: eligibilityConfirmed,
        terms_version: termsVersion,
        privacy_version: privacyVersion,
      },
    },
  });
  if (!result.ok) {
    return { ok: false, session: null, needsEmailVerification: false, error: result.error };
  }
  const session = toSession(result.body);
  return {
    ok: true,
    session,
    // No session issued => email confirmation required (the common case).
    needsEmailVerification: !session,
    error: null,
  };
}

/**
 * Sign in with email + password (GoTrue password grant).
 * @param {string} email
 * @param {string} password
 * @returns {Promise<{ ok: boolean, session: object|null, error: AuthClientError|null }>}
 */
export async function signInWithPassword(email, password) {
  const result = await postGoTrue('/token', {
    query: { grant_type: 'password' },
    body: { email: String(email || '').trim(), password },
  });
  if (!result.ok) {
    return { ok: false, session: null, error: result.error };
  }
  const session = toSession(result.body);
  if (!session) {
    return {
      ok: false,
      session: null,
      error: {
        message: 'The account service returned an unexpected response. Try again.',
        code: 'invalid_token_response',
        status: result.status,
        retryAfter: null,
      },
    };
  }
  return { ok: true, session, error: null };
}

/**
 * Decode a JWT's payload WITHOUT verifying its signature. GoTrue access tokens
 * are JWTs whose payload carries the Authenticator-Assurance-Level (`aal`)
 * claim. We only READ that claim to decide whether a second factor is still
 * owed; the token's authenticity is enforced server-side by GoTrue, never here.
 * @param {string|null|undefined} token
 * @returns {Record<string, unknown>|null}
 */
export function decodeJwtPayload(token) {
  if (!token || typeof token !== 'string') return null;
  const segment = token.split('.')[1];
  if (!segment) return null;
  try {
    const normalized = segment.replace(/-/g, '+').replace(/_/g, '/');
    const decoded = typeof atob === 'function'
      ? atob(normalized)
      : Buffer.from(normalized, 'base64').toString('binary');
    return JSON.parse(decoded);
  } catch {
    return null;
  }
}

/**
 * The user's VERIFIED TOTP factor for this session, or null. GoTrue returns the
 * enrolled factors on the token-grant response (`user.factors`); a factor only
 * gates sign-in once its status is 'verified' (a still-enrolling / unverified
 * factor must NOT force a second factor).
 * @param {object|null} session
 * @returns {{ id: string }|null}
 */
export function verifiedTotpFactor(session) {
  const factors = session?.user?.factors;
  if (!Array.isArray(factors)) return null;
  return factors.find(
    (factor) => factor?.factor_type === 'totp' && factor?.status === 'verified',
  ) || null;
}

/**
 * Whether a freshly-minted password session still owes a TOTP second factor.
 *
 * A password grant only ever satisfies AAL1 ("Authenticator Assurance Level" 1
 * — a single factor). If the account has a verified TOTP factor and the access
 * token's `aal` claim is not already 'aal2', GoTrue requires an AAL1 -> AAL2
 * step-up (challenge + verify) before the session is fully authenticated.
 *
 * Fail-closed: a verified TOTP factor paired with a token whose `aal` is
 * anything other than a proven 'aal2' — INCLUDING an undecodable token — still
 * needs the second factor. An enrolled account is never silently waved through
 * at AAL1.
 * @param {object|null} session
 * @returns {{ required: boolean, factorId: string|null, aal: string }}
 */
export function mfaStepUpForSession(session) {
  const factor = verifiedTotpFactor(session);
  if (!factor) return { required: false, factorId: null, aal: 'aal1' };
  const payload = decodeJwtPayload(session?.access_token);
  const aal = typeof payload?.aal === 'string' ? payload.aal : 'aal1';
  return { required: aal !== 'aal2', factorId: factor.id, aal };
}

/**
 * Begin a TOTP MFA challenge for an enrolled factor (GoTrue AAL2 step-up,
 * step 1 of 2). POSTs the session's AAL1 bearer to
 * `/auth/v1/factors/{factorId}/challenge`; GoTrue answers with a challenge id
 * that must be paired with the user's 6-digit code in verifyMfaChallenge().
 *
 * On the cloud front door these `/auth/v1/factors/*` routes reach GoTrue
 * directly (Caddy's `@gotrue` passthrough — see deploy/caddy/Caddyfile), not
 * the Viola FastAPI proxy, so the hand-rolled REST client below is the GoTrue
 * MFA API, verbatim.
 * @param {string} accessToken - the AAL1 session access token (bearer).
 * @param {string} factorId
 * @returns {Promise<{ ok: boolean, challengeId: string|null, error: AuthClientError|null }>}
 */
export async function challengeMfaFactor(accessToken, factorId) {
  const result = await postGoTrue(`/factors/${encodeURIComponent(factorId)}/challenge`, {
    token: accessToken,
  });
  if (!result.ok) {
    return { ok: false, challengeId: null, error: result.error };
  }
  const challengeId = typeof result.body?.id === 'string' ? result.body.id : '';
  if (!challengeId) {
    return {
      ok: false,
      challengeId: null,
      error: {
        message: 'The account service returned an unexpected response. Try again.',
        code: 'invalid_challenge_response',
        status: result.status,
        retryAfter: null,
      },
    };
  }
  return { ok: true, challengeId, error: null };
}

/**
 * Complete a TOTP MFA challenge (GoTrue AAL2 step-up, step 2 of 2). POSTs the
 * challenge id + the user's 6-digit code to `/auth/v1/factors/{factorId}/verify`
 * with the AAL1 bearer. On success GoTrue returns a NEW, fully-authenticated
 * AAL2 session (fresh access + refresh token) which we shape via toSession —
 * that upgraded session, not the AAL1 one, is the real sign-in.
 * @param {string} accessToken - the AAL1 session access token (bearer).
 * @param {string} factorId
 * @param {string} challengeId
 * @param {string} code - the 6-digit TOTP code.
 * @returns {Promise<{ ok: boolean, session: object|null, error: AuthClientError|null }>}
 */
export async function verifyMfaChallenge(accessToken, factorId, challengeId, code) {
  const result = await postGoTrue(`/factors/${encodeURIComponent(factorId)}/verify`, {
    token: accessToken,
    body: { challenge_id: challengeId, code: String(code || '').trim() },
  });
  if (!result.ok) {
    return { ok: false, session: null, error: result.error };
  }
  const session = toSession(result.body);
  if (!session) {
    return {
      ok: false,
      session: null,
      error: {
        message: 'The account service returned an unexpected response. Try again.',
        code: 'invalid_token_response',
        status: result.status,
        retryAfter: null,
      },
    };
  }
  return { ok: true, session, error: null };
}

/**
 * Run the two-step TOTP step-up (challenge then verify) and return the upgraded
 * AAL2 session. The front-door analogue of the SDK's `mfa.challengeAndVerify`,
 * for the hand-rolled REST client.
 * @param {string} accessToken - the AAL1 session access token (bearer).
 * @param {string} factorId
 * @param {string} code - the 6-digit TOTP code.
 * @returns {Promise<{ ok: boolean, session: object|null, error: AuthClientError|null }>}
 */
export async function challengeAndVerifyMfaTotp(accessToken, factorId, code) {
  const challenge = await challengeMfaFactor(accessToken, factorId);
  if (!challenge.ok) {
    return { ok: false, session: null, error: challenge.error };
  }
  return verifyMfaChallenge(accessToken, factorId, challenge.challengeId, code);
}

/**
 * Exchange a refresh token for a fresh access/refresh token pair.
 * @param {string} refreshToken
 * @returns {Promise<{ ok: boolean, session: object|null, error: AuthClientError|null }>}
 */
export async function refresh(refreshToken) {
  if (!refreshToken) {
    return {
      ok: false,
      session: null,
      error: { message: 'Session expired. Please sign in again.', code: 'no_refresh_token', status: 0, retryAfter: null },
    };
  }
  const result = await postGoTrue('/token', {
    query: { grant_type: 'refresh_token' },
    body: { refresh_token: refreshToken },
  });
  if (!result.ok) {
    return { ok: false, session: null, error: result.error };
  }
  const session = toSession(result.body);
  if (!session) {
    return {
      ok: false,
      session: null,
      error: {
        message: 'Session expired. Please sign in again.',
        code: 'invalid_token_response',
        status: result.status,
        retryAfter: null,
      },
    };
  }
  return { ok: true, session, error: null };
}

/**
 * Hydrate a desktop session from the persistent httpOnly `viola_session` cookie
 * (#2604 — persistent desktop sign-in; supersedes SEC-017's per-launch in-memory
 * behaviour WITHOUT reintroducing its vulnerability).
 *
 * On the desktop app the GoTrue access+refresh pair is persisted server-side,
 * encrypted at rest under the OS-keystore-backed master key (auth/desktop_session.py),
 * and the browser only ever holds an opaque, httpOnly `viola_session` cookie —
 * never a token. That cookie survives a full app quit+relaunch (QtWebEngine
 * ForcePersistentCookies). SEC-017 kept the webview's *own* GoTrue tokens in
 * memory only (never localStorage), which is why a relaunch previously started
 * signed-out: the in-memory session is gone and nothing re-established it.
 *
 * This call closes that gap the SEC-017-safe way. It POSTs a
 * `grant_type=refresh_token` request with an EMPTY body (no refresh token in
 * the browser to present) and same-origin credentials so the httpOnly cookie
 * rides along. The desktop GoTrue proxy resolves the session from that cookie
 * alone (`serve_desktop_gotrue_refresh_grant` via the store's single-flight)
 * and returns the current rotated token pair from the encrypted store — the
 * browser never persisted, and still never persists, a refresh token to disk.
 *
 * Fail-safe: when no desktop session cookie resolves (never signed in, or a
 * sign-out cleared it), the proxy answers `400 invalid_grant` and this returns
 * `{ ok:false }` — the normal signed-out state, no credential prompt forced.
 *
 * @returns {Promise<{ ok: boolean, session: object|null, error: AuthClientError|null }>}
 */
export async function hydrateDesktopSessionFromCookie() {
  // Empty body: the persistent httpOnly cookie (sent via same-origin
  // credentials by postGoTrue) is the sole credential. The proxy MUST NOT
  // receive a browser-held refresh token here — there isn't one, and the
  // whole point is that the token never lives in the browser.
  const result = await postGoTrue('/token', {
    query: { grant_type: 'refresh_token' },
    body: {},
  });
  if (!result.ok) {
    // 400 invalid_grant (no cookie / signed out) is the ordinary signed-out
    // path — surface it as a clean miss, not an error the UI must explain.
    return { ok: false, session: null, error: result.error };
  }
  const session = toSession(result.body);
  if (!session) {
    return {
      ok: false,
      session: null,
      error: {
        message: 'The account service returned an unexpected response.',
        code: 'invalid_token_response',
        status: result.status,
        retryAfter: null,
      },
    };
  }
  return { ok: true, session, error: null };
}

/**
 * Sign out — revokes the GoTrue session server-side. The proxy returns 204
 * and clears compatibility cookies. A failed logout still clears local state
 * (handled by the caller), so this resolves `ok:true` on transport failure to
 * avoid trapping the user in a signed-in shell.
 * @param {string} accessToken - the session's access token.
 * @returns {Promise<{ ok: boolean, error: AuthClientError|null }>}
 */
export async function signOut(accessToken) {
  const result = await postGoTrue('/logout', { token: accessToken });
  if (!result.ok && result.status !== 0 && result.status !== 401) {
    return { ok: false, error: result.error };
  }
  return { ok: true, error: null };
}

/**
 * The SPA path the Viola Cloud bundle is served under (see backend/cloud_static.py
 * and components/auth/cloudSurface.js). The recovery email must land here — on the
 * SPA that owns the set-new-password form — NOT on the marketing site (GoTrue's
 * SITE_URL). Without a `redirect_to`, GoTrue falls back to SITE_URL (useviola.com),
 * which has no password UI (#2608).
 */
const CLOUD_SPA_PATH = '/app';

/**
 * Build the recovery landing URL GoTrue redirects the email link to. Same-origin
 * with the SPA that made the request, so it resolves to `https://api.useviola.com/app`
 * on the cloud surface. The proxy's redirect allow-list (auth/gotrue_proxy.py
 * `_gotrue_redirect_allowed_hosts`) and GoTrue's own `GOTRUE_URI_ALLOW_LIST` must
 * both permit this host — see docker-compose.cloud.yml.
 * @returns {string} absolute recovery landing URL, or '' when no origin is resolvable.
 */
export function recoveryRedirectUrl() {
  const origin = resolveOrigin();
  if (!origin) return '';
  return `${origin.replace(/\/+$/, '')}${CLOUD_SPA_PATH}`;
}

/**
 * Parse a GoTrue recovery landing hash (`#access_token=...&type=recovery&...`).
 * GoTrue's `/verify?type=recovery` redirect returns the recovery session in the
 * URL fragment (implicit flow, since `/recover` is posted without a PKCE
 * challenge). Returns the recovery access token when the hash is a genuine
 * recovery landing, else null.
 * @param {string} [hash] - URL fragment including the leading '#'. Defaults to
 *   `window.location.hash`.
 * @returns {{ accessToken: string, refreshToken: string|null }|null}
 */
export function parseRecoveryHash(hash) {
  const raw = typeof hash === 'string'
    ? hash
    : (typeof window !== 'undefined' && window.location ? window.location.hash : '');
  if (!raw || raw.indexOf('access_token') === -1) return null;
  const params = new URLSearchParams(raw.replace(/^#/, ''));
  if (params.get('type') !== 'recovery') return null;
  const accessToken = params.get('access_token');
  if (!accessToken) return null;
  return { accessToken, refreshToken: params.get('refresh_token') || null };
}

/**
 * Request a password-reset email. The proxy is enumeration-safe: any non-rate-
 * limited request resolves to a generic success. `redirect_to` steers GoTrue's
 * recovery email link back to the SPA that owns the set-new-password form; omit
 * it and the link lands on the marketing SITE_URL, which has no password UI
 * (#2608).
 * @param {string} email
 * @param {string} [redirectTo] - recovery landing URL; defaults to
 *   `recoveryRedirectUrl()`.
 * @returns {Promise<{ ok: boolean, error: AuthClientError|null }>}
 */
export async function resetPassword(email, redirectTo = recoveryRedirectUrl()) {
  const body = { email: String(email || '').trim() };
  if (redirectTo) {
    body.redirect_to = redirectTo;
  }
  const result = await postGoTrue('/recover', { body });
  if (!result.ok) {
    return { ok: false, error: result.error };
  }
  return { ok: true, error: null };
}

/**
 * Set a new password using a recovery-session access token (the token GoTrue put
 * in the recovery landing URL fragment). Calls GoTrue `PUT /user` with the token
 * as Bearer. On success the proxy revokes the whole session family, so the caller
 * must send the user to sign in with the new password.
 *
 * Note: the proxy's credential-mutation step-up gate must accept a recovery
 * session (auth/gotrue_proxy.py `_proxy_user_step_up_verified`) — the user has no
 * current password to supply.
 * @param {string} accessToken - recovery-session access token from the URL hash.
 * @param {string} newPassword
 * @returns {Promise<{ ok: boolean, error: AuthClientError|null }>}
 */
export async function updatePasswordWithToken(accessToken, newPassword) {
  const result = await putGoTrue('/user', {
    token: accessToken,
    body: { password: String(newPassword || '') },
  });
  if (!result.ok) {
    return { ok: false, error: result.error };
  }
  return { ok: true, error: null };
}

/**
 * Resend the signup verification email.
 * @param {string} email
 * @returns {Promise<{ ok: boolean, error: AuthClientError|null }>}
 */
export async function resendVerification(email) {
  const result = await postGoTrue('/resend', {
    body: { type: 'signup', email: String(email || '').trim() },
  });
  if (!result.ok) {
    return { ok: false, error: result.error };
  }
  return { ok: true, error: null };
}

export default {
  GOTRUE_BASE,
  toSession,
  signUp,
  signInWithPassword,
  decodeJwtPayload,
  verifiedTotpFactor,
  mfaStepUpForSession,
  challengeMfaFactor,
  verifyMfaChallenge,
  challengeAndVerifyMfaTotp,
  refresh,
  hydrateDesktopSessionFromCookie,
  signOut,
  resetPassword,
  updatePasswordWithToken,
  recoveryRedirectUrl,
  parseRecoveryHash,
  resendVerification,
};
