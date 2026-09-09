import { useState, useRef, useCallback, useEffect } from 'react';
import { describeError, describeMicError, ERROR_CODE_MESSAGES } from '../utils/describeError';
import { authFetch } from './useViolaApi';

function getSupportedMimeType() {
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/ogg;codecs=opus',
    'audio/mp4',
  ];
  if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) {
    return '';
  }
  return candidates.find(function(t) { return MediaRecorder.isTypeSupported(t); }) || '';
}

/**
 * Custom hook for voice/PTT functionality
 * Handles audio recording, transcription, and command execution
 */
export function useVoice(onCommandResult, { existingStream, executeCommand = true } = {}) {
  const [isRecording, setIsRecording] = useState(false);
  const [isProcessing, setIsProcessing] = useState(false);
  const [transcript, setTranscript] = useState('');
  const [error, setError] = useState(null);

  const mediaRecorderRef = useRef(null);
  const audioChunksRef = useRef([]);
  const streamRef = useRef(null);
  const usingSharedStreamRef = useRef(false);
  const existingStreamRef = useRef(existingStream);
  const silenceTimerRef = useRef(null);
  const analyserRef = useRef(null);
  const audioCtxRef = useRef(null);
  const animFrameRef = useRef(null);
  const stopRecordingRef = useRef(null);
  const mimeTypeRef = useRef('');
  const executeCommandRef = useRef(executeCommand);

  // Keep existingStreamRef current without retriggering effects
  useEffect(() => {
    existingStreamRef.current = existingStream;
  }, [existingStream]);

  useEffect(() => {
    executeCommandRef.current = executeCommand;
  }, [executeCommand]);

  const _stopVAD = useCallback(() => {
    if (animFrameRef.current !== null) {
      window.cancelAnimationFrame(animFrameRef.current);
      animFrameRef.current = null;
    }

    silenceTimerRef.current = null;
    analyserRef.current = null;

    const audioCtx = audioCtxRef.current;
    audioCtxRef.current = null;

    if (audioCtx && audioCtx.state !== 'closed') {
      audioCtx.close().catch(() => {});
    }
  }, []);

  const _startVAD = useCallback((stream) => {
    _stopVAD();

    const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
    if (!stream || !AudioContextCtor) {
      return;
    }

    try {
      const audioCtx = new AudioContextCtor();
      const source = audioCtx.createMediaStreamSource(stream);
      const analyser = audioCtx.createAnalyser();
      const bufferLength = 128;
      const dataArray = new Float32Array(bufferLength);
      const silenceThreshold = 0.01;
      const silenceDurationMs = 1500;

      analyser.fftSize = 256;
      source.connect(analyser);

      analyserRef.current = analyser;
      audioCtxRef.current = audioCtx;
      silenceTimerRef.current = null;

      if (audioCtx.state === 'suspended') {
        audioCtx.resume().catch(() => {});
      }

      const tick = () => {
        const mediaRecorder = mediaRecorderRef.current;

        if (!analyserRef.current || !audioCtxRef.current || mediaRecorder?.state !== 'recording') {
          silenceTimerRef.current = null;
          animFrameRef.current = null;
          return;
        }

        analyserRef.current.getFloatTimeDomainData(dataArray);

        let sumSquares = 0;
        for (let i = 0; i < dataArray.length; i += 1) {
          sumSquares += dataArray[i] * dataArray[i];
        }

        const rms = Math.sqrt(sumSquares / dataArray.length);
        const now = window.performance.now();

        if (rms < silenceThreshold) {
          if (silenceTimerRef.current === null) {
            silenceTimerRef.current = now;
          } else if (now - silenceTimerRef.current >= silenceDurationMs) {
            _stopVAD();
            stopRecordingRef.current?.();
            return;
          }
        } else {
          silenceTimerRef.current = null;
        }

        animFrameRef.current = window.requestAnimationFrame(tick);
      };

      animFrameRef.current = window.requestAnimationFrame(tick);
    } catch (err) {
      _stopVAD();

      if (import.meta.env.DEV) {
        console.error('[useVoice] Failed to start VAD:', err);
      }
    }
  }, [_stopVAD]);

  // Start recording
  const startRecording = useCallback(async () => {
    try {
      setError(null);
      setTranscript('');
      audioChunksRef.current = [];

      // Debug logging for permission diagnosis
      if (import.meta.env.DEV) {
        console.log('[PTT_DEBUG] Starting recording...');
        console.log('[PTT_DEBUG] existingStream provided:', !!existingStreamRef.current);
      }

      // Reuse pre-acquired stream if available and has active tracks
      // (iOS AudioSession fix: calling getUserMedia again fails when the session is already held)
      let stream;
      if (existingStreamRef.current && existingStreamRef.current.active
          && existingStreamRef.current.getAudioTracks().some(t => t.readyState === 'live')) {
        stream = existingStreamRef.current;
        usingSharedStreamRef.current = true;
        if (import.meta.env.DEV) {
          console.log('[PTT_DEBUG] Reusing existing mic stream (shared)');
        }
      } else {
        usingSharedStreamRef.current = false;
        if (import.meta.env.DEV) {
          console.log('[PTT_DEBUG] window.location.origin:', window.location.origin);
          console.log('[PTT_DEBUG] navigator.mediaDevices exists:', !!navigator.mediaDevices);
          console.log('[PTT_DEBUG] isSecureContext:', window.isSecureContext);
        }

        // Check if mediaDevices API is available
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
          if (import.meta.env.DEV) {
            console.error('[PTT_DEBUG] mediaDevices.getUserMedia not available!');
          }
          setError('Microphone access is not available. Try restarting Viola.');
          return;
        }

        if (import.meta.env.DEV) {
          console.log('[PTT_DEBUG] Calling getUserMedia...');
        }

        // Switch iOS audio session for mic access (reverted in stopRecording)
        if (navigator.audioSession) {
          navigator.audioSession.type = 'play-and-record';
        }

        // Request microphone access
        stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,
            sampleRate: 16000,
            // echoCancellation OFF: on iOS, enabling it activates the
            // VoiceProcessingIO hardware unit which ducks playback.
            echoCancellation: false,
            noiseSuppression: true,
          }
        });
      }

      if (import.meta.env.DEV) {
        console.log('[PTT_DEBUG] Got stream, active:', stream.active);
      }
      streamRef.current = stream;

      // Create MediaRecorder
      const detectedMimeType = getSupportedMimeType();
      const mediaRecorder = new MediaRecorder(
        stream,
        detectedMimeType ? { mimeType: detectedMimeType } : {},
      );
      mimeTypeRef.current = detectedMimeType || mediaRecorder.mimeType || '';
      mediaRecorderRef.current = mediaRecorder;

      mediaRecorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          audioChunksRef.current.push(event.data);
        }
      };

      mediaRecorder.start(100); // Collect data every 100ms
      _startVAD(stream);
      setIsRecording(true);

      // Notify backend to duck audio
      try {
        await authFetch('/v1/audio/duck', { method: 'POST' });
      } catch (e) {
        // Continue anyway
      }

    } catch (err) {
      // Log the actual error for debugging
      if (import.meta.env.DEV) {
        console.error('[useVoice] getUserMedia failed:', err.name, err.message, err);
      }

      // Shared with useVoiceWs so both voice paths say the same thing about
      // the same denial (see utils/describeError.js).
      setError(describeMicError(err));
    }
  }, [_startVAD]);

  // Stop recording and process
  const stopRecording = useCallback(async () => {
    if (!mediaRecorderRef.current || !isRecording) return;

    return new Promise((resolve) => {
      const mediaRecorder = mediaRecorderRef.current;

      mediaRecorder.onstop = async () => {
        _stopVAD();
        setIsRecording(false);
        setIsProcessing(true);

        // Stop tracks only if we acquired the stream ourselves (not shared)
        if (streamRef.current && !usingSharedStreamRef.current) {
          streamRef.current.getTracks().forEach(track => track.stop());
        }
        streamRef.current = null;
        usingSharedStreamRef.current = false;

        // Revert iOS audio session to playback-only
        if (navigator.audioSession) {
          navigator.audioSession.type = 'playback';
        }

        // Create blob from chunks
        const audioBlob = new Blob(audioChunksRef.current, { type: mimeTypeRef.current || 'audio/webm' });
        audioChunksRef.current = [];

        if (audioBlob.size < 100) {
          setError('Recording too short');
          setIsProcessing(false);
          resolve(null);
          return;
        }

        try {
          // Send to transcription API
          const formData = new FormData();
          const ext = mimeTypeRef.current.includes('mp4') ? 'm4a'
            : mimeTypeRef.current.includes('ogg') ? 'ogg'
              : 'webm';
          formData.append('audio', audioBlob, 'recording.' + ext);

          const response = await authFetch('/v1/transcribe', {
            method: 'POST',
            body: formData,
          });

          const result = await response.json();

          // Unduck audio
          try {
            await authFetch('/v1/audio/unduck', { method: 'POST' });
          } catch (e) {
            // Continue anyway
          }

          if (result.ok && result.data?.text) {
            const text = result.data.text;
            setTranscript(text);

            if (!executeCommandRef.current) {
              setIsProcessing(false);
              resolve({ ok: true, data: { text } });
              return;
            }

            // Execute command
            const cmdResponse = await authFetch('/v1/command', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ text }),
            });
            const cmdResult = await cmdResponse.json();

            if (onCommandResult) {
              onCommandResult(cmdResult);
            }

            if (cmdResult.data?.continue_listening === true) {
              await new Promise((delayResolve) => window.setTimeout(delayResolve, 300));

              if (mediaRecorderRef.current?.state !== 'recording') {
                try {
                  await startRecording();
                } catch (startError) {
                  if (import.meta.env.DEV) {
                    console.error('[useVoice] Failed to auto-resume recording:', startError);
                  }
                }
              }
            }

            setIsProcessing(false);
            resolve(cmdResult);
          } else {
            /*
              Two bugs used to live on this line. `result.error` is the API's
              failure ENVELOPE — an object `{code, message}`, never a string —
              so pushing it into error state made React throw on render and the
              user read "AI Response couldn't load" instead of the reason. And
              the "No speech detected" fallback was applied to every failure,
              including a 413 too-large or an unreachable transcriber, telling
              the user they had been silent when they had not.

              describeError resolves the truthful sentence from whatever shape
              came back (envelope error, FastAPI's `{detail}`, a bare string),
              and only falls back to the no-speech wording for the one case it
              actually describes: a well-formed success with no words in it.
            */
            setError(describeError(result?.error ?? result, ERROR_CODE_MESSAGES.no_speech_detected));
            setIsProcessing(false);
            resolve(null);
          }
        } catch (err) {
          // Network drop, aborted request, or a non-JSON body from a proxy.
          setError(describeError(err, "I couldn't reach Viola to run that. Check your connection and try again."));
          setIsProcessing(false);
          resolve(null);
        }
      };

      mediaRecorder.stop();
    });
  }, [_stopVAD, isRecording, onCommandResult, startRecording]);

  stopRecordingRef.current = stopRecording;

  // Cancel recording without processing
  const cancelRecording = useCallback(() => {
    if (mediaRecorderRef.current && isRecording) {
      _stopVAD();
      mediaRecorderRef.current.stop();
      setIsRecording(false);
    }
    if (streamRef.current && !usingSharedStreamRef.current) {
      streamRef.current.getTracks().forEach(track => track.stop());
    }
    streamRef.current = null;
    usingSharedStreamRef.current = false;
    audioChunksRef.current = [];

    if (navigator.audioSession) {
      navigator.audioSession.type = 'playback';
    }

    // Unduck audio
    authFetch('/v1/audio/unduck', { method: 'POST' }).catch((err) => {
      console.error('[useVoice] Unduck request failed; audio may stay ducked (quiet):', err);
    });
  }, [_stopVAD, isRecording]);

  return {
    isRecording,
    isProcessing,
    transcript,
    error,
    startRecording,
    stopRecording,
    cancelRecording,
    clearError: () => setError(null),
  };
}
