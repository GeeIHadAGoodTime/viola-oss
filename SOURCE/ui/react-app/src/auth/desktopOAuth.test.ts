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
  OAUTH_EXCHANGE_TIMEOUT_MS,
  OAUTH_POLL_MAX_INTERVAL_MS,
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

/**
 * Viola 1.0.5 laptop report (2026-10-28): "Continue with Google" sat on
 * "Please wait..." while the log filled with
 * `Rate limit exceeded ... path=/auth/desktop/oauth/result limit=30/60s`.
 * The webview polled every 1.2 s (50 a minute) into a 30-a-minute limiter that
 * also counts the requests it refuses, so after 36 s every poll was refused and
 * the finished result could never be read.
 */
describe('polling the local result', () => {
  /** A local server behind the old limiter: 30 a minute, rejections recorded. */
  function limitedServer(clock: ReturnType<typeof fakeClock>, readyAtMs: number) {
    const hits: number[] = [];
    let rejected = 0;
    const fetchMock = vi.fn(async () => {
      const t = clock.now();
      const recent = hits.filter((at) => t - at < 60_000);
      hits.push(t); // rejected requests count too
      if (recent.length >= 30) {
        rejected += 1;
        return { ok: false, status: 429, json: async () => ({ ok: false, error: { code: 'rate_limited' }, data: null }) };
      }
      const data = t >= readyAtMs ? { status: 'ready', code: 'code-late' } : { status: 'pending' };
      return { ok: true, status: 200, json: async () => ({ ok: true, error: null, data }) };
    });
    return { fetchMock, rejected: () => rejected };
  }

  it('still reads a result that lands after a minute, even behind a 30-a-minute limiter', async () => {
    const clock = fakeClock();
    const server = limitedServer(clock, 75_000);
    vi.stubGlobal('fetch', server.fetchMock);
    exchangeCodeForSession.mockResolvedValue({ data: { session: { access_token: 'at' } }, error: null });

    const outcome = await completeDesktopOAuth('google', () => {}, clock);

    expect(outcome.status).toBe('signed_in');
    expect(exchangeCodeForSession).toHaveBeenCalledWith('code-late');
    expect(server.rejected()).toBe(0);
  });

  it('backs off: a long wait costs a request every few seconds, not every second', async () => {
    const clock = fakeClock();
    const fetchMock = respond([{ status: 'pending' }]);
    vi.stubGlobal('fetch', fetchMock);

    const outcome = await completeDesktopOAuth('google', () => {}, clock);

    expect(outcome).toMatchObject({ status: 'failed', code: 'oauth_timeout' });
    const perMinute = fetchMock.mock.calls.length / (OAUTH_WAIT_TIMEOUT_MS / 60_000);
    expect(perMinute).toBeLessThanOrEqual(60_000 / OAUTH_POLL_MAX_INTERVAL_MS + 2);
    expect(perMinute).toBeLessThan(30);
  });

  it('slows right down after a 429 instead of hammering the limiter', async () => {
    const clock = fakeClock();
    const times: number[] = [];
    let call = 0;
    vi.stubGlobal('fetch', vi.fn(async () => {
      call += 1;
      times.push(clock.now());
      if (call === 1) return { ok: false, status: 429, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => ({ ok: true, error: null, data: { status: 'ready', code: 'c' } }) };
    }));
    exchangeCodeForSession.mockResolvedValue({ data: { session: { access_token: 'at' } }, error: null });

    expect((await completeDesktopOAuth('google', () => {}, clock)).status).toBe('signed_in');
    expect(times[1] - times[0]).toBeGreaterThanOrEqual(8_000);
  });

  it('stops when the result was already used, with something to retry', async () => {
    vi.stubGlobal('fetch', respond([{ status: 'consumed' }]));

    const outcome = await completeDesktopOAuth('google', () => {}, fakeClock());

    expect(outcome).toMatchObject({ status: 'failed', code: 'oauth_consumed' });
    expect(exchangeCodeForSession).not.toHaveBeenCalled();
  });

  it('stops polling the moment the app is signed in another way', async () => {
    const clock = fakeClock();
    const fetchMock = respond([{ status: 'pending' }]);
    vi.stubGlobal('fetch', fetchMock);
    let signedIn = false;

    const outcome = await completeDesktopOAuth('google', () => {}, {
      ...clock,
      wait: async (ms: number) => {
        await clock.wait(ms);
        if (clock.now() > 5_000) signedIn = true;
      },
      isSignedIn: () => signedIn,
    });

    expect(outcome).toEqual({ status: 'already_signed_in' });
    expect(fetchMock.mock.calls.length).toBeLessThan(6);
  });

  it('gives up on a hung exchange instead of waiting forever', async () => {
    vi.useFakeTimers();
    try {
      vi.stubGlobal('fetch', respond([{ status: 'ready', code: 'code-1' }]));
      exchangeCodeForSession.mockReturnValue(new Promise(() => {}));

      const pending = completeDesktopOAuth('google', () => {}, {
        now: () => 0,
        wait: async () => {},
      });
      await vi.advanceTimersByTimeAsync(OAUTH_EXCHANGE_TIMEOUT_MS + 1);

      expect(await pending).toMatchObject({ status: 'failed', code: 'oauth_exchange_timeout' });
    } finally {
      vi.useRealTimers();
    }
  });

  it('a poll request that never answers is abandoned, not awaited forever', async () => {
    vi.useFakeTimers();
    try {
      let call = 0;
      vi.stubGlobal('fetch', vi.fn((_url: string, init?: { signal?: AbortSignal }) => {
        call += 1;
        if (call === 1) {
          return new Promise((_resolve, reject) => {
            init?.signal?.addEventListener('abort', () => reject(new Error('aborted')));
          });
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ ok: true, error: null, data: { status: 'ready', code: 'code-1' } }),
        });
      }));
      exchangeCodeForSession.mockResolvedValue({ data: { session: { access_token: 'at' } }, error: null });

      const pending = completeDesktopOAuth('google', () => {}, { now: () => 0, wait: async () => {} });
      await vi.advanceTimersByTimeAsync(10_000);

      expect((await pending).status).toBe('signed_in');
      expect(call).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
