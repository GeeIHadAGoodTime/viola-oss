import { useCallback, useEffect, useRef, useState } from 'react';
import { getWebSocketAuthToken } from '../lib/ws_auth';

/**
 * useAgentBrowserStream — consume the cloud agent-browser viewport stream.
 *
 * Opens a dedicated `ws(s)://<api>/ws/agent-browser` WebSocket (separate from
 * the shared `/ws/events` socket) so a plain web client — Chrome on
 * useviola.com — can watch cloud Viola's headless agent browser and relay
 * mouse/keyboard input back into it.
 *
 * This is the credential-less *public browse in the stage area* feature: the
 * cloud browser is identity-less and ephemeral, and the backend blocks
 * credential-shaped fields. The hook only renders frames and relays input; it
 * never persists anything.
 *
 * Wire protocol (server -> client), per backend/cloud_browser_stream_route.py:
 *  - binary frame  : a complete JPEG of the agent viewport. Rendered as an
 *                    object URL Blob (image/jpeg), exactly like the spoke path.
 *  - stream_ready  : { session_id, viewer_id, frame_format } — input may start.
 *  - stream_error  : { code, message } — socket closes after. `code` is one of
 *                    no_agent_browser | screencast_failed (plan/kill-switch
 *                    rejections arrive as WS close codes, surfaced here too).
 *  - input_blocked : { code:"credential_input_blocked" } — a credential field
 *                    rejected text; the stream stays open.
 *  - stream_end    : { reason } — agent finished, normal close.
 *
 * Client -> server:
 *  - { action:"agent_browser_input", payload:{...} } via `sendInput`.
 *  - { action:"ping" } keepalive (server replies { type:"pong" }).
 *
 * Auth uses the existing one-shot ws-auth ticket (getWebSocketAuthToken) passed
 * as the `?token=` query param — the same mechanism every other dedicated
 * Viola WebSocket uses. The backend accepts it via the ws-auth-token path
 * (ui/api/routes/websocket_auth.py:_verify_ws_auth_token_auth).
 *
 * Handshake-level rejections (#1061): the backend's kill-switch / origin /
 * auth / paid-plan checks in backend/cloud_browser_stream_route.py all close
 * the socket BEFORE calling `ws.accept()` (deliberate — gate
 * check-agent-browser-ws-gated requires those checks precede accept so a
 * blocked user is never granted a live protocol session). uvicorn's ASGI
 * WebSocket implementation collapses ANY pre-accept `websocket.close` into a
 * bare HTTP 403 with no headers/body, discarding whatever close code and
 * reason the app chose (confirmed against
 * uvicorn.protocols.websockets.websockets_impl.WebSocketProtocol.asgi_send —
 * the `elif message_type == "websocket.close"` branch hardcodes
 * `self.initial_response = (http.HTTPStatus.FORBIDDEN, [], b"")`
 * unconditionally). The browser's native WebSocket API mirrors that opacity:
 * a rejected handshake never fires `onopen` and always reports close code
 * 1006 with an empty reason, regardless of which of the three gates fired
 * server-side. `event.code` can therefore never actually be 1008/4401/4403
 * for this route in production — those branches below are unreachable there
 * (they only fire in a test harness that fakes accept-then-close). The one
 * signal the client CAN observe is "did this connection attempt ever open",
 * used below to stop the reconnect loop and show an honest message instead
 * of hammering a permanently-rejected socket every RECONNECT_DELAY_MS forever.
 */

// How long the old frame's object URL lingers so the crossfade can finish
// before we revoke it (matches the 150ms CSS transition + headroom).
const FRAME_REVOKE_DELAY_MS = 300;
// Keepalive cadence — well under any idle proxy timeout.
const PING_INTERVAL_MS = 25_000;
// Reconnect backoff after a transient socket close while still enabled.
const RECONNECT_DELAY_MS = 2_000;
// How many consecutive attempts may fail to ever open (a handshake-level
// rejection — kill-switch / origin / auth / plan-tier, all indistinguishable
// once uvicorn collapses them to a bare 403) before giving up and surfacing
// an honest "unavailable" message instead of retrying forever (#1061).
const MAX_HANDSHAKE_REJECT_RETRIES = 3;

// stream_error codes (or WS close reasons) that are terminal: the agent isn't
// browsing or the user can't use the feature, so reconnecting would just spin.
const TERMINAL_ERROR_CODES = new Set([
  'no_agent_browser',
  'plan_tier_required',
  'agent_browser_disabled',
  'stream_unavailable',
]);

function buildAgentBrowserUrl(token) {
  const base = window.__VIOLA_BASE_URL__ || window.location.origin;
  const wsBase = base.replace(/^http/, 'ws');
  const qs = token ? `?token=${encodeURIComponent(token)}` : '';
  return `${wsBase}/ws/agent-browser${qs}`;
}

