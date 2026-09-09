import { useEffect, useRef, useCallback, useState } from 'react';
import {
  cacheClientApiKey,
  getClientApiKey,
  getClientApiKeySync,
} from '../config';
import { getWebSocketAuthToken } from '../lib/ws_auth';

/** Reconnect backoff bounds: start at 2s, double each failed attempt, cap ~30s. */
const RECONNECT_BASE_MS = 2000;
const RECONNECT_MAX_MS = 30000;

const sharedSocket = {
  ws: null,
  connectPromise: null,
  reconnectTimeout: null,
  reconnectDelayMs: RECONNECT_BASE_MS,
  subscribers: new Set(),
  hasConnected: false,
  connectCount: 0,
  wsStatus: 'disconnected',
  wsUrl: '',
  msgCount: 0,
  lastMsgType: '(none)',
  lastError: '(none)',
  /** Server uptime_s from last /health check; detects server restarts. */
  lastServerUptime: null,
};

function getSharedSnapshot() {
  return {
    connectCount: sharedSocket.connectCount,
    wsStatus: sharedSocket.wsStatus,
    wsUrl: redactWsUrl(sharedSocket.wsUrl),
    msgCount: sharedSocket.msgCount,
    lastMsgType: sharedSocket.lastMsgType,
    lastError: sharedSocket.lastError,
  };
}

function publishSharedSnapshot() {
  const snapshot = getSharedSnapshot();
  sharedSocket.subscribers.forEach((subscriber) => {
    subscriber.onState(snapshot);
  });
}

function redactWsUrl(url) {
  return String(url)
    .replace(/([?&]spoke_token=)[^&]+/g, '$1REDACTED')
    .replace(/([?&]access_token=)[^&]+/g, '$1REDACTED')
    .replace(/([?&]token=)[^&]+/g, '$1REDACTED')
    .replace(/([?&]api_key=)[^&]+/g, '$1REDACTED');
}

async function fetchWebSocketAuthToken(base, apiKey) {
  const token = await getWebSocketAuthToken();
  if (token) return token;

  if (!apiKey) return '';

  try {
    const response = await fetch(`${base}/v1/ws/auth`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-API-Key': apiKey,
      },
      credentials: 'same-origin',
    });
    if (!response.ok) return '';
    const data = await response.json();
    return data?.data?.token || data?.token || '';
  } catch {
    return '';
  }
}

async function buildSocketUrl() {
  const base = window.__VIOLA_BASE_URL__ || window.location.origin;
  const currentParams = new URLSearchParams(window.location.search);
  const spokeToken = currentParams.get('spoke_token');
  const room = currentParams.get('room') || (spokeToken ? 'speaker' : '');
  if (spokeToken) {
    const spokeParams = new URLSearchParams();
    spokeParams.set('room', room);
    spokeParams.set('spoke_token', spokeToken);
    return `${base.replace(/^http/, 'ws')}/ws/events?${spokeParams.toString()}`;
  }

  let apiKey = window.__VIOLA_API_KEY__ || getClientApiKeySync() || '';
  if (!apiKey) {
    apiKey = (await getClientApiKey()) || '';
    if (apiKey) {
      cacheClientApiKey(apiKey);
    }
  }

  const params = new URLSearchParams();
  const wsToken = await fetchWebSocketAuthToken(base, apiKey);
  if (wsToken) {
    params.set('token', wsToken);
  }
  // SEC-005 (2026-06-09 sweep): never fall back to ?api_key= in the URL —
  // the raw key would land in server logs, proxy logs, and browser history,
  // and the server no longer accepts it. If the token mint fails the socket
  // connects unauthenticated and is rejected; the mint failure is the bug.

  if (room) {
    params.set('room', room);
  }

  const qs = params.toString();
  return base.replace(/^http/, 'ws') + '/ws/events' + (qs ? `?${qs}` : '');
}

/**
 * Schedule a reconnect attempt with capped exponential backoff (2s, 4s, 8s,
 * 16s, 30s, 30s, ...) if any subscriber still cares.
 *
 * This is the single place both the socket ``onclose`` handler AND a failed
 * connect attempt (``buildSocketUrl`` throwing, ``new WebSocket`` throwing)
 * funnel through. Before this existed, a connect that failed BEFORE the
 * ``WebSocket`` object was created attached no ``onclose`` handler, so no
 * reconnect was ever scheduled and the socket stayed permanently down — the
 * exact webview-reload gap that left the live phone-transcript panel empty
 * (call 977101ac: after a code-1001 reload no ``/ws/events`` socket ever
 * reconnected). Idempotent: a pending timer is not double-scheduled.
 *
 * A fixed 2s retry forever hammers a down/rejecting server (e.g. the
 * ``/v1/ws/auth`` token mint failing) with unbounded ``/ws/events`` +
 * ``/v1/ws/auth`` traffic; the delay is reset back to the base once a
 * connection actually succeeds (see ``ws.onopen`` below).
 */
