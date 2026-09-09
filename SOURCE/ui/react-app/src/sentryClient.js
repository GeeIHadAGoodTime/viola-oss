import * as Sentry from '@sentry/react';

const MAX_STRING_LENGTH = 240;
const MAX_OBJECT_DEPTH = 4;
const MAX_ARRAY_ITEMS = 12;
const SENSITIVE_KEY_PATTERN = /(token|secret|password|authorization|cookie|api[_-]?key|refresh|access[_-]?token|debug[_-]?auth)/i;

export const SENTRY_RELEASE = import.meta.env.VITE_SENTRY_RELEASE || 'viola-react@dev';

let sentryInitialized = false;
let replayIntegrationAdded = false;

// The SDK refuses to initialize without a DSN, and for 62 days that single
// fact was the whole outage: the DSN came from an off-VCS .env, so a build that
// missed it produced a UI that captured nothing and said nothing. Now that
// `beforeSend` drops every event into the same-origin desktop route (see
// below), the DSN is a transport artifact that is NEVER contacted -- no request
// is ever made to this host -- so it can be a committed constant instead of a
// build-environment dependency. That removes the failure mode entirely: a
// missing env var can no longer disarm error capture.
//
// It names our own API rather than an ingest vendor because that is where a
// report genuinely ends up (browser -> desktop -> relay -> cloud -> GlitchTip),
// and because that origin is already in the desktop CSP, so even a future
// regression that re-enabled the SDK transport would be visible rather than
// silently blocked.
export const INERT_LOCAL_DSN = 'https://violadesktopuierrors0000000@api.useviola.com/1';

function sentryDsn() {
  if (typeof window !== 'undefined' && window.__VIOLA_SENTRY__?.dsn) {
    return String(window.__VIOLA_SENTRY__.dsn);
  }
  return import.meta.env.VITE_SENTRY_DSN || INERT_LOCAL_DSN;
}

function sentryEnvironment() {
  if (typeof window !== 'undefined' && window.__VIOLA_SENTRY__?.environment) {
    return String(window.__VIOLA_SENTRY__.environment);
  }
  return import.meta.env.VITE_SENTRY_ENVIRONMENT || (import.meta.env.PROD ? 'production' : 'development');
}

export function isSentryConfigured() {
  return Boolean(sentryDsn());
}

export function isSentryReady() {
  return sentryInitialized && Boolean(Sentry.isInitialized?.());
}

function truncate(value) {
  if (typeof value !== 'string') return value;
  return value.length > MAX_STRING_LENGTH ? `${value.slice(0, MAX_STRING_LENGTH)}...` : value;
}

function redactUrl(value) {
  if (typeof value !== 'string' || !value) return value;
  try {
    const url = new URL(value, typeof window !== 'undefined' ? window.location.origin : undefined);
    url.username = '';
    url.password = '';
    url.search = '';
    url.hash = '';
    return truncate(url.toString());
  } catch {
    return truncate(value.split('?')[0].split('#')[0]);
  }
}

export function sanitizeSentryContext(value, depth = 0) {
  if (value == null) return value;
  if (depth > MAX_OBJECT_DEPTH) return '[truncated]';
  if (typeof value === 'string') return truncate(value);
  if (typeof value === 'number' || typeof value === 'boolean') return value;
  if (Array.isArray(value)) {
    return value.slice(0, MAX_ARRAY_ITEMS).map((item) => sanitizeSentryContext(item, depth + 1));
  }
  if (typeof value !== 'object') return String(value);

  return Object.fromEntries(
    Object.entries(value).map(([key, nested]) => {
      if (SENSITIVE_KEY_PATTERN.test(key)) return [key, '[redacted]'];
      if (/url/i.test(key)) return [key, redactUrl(nested)];
      return [key, sanitizeSentryContext(nested, depth + 1)];
    })
  );
}

function sanitizeEvent(event) {
  if (event.request?.url) {
    event.request.url = redactUrl(event.request.url);
  }
  if (event.request?.headers) {
    event.request.headers = sanitizeSentryContext(event.request.headers);
  }
  return event;
}

