/**
 * features.ts — VENDORED from the ViolaWake WASM port (npm package `violawake`
 * v0.1.0, Apache-2.0). Source of truth: J:\CLAUDE\PROJECTS\Wakeword\wasm\src\features.ts
 * (a line-for-line mirror of the Python SDK `oww_backbone.py`). Vendored here
 * because `violawake` is not published to the public npm registry; bundling the
 * source keeps everything in one CSP-safe, same-origin Vite graph.
 *
 * The ONLY local modification vs. the upstream source is the ORT import
 * specifier (`onnxruntime-web/wasm` — the CPU-only build — instead of the full
 * `onnxruntime-web`). Algorithm, constants, and streaming state are unchanged so
 * the Python-vs-WASM parity oracle (max_linf <= 1e-3) still holds.
 *
 * Attribution (Apache-2.0): ViolaWake SDK + temporal_cnn head, and the frozen
 * OpenWakeWord melspectrogram/embedding backbone it wraps. See NOTICE-violawake.txt.
 *
 * Streaming audio buffer and mel-spectrogram + embedding extraction that mirrors
 * OpenWakeWordBackbone from oww_backbone.py.
 *
 * Pipeline (identical to Python SDK):
 *   int16 PCM  ->  melspectrogram ONNX  ->  / 10.0 + 2.0  ->  76-frame window
 *               ->  embedding ONNX  ->  96-d float32 vector
 */

import * as ort from "onnxruntime-web/wasm";

// --- Constants (must match oww_backbone.py) ---
export const SAMPLE_RATE = 16_000;
export const MEL_FRAMES_PER_EMBEDDING = 76;
export const MEL_STRIDE = 8;
export const EMBEDDING_DIM = 96;
export const OWW_CHUNK_SAMPLES = 1_280; // 80ms at 16 kHz
const MELSPEC_CONTEXT_SAMPLES = 160 * 3; // 480 samples context overlap
const MAX_RAW_SAMPLES = SAMPLE_RATE * 10; // 10s ring buffer
const MAX_MELSPEC_FRAMES = 10 * 97; // ~10s of mel frames at ~97 frames/s

// ---------------------------------------------------------------------------
// Ring buffer (mirrors _RingBuffer in oww_backbone.py)
// Stores int16 samples in a fixed-capacity Int16Array.
// ---------------------------------------------------------------------------

class RingBuffer {
  private readonly buf: Int16Array;
  private readonly capacity: number;
  private writePos = 0;
  private count = 0;

  constructor(capacity: number) {
    this.capacity = capacity;
    this.buf = new Int16Array(capacity);
  }

  get length(): number {
    return this.count;
  }

  /** Append int16 samples. */
  extend(data: Int16Array): void {
    const n = data.length;
    if (n === 0) return;

    if (n >= this.capacity) {
      // Keep only the tail
      this.buf.set(data.subarray(data.length - this.capacity));
      this.writePos = 0;
      this.count = this.capacity;
      return;
    }

    const end = this.writePos + n;
    if (end <= this.capacity) {
      this.buf.set(data, this.writePos);
    } else {
      const first = this.capacity - this.writePos;
      this.buf.set(data.subarray(0, first), this.writePos);
      this.buf.set(data.subarray(first), 0);
    }

    this.writePos = end % this.capacity;
    this.count = Math.min(this.count + n, this.capacity);
  }

  /** Return the last n samples in chronological order. */
  tail(n: number): Int16Array {
    n = Math.min(n, this.count);
    if (n === 0) return new Int16Array(0);

    const start = (this.writePos - n + this.capacity) % this.capacity;
    if (start + n <= this.capacity) {
      return this.buf.slice(start, start + n);
    }

    // Wraps around — two slices
    const result = new Int16Array(n);
    const firstLen = this.capacity - start;
    result.set(this.buf.subarray(start), 0);
    result.set(this.buf.subarray(0, this.writePos), firstLen);
    return result;
  }
}

// ---------------------------------------------------------------------------
// OWW Backbone (mirrors OpenWakeWordBackbone from oww_backbone.py)
// ---------------------------------------------------------------------------

export interface BackboneResult {
  /** Whether a new embedding was produced this call. */
  produced: boolean;
  /** The latest 96-d embedding, or null if no embedding yet. */
  embedding: Float32Array | null;
}

export class OWWBackbone {
  private readonly melspecSession: ort.InferenceSession;
  private readonly embeddingSession: ort.InferenceSession;
  private readonly melspecInputName: string;
  private readonly embeddingInputName: string;

