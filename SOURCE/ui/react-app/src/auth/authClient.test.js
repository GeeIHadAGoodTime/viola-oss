import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  signUp,
  signInWithPassword,
  refresh,
  signOut,
  resetPassword,
  updatePasswordWithToken,
  recoveryRedirectUrl,
  parseRecoveryHash,
  resendVerification,
  toSession,
  decodeJwtPayload,
  verifiedTotpFactor,
  mfaStepUpForSession,
  challengeMfaFactor,
  verifyMfaChallenge,
  challengeAndVerifyMfaTotp,
} from './authClient';
import { ACCEPTED_PRIVACY_VERSION, ACCEPTED_TERMS_VERSION } from './legalVersions';

/**
 * Build a mock fetch Response.
 * @param {object} opts
 */
function mockResponse({ status = 200, body = {}, headers = {} } = {}) {
  const headerMap = new Map(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => headerMap.get(String(name).toLowerCase()) ?? null },
    text: () => Promise.resolve(status === 204 ? '' : JSON.stringify(body)),
  };
}

const TOKEN_BODY = {
  access_token: 'access-jwt',
  refresh_token: 'refresh-jwt',
  token_type: 'bearer',
  expires_in: 3600,
  user: { id: 'user-1', email: 'a@b.com' },
};

beforeEach(() => {
  global.fetch = vi.fn();
});

describe('authClient.toSession', () => {
  it('builds a session with a computed expires_at', () => {
    const before = Math.floor(Date.now() / 1000);
    const session = toSession(TOKEN_BODY);
    expect(session.access_token).toBe('access-jwt');
    expect(session.refresh_token).toBe('refresh-jwt');
    expect(session.expires_in).toBe(3600);
    expect(session.expires_at).toBeGreaterThanOrEqual(before + 3600);
  });

  it('returns null when no access token is present', () => {
    expect(toSession({})).toBeNull();
    expect(toSession(null)).toBeNull();
  });
});

describe('authClient.signInWithPassword', () => {
  it('POSTs to /auth/v1/token?grant_type=password and returns a session', async () => {
    global.fetch.mockResolvedValue(mockResponse({ body: TOKEN_BODY }));

    const result = await signInWithPassword('a@b.com', 'pw123456');

    expect(result.ok).toBe(true);
    expect(result.session.access_token).toBe('access-jwt');
    expect(result.error).toBeNull();

    const [url, init] = global.fetch.mock.calls[0];
    const parsed = new URL(url);
    expect(parsed.pathname).toBe('/auth/v1/token');
    expect(parsed.searchParams.get('grant_type')).toBe('password');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ email: 'a@b.com', password: 'pw123456' }); // pragma: allowlist secret
  });

  it('trims the email before sending', async () => {
    global.fetch.mockResolvedValue(mockResponse({ body: TOKEN_BODY }));
    await signInWithPassword('  spaced@b.com  ', 'pw');
    expect(JSON.parse(global.fetch.mock.calls[0][1].body).email).toBe('spaced@b.com');
  });

  it('maps invalid credentials to a friendly error', async () => {
    global.fetch.mockResolvedValue(mockResponse({
      status: 400,
      body: { error_code: 'invalid_credentials', msg: 'Invalid login credentials' },
    }));

    const result = await signInWithPassword('a@b.com', 'wrong');
    expect(result.ok).toBe(false);
    expect(result.session).toBeNull();
    expect(result.error.code).toBe('invalid_credentials');
    expect(result.error.message).toMatch(/check your email and password/i);
  });

  it('surfaces rate limiting with retryAfter', async () => {
    global.fetch.mockResolvedValue(mockResponse({
      status: 429,
      body: { error_code: 'rate_limited', msg: 'Too many attempts' },
      headers: { 'Retry-After': '30' },
    }));

    const result = await signInWithPassword('a@b.com', 'pw');
    expect(result.ok).toBe(false);
    expect(result.error.status).toBe(429);
    expect(result.error.retryAfter).toBe(30);
    expect(result.error.message).toMatch(/too many attempts/i);
  });

  it('returns a network error when fetch throws', async () => {
    global.fetch.mockRejectedValue(new TypeError('Failed to fetch'));
    const result = await signInWithPassword('a@b.com', 'pw');
    expect(result.ok).toBe(false);
    expect(result.error.code).toBe('network_error');
  });
});

