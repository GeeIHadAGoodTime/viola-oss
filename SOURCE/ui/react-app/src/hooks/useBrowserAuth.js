/**
 * React hook for browser-native auth API.
 *
 * Communicates with /v1/browser/auth/ endpoints to manage music provider
 * authentication via the embedded QWebEngineView browser.
 *
 * Unlike the OAuth consent flow (useMusicProviders), this hook drives a
 * browser-native login: the backend navigates QWebEngineView to the
 * provider's login page and detects session cookies automatically.
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { apiFetch } from './useViolaApi';
import { isFeatureHidden } from '../utils/featureSurface';

// Poll interval for login detection (ms). Chosen to avoid spamming the API
// while still giving responsive feedback (roughly 2 checks per second).
const LOGIN_POLL_INTERVAL_MS = 2000;

// Maximum time to wait for a login to complete before giving up (ms).
const LOGIN_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes

/**
 * Normalize a provider object from the API into a consistent shape.
 * The API returns logged_in (bool) + session_expired (bool); we derive
 * a status string for the UI: 'connected' | 'expired' | 'not_connected'.
 */
function normalizeProvider(raw) {
  let status = 'not_connected';
  if (raw.logged_in) {
    status = 'connected';
  } else if (raw.session_expired) {
    status = 'expired';
  } else if (raw.status) {
    status = raw.status;
  }

  return {
    name: raw.name || raw.provider_name || raw.id,
    displayName: raw.display_name || raw.displayName || raw.name || raw.id,
    status,
    iconUrl: raw.icon_url || raw.iconUrl || null,
    isDefault: raw.is_default || raw.isDefault || false,
    lastChecked: raw.last_checked || raw.lastChecked || null,
  };
}

