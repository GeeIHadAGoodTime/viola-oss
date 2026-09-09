import { describe, it, expect, beforeEach, vi } from 'vitest';
import { hydrateDesktopSessionFromCookie } from './authClient';

/**
 * Persistent desktop sign-in — cookie hydration (#2604).
 *
 * hydrateDesktopSessionFromCookie() restores a persisted desktop session from
 * the httpOnly viola_session cookie on launch, WITHOUT ever presenting a
 * browser-held refresh token (SEC-017: the token never lives in the browser).
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
  access_token: 'rotated-access-jwt',
  refresh_token: 'rotated-refresh-jwt',
  token_type: 'bearer',
  expires_in: 3600,
  user: { id: 'user-1', email: 'a@b.com' },
};

beforeEach(() => {
  global.fetch = vi.fn();
});

describe('authClient.hydrateDesktopSessionFromCookie', () => {
  it('POSTs grant_type=refresh_token with credentials and NO browser refresh token', async () => {
    global.fetch.mockResolvedValue(mockResponse({ body: TOKEN_BODY }));

    const result = await hydrateDesktopSessionFromCookie();

    expect(result.ok).toBe(true);
    expect(result.session.access_token).toBe('rotated-access-jwt');
    expect(result.session.refresh_token).toBe('rotated-refresh-jwt');

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = global.fetch.mock.calls[0];
    expect(String(url)).toContain('/auth/v1/token');
    expect(String(url)).toContain('grant_type=refresh_token');
    // The httpOnly cookie is the sole credential — same-origin credentials send it.
    expect(opts.credentials).toBe('same-origin');
    // Crucially: the request body carries NO refresh token from the browser.
    const sentBody = JSON.parse(opts.body || '{}');
    expect(sentBody).not.toHaveProperty('refresh_token');
  });

  it('returns a clean signed-out miss on 400 invalid_grant (no cookie / signed out)', async () => {
    global.fetch.mockResolvedValue(
      mockResponse({ status: 400, body: { error: 'invalid_grant', error_code: 'invalid_grant' } }),
    );

    const result = await hydrateDesktopSessionFromCookie();

    expect(result.ok).toBe(false);
    expect(result.session).toBeNull();
  });

  it('surfaces a transport failure as a non-ok result rather than throwing', async () => {
    global.fetch.mockRejectedValue(new TypeError('network down'));

    const result = await hydrateDesktopSessionFromCookie();

    expect(result.ok).toBe(false);
    expect(result.session).toBeNull();
  });
});
