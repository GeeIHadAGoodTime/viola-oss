/**
 * The one rule this flow exists to enforce: provider sign-in reports success
 * only after a session exists.
 *
 * The bug it replaces returned `{ success: true }` the instant it opened a
 * browser tab, which is how four abandoned authorization codes from three real
 * people ended up in production GoTrue with nobody signed in. So every test
 * below that ends without a session asserts `failed`, and the one that ends
 * with a session asserts the exchange actually ran.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const signInWithOAuth = vi.fn();
const exchangeCodeForSession = vi.fn();

vi.mock('../lib/gotrue_client', () => ({
  gotrueClient: {
    signInWithOAuth: (...args: unknown[]) => signInWithOAuth(...args),
    exchangeCodeForSession: (...args: unknown[]) => exchangeCodeForSession(...args),
  },
}));

const {
  buildDesktopRedirectUrl,
  completeDesktopOAuth,
  desktopServerPort,
  newFlowId,
  OAUTH_WAIT_TIMEOUT_MS,
} = await import('./desktopOAuth');

const AUTHORIZE_URL = 'https://api.useviola.com/auth/v1/authorize?provider=google';

/** Drive the flow's clock and sleep by hand so a test never really waits. */
function fakeClock() {
  let current = 0;
  return {
    now: () => current,
    wait: async (ms: number) => { current += ms; },
    advanceTo: (value: number) => { current = value; },
  };
}

/**
 * The local route answers Viola's canonical `{ok, error, data}` envelope, so
 * the payload is wrapped. The live app proved this the hard way: an unwrapped
 * body was rejected by the response-envelope middleware as a 500.
 */
function respond(payloads: unknown[]) {
  const queue = [...payloads];
  return vi.fn(async () => ({
    ok: true,
    json: async () => ({ ok: true, error: null, data: queue.length > 1 ? queue.shift() : queue[0] }),
  }));
}

beforeEach(() => {
  signInWithOAuth.mockReset();
  exchangeCodeForSession.mockReset();
  signInWithOAuth.mockResolvedValue({ data: { url: AUTHORIZE_URL }, error: null });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('the redirect target', () => {
  it('is the allow-listed https relay, carrying the port and flow', () => {
    const url = new URL(buildDesktopRedirectUrl('flow-abc', '8756'));
    expect(url.origin + url.pathname).toBe('https://useviola.com/desktop-auth-callback');
    expect(url.searchParams.get('p')).toBe('8756');
    expect(url.searchParams.get('f')).toBe('flow-abc');
  });

  it('keeps ONE path segment, because GoTrue globs the allow-list on "/"', () => {
    const path = new URL(buildDesktopRedirectUrl('flow-abc')).pathname;
    expect(path.split('/').filter(Boolean)).toHaveLength(1);
  });

  it('is never the loopback listener directly — production GoTrue 400s that', () => {
    expect(buildDesktopRedirectUrl('flow-abc')).not.toContain('127.0.0.1');
  });

  it('falls back to the default port when the page has none', () => {
    vi.stubGlobal('window', { location: { port: '' } });
    expect(desktopServerPort()).toBe('8756');
  });

  it('mints flow ids the server will accept', () => {
    expect(newFlowId()).toMatch(/^[A-Za-z0-9_-]{16,128}$/);
    expect(newFlowId()).not.toBe(newFlowId());
  });
});

describe('completeDesktopOAuth', () => {
  it('signs in only after the code is redeemed for a session', async () => {
    const session = { access_token: 'at', refresh_token: 'rt' };
    vi.stubGlobal('fetch', respond([{ status: 'pending' }, { status: 'ready', code: 'code-1' }]));
    exchangeCodeForSession.mockResolvedValue({ data: { session }, error: null });

    const clock = fakeClock();
    const opened: string[] = [];
    const outcome = await completeDesktopOAuth('google', (url) => opened.push(url), clock);

    expect(opened).toEqual([AUTHORIZE_URL]);
    expect(exchangeCodeForSession).toHaveBeenCalledWith('code-1');
    expect(outcome).toEqual({ status: 'signed_in', session });
  });

  it('keeps this page alive while the browser works, so the verifier survives', async () => {
    vi.stubGlobal('fetch', respond([{ status: 'ready', code: 'code-1' }]));
    exchangeCodeForSession.mockResolvedValue({ data: { session: { access_token: 'at' } }, error: null });

    await completeDesktopOAuth('google', () => {}, fakeClock());

    // skipBrowserRedirect is what keeps the webview put; without it auth-js
    // navigates away and takes the in-memory code_verifier with it.
    expect(signInWithOAuth).toHaveBeenCalledWith(
      expect.objectContaining({
        options: expect.objectContaining({ skipBrowserRedirect: true }),
      }),
    );
  });

  it('fails when the human abandons the browser', async () => {
    vi.stubGlobal('fetch', respond([{ status: 'pending' }]));
    const clock = fakeClock();

    const outcome = await completeDesktopOAuth('google', () => clock.advanceTo(OAUTH_WAIT_TIMEOUT_MS), clock);

    expect(outcome.status).toBe('failed');
    expect(exchangeCodeForSession).not.toHaveBeenCalled();
  });

  it('fails, and says why, when the provider refuses', async () => {
    vi.stubGlobal('fetch', respond([{ status: 'error', error: 'access_denied' }]));

    const outcome = await completeDesktopOAuth('google', () => {}, fakeClock());

    expect(outcome).toMatchObject({ status: 'failed', code: 'access_denied' });
    expect(outcome.status === 'failed' && outcome.message).toBe('Sign-in was cancelled.');
  });

  it('fails when the exchange is rejected', async () => {
    vi.stubGlobal('fetch', respond([{ status: 'ready', code: 'code-1' }]));
    exchangeCodeForSession.mockResolvedValue({ data: null, error: { message: 'invalid request' } });

    const outcome = await completeDesktopOAuth('google', () => {}, fakeClock());

    expect(outcome).toMatchObject({ status: 'failed', code: 'oauth_exchange_failed' });
  });

  it('fails when the exchange returns no session, even without an error', async () => {
    vi.stubGlobal('fetch', respond([{ status: 'ready', code: 'code-1' }]));
    exchangeCodeForSession.mockResolvedValue({ data: { session: null }, error: null });

    expect((await completeDesktopOAuth('google', () => {}, fakeClock())).status).toBe('failed');
  });

  it('fails when the provider cannot be started at all', async () => {
    signInWithOAuth.mockResolvedValue({
      data: null,
      error: { message: 'Unsupported provider: provider is not enabled' },
    });

    const outcome = await completeDesktopOAuth('apple', () => {}, fakeClock());

    expect(outcome).toMatchObject({ status: 'failed', code: 'oauth_start_failed' });
  });

  it('rides out a transient local read instead of calling the sign-in dead', async () => {
    let call = 0;
    vi.stubGlobal('fetch', vi.fn(async () => {
      call += 1;
      if (call === 1) throw new Error('connection refused');
      return { ok: true, json: async () => ({ ok: true, error: null, data: { status: 'ready', code: 'code-1' } }) };
    }));
    exchangeCodeForSession.mockResolvedValue({ data: { session: { access_token: 'at' } }, error: null });

    expect((await completeDesktopOAuth('google', () => {}, fakeClock())).status).toBe('signed_in');
  });
});
