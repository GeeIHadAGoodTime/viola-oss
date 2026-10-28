import { useState, useEffect, useCallback, useRef } from 'react';
import { apiFetch } from './useViolaApi';
import { isFeatureAvailable } from '../utils/featureSurface';

const errorMessage = (error, fallback) => error?.message || (typeof error === 'string' ? error : fallback);

// Speakers paired to this hub (a phone or browser that joined with a pairing
// code) are rooms in the hub's registry, not devices found by LAN discovery,
// so the discovery list alone never showed them: a joined "Default" speaker
// was missing from Rooms and could not be removed (1.0.5 laptop A7.6).
const pairedSpeakersFrom = (data) => {
  const rooms = Array.isArray(data?.rooms) ? data.rooms : (Array.isArray(data) ? data : []);
  return rooms
    .filter((room) => room && !room.is_local && !room.is_hub && (room.id || room.room_id))
    .map((room) => {
      const id = room.id || room.room_id;
      return {
        device_id: id,
        name: room.name || room.room_name || id,
        host: room.host || null,
        is_connected: true,
        kind: 'speaker',
        status: room.status || 'unknown',
      };
    });
};

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
  // device_id -> 'speaker' for paired speakers, so removal uses the room route.
  const kindsRef = useRef(new Map());

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
      if (data?.ok !== false && Array.isArray(data?.devices)) {
        const discovered = data.devices;
        let roomsData = null;
        try {
          roomsData = await apiFetch('/api/v1/rooms');
        } catch {
          roomsData = null; // paired speakers are extra; discovery still shows
        }
        const seen = new Set(discovered.map((d) => d.device_id));
        const speakers = roomsData?.ok === false
          ? []
          : pairedSpeakersFrom(roomsData).filter((s) => !seen.has(s.device_id));
        kindsRef.current = new Map(speakers.map((s) => [s.device_id, 'speaker']));
        setDevices([...discovered, ...speakers]);
        setError(null);
      } else {
        setError(errorMessage(data?.error, "Couldn't load rooms. Check your connection and try again."));
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
      if (data?.ok !== false && data?.device_id === deviceId) {
        // Optimistic update: mark device as connected with new name
        setDevices(prev => prev.map(d =>
          d.device_id === deviceId ? { ...d, is_connected: true, name: roomName || d.name } : d
        ));
        return { ok: true, data };
      } else {
        const message = errorMessage(data?.error, "Couldn't add this room. Make sure it is on the same network and try again.");
        setError(message);
        return { ok: false, error: message };
      }
    } catch {
      const message = "Couldn't add this room. Make sure it is on the same network and try again.";
      setError(message);
      return { ok: false, error: message };
    }
  }, []);

  // Remove a paired speaker: deletes its room and revokes its pairing.
  const removeSpeaker = useCallback(async (deviceId) => {
    try {
      setError(null);
      const data = await apiFetch(`/api/v1/rooms/${encodeURIComponent(deviceId)}`, {
        method: 'DELETE',
      });
      if (data?.ok !== false && data?.deleted === deviceId) {
        kindsRef.current.delete(deviceId);
        setDevices(prev => prev.filter(d => d.device_id !== deviceId));
        return { ok: true };
      }
      const message = errorMessage(data?.error, "Couldn't remove this speaker. Try again.");
      setError(message);
      return { ok: false, error: message };
    } catch {
      const message = "Couldn't remove this speaker. Try again.";
      setError(message);
      return { ok: false, error: message };
    }
  }, []);

  // Disconnect a device
  const disconnectDevice = useCallback(async (deviceId) => {
    if (kindsRef.current.get(deviceId) === 'speaker') {
      return removeSpeaker(deviceId);
    }
    try {
      setError(null);
      const data = await apiFetch(`/api/v1/devices/${deviceId}/disconnect`, {
        method: 'POST',
      });
      if (data?.ok !== false && data?.device_id === deviceId && data?.disconnected === true) {
        // Optimistic update: mark device as disconnected
        setDevices(prev => prev.map(d =>
          d.device_id === deviceId ? { ...d, is_connected: false } : d
        ));
        return { ok: true };
      } else {
        const message = errorMessage(data?.error, "Couldn't remove this room. Try again.");
        setError(message);
        return { ok: false, error: message };
      }
    } catch {
      const message = "Couldn't remove this room. Try again.";
      setError(message);
      return { ok: false, error: message };
    }
  }, [removeSpeaker]);

  // Rename a connected room
  const renameRoom = useCallback(async (deviceId, newName) => {
    try {
      setError(null);
      const data = await apiFetch(`/api/v1/rooms/${deviceId}`, {
        method: 'PATCH',
        body: JSON.stringify({ name: newName }),
      });
      if (data?.ok !== false && data?.room_id === deviceId && data?.name === newName) {
        // Optimistic update: update device name
        setDevices(prev => prev.map(d =>
          d.device_id === deviceId ? { ...d, name: newName } : d
        ));
        return { ok: true };
      } else {
        const message = errorMessage(data?.error, "Couldn't rename this room. Try again.");
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
