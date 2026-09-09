// Proof for LEVERAGE_RANKING quick-kill #4 (source: docs/audit/silent_failures.md F4):
// useVoice.js:386 - cancelRecording()'s unduck POST had `.catch(() => {})`, so a
// failed unduck request left audio silently stuck ducked (quiet) with zero
// diagnostic trail. This proves the failure is now logged via console.error
// instead of being silently dropped.

import { renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./useViolaApi', () => ({
  authFetch: vi.fn(),
}));

describe('useVoice cancelRecording unduck failure logging', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('logs to console when the post-cancel unduck request fails, instead of silently dropping it', async () => {
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { authFetch } = await import('./useViolaApi');
    authFetch.mockRejectedValue(new Error('simulated /v1/audio/unduck failure'));

    const { useVoice } = await import('./useVoice');
    const { result } = renderHook(() => useVoice());

    result.current.cancelRecording();

    // Flush the microtask queue so the rejected authFetch promise's .catch runs.
    await new Promise((resolve) => setTimeout(resolve, 0));
    await Promise.resolve();
    await Promise.resolve();

    expect(authFetch).toHaveBeenCalledWith('/v1/audio/unduck', { method: 'POST' });
    expect(consoleErrorSpy).toHaveBeenCalledWith(
      '[useVoice] Unduck request failed; audio may stay ducked (quiet):',
      expect.any(Error)
    );
  });
});
