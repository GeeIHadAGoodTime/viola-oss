// The settings payload carries `coming_soon` (owner-switched options) only while
// something is switched off; the hook hands it to Settings, empty otherwise.
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { useSettings } from './useSettings';

const apiFetch = vi.hoisted(() => vi.fn());

vi.mock('./useViolaApi', () => ({ apiFetch }));
vi.mock('./useWebSocket', () => ({ useWebSocket: () => {} }));
vi.mock('../components/auth/cloudSurface', () => ({ isCloudSurface: () => false }));

beforeEach(() => {
  apiFetch.mockReset();
});

describe('useSettings coming soon', () => {
  it('is empty when the payload has no coming_soon key', async () => {
    apiFetch.mockResolvedValue({ settings: { theme: 'dark' }, voice_status: null });
    const { result } = renderHook(() => useSettings());
    await waitFor(() => expect(result.current.hasSettingsSnapshot).toBe(true));
    expect(result.current.comingSoon).toEqual([]);
  });

  it('passes the switched-off options through', async () => {
    apiFetch.mockResolvedValue({ settings: {}, coming_soon: ['music.source.spotify'] });
    const { result } = renderHook(() => useSettings());
    await waitFor(() => expect(result.current.comingSoon).toEqual(['music.source.spotify']));
  });

  it('ignores an unreadable coming_soon value', async () => {
    apiFetch.mockResolvedValue({ settings: {}, coming_soon: 'music.source.spotify' });
    const { result } = renderHook(() => useSettings());
    await waitFor(() => expect(result.current.hasSettingsSnapshot).toBe(true));
    expect(result.current.comingSoon).toEqual([]);
  });
});
