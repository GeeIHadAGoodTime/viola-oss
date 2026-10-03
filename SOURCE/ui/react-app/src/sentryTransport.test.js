import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@sentry/react', () => ({
  init: vi.fn(),
  isInitialized: vi.fn(() => true),
  setTag: vi.fn(),
  setContext: vi.fn(),
}));

describe('desktop error capture without an external DSN', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    vi.stubEnv('VITE_SENTRY_DSN', '');
    delete window.__VIOLA_SENTRY__;
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true }));
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it('initializes with the existing capture-only fallback and drops the SDK event', async () => {
    const client = await import('./sentryClient');
    const sdk = await import('@sentry/react');
    expect(client.isSentryConfigured()).toBe(true);
    expect(client.initSentry()).toBe(true);
    const options = sdk.init.mock.calls[0][0];
    expect(options.dsn).toBe(client.INERT_LOCAL_DSN);
    expect(fetch).not.toHaveBeenCalled();
    expect(options.beforeSend({ exception: { values: [{ type: 'TypeError', value: 'render failed' }] } })).toBeNull();
    expect(fetch).toHaveBeenCalledTimes(1);
    const [path, request] = fetch.mock.calls[0];
    expect(path).toBe('/v1/diagnostics/ui-error');
    expect(request).toMatchObject({ method: 'POST', keepalive: true });
    expect(JSON.parse(request.body)).toMatchObject({ error_type: 'TypeError', error_value: 'render failed' });
  });

  it('leaves telemetry, replay, remote sessions and feedback disabled', async () => {
    const client = await import('./sentryClient');
    const sdk = await import('@sentry/react');
    client.initSentry();
    const options = sdk.init.mock.calls[0][0];
    expect(options).toMatchObject({
      sendDefaultPii: false,
      tracesSampleRate: 0,
      replaysSessionSampleRate: 0,
      replaysOnErrorSampleRate: 0,
    });
    expect(options.integrations([{ name: 'BrowserSession' }, { name: 'GlobalHandlers' }]))
      .toEqual([{ name: 'GlobalHandlers' }]);
    expect(client.enableSessionReplay()).toBe(false);
    expect(await client.openSentryUserFeedback()).toBe(false);
    expect(fetch).not.toHaveBeenCalled();
  });
});
