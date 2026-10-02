import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useDeviceDiscovery } from './useDeviceDiscovery';

vi.mock('../config', () => ({ getClientApiKey: async () => '', getClientApiKeySync: () => '', getCloudAccessToken: () => '' }));
vi.mock('../lib/gotrue_client', () => ({ getGoTrueAccessToken: async () => '' }));
vi.mock('../utils/featureSurface', () => ({ isFeatureAvailable: () => true }));
const response = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

beforeEach(() => { vi.stubGlobal('fetch', vi.fn()); });
afterEach(() => { vi.unstubAllGlobals(); });

describe('device discovery through the real apiFetch envelope contract', () => {
  it('shows successful discovery results after apiFetch unwraps the envelope', async () => {
    fetch.mockResolvedValue(response({ ok: true, data: { devices: [{ device_id: 'synthetic-room', name: 'Test room' }] } }));
    const { result } = renderHook(() => useDeviceDiscovery(true));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBeNull();
    expect(result.current.devices).toEqual([{ device_id: 'synthetic-room', name: 'Test room' }]);
  });
  it('treats an empty successful scan as empty rather than disconnected', async () => {
    fetch.mockResolvedValue(response({ ok: true, data: { devices: [], scanning: false } }));
    const { result } = renderHook(() => useDeviceDiscovery(true));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBeNull();
    expect(result.current.devices).toEqual([]);
  });
  it('retains a server failure and clears it only after a successful retry', async () => {
    fetch.mockResolvedValueOnce(response({ ok: false, error: { message: 'Discovery unavailable' } }));
    const { result } = renderHook(() => useDeviceDiscovery(true));
    await waitFor(() => expect(result.current.error).toBe('Discovery unavailable'));
    fetch.mockResolvedValueOnce(response({ ok: true, data: { devices: [] } }));
    await act(async () => { await result.current.refresh(); });
    expect(result.current.error).toBeNull();
  });
  it('does not report malformed success data as a successful scan', async () => {
    fetch.mockResolvedValue(response({ ok: true, data: { unexpected: true } }));
    const { result } = renderHook(() => useDeviceDiscovery(true));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBeTruthy();
  });
  it('connects, renames and disconnects using real unwrapped success payloads', async () => {
    fetch.mockResolvedValueOnce(response({ ok: true, data: { devices: [{ device_id: 'synthetic-room', name: 'Original', is_connected: false }] } }));
    const { result } = renderHook(() => useDeviceDiscovery(true));
    await waitFor(() => expect(result.current.loading).toBe(false));
    fetch.mockResolvedValueOnce(response({ ok: true, data: { device_id: 'synthetic-room', room_id: 'synthetic-room', room_name: 'Study' } }));
    let outcome;
    await act(async () => { outcome = await result.current.connectDevice('synthetic-room', 'Study'); });
    expect(outcome.ok).toBe(true);
    expect(outcome.data.device_id).toBe('synthetic-room');
    expect(result.current.devices[0]).toMatchObject({ name: 'Study', is_connected: true });
    fetch.mockResolvedValueOnce(response({ ok: true, data: { room_id: 'synthetic-room', name: 'Office' } }));
    await act(async () => { outcome = await result.current.renameRoom('synthetic-room', 'Office'); });
    expect(outcome.ok).toBe(true);
    expect(result.current.devices[0].name).toBe('Office');
    fetch.mockResolvedValueOnce(response({ ok: true, data: { device_id: 'synthetic-room', disconnected: true } }));
    await act(async () => { outcome = await result.current.disconnectDevice('synthetic-room'); });
    expect(outcome.ok).toBe(true);
    expect(result.current.devices[0].is_connected).toBe(false);
  });
  it('does not mark a denied connection as connected', async () => {
    fetch.mockResolvedValueOnce(response({ ok: true, data: { devices: [{ device_id: 'synthetic-room', is_connected: false }] } }));
    const { result } = renderHook(() => useDeviceDiscovery(true));
    await waitFor(() => expect(result.current.loading).toBe(false));
    fetch.mockResolvedValueOnce(response({ ok: false, error: { code: 'forbidden', message: 'Denied' } }, 403));
    let outcome;
    await act(async () => { outcome = await result.current.connectDevice('synthetic-room', 'Study'); });
    expect(outcome.ok).toBe(false);
    expect(result.current.devices[0].is_connected).toBe(false);
    expect(result.current.error).toBeTruthy();
  });

});
