/**
 * A failed shuffle has to be able to reach the caller that flipped the toggle.
 *
 * #4214. `SmartDisplay` flips the shuffle button optimistically and then calls
 * `api.setShuffle(next)`. For that flip to be reversible, two things have to be
 * true, and the first one lives here: the rejection has to carry the server's
 * own answer. `POST /v1/shuffle` reorders the queue before it records the
 * preference, so when the reorder fails the preference never moved, and the
 * route says so in its error envelope's `data.shuffle`. If `apiFetch` throws
 * that away -- as it did before this fix, keeping only `status` and `code` --
 * the caller has nothing to roll back to except a guess.
 *
 * The reason a rollback is needed at all, rather than waiting for the next
 * state broadcast to correct it: SmartDisplay's reconcile effect is keyed on
 * `playerState.shuffle`, so it only re-runs when the server's value *changes*.
 * A failed request never changes it, so the effect never fires and the toggle
 * stays diverged until some other actor moves shuffle.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { getClientApiKey, getClientApiKeySync, getCloudAccessToken } from '../config';
import { getGoTrueAccessToken } from '../lib/gotrue_client';
import { apiFetch } from './useViolaApi';

vi.mock('../config', () => ({
  getClientApiKey: vi.fn(),
  getClientApiKeySync: vi.fn(),
  getCloudAccessToken: vi.fn(),
}));

vi.mock('../lib/gotrue_client', () => ({
  getGoTrueAccessToken: vi.fn(),
}));

function respondWith(status, body) {
  global.fetch = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
    json: async () => body,
  });
}

describe('apiFetch failure envelopes', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.__VIOLA_API_KEY__ = '';
    getClientApiKey.mockResolvedValue('');
    getClientApiKeySync.mockReturnValue('');
    getCloudAccessToken.mockReturnValue('');
    getGoTrueAccessToken.mockResolvedValue('');
  });

  it('carries the server state a failed shuffle reports, so the toggle can roll back to the truth', async () => {
    respondWith(500, {
      ok: false,
      error: { code: 'shuffle_queue_failed', message: 'Shuffle could not be changed right now.' },
      data: { shuffle: false },
    });

    const err = await apiFetch('/v1/shuffle', {
      method: 'POST',
      body: JSON.stringify({ enabled: true }),
    }).then(
      () => null,
      (thrown) => thrown,
    );

    expect(err).toBeInstanceOf(Error);
    expect(err.status).toBe(500);
    expect(err.code).toBe('shuffle_queue_failed');
    expect(err.data).toEqual({ shuffle: false });
  });

  it('still reports code and status when a failure carries no data', async () => {
    respondWith(403, { ok: false, error: { code: 'consent_required' } });

    const err = await apiFetch('/v1/shuffle', { method: 'POST' }).catch((thrown) => thrown);

    expect(err.status).toBe(403);
    expect(err.code).toBe('consent_required');
    expect(err.data).toBeNull();
  });

  it('degrades to nulls rather than throwing again on a non-JSON error body', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 502,
      text: async () => '<html>gateway</html>',
    });

    const err = await apiFetch('/v1/shuffle', { method: 'POST' }).catch((thrown) => thrown);

    expect(err.status).toBe(502);
    expect(err.code).toBeNull();
    expect(err.data).toBeNull();
  });
});
