/**
 * useCallAudio — WebSocket hook for live call audio listen-in.
 *
 * Connects to /ws/call-listen/{callId}, receives binary PCM audio
 * with direction byte prefix, plays via AudioContext.
 *
 * @param {string} callId - Active call ID to listen to
 * @returns {{ isListening, startListening, stopListening, callStatus, takeoverActive, startTakeover, releaseTakeover, requestOwnerTakeover, transcripts, latestCostUsd, recipientState, sendOperatorMessage, endCall }}
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { authFetch } from './useViolaApi';
import { getWebSocketAuthToken } from '../lib/ws_auth';

const SAMPLE_RATE = 16000;
const DIR_INBOUND = 0x00;
const DIR_OUTBOUND = 0x01;
const TAKEOVER_AUDIO_CONSTRAINTS = {
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
  channelCount: 1,
  sampleRate: SAMPLE_RATE,
};

// Build an error for a failed phone/call request. The MESSAGE is plain-English
// and safe to render to a user (issue #768: the Phone tab used to show
// "Call history request failed: 404" verbatim); the raw HTTP status is carried
// on error.status for logging and callers that branch on it — never in the
// user-visible message.
function callRequestError(userMessage, status) {
  const error = new Error(userMessage);
  error.status = status;
  return error;
}

export function mergeTranscriptEntry(entries, entry) {
  if (!entry.text) return entries;

  if (!entry._optimistic) {
    const optimisticIndex = entries.findIndex((existing) => (
      existing._optimistic
      && existing.role === entry.role
      && existing.text === entry.text
      && !existing.partial
    ));
    if (optimisticIndex !== -1) {
      return [
        ...entries.slice(0, optimisticIndex),
        entry,
        ...entries.slice(optimisticIndex + 1),
      ];
    }
  }

  const last = entries[entries.length - 1];
  const shouldReplaceLast = last && last.role === entry.role && last.partial === true;

  if (entry.partial) {
    if (shouldReplaceLast) {
      return [...entries.slice(0, -1), entry];
    }
    return [...entries, entry];
  }

  if (shouldReplaceLast) {
    return [...entries.slice(0, -1), entry];
  }

  return [...entries, entry];
}

// DEFERRED — live listen-in AUDIO over /ws/call-listen stays localhost-bound.
// This is the one phone-tab surface NOT cloud-routed in the capstone fix. The
// REST data surfaces (history, transcript, queue, end, operator, takeover) now
// route cloud-first; the live transcript already reaches the desktop over the
// cloud /ws/events bridge (useCloudPhoneEvents). But raw call AUDIO is a
// separate, heavier piece: /ws/call-listen is NOT registered on the cloud
// surface at all (only /ws/phone-media for Telnyx ingress + the user-scoped
// /ws/events hub exist there — see telephony/cloud_routes.py
// _wire_cloud_transcript_event_hub_bridge). Routing live two-direction PCM to
// the cloud needs a NEW cloud audio-fanout WS endpoint (owner-scoped, minted
// ?token= auth) plus a cloud-side tee of the call's mixed audio to subscribers
// — genuinely larger than this data-path fix. Until that exists, listen-in
// audio works only against a local call; for a cloud call the transcript shows
// (via /ws/events) but the audio stream does not.
async function buildCallListenUrl(callId) {
  const base = window.__VIOLA_BASE_URL__ || window.location.origin;
  const wsBase = base.replace(/^http/, 'ws');
  const params = new URLSearchParams();

  try {
    const token = await getWebSocketAuthToken();
    if (token) params.set('token', token);
  } catch {
    // SEC-005 (requal-M2 follow-through): never fall back to ?api_key= in
    // the URL — the raw key would land in server logs, proxy logs, and
    // browser history. If the token mint fails the socket connects
    // unauthenticated and is rejected; the mint failure is the bug.
  }

  const qs = params.toString();
  return `${wsBase}/ws/call-listen/${encodeURIComponent(callId)}${qs ? `?${qs}` : ''}`;
}

function unwrapEnvelope(json, fallback = {}) {
  if (json && typeof json === 'object' && Object.prototype.hasOwnProperty.call(json, 'data') && json.ok !== false) {
    return json.data || fallback;
  }
  return json || fallback;
}

function extractPaidActionGatePayload(json) {
  const payload = json?.payload || json?.data || json || {};
  const nested = payload?.data && typeof payload.data === 'object' ? payload.data : {};
  const merged = { ...nested, ...payload };
  if (merged.error_code === 'login_required_for_paid_action' || merged.error_code === 'phone_tos_required') {
    return merged;
  }
  return null;
}

export function notifyPaidActionError(json) {
  const payload = extractPaidActionGatePayload(json);
  if (!payload || typeof window === 'undefined') return false;
  const eventName = payload.error_code === 'phone_tos_required'
    ? 'viola:phone-tos-required'
    : 'viola:paid-action-login-required';
  window.dispatchEvent(new CustomEvent(eventName, { detail: payload }));
  return true;
}

export async function fetchCallHistory(limit = 50, offset = 0) {
  const pageLimit = Number.isFinite(limit) ? Math.max(1, Math.min(200, Math.floor(limit))) : 50;
  const pageOffset = Number.isFinite(offset) ? Math.max(0, Math.floor(offset)) : 0;
  const params = new URLSearchParams({
    limit: String(pageLimit),
    offset: String(pageOffset),
  });
  // Always hit the desktop's LOCAL backend. When phone_mode is cloud (the SaaS
  // default), that backend PROXIES /v1/phone/history to the cloud's
  // /api/phone/history using the logged-in user's cloud bearer SERVER-SIDE — the
  // bearer never enters the browser (SEC-017). On a cloud/LAN web client the
  // local backend IS the cloud, so the same call works same-origin.
  const response = await authFetch(`/v1/phone/history?${params.toString()}`);
  if (!response.ok) {
    const json = await response.json().catch(() => null);
    notifyPaidActionError(json);
    throw callRequestError('Could not load your call history. Please try again.', response.status);
  }
  const data = unwrapEnvelope(await response.json(), { count: 0, calls: [] });
  return {
    count: Number(data.count) || 0,
    calls: Array.isArray(data.calls) ? data.calls : [],
  };
}

export async function getCallTranscript(callId) {
  if (!callId) return null;
  // Hit the LOCAL backend; in cloud mode it proxies to the cloud's
  // /api/phone/history/transcript with the server-side bearer (a past cloud
  // call's transcript lives only in the cloud container).
  const response = await authFetch(`/v1/phone/calls/${encodeURIComponent(callId)}/transcript`);
  if (!response.ok) {
    const json = await response.json().catch(() => null);
    notifyPaidActionError(json);
    throw callRequestError('Could not load the call transcript. Please try again.', response.status);
  }
  return unwrapEnvelope(await response.json(), null);
}

export async function fetchActiveCall() {
  // Recover the live call for a phone tab opened mid-call. The desktop learns
  // activeCallId from transient call_started / call_consultation WebSocket
  // events; a tab opened after those fired (or a reconnected socket) has no
  // way to know a call is live and would fall back to the history list. This
  // asks "is a call live for me right now?" — returns null when none, never
  // throwing on the no-call case.
  //
  // Hit the LOCAL backend; in cloud mode it proxies to the cloud's
  // /api/phone/active with the server-side bearer (the live call lives in the
  // cloud CallManager, not localhost).
  const response = await authFetch('/v1/calls/active');
  if (!response.ok) {
    const json = await response.json().catch(() => null);
    notifyPaidActionError(json);
    throw callRequestError('Could not check for an active call. Please try again.', response.status);
  }
  const data = unwrapEnvelope(await response.json(), { active_call: null });
  const active = data && typeof data === 'object' ? data.active_call : null;
  return active && typeof active === 'object' && active.call_id ? active : null;
}

export async function fetchCallQueue() {
  // Hit the LOCAL backend; in cloud mode it proxies to the cloud's
  // /api/phone/queue with the server-side bearer (cloud calls queue in the
  // cloud CallManager).
  const response = await authFetch('/v1/calls/queue');
  if (!response.ok) {
    const json = await response.json().catch(() => null);
    notifyPaidActionError(json);
    throw callRequestError('Could not load the call queue. Please try again.', response.status);
  }
  const data = unwrapEnvelope(await response.json(), { queue: [] });
  return Array.isArray(data.queue) ? data.queue : [];
}

export async function removeQueuedCall(position) {
  const safePosition = Number(position);
  if (!Number.isFinite(safePosition) || safePosition < 1) return null;
  const safePath = Math.floor(safePosition);
  // Hit the LOCAL backend; in cloud mode it proxies the removal to the cloud's
  // /api/phone/queue/{position} with the server-side bearer.
  const response = await authFetch(`/v1/calls/queue/${safePath}`, { method: 'DELETE' });
  const json = await response.json().catch(() => null);
  if (!response.ok) {
    notifyPaidActionError(json);
    throw callRequestError('Could not remove the queued call. Please try again.', response.status);
  }
  return unwrapEnvelope(json, null);
}

export default function useCallAudio(callId) {
  const [isListening, setIsListening] = useState(false);
  const [callStatus, setCallStatus] = useState(null);
  const [takeoverActive, setTakeoverActive] = useState(false);
  const [transcripts, setTranscripts] = useState([]);
  const [latestCostUsd, setLatestCostUsd] = useState(null);
  const [recipientState, setRecipientState] = useState(null);

  const wsRef = useRef(null);
  const connectingRef = useRef(false);
  const audioCtxRef = useRef(null);
  const nextPlayTimeRef = useRef(0);
  const micStreamRef = useRef(null);
  const micContextRef = useRef(null);
  const micSourceNodeRef = useRef(null);
  const micWorkletNodeRef = useRef(null);

  const teardownMicCapture = useCallback(() => {
    if (micWorkletNodeRef.current) {
      try {
        micWorkletNodeRef.current.port.onmessage = null;
        micWorkletNodeRef.current.disconnect();
      } catch (_) {
        // Best effort: the node may already be disconnected.
      }
      micWorkletNodeRef.current = null;
    }

    if (micStreamRef.current) {
      micStreamRef.current.getTracks().forEach((track) => track.stop());
      micStreamRef.current = null;
    }

    if (micSourceNodeRef.current) {
      try {
        micSourceNodeRef.current.disconnect();
      } catch (_) {
        // Best effort: the source may already be disconnected.
      }
      micSourceNodeRef.current = null;
    }

    if (micContextRef.current) {
      const context = micContextRef.current;
      micContextRef.current = null;
      if (context.state !== 'closed') {
        context.close().catch(() => {});
      }
    }
  }, []);

  const getAudioContext = useCallback(() => {
    if (!audioCtxRef.current) {
      audioCtxRef.current = new (window.AudioContext || window.webkitAudioContext)({
        sampleRate: SAMPLE_RATE,
      });
    }
    return audioCtxRef.current;
  }, []);

  const playPcm = useCallback(
    (pcmBytes) => {
      const ctx = getAudioContext();
      if (ctx.state === 'suspended') ctx.resume();

      // Convert Int16 PCM to Float32
      const int16 = new Int16Array(pcmBytes.buffer, pcmBytes.byteOffset, pcmBytes.length / 2);
      const float32 = new Float32Array(int16.length);
      for (let i = 0; i < int16.length; i++) {
        float32[i] = int16[i] / 32768;
      }

      const buffer = ctx.createBuffer(1, float32.length, SAMPLE_RATE);
      buffer.getChannelData(0).set(float32);

      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(ctx.destination);

      const now = ctx.currentTime;
      const startTime = Math.max(now, nextPlayTimeRef.current);
      source.start(startTime);
      nextPlayTimeRef.current = startTime + buffer.duration;
    },
    [getAudioContext]
  );

  const startListening = useCallback(async () => {
    // Issue #2770: `buildCallListenUrl` awaits an auth-token mint before the
    // socket is created, and `wsRef.current` isn't assigned until after that
    // await resolves. Two rapid/re-entrant invocations (e.g. a double-click
    // on "Listen") would both see `wsRef.current` as null at the top of this
    // function and both proceed to open a socket — the second overwrites
    // `wsRef.current`, orphaning the first (never closed, still playing
    // audio). `connectingRef` is set synchronously, before any `await`, so a
    // second concurrent call bails out immediately instead of racing.
    if (!callId || wsRef.current || connectingRef.current) return;
    connectingRef.current = true;

    let ws;
    try {
      const url = await buildCallListenUrl(callId);

      // Re-check after the await: a concurrent call could have slipped in
      // (belt-and-suspenders alongside connectingRef), or stopListening/
      // unmount could have run while we were awaiting the token mint.
      if (wsRef.current) return;

      ws = new WebSocket(url);
    } finally {
      connectingRef.current = false;
    }
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      setIsListening(true);
      nextPlayTimeRef.current = 0;
    };

    ws.onmessage = (event) => {
      if (typeof event.data === 'string') {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === 'call_status') {
            setCallStatus(msg.status);
            if (msg.status === 'takeover') setTakeoverActive(true);
            else if (msg.status === 'listening') setTakeoverActive(false);
          } else if (msg.type === 'transcript') {
            const transcriptPayload = msg.payload || msg;
            const entry = {
              role: ['them', 'viola', 'system'].includes(transcriptPayload.role)
                ? transcriptPayload.role
                : 'system',
              text: typeof transcriptPayload.text === 'string' ? transcriptPayload.text : '',
              partial: Boolean(transcriptPayload.partial),
              ts: transcriptPayload.ts || Date.now(),
            };
            setTranscripts((current) => mergeTranscriptEntry(current, entry));
          } else if (msg.type === 'call_cost_update') {
            const payload = msg.payload || msg;
            const cost = Number(
              payload.current_cost_usd
              ?? payload.cost_usd
              ?? payload.estimated_cost_usd
            );
            if (Number.isFinite(cost)) setLatestCostUsd(cost);
            if (typeof payload.recipient_state === 'string') {
              setRecipientState(payload.recipient_state);
            }
          } else if (typeof msg.recipient_state === 'string') {
            setRecipientState(msg.recipient_state);
          }
        } catch (e) {
          // Ignore parse errors
        }
      } else {
        // Binary: direction byte + PCM
        const data = new Uint8Array(event.data);
        if (data.length > 1) {
          const pcm = data.slice(1);
          playPcm(pcm);
        }
      }
    };

    ws.onclose = () => {
      setIsListening(false);
      setCallStatus(null);
      setTakeoverActive(false);
      teardownMicCapture();
      // Only clear the ref if it still points at THIS socket. An orphaned
      // older socket's close/error firing after a newer one has taken over
      // wsRef must never null out the live socket's reference (issue #2770).
      if (wsRef.current === ws) wsRef.current = null;
    };

    ws.onerror = () => {
      setIsListening(false);
      setTakeoverActive(false);
      teardownMicCapture();
      if (wsRef.current === ws) wsRef.current = null;
    };

    wsRef.current = ws;
  }, [callId, playPcm, teardownMicCapture]);

  const stopListening = useCallback(() => {
    teardownMicCapture();
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setIsListening(false);
    setTakeoverActive(false);
  }, [teardownMicCapture]);

  const startTakeover = useCallback(async () => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;

    try {
      if (!micStreamRef.current) {
        if (!navigator.mediaDevices?.getUserMedia) {
          throw new Error('mediaDevices.getUserMedia is not available');
        }

        const stream = await navigator.mediaDevices.getUserMedia({
          audio: TAKEOVER_AUDIO_CONSTRAINTS,
        });
        micStreamRef.current = stream;
        const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextCtor || !window.AudioWorkletNode) {
          throw new Error('AudioWorklet capture is not available');
        }

        const audioContext = new AudioContextCtor({ sampleRate: SAMPLE_RATE });
        micContextRef.current = audioContext;
        await audioContext.audioWorklet.addModule('/audio-worklets/takeover-pcm-worklet.js');

        const source = audioContext.createMediaStreamSource(stream);
        micSourceNodeRef.current = source;
        const worklet = new AudioWorkletNode(audioContext, 'takeover-pcm-worklet', {
          numberOfOutputs: 0,
        });
        worklet.port.onmessage = (event) => {
          if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
            wsRef.current.send(event.data);
          }
        };

        source.connect(worklet);
        micWorkletNodeRef.current = worklet;
      }
    } catch (error) {
      console.warn('[useCallAudio] takeover mic capture failed:', error);
      teardownMicCapture();
    } finally {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: 'takeover' }));
      }
    }
  }, [teardownMicCapture]);

  const releaseTakeover = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'release' }));
    }
    teardownMicCapture();
  }, [teardownMicCapture]);

  const endCall = useCallback(async (targetCallId = callId) => {
    if (!targetCallId) return null;
    try {
      // Hit the LOCAL backend; in cloud mode it proxies the hang-up to the
      // cloud's /api/phone/calls/{id} with the server-side bearer (a live cloud
      // call lives in the cloud CallManager).
      const response = await authFetch(`/v1/phone/calls/${encodeURIComponent(targetCallId)}`, {
        method: 'DELETE',
      });
      if (!response.ok) {
        console.warn('[useCallAudio] endCall failed:', response.status);
      }
      try {
        const json = await response.json();
        if (!response.ok) notifyPaidActionError(json);
        return json;
      } catch (_) {
        return null;
      }
    } catch (error) {
      console.warn('[useCallAudio] endCall request failed:', error);
      return null;
    }
  }, [callId]);

  const sendOperatorMessage = useCallback(async (text, targetCallId = callId) => {
    const message = typeof text === 'string' ? text.trim() : '';
    if (!targetCallId || !message) return null;

    const transcriptEntry = {
      role: 'system',
      text: `[operator] ${message}`,
      partial: false,
      ts: new Date().toISOString(),
      _optimistic: true,
    };
    setTranscripts((current) => mergeTranscriptEntry(current, transcriptEntry));

    try {
      // Hit the LOCAL backend; in cloud mode it proxies the note to the cloud's
      // /api/phone/calls/{id}/operator-message with the server-side bearer.
      const operatorBody = {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: message }),
      };
      const response = await authFetch(
        `/v1/phone/calls/${encodeURIComponent(targetCallId)}/operator-message`,
        operatorBody
      );
      if (!response.ok) {
        console.warn('[useCallAudio] operator message failed:', response.status);
        const json = await response.json().catch(() => null);
        notifyPaidActionError(json);
        return null;
      }
      try {
        return await response.json();
      } catch (_) {
        return null;
      }
    } catch (error) {
      console.warn('[useCallAudio] operator message request failed:', error);
      return null;
    }
  }, [callId]);

  const requestOwnerTakeover = useCallback(async (
    targetCallId = callId,
    reason = 'Owner requested live takeover from the phone-call UI.'
  ) => {
    if (!targetCallId) return null;
    try {
      // Hit the LOCAL backend; in cloud mode it proxies the takeover to the
      // cloud's /api/phone/calls/{id}/takeover with the server-side bearer (the
      // Telnyx conference is dialed from the cloud CallManager).
      const takeoverBody = {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason }),
      };
      const response = await authFetch(
        `/v1/phone/calls/${encodeURIComponent(targetCallId)}/takeover`,
        takeoverBody
      );
      const json = await response.json().catch(() => null);
      if (!response.ok) {
        console.warn('[useCallAudio] owner takeover failed:', response.status);
        notifyPaidActionError(json);
        return null;
      }
      return json;
    } catch (error) {
      console.warn('[useCallAudio] owner takeover request failed:', error);
      return null;
    }
  }, [callId]);

  // Inject a transcript line that arrived OUTSIDE the local call-listen socket.
  // Cloud calls live in the cloud container, so their live transcript reaches
  // the desktop over the cloud /ws/events channel (useCloudPhoneEvents) rather
  // than the localhost /ws/call-listen socket. Merging here keeps a single
  // source of truth for the transcripts prop PhoneCallPanel renders.
  const pushTranscript = useCallback((entry) => {
    if (!entry || typeof entry !== 'object') return;
    // The cloud /ws/events feed is USER-scoped, so it carries every call this
    // account has live (several are allowed — telephony/call_manager.py). Keep
    // another call's speech out of THIS call's panel. Only a positive mismatch
    // drops: an entry with no call_id (the per-call /ws/call-listen shape, and
    // any older frame) still merges, and so does anything arriving before
    // callId settles — those are cleared by the callId-change reset below
    // exactly as they are today, so this can only remove provably foreign lines.
    const entryCallId = typeof entry.call_id === 'string' ? entry.call_id : '';
    if (entryCallId && callId && entryCallId !== callId) return;
    const normalized = {
      role: ['them', 'viola', 'system'].includes(entry.role) ? entry.role : 'system',
      text: typeof entry.text === 'string' ? entry.text : '',
      partial: Boolean(entry.partial),
      ts: entry.ts || Date.now(),
    };
    setTranscripts((current) => mergeTranscriptEntry(current, normalized));
  }, [callId]);

  // Apply a cost/recipient-state update that arrived outside the call-listen
  // socket (cloud /ws/events call_cost_update). Mirrors the in-socket handler.
  const applyCostUpdate = useCallback((payload) => {
    if (!payload || typeof payload !== 'object') return;
    const cost = Number(
      payload.current_cost_usd ?? payload.cost_usd ?? payload.estimated_cost_usd
    );
    if (Number.isFinite(cost)) setLatestCostUsd(cost);
    if (typeof payload.recipient_state === 'string') {
      setRecipientState(payload.recipient_state);
    }
  }, []);

  useEffect(() => {
    setTranscripts([]);
    setLatestCostUsd(null);
    setRecipientState(null);
    stopListening();
  }, [callId, stopListening]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      stopListening();
      if (audioCtxRef.current) {
        audioCtxRef.current.close();
        audioCtxRef.current = null;
      }
    };
  }, [stopListening]);

  return {
    isListening,
    startListening,
    stopListening,
    callStatus,
    takeoverActive,
    startTakeover,
    releaseTakeover,
    requestOwnerTakeover,
    transcripts,
    latestCostUsd,
    recipientState,
    sendOperatorMessage,
    endCall,
    pushTranscript,
    applyCostUpdate,
  };
}
