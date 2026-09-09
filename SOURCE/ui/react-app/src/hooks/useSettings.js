import { useState, useEffect, useCallback } from 'react';
import { apiFetch } from './useViolaApi';
import { useWebSocket } from './useWebSocket';
import { isCloudSurface } from '../components/auth/cloudSurface';

// On the cloud browser SPA the desktop `/v1/settings` route is not served
// (it would custody desktop/global SettingsManager state). The cloud-native,
// credential-less equivalent is `/api/v1/cloud/settings` (Tier-2, consent-
// gated, RLS-scoped). Reads return `{settings: {...}}`; writes are per-key
// PUTs to `/api/v1/cloud/settings/{key}` with `{value}`. The surface is read
// at call time so the runtime surface (not import order) decides the route.

async function fetchSettingsPayload() {
  if (isCloudSurface()) {
    // apiFetch unwraps the success envelope -> { settings: {...} }.
    return apiFetch('/api/v1/cloud/settings');
  }
  return apiFetch('/v1/settings');
}

async function pushSettingsPayload(newSettings) {
  if (isCloudSurface()) {
    // Cloud settings are written one key at a time. Apply each in turn, then
    // return the freshly merged settings so callers see the post-write state.
    for (const [key, value] of Object.entries(newSettings || {})) {
      await apiFetch(`/api/v1/cloud/settings/${encodeURIComponent(key)}`, {
        method: 'PUT',
        body: JSON.stringify({ value }),
      });
    }
    return fetchSettingsPayload();
  }
  return apiFetch('/v1/settings', {
    method: 'POST',
    body: JSON.stringify({ settings: newSettings }),
  });
}

// Failure codes the settings write can answer with that the user can actually
// act on, so the footer says what happened instead of "Failed to save settings".
// Both come from the cloud-sync consent mirror (#4789): the desktop toggle now
// has to move the authoritative cloud row before anything is stored locally, so
// when that write does not land NOTHING is saved, and saying so is the point —
// a consent control that reported success without recording consent is the bug
// being fixed.
const SETTINGS_ERROR_BY_CODE = {
  cloud_sync_consent_signin_required:
    'Sign in to your Viola account before changing cloud sync. Nothing was saved.',
  cloud_sync_consent_not_recorded:
    "Cloud sync was not changed: Viola couldn't reach your account. Nothing was saved. Try again.",
};

/**
 * Custom hook for settings management.
 * Uses apiFetch for consistent credentials and envelope unwrapping.
 */
// Convert playlists dict from API into an array with name and is_default fields
function playlistsToArray(playlistsObj, defaultPlaylist) {
  if (!playlistsObj || typeof playlistsObj !== 'object') return [];
  if (Array.isArray(playlistsObj)) return playlistsObj;
  return Object.entries(playlistsObj).map(([name, meta]) => ({
    name,
    ...meta,
    is_default: name === defaultPlaylist,
  }));
}