/**
 * @param {object}  options
 * @param {boolean} options.enabled  Open the stream only when true. Toggling to
 *   false closes the socket and clears all frame/error state.
 * @returns {{
 *   frameSrc: string|null,
 *   frameFade: string|null,
 *   ready: boolean,
 *   sessionId: string|null,
 *   viewerId: number|null,
 *   frameFormat: string|null,
 *   status: 'idle'|'connecting'|'connected'|'ready'|'error'|'ended',
 *   streamError: { code: string, message: string }|null,
 *   inputBlocked: boolean,
 *   sendInput: (payload: object) => boolean,
 * }}
 */
export function useAgentBrowserStream({ enabled = false } = {}) {
  const [frameSrc, setFrameSrc] = useState(null);
  const [frameFade, setFrameFade] = useState(null);
  const [ready, setReady] = useState(false);
  const [sessionId, setSessionId] = useState(null);
  const [viewerId, setViewerId] = useState(null);
  const [frameFormat, setFrameFormat] = useState(null);
  const [status, setStatus] = useState('idle');
  const [streamError, setStreamError] = useState(null);
  const [inputBlocked, setInputBlocked] = useState(false);

  const wsRef = useRef(null);
  // The object URL currently shown — revoked when replaced/torn down.
  const currentUrlRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const pingTimerRef = useRef(null);
  // True once a terminal error landed, so onclose does NOT schedule a reconnect.
  const terminalRef = useRef(false);
  // Bumps to invalidate any in-flight async connect when enabled flips.
  const connectGenRef = useRef(0);
  // True once THIS attempt's onopen has fired. Reset per attempt in openSocket().
  const attemptOpenedRef = useRef(false);
  // Consecutive attempts that closed WITHOUT ever opening (handshake-level
  // rejection — see the module docstring). Reset to 0 on any successful open.
  const neverOpenedStreakRef = useRef(0);

  const clearTimers = useCallback(() => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (pingTimerRef.current) {
      clearInterval(pingTimerRef.current);
      pingTimerRef.current = null;
    }
  }, []);

  const revokeCurrentUrl = useCallback(() => {
    if (currentUrlRef.current) {
      URL.revokeObjectURL(currentUrlRef.current);
      currentUrlRef.current = null;
    }
  }, []);

  // sendInput relays a takeover/browse event to the headless page. Returns
  // false (and sends nothing) when the socket isn't open yet.
  const sendInput = useCallback((payload) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    try {
      ws.send(JSON.stringify({ action: 'agent_browser_input', payload }));
      return true;
    } catch {
      return false;
    }
  }, []);

  useEffect(() => {
    if (!enabled) {
      // Disabled: tear everything down and reset to idle.
      connectGenRef.current += 1;
      clearTimers();
      if (wsRef.current) {
        const ws = wsRef.current;
        wsRef.current = null;
        try { ws.close(1000, 'disabled'); } catch { /* already closing */ }
      }
      revokeCurrentUrl();
      terminalRef.current = false;
      neverOpenedStreakRef.current = 0;
      setFrameSrc(null);
      setFrameFade(null);
      setReady(false);
      setSessionId(null);
      setViewerId(null);
      setFrameFormat(null);
      setStreamError(null);
      setInputBlocked(false);
      setStatus('idle');
      return undefined;
    }

    let cancelled = false;
    const myGen = connectGenRef.current + 1;
    connectGenRef.current = myGen;
    terminalRef.current = false;
    neverOpenedStreakRef.current = 0;

    const scheduleReconnect = () => {
      if (cancelled || terminalRef.current) return;
      if (reconnectTimerRef.current) return;
      reconnectTimerRef.current = setTimeout(() => {
        reconnectTimerRef.current = null;
        void openSocket();
      }, RECONNECT_DELAY_MS);
    };

    const handleFrame = (arrayBuffer) => {
      const blob = new Blob([arrayBuffer], { type: 'image/jpeg' });
      const nextUrl = URL.createObjectURL(blob);
      const prevUrl = currentUrlRef.current;
      currentUrlRef.current = nextUrl;
      // Crossfade: the previous frame fades out underneath the new one.
      setFrameFade(prevUrl);
      setFrameSrc(nextUrl);
      if (prevUrl) {
        setTimeout(() => URL.revokeObjectURL(prevUrl), FRAME_REVOKE_DELAY_MS);
      }
    };

    const handleJson = (data) => {
      const type = data?.type;
      if (type === 'stream_ready') {
        setReady(true);
        setStatus('ready');
        setStreamError(null);
        setSessionId(data.session_id ?? null);
        setViewerId(typeof data.viewer_id === 'number' ? data.viewer_id : null);
        setFrameFormat(data.frame_format ?? null);
        return;
      }
      if (type === 'stream_error') {
        const code = data.code || 'stream_error';
        if (TERMINAL_ERROR_CODES.has(code)) terminalRef.current = true;
        setStreamError({ code, message: data.message || '' });
        setReady(false);
        setStatus('error');
        return;
      }
      if (type === 'input_blocked') {
        // Transient: the focused field is credential-shaped. Surface a flag the
        // stage can flash; the stream stays open. We never see the raw text.
        setInputBlocked(true);
        return;
      }
      if (type === 'stream_end') {
        terminalRef.current = true;
        setReady(false);
        setStatus('ended');
        return;
      }
      // pong / unknown types are ignored.
    };

    async function openSocket() {
      if (cancelled || connectGenRef.current !== myGen) return;
      setStatus('connecting');
      setInputBlocked(false);
      attemptOpenedRef.current = false;

      let token = '';
      try {
        token = (await getWebSocketAuthToken()) || '';
      } catch {
        token = '';
      }
      if (cancelled || connectGenRef.current !== myGen) return;

      let ws;
      try {
        ws = new WebSocket(buildAgentBrowserUrl(token));
      } catch {
        neverOpenedStreakRef.current += 1;
        if (neverOpenedStreakRef.current >= MAX_HANDSHAKE_REJECT_RETRIES) {
          terminalRef.current = true;
          setStreamError((prev) => prev || {
            code: 'stream_unavailable',
            message: 'Live browsing could not connect.',
          });
        }
        setStatus('error');
        scheduleReconnect();
        return;
      }
      ws.binaryType = 'arraybuffer';
      wsRef.current = ws;

      ws.onopen = () => {
        if (wsRef.current !== ws) return;
        attemptOpenedRef.current = true;
        neverOpenedStreakRef.current = 0;
        setStatus('connected');
        if (pingTimerRef.current) clearInterval(pingTimerRef.current);
        pingTimerRef.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            try { ws.send(JSON.stringify({ action: 'ping' })); } catch { /* closing */ }
          }
        }, PING_INTERVAL_MS);
      };

      ws.onmessage = (event) => {
        if (wsRef.current !== ws) return;
        if (event.data instanceof ArrayBuffer) {
          handleFrame(event.data);
          return;
        }
        try {
          handleJson(JSON.parse(event.data));
        } catch {
          /* malformed JSON frame — ignore, never crash the stream */
        }
      };

      ws.onerror = () => {
        if (wsRef.current !== ws) return;
        // onclose follows and drives reconnect/teardown.
        if (!terminalRef.current) setStatus('error');
      };

      ws.onclose = (event) => {
        if (wsRef.current !== ws) return;
        wsRef.current = null;
        if (pingTimerRef.current) {
          clearInterval(pingTimerRef.current);
          pingTimerRef.current = null;
        }
        setReady(false);
        // Map the policy-violation close codes to a surfaced stream_error so the
        // stage shows the reason instead of a permanent spinner. In production
        // these three branches are unreachable for THIS route (see the module
        // docstring: uvicorn collapses every pre-accept close to a bare 403,
        // so the browser never sees 1008/4401/4403 here) — kept for any WS
        // route/environment that DOES deliver protocol-level close codes.
        if (event.code === 1008) {
          const reason = event.reason || '';
          const code = reason.includes('plan') ? 'plan_tier_required' : 'agent_browser_disabled';
          terminalRef.current = true;
          setStreamError((prev) => prev || { code, message: reason });
          setStatus('error');
        } else if (event.code === 4401 || event.code === 4403) {
          terminalRef.current = true;
          setStatus('error');
        } else if (!attemptOpenedRef.current) {
          // The handshake itself never completed (no onopen fired) — a
          // kill-switch / origin / auth / plan-tier rejection, OR a genuine
          // transient network failure; the browser cannot tell them apart
          // (#1061). Retry a bounded number of times in case it's transient,
          // then stop and show an honest message instead of hammering a
          // permanently-rejected socket every RECONNECT_DELAY_MS forever.
          neverOpenedStreakRef.current += 1;
          if (neverOpenedStreakRef.current >= MAX_HANDSHAKE_REJECT_RETRIES) {
            terminalRef.current = true;
            setStreamError((prev) => prev || {
              code: 'stream_unavailable',
              message: 'Live browsing could not connect.',
            });
            setStatus('error');
          } else if (status !== 'ended') {
            setStatus('connecting');
          }
        } else if (!terminalRef.current && status !== 'ended') {
          // Was open, then dropped (e.g. a mid-stream server restart) — a
          // genuinely transient case, keep retrying without a cap.
          setStatus('connecting');
        }
        scheduleReconnect();
      };
    }

    void openSocket();

    return () => {
      cancelled = true;
      clearTimers();
      const ws = wsRef.current;
      if (ws) {
        wsRef.current = null;
        try { ws.close(1000, 'unmount'); } catch { /* already closing */ }
      }
      revokeCurrentUrl();
    };
    // `status` is intentionally NOT a dep: it's read transiently inside onclose
    // and including it would tear down the socket on every status change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, clearTimers, revokeCurrentUrl]);

  return {
    frameSrc,
    frameFade,
    ready,
    sessionId,
    viewerId,
    frameFormat,
    status,
    streamError,
    inputBlocked,
    sendInput,
  };
}

export default useAgentBrowserStream;