  // Streaming state
  private rawBuffer: RingBuffer;
  private melspecBuffer: Float32Array; // (N, 32) stored flat, row-major
  private melspecRows = 0; // number of mel frames in buffer
  private accumulatedSamples = 0;
  private remainder: Int16Array = new Int16Array(0);
  private lastEmbedding: Float32Array | null = null;

  private constructor(
    melspecSession: ort.InferenceSession,
    embeddingSession: ort.InferenceSession,
  ) {
    this.melspecSession = melspecSession;
    this.embeddingSession = embeddingSession;
    this.melspecInputName = melspecSession.inputNames[0];
    this.embeddingInputName = embeddingSession.inputNames[0];

    // Pre-fill melspec buffer with 1.0 (matches Python: np.ones((76, 32)))
    this.rawBuffer = new RingBuffer(MAX_RAW_SAMPLES);
    this.melspecBuffer = new Float32Array(MAX_MELSPEC_FRAMES * 32).fill(1.0);
    this.melspecRows = MEL_FRAMES_PER_EMBEDDING; // pre-warmed context
  }

  static async create(
    melspecModelUrl: string,
    embeddingModelUrl: string,
    ortOptions?: ort.InferenceSession.SessionOptions,
  ): Promise<OWWBackbone> {
    const opts: ort.InferenceSession.SessionOptions = {
      executionProviders: ["wasm"],
      ...ortOptions,
    };
    const [mel, emb] = await Promise.all([
      ort.InferenceSession.create(melspecModelUrl, opts),
      ort.InferenceSession.create(embeddingModelUrl, opts),
    ]);
    return new OWWBackbone(mel, emb);
  }

  reset(): void {
    this.rawBuffer = new RingBuffer(MAX_RAW_SAMPLES);
    this.melspecBuffer = new Float32Array(MAX_MELSPEC_FRAMES * 32).fill(1.0);
    this.melspecRows = MEL_FRAMES_PER_EMBEDDING;
    this.accumulatedSamples = 0;
    this.remainder = new Int16Array(0);
    this.lastEmbedding = null;
  }

  /**
   * Push an audio frame (int16 PCM or float32 normalised to [-1, 1]).
   * Returns {produced, embedding} matching Python push_audio().
   */
  async pushAudio(audioFrame: Int16Array | Float32Array): Promise<BackboneResult> {
    // Convert to int16 (mirrors _to_pcm_int16)
    let pcmI16: Int16Array;
    if (audioFrame instanceof Int16Array) {
      pcmI16 = audioFrame;
    } else {
      // float32 in [-1, 1] -> int16
      pcmI16 = new Int16Array(audioFrame.length);
      for (let i = 0; i < audioFrame.length; i++) {
        const s = Math.max(-1, Math.min(1, audioFrame[i]));
        pcmI16[i] = Math.trunc(s * 32767);
      }
    }

    // Prepend remainder from previous call
    if (this.remainder.length > 0) {
      const merged = new Int16Array(this.remainder.length + pcmI16.length);
      merged.set(this.remainder);
      merged.set(pcmI16, this.remainder.length);
      pcmI16 = merged;
      this.remainder = new Int16Array(0);
    }

    const total = this.accumulatedSamples + pcmI16.length;
    const remainder = total % OWW_CHUNK_SAMPLES;
    const toBuffer = remainder > 0 ? pcmI16.subarray(0, pcmI16.length - remainder) : pcmI16;

    if (remainder > 0) {
      this.remainder = pcmI16.slice(pcmI16.length - remainder);
    }

    this.rawBuffer.extend(toBuffer);
    this.accumulatedSamples += toBuffer.length;

    const newEmbeddings: Float32Array[] = [];

    if (
      this.accumulatedSamples >= OWW_CHUNK_SAMPLES &&
      this.accumulatedSamples % OWW_CHUNK_SAMPLES === 0
    ) {
      await this._streamingMelspectrogram(this.accumulatedSamples);

      const nChunks = this.accumulatedSamples / OWW_CHUNK_SAMPLES;

      // Iterate newest-first (matches Python loop: range(n_chunks-1, -1, -1))
      for (let chunkIdx = nChunks - 1; chunkIdx >= 0; chunkIdx--) {
        const offset = MEL_STRIDE * chunkIdx; // frames from end
        const endRow = this.melspecRows - offset;
        const startRow = endRow - MEL_FRAMES_PER_EMBEDDING;

        if (startRow >= 0 && endRow <= this.melspecRows) {
          const window = this._getMelWindow(startRow, endRow); // (76, 32)
          const embedding = await this._predictEmbedding(window);
          this.lastEmbedding = embedding;
          newEmbeddings.push(embedding);
        }
      }

      this.accumulatedSamples = 0;
    }

    if (newEmbeddings.length > 0) {
      return { produced: true, embedding: newEmbeddings[newEmbeddings.length - 1] };
    }
    return { produced: false, embedding: this.lastEmbedding };
  }

