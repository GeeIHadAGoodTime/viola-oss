import { useMemo } from 'react';
import {
  getClientApiKey,
  getClientApiKeySync,
  getCloudAccessToken,
} from '../config';
import { getGoTrueAccessToken } from '../lib/gotrue_client';

const BASE = window.__VIOLA_BASE_URL__ || '';
const API_KEY = () => window.__VIOLA_API_KEY__ || '';
const COMMAND_STREAM_DONE_WAIT_MS = 1500;

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function createStreamId() {
  if (window.crypto?.randomUUID) {
    return window.crypto.randomUUID();
  }
  return `cmd-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function resolveApiKey() {
  let apiKey = API_KEY();
  if (!apiKey) {
    apiKey = getClientApiKeySync() || (await getClientApiKey()) || '';
  }
  return apiKey;
}

async function resolveStreamAuthToken(_apiKey = '', streamId = '') {
  // Goes through authFetch so the desktop API key, cloud GoTrue Bearer, AND the
  // spoke pairing token (X-Spoke-Token) are all preserved uniformly. Manual
  // header construction here previously dropped X-Spoke-Token, which broke
  // SSE auth for paired trusted browsers (Stop-hook gate
  // check-frontend-protected-fetch-contract).
  try {
    const authParams = new URLSearchParams({
      purpose: 'sse',
      stream_id: streamId
    });
    const response = await authFetch(`/v1/ws/auth?${authParams.toString()}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    if (!response.ok) {
      return '';
    }
    const json = await response.json();
    return (json?.data?.token || json?.token || '').trim();
  } catch {
    return '';
  }
}

/**
 * Read the spoke pairing token from the URL when present.
 *
 * Path C2 / pairing flow: a trusted browser that completes
 * `POST /bootstrap/request` + `POST /bootstrap/confirm` may continue using
 * the dashboard by appending `?spoke_token=<vspk1.*>` to its URL.
 * The WebSocket already honors this token; this helper extends it to REST
 * so authenticated `/v1/*` calls succeed instead of returning 401.
 *
 * Returns empty string when no spoke token is present.
 */
function readSpokeTokenFromUrl() {
  if (typeof window === 'undefined' || !window.location) {
    return '';
  }
  try {
    const params = new URLSearchParams(window.location.search);
    return (params.get('spoke_token') || '').trim();
  } catch {
    return '';
  }
}

export async function buildStreamUrl(streamId, { create = false } = {}) {
  const params = new URLSearchParams();
  if (create) {
    params.set('create', '1');
  }
  const apiKey = await resolveApiKey();
  const streamToken = await resolveStreamAuthToken(apiKey, streamId);
  if (streamToken) {
    params.set('stream_token', streamToken);
  }
  const query = params.toString();
  return `${BASE}/api/stream/${encodeURIComponent(streamId)}${query ? `?${query}` : ''}`;
}

/**
 * The browser's own IANA timezone (e.g. "America/Chicago").
 *
 * The cloud backend runs in UTC, so without this it had no way to know what
 * wall-clock time a user meant: asking Viola for a 2pm meeting stored 14:00Z
 * and handed back a 9:00 AM event (#3557). Sending the zone on every request
 * keeps nothing at rest (no cloud-sync consent surface) and follows the user
 * when they travel. Returns '' when the runtime can't tell us.
 */
function browserTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
  } catch {
    return '';
  }
}

