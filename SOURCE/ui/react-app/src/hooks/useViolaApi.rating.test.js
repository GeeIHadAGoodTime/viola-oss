import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, renderHook } from '@testing-library/react';

import { getClientApiKey, getClientApiKeySync, getCloudAccessToken } from '../config';
import { getGoTrueAccessToken } from '../lib/gotrue_client';
import { useViolaApi } from './useViolaApi';

vi.mock('../config', () => ({
  getClientApiKey: vi.fn(),
  getClientApiKeySync: vi.fn(),
  getCloudAccessToken: vi.fn(),
}));
vi.mock('../lib/gotrue_client', () => ({ getGoTrueAccessToken: vi.fn() }));

describe('rating target transport', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('__VIOLA_API_KEY__', '');
    getClientApiKey.mockResolvedValue('');
    getClientApiKeySync.mockReturnValue('');
    getCloudAccessToken.mockReturnValue('');
    getGoTrueAccessToken.mockResolvedValue('');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, video_id: 'track-a', rating: 'liked' }),
    }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it.each(['liked', 'disliked', null])('binds %s to the ID captured by the caller', async (rating) => {
    const { result } = renderHook(() => useViolaApi());
    await result.current.setRating(rating, 'track-a');
    expect(global.fetch).toHaveBeenCalledWith('/v1/rating', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ rating, expected_track_id: 'track-a' }),
    }));
  });

  it.each(['liked', 'disliked', null])('preserves the legacy one-argument %s body', async (rating) => {
    const { result } = renderHook(() => useViolaApi());
    await result.current.setRating(rating);
    expect(JSON.parse(global.fetch.mock.calls[0][1].body)).toEqual({ rating });
  });

  it('does not drop an explicitly invalid target into the legacy unbound contract', async () => {
    const { result } = renderHook(() => useViolaApi());
    await result.current.setRating('liked', null);
    expect(JSON.parse(global.fetch.mock.calls[0][1].body)).toEqual({
      rating: 'liked', expected_track_id: null,
    });
  });

  it('preserves the click-time target while authentication is delayed', async () => {
    let releaseAuth;
    getClientApiKey.mockReturnValue(new Promise((resolve) => { releaseAuth = resolve; }));
    const { result } = renderHook(() => useViolaApi());
    let displayedId = 'track-a';
    const pending = result.current.setRating('liked', displayedId);
    displayedId = 'track-b';
    expect(global.fetch).not.toHaveBeenCalled();
    releaseAuth('desktop-key');
    await pending;
    expect(displayedId).toBe('track-b');
    expect(JSON.parse(global.fetch.mock.calls[0][1].body)).toEqual({
      rating: 'liked', expected_track_id: 'track-a',
    });
  });

  it('propagates the stale-target failure status and code without retrying', async () => {
    global.fetch.mockResolvedValue({
      ok: false,
      status: 409,
      text: async () => JSON.stringify({
        ok: false, error: { code: 'rating_track_changed', message: 'Rating track changed.' },
      }),
    });
    const { result } = renderHook(() => useViolaApi());
    await expect(result.current.setRating('disliked', 'track-a')).rejects.toMatchObject({
      status: 409, code: 'rating_track_changed',
    });
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });
});