  // --- Private helpers ---

  private async _streamingMelspectrogram(nSamples: number): Promise<void> {
    const windowSamples = nSamples + MELSPEC_CONTEXT_SAMPLES;
    const raw = this.rawBuffer.tail(windowSamples);
    const newFrames = await this._predictMelspectrogram(raw);

    // Append new frames and trim to MAX_MELSPEC_FRAMES
    const newRows = newFrames.length / 32;
    const combined = this._appendMelFrames(newFrames, newRows);
    this.melspecBuffer = combined.buffer;
    this.melspecRows = combined.rows;
  }

  private _appendMelFrames(
    newFrames: Float32Array,
    newRows: number,
  ): { buffer: Float32Array; rows: number } {
    const totalRows = this.melspecRows + newRows;
    if (totalRows <= MAX_MELSPEC_FRAMES) {
      const buf = new Float32Array(totalRows * 32);
      buf.set(this.melspecBuffer.subarray(0, this.melspecRows * 32));
      buf.set(newFrames, this.melspecRows * 32);
      return { buffer: buf, rows: totalRows };
    }
    // Trim oldest frames
    const keepRows = Math.min(MAX_MELSPEC_FRAMES, totalRows);
    const buf = new Float32Array(keepRows * 32);
    const dropRows = totalRows - keepRows;
    // Copy tail of old buffer (after dropRows) plus new frames
    const oldKeepRows = this.melspecRows - dropRows;
    if (oldKeepRows > 0) {
      buf.set(this.melspecBuffer.subarray(dropRows * 32, this.melspecRows * 32));
      buf.set(newFrames, oldKeepRows * 32);
    } else {
      // New frames alone exceed the limit — keep their tail
      const dropNew = -oldKeepRows;
      buf.set(newFrames.subarray(dropNew * 32));
    }
    return { buffer: buf, rows: keepRows };
  }

  private _getMelWindow(startRow: number, endRow: number): Float32Array {
    return this.melspecBuffer.slice(startRow * 32, endRow * 32);
  }

  private async _predictMelspectrogram(pcmI16: Int16Array): Promise<Float32Array> {
    // Input: float32 batch (1, N) — model expects raw float32 PCM in int16 range
    const f32 = new Float32Array(pcmI16.length);
    for (let i = 0; i < pcmI16.length; i++) {
      f32[i] = pcmI16[i]; // Keep int16 magnitude, cast to float32
    }
    const tensor = new ort.Tensor("float32", f32, [1, f32.length]);
    const feeds: Record<string, ort.Tensor> = { [this.melspecInputName]: tensor };
    const results = await this.melspecSession.run(feeds);
    const output = results[this.melspecSession.outputNames[0]];
    // Shape: (1, N_frames, 32) — squeeze batch dim
    const raw = output.data as Float32Array;
    // Apply OWW normalization: / 10.0 + 2.0
    const out = new Float32Array(raw.length);
    for (let i = 0; i < raw.length; i++) {
      out[i] = raw[i] / 10.0 + 2.0;
    }
    return out; // flat (N_frames * 32)
  }

  private async _predictEmbedding(melWindow: Float32Array): Promise<Float32Array> {
    // Input shape: (1, 76, 32, 1) — batch, frames, bins, channel
    const rows = MEL_FRAMES_PER_EMBEDDING;
    const cols = 32;
    const input = new Float32Array(rows * cols); // batch+channel squeezed
    input.set(melWindow.subarray(0, rows * cols));
    const tensor = new ort.Tensor("float32", input, [1, rows, cols, 1]);
    const feeds: Record<string, ort.Tensor> = { [this.embeddingInputName]: tensor };
    const results = await this.embeddingSession.run(feeds);
    const output = results[this.embeddingSession.outputNames[0]];
    const raw = output.data as Float32Array;
    // Flatten to 96-d
    const emb = new Float32Array(EMBEDDING_DIM);
    emb.set(raw.subarray(0, EMBEDDING_DIM));
    return emb;
  }
}
