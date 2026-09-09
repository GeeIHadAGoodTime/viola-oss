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
  onWakeDetected,
  onTranscription,
  existingStream,
  onStreamAcquired,
} = {}) {
  const [isStreaming, setIsStreaming] = useState(false);
  const wsRef = useRef(null);
  const audioContextRef = useRef(null);
  const processorRef = useRef(null);
  const streamRef = useRef(null);
  const sourceRef = useRef(null);
  const callbacksRef = useRef({ onWakeDetected, onTranscription });
  const existingStreamRef = useRef(existingStream);

  // Keep callbacks ref up to date without retriggering effects
  useEffect(() => {
    callbacksRef.current = { onWakeDetected, onTranscription };
  }, [onWakeDetected, onTranscription]);

  useEffect(() => {
    existingStreamRef.current = existingStream;
  }, [existingStream]);

  const isSupported = typeof navigator !== 'undefined'
    && !!navigator.mediaDevices
    && !!navigator.mediaDevices.getUserMedia
    && typeof AudioContext !== 'undefined';

  /**
   * Clean up all audio and WebSocket resources.
   * @private
   */
  const cleanup = useCallback(() => {
    // Disconnect ScriptProcessor
    if (processorRef.current) {
      processorRef.current.disconnect();
      processorRef.current.onaudioprocess = null;
      processorRef.current = null;
    }

    // Disconnect source
    if (sourceRef.current) {
      sourceRef.current.disconnect();
      sourceRef.current = null;
    }

    // Close AudioContext
    if (audioContextRef.current) {
      audioContextRef.current.close().catch(() => {});
      audioContextRef.current = null;
    }

    // Stop media stream tracks — but NOT if it's the shared stream (caller owns it)
    if (streamRef.current) {
      if (streamRef.current !== existingStreamRef.current) {
        streamRef.current.getTracks().forEach((track) => track.stop());
      }
      streamRef.current = null;
    }

    // Close WebSocket
    if (wsRef.current) {
      try {
        if (wsRef.current.readyState === WebSocket.OPEN) {
          wsRef.current.send(JSON.stringify({ type: 'stop_listening' }));
        }
        wsRef.current.close();
      } catch {
        // Already closed
      }
      wsRef.current = null;
    }

    // Revert iOS audio session to playback-only.  This deactivates the
    // VoiceProcessingIO unit and stops playback ducking.
    if (navigator.audioSession) {
      navigator.audioSession.type = 'playback';
    }

    setIsStreaming(false);
  }, []);

  /**
   * Start capturing audio and streaming PCM frames to the hub.
   */
  const startStreaming = useCallback(async () => {
    if (!isSupported || isStreaming) return;

    try {
      // Switch iOS audio session to allow simultaneous playback + mic.
      // This MUST happen before getUserMedia — Safari rejects mic access
      // when the session is set to 'playback'.  Reverted in cleanup().
      if (navigator.audioSession) {
        navigator.audioSession.type = 'play-and-record';
      }

      // 1. Get microphone access — reuse pre-acquired stream if available
      let mediaStream;
      if (existingStreamRef.current && existingStreamRef.current.active) {
        mediaStream = existingStreamRef.current;
      } else {
        mediaStream = await navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,
            sampleRate: SAMPLE_RATE,
            // echoCancellation OFF: on iOS, enabling it activates the
            // VoiceProcessingIO (VPIO) hardware unit which aggressively
            // ducks playback whenever the mic picks up ANY sound.  The
            // hub's ViolaAEC handles echo cancellation on the raw PCM.
            echoCancellation: false,
            noiseSuppression: true,
            autoGainControl: true,
          },
        });
      }
      streamRef.current = mediaStream;
      if (!existingStreamRef.current && typeof onStreamAcquired === 'function') {
        onStreamAcquired(mediaStream);
      }

      // 2. Create AudioContext at 16kHz
      const audioContext = new AudioContext({ sampleRate: SAMPLE_RATE });
      audioContextRef.current = audioContext;
      // iOS Safari silently ignores the requested sampleRate and returns the
      // hardware rate (44100 / 48000 Hz).  Log a warning so it's visible in
      // remote console dumps; resampleLinear() handles the conversion below.
      if (audioContext.sampleRate !== SAMPLE_RATE) {
        console.warn(
          '[useVoiceStream] AudioContext sampleRate mismatch: requested=%d actual=%d — resampling enabled',
          SAMPLE_RATE,
          audioContext.sampleRate,
        );
      }

      // 3. Connect source -> processor
      const source = audioContext.createMediaStreamSource(mediaStream);
      sourceRef.current = source;

      // ScriptProcessorNode is deprecated but widely supported.
      // AudioWorklet would be preferred in a future iteration.
      const processor = audioContext.createScriptProcessor(BUFFER_SIZE, 1, 1);
      processorRef.current = processor;

      // 4. Open WebSocket to voice-stream endpoint
      const base = (window.__VIOLA_BASE_URL__ || window.location.origin).replace(/^http/, 'ws');
      const params = new URLSearchParams();
      if (room) params.set('room', room);
      const wsAuthToken = await getWebSocketAuthToken();
      if (wsAuthToken) params.set('token', wsAuthToken);
      const currentParams = new URLSearchParams(window.location.search);
      const spokeToken = currentParams.get('spoke_token');
      if (spokeToken) params.set('spoke_token', spokeToken);
      const qs = params.toString();
      const wsUrl = `${base}/ws/voice-stream${qs ? `?${qs}` : ''}`;

      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.binaryType = 'arraybuffer';

      ws.onopen = () => {
        // Always report SAMPLE_RATE (16000) — the rate of the PCM data
        // actually sent.  resampleLinear() converts any iOS hardware rate
        // (44100/48000) to SAMPLE_RATE before Int16 encoding, so the server
        // always receives 16 kHz PCM regardless of the iOS device.
        ws.send(JSON.stringify({ type: 'start_listening', sample_rate: SAMPLE_RATE }));
        setIsStreaming(true);

        // Wire up audio processing AFTER WebSocket is open
        processor.onaudioprocess = (e) => {
          if (ws.readyState !== WebSocket.OPEN) return;
          const inputData = e.inputBuffer.getChannelData(0);
          // Resample to 16kHz if iOS gave us a different hardware rate.
          // On desktop (Chrome/Firefox) fromRate === SAMPLE_RATE so this is
          // a zero-cost identity pass-through.
          const sampleData = resampleLinear(inputData, audioContext.sampleRate, SAMPLE_RATE);
          // Convert Float32 [-1, 1] to Int16 [-32768, 32767]
          const int16 = new Int16Array(sampleData.length);
          for (let i = 0; i < sampleData.length; i++) {
            const s = Math.max(-1, Math.min(1, sampleData[i]));
            int16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
          }
          ws.send(int16.buffer);
        };

        source.connect(processor);
        processor.connect(audioContext.destination);
      };

      ws.onmessage = (event) => {
        const data = event.data;

        // Binary frame: TTS PCM from the hub's _send_command_result.
        // The wake-on-spoke flow synthesizes TTS server-side and pushes
        // the PCM here; without this branch the audio response is
        // silently dropped (JSON.parse on an ArrayBuffer throws into
        // the swallowing catch below).
        if (data instanceof ArrayBuffer) {
          if (isTtsFrame(data)) {
            const { pcmBuffer, sampleRate } = decodeTtsFrame(data);
            playTtsPcm(pcmBuffer, sampleRate);
          }
          return;
        }

        try {
          const msg = JSON.parse(data);
          if (msg.type === 'wake_detected' && callbacksRef.current.onWakeDetected) {
            callbacksRef.current.onWakeDetected(msg.payload || msg);
          }
          if ((msg.type === 'transcription' || msg.type === 'command_result')
            && callbacksRef.current.onTranscription) {
            callbacksRef.current.onTranscription(msg.payload || msg);
          }
        } catch {
          // Non-JSON text message — ignore
        }
      };

      ws.onerror = () => {
        cleanup();
      };

      ws.onclose = () => {
        cleanup();
      };
    } catch (err) {
      console.error('[useVoiceStream] Failed to start streaming:', err.name, err.message);
      cleanup();
    }
  }, [isSupported, isStreaming, room, cleanup, onStreamAcquired]);

  /**
   * Stop capturing and streaming audio.
   */
  const stopStreaming = useCallback(() => {
    cleanup();
  }, [cleanup]);

  // Clean up on unmount
  useEffect(() => {
    return () => {
      cleanup();
    };
  }, [cleanup]);

  return {
    startStreaming,
    stopStreaming,
    isStreaming,
    isSupported,
  };
}

export default useVoiceStream;
