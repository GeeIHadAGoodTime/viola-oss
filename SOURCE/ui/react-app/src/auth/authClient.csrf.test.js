/**
 * Returning-visitor CSRF regression suite (#362 / #3547).
 *
 * LIVE DEFECT, reproduced against https://api.useviola.com/app on 2026-08-04:
 * a first-ever visitor could sign in, but every visit after that returned
 * `403 {"detail":"CSRF token validation failed"}` from
 * `POST /auth/v1/token?grant_type=password`, surfacing only as
 * "Something went wrong. Please try again." with no in-page recovery.
 *
 * MECHANISM (probed live, 5 arms, one variable each):
 *   no cookies                       -> 400 invalid_credentials  (check skipped)
 *   viola_csrf only                  -> 400 invalid_credentials  (check skipped)
 *   viola_session only               -> 403 CSRF                 (check armed)
 *   viola_session + viola_csrf       -> 403 CSRF                 (no header sent)
 *   viola_session + csrf + header    -> 400 invalid_credentials  (check passed)
 *
 * So the trigger is the `viola_session` cookie that the first sign-in plants
 * (`auth/gotrue_proxy.py` `_attach_compat_session_cookies`), and the fix is for
 * this client to honour the double-submit contract `auth/csrf.py` documents.
 *
 * These tests assert the POSITIVE shape — the header goes out, matching the
 * cookie — so they fail on the pre-fix client that sent no CSRF header at all.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  signInWithPassword,
  signUp,
  refresh,
  hydrateDesktopSessionFromCookie,
  signOut,
} from './authClient';

const TOKEN_BODY = {
  access_token: 'access-jwt',
  refresh_token: 'refresh-jwt',
  token_type: 'bearer',
  expires_in: 3600,
  user: { id: 'user-1', email: 'a@b.com' },
};

function mockResponse({ status = 200, body = {} } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    text: () => Promise.resolve(status === 204 ? '' : JSON.stringify(body)),
  };
}

/** Headers the single recorded fetch call was made with. */
function sentHeaders() {
  expect(global.fetch).toHaveBeenCalled();
  return global.fetch.mock.calls[0][1].headers || {};
}

/** Case-insensitive header lookup — header names are not case-sensitive. */
function headerValue(headers, name) {
  const key = Object.keys(headers).find((k) => k.toLowerCase() === name.toLowerCase());
  return key === undefined ? undefined : headers[key];
}

/** Simulate the browser's cookie jar for this document. */
function setCookieJar(value) {
  Object.defineProperty(document, 'cookie', {
    configurable: true,
    get: () => value,
    set: () => {},
  });
}

let originalCookie;

beforeEach(() => {
  global.fetch = vi.fn().mockResolvedValue(mockResponse({ body: TOKEN_BODY }));
  originalCookie = Object.getOwnPropertyDescriptor(Document.prototype, 'cookie')
    || Object.getOwnPropertyDescriptor(document, 'cookie');
});

afterEach(() => {
  if (originalCookie) {
    Object.defineProperty(document, 'cookie', originalCookie);
  }
  vi.restoreAllMocks();
});

