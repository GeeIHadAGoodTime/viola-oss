import { describe, expect, it, vi } from 'vitest';
import { createGoTrueClient, externalOAuthAuthorizeUrl } from './gotrue_client';

describe('gotrue_client', () => {
  it('rewrites desktop loopback OAuth authorize URLs to the public GoTrue host', () => {
    const localUrl = new URL('http://localhost:8756/auth/v1/authorize');
    localUrl.searchParams.set('provider', 'google');
    localUrl.searchParams.set('redirect_to', 'https://useviola.com/login?auth=oauth');
    localUrl.searchParams.set('code_challenge', 'challenge-123');
    localUrl.searchParams.set('code_challenge_method', 's256');

    const rewritten = new URL(externalOAuthAuthorizeUrl(localUrl.toString()));

    expect(rewritten.origin).toBe('https://api.useviola.com');
    expect(rewritten.pathname).toBe('/auth/v1/authorize');
    expect(rewritten.searchParams.get('provider')).toBe('google');
    expect(rewritten.searchParams.get('redirect_to')).toBe('https://useviola.com/login?auth=oauth');
    expect(rewritten.searchParams.get('code_challenge')).toBe('challenge-123');
    expect(rewritten.searchParams.get('code_challenge_method')).toBe('s256');
  });

  it('rewrites IPv4 and IPv6 loopback OAuth authorize URLs', () => {
    expect(externalOAuthAuthorizeUrl('https://127.0.0.1:8756/auth/v1/authorize?provider=apple')).toBe(
      'https://api.useviola.com/auth/v1/authorize?provider=apple',
    );
    expect(externalOAuthAuthorizeUrl('https://[::1]:8756/auth/v1/authorize?provider=google')).toBe(
      'https://api.useviola.com/auth/v1/authorize?provider=google',
    );
  });

  it('does not rewrite public authorize URLs or same-origin API calls', () => {
    const publicUrl = 'https://api.useviola.com/auth/v1/authorize?provider=google';
    const tokenUrl = 'http://localhost:8756/auth/v1/token?grant_type=password';

    expect(externalOAuthAuthorizeUrl(publicUrl)).toBe(publicUrl);
    expect(externalOAuthAuthorizeUrl(tokenUrl)).toBe(tokenUrl);
  });

  it('keeps auth-js same-origin OAuth construction separate from the external browser URL', async () => {
    const client = createGoTrueClient({
      url: 'http://localhost:8756/auth/v1',
      autoRefreshToken: false,
      persistSession: false,
      detectSessionInUrl: false,
      storageKey: 'viola-gotrue-oauth-test-session',
    });

    const { data, error } = await client.signInWithOAuth({
      provider: 'google',
      options: {
        redirectTo: 'https://useviola.com/login?auth=oauth',
        skipBrowserRedirect: true,
      },
    });

    expect(error).toBeNull();
    expect(data.url).toBeTruthy();
    const authJsUrl = new URL(data.url || '');
    expect(authJsUrl.origin).toBe('http://localhost:8756');
    expect(authJsUrl.pathname).toBe('/auth/v1/authorize');
    expect(authJsUrl.searchParams.get('provider')).toBe('google');

    const externalUrl = new URL(externalOAuthAuthorizeUrl(data.url || ''));
    expect(externalUrl.origin).toBe('https://api.useviola.com');
    expect(externalUrl.pathname).toBe('/auth/v1/authorize');
    expect(externalUrl.searchParams.get('provider')).toBe('google');
  });

  it('uses the GoTrue password token endpoint for email sign-in', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      access_token: 'access-token',
      refresh_token: 'refresh-token',
      expires_in: 3600,
      token_type: 'bearer',
      user: {
        id: 'user-1',
        aud: 'authenticated',
        role: 'authenticated',
        email: 'person@example.com',
        app_metadata: { plan_tier: 'free' },
        user_metadata: {},
        created_at: '2026-05-19T00:00:00.000Z',
        updated_at: '2026-05-19T00:00:00.000Z',
      },
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));

    const client = createGoTrueClient({
      url: 'https://api.useviola.com/auth/v1',
      fetch: fetchMock,
      autoRefreshToken: false,
      persistSession: false,
      detectSessionInUrl: false,
      storageKey: 'viola-gotrue-test-session',
    });

    const { error } = await client.signInWithPassword({
      email: 'person@example.com',
      password: 'CorrectHorse123!', // pragma: allowlist secret
    });

    expect(error).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    const [url, init] = fetchMock.mock.calls[0];
    const calledUrl = new URL(String(url));
    expect(calledUrl.pathname).toBe('/auth/v1/token');
    expect(calledUrl.searchParams.get('grant_type')).toBe('password');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toMatchObject({
      email: 'person@example.com',
      password: 'CorrectHorse123!', // pragma: allowlist secret
    });
  });
});
