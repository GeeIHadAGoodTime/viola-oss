/**
 * One place that turns "something failed" into the sentence a person reads.
 *
 * Failures arrive here in every shape the stack produces: the API's envelope
 * error object (`{code, message}` from contracts/api_response.py), FastAPI's
 * validation shape (`{detail}`), a raw string, a DOMException from
 * getUserMedia, a fetch TypeError, or nothing at all. Before this module every
 * call site improvised that conversion, and improvising it is what produced
 * the two failure modes this file exists to end:
 *
 *   - putting a non-string where a string was expected, so React threw
 *     "Objects are not valid as a React child" and the user read the
 *     ErrorBoundary's "AI Response couldn't load" instead of the reason;
 *   - substituting a plausible-but-false sentence, so a 413 "file too large"
 *     or an unreachable transcriber both told the user "No speech detected"
 *     and blamed them for a bug.
 *
 * `describeError` is total: every input returns a non-empty human sentence. It
 * never returns "[object Object]", and it never shows a machine code to a
 * person.
 */

// Codes the voice path actually emits, mapped to what they mean for the user.
// Sources: ui/api/routes/transcription.py (_handle_transcription),
// ui/api/routes/voice_stream.py, and core/error_messages.py.
export const ERROR_CODE_MESSAGES = {
  no_speech_detected: "I didn't catch that. Try speaking again, a little closer to the mic.",
  transcription_failed: "I couldn't make out that recording. Try again?",
  transcriber_not_available: 'Speech recognition is still warming up. Give it a moment and try again.',
  transcription_unavailable: "Speech recognition isn't responding right now. Try again in a moment.",
  audio_too_short: "That was too short for me to hear. Hold the button while you speak.",
  command_failed: "I heard you, but that command didn't go through. Try again?",
  auth_required: 'Sign in to use voice.',
  ducking_unavailable: 'Audio controls are temporarily unavailable.',
  ducking_error: 'Audio controls hit a snag. Try again in a moment.',
  transcriber_test_failed: 'Speech recognition failed to start. Try again in a moment.',
  local_playback_failed: "I answered, but I couldn't play it out loud. Check your speaker or output device.",
  audio_output_unavailable: "I can't reach an audio output device, so you'll see my replies but not hear them.",
  audio_input_unavailable: "I can't reach a microphone, so voice input won't work until one is connected.",
};

export const DEFAULT_ERROR_MESSAGE = 'Something went wrong. Try that again?';

// A machine code looks like `snake_case_token` — no spaces, no sentence
// punctuation. Showing one to a person is never right, so an unmapped code
// falls through to the caller's fallback rather than leaking.
const MACHINE_CODE = /^[a-z0-9]+(?:[._-][a-z0-9]+)+$/i;

function isMachineCode(text) {
  return MACHINE_CODE.test(text) && !text.includes(' ');
}

function fromString(text, fallback) {
  const trimmed = text.trim();
  if (!trimmed) return fallback;
  const mapped = ERROR_CODE_MESSAGES[trimmed];
  if (mapped) return mapped;
  if (isMachineCode(trimmed)) return fallback;
  return trimmed;
}

/**
 * Convert any error value into a sentence safe to show a person.
 *
 * @param {unknown} value    Whatever the failure produced.
 * @param {string}  fallback Sentence to use when the value carries no usable
 *                           human text. Defaults to a generic retry prompt.
 * @returns {string} Always a non-empty string.
 */
export function describeError(value, fallback = DEFAULT_ERROR_MESSAGE) {
  const safeFallback = (typeof fallback === 'string' && fallback.trim()) ? fallback : DEFAULT_ERROR_MESSAGE;

  if (value === null || value === undefined) return safeFallback;

  if (typeof value === 'string') return fromString(value, safeFallback);

  // A DOMException / TypeError / any Error. `.message` is the human-ish part;
  // some (an aborted fetch) carry an empty one.
  if (value instanceof Error) {
    return value.message ? fromString(value.message, safeFallback) : safeFallback;
  }

  if (typeof value === 'object') {
    // Preferred first: a message the backend deliberately wrote for a user.
    if (typeof value.user_message === 'string' && value.user_message.trim()) {
      return value.user_message.trim();
    }
    // A known code outranks the backend's own wording, because the code is
    // what we can guarantee is truthful about which failure occurred.
    if (typeof value.code === 'string' && ERROR_CODE_MESSAGES[value.code.trim()]) {
      return ERROR_CODE_MESSAGES[value.code.trim()];
    }
    if (typeof value.message === 'string' && value.message.trim()) {
      return fromString(value.message, safeFallback);
    }
    // FastAPI's HTTPException shape, which is not the envelope: {"detail": ...}
    if (typeof value.detail === 'string' && value.detail.trim()) {
      return fromString(value.detail, safeFallback);
    }
    // An envelope handed in whole, or an error nested one level deeper.
    if (value.error !== undefined && value.error !== value) {
      return describeError(value.error, safeFallback);
    }
    if (typeof value.code === 'string' && value.code.trim()) {
      return fromString(value.code, safeFallback);
    }
    return safeFallback;
  }

  // Numbers, booleans, symbols: nothing a person should read.
  return safeFallback;
}

/**
 * What went wrong when the browser refused to give us the microphone.
 *
 * getUserMedia reports failures as a DOMException whose `.name` is the real
 * signal and whose `.message` is browser-specific technical text ("Requested
 * device not found"). Both voice hooks need this mapping; only the HTTP one
 * used to have it, so the WebSocket path (what every cloud and spoke user is
 * on) showed the raw DOMException text instead. Shared here so the two paths
 * cannot drift apart again.
 *
 * @param {unknown} err The rejection from getUserMedia.
 * @returns {string} Always a non-empty string.
 */
export function describeMicError(err) {
  const name = (err && typeof err.name === 'string') ? err.name : '';

  switch (name) {
    case 'NotAllowedError':
    case 'PermissionDeniedError':
      return 'Microphone access was denied. Click "Allow" when prompted for microphone access.';
    case 'NotFoundError':
    case 'DevicesNotFoundError':
      return 'No microphone found';
    case 'NotReadableError':
    case 'TrackStartError':
      return 'Microphone is in use by another application';
    case 'OverconstrainedError':
      return 'Your microphone is not compatible. Try a different microphone or check your audio settings.';
    case 'InvalidStateError':
      return 'Audio conflict detected. Try pausing music, then try speaking again.';
    case 'SecurityError':
      return 'The microphone is blocked on this page. Open Viola over a secure (https) address and try again.';
    case 'AbortError':
      return 'The microphone stopped responding. Try again.';
    default:
      return describeError(err, 'Microphone unavailable, check device settings');
  }
}

export default describeError;
