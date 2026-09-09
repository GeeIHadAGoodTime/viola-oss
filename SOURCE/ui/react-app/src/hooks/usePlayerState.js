import { useState, useCallback, useEffect, useRef } from 'react';
import { describeError } from '../utils/describeError';
import { useWebSocket } from './useWebSocket';
import { useViolaApi } from './useViolaApi';

const INITIAL_STATE = {
  is_playing: false,
  now_playing: null,
  queue: [],
  volume: 80,
  position: 0,
  duration: 0,
  position_percentage: 0,
  yt_hub_muted: false,
  hub_local_playback_active: false,
  cef_active: false,
};

const YOUTUBE_ID_PATTERN = /(?:youtube\.com\/watch\?v=|youtube\.com\/embed\/|youtu\.be\/|[?&]video=)([A-Za-z0-9_-]{6,})/;

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function extractYouTubeVideoId(value) {
  if (typeof value !== 'string' || !value) return '';
  if (/^[A-Za-z0-9_-]{11}$/.test(value)) return value;
  const match = value.match(YOUTUBE_ID_PATTERN);
  return match?.[1] || '';
}

function isSameTrack(a, b) {
  if (!isObject(a) || !isObject(b)) return false;
  if (a.id && b.id && a.id === b.id) return true;
  if (a.url && b.url && a.url === b.url) return true;
  return Boolean(a.title && b.title && a.title === b.title && (a.artist || '') === (b.artist || ''));
}

function shouldRehydrateEmbeddedNowPlaying(payload) {
  if (!isObject(payload)) return false;
  const nowPlaying = payload.now_playing;
  if (!isObject(nowPlaying) || nowPlaying.video_id) return false;

  const capabilities = isObject(nowPlaying.capabilities) ? nowPlaying.capabilities : {};
  const provider = String(nowPlaying.provider || '');
  const source = String(nowPlaying.source || '');
  const playbackMode = String(nowPlaying.playback_mode || payload.playback_mode || '');
  const url = String(nowPlaying.url || nowPlaying.stream_token || '');

  return Boolean(
    capabilities.requires_embedded_player
    || capabilities.video_playback
    || playbackMode.includes('embedded')
    || provider.includes('youtube')
    || source.startsWith('yt')
    || extractYouTubeVideoId(url)
  );
}

export function normalizePlayerStatePayloadForTest(payload, previousState = {}) {
  if (!isObject(payload)) return payload;
  const next = { ...payload };
  const nowPlaying = next.now_playing;
  const previousNowPlaying = isObject(previousState) ? previousState.now_playing : null;

  if (isObject(nowPlaying)) {
    const merged = { ...nowPlaying };
    const inferredVideoId = extractYouTubeVideoId(
      merged.video_id || merged.url || merged.stream_token || merged.id || ''
    );
    if (!merged.video_id && inferredVideoId) {
      merged.video_id = inferredVideoId;
    }

    if (isObject(previousNowPlaying) && isSameTrack(previousNowPlaying, merged)) {
      for (const field of [
        'video_id',
        'url',
        'artwork_url',
        'thumbnail_url',
        'capabilities',
        'playback_mode',
        'provider',
        'source',
        'stream_token',
      ]) {
        if (
          (merged[field] === undefined || merged[field] === null || merged[field] === '')
          && previousNowPlaying[field] !== undefined
          && previousNowPlaying[field] !== null
          && previousNowPlaying[field] !== ''
        ) {
          merged[field] = previousNowPlaying[field];
        }
      }
    }

    next.now_playing = merged;
  }

  return next;
}

export const shouldRehydrateEmbeddedNowPlayingForTest = shouldRehydrateEmbeddedNowPlaying;

/**
 * Turn an incoming `type: 'error'` frame into the shape the toast handler reads.
 *
 * Producers send this frame in three different shapes and the consumer used to
 * accept only one of them (`msg.payload`), silently dropping the other two —
 * so the event hub's own "no handler registered" and "handler exception"
 * errors reached the browser and were thrown away.
 */
export function normalizeErrorFrameForTest(msg) {
  const source = (msg && (msg.payload || msg.error)) || msg || {};
  const base = (typeof source === 'object' && source !== null) ? source : {};
  return {
    ...base,
    user_message: describeError(source),
    level: base.level || 'error',
  };
}

