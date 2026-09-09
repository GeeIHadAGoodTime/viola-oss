/**
 * ViolaWake WASM — browser wake-word detection (vendored from npm `violawake`
 * v0.1.0, Apache-2.0). See features.ts / detector.ts headers + NOTICE-violawake.txt.
 */

export { WakeDetector } from "./detector";
export type { WakeDetectorOptions } from "./detector";
export {
  OWWBackbone,
  SAMPLE_RATE,
  EMBEDDING_DIM,
  MEL_FRAMES_PER_EMBEDDING,
  MEL_STRIDE,
  OWW_CHUNK_SAMPLES,
} from "./features";
