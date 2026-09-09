/**
 * useBrowserWakeWord — client-side, in-tab wake-word detection for the browser
 * surface. When enabled, it runs the ViolaWake ONNX chain (melspectrogram ->
 * embedding -> temporal CNN) entirely in the tab via ONNX Runtime Web (WASM).
 * The always-listening loop costs the server nothing and NO audio leaves the
 * device until wake fires — only then does the caller open the voice-turn
 * WebSocket (exactly like push-to-talk).
 *
 * Cost discipline: ORT and the ~2.4 MB of models are dynamically imported and
 * fetched ONLY when `enabled` first becomes true, so the funnel page pays zero
 * bytes/CPU until the user opts in.
 *
 * Contract with the caller:
 *   - enabled: master switch. false => absolutely no mic capture, no models.
 *   - paused:  true while a voice turn is active (caller sets it) so the wake
 *              loop stops feeding audio to the detector and cannot self-trigger
 *              on the user's command or Viola's TTS. On paused:true->false the
 *              detector streaming state is reset for a clean next detection.
 *   - onWake:  invoked (no args) the instant a wake is confirmed in-tab.
 *   - onStreamReady(stream|null): the persistent mic MediaStream, so the caller
 *              can reuse it for the voice turn instead of a second getUserMedia.
 */

import { useEffect, useRef, useState } from 'react';

const MODELS = {
  melspec: 'wake/melspectrogram.onnx',
  embedding: 'wake/embedding_model.onnx',
  classifier: 'wake/temporal_cnn.onnx',
};

function assetBase() {
  const base = (import.meta.env && import.meta.env.BASE_URL) || '/static/react/';
  return base.endsWith('/') ? base : `${base}/`;
}

