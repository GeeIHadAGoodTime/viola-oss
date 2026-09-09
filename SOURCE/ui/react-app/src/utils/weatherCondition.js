/**
 * One place that turns a weather payload's condition fields into something the
 * UI can draw.
 *
 * The backend publishes `condition_code` (a small closed vocabulary) alongside
 * the human-readable `condition` text, and it omits the text entirely when the
 * provider did not report a sky state. Every surface must read that the same
 * way, and every surface must be able to say "unknown" — the topbar previously
 * kept its cheerful initial glyph whenever the payload did not match one of its
 * keywords, so a missing condition rendered as a confident partly-cloudy claim.
 */

export const UNKNOWN_CONDITION = 'unknown';

export const CONDITION_UNAVAILABLE_TEXT = 'Condition unavailable';

export const CONDITION_UNAVAILABLE_SHORT_TEXT = 'Unavailable';

// Text the backend (or an older cached payload) may put in the condition slot
// that carries no sky information. Mirrors NON_CONDITION_TEXTS in
// utils/weather_normalization.py.
const NON_CONDITION_TEXTS = new Set([
  'unknown',
  'unknown condition',
  'n/a',
  'na',
  'none',
  'null',
  'not available',
  'unavailable',
  '--',
  '-',
  'weather',
]);

// Canonical icon keys. `unknown` is a first-class member, not a fallback.
const CONDITION_KEYS = new Set([
  'clear',
  'partly-cloudy',
  'cloudy',
  'overcast',
  'rain',
  'drizzle',
  'storm',
  'snow',
  'sleet',
  'hail',
  'fog',
  'mist',
  'haze',
  'smoke',
  'dust',
  'wind',
  UNKNOWN_CONDITION,
]);

// Substring rules, most specific first. These read BOTH the backend's
// condition_code vocabulary (`partly_cloudy`, `thunderstorm`, ...) and raw
// provider text ("Mostly Cloudy", "Chance Showers And Thunderstorms").
const CONDITION_PATTERNS = [
  ['thunder', 'storm'],
  ['storm', 'storm'],
  ['blizzard', 'snow'],
  ['snow', 'snow'],
  ['flurr', 'snow'],
  ['sleet', 'sleet'],
  ['freezing', 'sleet'],
  ['hail', 'hail'],
  ['drizzle', 'drizzle'],
  ['rain', 'rain'],
  ['shower', 'rain'],
  ['fog', 'fog'],
  ['mist', 'mist'],
  ['haze', 'haze'],
  ['smoke', 'smoke'],
  ['dust', 'dust'],
  ['overcast', 'overcast'],
  ['partly', 'partly-cloudy'],
  ['mainly', 'partly-cloudy'],
  ['mostly sunny', 'clear'],
  ['mostly clear', 'clear'],
  ['cloud', 'cloudy'],
  ['clear', 'clear'],
  ['sunny', 'clear'],
  ['wind', 'wind'],
];

function toSearchable(value) {
  return String(value ?? '').toLowerCase().replace(/[_-]/g, ' ').trim();
}

/**
 * Is this condition text a real report, or a placeholder standing in for one?
 */
export function isKnownConditionText(value) {
  const text = String(value ?? '').trim();
  if (!text) return false;
  return !NON_CONDITION_TEXTS.has(text.toLowerCase());
}

/**
 * Resolve a payload's condition_code (preferred) or free text to an icon key.
 * Returns `unknown` when nothing in the input identifies a sky state — never a
 * plausible-looking guess.
 */
export function normalizeConditionKey(...values) {
  for (const value of values) {
    if (!isKnownConditionText(value)) continue;

    const direct = toSearchable(value).replace(/\s+/g, '-');
    if (CONDITION_KEYS.has(direct)) return direct;

    const searchable = toSearchable(value);
    const match = CONDITION_PATTERNS.find(([needle]) => searchable.includes(needle));
    if (match) return match[1];
  }
  return UNKNOWN_CONDITION;
}

/**
 * Text to show beside the icon. Falls back to a humanised condition_code (that
 * is still the payload's own data) and finally to an explicit unavailable
 * label, so the words never claim more than the payload does.
 */
export function describeCondition(conditionText, conditionCode, { short = false } = {}) {
  if (isKnownConditionText(conditionText)) return String(conditionText).trim();

  const key = normalizeConditionKey(conditionCode);
  if (key !== UNKNOWN_CONDITION) {
    const words = key.replace(/-/g, ' ');
    return words.charAt(0).toUpperCase() + words.slice(1);
  }
  return short ? CONDITION_UNAVAILABLE_SHORT_TEXT : CONDITION_UNAVAILABLE_TEXT;
}
