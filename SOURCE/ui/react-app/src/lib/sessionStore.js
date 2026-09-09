/**
 * sessionStore - in-memory GoTrue session storage (SEC-017 hardening).
 *
 * Why this exists: GoTrue access AND refresh tokens were persisted in
 * localStorage. localStorage is readable by any JavaScript running in the
 * origin, so a single successful XSS reads the refresh token off disk and
 * mints a persistent account takeover via `grant_type=refresh_token` - the
 * max-impact payoff of any XSS that lands on the web surface.
 *
 * The fix is to keep tokens in JS memory only - never written to a storage
 * medium an XSS payload can scrape (localStorage / sessionStorage). The
 * tradeoff: a full page reload no longer restores the session (memory is
 * cleared), so the user re-authenticates after a hard reload / tab close. That
 * is the deliberate "in memory" option from the sweep fix plan; the
 * httpOnly-cookie alternative needs server-side cookie minting from the GoTrue
 * proxy, a separate cross-service change.
 *
 * This module implements the auth-js SupportedStorage contract
 * (getItem/setItem/removeItem) so it can be passed as the GoTrueClient
 * `storage` option, and exposes the same shape for the custom AuthProvider.
 */

/** @type {Map<string, string>} */
const memory = new Map();

/**
 * Storage adapter conforming to auth-js SupportedStorage. Synchronous; the
 * library promisifies as needed. Lives only in this tab's JS heap.
 */
export const inMemorySessionStorage = {
  getItem(key) {
    return memory.has(key) ? memory.get(key) : null;
  },
  setItem(key, value) {
    memory.set(key, String(value));
  },
  removeItem(key) {
    memory.delete(key);
  },
};

/**
 * Best-effort one-time sweep of any GoTrue/session tokens a PRIOR build left in
 * localStorage, so upgrading users don't keep a stale refresh token sitting on
 * disk after we stopped writing there. Safe to call on module load.
 *
 * @param {string[]} keys - storage keys previously used for sessions.
 */
export function purgeLegacyLocalStorageSessions(keys) {
  if (typeof localStorage === 'undefined') return;
  for (const key of keys) {
    try {
      localStorage.removeItem(key);
    } catch {
      /* private browsing / quota — nothing to clean up */
    }
  }
}