describe('authClient.signUp', () => {
  it('treats a 200 with no session as needing email verification', async () => {
    global.fetch.mockResolvedValue(mockResponse({
      body: { message: 'If this email can be used, a confirmation email will be sent.' },
    }));

    const result = await signUp('new@b.com', 'pw123456', {
      tosAccepted: true,
      legalEligibilityConfirmed: true,
    });
    expect(result.ok).toBe(true);
    expect(result.needsEmailVerification).toBe(true);
    expect(result.session).toBeNull();

    const [url, init] = global.fetch.mock.calls[0];
    expect(new URL(url).pathname).toBe('/auth/v1/signup');
    expect(JSON.parse(init.body)).toMatchObject({
      email: 'new@b.com',
      data: {
        coppa_age_confirmed: true,
        tos_accepted: true,
        legal_eligibility_confirmed: true,
        terms_version: ACCEPTED_TERMS_VERSION,
        privacy_version: ACCEPTED_PRIVACY_VERSION,
      },
    });
  });

  it('maps legacy legal eligibility consent to the canonical COPPA metadata key', async () => {
    global.fetch.mockResolvedValue(mockResponse({
      body: { message: 'If this email can be used, a confirmation email will be sent.' },
    }));

    const result = await signUp('new@b.com', 'pw123456', {
      tosAccepted: true,
      legalEligibilityConfirmed: true,
    });

    expect(result.ok).toBe(true);
    const [, init] = global.fetch.mock.calls[0];
    expect(JSON.parse(init.body).data).toMatchObject({
      coppa_age_confirmed: true,
      legal_eligibility_confirmed: true,
      tos_accepted: true,
      terms_version: ACCEPTED_TERMS_VERSION,
      privacy_version: ACCEPTED_PRIVACY_VERSION,
    });
  });

  it('honours a session returned directly by signup', async () => {
    global.fetch.mockResolvedValue(mockResponse({ body: TOKEN_BODY }));
    const result = await signUp('new@b.com', 'pw123456', {
      tosAccepted: true,
      legalEligibilityConfirmed: true,
    });
    expect(result.ok).toBe(true);
    expect(result.needsEmailVerification).toBe(false);
    expect(result.session.access_token).toBe('access-jwt');
  });

  it('returns the error on a non-2xx response', async () => {
    global.fetch.mockResolvedValue(mockResponse({
      status: 400,
      body: { error_code: 'weak_password', msg: 'Password is too short' },
    }));
    const result = await signUp('new@b.com', 'x', {
      tosAccepted: true,
      legalEligibilityConfirmed: true,
    });
    expect(result.ok).toBe(false);
    expect(result.error.code).toBe('weak_password');
  });
});

