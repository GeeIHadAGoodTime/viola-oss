import { useState, useEffect, useCallback } from 'react';
import { apiFetch } from './useViolaApi';
import { isFeatureAvailable } from '../utils/featureSurface';

const errorMessage = (error, fallback) => error?.message || (typeof error === 'string' ? error : fallback);

/**
 * Custom hook for room groups management (multi-room sync)
 *
 * The `/v1/rooms/*` group is not served on the cloud SPA
 * (backend/cloud_route_manifest.py "rooms" has no registration spec — it reads
 * the Tier-3 desktop room registry and drives LAN spokes), so on that surface
 * every call here 404s. The gate keeps this hook silent there; RoomGroupsModal
 * renders a DesktopUpsell instead (#3553).
 */
export function useRoomGroups() {
  const [groups, setGroups] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  // Fetch all room groups
  const fetchGroups = useCallback(async () => {
    if (!isFeatureAvailable('rooms')) {
      setGroups([]);
      setLoading(false);
      setError(null);
      return;
    }
    try {
      setLoading(true);
      const data = await apiFetch('/v1/rooms/groups');
      if (data.ok) {
        setGroups(data.data?.groups || []);
      } else {
        setError(errorMessage(data.error, "Couldn't load groups. Check your connection and try again."));
      }
    } catch {
      setError("Couldn't load groups. Check your connection and try again.");
    } finally {
      setLoading(false);
    }
  }, []);

  // Create a new room group
  const createGroup = useCallback(async (name, roomIds) => {
    try {
      setSaving(true);
      setError(null);
      const data = await apiFetch('/v1/rooms/groups', {
        method: 'POST',
        body: JSON.stringify({ name: name, room_ids: roomIds }),
      });
      if (data.ok) {
        setGroups(prev => [...prev, data.data.group]);
        return { ok: true, group: data.data.group };
      } else {
        const message = errorMessage(data.error, "Couldn't create this group. Check your connection and try again.");
        setError(message);
        return { ok: false, error: message };
      }
    } catch {
      const message = "Couldn't create this group. Check your connection and try again.";
      setError(message);
      return { ok: false, error: message };
    } finally {
      setSaving(false);
    }
  }, []);

  // Update a room group
  const updateGroup = useCallback(async (groupId, updates) => {
    try {
      setSaving(true);
      setError(null);
      const data = await apiFetch(`/v1/rooms/groups/${groupId}`, {
        method: 'PUT',
        body: JSON.stringify(updates),
      });
      if (data.ok) {
        setGroups(prev => prev.map(g => g.group_id === groupId ? data.data.group : g));
        return { ok: true, group: data.data.group };
      } else {
        const message = errorMessage(data.error, "Couldn't update this group. Check your connection and try again.");
        setError(message);
        return { ok: false, error: message };
      }
    } catch {
      const message = "Couldn't update this group. Check your connection and try again.";
      setError(message);
      return { ok: false, error: message };
    } finally {
      setSaving(false);
    }
  }, []);

  // Delete a room group
  const deleteGroup = useCallback(async (groupId) => {
    try {
      setSaving(true);
      setError(null);
      const data = await apiFetch(`/v1/rooms/groups/${groupId}`, {
        method: 'DELETE',
      });
      if (data.ok) {
        setGroups(prev => prev.filter(g => g.group_id !== groupId));
        return { ok: true };
      } else {
        const message = errorMessage(data.error, "Couldn't delete this group. Check your connection and try again.");
        setError(message);
        return { ok: false, error: message };
      }
    } catch {
      const message = "Couldn't delete this group. Check your connection and try again.";
      setError(message);
      return { ok: false, error: message };
    } finally {
      setSaving(false);
    }
  }, []);

  // Set master volume for a group
  const setMasterVolume = useCallback(async (groupId, volume) => {
    try {
      const data = await apiFetch(`/v1/rooms/groups/${groupId}/volume`, {
        method: 'POST',
        body: JSON.stringify({ volume }),
      });
      if (data.ok) {
        setGroups(prev => prev.map(g =>
          g.group_id === groupId ? { ...g, master_volume: volume } : g
        ));
        return { ok: true };
      }
      return { ok: false, error: data.error };
    } catch (err) {
      return { ok: false, error: "Couldn't set room volume. Check your connection and try again." };
    }
  }, []);

  // Set volume offset for a specific room in a group
  const setRoomVolume = useCallback(async (groupId, roomId, offset) => {
    try {
      const data = await apiFetch(`/v1/rooms/groups/${groupId}/rooms/${roomId}/volume`, {
        method: 'POST',
        body: JSON.stringify({ offset: offset }),
      });
      if (data.ok) {
        setGroups(prev => prev.map(g => {
          if (g.group_id !== groupId) return g;
          return {
            ...g,
            members: g.members.map(m =>
              m.room_id === roomId ? { ...m, volume_offset: offset } : m
            ),
          };
        }));
        return { ok: true };
      }
      return { ok: false, error: data.error };
    } catch (err) {
      return { ok: false, error: "Couldn't adjust room volume. Check your connection and try again." };
    }
  }, []);

  // Mute/unmute a specific room in a group
  const setRoomMute = useCallback(async (groupId, roomId, muted) => {
    try {
      const data = await apiFetch(`/v1/rooms/groups/${groupId}/rooms/${roomId}/mute`, {
        method: 'POST',
        body: JSON.stringify({ muted }),
      });
      if (data.ok) {
        setGroups(prev => prev.map(g => {
          if (g.group_id !== groupId) return g;
          return {
            ...g,
            members: g.members.map(m =>
              m.room_id === roomId ? { ...m, is_muted: muted } : m
            ),
          };
        }));
        return { ok: true };
      }
      return { ok: false, error: data.error };
    } catch (err) {
      return { ok: false, error: "Couldn't mute room. Check your connection and try again." };
    }
  }, []);

  // Calculate effective volume for a room
  const getEffectiveVolume = useCallback((groupId, roomId) => {
    const group = groups.find(g => g.group_id === groupId);
    if (!group) return 0;

    const member = group.members?.find(m => m.room_id === roomId);
    if (!member) return group.master_volume || 0;

    if (member.is_muted) return 0;

    const effective = (group.master_volume || 0) + (member.volume_offset || 0);
    return Math.max(0, Math.min(100, effective));
  }, [groups]);

  // Load groups on mount
  useEffect(() => {
    fetchGroups();
  }, [fetchGroups]);

  return {
    groups,
    loading,
    saving,
    error,
    createGroup,
    updateGroup,
    deleteGroup,
    setMasterVolume,
    setRoomVolume,
    setRoomMute,
    getEffectiveVolume,
    refreshGroups: fetchGroups,
    clearError: () => setError(null),
  };
}
