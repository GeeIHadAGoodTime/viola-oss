// The saved `wake_sensitivity` setting IS the wake-word score threshold: a
// HIGHER value is STRICTER (fewer false wakes), a lower one wakes more easily.
// The old slider showed that raw number and called it "sensitivity", so moving
// it up made Viola harder to wake while the tooltip promised the opposite.
//
// The control now shows named positions instead of the number. Moving toward
// "More sensitive" picks a LOWER threshold. What is saved is still the same
// threshold as before, so nobody's saved value changes meaning.
//
// Keep the range and default in step with core/constants.py
// (WAKE_THRESHOLD_USER_MIN / WAKE_THRESHOLD_USER_MAX) and config/defaults.py
// (DEFAULT_WAKE_SENSITIVITY).
//
// Since 1.0.5 build 5 the desktop detector scores every 80 ms and wakes on
// k-of-n frame agreement. Each saved level below is then one measured
// operating point (rule + per-frame threshold), listed per level in
// violawake/config.py STREAM_OPERATING_POINTS; keep these levels in step with
// that table. Every position has fewer false wakes and at least the recall it
// had before, so the labels still say what each position does.

export const WAKE_THRESHOLD_MIN = 0.8;
export const WAKE_THRESHOLD_MAX = 0.97;
export const WAKE_THRESHOLD_DEFAULT = 0.9;

// Ordered left to right: "Fewer false wakes" ... "More sensitive".
export const WAKE_SENSITIVITY_LEVELS = Object.freeze([
  Object.freeze({ threshold: 0.97, label: 'Fewest false wakes' }),
  Object.freeze({ threshold: 0.95, label: 'Fewer false wakes' }),
  Object.freeze({ threshold: 0.93, label: 'Slightly stricter' }),
  Object.freeze({ threshold: 0.9, label: 'Balanced (default)' }),
  Object.freeze({ threshold: 0.87, label: 'Slightly more sensitive' }),
  Object.freeze({ threshold: 0.84, label: 'More sensitive' }),
  Object.freeze({ threshold: 0.8, label: 'Most sensitive' }),
]);

export const WAKE_SENSITIVITY_MAX_POSITION = WAKE_SENSITIVITY_LEVELS.length - 1;

/** A saved threshold as one the detector will actually use (0.80-0.97). */
export function clampWakeThreshold(value) {
  const numeric = typeof value === 'string' && value.trim() === '' ? NaN : Number(value);
  if (value === null || value === undefined || typeof value === 'boolean' || !Number.isFinite(numeric)) {
    return WAKE_THRESHOLD_DEFAULT;
  }
  return Math.min(WAKE_THRESHOLD_MAX, Math.max(WAKE_THRESHOLD_MIN, numeric));
}

/** The slider position (0 = strictest) closest to a saved threshold. */
export function wakeThresholdToPosition(value) {
  const threshold = clampWakeThreshold(value);
  let best = 0;
  let bestDistance = Infinity;
  WAKE_SENSITIVITY_LEVELS.forEach((level, index) => {
    const distance = Math.abs(level.threshold - threshold);
    if (distance < bestDistance - 1e-9) {
      best = index;
      bestDistance = distance;
    }
  });
  return best;
}

/** The threshold to save for a slider position. */
export function positionToWakeThreshold(position) {
  const index = Math.round(Number(position));
  const safe = Number.isFinite(index) ? Math.min(WAKE_SENSITIVITY_MAX_POSITION, Math.max(0, index)) : 3;
  return WAKE_SENSITIVITY_LEVELS[safe].threshold;
}

export function wakeSensitivityLabel(position) {
  const index = Math.min(WAKE_SENSITIVITY_MAX_POSITION, Math.max(0, Math.round(Number(position)) || 0));
  return WAKE_SENSITIVITY_LEVELS[index].label;
}