function scheduleReconnect() {
  if (sharedSocket.reconnectTimeout) return;
  if (sharedSocket.subscribers.size === 0) return;
  const delay = sharedSocket.reconnectDelayMs;
  sharedSocket.reconnectDelayMs = Math.min(delay * 2, RECONNECT_MAX_MS);
  sharedSocket.reconnectTimeout = setTimeout(() => {
    sharedSocket.reconnectTimeout = null;
    void connectSharedSocket();
  }, delay);
}

function refreshServerUptime() {
  const base = window.__VIOLA_BASE_URL__ || window.location.origin;
  return fetch(`${base}/health`)
    .then((r) => r.json())
    .then((h) => h?.uptime_s)
    .catch(() => null);
}

async function connectSharedSocket() {
  if (
    sharedSocket.ws
    && (sharedSocket.ws.readyState === WebSocket.OPEN || sharedSocket.ws.readyState === WebSocket.CONNECTING)
  ) {
    return;
  }

  if (sharedSocket.connectPromise) {
    await sharedSocket.connectPromise;
    return;
  }

  sharedSocket.connectPromise = (async () => {
    if (sharedSocket.reconnectTimeout) {
      clearTimeout(sharedSocket.reconnectTimeout);
      sharedSocket.reconnectTimeout = null;
    }

    sharedSocket.wsStatus = 'connecting';
    sharedSocket.lastError = '(none)';
    publishSharedSnapshot();

    const wsUrl = await buildSocketUrl();
    if (sharedSocket.subscribers.size === 0) {
      sharedSocket.wsStatus = 'disconnected';
      sharedSocket.wsUrl = '';
      publishSharedSnapshot();
      return;
    }

    sharedSocket.wsUrl = wsUrl;
    publishSharedSnapshot();

    const ws = new WebSocket(wsUrl);
    ws.binaryType = 'arraybuffer';
    sharedSocket.ws = ws;

    ws.onopen = () => {
      if (sharedSocket.ws !== ws) return;
      if (import.meta.env.DEV) {
        console.log('[WS] Connected to', redactWsUrl(sharedSocket.wsUrl));
      }
      sharedSocket.wsStatus = 'connected';
      // A real connection succeeded — reset backoff so the next disconnect
      // (however far in the future) starts retrying from the base delay
      // again instead of picking up wherever a prior outage left off.
      sharedSocket.reconnectDelayMs = RECONNECT_BASE_MS;

      if (sharedSocket.hasConnected) {
        refreshServerUptime().then((uptime) => {
          if (
            typeof uptime === 'number'
            && sharedSocket.lastServerUptime !== null
            && uptime < sharedSocket.lastServerUptime
          ) {
            console.log(
              '[WS] Server restarted (uptime dropped %s -> %s) - reloading page',
              sharedSocket.lastServerUptime,
              uptime,
            );
            window.location.reload();
          }
          if (typeof uptime === 'number') {
            sharedSocket.lastServerUptime = uptime;
          }
        });
      } else {
        refreshServerUptime().then((uptime) => {
          if (typeof uptime === 'number') {
            sharedSocket.lastServerUptime = uptime;
          }
        });
      }

      sharedSocket.hasConnected = true;
      sharedSocket.connectCount += 1;
      publishSharedSnapshot();
    };

    ws.onmessage = (event) => {
      if (sharedSocket.ws !== ws) return;

      if (event.data instanceof ArrayBuffer) {
        sharedSocket.subscribers.forEach((subscriber) => {
          if (subscriber.onBinaryMessageRef.current) {
            subscriber.onBinaryMessageRef.current(event.data);
          }
        });
        return;
      }

      try {
        const data = JSON.parse(event.data);
        sharedSocket.msgCount += 1;
        sharedSocket.lastMsgType = data.type || '(no type)';
        publishSharedSnapshot();
        if (import.meta.env.DEV) {
          if (data.type === 'state' && data.payload?.volume !== undefined) {
            console.log('[WS_DIAG] Received state with volume:', data.payload.volume);
          }
        }
        if (
          data.type === 'room_playback_snapshot'
          || data.type === 'room_playback_command'
          || data.type === 'position_correction'
        ) {
          if (import.meta.env.DEV) {
            console.log('[SPOKE_WS] Received:', data.type);
          }
        }
        sharedSocket.subscribers.forEach((subscriber) => {
          subscriber.onMessageRef.current(data);
        });
      } catch (e) {
        const errMsg = e?.message || String(e);
        sharedSocket.lastError = errMsg;
        publishSharedSnapshot();
        console.warn('[WS] onmessage error:', errMsg);
      }
    };

    ws.onclose = (event) => {
      if (sharedSocket.ws !== ws) return;
      if (import.meta.env.DEV) {
        console.log('[WS] Disconnected, code:', event.code, '- reconnecting in 2s');
      }
      sharedSocket.ws = null;
      sharedSocket.wsStatus = 'disconnected';
      sharedSocket.lastError = event.reason ? `close ${event.code}: ${event.reason}` : `close ${event.code}`;
      publishSharedSnapshot();
      if (sharedSocket.hasConnected) {
        sharedSocket.subscribers.forEach((subscriber) => {
          if (subscriber.onDisconnectRef.current) {
            subscriber.onDisconnectRef.current();
          }
        });
      }
      scheduleReconnect();
    };

    ws.onerror = () => {
      if (sharedSocket.ws !== ws) return;
      sharedSocket.wsStatus = 'error';
      publishSharedSnapshot();
    };
  })().catch((err) => {
    // A failure BEFORE the WebSocket object exists (buildSocketUrl rejecting,
    // new WebSocket throwing on a bad URL) attaches no onclose handler, so
    // without this branch no reconnect would ever be scheduled and the socket
    // would stay permanently down (the webview-reload gap, call 977101ac).
    // Route every such failure through the same reconnect path.
    sharedSocket.ws = null;
    sharedSocket.wsStatus = 'disconnected';
    sharedSocket.lastError = err?.message || String(err);
    publishSharedSnapshot();
    scheduleReconnect();
  });

  try {
    await sharedSocket.connectPromise;
  } catch {
    // Already handled + reconnect scheduled in the .catch above; swallow so the
    // caller's `void connectSharedSocket()` does not surface an unhandled
    // rejection.
  } finally {
    sharedSocket.connectPromise = null;
  }
}

