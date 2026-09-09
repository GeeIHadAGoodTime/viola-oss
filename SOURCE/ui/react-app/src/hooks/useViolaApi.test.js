import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getClientApiKey, getClientApiKeySync, getCloudAccessToken } from '../config';
import { getGoTrueAccessToken } from '../lib/gotrue_client';
import { authFetch, buildStreamUrl, sendCommandStreaming } from './useViolaApi';

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
