/**
 * Music providers hook for managing music service connections.
 *
 * Uses the /api/v1/consent/* API to:
 * - List available providers and their status
 * - Start OAuth connection flows
 * - Disconnect providers
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { API } from '../config';
import { apiFetch } from './useViolaApi';
import { isFeatureHidden } from '../utils/featureSurface';

const CONSENT_BASE = API.CONSENT;
const DESKTOP_ONLY_MESSAGE = 'Connecting music services is available in the desktop app.';

// Providers exposed in the launch settings UI.
const IMPLEMENTED_PROVIDER_IDS = new Set(['youtube_music', 'local', 'spotify']);

export function useMusicProviders() {
  const [providers, setProviders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [connecting, setConnecting] = useState(null); // provider_id currently connecting
  const activePopupRef = useRef(null);

  /**
   * Fetch provider list with connection status
   */
  const fetchProviders = useCallback(async () => {
    // OAuth provider linking (Spotify/YouTube Music) persists tokens to the
    // desktop's local encrypted vault -- Tier-3, desktop-only (#1064). Skip
    // the dead fetch on the cloud SPA instead of a confusing 404.
    if (isFeatureHidden('oauth_provider_linking')) {
      setProviders([]);
      setLoading(false);
      return;
    }
    try {
      setLoading(true);
      const data = await apiFetch(`${CONSENT_BASE}/providers`, {
        credentials: 'include',
      });

      if (data.ok && data.providers) {
        // Filter to only implemented music providers (hides stubs)
        const musicProviders = data.providers.filter(
          p => p.is_music_provider && IMPLEMENTED_PROVIDER_IDS.has(p.id)
        );
        setProviders(musicProviders);
      } else {
        throw new Error(data.error || 'Failed to fetch providers');
      }
    } catch {
      setError("Couldn't load music services. Check your connection and try again.");
    } finally {
      setLoading(false);
    }
  }, []);

  /**
   * Start OAuth connection flow for a provider
   */
  const connect = useCallback(async (providerId) => {
    if (isFeatureHidden('oauth_provider_linking')) {
      setError(DESKTOP_ONLY_MESSAGE);
      return { success: false, error: DESKTOP_ONLY_MESSAGE };
    }
    setError(null);
    setConnecting(providerId);

    try {
      // Start a consent session for this provider
      const data = await apiFetch(`${CONSENT_BASE}/session`, {
        method: 'POST',
        credentials: 'include',
        body: JSON.stringify({
          providers: [providerId],
        }),
      });

      if (!data.ok || !data.session) {
        throw new Error(data.error || 'Failed to start connection');
      }

      const session = data.session;
      const step = session.steps?.[0];

      if (!step?.authorization_url) {
        const msg = step?.status === 'unavailable'
          ? 'Spotify is not configured. To use the OAuth flow, add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to your .env file.'
          : 'No authorization URL returned from server.';
        throw new Error(msg);
      }

      // Open OAuth popup
      const width = 500;
      const height = 600;
      const left = window.screenX + (window.outerWidth - width) / 2;
      const top = window.screenY + (window.outerHeight - height) / 2;

      const popup = window.open(
        step.authorization_url,
        'oauth_music',
        `width=${width},height=${height},left=${left},top=${top}`
      );

      activePopupRef.current = popup;

      // Set up message listener for OAuth callback
      const handleMessage = async (event) => {
        if (event.data?.type === 'oauth_callback' && event.data?.code) {
          // Complete the OAuth step
          try {
            const completeData = await apiFetch(
              `${CONSENT_BASE}/session/${session.session_id}/${providerId}`,
              {
                method: 'POST',
                credentials: 'include',
                body: JSON.stringify({
                  code: event.data.code,
                  state: event.data.state,
                }),
              }
            );

            if (!completeData.ok) {
              throw new Error(completeData.error || 'Failed to complete connection');
            }

            // Refresh providers list
            await fetchProviders();
          } catch {
            setError("Couldn't connect this music service. Try again.");
          }

          window.removeEventListener('message', handleMessage);
          setConnecting(null);
        }
      };

      window.addEventListener('message', handleMessage);

      // Poll for popup close (fallback if postMessage doesn't work)
      const pollInterval = setInterval(async () => {
        if (popup.closed) {
          clearInterval(pollInterval);
          window.removeEventListener('message', handleMessage);
          activePopupRef.current = null;
          setConnecting(null);

          // Refresh providers list in case connection completed
          await fetchProviders();
        }
      }, 500);

      return { success: true };
    } catch {
      setError("Couldn't connect this music service. Try again.");
      setConnecting(null);
      return { success: false, error: "Couldn't connect this music service. Try again." };
    }
  }, [fetchProviders]);

  /**
   * Disconnect a provider
   */
  const disconnect = useCallback(async (providerId) => {
    if (isFeatureHidden('oauth_provider_linking')) {
      setError(DESKTOP_ONLY_MESSAGE);
      return { success: false, error: DESKTOP_ONLY_MESSAGE };
    }
    setError(null);

    try {
      const data = await apiFetch(`${CONSENT_BASE}/revoke`, {
        method: 'POST',
        credentials: 'include',
        body: JSON.stringify({
          provider_id: providerId,
        }),
      });

      if (!data.ok) {
        throw new Error(data.error || 'Failed to disconnect');
      }

      // Refresh providers list
      await fetchProviders();
      return { success: true };
    } catch {
      setError("Couldn't disconnect this music service. Try again.");
      return { success: false, error: "Couldn't disconnect this music service. Try again." };
    }
  }, [fetchProviders]);

  /**
   * Clear error state
   */
  const clearError = useCallback(() => {
    setError(null);
  }, []);

  // Fetch providers on mount
  useEffect(() => {
    fetchProviders();
  }, [fetchProviders]);

  // Cleanup popup on unmount
  useEffect(() => {
    return () => {
      if (activePopupRef.current && !activePopupRef.current.closed) {
        activePopupRef.current.close();
      }
    };
  }, []);

  return {
    providers,
    loading,
    error,
    connecting,
    connect,
    disconnect,
    refresh: fetchProviders,
    clearError,
  };
}

export default useMusicProviders;
