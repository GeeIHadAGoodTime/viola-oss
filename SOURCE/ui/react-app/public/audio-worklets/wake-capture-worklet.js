/**
 * wake-capture-worklet.js
 *
 * Always-on microphone capture for the browser hands-free wake word. Runs in
 * the AudioWorklet thread so the always-listening loop never blocks the UI. It
 * resamples the input to 16 kHz (mono) if the AudioContext could not honor a
 * 16 kHz rate, then emits exactly-320-sample (20 ms) Float32 frames to the main
 * thread, which feeds them to the ONNX WakeDetector.
 *
 * NOTE: no audio ever leaves the worklet except as postMessage to the SAME tab.
 * The wake detector runs in-tab; nothing is uploaded until wake fires and the
 * separate voice-turn WebSocket opens.
 */

const TARGET_RATE = 16000;
const FRAME_SAMPLES = 320; // 20 ms at 16 kHz

class WakeCaptureWorklet extends AudioWorkletProcessor {
  constructor() {
    super();
    // Ratio of input rate to target rate (1.0 when context is already 16 kHz).
    this._ratio = sampleRate / TARGET_RATE;
    // Fractional read cursor into the pending input buffer, for linear resample.
    this._readPos = 0;
    // Pending input samples not yet fully consumed by the resampler.
    this._pending = new Float32Array(0);
    // Accumulated resampled (16 kHz) samples awaiting framing into 320-blocks.
    this._out = new Float32Array(0);
    this._active = true;

    this.port.onmessage = (event) => {
      if (event.data && event.data.type === "stop") {
        this._active = false;
      }
    };
  }

  process(inputs) {
    if (!this._active) return false; // returning false ends the processor
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const channel = input[0];
    if (!channel || channel.length === 0) return true;

    // Append this render block to the pending input buffer.
    const merged = new Float32Array(this._pending.length + channel.length);
    merged.set(this._pending);
    merged.set(channel, this._pending.length);
    this._pending = merged;

    // Linear-resample pending -> 16 kHz. Consume up to the last sample pair we
    // can interpolate; keep the tail for the next block.
    const produced = [];
    const ratio = this._ratio;
    let pos = this._readPos;
    const maxIndex = this._pending.length - 1;
    while (pos < maxIndex) {
      const lo = Math.floor(pos);
      const frac = pos - lo;
      produced.push(this._pending[lo] * (1 - frac) + this._pending[lo + 1] * frac);
      pos += ratio;
    }
    // Drop the consumed head, retain the fractional remainder so the cursor is
    // continuous across process() calls.
    const consumed = Math.floor(pos);
    if (consumed > 0) {
      this._pending = this._pending.slice(consumed);
      pos -= consumed;
    }
    this._readPos = pos;

    if (produced.length > 0) {
      const combined = new Float32Array(this._out.length + produced.length);
      combined.set(this._out);
      combined.set(produced, this._out.length);
      this._out = combined;
    }

    // Emit every complete 320-sample frame.
    while (this._out.length >= FRAME_SAMPLES) {
      const frame = this._out.slice(0, FRAME_SAMPLES);
      this._out = this._out.slice(FRAME_SAMPLES);
      // Transfer the buffer to avoid a copy on the main thread.
      this.port.postMessage(frame, [frame.buffer]);
    }

    return true;
  }
}

registerProcessor("wake-capture-worklet", WakeCaptureWorklet);