export async function authFetch(path, options = {}) {
  const url = `${BASE}${path}`;
  const headers = { ...(options.headers || {}) };
  if (!headers['X-Viola-Timezone']) {
    const timezone = browserTimezone();
    if (timezone) {
      headers['X-Viola-Timezone'] = timezone;
    }
  }
  const apiKey = await resolveApiKey();
  if (apiKey) {
    headers['X-API-Key'] = apiKey;
  }
  // Desktop calls never attach the cloud GoTrue Bearer: the desktop-local
  // backend authenticates via X-API-Key + the `viola_session` cookie (sent
  // automatically by `credentials: 'same-origin'` below), and a raw cloud
  // GoTrue JWT can NEVER validate as a desktop-local session token
  // (auth/desktop_session.py's is_desktop_local_session_token() requires the
  // `vls_` local-session prefix, which a cloud JWT structurally never has).
  // Attaching it anyway only risked masking the valid cookie/API-key auth
  // with a token the server was guaranteed to reject (issue #340: it also
  // took `extract_token_from_request` priority over the cookie in
  // auth/middleware.py, so a signed-into-cloud desktop session's own /v1/*
  // calls carried a Bearer that could never authenticate them).
  if (!apiKey && !headers.Authorization && !headers.authorization) {
    // Cloud GoTrue session token — only meaningful for the cloud/LAN web
    // client (no desktop API key), which authenticates the cloud surface
    // directly via this Bearer. config.js is the canonical source — it is
    // kept current by AuthProvider (src/auth/). Fall back to the standalone
    // gotrue_client session store when no AuthProvider session is mirrored.
    const accessToken = getCloudAccessToken() || (await getGoTrueAccessToken());
    if (accessToken) {
      headers.Authorization = `Bearer ${accessToken}`;
    }
  }
  // Spoke pairing fallback: when no master API key and no GoTrue JWT are
  // present but the URL carries a `?spoke_token=vspk1.*` from the documented
  // pairing flow, attach it as X-Spoke-Token. Server `ui/server.py` already
  // CORS-whitelists this header and the spoke-credential verifier accepts it
  // for /v1/* endpoints. Without this, a paired trusted browser sees its
  // dashboard skeleton render (gate bypass via spoke_token URL param) but
  // every /v1/* call returns 401 — the user-reported "Open Viola Desktop"
  // / "Disconnected" failure mode discovered during the LOCAL GUI walk on
  // 2026-05-20.
  if (!apiKey && !headers.Authorization && !headers.authorization && !headers['X-Spoke-Token']) {
    const spokeToken = readSpokeTokenFromUrl();
    if (spokeToken) {
      headers['X-Spoke-Token'] = spokeToken;
    }
  }
  const response = await fetch(url, {
    ...options,
    headers,
    credentials: options.credentials || 'same-origin',
  });

  if (response.status === 401) {
    console.warn(`[Viola] Auth failed for ${path}; reload the desktop shell or re-pair the LAN client.`);
  }

  return response;
}

// NOTE: the former `cloudPhoneFetch` (a browser-held cloud-bearer fetch straight
// to the cloud /api/phone/*) was REMOVED in the 2026-06-29 capstone. The desktop
// must never hold a cloud bearer in the browser (SEC-017), and on the desktop
// getCloudAccessToken() is always empty, so that path silently no-oped to
// localhost. Phone-data fetches now use plain authFetch('/v1/...') to the LOCAL
// backend, which proxies to the cloud with the server-side bearer
// (telephony/desktop_cloud_proxy.py). A same-origin web client reaches the cloud
// through its normal authFetch base.

export async function apiFetch(path, options = {}) {
  const wantsJsonHeader = !(options.body instanceof FormData);
  const response = await authFetch(path, {
    ...options,
    headers: wantsJsonHeader
      ? { 'Content-Type': 'application/json', ...options.headers }
      : { ...options.headers },
  });

  if (!response.ok) {
    const errorText = await response.text();
    if (response.status === 429) {
      console.warn(`[Viola] Rate limited on ${path} — waiting before retry`);
    } else {
      console.warn(`[Viola] API ${path} failed: ${response.status} ${errorText}`);
    }
    const err = new Error("We couldn't complete that request. Please try again.");
    // Preserve the status + ResponseEnvelope error code so callers can react to
    // specific, expected failure shapes (e.g. `consent_required` for Tier-2
    // cloud-sync-gated routes) instead of only showing a generic message.
    err.status = response.status;
    try {
      const parsed = JSON.parse(errorText);
      err.code = parsed?.error?.code || null;
      // A failure envelope may also carry the server's real state, which is
      // what a caller that flipped a control optimistically needs in order to
      // roll back to the truth rather than to a guess (#4214).
      err.data = parsed?.data ?? null;
    } catch {
      err.code = null;
      err.data = null;
    }
    throw err;
  }
  try {
    const json = await response.json();
    // Auto-unwrap ResponseEnvelope — only unwrap on success.
    // On error (ok===false), return the full envelope so callers can inspect the error.
    if (json.data !== undefined && json.ok !== false) {
      return json.data;
    }
    return json;
  } catch {
    throw new Error("We couldn't complete that request. Please try again.");
  }
}