export function useBrowserWakeWord({
  enabled = false,
  paused = false,
  onWake,
  onStreamReady,
  threshold = 0.9,
  confirmCount = 1,
  cooldownS = 2.0,
} = {}) {
  const [status, setStatus] = useState('off'); // off | loading | listening | error
  const [error, setError] = useState(null);
  const [lastScore, setLastScore] = useState(0);

  // Latest-value refs so the long-lived capture loop reads current props.
  const pausedRef = useRef(paused);
  const onWakeRef = useRef(onWake);
  const onStreamReadyRef = useRef(onStreamReady);
  useEffect(() => { pausedRef.current = paused; }, [paused]);
  useEffect(() => { onWakeRef.current = onWake; }, [onWake]);
  useEffect(() => { onStreamReadyRef.current = onStreamReady; }, [onStreamReady]);

  // Live resources for teardown.
  const streamRef = useRef(null);
  const audioCtxRef = useRef(null);
  const sourceRef = useRef(null);
  const workletRef = useRef(null);
  const detectorRef = useRef(null);
  const queueRef = useRef([]);
  const pumpingRef = useRef(false);
  const wasPausedRef = useRef(false);
  // True once `paused` has actually been observed true since the last
  // detection — distinct from wasPausedRef, which is set the instant onWake
  // fires (suppression should start immediately). onWake -> startRecording()
  // is async, so frames can keep arriving with pausedRef.current still false
  // for a moment; without this flag the code below would take the
  // "turn just ended" branch on one of those frames and reset the detector's
  // cooldown before the turn genuinely started (a possible double-wake on the
  // wake-phrase tail).
  const pauseObservedRef = useRef(false);
  const runIdRef = useRef(0);

  useEffect(() => {
    if (!enabled) return undefined;

    const runId = ++runIdRef.current;
    let cancelled = false;
    setError(null);
    setStatus('loading');

    const teardown = () => {
      queueRef.current = [];
      pumpingRef.current = false;
      if (workletRef.current) {
        try { workletRef.current.port.postMessage({ type: 'stop' }); } catch { /* noop */ }
        try { workletRef.current.disconnect(); } catch { /* noop */ }
        workletRef.current.port.onmessage = null;
        workletRef.current = null;
      }
      if (sourceRef.current) {
        try { sourceRef.current.disconnect(); } catch { /* noop */ }
        sourceRef.current = null;
      }
      if (audioCtxRef.current) {
        audioCtxRef.current.close().catch(() => {});
        audioCtxRef.current = null;
      }
      if (streamRef.current) {
        streamRef.current.getTracks().forEach((t) => t.stop());
        streamRef.current = null;
      }
      if (detectorRef.current) {
        try { detectorRef.current.dispose(); } catch { /* noop */ }
        detectorRef.current = null;
      }
      if (onStreamReadyRef.current) {
        try { onStreamReadyRef.current(null); } catch { /* noop */ }
      }
    };

    const pump = async () => {
      if (pumpingRef.current) return;
      pumpingRef.current = true;
      try {
        while (queueRef.current.length > 0) {
          const frame = queueRef.current.shift();
          const detector = detectorRef.current;
          if (!detector) break;

          if (pausedRef.current) {
            // A turn is active: don't feed audio to the detector.
            wasPausedRef.current = true;
            pauseObservedRef.current = true;
            continue;
          }
          if (wasPausedRef.current) {
            if (!pauseObservedRef.current) {
              // Suppressed since a detection, but the caller's async
              // startRecording() hasn't flipped `paused` true yet — keep
              // suppressing without touching the detector's cooldown until
              // the turn has genuinely been observed to start.
              continue;
            }
            // Turn just ended — reset streaming state for a clean detection.
            wasPausedRef.current = false;
            pauseObservedRef.current = false;
            try { detector.reset(); } catch { /* noop */ }
          }

          let detected = false;
          try {
            detected = await detector.detect(frame);
          } catch {
            // A single bad frame must not kill the loop.
            continue;
          }
          if (cancelled || runId !== runIdRef.current) return;
          setLastScore(detector.lastScore);

          if (detected && !pausedRef.current) {
            wasPausedRef.current = true; // suppress until the turn cycle completes
            if (onWakeRef.current) {
              try { onWakeRef.current(); } catch { /* noop */ }
            }
          }
        }
      } finally {
        pumpingRef.current = false;
      }
    };

    (async () => {
      try {
        // Lazy-load runtime + detector only now (zero cost before opt-in).
        const [ort, { WakeDetector }] = await Promise.all([
          import('onnxruntime-web/wasm'),
          import('../lib/wake'),
        ]);
        if (cancelled || runId !== runIdRef.current) return;

        const base = assetBase();
        // The onnxruntime-web/wasm bundle resolves its .wasm binary via
        // import.meta.url, which Vite rewrites to a same-origin hashed asset —
        // no wasmPaths override or CDN needed (strict CSP stays satisfied).
        ort.env.wasm.numThreads = 1; // single-threaded: no SharedArrayBuffer / COOP-COEP
        ort.env.wasm.simd = true;
        ort.env.wasm.proxy = false;

        const detector = new WakeDetector({
          threshold,
          confirmCount,
          cooldownS,
          melspecModelUrl: `${base}${MODELS.melspec}`,
          embeddingModelUrl: `${base}${MODELS.embedding}`,
          classifierModelUrl: `${base}${MODELS.classifier}`,
          ortOptions: { executionProviders: ['wasm'] },
        });
        await detector.load();
        if (cancelled || runId !== runIdRef.current) return;
        detectorRef.current = detector;

        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
          throw new Error('Microphone capture is not available in this browser.');
        }
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,
            sampleRate: 16000,
            echoCancellation: true, // suppress Viola's own TTS so it can't self-trigger
            noiseSuppression: true,
            autoGainControl: true,
          },
        });
        if (cancelled || runId !== runIdRef.current) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        streamRef.current = stream;
        if (onStreamReadyRef.current) {
          try { onStreamReadyRef.current(stream); } catch { /* noop */ }
        }

        const AudioCtor = window.AudioContext || window.webkitAudioContext;
        const audioCtx = new AudioCtor({ sampleRate: 16000 });
        audioCtxRef.current = audioCtx;
        if (audioCtx.state === 'suspended') {
          await audioCtx.resume().catch(() => {});
        }
        await audioCtx.audioWorklet.addModule(`${base}audio-worklets/wake-capture-worklet.js`);
        if (cancelled || runId !== runIdRef.current) return;

        const source = audioCtx.createMediaStreamSource(stream);
        sourceRef.current = source;
        const worklet = new AudioWorkletNode(audioCtx, 'wake-capture-worklet');
        workletRef.current = worklet;
        worklet.port.onmessage = (event) => {
          const frame = event.data;
          if (!(frame instanceof Float32Array)) return;
          // Bounded queue: if we ever fall behind realtime, drop oldest so we
          // never grow unbounded (a detect() slower than realtime is the only
          // way here, which the tiny models make very unlikely).
          const q = queueRef.current;
          q.push(frame);
          if (q.length > 250) q.splice(0, q.length - 250);
          pump();
        };
        source.connect(worklet);
        // Do NOT connect the worklet to destination — we don't want to play the
        // mic back. A worklet still runs process() without a downstream sink.

        if (cancelled || runId !== runIdRef.current) return;
        setStatus('listening');
      } catch (err) {
        if (cancelled || runId !== runIdRef.current) return;
        setError(err && err.message ? err.message : 'Wake word unavailable');
        setStatus('error');
        teardown();
      }
    })();

    return () => {
      cancelled = true;
      teardown();
      setStatus('off');
    };
    // threshold/confirmCount/cooldownS are read at load; changing them re-inits.
  }, [enabled, threshold, confirmCount, cooldownS]);

  return { status, error, lastScore };
}

export default useBrowserWakeWord;
