/**
 * detector.ts — VENDORED from the ViolaWake WASM port (npm `violawake` v0.1.0,
 * Apache-2.0). Source of truth: J:\CLAUDE\PROJECTS\Wakeword\wasm\src\detector.ts,
 * a browser/WASM port of `violawake_sdk.WakeDetector` (wake_detector.py). Vendored
 * because `violawake` is unpublished; see features.ts header + NOTICE-violawake.txt.
 *
 * Local modifications vs. upstream: (1) ORT import is the CPU-only
 * `onnxruntime-web/wasm` build; (2) local import is extensionless so Vite/esbuild
 * resolves the `.ts` source. Algorithm and the 4-gate decision policy are
 * unchanged (Python-vs-WASM parity oracle max_linf <= 1e-3 still holds).
 *
 * Pipeline (matches wake_detector.py):
 *   audio frame (Float32Array, 16kHz mono, 320 samples / 20ms)
 *     -> OWWBackbone (melspec + embedding ONNX)
 *     -> Temporal CNN ONNX (or plain MLP, per model input rank)
 *     -> score (0.0 - 1.0)
 *     -> 4-gate decision (RMS, threshold, confirm, cooldown)
 */

import * as ort from "onnxruntime-web/wasm";
import { OWWBackbone, EMBEDDING_DIM, SAMPLE_RATE } from "./features";

const FRAME_SAMPLES = (SAMPLE_RATE / 1000) * 20;

export interface WakeDetectorOptions {
  /** Detection confidence threshold (0.0-1.0). Default: 0.80 (Python SDK default). */
  threshold?: number;
  /** Minimum seconds between consecutive detections. Default: 2.0 */
  cooldownS?: number;
  /** Consecutive above-threshold frames required before firing. Default: 1. */
  confirmCount?: number;
  /** URL for the melspectrogram backbone ONNX. */
  melspecModelUrl?: string;
  /** URL for the embedding backbone ONNX. */
  embeddingModelUrl?: string;
  /** URL for the ViolaWake classifier ONNX. */
  classifierModelUrl?: string;
  /** ONNX Runtime Web session options forwarded to all three sessions. */
  ortOptions?: ort.InferenceSession.SessionOptions;
}

export class WakeDetector {
  private readonly threshold: number;
  private readonly cooldownS: number;
  private readonly confirmCount: number;

  private backbone!: OWWBackbone;
  private classifierSession!: ort.InferenceSession;
  private classifierInputName!: string;

  private isTemporal = false;
  private temporalSeqLen = 9;
  private embeddingBuffer: Float32Array[] = [];

  private _lastScore = 0.0;

  /** The raw score from the most recent `detect()` or `getScore()` call. */
  get lastScore(): number {
    return this._lastScore;
  }
  private lastDetectionTime = 0; // performance.now() ms
  private confirmCounter = 0;

  private readonly melspecModelUrl: string;
  private readonly embeddingModelUrl: string;
  private readonly classifierModelUrl: string;
  private readonly ortOptions: ort.InferenceSession.SessionOptions;

  constructor(options: WakeDetectorOptions = {}) {
    this.threshold = options.threshold ?? 0.8;
    this.cooldownS = options.cooldownS ?? 2.0;
    this.confirmCount = options.confirmCount ?? 1;
    this.melspecModelUrl = options.melspecModelUrl ?? "./models/melspectrogram.onnx";
    this.embeddingModelUrl = options.embeddingModelUrl ?? "./models/embedding_model.onnx";
    this.classifierModelUrl = options.classifierModelUrl ?? "./models/temporal_cnn.onnx";
    this.ortOptions = options.ortOptions ?? { executionProviders: ["wasm"] };

    if (this.threshold < 0.0 || this.threshold > 1.0) {
      throw new RangeError(`threshold must be in [0.0, 1.0], got ${this.threshold}`);
    }
    if (this.cooldownS < 0) {
      throw new RangeError(`cooldownS must be >= 0, got ${this.cooldownS}`);
    }
    if (this.confirmCount < 1) {
      throw new RangeError(`confirmCount must be >= 1, got ${this.confirmCount}`);
    }
  }