export async function sendCommandStreaming(text, onToken, history = [], onThinking) {
  const commandHistory = typeof history === 'function' ? [] : history;
  const thinkingHandler = typeof history === 'function' ? history : onThinking;
  const streamId = createStreamId();
  const streamUrl = await buildStreamUrl(streamId, { create: true });
  let streamDone = false;
  let source = null;

  const streamPromise = new Promise((resolve) => {
    source = new EventSource(streamUrl, { withCredentials: true });
    source.onmessage = (event) => {
      let payload = null;
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      if (payload?.token && onToken) {
        onToken(payload.token);
      }
      if (payload?.thinking && thinkingHandler) {
        thinkingHandler(payload.thinking);
      }
      if (payload?.done || payload?.error) {
        streamDone = true;
        source.close();
        resolve(payload);
      }
    };
    source.onerror = () => {
      if (!streamDone) {
        source.close();
        resolve(null);
      }
    };
  });

  try {
    const result = await apiFetch('/v1/command', {
      method: 'POST',
      body: JSON.stringify({ text, history: commandHistory, stream_id: streamId })
    });
    if (!streamDone) {
      await Promise.race([streamPromise, wait(COMMAND_STREAM_DONE_WAIT_MS)]);
    }
    return result;
  } catch (err) {
    if (source) {
      source.close();
    }
    throw err;
  } finally {
    if (source) {
      source.close();
    }
  }
}

export function useViolaApi() {
  // Memoize the api object to ensure stable reference across renders
  return useMemo(() => ({
    // State
    getState: () => apiFetch('/v1/state'),

    // Playback control
    play: (query, source) => apiFetch('/v1/play', {
      method: 'POST',
      body: JSON.stringify({ query, ...(source ? { source } : {}) })
    }),
    pause: () => apiFetch('/v1/pause', { method: 'POST' }),
    resume: () => apiFetch('/v1/resume', { method: 'POST' }),
    stop: () => apiFetch('/v1/stop', { method: 'POST' }),
    skip: () => apiFetch('/v1/skip', { method: 'POST' }),
    next: () => apiFetch('/v1/next', { method: 'POST' }),
    previous: () => apiFetch('/v1/previous', { method: 'POST' }),
    seek: (position) => apiFetch('/v1/seek', { method: 'POST', body: JSON.stringify({ position }) }),
    setVolume: (level) => apiFetch('/v1/volume', { method: 'POST', body: JSON.stringify({ level }) }),

    // Rating
    setRating: (rating) => apiFetch('/v1/rating', { method: 'POST', body: JSON.stringify({ rating }) }),

    // Queue
    getQueue: () => apiFetch('/v1/queue'),
    clearQueue: () => apiFetch('/v1/queue/clear', { method: 'POST' }),
    removeFromQueue: (itemId) => apiFetch(`/v1/queue/item/${itemId}`, { method: 'DELETE' }),
    playQueueItem: (itemId) => apiFetch('/v1/queue/play', { method: 'POST', body: JSON.stringify({ item_id: itemId }) }),
    reorderQueue: (fromIndex, toIndex) => apiFetch('/v1/queue/reorder', {
      method: 'POST',
      body: JSON.stringify({ from_index: fromIndex, to_index: toIndex })
    }),

    // Commands
    sendCommand: (text, history = []) => apiFetch('/v1/command', {
      method: 'POST',
      body: JSON.stringify({ text, history })
    }),
    sendCommandStreaming,

    // Weather
    getWeather: (city, options = {}) => {
      const params = new URLSearchParams();
      if (city) params.set('city', city);
      if (options.forceRefresh) params.set('force_refresh', 'true');
      return apiFetch(`/v1/weather?${params.toString()}`);
    },

    // Preferences
    setRepeat: (mode) => apiFetch('/v1/repeat', { method: 'POST', body: JSON.stringify({ mode }) }),
    setShuffle: (enabled) => apiFetch('/v1/shuffle', { method: 'POST', body: JSON.stringify({ enabled }) }),

    // Diagnostics
    getDiagnostics: () => apiFetch('/v1/diagnostics'),
    submitBugReport: (message, context = {}) => apiFetch('/v1/bug-report', {
      method: 'POST',
      body: JSON.stringify({ message, type: 'bug', context })
    }),

    // Calendar
    getCalendarEvents: (date = 'today') => apiFetch(`/v1/calendar/events?date=${encodeURIComponent(date)}`),
    getCalendarStatus: () => apiFetch('/v1/calendar/status'),
    getCalendarNextEvent: () => apiFetch('/v1/calendar/events/next'),
  }), []); // Empty deps - apiFetch and BASE are module-level constants
}
