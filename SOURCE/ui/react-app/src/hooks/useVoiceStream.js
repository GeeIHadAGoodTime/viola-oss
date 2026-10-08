import { useState, useRef, useCallback, useEffect } from 'react';
import { decodeTtsFrame, isTtsFrame, playTtsPcm } from '../utils/ttsPlayback';
import { getWebSocketAuthToken } from '../lib/ws_auth';

const BUFFER_SIZE = 4096;
const SAMPLE_RATE = 16000;

/**
 * Resample a Float32 audio buffer from one sample rate to another using
 * linear interpolation.  Used to handle iOS Safari AudioContext which
 * silently ignores requested sampleRate and returns the hardware rate
 * (44100 or 48000 Hz) instead.
 *
 * @param {Float32Array} inputData  - Source samples
 * @param {number}       fromRate   - Actual sample rate of inputData
 * @param {number}       toRate     - Target sample rate (16000)
 * @returns {Float32Array} Resampled buffer at toRate
 */
function resampleLinear(inputData, fromRate, toRate) {
  if (fromRate === toRate) return inputData;
  const ratio = fromRate / toRate;
  const outputLength = Math.round(inputData.length / ratio);
  const output = new Float32Array(outputLength);
  for (let i = 0; i < outputLength; i++) {
    const srcIdx = i * ratio;
    const lo = Math.floor(srcIdx);
    const hi = Math.min(lo + 1, inputData.length - 1);
    const frac = srcIdx - lo;
    output[i] = inputData[lo] * (1 - frac) + inputData[hi] * frac;
  }
  return output;
}

/**
 * Hook for continuous voice audio streaming to hub for wake word detection.
 * Phase 2: Enable continuous wake word detection by connecting the hub's
 * /ws/voice-stream endpoint to ViolaWake. Client-side audio capture and
 * streaming is already wired here.
 *
 * @param {Object} options
 * @param {string} options.room - Room name for the WebSocket connection
 * @param {Function} [options.onWakeDetected] - Called when hub detects wake word
 * @param {Function} [options.onTranscription] - Called when hub sends transcription
 * @param {Function} [options.onStreamAcquired] - Called when this hook acquires a new mic stream
 * @returns {{ startStreaming: Function, stopStreaming: Function, isStreaming: boolean, isSupported: boolean }}
 */
