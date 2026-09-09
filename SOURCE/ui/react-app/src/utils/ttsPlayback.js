/**
 * Lightweight playback helper for hub-streamed TTS PCM frames.
 *
 * The hub's /ws/voice-stream endpoint sends synthesized TTS as binary
 * WebSocket frames prefixed with `TTS_PREFIX` (4 bytes "TTS\0"). New frames
 * include a small sample-rate header before the raw int16 mono PCM payload;
 * legacy frames without the header are treated as 16 kHz PCM. This module
 * owns a single AudioContext that survives across multiple TTS bursts so
 * successive utterances are gap-free.
 *
 * `playTtsPcm` accepts an ArrayBuffer or Uint8Array of int16 PCM samples
 * (prefix already stripped) and schedules playback on the shared context.
 * `isTtsFrame` returns true for raw incoming WS frames that carry the
 * TTS prefix; `stripTtsPrefix` returns the PCM payload.
 */

export const TTS_PREFIX = new Uint8Array([0x54, 0x54, 0x53, 0x00]); // "TTS\0"
export const TTS_PREFIX_LEN = TTS_PREFIX.length;
const TTS_SAMPLE_RATE_MAGIC = new Uint8Array([0x53, 0x52, 0x41, 0x54]); // "SRAT"
const TTS_SAMPLE_RATE_HEADER_LEN = TTS_SAMPLE_RATE_MAGIC.length + 4;
const TTS_SAMPLE_RATE = 16000;

let _ctx = null;
let _nextStartTime = 0;
const _activeSources = new Set();

function _ensureContext() {
  if (typeof window === 'undefined') return null;
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) return null;
  if (!_ctx || _ctx.state === 'closed') {
    _ctx = new Ctor();
    _nextStartTime = 0;
  }
  if (_ctx.state === 'suspended') {
    _ctx.resume().catch(() => {});
  }
  return _ctx;
}

/**
 * Pre-warm the shared TTS AudioContext from inside a user-gesture handler.
 * iOS Safari requires AudioContext.resume() to run synchronously inside a
 * click/touch stack; the WebSocket message that delivers TTS PCM is NOT a
 * gesture, so without this call iOS keeps the context suspended and TTS
 * plays silently. Call this from your "connect" / "start" button handler
 * BEFORE any async work.
 */
export function prewarmTtsContext() {
  _ensureContext();
}

/**
 * Test whether a binary frame is a TTS PCM payload.
 * @param {ArrayBuffer|Uint8Array} data
 * @returns {boolean}
 */
export function isTtsFrame(data) {
  if (!data) return false;
  const view = data instanceof Uint8Array ? data : new Uint8Array(data);
  if (view.byteLength <= TTS_PREFIX_LEN) return false;
  for (let i = 0; i < TTS_PREFIX_LEN; i += 1) {
    if (view[i] !== TTS_PREFIX[i]) return false;
  }
  return true;
}

/**
 * Return the PCM payload from a TTS-prefixed frame. Caller must have
 * already confirmed `isTtsFrame(data)`.
 * @param {ArrayBuffer|Uint8Array} data
 * @returns {ArrayBuffer}
 */
export function stripTtsPrefix(data) {
  const view = data instanceof Uint8Array ? data : new Uint8Array(data);
  return decodeTtsFrame(view).pcmBuffer;
}

/**
 * Decode a TTS-prefixed frame into PCM payload and sample rate.
 * @param {ArrayBuffer|Uint8Array} data
 * @returns {{pcmBuffer: ArrayBuffer, sampleRate: number}}
 */
export function decodeTtsFrame(data) {
  const view = data instanceof Uint8Array ? data : new Uint8Array(data);
  let offset = TTS_PREFIX_LEN;
  let sampleRate = TTS_SAMPLE_RATE;

  if (view.byteLength >= TTS_PREFIX_LEN + TTS_SAMPLE_RATE_HEADER_LEN) {
    let hasSampleRateHeader = true;
    for (let i = 0; i < TTS_SAMPLE_RATE_MAGIC.length; i += 1) {
      if (view[TTS_PREFIX_LEN + i] !== TTS_SAMPLE_RATE_MAGIC[i]) {
        hasSampleRateHeader = false;
        break;
      }
    }
    if (hasSampleRateHeader) {
      const rateOffset = TTS_PREFIX_LEN + TTS_SAMPLE_RATE_MAGIC.length;
      const dv = new DataView(view.buffer, view.byteOffset + rateOffset, 4);
      const parsedRate = dv.getUint32(0, true);
      if (parsedRate > 0) {
        sampleRate = parsedRate;
      }
      offset += TTS_SAMPLE_RATE_HEADER_LEN;
    }
  }

  return {
    pcmBuffer: view.slice(offset).buffer,
    sampleRate,
  };
}

/**
 * Play a chunk of int16 mono PCM, queued behind any in-flight
 * TTS audio so successive bursts run gap-free.
 * @param {ArrayBuffer} pcmBuffer  Raw int16 PCM (no prefix)
 * @param {number} sampleRate Sample rate of the PCM payload
 */
export function playTtsPcm(pcmBuffer, sampleRate = TTS_SAMPLE_RATE) {
  const ctx = _ensureContext();
  if (!ctx || !pcmBuffer || pcmBuffer.byteLength < 2) return;

  const int16 = new Int16Array(pcmBuffer);
  if (int16.length === 0) return;

  const audioBuffer = ctx.createBuffer(1, int16.length, sampleRate || TTS_SAMPLE_RATE);
  const channel = audioBuffer.getChannelData(0);
  for (let i = 0; i < int16.length; i += 1) {
    channel[i] = int16[i] / 32768;
  }

  const source = ctx.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(ctx.destination);

  const now = ctx.currentTime;
  const startAt = Math.max(now, _nextStartTime);
  source.start(startAt);
  _nextStartTime = startAt + audioBuffer.duration;

  _activeSources.add(source);
  source.onended = () => {
    _activeSources.delete(source);
    try { source.disconnect(); } catch { /* noop */ }
  };
}

/**
 * Drop any queued TTS audio (used to silence playback on cancel).
 */
export function stopTtsPlayback() {
  _activeSources.forEach((src) => {
    try { src.stop(); } catch { /* noop */ }
    try { src.disconnect(); } catch { /* noop */ }
  });
  _activeSources.clear();
  if (_ctx) _nextStartTime = _ctx.currentTime;
}
