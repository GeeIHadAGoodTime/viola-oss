import { useState, useEffect, useCallback } from 'react';
import { apiFetch } from './useViolaApi';
import { isFeatureAvailable } from '../utils/featureSurface';

// Multi-room speaker sync management is hidden only on the credential-less
// cloud SPA. Hub-backed spokes authenticate to the desktop hub and use the
// same rooms routes as the desktop SmartDisplay.

/**
 * Custom hook for fetching available rooms for multi-room sync
 *
 * @returns {Object} Hook state and methods
 * @property {Array} rooms - Array of available room objects
 * @property {boolean} loading - Whether rooms are being fetched
 * @property {string|null} error - Error message if fetch failed
 * @property {Function} refresh - Function to manually refresh rooms list
 */
export function useAvailableRooms() {
  const [rooms, setRooms] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Fetch available rooms from the API
  const fetchRooms = useCallback(async () => {
    if (!isFeatureAvailable('rooms')) {
      setRooms([]);
      setLoading(false);
      setError(null);
      return;
    }
    try {
      setLoading(true);
      setError(null);
      const data = await apiFetch('/api/v1/rooms');
      const normalizedRooms = (data?.rooms || []).map(room => ({
        id: room.id || room.room_id,
        name: room.name || room.display_name || 'Unknown Room',
        device_id: room.device_id || room.id,
        is_local: Boolean(room.is_local),
        status: room.status || (room.online ? 'online' : 'offline'),
      }));
      setRooms(normalizedRooms);
    } catch (err) {
      setError('Failed to load available rooms');
    } finally {
      setLoading(false);
    }
  }, []);

  // Auto-refresh on mount
  useEffect(() => {
    fetchRooms();
  }, [fetchRooms]);

  return {
    rooms,
    loading,
    error,
    refresh: fetchRooms,
  };
}

export default useAvailableRooms;
