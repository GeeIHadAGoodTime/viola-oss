/**
 * safeUrl - scheme allow-list guard for any URL that reaches an active
 * navigation sink (window.open, window.location.href, a system-browser bridge).
 *
 * Why this exists (SEC-018, sweep 2026-06-09): Viola renders agent/LLM output
 * as content cards. The agent's output is influenceable by prompt injection
 * from web content and tool results it processes. A poisoned response can set a
 * card/CTA URL to "javascript:..." (or "data:" / "vbscript:"). When that string
 * reaches window.location.href - e.g. the catch fallback after a blocked
 * window.open - it executes in the page's origin = DOM-XSS on the desktop
 * WebView UI (and on useviola.com if the same bundle serves the funnel).
 *
 * The defense is a scheme allow-list AT THE SINK: only http:/https: (and a few
 * benign, non-executing schemes a user might legitimately open) pass; every
 * active/script-capable scheme (javascript:, data:, vbscript:, file:, blob:,
 * unknown custom protocols) is rejected. This is intentionally a sink guard,
 * not input parsing: it cannot be bypassed by where the URL came from.
 */

/**
 * Schemes that are safe to hand to a navigation sink. Lower-case, no colon.
 * mailto/tel are benign user-intent schemes the OS handles; they never execute
 * script in our origin.
 */
const ALLOWED_SCHEMES = new Set(['http', 'https', 'mailto', 'tel']);

// ASCII control chars (codes 0-31) and space (32). Browsers strip these from
// URLs before parsing the scheme, so "java\tscript:" is a javascript: URL to
// the browser. Strip them before scheme-testing so the same bypass can't get
// past this guard. Constructed via RegExp from a code range so the source file
// stays free of raw control bytes.
const STRIP_BEFORE_SCHEME = new RegExp(`[${String.fromCharCode(0)}-${String.fromCharCode(32)}]`, 'g');
const SCHEME_RE = /^([a-zA-Z][a-zA-Z0-9+.-]*):/;

/**
 * Return true when url is safe to pass to an active navigation sink.
 *
 * Relative URLs (no scheme) resolve against the current origin and cannot carry
 * a javascript:/data: payload, so they are allowed. Anything that parses to a
 * scheme outside the allow-list - including the script-capable javascript:,
 * data:, and vbscript: - is rejected.
 *
 * @param {unknown} url
 * @returns {boolean}
 */
export function isSafeNavigationUrl(url) {
  if (typeof url !== 'string') return false;
  const trimmed = url.trim();
  if (!trimmed) return false;

  const noControls = trimmed.replace(STRIP_BEFORE_SCHEME, '');
  const schemeMatch = SCHEME_RE.exec(noControls);

  // No scheme => relative URL (e.g. "/login", "foo/bar") - safe; resolves
  // against the page origin and cannot be a javascript: payload.
  if (!schemeMatch) return true;

  const scheme = schemeMatch[1].toLowerCase();
  return ALLOWED_SCHEMES.has(scheme);
}

/**
 * Open url in a new tab, falling back to same-tab navigation only when the URL
 * passed the scheme guard. Unsafe URLs (javascript:, data:, ...) are dropped
 * silently - Viola never executes agent-controlled active-URI payloads, and the
 * UI degrades quietly rather than throwing in the user's face.
 *
 * @param {unknown} url
 * @returns {boolean} whether a navigation was attempted
 */
export function openSafeExternalUrl(url) {
  if (!isSafeNavigationUrl(url)) return false;
  try {
    window.open(url, '_blank', 'noopener,noreferrer');
  } catch {
    // Popup blocked - fall back to a same-tab nav. Still guarded above, so the
    // location.href assignment can never receive a javascript:/data: URI.
    window.location.href = url;
  }
  return true;
}