// ---------------------------------------------------------------------------
// Where a desktop UI error actually goes (2026-08-08).
//
// It does NOT go to an ingest host. For 62 days it tried to, and the desktop's
// own CSP refused every request: `connect-src` in ui/security/config.py has
// never contained an ingest origin, so the browser blocked the POST and the
// SDK's failure was invisible from inside the page. Proven live by throwing a
// real TypeError in the running app and reading the securitypolicyviolation
// event (violatedDirective: connect-src, disposition: enforce).
//
// So the SDK keeps doing what it is good at -- catching the error, normalizing
// window.onerror / unhandledrejection / ErrorBoundary, and parsing the stack
// into structured frames -- and `beforeSend` takes the event, forwards a small
// explicit payload to the desktop's OWN origin, and returns null so the SDK
// never contacts a remote host at all. `connect-src 'self'` already permits
// that and a CSP edit cannot silently drop it, so the transport can no longer
// be the thing that fails quietly.
//
// The desktop rebuilds this payload server-side through the allowlist sanitizer
// (diagnostics/diagnostic_minimum.py) and applies the consent gates there, so
// nothing here is trusted and nothing here decides what may leave the machine.
// We still send the narrow shape rather than the whole Sentry event, because
// the event carries breadcrumbs and request context we have no reason to move.
// ---------------------------------------------------------------------------

export const UI_ERROR_REPORT_PATH = '/v1/diagnostics/ui-error';

const MAX_REPORTED_FRAMES = 30;

export function buildUiErrorReport(event) {
  const exception = event?.exception?.values?.[event.exception.values.length - 1] || {};
  const rawFrames = exception.stacktrace?.frames || [];
  const frames = rawFrames.slice(-MAX_REPORTED_FRAMES).map((frame) => ({
    // The server reduces filename to an asset basename; send the URL it needs
    // to do that and nothing else. No colno, no source context, no vars.
    filename: typeof frame.filename === 'string' ? frame.filename : '',
    function: typeof frame.function === 'string' ? frame.function : '',
    lineno: typeof frame.lineno === 'number' ? frame.lineno : null,
  }));

  return {
    error_type: truncate(String(exception.type || event?.level || 'Error')),
    error_value: truncate(String(exception.value || event?.message || '')),
    frames,
    app_state: sanitizeSentryContext(event?.contexts?.viola_app_state || {}),
  };
}

function postUiErrorReport(report) {
  try {
    const body = JSON.stringify(report);
    // keepalive so a report survives the navigation/reload that a fatal render
    // error often triggers; fetch is used rather than sendBeacon because the
    // route answers JSON we surface in tests.
    fetch(UI_ERROR_REPORT_PATH, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
      keepalive: true,
    }).catch(() => {
      /* reporting must never throw into the page it is reporting about */
    });
  } catch {
    /* same */
  }
}

export function initSentry() {
  if (sentryInitialized || !isSentryConfigured()) {
    return sentryInitialized;
  }

  Sentry.init({
    dsn: sentryDsn(),
    environment: sentryEnvironment(),
    release: SENTRY_RELEASE,
    sendDefaultPii: false,
    tracesSampleRate: 0,
    replaysSessionSampleRate: 0,
    replaysOnErrorSampleRate: 0,
    // Forward to the desktop, then DROP. Returning null stops the SDK before
    // its transport, so no request is ever made to the DSN host.
    beforeSend: (event) => {
      try {
        postUiErrorReport(buildUiErrorReport(sanitizeEvent(event)));
      } catch {
        /* a reporter must not become the error */
      }
      return null;
    },
    // Session ("release health") envelopes go to the ingest host on their own
    // schedule, independently of beforeSend -- they were half of the blocked
    // traffic observed on 2026-08-08. Drop the integration so the SDK has no
    // remaining reason to touch the network at all.
    // Only the capture integrations remain. The feedback widget is gone (see
    // openSentryUserFeedback), and with it the last thing in this SDK that
    // would have opened a socket to a host the desktop cannot reach.
    integrations: (defaults) => defaults.filter((integration) => !/session/i.test(integration?.name || '')),
  });

  sentryInitialized = true;
  Sentry.setTag('viola.release', SENTRY_RELEASE);
  // The server allowlists app_state against APP_STATE_FIELD_SPEC, so anything
  // put here is filtered on arrival; surface is the one field we always know.
  Sentry.setContext('viola_app_state', { surface: 'desktop_react' });
  return true;
}

