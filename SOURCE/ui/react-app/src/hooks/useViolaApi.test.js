import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getClientApiKey, getClientApiKeySync, getCloudAccessToken } from '../config';
import { getGoTrueAccessToken } from '../lib/gotrue_client';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { useRoomGroups } from './useRoomGroups';
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


describe('room groups through the real API envelope boundary', () => {
  const group = { group_id: 'g1', group_name: 'Studio', master_volume: 60,
    members: [{ room_id: 'local', volume_offset: 0, is_muted: false }] };
  const response = (data) => ({ ok: true, status: 200, json: async () => ({ ok: true, data }) });
  let stored;

  beforeEach(() => {
    vi.clearAllMocks();
    window.viola = {};
    window.__VIOLA_API_KEY__ = '';
    getClientApiKey.mockResolvedValue('');
    getClientApiKeySync.mockReturnValue('');
    getCloudAccessToken.mockReturnValue('');
    getGoTrueAccessToken.mockResolvedValue('');
    stored = [];
    global.fetch = vi.fn(async (_url, options = {}) => {
      if (options.method === 'POST') {
        stored.push(group);
        return response({ group });
      }
      return response({ groups: [...stored], count: stored.length });
    });
  });

  afterEach(() => { cleanup(); delete window.viola; vi.useRealTimers(); });

  async function mountGroups() {
    const hook = renderHook(() => useRoomGroups());
    await waitFor(() => expect(hook.result.current.loading).toBe(false));
    return hook;
  }

  it('loads an empty or populated durable list without a false error', async () => {
    stored = [group];
    const { result } = await mountGroups();
    expect(result.current.error).toBeNull();
    expect(result.current.groups).toEqual([group]);
  });

  it('reports one successful create, keeps it after refresh, and needs no duplicate retry', async () => {
    const { result } = await mountGroups();
    let answer;
    await act(async () => { answer = await result.current.createGroup('Studio', ['local']); });
    expect(answer).toEqual({ ok: true, group });
    expect(result.current.error).toBeNull();
    expect(result.current.groups).toEqual([group]);
    await act(async () => { await result.current.refreshGroups(); });
    expect(result.current.groups).toEqual([group]);
    expect(stored).toEqual([group]);
    expect(global.fetch.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1);
  });

  it('updates and deletes the actual group instead of reporting durable mutations as failures', async () => {
    stored = [group];
    const { result } = await mountGroups();
    const renamed = { ...group, group_name: 'Office' };
    global.fetch.mockResolvedValueOnce(response({ group: renamed }));
    let answer;
    await act(async () => { answer = await result.current.updateGroup('g1', { group_name: 'Office' }); });
    expect(answer).toEqual({ ok: true, group: renamed });
    expect(result.current.groups).toEqual([renamed]);
    global.fetch.mockResolvedValueOnce(response({ deleted: true, group_id: 'g1' }));
    await act(async () => { answer = await result.current.deleteGroup('g1'); });
    expect(answer).toEqual({ ok: true });
    expect(result.current.groups).toEqual([]);
  });

  it('accepts master volume, room offset and mute acknowledgments including zero and false', async () => {
    stored = [group];
    const { result } = await mountGroups();
    let answer;
    global.fetch.mockResolvedValueOnce(response({ group: { ...group, master_volume: 0 }, master_volume: 0 }));
    await act(async () => { answer = await result.current.setMasterVolume('g1', 0); });
    expect(answer).toEqual({ ok: true, value: 0, uiCurrent: true });
    expect(result.current.groups[0].master_volume).toBe(0);
    global.fetch.mockResolvedValueOnce(response({ room_id: 'local', offset: 0, effective_volume: 0 }));
    await act(async () => { answer = await result.current.setRoomVolume('g1', 'local', 0); });
    expect(answer).toEqual({ ok: true, value: 0, uiCurrent: true });
    for (const muted of [true, false]) {
      global.fetch.mockResolvedValueOnce(response({ room_id: 'local', is_muted: muted, effective_volume: 0 }));
      await act(async () => { answer = await result.current.setRoomMute('g1', 'local', muted); });
      expect(answer).toEqual({ ok: true });
      expect(result.current.groups[0].members[0].is_muted).toBe(muted);
    }
  });

  it('uses acknowledged control values rather than repeating the request as durable state', async () => {
    stored = [group];
    const { result } = await mountGroups();
    global.fetch.mockResolvedValueOnce(response({ group: { ...group, master_volume: 80 }, master_volume: 80 }));
    await act(async () => { expect(await result.current.setMasterVolume('g1', 84)).toEqual({ ok: true, value: 80, uiCurrent: true }); });
    expect(result.current.groups[0].master_volume).toBe(80);
    global.fetch.mockResolvedValueOnce(response({ room_id: 'local', offset: 8, effective_volume: 88 }));
    await act(async () => { expect(await result.current.setRoomVolume('g1', 'local', 10)).toEqual({ ok: true, value: 8, uiCurrent: true }); });
    expect(result.current.groups[0].members[0].volume_offset).toBe(8);
    global.fetch.mockResolvedValueOnce(response({ room_id: 'local', is_muted: false, effective_volume: 88 }));
    await act(async () => { await result.current.setRoomMute('g1', 'local', true); });
    expect(result.current.groups[0].members[0].is_muted).toBe(false);
  });

  it('rejects update and delete acknowledgments belonging to another group', async () => {
    stored = [group];
    const { result } = await mountGroups();
    let answer;
    global.fetch.mockResolvedValueOnce(response({ group: { ...group, group_id: 'other' } }));
    await act(async () => { answer = await result.current.updateGroup('g1', { group_name: 'Office' }); });
    expect(answer.ok).toBe(false);
    expect(result.current.groups).toEqual([group]);
    global.fetch.mockResolvedValueOnce(response({ deleted: true, group_id: 'other' }));
    await act(async () => { answer = await result.current.deleteGroup('g1'); });
    expect(answer.ok).toBe(false);
    expect(result.current.groups).toEqual([group]);
  });

  it.each(['master', 'volume', 'mute'])('REVIEW rejects %s controls acknowledged for another target', async (kind) => {
    stored = [group];
    const { result } = await mountGroups();
    let answer;
    const payloads = {
      master: { group: { ...group, group_id: 'other-group', master_volume: 23 }, master_volume: 23 },
      volume: { room_id: 'other-room', offset: 8, effective_volume: 68 },
      mute: { room_id: 'other-room', is_muted: true, effective_volume: 0 },
    };
    global.fetch.mockResolvedValueOnce(response(payloads[kind]));
    await act(async () => {
      answer = kind === 'master' ? await result.current.setMasterVolume('g1', 23)
        : kind === 'volume' ? await result.current.setRoomVolume('g1', 'local', 8)
          : await result.current.setRoomMute('g1', 'local', true);
    });
    expect(answer.ok).toBe(false);
    expect(result.current.groups).toEqual([group]);
  });

  it('clears a real list failure after a successful explicit retry', async () => {
    global.fetch.mockRejectedValueOnce(new Error('offline'));
    const { result } = await mountGroups();
    expect(result.current.error).toMatch(/load groups/);
    await act(async () => { await result.current.refreshGroups(); });
    expect(result.current.error).toBeNull();
    expect(result.current.groups).toEqual([]);
  });

  it.each(['http', 'envelope', 'malformed'])('does not mutate or claim create success for %s failures', async (kind) => {
    const { result } = await mountGroups();
    const failure = { ok: false, error: { code: 'create_failed', message: 'Create failed' } };
    global.fetch.mockResolvedValueOnce(kind === 'http'
      ? { ok: false, status: 500, text: async () => JSON.stringify(failure) }
      : { ok: true, status: 200, json: async () => kind === 'envelope' ? failure : { ok: true, data: {} } });
    let answer;
    await act(async () => { answer = await result.current.createGroup('Studio', ['local']); });
    expect(answer.ok).toBe(false);
    expect(result.current.error).toBeTruthy();
    expect(result.current.groups).toEqual([]);
    expect(result.current.saving).toBe(false);
  });

  const volumeReply = (kind, value, groupId = 'g1') => kind === 'master'
    ? { group: { ...group, group_id: groupId, master_volume: value }, master_volume: value }
    : { room_id: 'local', offset: value };
  const sendVolume = (current, kind, value, groupId = 'g1') => kind === 'master'
    ? current.setMasterVolume(groupId, value) : current.setRoomVolume(groupId, 'local', value);
  const readVolume = (current, kind, groupId = 'g1') => {
    const row = current.groups.find(item => item.group_id === groupId);
    return kind === 'master' ? row.master_volume : row.members[0].volume_offset;
  };
  const deferredResponse = () => {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return { promise, resolve };
  };
  it.each(['master', 'volume'].flatMap(kind => ['none', 'pending', 'accepted'].map(newer => ({ kind, newer }))))(
    'retires timed-out $kind parent publication with newer request $newer', async ({ kind, newer }) => {
      stored = [group];
      const { result } = await mountGroups();
      vi.useFakeTimers();
      const old = deferredResponse(), next = deferredResponse();
      global.fetch.mockReturnValueOnce(old.promise);
      let first, second;
      await act(async () => { first = sendVolume(result.current, kind, 10); });
      await act(async () => vi.advanceTimersByTime(15000));
      if (newer !== 'none') {
        global.fetch.mockReturnValueOnce(next.promise);
        await act(async () => { second = sendVolume(result.current, kind, 20); });
        if (newer === 'accepted') await act(async () => { next.resolve(response(volumeReply(kind, 20))); await second; });
      }
      await act(async () => { old.resolve(response(volumeReply(kind, 10))); expect(await first).toEqual({ ok: true, value: 10, uiCurrent: false }); });
      expect(readVolume(result.current, kind)).toBe(newer === 'accepted' ? 20 : kind === 'master' ? 60 : 0);
      if (newer === 'pending') {
        await act(async () => { next.resolve(response(volumeReply(kind, 20))); await second; });
        expect(readVolume(result.current, kind)).toBe(20);
      }
    },
  );
  it.each(['master', 'volume'])('does not overwrite a newer acknowledgement before expiry for %s', async kind => {
    stored = [group];
    const { result } = await mountGroups();
    const old = deferredResponse();
    global.fetch.mockReturnValueOnce(old.promise);
    let first;
    await act(async () => { first = sendVolume(result.current, kind, 10); });
    global.fetch.mockResolvedValueOnce(response(volumeReply(kind, 20)));
    await act(async () => { await sendVolume(result.current, kind, 20); });
    await act(async () => { old.resolve(response(volumeReply(kind, 10))); await first; });
    expect(readVolume(result.current, kind)).toBe(20);
  });
  it.each(['master', 'volume'])('still publishes a timely response immediately before expiry for %s', async kind => {
    stored = [group];
    const { result } = await mountGroups();
    vi.useFakeTimers();
    const request = deferredResponse();
    global.fetch.mockReturnValueOnce(request.promise);
    let pending;
    await act(async () => { pending = sendVolume(result.current, kind, 10); });
    await act(async () => vi.advanceTimersByTime(14999));
    await act(async () => { request.resolve(response(volumeReply(kind, 10))); await pending; });
    expect(readVolume(result.current, kind)).toBe(10);
  });
  it.each(['different control', 'different group'])('keeps volume request ownership independent for %s', async scope => {
    stored = [group, { ...group, group_id: 'g2' }];
    const { result } = await mountGroups();
    const first = deferredResponse(), second = deferredResponse();
    global.fetch.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const nextKind = scope === 'different control' ? 'volume' : 'master';
    const nextGroup = scope === 'different group' ? 'g2' : 'g1';
    let one, two;
    await act(async () => { one = sendVolume(result.current, 'master', 30); two = sendVolume(result.current, nextKind, 40, nextGroup); });
    await act(async () => { second.resolve(response(volumeReply(nextKind, 40, nextGroup))); await two; });
    await act(async () => { first.resolve(response(volumeReply('master', 30))); await one; });
    expect(readVolume(result.current, 'master')).toBe(30);
    expect(readVolume(result.current, nextKind, nextGroup)).toBe(40);
  });

});
