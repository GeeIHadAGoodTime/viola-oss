import { useState, useEffect, useCallback, useRef } from 'react';
import { apiFetch } from './useViolaApi';
import { isFeatureAvailable } from '../utils/featureSurface';

const errorMessage = (error, fallback) => error?.message || (typeof error === 'string' ? error : fallback);

/**
 * Custom hook for device discovery and connection management.
 *
 * Polls GET /api/v1/devices/discovered every 5 seconds when enabled.
 * Provides connect, disconnect, and rename actions for discovered devices.
 *
 * @param {boolean} enabled - Whether to actively poll for devices
 * @returns {Object} Hook state and methods
 */
export function useDeviceDiscovery(enabled = false) {
  const [devices, setDevices] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const intervalRef = useRef(null);

  // Fetch discovered devices
  const fetchDevices = useCallback(async () => {
    // Discovery walks the user's own LAN from the desktop hub, and the routes
    // it polls are unserved on cloud (#3553), so the poll stays silent there.
    if (!isFeatureAvailable('rooms')) {
      setDevices([]);
      setLoading(false);
      setError(null);
      return;
    }
    try {
      const data = await apiFetch('/api/v1/devices/discovered');
      if (data.ok) {
        setDevices(data.data?.devices || []);
        setError(null);
      } else {
        setError(errorMessage(data.error, "Couldn't load rooms. Check your connection and try again."));
      }
    } catch {
      setError("Couldn't load rooms. Check your connection and try again.");
    } finally {
      setLoading(false);
    }
  }, []);

  // Connect a device as a room
  const connectDevice = useCallback(async (deviceId, roomName) => {
    try {
      setError(null);
      const data = await apiFetch(`/api/v1/devices/${deviceId}/connect`, {
        method: 'POST',
        body: JSON.stringify({ room_name: roomName }),
      });
      if (data.ok) {
        // Optimistic update: mark device as connected with new name
        setDevices(prev => prev.map(d =>
          d.device_id === deviceId ? { ...d, is_connected: true, name: roomName || d.name } : d
        ));
        return { ok: true, data: data.data };
      } else {
        const message = errorMessage(data.error, "Couldn't add this room. Make sure it is on the same network and try again.");
        setError(message);
        return { ok: false, error: message };
      }
    } catch {
      const message = "Couldn't add this room. Make sure it is on the same network and try again.";
      setError(message);
      return { ok: false, error: message };
    }
  }, []);

  // Disconnect a device
  const disconnectDevice = useCallback(async (deviceId) => {
    try {
      setError(null);
      const data = await apiFetch(`/api/v1/devices/${deviceId}/disconnect`, {
        method: 'POST',
      });
      if (data.ok) {
        // Optimistic update: mark device as disconnected
        setDevices(prev => prev.map(d =>
          d.device_id === deviceId ? { ...d, is_connected: false } : d
        ));
        return { ok: true };
      } else {
        const message = errorMessage(data.error, "Couldn't remove this room. Try again.");
        setError(message);
        return { ok: false, error: message };
      }
    } catch {
      const message = "Couldn't remove this room. Try again.";
      setError(message);
      return { ok: false, error: message };
    }
  }, []);

  // Rename a connected room
  const renameRoom = useCallback(async (deviceId, newName) => {
    try {
      setError(null);
      const data = await apiFetch(`/api/v1/rooms/${deviceId}`, {
        method: 'PATCH',
        body: JSON.stringify({ name: newName }),
      });
      if (data.ok) {
        // Optimistic update: update device name
        setDevices(prev => prev.map(d =>
          d.device_id === deviceId ? { ...d, name: newName } : d
        ));
        return { ok: true };
      } else {
        const message = errorMessage(data.error, "Couldn't rename this room. Try again.");
        setError(message);
        return { ok: false, error: message };
      }
    } catch {
      const message = "Couldn't rename this room. Try again.";
      setError(message);
      return { ok: false, error: message };
    }
  }, []);

  // Poll when enabled, stop when disabled or unmounted
  useEffect(() => {
    if (!enabled) {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
      return;
    }

    // Initial fetch
    setLoading(true);
    fetchDevices();

    // Poll every 5 seconds
    intervalRef.current = setInterval(fetchDevices, 5000);

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [enabled, fetchDevices]);

  return {
    devices,
    loading,
    error,
    connectDevice,
    disconnectDevice,
    renameRoom,
    refresh: fetchDevices,
    clearError: () => setError(null),
  };
}

export default useDeviceDiscovery;