  /** Load all three ONNX models. Must be called before detect() / getScore(). */
  async load(): Promise<void> {
    this.backbone = await OWWBackbone.create(
      this.melspecModelUrl,
      this.embeddingModelUrl,
      this.ortOptions,
    );

    this.classifierSession = await ort.InferenceSession.create(
      this.classifierModelUrl,
      this.ortOptions,
    );
    this.classifierInputName = this.classifierSession.inputNames[0];

    const shape = this._getClassifierInputShape(this.classifierInputName);
    if (shape?.length === 3) {
      this._validateClassifierInputShape(shape);
      this.isTemporal = true;
      this.temporalSeqLen = typeof shape[1] === "number" && shape[1] > 0 ? shape[1] : 9;
    } else if (shape?.length === 2) {
      this._validateClassifierInputShape(shape);
      this.isTemporal = false;
    } else if (this._classifierLooksTemporal()) {
      this.isTemporal = true;
      this.temporalSeqLen = 9;
    } else {
      throw new Error(
        `Classifier model has unsupported input shape ${JSON.stringify(shape)}; expected ` +
          `[1, ${EMBEDDING_DIM}] or [1, seq_len, ${EMBEDDING_DIM}].`,
      );
    }
    this._validateClassifierOutputShape();
  }

  /**
   * Process a 20ms audio frame (320 samples at 16kHz, float32 in [-1, 1]).
   * Returns true if wake word detected. Applies the 4-gate decision policy.
   */
  async detect(audioBuffer: Float32Array): Promise<boolean> {
    const score = await this.getScore(audioBuffer);

    // Gate 1: RMS floor (silence / DC offset guard)
    const rms = this._computeRms(audioBuffer);
    if (rms < 1.0 / 32768.0) {
      return false;
    }

    // Gate 2: Threshold
    if (score < this.threshold) {
      this.confirmCounter = 0;
      return false;
    }

    // Gate 3 (K2): Confirmation
    this.confirmCounter++;
    if (this.confirmCounter < this.confirmCount) {
      return false;
    }
    this.confirmCounter = 0;

    // Gate 4: Cooldown
    const now = performance.now();
    if (now - this.lastDetectionTime < this.cooldownS * 1000) {
      return false;
    }
    this.lastDetectionTime = now;
    return true;
  }

  /**
   * Process a 20ms audio frame and return the raw classifier score (0.0-1.0).
   * Bypasses all decision gates.
   */
  async getScore(audioBuffer: Float32Array): Promise<number> {
    if (!this.backbone || !this.classifierSession) {
      throw new Error("WakeDetector not loaded. Call load() first.");
    }
    this._validateAudioFrame(audioBuffer);

    const { produced, embedding } = await this.backbone.pushAudio(audioBuffer);

    let score: number;

    if (embedding === null) {
      score = this._lastScore;
    } else if (this.isTemporal) {
      if (produced) {
        this.embeddingBuffer.push(embedding.slice());
        if (this.embeddingBuffer.length > this.temporalSeqLen) {
          this.embeddingBuffer.shift();
        }
        if (this.embeddingBuffer.length >= this.temporalSeqLen) {
          score = await this._runTemporalClassifier();
        } else {
          score = 0.0;
        }
      } else {
        score = this._lastScore;
      }
    } else {
      if (produced) {
        score = await this._runMlpClassifier(embedding);
      } else {
        score = this._lastScore;
      }
    }

    this._lastScore = score;
    return score;
  }

  /** Reset internal streaming state. Does NOT unload the ONNX sessions. */
  reset(): void {
    this.backbone?.reset();
    this.embeddingBuffer = [];
    this._lastScore = 0.0;
    this.lastDetectionTime = 0;
    this.confirmCounter = 0;
  }

  /** Reset the cooldown window, allowing immediate re-detection. */
  resetCooldown(): void {
    this.lastDetectionTime = 0;
  }

  /** Release ONNX inference sessions. */
  dispose(): void {
    this.reset();
    (this.classifierSession as unknown as { release?: () => void })?.release?.();
  }

  // --- Private helpers ---

  private async _runTemporalClassifier(): Promise<number> {
    const flat = new Float32Array(this.temporalSeqLen * EMBEDDING_DIM);
    for (let i = 0; i < this.temporalSeqLen; i++) {
      flat.set(this.embeddingBuffer[i], i * EMBEDDING_DIM);
    }
    const tensor = new ort.Tensor("float32", flat, [1, this.temporalSeqLen, EMBEDDING_DIM]);
    const feeds: Record<string, ort.Tensor> = { [this.classifierInputName]: tensor };
    const results = await this.classifierSession.run(feeds);
    const output = results[this.classifierSession.outputNames[0]];
    return (output.data as Float32Array)[0];
  }

