import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getClientApiKey, getClientApiKeySync, getCloudAccessToken } from '../config';
import { getGoTrueAccessToken } from '../lib/gotrue_client';
import { renderHook } from '@testing-library/react';
import { useViolaApi, authFetch, buildStreamUrl, sendCommandStreaming } from './useViolaApi';

vi.mock('../config', () => ({
  getClientApiKey: vi.fn(),
  getClientApiKeySync: vi.fn(),
  getCloudAccessToken: vi.fn(),
}));

vi.mock('../lib/gotrue_client', () => ({
  getGoTrueAccessToken: vi.fn(),
}));

describe('buildStreamUrl', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.__VIOLA_API_KEY__ = '';
    getClientApiKey.mockResolvedValue('');
    getClientApiKeySync.mockReturnValue('');
    getCloudAccessToken.mockReturnValue('');
    getGoTrueAccessToken.mockResolvedValue('');
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ data: { token: 'stream-token' } }),
    });
  });

  it('exchanges the desktop API key for a stream token instead of putting the key in the URL', async () => {
    getClientApiKeySync.mockReturnValue('desktop-api-key');

    const url = await buildStreamUrl('stream-1', { create: true });

    expect(url).toBe('/api/stream/stream-1?create=1&stream_token=stream-token');
    expect(url).not.toContain('api_key=');
    expect(global.fetch).toHaveBeenCalledWith(
      '/v1/ws/auth?purpose=sse&stream_id=stream-1',
      expect.objectContaining({
        headers: expect.objectContaining({
          'X-API-Key': 'desktop-api-key',
        }),
      }),
    );
  });

  it('fails closed instead of falling back to query API-key auth when token minting fails', async () => {
    getClientApiKeySync.mockReturnValue('desktop-api-key');
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      json: async () => ({}),
    });

    const url = await buildStreamUrl('stream-2');

    expect(url).toBe('/api/stream/stream-2');
    expect(url).not.toContain('api_key=');
  });
});

describe('authFetch — desktop calls never carry the cloud Bearer (issue #340)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.__VIOLA_API_KEY__ = '';
    getClientApiKey.mockResolvedValue('');
    getClientApiKeySync.mockReturnValue('');
    getCloudAccessToken.mockReturnValue('');
    getGoTrueAccessToken.mockResolvedValue('');
    global.fetch = vi.fn().mockResolvedValue({ status: 200, ok: true });
  });

  it('does NOT attach Authorization when a desktop API key is present, even with a live cloud session', async () => {
    getClientApiKeySync.mockReturnValue('desktop-api-key');
    getCloudAccessToken.mockReturnValue('raw-cloud-jwt');

    await authFetch('/v1/state');

    const [, options] = global.fetch.mock.calls[0];
    expect(options.headers['X-API-Key']).toBe('desktop-api-key');
    expect(options.headers.Authorization).toBeUndefined();
  });

  it('still attaches Authorization for the cloud/LAN web client (no desktop API key)', async () => {
    getClientApiKeySync.mockReturnValue('');
    getCloudAccessToken.mockReturnValue('raw-cloud-jwt');

    await authFetch('/v1/state');

    const [, options] = global.fetch.mock.calls[0];
    expect(options.headers['X-API-Key']).toBeUndefined();
    expect(options.headers.Authorization).toBe('Bearer raw-cloud-jwt');
  });
});