export function useVoiceStream({
  room,
  enabled = true,
  onWakeDetected,
  onTranscription,
  existingStream,
  onStreamAcquired,
} = {}) {
  const [isStreaming, setIsStreaming] = useState(false);
  const sessionRef = useRef(null);
  const mountedRef = useRef(true);
  const enabledRef = useRef(enabled);
  const callbacksRef = useRef({});
  const existingStreamRef = useRef(existingStream);
  enabledRef.current = enabled;
  callbacksRef.current = { onWakeDetected, onTranscription, onStreamAcquired };
  existingStreamRef.current = existingStream;

  const isSupported = typeof navigator !== 'undefined'
    && !!navigator.mediaDevices?.getUserMedia
    && typeof AudioContext !== 'undefined';

  // Every asynchronous continuation belongs to one capture owner. Retire it
  // before releasing resources: even synchronous close callbacks are stale.
  const cleanup = useCallback((session = sessionRef.current) => {
    if (!session) return;
    const isCurrent = sessionRef.current === session;
    if (isCurrent) sessionRef.current = null;
    if (session.processor) {
      session.processor.onaudioprocess = null;
      session.processor.disconnect();
      session.processor = null;
    }
    if (session.source) {
      session.source.disconnect();
      session.source = null;
    }
    if (session.audioContext) {
      session.audioContext.close().catch(() => {});
      session.audioContext = null;
    }
    // Ownership is decided when acquired. Echoing a hook-owned stream back
    // through existingStream does not transfer its cleanup to the caller.
    if (session.stream && session.ownsStream) {
      session.stream.getTracks().forEach((track) => track.stop());
    }
    session.stream = null;
    if (session.ws) {
      const ws = session.ws;
      session.ws = null;
      ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null;
      try {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'stop_listening' }));
        }
        ws.close();
      } catch { /* Already closed. */ }
    }
    if (isCurrent) {
      if (navigator.audioSession) navigator.audioSession.type = 'playback';
      if (mountedRef.current) setIsStreaming(false);
    }
  }, []);

  const startStreaming = useCallback(async () => {
    if (!isSupported || !mountedRef.current || !enabledRef.current || sessionRef.current) return;
    const session = { stream: null, ownsStream: false, ws: null, audioContext: null, source: null, processor: null };
    sessionRef.current = session;
    const isCurrent = () => mountedRef.current && enabledRef.current && sessionRef.current === session;

    try {
      // Safari needs play-and-record before requesting microphone permission.
      if (navigator.audioSession) navigator.audioSession.type = 'play-and-record';
      let mediaStream = existingStreamRef.current;
      if (!mediaStream?.active) {
        session.ownsStream = true;
        mediaStream = await navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,
            sampleRate: SAMPLE_RATE,
            // Keep iOS VPIO ducking off; hub ViolaAEC handles echo.
            echoCancellation: false,
            noiseSuppression: true,
            autoGainControl: true,
          },
        });
      }
      if (!isCurrent()) {
        if (session.ownsStream) mediaStream.getTracks().forEach((track) => track.stop());
        return;
      }
      session.stream = mediaStream;
      if (session.ownsStream) callbacksRef.current.onStreamAcquired?.(mediaStream);
      if (!isCurrent()) return;

      const audioContext = new AudioContext({ sampleRate: SAMPLE_RATE });
      session.audioContext = audioContext;
      if (audioContext.sampleRate !== SAMPLE_RATE) {
        console.warn(
          '[useVoiceStream] AudioContext sampleRate mismatch: requested=%d actual=%d — resampling enabled',
          SAMPLE_RATE, audioContext.sampleRate,
        );
      }
      const source = audioContext.createMediaStreamSource(mediaStream);
      session.source = source;
      const processor = audioContext.createScriptProcessor(BUFFER_SIZE, 1, 1);
      session.processor = processor;

      const base = (window.__VIOLA_BASE_URL__ || window.location.origin).replace(/^http/, 'ws');
      const params = new URLSearchParams();
      if (room) params.set('room', room);
      const wsAuthToken = await getWebSocketAuthToken();
      if (!isCurrent()) return;
      if (wsAuthToken) params.set('token', wsAuthToken);
      const spokeToken = new URLSearchParams(window.location.search).get('spoke_token');
      if (spokeToken) params.set('spoke_token', spokeToken);
      const qs = params.toString();
      const ws = new WebSocket(`${base}/ws/voice-stream${qs ? `?${qs}` : ''}`);
      session.ws = ws;
      ws.binaryType = 'arraybuffer';
      const ownsSocket = () => isCurrent() && session.ws === ws;

      ws.onopen = () => {
        if (!ownsSocket() || ws.readyState !== WebSocket.OPEN) return;
        // Actual wire PCM is 16 kHz, including resampled iOS hardware audio.
        ws.send(JSON.stringify({ type: 'start_listening', sample_rate: SAMPLE_RATE }));
        setIsStreaming(true);
        processor.onaudioprocess = (e) => {
          if (!ownsSocket() || ws.readyState !== WebSocket.OPEN) return;
          const inputData = e.inputBuffer.getChannelData(0);
          const sampleData = resampleLinear(inputData, audioContext.sampleRate, SAMPLE_RATE);
          const int16 = new Int16Array(sampleData.length);
          for (let i = 0; i < sampleData.length; i++) {
            const value = Math.max(-1, Math.min(1, sampleData[i]));
            int16[i] = value < 0 ? value * 0x8000 : value * 0x7FFF;
          }
          ws.send(int16.buffer);
        };
        source.connect(processor);
        processor.connect(audioContext.destination);
      };
      ws.onmessage = (event) => {
        if (!ownsSocket()) return;
        const data = event.data;
        if (data instanceof ArrayBuffer) {
          if (isTtsFrame(data)) {
            const { pcmBuffer, sampleRate } = decodeTtsFrame(data);
            playTtsPcm(pcmBuffer, sampleRate);
          }
          return;
        }
        try {
          const msg = JSON.parse(data);
          if (msg.type === 'wake_detected') callbacksRef.current.onWakeDetected?.(msg.payload || msg);
          if (msg.type === 'transcription' || msg.type === 'command_result') {
            callbacksRef.current.onTranscription?.(msg.payload || msg);
          }
        } catch { /* Non-JSON text message. */ }
      };
      ws.onerror = ws.onclose = () => {
        if (ownsSocket()) cleanup(session);
      };
    } catch (err) {
      if (!isCurrent()) return;
      console.error('[useVoiceStream] Failed to start streaming:', err.name, err.message);
      cleanup(session);
    }
  }, [isSupported, room, cleanup]);

  const stopStreaming = useCallback(() => cleanup(), [cleanup]);
  useEffect(() => {
    if (!enabled) cleanup();
  }, [enabled, cleanup]);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      cleanup();
    };
  }, [cleanup]);

  return { startStreaming, stopStreaming, isStreaming, isSupported };
}

export default useVoiceStream;
