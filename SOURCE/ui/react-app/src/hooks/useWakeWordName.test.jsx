import { describe, expect, it, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import useWakeWordName from './useWakeWordName';
import { authFetch } from './useViolaApi';
import { isFeatureHidden } from '../utils/featureSurface';

vi.mock('./useViolaApi', () => ({ authFetch: vi.fn() }));
vi.mock('../utils/featureSurface', () => ({ isFeatureHidden: vi.fn(() => false) }));

const models = (rows) => Promise.resolve({ json: async () => ({ data: { models: rows } }) });

describe('useWakeWordName', () => {
  beforeEach(() => {
    authFetch.mockReset();
    isFeatureHidden.mockReturnValue(false);
  });

  it('names the word the user trained', async () => {
    authFetch.mockReturnValue(models([{ name: 'Viola' }, { name: 'zephyra', is_active: true }]));
    const { result } = renderHook(() => useWakeWordName());
    await waitFor(() => expect(result.current).toBe('Zephyra'));
  });

  it('says Viola when that is the active word', async () => {
    authFetch.mockReturnValue(models([{ name: 'Viola', is_active: true }]));
    const { result } = renderHook(() => useWakeWordName());
    await waitFor(() => expect(authFetch).toHaveBeenCalled());
    expect(result.current).toBe('Viola');
  });

  it('falls back to Viola when the list cannot be read', async () => {
    authFetch.mockRejectedValue(new Error('offline'));
    const { result } = renderHook(() => useWakeWordName());
    await waitFor(() => expect(authFetch).toHaveBeenCalled());
    expect(result.current).toBe('Viola');
  });

  it('asks nothing on the cloud, where the word is always Viola', () => {
    isFeatureHidden.mockReturnValue(true);
    const { result } = renderHook(() => useWakeWordName());
    expect(result.current).toBe('Viola');
    expect(authFetch).not.toHaveBeenCalled();
  });
});
