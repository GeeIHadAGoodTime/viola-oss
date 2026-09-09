/**
 * iCloud / generic CalDAV calendar connect hook.
 *
 * Backs the "Connect iCloud Calendar" settings card (#3281). Unlike
 * useCalendarProviders.js (Google/Microsoft OAuth), CalDAV has no redirect
 * flow -- Apple requires 2FA + an app-specific password for third-party
 * access, so this is a plain form POST to the two REST routes added for
 * #1392: POST /v1/calendar/caldav/connect and /v1/calendar/caldav/disconnect
 * (ui/api/routes/calendar.py). The backend refuses both on the cloud
 * deployment (a user's own iCloud app-specific password is a Tier-3
 * external-account secret, desktop-only per CLAUDE.md's storage rule), so
 * this hook mirrors that gate client-side with the same feature-surface key
 * already used to hide Google/Graph OAuth linking on the cloud SPA.
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { apiFetch } from './useViolaApi';
import { isFeatureHidden } from '../utils/featureSurface';

export const CALDAV_DESKTOP_ONLY_MESSAGE =
  'Connecting an external CalDAV calendar (like iCloud) is available in the desktop app.';

const ERROR_MESSAGES = {
  missing_field: 'Enter your Apple ID and an app-specific password.',
  caldav_credentials_invalid: 'Those credentials look incomplete. Check the Apple ID and password.',
  caldav_connect_failed:
    "Couldn't connect to that account. Check the Apple ID, and make sure you're using an "
    + 'app-specific password (not your regular Apple ID password).',
  caldav_connect_desktop_only: CALDAV_DESKTOP_ONLY_MESSAGE,
  caldav_unavailable: "CalDAV support isn't available on this build.",
  caldav_not_connected: 'No iCloud calendar was connected.',
};

function messageForError(err) {
  return ERROR_MESSAGES[err?.code] || "Couldn't connect that calendar. Please try again.";
}

/**
 * @returns {{
 *   status: 'loading'|'connected'|'disconnected'|'unavailable',
 *   calendars: Array<object>,
 *   connecting: boolean,
 *   disconnecting: boolean,
 *   error: string|null,
 *   connect: (opts: {username: string, password: string, url?: string, verifySsl?: boolean}) => Promise<{success: boolean, error?: string, calendars?: Array<object>}>,
 *   disconnect: () => Promise<{success: boolean, error?: string}>,
 *   refresh: () => Promise<void>,
 *   clearError: () => void,
 * }}
 */
export function useCalDAVCalendar() {
  const [status, setStatus] = useState('loading');
  const [calendars, setCalendars] = useState([]);
  const [connecting, setConnecting] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);
  const [error, setError] = useState(null);
  const mountedRef = useRef(true);

  const refresh = useCallback(async () => {
    if (isFeatureHidden('oauth_provider_linking')) {
      if (mountedRef.current) setStatus('unavailable');
      return;
    }
    try {
      const data = await apiFetch('/v1/calendar/status');
      const providers = data?.providers ?? [];
      const caldav = providers.find((provider) => provider?.provider === 'caldav');
      if (mountedRef.current) {
        setStatus(caldav?.configured ? 'connected' : 'disconnected');
      }
    } catch {
      if (mountedRef.current) setStatus('disconnected');
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    refresh();
    return () => {
      mountedRef.current = false;
    };
  }, [refresh]);

  const connect = useCallback(async ({ username, password, url, verifySsl = true } = {}) => {
    if (isFeatureHidden('oauth_provider_linking')) {
      setError(CALDAV_DESKTOP_ONLY_MESSAGE);
      return { success: false, error: CALDAV_DESKTOP_ONLY_MESSAGE };
    }
    setError(null);
    setConnecting(true);
    try {
      const body = { username, password, verify_ssl: verifySsl };
      if (url) {
        body.url = url;
      } else {
        body.icloud = true;
      }
      const data = await apiFetch('/v1/calendar/caldav/connect', {
        method: 'POST',
        body: JSON.stringify(body),
      });
      const connectedCalendars = Array.isArray(data?.calendars) ? data.calendars : [];
      if (mountedRef.current) {
        setCalendars(connectedCalendars);
        setStatus('connected');
      }
      return { success: true, calendars: connectedCalendars };
    } catch (err) {
      const message = messageForError(err);
      if (mountedRef.current) setError(message);
      return { success: false, error: message };
    } finally {
      if (mountedRef.current) setConnecting(false);
    }
  }, []);

  const disconnect = useCallback(async () => {
    if (isFeatureHidden('oauth_provider_linking')) {
      setError(CALDAV_DESKTOP_ONLY_MESSAGE);
      return { success: false, error: CALDAV_DESKTOP_ONLY_MESSAGE };
    }
    setError(null);
    setDisconnecting(true);
    try {
      await apiFetch('/v1/calendar/caldav/disconnect', { method: 'POST' });
      if (mountedRef.current) {
        setStatus('disconnected');
        setCalendars([]);
      }
      return { success: true };
    } catch (err) {
      const message = messageForError(err);
      if (mountedRef.current) setError(message);
      return { success: false, error: message };
    } finally {
      if (mountedRef.current) setDisconnecting(false);
    }
  }, []);

  const clearError = useCallback(() => setError(null), []);

  return {
    status,
    calendars,
    connecting,
    disconnecting,
    error,
    connect,
    disconnect,
    refresh,
    clearError,
  };
}

export default useCalDAVCalendar;