export function syncSentryUser(user) {
  if (!isSentryReady()) return;
  const userId = user?.id || user?.sub || user?.user_id || null;
  Sentry.setUser(userId ? { id: String(userId) } : null);
}

// Session replay uploads its own recording envelopes straight to the ingest
// host, on a path that `beforeSend` does not touch. On the desktop that host is
// unreachable by CSP, so turning this on produced blocked requests and never a
// recording -- a consent toggle that promised something it could not deliver.
// It stays off here until replay has a same-origin destination of its own; the
// consent flag is still honoured and still tagged, so the user-facing setting
// keeps meaning what it says (nothing is being recorded).
export function enableSessionReplay() {
  if (!isSentryReady() || replayIntegrationAdded) return false;
  return false;
}

export function syncSentrySettings(settings) {
  if (!isSentryReady()) return;
  const errorReporting = Boolean(settings?.consent_error_reporting);
  const replayConsent = Boolean(settings?.consent_session_replay);
  Sentry.setTag('viola.error_reporting_consent', errorReporting ? 'enabled' : 'disabled');
  Sentry.setTag('viola.session_replay_consent', replayConsent ? 'enabled' : 'disabled');
  if (replayConsent) {
    enableSessionReplay();
  }
}

function sanitizeRecentAction(recentAction) {
  if (!recentAction || typeof recentAction !== 'object') {
    return { kind: 'unknown' };
  }
  const kind = String(recentAction.kind || 'unknown');
  if (kind === 'agent_task') {
    return {
      kind,
      phase: sanitizeSentryContext(recentAction.phase),
      status: sanitizeSentryContext(recentAction.status),
    };
  }
  if (kind === 'browser_stage') {
    return {
      kind,
      url: redactUrl(recentAction.url),
    };
  }
  if (kind === 'playback') {
    return {
      kind,
      provider: sanitizeSentryContext(recentAction.provider),
      is_playing: Boolean(recentAction.is_playing),
    };
  }
  if (kind === 'phone_call') {
    return { kind };
  }
  return {
    kind,
    display_mode: sanitizeSentryContext(recentAction.display_mode),
    stage_mode: sanitizeSentryContext(recentAction.stage_mode),
  };
}

// The context shape attached to a user-initiated bug report. It survives the
// feedback widget's removal because it is the sanitizer contract for that
// context, and `sanitizeRecentAction` below is where "what the user was doing"
// gets reduced to something safe to send.
export function buildSentryFeedbackContext(context = {}) {
  return {
    source: sanitizeSentryContext(context.source || 'react_topbar'),
    surface: sanitizeSentryContext(context.surface || 'react_ui'),
    ui_entrypoint: sanitizeSentryContext(context.ui_entrypoint || 'react_topbar'),
    current_url: redactUrl(context.current_url || ''),
    display_mode: sanitizeSentryContext(context.display_mode),
    stage_mode: sanitizeSentryContext(context.stage_mode),
    recent_action: sanitizeRecentAction(context.recent_action),
    viewport: sanitizeSentryContext(context.viewport || {}),
    screen_capture_metadata: {
      requested: false,
      provided: false,
      storage: 'metadata_only',
    },
  };
}


/**
 * Always false: the desktop bug-report button must open Viola's own form.
 *
 * This used to open the Sentry feedback widget, and returning `true` told
 * SmartDisplay's `handleOpenBugReport` not to fall back to the native
 * `BugReportModal`. The consequence was the worst kind of silent loss, because
 * the user was not guessing that something broke -- they had deliberately sat
 * down and typed a bug report. The widget submits its own feedback envelope
 * straight to the ingest host, which the desktop CSP blocks, so the report died
 * in the browser, while `POST /v1/bug-report` (which relays to the cloud and
 * pages the founder on Telegram, and is the one path with delivery receipts in
 * `_diag/red_alerts/red_alerts.jsonl`) was skipped precisely because the broken
 * widget claimed the job.
 *
 * `beforeSend` does not save it either: a feedback envelope is not an error
 * event and does not pass through that hook. So the honest fix is to stop
 * offering the widget on this surface and let the working form take every
 * click. Kept as a function rather than deleted at the call site so the
 * decision, and the reason, live where the next person will look for them.
 */
export async function openSentryUserFeedback() {
  return false;
}

export { Sentry };
