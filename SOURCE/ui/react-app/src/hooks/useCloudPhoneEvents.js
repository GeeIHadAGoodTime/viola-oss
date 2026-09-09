/**
 * useCloudPhoneEvents — live cloud phone-call events on the desktop, with NO
 * browser-held cloud token (capstone live transcript, 2026-06-30).
 *
 * Background
 * ----------
 * Phone calls are cloud-only: a live call lives in the cloud container's
 * CallManager, which broadcasts call_started / call_consultation /
 * call_cost_update / recipient_state / call_queue_updated / call_ended AND live
 * transcript lines to the CLOUD /ws/events hub, scoped to the call's owner.
 *
 * SEC-017 forbids putting the cloud GoTrue bearer in the browser, so this hook
 * does NOT open its own socket to the cloud (the earlier, reverted design did,
 * and was dead on the desktop because the browser bearer is always empty there).
 * Instead the desktop's LOCAL backend subscribes to the cloud /ws/events
 * SERVER-SIDE with the bearer resolved server-side and republishes the phone
 * events onto the desktop's LOCAL /ws/events hub
 * (telephony/phone_cloud_event_relay.py). This hook therefore:
 *
 *   1. Drives that server-side relay's lifecycle: it asks the LOCAL backend to
 *      start the relay while the user cares about phone events (`enabled`) and
 *      to tear it down when there is no call. The request carries only the local
 *      api key — never a cloud token.
 *   2. Listens to the LOCAL shared /ws/events socket (the same one the rest of
 *      the app uses) and routes the live `transcript` lines to `onTranscript`
 *      and the cost/phone events to `onMessage`, so the call screen renders the
 *      streaming transcript and ticking cost without any browser cloud bearer.
 *
 * On a same-origin web client (useviola.com) the cloud IS the origin, so its
 * normal /ws/events socket already carries these events — the relay start call
 * is a clean no-op there and the same local-socket listener renders them.
 *
 * @param {object}   params
 * @param {boolean}  params.enabled      - whether phone events are wanted now.
 * @param {function} params.onMessage    - receives phone event frames {type,payload}.
 * @param {function} params.onTranscript - receives normalized transcript entries.
 */
import { useCallback, useEffect, useRef } from 'react';
import { useWebSocket } from './useWebSocket';
import { authFetch } from './useViolaApi';

// The cloud phone events the call screen consumes off the LOCAL socket. Anything
// else on that socket (player state, playback, ...) is ignored here.
const PHONE_EVENT_TYPES = new Set([
  'call_started',
  'call_consultation',
  'call_briefing',
  'call_cost_update',
  'recipient_state',
  'call_queue_updated',
  'call_ended',
]);

export default function useCloudPhoneEvents({ enabled = false, onMessage, onTranscript } = {}) {
  const onMessageRef = useRef(onMessage);
  const onTranscriptRef = useRef(onTranscript);
  useEffect(() => {
    onMessageRef.current = onMessage;
  }, [onMessage]);
  useEffect(() => {
    onTranscriptRef.current = onTranscript;
  }, [onTranscript]);

  // Lifecycle: ask the LOCAL backend to run the server-side cloud->local relay
  // while phone events are wanted, and tear it down otherwise. authFetch uses the
  // local api key only; the cloud bearer is resolved server-side (SEC-017).
  useEffect(() => {
    if (!enabled) return undefined;
    (async () => {
      try {
        await authFetch('/v1/phone/cloud-events/start', { method: 'POST' });
      } catch (_) {
        // Best-effort: a same-origin web client (no local relay) and transient
        // errors are both fine — the local-socket listener below still renders.
      }
    })();
    return () => {
      (async () => {
        try {
          await authFetch('/v1/phone/cloud-events/stop', { method: 'POST' });
        } catch (_) {
          // Best-effort teardown; the relay also self-stops on app shutdown.
        }
      })();
    };
  }, [enabled]);

  // Route the relayed (desktop) / same-origin (web) phone events off the LOCAL
  // /ws/events socket: transcript lines to onTranscript, phone events to onMessage.
  const handleLocalSocketMessage = useCallback((data) => {
    if (!data || typeof data !== 'object') return;
    const type = data.type;
    if (type === 'transcript') {
      const p = data.payload || data;
      const entry = {
        // The cloud /ws/events hub is scoped to the USER, not to one call, and a
        // single account may have several calls live at once (up to
        // config.max_concurrent_calls, default 25 — telephony/call_manager.py).
        // So this socket carries the transcript of EVERY call the account has
        // running, and the call_id is the only thing that tells them apart.
        // Carry it through so the consumer can keep another call's speech out of
        // the panel (useCallAudio.pushTranscript). The localhost
        // /ws/call-listen/{callId} socket needs no such field because it is
        // already per-call.
        call_id: typeof p.call_id === 'string' ? p.call_id : '',
        role: ['them', 'viola', 'system'].includes(p.role) ? p.role : 'system',
        text: typeof p.text === 'string' ? p.text : '',
        partial: Boolean(p.partial),
        ts: p.ts || Date.now(),
      };
      if (onTranscriptRef.current) onTranscriptRef.current(entry);
      return;
    }
    if (PHONE_EVENT_TYPES.has(type) && onMessageRef.current) {
      onMessageRef.current(data);
    }
  }, []);

  useWebSocket(handleLocalSocketMessage);
}