export function useSettings(options = {}) {
  const { initialFetchDelayMs = 0 } = options;
  const [settings, setSettings] = useState({});
  const [voiceStatus, setVoiceStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [devices, setDevices] = useState({ input: [], output: [] });
  const [playlists, setPlaylists] = useState([]);

  // Listen for real-time settings changes via WebSocket.
  // This covers settings changed by voice commands or other clients.
  // The REST-based fetch on mount remains as the initial load and fallback.
  const handleWsMessage = useCallback((msg) => {
    if (msg.type === 'settings_changed' && msg.payload?.settings) {
      setSettings(msg.payload.settings);
      if (msg.payload.voice_status) {
        setVoiceStatus(msg.payload.voice_status);
      }
    }
  }, []);

  useWebSocket(handleWsMessage);

  // Fetch current settings
  const fetchSettings = useCallback(async () => {
    try {
      setLoading(true);
      const data = await fetchSettingsPayload();
      if (data.ok !== false) {
        setSettings(data.settings || {});
        setVoiceStatus(data.voice_status || null);
      } else {
        setError(data.error);
      }
    } catch (err) {
      setError('Failed to load settings');
    } finally {
      setLoading(false);
    }
  }, []);

  // Update settings
  const updateSettings = useCallback(async (newSettings) => {
    try {
      setSaving(true);
      setError(null);
      const data = await pushSettingsPayload(newSettings);
      if (data.ok !== false) {
        setSettings(data.settings || {});
        setVoiceStatus(data.voice_status || null);
        return true;
      } else {
        setError(data.error);
        return false;
      }
    } catch (err) {
      setError(SETTINGS_ERROR_BY_CODE[err?.code] || 'Failed to save settings');
      return false;
    } finally {
      setSaving(false);
    }
  }, []);

  // Update a single setting
  const updateSetting = useCallback(async (key, value) => {
    return updateSettings({ [key]: value });
  }, [updateSettings]);

  // Reset to defaults
  const resetSettings = useCallback(async () => {
    try {
      setSaving(true);
      const data = await apiFetch('/v1/settings/reset', { method: 'POST' });
      if (data.ok !== false) {
        setSettings(data.settings || {});
        setVoiceStatus(data.voice_status || null);
        return true;
      } else {
        setError(data.error);
        return false;
      }
    } catch (err) {
      setError('Failed to reset settings');
      return false;
    } finally {
      setSaving(false);
    }
  }, []);

  // Fetch audio devices
  const fetchDevices = useCallback(async () => {
    try {
      const data = await apiFetch('/v1/settings/devices');
      if (data.ok) {
        setDevices({
          input: data.input_devices || [],
          output: data.output_devices || [],
        });
      }
    } catch (err) {
      // Continue with empty list
    }
  }, []);

  // Fetch playlists
  const fetchPlaylists = useCallback(async () => {
    try {
      const data = await apiFetch('/v1/settings/playlists');
      if (data.ok) {
        setPlaylists(playlistsToArray(data.playlists, data.default_playlist));
      }
    } catch (err) {
      // Continue with empty list
    }
  }, []);

  // Add playlist
  const addPlaylist = useCallback(async (name, url, shuffle = true) => {
    try {
      const data = await apiFetch(`/v1/settings/playlists?name=${encodeURIComponent(name)}&url=${encodeURIComponent(url)}&shuffle=${shuffle}`, {
        method: 'POST',
      });
      if (data.ok) {
        setPlaylists(playlistsToArray(data.playlists, data.default_playlist));
        return true;
      }
      return false;
    } catch (err) {
      return false;
    }
  }, []);

  // Delete playlist
  const deletePlaylist = useCallback(async (name) => {
    try {
      const data = await apiFetch(`/v1/settings/playlists/${encodeURIComponent(name)}`, {
        method: 'DELETE',
      });
      if (data.ok) {
        setPlaylists(playlistsToArray(data.playlists, data.default_playlist));
        return true;
      }
      return false;
    } catch (err) {
      return false;
    }
  }, []);

  // Rename playlist
  const renamePlaylist = useCallback(async (oldName, newName) => {
    try {
      const data = await apiFetch(`/v1/settings/playlists/rename?old_name=${encodeURIComponent(oldName)}&new_name=${encodeURIComponent(newName)}`, {
        method: 'POST',
      });
      if (data.ok) {
        setPlaylists(playlistsToArray(data.playlists, data.default_playlist));
        return true;
      }
      return false;
    } catch (err) {
      return false;
    }
  }, []);

  // Star playlist
  const starPlaylist = useCallback(async (name, starred = true) => {
    try {
      const data = await apiFetch(`/v1/settings/playlists/star?name=${encodeURIComponent(name)}&starred=${starred}`, {
        method: 'POST',
      });
      if (data.ok) {
        setPlaylists(playlistsToArray(data.playlists, data.default_playlist));
        return true;
      }
      return false;
    } catch (err) {
      return false;
    }
  }, []);

  // Set default playlist
  const setDefaultPlaylist = useCallback(async (name) => {
    try {
      const data = await apiFetch(`/v1/settings/playlists/default?name=${encodeURIComponent(name)}`, {
        method: 'POST',
      });
      if (data.ok) {
        setPlaylists(playlistsToArray(data.playlists, data.default_playlist));
        return true;
      }
      return false;
    } catch (err) {
      return false;
    }
  }, []);

  // Sync playlists from provider
  const syncPlaylists = useCallback(async () => {
    try {
      const data = await apiFetch('/v1/settings/playlists/sync', { method: 'POST' });
      if (data.ok) {
        setPlaylists(playlistsToArray(data.playlists, data.default_playlist));
        return { ok: true, synced_count: data.synced_count };
      }
      return { ok: false, error: data.error };
    } catch (err) {
      return { ok: false, error: "Couldn't sync playlists. Check your connection and try again." };
    }
  }, []);

  // Load settings on mount — devices and playlists are fetched lazily
  // by calling refreshDevices() / refreshPlaylists() when their UI is shown
  useEffect(() => {
    if (initialFetchDelayMs <= 0) {
      fetchSettings();
      return undefined;
    }

    const timeoutId = setTimeout(() => {
      fetchSettings();
    }, initialFetchDelayMs);

    return () => clearTimeout(timeoutId);
  }, [fetchSettings, initialFetchDelayMs]);

  return {
    settings,
    voiceStatus,
    loading,
    saving,
    error,
    devices,
    playlists,
    updateSettings,
    updateSetting,
    resetSettings,
    refreshSettings: fetchSettings,
    refreshDevices: fetchDevices,
    refreshPlaylists: fetchPlaylists,
    addPlaylist,
    deletePlaylist,
    renamePlaylist,
    starPlaylist,
    setDefaultPlaylist,
    syncPlaylists,
    clearError: () => setError(null),
  };
}
