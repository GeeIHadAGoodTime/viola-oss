/**
 * authValidation — lightweight client-side checks for the auth screens.
 *
 * These exist purely to give fast, friendly feedback before a network round
 * trip. The backend remains the source of truth for credential rules — the
 * UI never blocks on a check the server would not also enforce.
 */

// Pragmatic email shape check — not RFC 5322, just "looks like an address".
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** Minimum password length the sign-up screen enforces locally. */
export const MIN_PASSWORD_LENGTH = 12;

/**
 * @param {string} email
 * @returns {string|null} error message, or null when the email looks valid.
 */
export function validateEmail(email) {
  const trimmed = (email || '').trim();
  if (!trimmed) return 'Enter your email address.';
  if (!EMAIL_RE.test(trimmed)) return 'Enter a valid email address.';
  return null;
}

/**
 * @param {string} password
 * @returns {string|null} error message, or null when the password is present.
 */
export function validatePassword(password) {
  if (!password) return 'Enter your password.';
  return null;
}

/**
 * Stricter check used by the sign-up screen.
 * @param {string} password
 * @returns {string|null}
 */
export function validateNewPassword(password) {
  if (!password) return 'Choose a password.';
  if (password.length < MIN_PASSWORD_LENGTH) {
    return `Password must be at least ${MIN_PASSWORD_LENGTH} characters.`;
  }
  return null;
}

/**
 * Extract a user-safe message from useAuth()'s normalised error.
 *
 * The AuthProvider returns `error` as a `{ message, code, status, retryAfter }`
 * object whose `message` is already user-safe (see auth/authClient.js). This
 * helper tolerates a missing error, a bare string, or the object form, and
 * falls back to `fallback` when nothing usable is present.
 *
 * @param {{message?: string}|string|null|undefined} error
 * @param {string} fallback
 * @returns {string}
 */
export function authErrorMessage(error, fallback) {
  if (!error) return fallback;
  if (typeof error === 'string') return error || fallback;
  if (typeof error === 'object' && typeof error.message === 'string' && error.message) {
    return error.message;
  }
  return fallback;
}
