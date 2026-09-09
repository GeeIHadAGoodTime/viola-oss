/**
 * Calendar providers hook for managing calendar service connections.
 *
 * Uses the /api/v1/consent/* API for provider status and disconnect,
 * and the auth OAuth flow for connecting (system browser per RFC 8252).
 *
 * Google blocks sign-in from embedded browsers, so the connect flow
 * opens the system browser via the Qt bridge (window.viola.openExternalUrl).
 */

import { useState, useEffect, useCallback } from 'react';
import { API } from '../config';
import { apiFetch } from './useViolaApi';
import { isFeatureHidden } from '../utils/featureSurface';

const CONSENT_BASE = API.CONSENT;
const DESKTOP_ONLY_MESSAGE = 'Connecting Google Calendar is available in the desktop app.';
const POLL_INTERVAL_MS = 2000;
const POLL_TIMEOUT_MS = 120000; // 2 minutes
const STATUS_PROVIDER_ALIASES = {
  google_calendar: 'google',
};

function isRemoteProviderConnected(statusData, providerId) {
  const providers = statusData?.providers ?? statusData?.data?.providers ?? [];
  const statusProviderId = STATUS_PROVIDER_ALIASES[providerId] || providerId;
  return providers.some((provider) => (
    (provider.provider === statusProviderId || provider.id === providerId || provider.provider_id === providerId)
    && provider.configured === true
  ));
}

export function useCalendarProviders() {
  const [providers, setProviders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [connecting, setConnecting] = useState(null); // provider_id currently connecting

  /**
   * Fetch provider list with connection status
   */
  const fetchProviders = useCallback(async () => {
    // OAuth provider linking (Google Calendar sync) persists tokens to the
    // desktop's local encrypted vault -- Tier-3, desktop-only (#1064). Skip
    // the dead fetch on the cloud SPA instead of a confusing 404.
    if (isFeatureHidden('oauth_provider_linking')) {
      setProviders([]);
      setLoading(false);
      return;
    }
    try {
      setLoading(true);
      const data = await apiFetch(`${CONSENT_BASE}/providers`);

      if (data.ok && data.providers) {
        // Filter to only calendar providers (not music providers)
        const calendarProviders = data.providers.filter(p => !p.is_music_provider);
        setProviders(calendarProviders);
      } else {
        throw new Error(data.error || 'Failed to fetch providers');
      }
    } catch {
      setError("Couldn't load calendars. Check your connection and try again.");
    } finally {
      setLoading(false);
    }
  }, []);

  /**
   * Start OAuth connection flow for a provider.
   *
   * Opens the authorization URL in the system browser via Qt bridge
   * (falls back to window.open). Polls /v1/calendar/status for completion
   * instead of relying on postMessage (which doesn't work cross-window
   * from system browser to Qt webview).
   */
  const connect = useCallback(async (providerId) => {
    if (isFeatureHidden('oauth_provider_linking')) {
      setError(DESKTOP_ONLY_MESSAGE);
      return { success: false, error: DESKTOP_ONLY_MESSAGE };
    }
    setError(null);
    setConnecting(providerId);

    try {
      // Start a consent session to get the authorization URL
      const data = await apiFetch(`${CONSENT_BASE}/session`, {
        method: 'POST',
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
        throw new Error('No authorization URL returned');
      }

      // Open in system browser — prefer Qt bridge, fall back to window.open
      if (window.viola && typeof window.viola.openExternalUrl === 'function') {
        if (import.meta.env.DEV) {
          console.log('[Calendar OAuth] Using Qt bridge → system browser');
        }
        window.viola.openExternalUrl(step.authorization_url);
      } else {
        console.warn('[Calendar OAuth] Qt bridge not available, trying window.open');
        const win = window.open(step.authorization_url, '_blank');
        if (!win) {
          console.warn('[Calendar OAuth] window.open blocked, retrying with noopener');
          const retryWin = window.open(step.authorization_url, '_blank', 'noopener,noreferrer');
          if (!retryWin) {
            throw new Error('OAuth popup was blocked. Please allow popups for this page and try again.');
          }
        }
      }

      // Poll for calendar connection after the consent callback finishes
      // the backend exchange and /v1/calendar/status sees the linked token.
      const startTime = Date.now();
      await new Promise((resolve) => {
        const pollInterval = setInterval(async () => {
          try {
            const statusData = await apiFetch('/v1/calendar/status');
            const connected = isRemoteProviderConnected(statusData, providerId);

            if (connected) {
              clearInterval(pollInterval);
              await fetchProviders();
              setConnecting(null);
              resolve();
            } else if (Date.now() - startTime > POLL_TIMEOUT_MS) {
              clearInterval(pollInterval);
              setConnecting(null);
              resolve();
            }
          } catch {
            // Ignore polling errors, keep trying
          }
        }, POLL_INTERVAL_MS);
      });

      return { success: true };
    } catch {
      setError("Couldn't connect calendar. Try again.");
      setConnecting(null);
      return { success: false, error: "Couldn't connect calendar. Try again." };
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
      setError("Couldn't disconnect calendar. Try again.");
      return { success: false, error: "Couldn't disconnect calendar. Try again." };
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

export default useCalendarProviders;