describe('authClient double-submit CSRF header', () => {
  it('attaches X-CSRF-Token matching the viola_csrf cookie on sign-in', async () => {
    // The returning visitor's jar: a session cookie from the previous visit
    // (httpOnly, so not visible here) plus the readable double-submit cookie.
    setCookieJar('viola_csrf=returning-visitor-csrf-token; other=1');

    await signInWithPassword('a@b.com', 'pw');

    expect(headerValue(sentHeaders(), 'X-CSRF-Token'))
      .toBe('returning-visitor-csrf-token');
  });

  it('attaches the header on the reload-time refresh-token hydration', async () => {
    // Same 403 class as sign-in: on reload the SPA POSTs grant_type=refresh_token
    // with the session cookie riding along, which arms the server check.
    setCookieJar('viola_csrf=hydrate-token');

    await hydrateDesktopSessionFromCookie();

    expect(headerValue(sentHeaders(), 'X-CSRF-Token')).toBe('hydrate-token');
  });

  it('attaches the header on an explicit refresh', async () => {
    setCookieJar('viola_csrf=refresh-token-value');

    await refresh('some-refresh-jwt');

    expect(headerValue(sentHeaders(), 'X-CSRF-Token')).toBe('refresh-token-value');
  });

  it('attaches the header on signup', async () => {
    setCookieJar('viola_csrf=signup-token');

    await signUp('a@b.com', 'pw', { tosAccepted: true, coppaAgeConfirmed: true });

    expect(headerValue(sentHeaders(), 'X-CSRF-Token')).toBe('signup-token');
  });

  it('attaches the header even when a Bearer token is also sent', async () => {
    // Belt and braces: the server currently treats a Bearer as a CSRF
    // exemption, but the client should not depend on that staying true.
    setCookieJar('viola_csrf=logout-token');
    global.fetch = vi.fn().mockResolvedValue(mockResponse({ status: 204 }));

    await signOut('access-jwt');

    const headers = sentHeaders();
    expect(headerValue(headers, 'X-CSRF-Token')).toBe('logout-token');
    expect(headerValue(headers, 'Authorization')).toBe('Bearer access-jwt');
  });

  it('picks the right cookie when other cookies share a suffix', async () => {
    setCookieJar('not_viola_csrf=WRONG; viola_csrf=RIGHT; viola_csrf_backup=ALSOWRONG');

    await signInWithPassword('a@b.com', 'pw');

    expect(headerValue(sentHeaders(), 'X-CSRF-Token')).toBe('RIGHT');
  });

  it('percent-decodes the cookie value the way the server encoded it', async () => {
    setCookieJar('viola_csrf=a%2Bb%2Fc');

    await signInWithPassword('a@b.com', 'pw');

    expect(headerValue(sentHeaders(), 'X-CSRF-Token')).toBe('a+b/c');
  });

  it('omits the header entirely when no CSRF cookie is readable', async () => {
    // An empty header value fails the server's compare_digest identically to a
    // missing one, but makes the failure harder to read. Send nothing instead.
    setCookieJar('unrelated=1');

    await signInWithPassword('a@b.com', 'pw');

    expect(headerValue(sentHeaders(), 'X-CSRF-Token')).toBeUndefined();
  });

  it('surfaces a CSRF refusal distinguishably instead of "Something went wrong"', async () => {
    // The generic message is what hid this defect in prod for its whole life.
    setCookieJar('viola_csrf=stale');
    global.fetch = vi.fn().mockResolvedValue(
      mockResponse({ status: 403, body: { detail: 'CSRF token validation failed' } }),
    );

    const result = await signInWithPassword('a@b.com', 'pw');

    expect(result.ok).toBe(false);
    expect(result.error.code).toBe('csrf_token_invalid');
    expect(result.error.message).not.toBe('Something went wrong. Please try again.');
    expect(result.error.message).toMatch(/refresh the page/i);
  });

  it('also recognises the csrf_required dependency refusal shape', async () => {
    // auth/csrf.py's route dependency answers a nested object, not a string.
    setCookieJar('viola_csrf=stale');
    global.fetch = vi.fn().mockResolvedValue(
      mockResponse({
        status: 403,
        body: { detail: { error: 'csrf_mismatch', message: 'CSRF token missing or invalid' } },
      }),
    );

    const result = await signInWithPassword('a@b.com', 'pw');

    expect(result.error.code).toBe('csrf_token_invalid');
    expect(result.error.message).toMatch(/refresh the page/i);
  });

  it('does not mistake an unrelated 403 for a CSRF failure', async () => {
    setCookieJar('viola_csrf=tok');
    global.fetch = vi.fn().mockResolvedValue(
      mockResponse({ status: 403, body: { error_code: 'user_banned', msg: 'User is banned' } }),
    );

    const result = await signInWithPassword('a@b.com', 'pw');

    expect(result.error.code).toBe('user_banned');
    expect(result.error.message).not.toMatch(/refresh the page/i);
  });

  it('still sends Content-Type and same-origin credentials', async () => {
    // The cookie has to ride along for the double-submit pair to be compared.
    setCookieJar('viola_csrf=tok');

    await signInWithPassword('a@b.com', 'pw');

    const [, init] = global.fetch.mock.calls[0];
    expect(headerValue(init.headers, 'Content-Type')).toBe('application/json');
    expect(init.credentials).toBe('same-origin');
  });
});