describe('authFetch — desktop Phone Terms double-submit CSRF', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('__VIOLA_API_KEY__', 'desktop-api-key');
    getCloudAccessToken.mockReturnValue('raw-cloud-jwt');
    getGoTrueAccessToken.mockResolvedValue('fallback-cloud-jwt');
    document.cookie = 'viola_csrf=; Max-Age=0; Path=/';
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ status: 200, ok: true }));
  });

  afterEach(() => {
    document.cookie = 'viola_csrf=; Max-Age=0; Path=/';
    vi.unstubAllGlobals();
  });

  it('echoes the readable cookie while retaining desktop auth and same-origin credentials', async () => {
    document.cookie = 'viola_csrf=desktop-csrf-token; Path=/';

    await authFetch('/v1/phone/accept-tos', { method: 'POST' });

    expect(global.fetch).toHaveBeenCalledExactlyOnceWith(
      '/v1/phone/accept-tos',
      expect.objectContaining({
        method: 'POST',
        credentials: 'same-origin',
        headers: expect.objectContaining({
          'X-API-Key': 'desktop-api-key',
          'X-CSRF-Token': 'desktop-csrf-token',
        }),
      }),
    );
    const [, { headers }] = global.fetch.mock.calls[0];
    expect(Object.keys(headers).some((name) => name.toLowerCase() === 'authorization')).toBe(false);
    expect(getCloudAccessToken).not.toHaveBeenCalled();
    expect(getGoTrueAccessToken).not.toHaveBeenCalled();
  });

  it('reads the latest token on every request after the cookie rotates', async () => {
    document.cookie = 'viola_csrf=first-token; Path=/';
    await authFetch('/v1/phone/accept-tos', { method: 'POST' });

    document.cookie = 'viola_csrf=rotated-token; Path=/';
    await authFetch('/v1/phone/accept-tos', { method: 'POST' });

    expect(global.fetch.mock.calls.map(([, options]) => options.headers['X-CSRF-Token']))
      .toEqual(['first-token', 'rotated-token']);
  });

  it.each(['X-CSRF-Token', 'x-csrf-token', 'X-CsRf-ToKeN'])(
    'preserves an explicit %s header without adding another or mutating the caller',
    async (headerName) => {
      document.cookie = 'viola_csrf=cookie-token; Path=/';
      const headers = { [headerName]: 'caller-token', 'Content-Type': 'application/json' };

      await authFetch('/v1/phone/accept-tos', { method: 'POST', headers });

      const [, options] = global.fetch.mock.calls[0];
      const csrfHeaders = Object.entries(options.headers)
        .filter(([name]) => name.toLowerCase() === 'x-csrf-token');
      expect(csrfHeaders).toEqual([[headerName, 'caller-token']]);
      expect(headers).toEqual({ [headerName]: 'caller-token', 'Content-Type': 'application/json' });
    },
  );

  it.each(['https://other.example/v1/phone/accept-tos', '//other.example/v1/phone/accept-tos'])(
    'does not leak the CSRF cookie to cross-origin URL %s',
    async (url) => {
      document.cookie = 'viola_csrf=local-only-token; Path=/';

      await authFetch(url, { method: 'POST' });

      const [requestUrl, options] = global.fetch.mock.calls[0];
      expect(requestUrl).toBe(url);
      expect(Object.keys(options.headers).some((name) => name.toLowerCase() === 'x-csrf-token')).toBe(false);
    },
  );

  it('does not invent an empty CSRF header when the cookie is missing', async () => {
    await authFetch('/v1/phone/accept-tos', { method: 'POST' });

    const [, options] = global.fetch.mock.calls[0];
    expect(options.headers['X-CSRF-Token']).toBeUndefined();
    expect(options.headers['X-API-Key']).toBe('desktop-api-key');
    expect(options.credentials).toBe('same-origin');
  });
});

describe('sendCommandStreaming', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.__VIOLA_API_KEY__ = '';
    getClientApiKey.mockResolvedValue('');
    getClientApiKeySync.mockReturnValue('desktop-api-key');
    getCloudAccessToken.mockReturnValue('');
    getGoTrueAccessToken.mockResolvedValue('');
    global.fetch = vi.fn((url) => {
      // resolveStreamAuthToken appends ?purpose=sse&stream_id=... (stream-scoped
      // SSE tokens, d4aea9804) — match the path, not the exact URL.
      if (url.startsWith('/v1/ws/auth')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ data: { token: 'stream-token' } }),
        });
      }
      return Promise.resolve({
        ok: true,
        json: async () => ({ ok: true, data: { message: 'done' } }),
        text: async () => '',
      });
    });
  });

  afterEach(() => {
    delete window.EventSource;
    delete global.EventSource;
  });

  it('closes failed EventSource connections so browser auto-retry cannot loop', async () => {
    const sources = [];

    class CapturingEventSource {
      constructor(url, options) {
        this.url = url;
        this.options = options;
        this.closed = false;
        sources.push(this);
        setTimeout(() => this.onerror?.(new Event('error')), 0);
      }

      close() {
        this.closed = true;
      }
    }

    window.EventSource = CapturingEventSource;
    global.EventSource = CapturingEventSource;

    const result = await sendCommandStreaming('hello', vi.fn());

    expect(result).toEqual({ message: 'done' });
    expect(sources).toHaveLength(1);
    expect(sources[0].url).toMatch(/^\/api\/stream\/.+\?create=1&stream_token=stream-token$/);
    expect(sources[0].closed).toBe(true);
    expect(global.fetch).toHaveBeenCalledWith(
      '/v1/command',
      expect.objectContaining({ method: 'POST' }),
    );
  });
});


describe('playback seek units', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getClientApiKey.mockResolvedValue('');
    getClientApiKeySync.mockReturnValue('');
    getCloudAccessToken.mockReturnValue('');
    getGoTrueAccessToken.mockResolvedValue('');
    global.fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ data: {} }) });
  });

  it.each([[0, 0], [3, 3000], [3.125, 3125], [119.9996, 120000]])(
    'converts UI %s seconds to backend %s milliseconds', async (seconds, milliseconds) => {
      const { result } = renderHook(() => useViolaApi());
      await result.current.seek(seconds);
      expect(global.fetch).toHaveBeenCalledWith('/v1/seek', expect.objectContaining({
        method: 'POST', body: JSON.stringify({ position: milliseconds }),
      }));
    },
  );
});