describe('authClient.refresh', () => {
  it('POSTs to /auth/v1/token?grant_type=refresh_token', async () => {
    global.fetch.mockResolvedValue(mockResponse({ body: TOKEN_BODY }));

    const result = await refresh('old-refresh');
    expect(result.ok).toBe(true);
    expect(result.session.access_token).toBe('access-jwt');

    const [url, init] = global.fetch.mock.calls[0];
    expect(new URL(url).searchParams.get('grant_type')).toBe('refresh_token');
    expect(JSON.parse(init.body)).toEqual({ refresh_token: 'old-refresh' });
  });

  it('fails fast without a refresh token', async () => {
    const result = await refresh('');
    expect(result.ok).toBe(false);
    expect(result.error.code).toBe('no_refresh_token');
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('returns the error when the refresh token is revoked', async () => {
    global.fetch.mockResolvedValue(mockResponse({
      status: 401,
      body: { error_code: 'refresh_token_reused', msg: 'Refresh token reuse detected' },
    }));
    const result = await refresh('replayed');
    expect(result.ok).toBe(false);
    expect(result.session).toBeNull();
    expect(result.error.code).toBe('refresh_token_reused');
  });
});

describe('authClient.signOut', () => {
  it('POSTs to /auth/v1/logout with a bearer token', async () => {
    global.fetch.mockResolvedValue(mockResponse({ status: 204 }));

    const result = await signOut('access-jwt');
    expect(result.ok).toBe(true);

    const [url, init] = global.fetch.mock.calls[0];
    expect(new URL(url).pathname).toBe('/auth/v1/logout');
    expect(init.headers.Authorization).toBe('Bearer access-jwt');
  });

  it('still resolves ok on a network failure (never traps the user)', async () => {
    global.fetch.mockRejectedValue(new TypeError('offline'));
    const result = await signOut('access-jwt');
    expect(result.ok).toBe(true);
  });

  it('still resolves ok on a 401 (token already dead)', async () => {
    global.fetch.mockResolvedValue(mockResponse({ status: 401, body: { msg: 'expired' } }));
    const result = await signOut('stale');
    expect(result.ok).toBe(true);
  });

  it('reports a genuine server error', async () => {
    global.fetch.mockResolvedValue(mockResponse({ status: 502, body: { code: 'auth_service_error' } }));
    const result = await signOut('access-jwt');
    expect(result.ok).toBe(false);
    expect(result.error.message).toMatch(/temporarily unavailable/i);
  });
});

describe('authClient.resetPassword', () => {
  it('POSTs to /auth/v1/recover with a redirect_to so the email lands on the SPA (#2608)', async () => {
    global.fetch.mockResolvedValue(mockResponse({ body: { message: 'sent' } }));
    const result = await resetPassword('a@b.com');
    expect(result.ok).toBe(true);
    expect(new URL(global.fetch.mock.calls[0][0]).pathname).toBe('/auth/v1/recover');
    const body = JSON.parse(global.fetch.mock.calls[0][1].body);
    expect(body.email).toBe('a@b.com');
    // The redirect_to must point at the SPA /app landing, not the marketing site.
    expect(body.redirect_to).toBe(recoveryRedirectUrl());
    expect(body.redirect_to.endsWith('/app')).toBe(true);
  });

  it('honours an explicit redirectTo override', async () => {
    global.fetch.mockResolvedValue(mockResponse({ body: { message: 'sent' } }));
    await resetPassword('a@b.com', 'https://api.useviola.com/app');
    expect(JSON.parse(global.fetch.mock.calls[0][1].body).redirect_to).toBe('https://api.useviola.com/app');
  });
});

describe('authClient.recoveryRedirectUrl', () => {
  it('resolves to the same-origin /app SPA path', () => {
    expect(recoveryRedirectUrl()).toBe(`${window.location.origin}/app`);
  });
});

describe('authClient.parseRecoveryHash', () => {
  it('extracts the recovery access token from a genuine recovery landing hash', () => {
    const parsed = parseRecoveryHash('#access_token=abc123&refresh_token=r1&type=recovery&expires_in=300');
    expect(parsed).toEqual({ accessToken: 'abc123', refreshToken: 'r1' });
  });

  it('returns null for a non-recovery hash (e.g. a normal implicit sign-in)', () => {
    expect(parseRecoveryHash('#access_token=abc123&type=magiclink')).toBeNull();
  });

  it('returns null when there is no access token', () => {
    expect(parseRecoveryHash('#type=recovery')).toBeNull();
    expect(parseRecoveryHash('')).toBeNull();
    expect(parseRecoveryHash('#error=access_denied')).toBeNull();
  });
});

describe('authClient.updatePasswordWithToken', () => {
  it('PUTs /auth/v1/user with the recovery token as Bearer and the new password', async () => {
    global.fetch.mockResolvedValue(mockResponse({ body: { id: 'user-1' } }));
    const result = await updatePasswordWithToken('recovery-jwt', 'BrandNewPassword42!');
    expect(result.ok).toBe(true);
    const [url, init] = global.fetch.mock.calls[0];
    expect(new URL(url).pathname).toBe('/auth/v1/user');
    expect(init.method).toBe('PUT');
    expect(init.headers.Authorization).toBe('Bearer recovery-jwt');
    expect(JSON.parse(init.body)).toEqual({ password: 'BrandNewPassword42!' }); // pragma: allowlist secret
  });

  it('surfaces a reauth_required rejection as not-ok', async () => {
    global.fetch.mockResolvedValue(mockResponse({ status: 403, body: { error_code: 'reauth_required', msg: 'nope' } }));
    const result = await updatePasswordWithToken('recovery-jwt', 'x');
    expect(result.ok).toBe(false);
    expect(result.error).toBeTruthy();
  });
});

describe('authClient.resendVerification', () => {
  it('POSTs to /auth/v1/resend with type signup', async () => {
    global.fetch.mockResolvedValue(mockResponse({ body: { message: 'sent' } }));
    const result = await resendVerification('a@b.com');
    expect(result.ok).toBe(true);
    expect(new URL(global.fetch.mock.calls[0][0]).pathname).toBe('/auth/v1/resend');
    expect(JSON.parse(global.fetch.mock.calls[0][1].body)).toEqual({
      type: 'signup',
      email: 'a@b.com',
    });
  });
});

// ===========================================================================
// TOTP MFA step-up (#2404): AAL derivation + the raw GoTrue challenge/verify
// REST dance the hand-rolled front-door client uses (the cloud edge routes
// /auth/v1/factors/* straight to GoTrue — deploy/caddy/Caddyfile @gotrue).
// ===========================================================================

/** Build an unsigned JWT carrying the given payload (only the payload is read). */
function makeJwt(payload) {
  const b64url = (obj) => btoa(JSON.stringify(obj))
    .replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
  return `${b64url({ alg: 'HS256', typ: 'JWT' })}.${b64url(payload)}.sig`;
}

/** A session whose access token carries `aal` and whose user has the given factors. */
function makeMfaSession({ aal = 'aal1', factors = [] } = {}) {
  return {
    access_token: makeJwt({ aal, sub: 'user-1' }),
    refresh_token: 'refresh-jwt',
    token_type: 'bearer',
    expires_in: 3600,
    user: { id: 'user-1', email: 'a@b.com', factors },
  };
}

const VERIFIED_TOTP = { id: 'factor-1', factor_type: 'totp', status: 'verified' };
const UNVERIFIED_TOTP = { id: 'factor-2', factor_type: 'totp', status: 'unverified' };

describe('authClient.decodeJwtPayload', () => {
  it('decodes the aal claim from a GoTrue access token', () => {
    expect(decodeJwtPayload(makeJwt({ aal: 'aal2' }))?.aal).toBe('aal2');
  });
  it('returns null for a non-JWT / undecodable token', () => {
    expect(decodeJwtPayload('not-a-jwt')).toBeNull();
    expect(decodeJwtPayload('')).toBeNull();
    expect(decodeJwtPayload(null)).toBeNull();
  });
});

describe('authClient.verifiedTotpFactor', () => {
  it('finds a verified TOTP factor', () => {
    expect(verifiedTotpFactor(makeMfaSession({ factors: [VERIFIED_TOTP] }))?.id).toBe('factor-1');
  });
  it('ignores an unverified (still-enrolling) factor', () => {
    expect(verifiedTotpFactor(makeMfaSession({ factors: [UNVERIFIED_TOTP] }))).toBeNull();
  });
  it('returns null when the user has no factors', () => {
    expect(verifiedTotpFactor(makeMfaSession())).toBeNull();
    expect(verifiedTotpFactor(null)).toBeNull();
  });
});

describe('authClient.mfaStepUpForSession', () => {
  it('requires the second factor for an enrolled account at AAL1', () => {
    const step = mfaStepUpForSession(makeMfaSession({ aal: 'aal1', factors: [VERIFIED_TOTP] }));
    expect(step.required).toBe(true);
    expect(step.factorId).toBe('factor-1');
  });
  it('does NOT require it once the session is already AAL2', () => {
    const step = mfaStepUpForSession(makeMfaSession({ aal: 'aal2', factors: [VERIFIED_TOTP] }));
    expect(step.required).toBe(false);
  });
  it('does NOT require it for a non-enrolled account', () => {
    expect(mfaStepUpForSession(makeMfaSession({ aal: 'aal1' })).required).toBe(false);
  });
  it('fails closed: enrolled account with an undecodable token still needs the code', () => {
    const session = { access_token: 'garbage', user: { factors: [VERIFIED_TOTP] } };
    const step = mfaStepUpForSession(session);
    expect(step.required).toBe(true);
    expect(step.factorId).toBe('factor-1');
  });
});

describe('authClient.challengeMfaFactor', () => {
  it('POSTs to /factors/{id}/challenge with the bearer and returns the challenge id', async () => {
    global.fetch.mockResolvedValue(mockResponse({ body: { id: 'challenge-1', type: 'totp' } }));
    const result = await challengeMfaFactor('aal1-token', 'factor-1');
    expect(result.ok).toBe(true);
    expect(result.challengeId).toBe('challenge-1');
    const [url, init] = global.fetch.mock.calls[0];
    expect(new URL(url).pathname).toBe('/auth/v1/factors/factor-1/challenge');
    expect(init.method).toBe('POST');
    expect(init.headers.Authorization).toBe('Bearer aal1-token');
  });
  it('errors when GoTrue returns no challenge id', async () => {
    global.fetch.mockResolvedValue(mockResponse({ body: {} }));
    const result = await challengeMfaFactor('aal1-token', 'factor-1');
    expect(result.ok).toBe(false);
    expect(result.challengeId).toBeNull();
  });
});

describe('authClient.verifyMfaChallenge', () => {
  it('POSTs the challenge id + code and returns the upgraded AAL2 session', async () => {
    const aal2Body = {
      access_token: makeJwt({ aal: 'aal2' }),
      refresh_token: 'refresh-2',
      token_type: 'bearer',
      expires_in: 3600,
      user: { id: 'user-1' },
    };
    global.fetch.mockResolvedValue(mockResponse({ body: aal2Body }));
    const result = await verifyMfaChallenge('aal1-token', 'factor-1', 'challenge-1', ' 123456 ');
    expect(result.ok).toBe(true);
    expect(decodeJwtPayload(result.session.access_token)?.aal).toBe('aal2');
    const [url, init] = global.fetch.mock.calls[0];
    expect(new URL(url).pathname).toBe('/auth/v1/factors/factor-1/verify');
    expect(init.headers.Authorization).toBe('Bearer aal1-token');
    // Code is trimmed before sending.
    expect(JSON.parse(init.body)).toEqual({ challenge_id: 'challenge-1', code: '123456' });
  });
  it('maps a rejected code to a friendly error', async () => {
    global.fetch.mockResolvedValue(mockResponse({
      status: 400,
      body: { error_code: 'mfa_verification_failed', msg: 'Invalid TOTP code entered' },
    }));
    const result = await verifyMfaChallenge('aal1-token', 'factor-1', 'challenge-1', '000000');
    expect(result.ok).toBe(false);
    expect(result.error.message).toMatch(/code is not valid/i);
  });
});

describe('authClient.challengeAndVerifyMfaTotp', () => {
  it('runs challenge then verify and returns the AAL2 session', async () => {
    global.fetch
      .mockResolvedValueOnce(mockResponse({ body: { id: 'challenge-1' } }))
      .mockResolvedValueOnce(mockResponse({
        body: { access_token: makeJwt({ aal: 'aal2' }), refresh_token: 'r2', expires_in: 3600, user: {} },
      }));
    const result = await challengeAndVerifyMfaTotp('aal1-token', 'factor-1', '123456');
    expect(result.ok).toBe(true);
    expect(result.session.access_token).toBeTruthy();
    expect(new URL(global.fetch.mock.calls[0][0]).pathname).toBe('/auth/v1/factors/factor-1/challenge');
    expect(new URL(global.fetch.mock.calls[1][0]).pathname).toBe('/auth/v1/factors/factor-1/verify');
  });
  it('does not attempt verify when the challenge step fails', async () => {
    global.fetch.mockResolvedValue(mockResponse({ status: 401, body: { msg: 'nope' } }));
    const result = await challengeAndVerifyMfaTotp('aal1-token', 'factor-1', '123456');
    expect(result.ok).toBe(false);
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });
});