export function usePlayerState() {
  const [state, setState] = useState(INITIAL_STATE);
  const [connected, setConnected] = useState(false);
  // Optimistic state: allows UI to update immediately before server confirms
  const [localIsPlaying, setLocalIsPlaying] = useState(null);
  // Callback for handling diagnostic requests
  const diagnosticRequestCallbackRef = useRef(null);
  // Callback for handling error events (toast notifications)
  const errorCallbackRef = useRef(null);
  // Callback for handling spoke-mode messages (room_playback_snapshot, room_playback_command, position_correction)
  const spokeMessageCallbackRef = useRef(null);
  // Callback for browser overlay state changes
  const overlayCallbackRef = useRef(null);
  // Callback for agent progress events
  const agentProgressCallbackRef = useRef(null);
  // Callback for display priority override (voice-commanded display toggle)
  const displayPriorityCallbackRef = useRef(null);
  // Callback for binary agent viewport frames (spoke mode)
  const agentFrameCallbackRef = useRef(null);
  // Callback for playback commands from backend (e.g., seek from voice/REST)
  const playbackCommandCallbackRef = useRef(null);
  // Callback for chat response events (assistant response text via WS)
  const chatResponseCallbackRef = useRef(null);
  // Callback for calendar mutation broadcasts from backend
  const calendarUpdateCallbackRef = useRef(null);
  const rehydrateRef = useRef({ key: null, inFlight: false });
  // Multiroom visual delay timer
  const delayTimerRef = useRef(null);
  const api = useViolaApi();

  const maybeRehydrateEmbeddedState = useCallback((payload) => {
    if (!shouldRehydrateEmbeddedNowPlaying(payload)) return;
    const nowPlaying = payload.now_playing || {};
    const key = [
      nowPlaying.id || '',
      nowPlaying.title || '',
      nowPlaying.url || '',
      payload.playback_mode || nowPlaying.playback_mode || '',
    ].join('|');
    if (rehydrateRef.current.inFlight && rehydrateRef.current.key === key) return;

    rehydrateRef.current = { key, inFlight: true };
    api.getState()
      .then((res) => {
        const authoritative = res?.data || res;
        if (!isObject(authoritative) || !isObject(authoritative.now_playing)) return;
        setState(prev => normalizePlayerStatePayloadForTest({
          ...prev,
          ...authoritative,
        }, prev));
      })
      .catch((err) => {
        console.error('[usePlayerState] Player-state rehydrate fetch failed; UI may show a stale track/position:', err);
      })
      .finally(() => {
        if (rehydrateRef.current.key === key) {
          rehydrateRef.current = { key: null, inFlight: false };
        }
      });
  }, [api]);

  const handleMessage = useCallback((msg) => {
    if (msg.type === 'state' && msg.payload) {
      // DIAGNOSTIC: Log volume changes from WebSocket to backend
      if (import.meta.env.DEV) {
        const newVolume = msg.payload.volume;
        const debugToken = window.__VIOLA_DEBUG_AUTH_TOKEN__ || '';
        if (debugToken) {
          fetch('/v1/debug/yt-iframe-event', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Debug-Auth-Token': debugToken },
            body: JSON.stringify({
              event: 'REACT_WS_STATE',
              volume: newVolume,
              is_playing: msg.payload.is_playing,
              timestamp: Date.now()
            })
          }).catch((err) => {
            console.error('[usePlayerState] DEV volume-diagnostic POST failed:', err);
          });
        }
      }

      // Multiroom visual delay: when hub local playback is active, delay
      // state updates by hub_buffer_ms so the UI matches what the hub
      // speakers are playing.  Volume and error changes bypass the delay.
      const hubActive = msg.payload.hub_local_playback_active || false;
      const hubBufferMs = msg.payload.hub_buffer_ms || 0;

      if (hubActive && hubBufferMs > 0) {
        // Volume and error changes apply IMMEDIATELY
        setState(prev => ({
          ...prev,
          volume: msg.payload.volume !== undefined ? msg.payload.volume : prev.volume,
          playback_errors: msg.payload.playback_errors !== undefined ? msg.payload.playback_errors : prev.playback_errors,
        }));

        // Delayed full state update
        if (delayTimerRef.current !== null) {
          clearTimeout(delayTimerRef.current);
        }
        delayTimerRef.current = setTimeout(() => {
          delayTimerRef.current = null;
          setState(prev => normalizePlayerStatePayloadForTest(msg.payload, prev));
          setConnected(true);
          setLocalIsPlaying(null);
        }, hubBufferMs);
      } else {
        setState(prev => normalizePlayerStatePayloadForTest(msg.payload, prev));
        setConnected(true);
        setLocalIsPlaying(null);
      }
      maybeRehydrateEmbeddedState(msg.payload);
      if (!hubActive || hubBufferMs <= 0) {
        setConnected(true);
        setLocalIsPlaying(null);
      }
    }
    if (msg.type === 'hub_mute_update' && msg.payload) {
      setState(prev => ({
        ...prev,
        yt_hub_muted: msg.payload.yt_hub_muted !== undefined ? msg.payload.yt_hub_muted : prev.yt_hub_muted,
        hub_local_playback_active: msg.payload.hub_local_playback_active !== undefined
          ? msg.payload.hub_local_playback_active
          : prev.hub_local_playback_active,
        cef_active: msg.payload.cef_active !== undefined ? msg.payload.cef_active : prev.cef_active,
      }));
      setConnected(true);
    }
    // Handle diagnostic requests from backend
    if (msg.type === 'diagnostic_request' && msg.payload) {
      if (import.meta.env.DEV) { console.log('[DIAG] Received diagnostic request from backend'); }
      if (diagnosticRequestCallbackRef.current) {
        diagnosticRequestCallbackRef.current(msg.payload);
      }
    }
    // Handle error events from backend (for toast notifications).
    //
    // This used to require `msg.payload`, and NOTHING in the backend has ever
    // sent that key alongside `type: 'error'` — the event hub sends
    // `{type:'error', error:{code, message}}` and the voice-session route sends
    // `{type:'error', message:'...'}`. So the toast channel that exists
    // specifically "to surface backend ErrorContext user_message fields" had
    // never once fired, and every subsystem that needed to tell the user
    // something either grew its own private path or gave up and wrote to a log
    // file. Accept every shape a producer actually emits, and normalise to the
    // one the toast handler reads.
    if (msg.type === 'error') {
      if (errorCallbackRef.current) {
        errorCallbackRef.current(normalizeErrorFrameForTest(msg));
      }
    }
    // Handle spoke-mode messages (late-join snapshot + live commands + sync corrections)
    if (
      (msg.type === 'room_playback_snapshot' || msg.type === 'room_playback_command' || msg.type === 'position_correction')
      && msg.payload
    ) {
      if (spokeMessageCallbackRef.current) {
        if (import.meta.env.DEV) {
          console.log('[SPOKE] Forwarding to handler:', msg.type);
        }
        spokeMessageCallbackRef.current(msg);
      } else {
        console.warn('[SPOKE] No callback registered — message dropped:', msg.type);
      }
    }
    // Handle browser overlay state changes from backend
    if (msg.type === 'browser_overlay_state') {
      if (
        typeof window !== 'undefined'
        && typeof window.dispatchEvent === 'function'
        && typeof CustomEvent === 'function'
      ) {
        window.dispatchEvent(new CustomEvent('viola:browser-activity', {
          detail: {
            visible: msg.visible ?? msg.payload?.visible,
            mode: msg.mode || msg.payload?.mode || '',
            url: msg.url || msg.payload?.url || '',
            agentBusy: Boolean(msg.agent_busy || msg.payload?.agent_busy),
            activity: msg.browser_activity || msg.payload?.browser_activity || null,
            agentTask: msg.agent_task || msg.payload?.agent_task || null,
          },
        }));
      }
      if (overlayCallbackRef.current) {
        overlayCallbackRef.current(msg);
      }
    }
    if (msg.type === 'agent_progress' && msg.payload) {
      if (agentProgressCallbackRef.current) {
        agentProgressCallbackRef.current(msg.payload);
      }
    }
    // Handle display priority override from voice commands
    if (msg.type === 'display_priority_override' && msg.payload) {
      if (displayPriorityCallbackRef.current) {
        displayPriorityCallbackRef.current(msg.payload);
      }
    }
    // CB-7 FIX: Handle playback commands from backend (e.g., seek from voice/REST API).
    // When seek is triggered via voice or REST, the backend broadcasts a command message
    // so the React UI can forward it to the YouTube iframe via postMessage.
    if (msg.type === 'command' && msg.command && msg.payload) {
      if (playbackCommandCallbackRef.current) {
        playbackCommandCallbackRef.current(msg.command, msg.payload);
      }
    }
    // Handle chat response events (assistant response text broadcast from backend)
    if (msg.type === 'chat_response' && msg.payload) {
      if (chatResponseCallbackRef.current) {
        chatResponseCallbackRef.current(msg.payload);
      }
    }
    if (msg.type === 'calendar_updated' && msg.payload) {
      if (calendarUpdateCallbackRef.current) {
        calendarUpdateCallbackRef.current(msg.payload);
      }
    }
  }, [maybeRehydrateEmbeddedState]);

  // The multiroom visual-delay timer above is scheduled from handleMessage,
  // which can fire at any point in this component's lifetime. Without this
  // cleanup, an unmount inside the hub_buffer_ms window leaves the timer
  // pending: it fires setState after unmount (a no-op React warns about) and,
  // if a rehydrate fetch already resolved first, the delayed older payload can
  // still land and overwrite the newer state on a later mount of this hook.
  useEffect(() => {
    return () => {
      if (delayTimerRef.current !== null) {
        clearTimeout(delayTimerRef.current);
        delayTimerRef.current = null;
      }
    };
  }, []);

  // Binary message handler: route agent viewport JPEG frames to callback
  const handleBinaryMessage = useCallback((arrayBuffer) => {
    if (agentFrameCallbackRef.current) {
      agentFrameCallbackRef.current(arrayBuffer);
    }
  }, []);

  const { send, connectCount, setBinaryCallback, setDisconnectCallback, getWsDebug } = useWebSocket(handleMessage, {
    onBinaryMessage: handleBinaryMessage,
  });

  // Function to register a diagnostic request callback
  const setDiagnosticRequestCallback = useCallback((callback) => {
    diagnosticRequestCallbackRef.current = callback;
  }, []);

  // Function to register an error event callback (for toast notifications)
  const setErrorCallback = useCallback((callback) => {
    errorCallbackRef.current = callback;
  }, []);

  // Function to register a spoke message callback (for room_playback_snapshot, room_playback_command, position_correction)
  const setSpokeMessageCallback = useCallback((callback) => {
    spokeMessageCallbackRef.current = callback;
  }, []);

  // Function to register a browser overlay state callback
  const setOverlayCallback = useCallback((callback) => {
    overlayCallbackRef.current = callback;
  }, []);

  const setAgentProgressCallback = useCallback((callback) => {
    agentProgressCallbackRef.current = callback;
  }, []);

  // Function to register a display priority override callback
  const setDisplayPriorityCallback = useCallback((callback) => {
    displayPriorityCallbackRef.current = callback;
  }, []);

  // Function to register an agent frame callback (spoke mode — receives JPEG ArrayBuffers)
  const setAgentFrameCallback = useCallback((callback) => {
    agentFrameCallbackRef.current = callback;
  }, []);

  // Function to register a playback command callback (CB-7: seek from voice/REST → iframe)
  const setPlaybackCommandCallback = useCallback((callback) => {
    playbackCommandCallbackRef.current = callback;
  }, []);

  // Function to register a chat response callback (assistant response text via WS)
  const setChatResponseCallback = useCallback((callback) => {
    chatResponseCallbackRef.current = callback;
  }, []);

  const setCalendarUpdateCallback = useCallback((callback) => {
    calendarUpdateCallbackRef.current = callback;
  }, []);

  // Initial fetch
  useEffect(() => {
    api.getState()
      .then((res) => {
        if (res.ok !== false) {
          setState(res.data || res);
          setConnected(true);
        }
      })
      .catch(() => {
        // Will retry via WebSocket
      });
  }, [api]);

  // Use optimistic state if set, otherwise use server state
  const effectiveIsPlaying = localIsPlaying !== null ? localIsPlaying : state.is_playing;

  return {
    ...state,
    is_playing: effectiveIsPlaying,
    connected,
    send,  // Expose WebSocket send for youtube_state messages
    connectCount,  // Incremented on each WebSocket (re-)connect — for re-subscribing
    setLocalIsPlaying,  // Expose for optimistic updates
    setDiagnosticRequestCallback,  // Expose for diagnostic request handling
    setErrorCallback,  // Expose for error event handling (toast notifications)
    setSpokeMessageCallback,  // Expose for spoke-mode message handling
    setOverlayCallback,  // Expose for browser overlay state handling
    setAgentProgressCallback,  // Expose for agent progress status handling
    setDisplayPriorityCallback,  // Expose for display priority override handling
    setAgentFrameCallback,  // Expose for agent viewport frame streaming (spoke mode)
    setPlaybackCommandCallback,  // Expose for playback command handling (CB-7: seek from voice/REST)
    setChatResponseCallback,  // Expose for chat response handling (assistant text via WS)
    setCalendarUpdateCallback,  // Expose for calendar mutation broadcasts
    setDisconnectCallback,  // Expose for WS disconnect toast notification
    getWsDebug,  // Access WS debug info without subscribing the UI to message-rate renders
  };
}