function disconnectSharedSocket() {
  if (sharedSocket.reconnectTimeout) {
    clearTimeout(sharedSocket.reconnectTimeout);
    sharedSocket.reconnectTimeout = null;
  }

  if (sharedSocket.ws) {
    const ws = sharedSocket.ws;
    sharedSocket.ws = null;
    ws.close();
  }

  sharedSocket.wsStatus = 'disconnected';
  publishSharedSnapshot();
}

export function useWebSocket(onMessage, { onBinaryMessage } = {}) {
  const wsRef = useRef(sharedSocket.ws);
  const onMessageRef = useRef(onMessage);
  const onBinaryMessageRef = useRef(onBinaryMessage || null);
  const onDisconnectRef = useRef(null);
  const [connectCount, setConnectCount] = useState(sharedSocket.connectCount);
  const connectCountRef = useRef(sharedSocket.connectCount);
  const wsDebugRef = useRef({
    wsStatus: sharedSocket.wsStatus,
    wsUrl: sharedSocket.wsUrl,
    msgCount: sharedSocket.msgCount,
    lastMsgType: sharedSocket.lastMsgType,
    lastError: sharedSocket.lastError,
  });

  useEffect(() => {
    onMessageRef.current = onMessage;
  }, [onMessage]);

  useEffect(() => {
    onBinaryMessageRef.current = onBinaryMessage || null;
  }, [onBinaryMessage]);

  const send = useCallback((message) => {
    if (sharedSocket.ws?.readyState === WebSocket.OPEN) {
      sharedSocket.ws.send(JSON.stringify(message));
    }
  }, []);

  useEffect(() => {
    const subscriber = {
      onMessageRef,
      onBinaryMessageRef,
      onDisconnectRef,
      onState: (snapshot) => {
        wsRef.current = sharedSocket.ws;
        wsDebugRef.current = {
          wsStatus: snapshot.wsStatus,
          wsUrl: snapshot.wsUrl,
          msgCount: snapshot.msgCount,
          lastMsgType: snapshot.lastMsgType,
          lastError: snapshot.lastError,
        };
        if (snapshot.connectCount !== connectCountRef.current) {
          connectCountRef.current = snapshot.connectCount;
          setConnectCount(snapshot.connectCount);
        }
      },
    };

    sharedSocket.subscribers.add(subscriber);
    subscriber.onState(getSharedSnapshot());
    void connectSharedSocket();

    return () => {
      sharedSocket.subscribers.delete(subscriber);
      if (sharedSocket.subscribers.size === 0) {
        disconnectSharedSocket();
      }
    };
  }, []);

  const setBinaryCallback = useCallback((cb) => {
    onBinaryMessageRef.current = cb;
  }, []);

  const setDisconnectCallback = useCallback((cb) => {
    onDisconnectRef.current = cb;
  }, []);

  return {
    wsRef,
    send,
    connectCount,
    setBinaryCallback,
    setDisconnectCallback,
    getWsDebug: () => wsDebugRef.current,
  };
}