  private async _runMlpClassifier(embedding: Float32Array): Promise<number> {
    const tensor = new ort.Tensor("float32", embedding.slice(), [1, EMBEDDING_DIM]);
    const feeds: Record<string, ort.Tensor> = { [this.classifierInputName]: tensor };
    const results = await this.classifierSession.run(feeds);
    const output = results[this.classifierSession.outputNames[0]];
    return (output.data as Float32Array)[0];
  }

  private _getClassifierInputShape(inputName: string): unknown[] | null {
    const metadata = (this.classifierSession as unknown as { inputMetadata?: unknown })?.inputMetadata;
    if (!metadata) return null;
    const inputMeta = Array.isArray(metadata)
      ? metadata.find((entry: { name?: string }) => entry?.name === inputName) ?? metadata[0]
      : (metadata as Record<string, unknown>)[inputName];
    const meta = inputMeta as { dimensions?: unknown; shape?: unknown } | undefined;
    const shape = meta?.dimensions ?? meta?.shape;
    return Array.isArray(shape) ? shape : null;
  }

  private _getClassifierOutputShape(): unknown[] | null {
    const metadata = (this.classifierSession as unknown as { outputMetadata?: unknown })?.outputMetadata;
    if (!metadata) return null;
    const outputName = this.classifierSession.outputNames[0];
    const outputMeta = Array.isArray(metadata)
      ? metadata.find((entry: { name?: string }) => entry?.name === outputName) ?? metadata[0]
      : (metadata as Record<string, unknown>)[outputName];
    const meta = outputMeta as { dimensions?: unknown; shape?: unknown } | undefined;
    const shape = meta?.dimensions ?? meta?.shape;
    return Array.isArray(shape) ? shape : null;
  }

  private _validateClassifierInputShape(shape: unknown[]): void {
    const lastDim = shape[shape.length - 1];
    if (lastDim !== EMBEDDING_DIM) {
      throw new Error(
        `Classifier model input must end with ${EMBEDDING_DIM} OWW embedding dimensions; ` +
          `got shape ${JSON.stringify(shape)}.`,
      );
    }
    if (shape.length === 3) {
      const seqLen = shape[1];
      if (typeof seqLen === "number" && seqLen < 1) {
        throw new Error(`Classifier temporal sequence length must be >= 1; got ${seqLen}.`);
      }
    }
  }

  private _validateClassifierOutputShape(): void {
    const shape = this._getClassifierOutputShape();
    if (shape === null) return;
    const concreteDims = shape.filter((dim) => typeof dim === "number") as number[];
    const elementCount = concreteDims.reduce((acc, dim) => acc * dim, 1);
    if (concreteDims.length > 0 && elementCount !== 1) {
      throw new Error(
        `Classifier model output must be a single scalar score; got shape ${JSON.stringify(shape)}.`,
      );
    }
  }

  private _validateAudioFrame(audioBuffer: Float32Array): void {
    if (!(audioBuffer instanceof Float32Array)) {
      throw new TypeError("audioBuffer must be a Float32Array.");
    }
    if (audioBuffer.length !== FRAME_SAMPLES) {
      throw new RangeError(
        `audioBuffer must contain exactly ${FRAME_SAMPLES} samples ` +
          `(20ms at ${SAMPLE_RATE}Hz); got ${audioBuffer.length}.`,
      );
    }
    for (let i = 0; i < audioBuffer.length; i++) {
      const sample = audioBuffer[i];
      if (!Number.isFinite(sample)) {
        throw new RangeError(`audioBuffer sample ${i} is not finite.`);
      }
      if (sample < -1.0 || sample > 1.0) {
        throw new RangeError(
          `audioBuffer sample ${i} must be normalized to [-1, 1]; got ${sample}.`,
        );
      }
    }
  }

  private _classifierLooksTemporal(): boolean {
    return /temporal|convgru|gru/i.test(this.classifierModelUrl);
  }

  private _computeRms(audioBuffer: Float32Array): number {
    let sum = 0;
    for (let i = 0; i < audioBuffer.length; i++) {
      sum += audioBuffer[i] * audioBuffer[i];
    }
    return Math.sqrt(sum / audioBuffer.length);
  }
}