export function useBrowserAuth() {
  const [providers, setProviders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Track which provider is currently in a login flow
  const [loggingIn, setLoggingIn] = useState(null);

  // Refs for cleanup of polling timers
  const pollTimerRef = useRef(null);
  const timeoutTimerRef = useRef(null);

  /**
   * Fetch all provider auth statuses from the backend.
   */
  const fetchStatus = useCallback(async () => {
    // #4226: `/v1/browser/auth/*` is the LOCAL_ONLY `browser_auth` route group
    // (backend/cloud_route_manifest.py) — the login it drives happens in a real
    // browser window on the user's own machine, which a headless cloud backend
    // does not have. Same wall the sibling OAuth hook already puts up, so this
    // hook cannot become a live dead-fetch the moment someone renders it.
    if (isFeatureHidden('music_services')) {
      setProviders([]);
      setError(null);
      setLoading(false);
      return;
    }
    try {
      setLoading(true);
      setError(null);
      const data = await apiFetch('/v1/browser/auth/status');
      // API returns { providers: [...] } or an array directly
      const rawProviders = Array.isArray(data) ? data : (data.providers || []);
      setProviders(rawProviders.map(normalizeProvider));
    } catch (_err) {
      setError('Unable to check music service status. Please try again.');
    } finally {
      setLoading(false);
    }
  }, []);

  /**
   * Stop any active login polling.
   */
  const stopPolling = useCallback(() => {
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    if (timeoutTimerRef.current) {
      clearTimeout(timeoutTimerRef.current);
      timeoutTimerRef.current = null;
    }
  }, []);

  /**
   * Initiate a login flow for the given provider.
   *
   * This tells the backend to navigate the QWebEngineView to the provider's
   * login page. We then poll the status endpoint until the provider reports
   * 'connected' or we time out.
   *
   * @param {string} providerName - e.g. 'youtube_music', 'spotify'
   * @returns {Promise<{success: boolean, error?: string}>}
   */
  const initiateLogin = useCallback(async (providerName) => {
    // Don't start a new login if one is already in progress
    if (loggingIn) {
      return { success: false, error: 'A login is already in progress' };
    }

    setError(null);
    setLoggingIn(providerName);

    const loginUrl = `/v1/browser/auth/login/${providerName}`;
    const statusUrl = `/v1/browser/auth/status/${providerName}`;

    try {
      // Tell the backend to start the browser login flow
      const loginData = await apiFetch(loginUrl, {
        method: 'POST',
      });

      // If the browser controller is unavailable, fail fast instead of waiting indefinitely.
      if (loginData && loginData.controller_attached === false) {
        setLoggingIn(null);
        const msg = 'Browser login requires the Viola desktop app. Open Viola on your device and try again.';
        setError(msg);
        return { success: false, error: msg };
      }

      // Poll for login completion with debounced interval
      return new Promise((resolve) => {
        const startTime = Date.now();

        pollTimerRef.current = setInterval(async () => {
          try {
            const data = await apiFetch(statusUrl);
            const status = data?.logged_in ? 'connected' : 'not_connected';

            if (status === 'connected') {
              stopPolling();
              setLoggingIn(null);
              // Hide the browser overlay (login complete)
              if (window.viola && window.viola.hideBrowserOverlay) {
                window.viola.hideBrowserOverlay();
              }
              // Refresh the full providers list
              await fetchStatus();
              resolve({ success: true });
            } else if (Date.now() - startTime > LOGIN_TIMEOUT_MS) {
              stopPolling();
              setLoggingIn(null);
              setError('Login timed out. Please try again.');
              resolve({ success: false, error: 'Login timed out' });
            }
          } catch (pollErr) {
            // Don't fail on individual poll errors; the network may hiccup
            console.warn('[BrowserAuth] Poll error:', pollErr.message);
          }
        }, LOGIN_POLL_INTERVAL_MS);

        // Hard timeout safety net
        timeoutTimerRef.current = setTimeout(() => {
          stopPolling();
          setLoggingIn(null);
          setError('Login timed out. Please try again.');
          resolve({ success: false, error: 'Login timed out' });
        }, LOGIN_TIMEOUT_MS);
      });
    } catch (err) {
      setLoggingIn(null);
      setError('Unable to connect to music service. Please try again.');
      return { success: false, error: "Couldn't sign in. Try again." };
    }
  }, [loggingIn, fetchStatus, stopPolling]);

  /**
   * Refresh the auth check for a single provider (non-blocking).
   *
   * @param {string} providerName
   * @returns {Promise<{success: boolean, status?: string, error?: string}>}
   */
  const refreshCheck = useCallback(async (providerName) => {
    try {
      const data = await apiFetch(`/v1/browser/auth/refresh/${providerName}`, {
        method: 'POST',
      });
      const status = data?.logged_in ? 'connected'
        : data?.session_expired ? 'expired'
        : 'not_connected';

      // Update that provider's status in local state
      setProviders((prev) =>
        prev.map((p) =>
          p.name === providerName ? { ...p, status } : p
        )
      );

      return { success: true, status };
    } catch (err) {
      return { success: false, error: "Couldn't refresh connection status. Try again." };
    }
  }, []);

  /**
   * Cancel an in-progress login flow.
   */
  const cancelLogin = useCallback(() => {
    stopPolling();
    setLoggingIn(null);
    // Hide the browser overlay when cancelling
    if (window.viola && window.viola.hideBrowserOverlay) {
      window.viola.hideBrowserOverlay();
    }
  }, [stopPolling]);

  /**
   * Clear the error state.
   */
  const clearError = useCallback(() => {
    setError(null);
  }, []);

  // Auto-fetch provider status on mount
  useEffect(() => {
    fetchStatus();
  }, [fetchStatus]);

  // Cleanup polling on unmount
  useEffect(() => {
    return () => {
      stopPolling();
    };
  }, [stopPolling]);

  return {
    providers,
    loading,
    error,
    loggingIn,
    fetchStatus,
    initiateLogin,
    refreshCheck,
    cancelLogin,
    clearError,
  };
}

export default useBrowserAuth;
