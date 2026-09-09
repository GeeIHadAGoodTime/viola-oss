/**
 * useVoiceWs — push-to-talk over /ws/voice-stream for browser spokes.
 *
 * Mirrors the public surface of `useVoice` (isRecording, isProcessing,
 * transcript, error, startRecording, stopRecording, cancelRecording) so
 * SmartDisplay can swap implementations based on isSpoke without
 * reshaping the call site.
 *
 * Why a WebSocket: the HTTP /v1/transcribe + /v1/command flow used by
 * `useVoice` returns JSON only. The hub's TTS response plays via local
 * sounddevice on the hub, and ProcTap capture is locked to whichever
 * child process is currently producing audio (music backend) — so the
 * TTS PCM never reaches the spoke during music playback. The
 * /ws/voice-stream channel sends the response transcript AND the
 * synthesized TTS PCM directly back to the originating spoke, which
 * plays it via the shared TTS playback queue.
 *
 * CONFIRM-6 warm-keeping (campaign: browser Viola parity/companion/quality,
 * twinkling-waddling-sparkle.md): this connection now survives ACROSS turns
 * instead of being torn down and rebuilt every single push-to-talk press.
 * Rebuilding per turn used to cost, on every command: a fresh single-use
 * ws-auth-token mint (POST /v1/ws/auth), the WS upgrade handshake, and a new
 * AudioContext. Those costs are now paid once per warm session, not once per
 * turn — the socket is kept alive between turns with a cheap client ping
 * (~20s cadence, comfortably under the server's 90s idle-close in
 * ui/api/routes/voice_stream.py) and reused on the next `startRecording()`
 * call. Guardrails, all enforced client-side here:
 *   - The MIC ITSELF is never kept warm by this change — capture (getUserMedia
 *     track, AudioContext, processor/source) is still acquired fresh and fully
 *     released at the end of every turn, exactly as before. Only the WS
 *     connection (no audio without an active turn) and, optionally, the
 *     already-consented hands-free-wake mic stream (existingStream, unrelated
 *     to this change) persist.
 *   - Background tab: `document.visibilitychange` closes an IDLE warm
 *     connection the instant the tab is hidden (never mid-turn) — a
 *     backgrounded tab holds nothing open.
 *   - Battery saver: browsers expose no universal "low power mode" flag, so
 *     `navigator.getBattery()` (where available) — not charging AND at/below
 *     BATTERY_SAVER_LEVEL_THRESHOLD — is used as an honest proxy to suspend
 *     warm-keeping the same way a hidden tab does.
 *   - Bounded idle lifetime: even on a visible, plugged-in tab, an idle warm
 *     connection with no new turn for IDLE_KEEPALIVE_CLOSE_MS self-closes
 *     client-side (belt-and-braces alongside the server's own idle-timeout).
 *   - `prewarmConnection()` lets a caller open the connection speculatively on
 *     an intent signal (mic-button hover/focus) WITHOUT starting any audio
 *     capture — it only does the network handshake, never touches the mic.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { describeError, describeMicError } from '../utils/describeError';
import { decodeTtsFrame, isTtsFrame, playTtsPcm } from '../utils/ttsPlayback';
import { getWebSocketAuthToken } from '../lib/ws_auth';

const SAMPLE_RATE = 16000;
const BUFFER_SIZE = 4096;
// Bail if the hub doesn't respond within this window after ptt_stop. A slow
// agent turn (browser_navigate, web_search) can take 25-30s before the hub
// emits command_result, so this must be long enough not to kill a legitimately
// slow turn; a genuinely dead hub still surfaces once it elapses.
const RESPONSE_TIMEOUT_MS = 35000;
// After command_result the hub synthesizes TTS and streams it back as one or
// more binary PCM frames, THEN closes. Synthesis can take several seconds, so
// keep the socket open until the audio is RECEIVED rather than a blind short
// timer that closes before the frame arrives:
//   - TTS_WAIT_CAP_MS: hard cap waiting for the first frame, so an absent or
//     failed TTS never hangs the session.
//   - TTS_IDLE_TEARDOWN_MS: once a frame lands, tear down after a brief gap with
//     no further frames (handles multi-frame TTS without holding open).
// playTtsPcm copies each frame into its own module-level AudioContext, so once a
// frame is received the socket may close and playback still completes.
const TTS_WAIT_CAP_MS = 10000;
const TTS_IDLE_TEARDOWN_MS = 1000;

// --- CONFIRM-6 warm-keeping constants ------------------------------------ //
// Client keepalive cadence while the connection is open but idle (no turn in
// flight). Comfortably under the server's VOICE_STREAM_IDLE_TIMEOUT_S (90s,
// ui/api/routes/voice_stream.py) so a live tab's socket never trips it.
const PING_INTERVAL_MS = 20000;
// Bound how long an idle (no new turn) warm connection is held open
// CLIENT-side, even on a visible, plugged-in tab — belt-and-braces alongside
// the server's own idle-close so a forgotten-open tab doesn't hold a socket
// (and the server-side ring buffer / wake-engine fork it costs) forever.
const IDLE_KEEPALIVE_CLOSE_MS = 3 * 60 * 1000;
// Battery-saver proxy threshold. No browser exposes a direct "low power mode"
// flag to the page; "not charging and at/below this level" is the same
// heuristic shape commonly used for this purpose.
const BATTERY_SAVER_LEVEL_THRESHOLD = 0.2;

function resampleLinear(inputData, fromRate, toRate) {
  if (fromRate === toRate) return inputData;
  const ratio = fromRate / toRate;
  const outputLength = Math.round(inputData.length / ratio);
  const output = new Float32Array(outputLength);
  for (let i = 0; i < outputLength; i += 1) {
    const srcIdx = i * ratio;
    const lo = Math.floor(srcIdx);
    const hi = Math.min(lo + 1, inputData.length - 1);
    const frac = srcIdx - lo;
    output[i] = inputData[lo] * (1 - frac) + inputData[hi] * frac;
  }
  return output;
}

function isBatterySaverLike(battery) {
  return Boolean(
    battery
    && battery.charging === false
    && typeof battery.level === 'number'
    && battery.level <= BATTERY_SAVER_LEVEL_THRESHOLD,
  );
}

// Maps a server-chosen WS close code to an honest, actionable message instead
// of the mic silently stopping (#2606 — frontend half of commit 8077f878,
// which taught every cloud WS route to accept-then-close via
// ui.core.security.reject_websocket so the client's onclose actually receives
// the app's intended code/reason instead of an opaque abnormal-closure 1006).
// Codes come from ui/api/routes/voice_stream.py's pre-accept rejects
// (4401/4429/1013/1008) and backend/cloud_app.py's graceful-shutdown
// broadcast (1001, reason "server_shutdown"), the same surfaced-error shape
// useAgentBrowserStream.js already uses for its own WS route.
function _describeWsCloseCode(code, reason) {
  switch (code) {
    case 4401:
      // ws_voice_stream sends this for both an outright-missing/invalid
      // session and a token that expired mid-warm-keep (a reconnect attempt
      // racing a refresh) — same user-facing fix either way.
      return 'Your session expired. Please sign in again.';
    case 4429:
      return 'Too many active voice connections. Close another tab or device and try again.';
    case 1013:
      return 'Voice is temporarily unavailable. Please try again in a moment.';
    case 1001:
      return "Viola's server restarted. Please try again.";
    case 1008:
      return 'Voice connection was blocked. Please refresh and try again.';
    default:
      return reason ? `Voice connection lost: ${reason}` : 'Voice connection lost. Please try again.';
  }
}

export function useVoiceWs(onCommandResult, options = {}) {
  const { room, executeCommand = true, existingStream } = options;

  const [isRecording, setIsRecording] = useState(false);
  const [isProcessing, setIsProcessing] = useState(false);
  const [transcript, setTranscript] = useState('');
  const [error, setError] = useState(null);
  // #385: `isBusy` is the OBSERVABLE mirror of `sessionActiveRef.current` — true
  // for the ENTIRE turn/guard window (press -> STT -> agent -> command_result ->
  // TTS playback -> teardown), NOT just the isRecording/isProcessing sub-phases.
  // `isProcessing` flips false the instant command_result lands, but the
  // sessionActiveRef guard in startRecording() keeps rejecting new presses until
  // teardown finishes (up to TTS_WAIT_CAP_MS later). Without a signal that spans
  // that whole window a rapid second press was SILENTLY dropped: isRecording and
  // isProcessing both read false, so the button/hotkey/harness thought the
  // session was idle and let the press through into a no-op. Consumers gate the
  // PTT surface on isBusy so a press during teardown yields a visible busy state
  // instead of vanishing. sessionActiveRef (a ref) stays the synchronous guard
  // truth; isBusy is the render-visible copy set in lockstep with it.
  const [isBusy, setIsBusy] = useState(false);

  const wsRef = useRef(null);
  const audioContextRef = useRef(null);
  const processorRef = useRef(null);
  const sourceRef = useRef(null);
  const streamRef = useRef(null);
  const usingSharedStreamRef = useRef(false);
  const existingStreamRef = useRef(existingStream);
  const onCommandResultRef = useRef(onCommandResult);
  const executeCommandRef = useRef(executeCommand);
  const roomRef = useRef(room);
  const responseTimerRef = useRef(null);
  const sessionActiveRef = useRef(false);
  // Pending-teardown timer used AFTER command_result while we wait for the TTS
  // PCM frame(s). awaitingTtsRef marks that we're in that wait window so a TTS
  // frame re-arms a short idle teardown instead of the long response timeout.
  const ttsWaitTimerRef = useRef(null);
  const awaitingTtsRef = useRef(false);
  // CONFIRM-6 warm-keeping state.
  const pingTimerRef = useRef(null);
  const idleCloseTimerRef = useRef(null);
  const batteryRef = useRef(null);
  const connectingPromiseRef = useRef(null);

  useEffect(() => { onCommandResultRef.current = onCommandResult; }, [onCommandResult]);
  useEffect(() => { executeCommandRef.current = executeCommand; }, [executeCommand]);
  useEffect(() => { roomRef.current = room; }, [room]);
  useEffect(() => { existingStreamRef.current = existingStream; }, [existingStream]);

  const _clearResponseTimer = useCallback(() => {
    if (responseTimerRef.current) {
      clearTimeout(responseTimerRef.current);
      responseTimerRef.current = null;
    }
  }, []);

  const _clearTtsWaitTimer = useCallback(() => {
    if (ttsWaitTimerRef.current) {
      clearTimeout(ttsWaitTimerRef.current);
      ttsWaitTimerRef.current = null;
    }
  }, []);

  const _clearPingTimer = useCallback(() => {
    if (pingTimerRef.current) {
      clearInterval(pingTimerRef.current);
      pingTimerRef.current = null;
    }
  }, []);

  const _clearIdleCloseTimer = useCallback(() => {
    if (idleCloseTimerRef.current) {
      clearTimeout(idleCloseTimerRef.current);
      idleCloseTimerRef.current = null;
    }
  }, []);

  // True when it is worth keeping an idle connection warm: tab visible AND no
  // battery-saver-like signal. False suspends warm-keeping exactly like the
  // CONFIRM-6 guardrail requires (hidden tab / battery saver -> don't hold
  // anything open).
  const _shouldWarmKeep = useCallback(() => {
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return false;
    if (isBatterySaverLike(batteryRef.current)) return false;
    return true;
  }, []);

  const _armPingTimer = useCallback(() => {
    _clearPingTimer();
    pingTimerRef.current = setInterval(() => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(JSON.stringify({ type: 'ping' })); } catch { /* noop */ }
      }
    }, PING_INTERVAL_MS);
  }, [_clearPingTimer]);

  const _armIdleCloseTimer = useCallback(() => {
    _clearIdleCloseTimer();
    idleCloseTimerRef.current = setTimeout(() => {
      if (sessionActiveRef.current) return; // a turn started in the meantime
      _clearPingTimer();
      const ws = wsRef.current;
      wsRef.current = null;
      if (ws) {
        try { ws.close(); } catch { /* noop */ }
      }
    }, IDLE_KEEPALIVE_CLOSE_MS);
  }, [_clearIdleCloseTimer, _clearPingTimer]);

  // Full teardown: audio graph AND the WebSocket. Used for a mid-turn drop,
  // explicit cancel, unmount, and the hidden-tab/battery-saver/idle-timeout
  // forced close — every case where warm-keeping is NOT appropriate.
  const _teardown = useCallback(() => {
    _clearResponseTimer();
    _clearTtsWaitTimer();
    _clearPingTimer();
    _clearIdleCloseTimer();
    awaitingTtsRef.current = false;
    sessionActiveRef.current = false;
    setIsBusy(false);

    if (processorRef.current) {
      try { processorRef.current.disconnect(); } catch { /* noop */ }
      processorRef.current.onaudioprocess = null;
      processorRef.current = null;
    }
    if (sourceRef.current) {
      try { sourceRef.current.disconnect(); } catch { /* noop */ }
      sourceRef.current = null;
    }
    if (audioContextRef.current) {
      audioContextRef.current.close().catch(() => {});
      audioContextRef.current = null;
    }
    if (streamRef.current) {
      if (!usingSharedStreamRef.current) {
        streamRef.current.getTracks().forEach((t) => t.stop());
      }
      streamRef.current = null;
      usingSharedStreamRef.current = false;
    }
    if (wsRef.current) {
      try { wsRef.current.close(); } catch { /* noop */ }
      wsRef.current = null;
    }
    if (navigator.audioSession) {
      navigator.audioSession.type = 'playback';
    }
    setIsRecording(false);
  }, [_clearResponseTimer, _clearTtsWaitTimer, _clearPingTimer, _clearIdleCloseTimer]);

  // End-of-turn teardown that keeps the WS warm: releases the mic/AudioContext
  // (privacy: no capture between turns) but leaves the authenticated
  // connection open, armed with the idle ping + bounded auto-close.
  const _teardownAudioOnly = useCallback(() => {
    _clearResponseTimer();
    _clearTtsWaitTimer();
    awaitingTtsRef.current = false;
    sessionActiveRef.current = false;
    setIsBusy(false);

    if (processorRef.current) {
      try { processorRef.current.disconnect(); } catch { /* noop */ }
      processorRef.current.onaudioprocess = null;
      processorRef.current = null;
    }
    if (sourceRef.current) {
      try { sourceRef.current.disconnect(); } catch { /* noop */ }
      sourceRef.current = null;
    }
    if (audioContextRef.current) {
      audioContextRef.current.close().catch(() => {});
      audioContextRef.current = null;
    }
    if (streamRef.current) {
      if (!usingSharedStreamRef.current) {
        streamRef.current.getTracks().forEach((t) => t.stop());
      }
      streamRef.current = null;
      usingSharedStreamRef.current = false;
    }
    if (navigator.audioSession) {
      navigator.audioSession.type = 'playback';
    }
    setIsRecording(false);

    _armPingTimer();
    _armIdleCloseTimer();
  }, [_clearResponseTimer, _clearTtsWaitTimer, _armPingTimer, _armIdleCloseTimer]);

  // Chooses full teardown vs warm-keep teardown for the end of the CURRENT
  // turn. A no-op if the turn already ended some other way (e.g. a mid-turn
  // ws.onclose already ran the full teardown).
  const _endOfTurn = useCallback(() => {
    if (!sessionActiveRef.current) return;
    if (_shouldWarmKeep()) {
      _teardownAudioOnly();
    } else {
      _teardown();
    }
  }, [_shouldWarmKeep, _teardownAudioOnly, _teardown]);

  const _finishWithResult = useCallback((payload) => {
    _clearResponseTimer();
    setIsProcessing(false);
    if (onCommandResultRef.current) {
      try { onCommandResultRef.current(payload); } catch { /* noop */ }
    }
    // The hub keeps the socket open to stream TTS PCM back AFTER command_result.
    // Wait for that audio before tearing down, instead of a blind short timer
    // that races (and loses to) a slow synthesis. No TTS is expected when
    // execution is suppressed (onboarding mic check) or the turn errored, so
    // tear those down promptly.
    _clearTtsWaitTimer();
    const expectTts = executeCommandRef.current && payload && payload.ok;
    if (!expectTts) {
      awaitingTtsRef.current = false;
      ttsWaitTimerRef.current = setTimeout(_endOfTurn, TTS_IDLE_TEARDOWN_MS);
      return;
    }
    // Backstop: if no TTS frame ever arrives, don't hang the session open.
    awaitingTtsRef.current = true;
    ttsWaitTimerRef.current = setTimeout(() => {
      awaitingTtsRef.current = false;
      _endOfTurn();
    }, TTS_WAIT_CAP_MS);
  }, [_clearResponseTimer, _clearTtsWaitTimer, _endOfTurn]);

  // Shared response-dropped-turn backstop: bail if the hub never resolves
  // this turn within RESPONSE_TIMEOUT_MS. Used both by an explicit
  // stopRecording() (hold-and-release) AND by _handleCaptureEnded below (a
  // tap-mode turn the SERVER auto-ended via silence/timeout endpointing) so
  // every path that leaves recording arms the identical backstop.
  const _armResponseTimeout = useCallback(() => {
    _clearResponseTimer();
    responseTimerRef.current = setTimeout(() => {
      if (sessionActiveRef.current) {
        setError('No response from hub');
        _finishWithResult({ ok: false, error: 'response_timeout' });
      }
    }, RESPONSE_TIMEOUT_MS);
  }, [_clearResponseTimer, _finishWithResult]);

  // #2769: a TAP-mode turn (SmartDisplay's handlePTTEnd skips stopRecording()
  // for a quick tap so the mic keeps listening after release) is ended by the
  // SERVER's own silence/timeout endpointing (voice_stream.py
  // feed_command_pcm), not by any client action. Before this handler, the
  // client had NO signal for that transition at all: isRecording stayed true
  // for the whole agent-thinking window (mic UI stuck "recording") AND no
  // RESPONSE_TIMEOUT_MS backstop was ever armed (that only happened inside
  // stopRecording()), so a dropped/never-answered turn left the mic hot
  // indefinitely with no client-side recovery. The server now sends a
  // `capture_ended` message the instant it ends a tap-mode capture (see
  // voice_stream.py's feed_command_pcm auto-end branch); this mirrors the
  // tail half of stopRecording() below: stop pushing mic frames, flip to
  // processing, and arm the same backstop. It deliberately does NOT send
  // ptt_stop back — the server already ended capture on its own, and a
  // sessionActiveRef guard makes this a no-op if the turn already ended some
  // other way (e.g. an explicit stopRecording() raced it).
  const _handleCaptureEnded = useCallback(() => {
    if (!sessionActiveRef.current) return;

    if (processorRef.current) {
      try { processorRef.current.disconnect(); } catch { /* noop */ }
      processorRef.current.onaudioprocess = null;
      processorRef.current = null;
    }
    if (sourceRef.current) {
      try { sourceRef.current.disconnect(); } catch { /* noop */ }
      sourceRef.current = null;
    }

    setIsRecording(false);
    setIsProcessing(true);
    _armResponseTimeout();
  }, [_armResponseTimeout]);

  // Opens (or reuses an in-flight) /ws/voice-stream connection and wires its
  // message/error/close handlers. Resolves the open socket, or null on
  // failure. Does NOT touch the mic — callers that also need audio capture
  // wire it up separately via _startAudioCapture once this resolves.
  const _connectSocket = useCallback(() => {
    if (connectingPromiseRef.current) return connectingPromiseRef.current;

    const promise = new Promise((resolve) => {
      let settled = false;
      const settle = (value) => {
        if (settled) return;
        settled = true;
        resolve(value);
      };

      (async () => {
        const base = (window.__VIOLA_BASE_URL__ || window.location.origin).replace(/^http/, 'ws');
        const params = new URLSearchParams();
        if (roomRef.current) params.set('room', roomRef.current);
        const wsAuthToken = await getWebSocketAuthToken();
        if (wsAuthToken) params.set('token', wsAuthToken);
        const currentParams = new URLSearchParams(window.location.search);
        const spokeToken = currentParams.get('spoke_token');
        if (spokeToken) params.set('spoke_token', spokeToken);
        const qs = params.toString();
        const ws = new WebSocket(`${base}/ws/voice-stream${qs ? `?${qs}` : ''}`);
        ws.binaryType = 'arraybuffer';
        wsRef.current = ws;

        ws.onopen = () => { settle(ws); };

        ws.onmessage = (event) => {
          const data = event.data;
          if (typeof data === 'string') {
            let msg;
            try { msg = JSON.parse(data); } catch { return; }
            if (msg.type === 'pong') return;
            if (msg.type === 'capture_ended') {
              _handleCaptureEnded();
              return;
            }
            if (msg.type === 'command_result') {
              const text = msg.transcript || '';
              const responseText = msg.response || '';
              if (text) setTranscript(text);
              // A spoken cap denial has nothing to tap, so carry the managed-AI
              // cap state through to the response area, which renders the
              // upgrade route beside the reply (C-077). The frame only carries
              // it on a real denial.
              const capState = msg.cap_state;
              _finishWithResult({
                ok: true,
                data: {
                  text,
                  transcript: text,
                  message: responseText,
                  response: responseText,
                  ...(capState ? { cap_state: capState } : {}),
                },
              });
            } else if (msg.type === 'error') {
              // The hub sends this frame in more than one shape: a bare
              // `message` string from the voice-session route, and a nested
              // `error: {code, message}` object from the event hub. Reading
              // only `msg.message` turned the second shape into the useless
              // "Voice stream error" for every one of them.
              setError(describeError(msg, 'The voice connection hit an error. Try again.'));
              _finishWithResult({ ok: false, error: msg.message || 'voice_stream_error' });
            }
            return;
          }

          if (data instanceof ArrayBuffer && isTtsFrame(data)) {
            if (!executeCommandRef.current) return;
            const { pcmBuffer, sampleRate } = decodeTtsFrame(data);
            playTtsPcm(pcmBuffer, sampleRate);
            // The frame's bytes are now copied into playTtsPcm's own AudioContext,
            // so the socket is free to close/warm-keep. If we were holding the
            // turn open for TTS, re-arm a short end-of-turn timer: any further
            // frames refresh it, and we end the turn once audio stops arriving.
            if (awaitingTtsRef.current) {
              _clearTtsWaitTimer();
              ttsWaitTimerRef.current = setTimeout(() => {
                awaitingTtsRef.current = false;
                _endOfTurn();
              }, TTS_IDLE_TEARDOWN_MS);
            }
          }
        };

        ws.onerror = () => {
          // A prewarm/reuse connect attempt that never turns into a real turn
          // should fail silently — the next real startRecording() falls back
          // to a fresh connect. Only surface an error when a turn is actually
          // in flight on this socket.
          if (sessionActiveRef.current) setError('Voice stream connection failed');
        };

        ws.onclose = (event) => {
          if (sessionActiveRef.current) {
            // Dropped mid-turn without delivering a command_result. Surface
            // WHY via the server's close code instead of the mic just going
            // quiet with no explanation (#2606) — expired auth, a
            // token-refresh race, and the 4429 too-many-connections cap all
            // arrive as a real code/reason now (see _describeWsCloseCode).
            setError(_describeWsCloseCode(event && event.code, event && event.reason));
            _clearResponseTimer();
            setIsProcessing(false);
            _teardown();
          } else {
            // Either the server's idle-timeout closed a warm-but-idle
            // connection, or the connect attempt itself failed before
            // opening — either way, stop pinging and drop the stale ref so
            // the next startRecording()/prewarmConnection() reconnects.
            _clearPingTimer();
            _clearIdleCloseTimer();
            if (wsRef.current === ws) wsRef.current = null;
          }
          settle(null);
        };
      })();
    }).finally(() => {
      connectingPromiseRef.current = null;
    });

    connectingPromiseRef.current = promise;
    return promise;
  }, [_finishWithResult, _teardown, _clearResponseTimer, _clearTtsWaitTimer, _clearPingTimer, _clearIdleCloseTimer, _endOfTurn, _handleCaptureEnded]);

  // Wires the mic stream into the (already open) socket and starts sending
  // PCM. Shared by both the fresh-connect path and the warm-reuse path so
  // the two behave identically once a socket is available.
  const _startAudioCapture = useCallback((ws, audioContext, mediaStream) => {
    const source = audioContext.createMediaStreamSource(mediaStream);
    sourceRef.current = source;
    const processor = audioContext.createScriptProcessor(BUFFER_SIZE, 1, 1);
    processorRef.current = processor;

    processor.onaudioprocess = (e) => {
      if (ws.readyState !== WebSocket.OPEN) return;
      const inputData = e.inputBuffer.getChannelData(0);
      const sampleData = resampleLinear(inputData, audioContext.sampleRate, SAMPLE_RATE);
      const int16 = new Int16Array(sampleData.length);
      for (let i = 0; i < sampleData.length; i += 1) {
        const s = Math.max(-1, Math.min(1, sampleData[i]));
        int16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
      }
      ws.send(int16.buffer);
    };

    source.connect(processor);
    processor.connect(audioContext.destination);
    // Tell the server this turn is a PTT (skip wake inference).
    ws.send(JSON.stringify({ type: 'ptt_start' }));
    setIsRecording(true);
  }, []);

  const startRecording = useCallback(async () => {
    // #385: a turn is already in flight (recording, processing, or tearing down
    // after command_result while TTS plays). Reject the press to keep the guard's
    // real job — never overlap two capture sessions on one socket — but do NOT
    // do it silently: `isBusy` is already true and drives a visible busy state on
    // the PTT surface, so a rapid second press is acknowledged, not swallowed.
    if (sessionActiveRef.current) return;
    setError(null);
    setTranscript('');
    sessionActiveRef.current = true;
    setIsBusy(true);
    // A real turn is starting: stop treating the connection as idle-warm.
    _clearPingTimer();
    _clearIdleCloseTimer();

    try {
      if (navigator.audioSession) {
        navigator.audioSession.type = 'play-and-record';
      }

      let mediaStream;
      if (existingStreamRef.current && existingStreamRef.current.active
          && existingStreamRef.current.getAudioTracks().some((t) => t.readyState === 'live')) {
        mediaStream = existingStreamRef.current;
        usingSharedStreamRef.current = true;
      } else {
        usingSharedStreamRef.current = false;
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
          setError('Microphone access is not available.');
          sessionActiveRef.current = false;
          setIsBusy(false);
          return;
        }
        mediaStream = await navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,
            sampleRate: SAMPLE_RATE,
            echoCancellation: false,
            noiseSuppression: true,
            autoGainControl: true,
          },
        });
      }
      streamRef.current = mediaStream;

      const AudioCtor = window.AudioContext || window.webkitAudioContext;
      const audioContext = new AudioCtor({ sampleRate: SAMPLE_RATE });
      audioContextRef.current = audioContext;

      // Fast resume: an already-open, already-authenticated connection from a
      // prior turn (or a hover/focus prewarm) skips the auth-token mint + WS
      // handshake entirely.
      const warmSocket = wsRef.current && wsRef.current.readyState === WebSocket.OPEN
        ? wsRef.current
        : null;
      const ws = warmSocket || (await _connectSocket());
      if (!ws) {
        // A failed fresh connect already ran its own onerror (sets the error
        // message) and onclose (full teardown, incl. the mic/AudioContext we
        // just acquired above) before resolving null here — nothing left to
        // do.
        return;
      }
      _startAudioCapture(ws, audioContext, mediaStream);
    } catch (err) {
      // Was showing the browser's raw DOMException text ("Requested device not
      // found"). The HTTP voice path already had human wording for each
      // getUserMedia failure name; describeMicError is that same mapping,
      // shared, so both paths explain a denied mic identically.
      setError(describeMicError(err));
      sessionActiveRef.current = false;
      setIsBusy(false);
      _teardown();
    }
  }, [_connectSocket, _startAudioCapture, _teardown, _clearPingTimer, _clearIdleCloseTimer]);

  const stopRecording = useCallback(async () => {
    if (!sessionActiveRef.current) return null;
    if (!isRecording && !isProcessing) return null;

    // Stop pushing mic frames the moment the user releases the button so
    // we don't keep sending audio after the turn has ended.
    if (processorRef.current) {
      try { processorRef.current.disconnect(); } catch { /* noop */ }
      processorRef.current.onaudioprocess = null;
      processorRef.current = null;
    }
    if (sourceRef.current) {
      try { sourceRef.current.disconnect(); } catch { /* noop */ }
      sourceRef.current = null;
    }

    setIsRecording(false);
    setIsProcessing(true);

    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: 'ptt_stop' })); } catch { /* noop */ }
    }

    _armResponseTimeout();

    return null;
  }, [_armResponseTimeout, isProcessing, isRecording]);

  const cancelRecording = useCallback(() => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: 'ptt_stop' })); } catch { /* noop */ }
    }
    // An explicit cancel always fully tears down (including the WS) rather
    // than warm-keeping — a deliberate abort, not a normal turn boundary.
    _teardown();
  }, [_teardown]);

  // Speculative warm-up on an intent signal (mic-button hover/focus, mic
  // permission already granted from a prior turn) — opens the connection
  // AHEAD of an actual press so the fast-resume path above has something to
  // reuse. Never touches the mic and never runs when a real turn is already
  // starting/in-flight or when the tab is hidden / battery-saver-like.
  const prewarmConnection = useCallback(() => {
    if (sessionActiveRef.current) return;
    if (wsRef.current
        && (wsRef.current.readyState === WebSocket.OPEN || wsRef.current.readyState === WebSocket.CONNECTING)) {
      return;
    }
    if (!_shouldWarmKeep()) return;
    _connectSocket().then((ws) => {
      if (ws && !sessionActiveRef.current) {
        _armPingTimer();
        _armIdleCloseTimer();
      }
    }).catch(() => {});
  }, [_connectSocket, _shouldWarmKeep, _armPingTimer, _armIdleCloseTimer]);

  // Hidden-tab / battery-saver suspend: close an IDLE warm connection the
  // instant either condition is true. Never interrupts a turn in progress.
  useEffect(() => {
    const closeIfIdleAndUnfavorable = () => {
      if (sessionActiveRef.current) return;
      if (_shouldWarmKeep()) return;
      _clearPingTimer();
      _clearIdleCloseTimer();
      const ws = wsRef.current;
      wsRef.current = null;
      if (ws) {
        try { ws.close(); } catch { /* noop */ }
      }
    };

    const handleVisibilityChange = () => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
        closeIfIdleAndUnfavorable();
      }
    };
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', handleVisibilityChange);
    }

    let batteryHandle = null;
    let onBatteryChange = null;
    if (typeof navigator !== 'undefined' && typeof navigator.getBattery === 'function') {
      navigator.getBattery().then((battery) => {
        batteryHandle = battery;
        batteryRef.current = battery;
        onBatteryChange = () => {
          batteryRef.current = battery;
          closeIfIdleAndUnfavorable();
        };
        battery.addEventListener('levelchange', onBatteryChange);
        battery.addEventListener('chargingchange', onBatteryChange);
      }).catch(() => { /* Battery Status API unavailable/blocked — no proxy signal */ });
    }

    return () => {
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', handleVisibilityChange);
      }
      if (batteryHandle && onBatteryChange) {
        batteryHandle.removeEventListener('levelchange', onBatteryChange);
        batteryHandle.removeEventListener('chargingchange', onBatteryChange);
      }
    };
  }, [_shouldWarmKeep, _clearPingTimer, _clearIdleCloseTimer]);

  useEffect(() => () => _teardown(), [_teardown]);

  return {
    isRecording,
    isProcessing,
    isBusy,
    transcript,
    error,
    startRecording,
    stopRecording,
    cancelRecording,
    prewarmConnection,
    clearError: () => setError(null),
  };
}

export default useVoiceWs;
