/**
 * Frontend oracle for the desktop phone-data routing (capstone, 2026-06-29).
 *
 * The desktop phone tab must hit its OWN LOCAL backend (window.__VIOLA_BASE_URL__
 * = localhost) with plain authFetch — NEVER a cross-origin cloud fetch and NEVER
 * a browser-held cloud bearer (SEC-017; the cloud bearer is kept out of the
 * React layer). The LOCAL backend proxies to the cloud with the server-side
 * bearer (telephony/desktop_cloud_proxy.py).
 *
 * The earlier version of this oracle asserted the OPPOSITE (a cross-origin
 * cloudPhoneFetch to <cloud>/api/phone/* with a Bearer header). That path was
 * dead on the desktop and was removed; these assertions replace it and lock in
 * the local-backend routing so the browser-bearer path can't return.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { fetchActiveCall, fetchCallHistory } from './useCallAudio';

vi.mock('../config', () => ({
  getClientApiKey: vi.fn().mockResolvedValue('desktop-api-key'),
  getClientApiKeySync: vi.fn().mockReturnValue('desktop-api-key'),
  getCloudAccessToken: vi.fn().mockReturnValue(''),
}));

vi.mock('../lib/gotrue_client', () => ({
  getGoTrueAccessToken: vi.fn().mockResolvedValue(''),
}));

// useViolaApi captures `BASE = window.__VIOLA_BASE_URL__` at module-eval time,
// so the base URL must exist BEFORE the import graph evaluates — a beforeEach
// assignment is too late (the test then asserts against relative URLs).
// vi.hoisted runs ahead of the hoisted imports, after the jsdom env + setup.js.
const LOCAL_BASE = vi.hoisted(() => {
  window.__VIOLA_BASE_URL__ = 'http://localhost:8756';
  return 'http://localhost:8756';
});

describe('desktop phone-data routing (local backend, no browser bearer)', () => {
  beforeEach(() => {
    window.__VIOLA_BASE_URL__ = LOCAL_BASE;
    window.__VIOLA_API_KEY__ = 'desktop-api-key'; // pragma: allowlist secret
    // A distinct cloud URL being present must NOT cause a cross-origin fetch.
    window.__VIOLA_CLOUD_URL__ = 'https://api.useviola.com';
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, data: { active_call: null, count: 0, calls: [] } }),
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    delete window.__VIOLA_BASE_URL__;
    delete window.__VIOLA_API_KEY__;
    delete window.__VIOLA_CLOUD_URL__;
  });

  it('fetchActiveCall hits the LOCAL /v1/calls/active, never the cloud origin', async () => {
    await fetchActiveCall();
    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [url, options] = global.fetch.mock.calls[0];
    expect(url).toBe(`${LOCAL_BASE}/v1/calls/active`);
    expect(url).not.toContain('api.useviola.com');
    expect(url).not.toContain('/api/phone/');
    // No browser-held cloud bearer: identity is the desktop API key, not a Bearer.
    const headers = options?.headers || {};
    expect(headers.Authorization || headers.authorization).toBeUndefined();
    expect(headers['X-API-Key']).toBe('desktop-api-key');
  });

  it('fetchCallHistory hits the LOCAL /v1/phone/history, never the cloud origin', async () => {
    await fetchCallHistory(50, 0);
    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [url, options] = global.fetch.mock.calls[0];
    expect(url).toContain(`${LOCAL_BASE}/v1/phone/history`);
    expect(url).not.toContain('api.useviola.com');
    const headers = options?.headers || {};
    expect(headers.Authorization || headers.authorization).toBeUndefined();
  });
});
