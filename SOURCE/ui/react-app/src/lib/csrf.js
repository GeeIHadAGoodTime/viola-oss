/**
 * Double-submit CSRF token helper for the cloud browser SPA.
 *
 * The cloud backend (`auth/csrf.py`) protects cookie-authenticated
 * state-changing requests with the standard double-submit-cookie pattern:
 *
 *   - the server mints a JS-READABLE `viola_csrf` cookie on GET responses (and
 *     alongside any response that issues a `viola_session` cookie),
 *   - the client must echo that exact value back in the `X-CSRF-Token` header
 *     on every state-changing request that relies on cookies for authority.
 *
 * `auth/csrf.py`'s `CSRFMiddleware` only ENFORCES the pairing when the request
 * carries a `viola_session` cookie and no `Authorization: Bearer` — because
 * that is the only case where a cookie could supply ambient authority.
 *
 * That enforcement predicate is exactly why the header cannot be optional here
 * (#362 / #3547). A first-ever visitor has no `viola_session` cookie, so their
 * sign-in POST skips the check entirely and succeeds even with no header. The
 * moment they sign in, the GoTrue proxy plants `viola_session`
 * (`auth/gotrue_proxy.py` `_attach_compat_session_cookies`), so every
 * SUBSEQUENT auth POST from that browser — the returning visitor's sign-in, and
 * the reload-time `grant_type=refresh_token` hydration — does hit the check and
 * hard-fails 403 without this header. The failure is permanent and
 * unrecoverable in-page, because the stale cookie is what arms it.
 *
 * SAME-ORIGIN ONLY, deliberately. A custom request header on a same-origin
 * fetch never triggers a CORS preflight, so attaching it there is free. A
 * cross-origin fetch WOULD preflight, and the desktop server's CORS allowlist
 * (`ui/server.py`) does not include `X-CSRF-Token` — attaching it on the
 * desktop LAN-spoke path would turn a working request into a CORS rejection.
 * The cookie is only useful same-origin anyway, so scoping the header to
 * same-origin costs nothing and removes that regression surface.
 */

/** Name of the JS-readable double-submit cookie minted by `auth/csrf.py`. */
export const CSRF_COOKIE_NAME = 'viola_csrf';

/** Header the server compares the cookie against (`auth/csrf.py`). */
export const CSRF_HEADER_NAME = 'X-CSRF-Token';

/**
 * Read the current `viola_csrf` cookie value.
 *
 * Read fresh at request time rather than cached at module load: the server may
 * re-mint the cookie on any response, and a stale in-memory copy would fail the
 * comparison exactly like sending nothing.
 *
 * @returns {string} the cookie value, or '' when absent/unreadable.
 */
export function readCsrfToken() {
  if (typeof document === 'undefined' || typeof document.cookie !== 'string') {
    return '';
  }
  const match = document.cookie.match(/(?:^|;\s*)viola_csrf=([^;]*)/);
  if (!match) return '';
  try {
    return decodeURIComponent(match[1]);
  } catch {
    // A cookie value that isn't valid percent-encoding is still the literal
    // token the server set — compare it as-is rather than dropping the header.
    return match[1];
  }
}

/**
 * True when `url` resolves to the same origin as the current document, so a
 * custom header can be attached without provoking a CORS preflight.
 *
 * Fails CLOSED (returns false) when the origin cannot be determined, so an
 * unparseable URL never silently gains a preflight-triggering header.
 *
 * @param {string} url - absolute or relative request URL.
 * @returns {boolean}
 */
export function isSameOriginUrl(url) {
  if (typeof window === 'undefined' || !window.location) return false;
  const pageOrigin = window.location.origin;
  if (!pageOrigin) return false;
  try {
    return new URL(String(url), pageOrigin).origin === pageOrigin;
  } catch {
    return false;
  }
}

/**
 * Return a copy of `headers` with `X-CSRF-Token` attached when the double-submit
 * cookie is readable and `url` is same-origin.
 *
 * Never overwrites a header the caller set explicitly, and never adds an empty
 * header — an empty value fails the server's `compare_digest` identically to a
 * missing one while making the failure harder to read.
 *
 * @param {Record<string,string>} headers - headers to augment (not mutated).
 * @param {string} url - the request URL the headers are for.
 * @returns {Record<string,string>}
 */
export function withCsrfHeader(headers, url) {
  const next = { ...(headers || {}) };
  const alreadySet = Object.keys(next).some(
    (name) => name.toLowerCase() === CSRF_HEADER_NAME.toLowerCase(),
  );
  if (alreadySet || !isSameOriginUrl(url)) return next;
  const token = readCsrfToken();
  if (!token) return next;
  next[CSRF_HEADER_NAME] = token;
  return next;
}
