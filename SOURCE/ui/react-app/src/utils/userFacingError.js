// Map an internal error/exception to a plain-English, user-facing message.
//
// User-facing error surfaces (toasts, banners, inline error text) must NEVER
// render a raw exception message, HTTP status text, or provider/internal jargon
// verbatim (issue #768: the Phone tab showed "Call history request failed: 404"
// as its entire content). Route caught errors through toUserMessage(), which
// logs the real error for diagnostics and returns a message safe to show a user
// — either the fallback, or the error's own message when it is already
// user-safe (it passes none of the technical-tells below).

const TECHNICAL_TELLS = [
  /^error:/i,
  /traceback/i,
  /exception/i,
  /\bstack\b/i,
  /\bCDP\b/i,
  /DevTools/i,
  /QWebEngineView/i,
  /ResponseEnvelope/i,
  /controller_attached/i,
  /\/v\d+\//i, // /v1/... route fragments
  /\bHTTP\s*\d{3}\b/i, // "HTTP 404"
  /\bstatus\s*code\b/i,
  /request failed/i, // "... request failed: 404"
  /\bundefined\b/i,
  /\[object [A-Za-z]+\]/, // stringified objects
  /\b[45]\d{2}\b/, // bare 4xx/5xx status codes
];

export function toUserMessage(err, fallback) {
  // Always log the real error so diagnostics keep the detail the user never sees.
  try {
    console.error('[viola] user-facing error:', err);
  } catch {
    /* logging must never throw */
  }

  const raw = err instanceof Error ? err.message : typeof err === 'string' ? err : '';
  const trimmed = (raw || '').trim();
  if (!trimmed) return fallback;
  return TECHNICAL_TELLS.some((pattern) => pattern.test(trimmed)) ? fallback : trimmed;
}

export default toUserMessage;
