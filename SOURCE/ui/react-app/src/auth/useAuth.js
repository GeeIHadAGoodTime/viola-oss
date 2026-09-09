/**
 * useAuth — React hook for Viola Cloud GoTrue session auth.
 *
 * Must be called inside an `<AuthProvider>` (see ./AuthProvider.jsx).
 *
 * Returns the auth context value:
 *
 *   {
 *     status: 'loading' | 'signedOut' | 'signedIn',
 *     user,                  // GoTrue user object, or null
 *     session,               // { access_token, refresh_token, expires_at, ... } or null
 *     mfaPending,            // true when a password sign-in owes a TOTP second factor
 *     mfaFactorId,           // the verified TOTP factor id to challenge, or null
 *     signIn(email, password)            -> Promise<{ ok, mfaRequired?, factorId?, error }>
 *     verifyMfaTotp(code)                -> Promise<{ ok, error }>
 *     cancelMfa()                        -> Promise<{ ok, error }>
 *     signUp(email, password, consents)  -> Promise<{ ok, needsEmailVerification, error }>
 *     signOut()                          -> Promise<{ ok, error }>
 *     resetPassword(email)               -> Promise<{ ok, error }>
 *     resendVerification(email)          -> Promise<{ ok, error }>
 *   }
 *
 * `error` (when present) is a normalised `{ message, code, status, retryAfter }`
 * object whose `message` is safe to render to users.
 */

import { useContext } from 'react';
import { AuthContext } from './AuthProvider';

/**
 * @typedef {object} AuthClientError
 * @property {string} message - user-safe error message.
 * @property {string} code - GoTrue / proxy error code.
 * @property {number} status - HTTP status (0 for transport failures).
 * @property {number|null} retryAfter - seconds to wait, when rate-limited.
 */

/**
 * @typedef {object} AuthContextValue
 * @property {'loading'|'signedOut'|'signedIn'} status
 * @property {object|null} user
 * @property {object|null} session
 * @property {boolean} mfaPending
 * @property {string|null} mfaFactorId
 * @property {(email: string, password: string) => Promise<{ ok: boolean, mfaRequired?: boolean, factorId?: string, error: AuthClientError|null }>} signIn
 * @property {(code: string) => Promise<{ ok: boolean, error: AuthClientError|null }>} verifyMfaTotp
 * @property {() => Promise<{ ok: boolean, error: AuthClientError|null }>} cancelMfa
 * @property {(email: string, password: string, consents?: { tosAccepted?: boolean, legalEligibilityConfirmed?: boolean, termsVersion?: string, privacyVersion?: string }) => Promise<{ ok: boolean, needsEmailVerification: boolean, error: AuthClientError|null }>} signUp
 * @property {() => Promise<{ ok: boolean, error: AuthClientError|null }>} signOut
 * @property {(email: string) => Promise<{ ok: boolean, error: AuthClientError|null }>} resetPassword
 * @property {(email: string) => Promise<{ ok: boolean, error: AuthClientError|null }>} resendVerification
 */

/**
 * Access the Viola Cloud auth context.
 * @returns {AuthContextValue}
 */
export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth() must be used within an <AuthProvider>');
  }
  return ctx;
}

export default useAuth;
