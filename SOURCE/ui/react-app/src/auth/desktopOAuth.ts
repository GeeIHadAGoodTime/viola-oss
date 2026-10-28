/**
 * Provider (Google) sign-in for the DESKTOP app, run to completion.
 *
 * The problem this solves
 * -----------------------
 * Google refuses OAuth in an embedded webview, so the authorization leg has
 * to happen in the system browser — a different process from the one that
 * minted the PKCE `code_verifier`. Left unmanaged, that split simply loses:
 * the verifier stays in the app and the authorization code lands in the
 * browser, and the exchange that turns one into a session can never happen.
 *
 * RFC 8252 §7.3 closes the split by making the app's own loopback listener
 * the redirect target, so the code comes home to the process holding the
 * verifier. That is what this module does. One detail differs from the
 * textbook shape and it is deliberate: GoTrue's redirect allow-list and the
 * cloud auth proxy both require an https target on a host we serve
 * (production answers 400 to a `http://127.0.0.1:...` redirect_to), so the
 * browser is sent to `https://useviola.com/desktop-auth-callback`, which
 * forwards to the loopback listener. The app is still the endpoint that
 * receives the code.
 *
 * The honesty rule
 * ----------------
 * This function resolves successfully ONLY after `exchangeCodeForSession`
 * has returned a real session. Every other outcome — the human closing the
 * browser, the provider erroring, the exchange failing, the deadline
 * passing — resolves as a failure with something true to say. The bug this
 * replaces returned `{ success: true }` the instant it opened a browser tab,
 * which is how four abandoned authorization codes from three real people
 * ended up in production GoTrue with nobody ever signed in.
 */

import type { Session } from '@supabase/auth-js';
import { gotrueClient } from '../lib/gotrue_client';

/** Single-path-segment landing page; see ViolaWebsite/desktop-auth-callback.html. */
export const DESKTOP_OAUTH_RELAY_URL = 'https://useviola.com/desktop-auth-callback';

/** Where the local server parks the browser leg's result. */
export const DESKTOP_OAUTH_RESULT_PATH = '/auth/desktop/oauth/result';

/** Matches core/constants.DEFAULT_API_PORT — only used if the page has no port. */
const DEFAULT_DESKTOP_PORT = '8756';

/**
 * How long we wait for the human to finish in the browser. Generous on
 * purpose: picking an account and clearing a consent screen on a slow machine
 * is not fast, and a premature failure here reads as a broken button. Kept
 * under the server's own flow TTL (auth/desktop_oauth_callback.FLOW_TTL_SECONDS)
 * so the server, not the client, is the thing that expires a stale code.
 */
export const OAUTH_WAIT_TIMEOUT_MS = 180_000;

/**
 * Poll cadence while the human is still in the browser: quick at first (a
 * Google account that already consented comes back in a few seconds), then
 * backing off so a long wait costs a request every few seconds, not every one.
 *
 * The old fixed 1.2 s cadence was 50 requests a minute against a 30-a-minute
 * local limiter that also counts rejected requests, so after 36 s every poll was
 * refused, the finished result could never be read, and the button sat on
 * "Please wait..." (Viola 1.0.5 laptop report, 2026-10-28).
 */
export const OAUTH_POLL_INTERVAL_MS = 1_000;
export const OAUTH_POLL_BACKOFF = 1.5;
export const OAUTH_POLL_MAX_INTERVAL_MS = 4_000;
/** After a 429 from the local server: wait at least this long before asking again. */
export const OAUTH_POLL_RATE_LIMITED_INTERVAL_MS = 8_000;
/** One poll request may not hang the loop past its deadline. */
export const OAUTH_POLL_REQUEST_TIMEOUT_MS = 5_000;
/** The code exchange is one request to the account service; it gets a bound too. */
export const OAUTH_EXCHANGE_TIMEOUT_MS = 30_000;

export type DesktopOAuthOutcome =
  | { status: 'signed_in'; session: Session }
  | { status: 'already_signed_in' }
  | { status: 'failed'; message: string; code: string };

type ResultPayload = {
  status?: string;
  code?: string;
  error?: string;
  error_description?: string;
};

/** What one poll learned. `rate_limited` and `unavailable` mean "ask again later". */
type PollRead =
  | { kind: 'result'; payload: ResultPayload }
  | { kind: 'rate_limited' }
  | { kind: 'unavailable' };

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => { setTimeout(resolve, ms); });
}

/** The port the desktop's local server is actually serving this page on. */
export function desktopServerPort(): string {
  if (typeof window === 'undefined') return DEFAULT_DESKTOP_PORT;
  const port = window.location?.port;
  return port && /^[0-9]{1,5}$/.test(port) ? port : DEFAULT_DESKTOP_PORT;
}

/** Unguessable, single-use id tying the browser leg back to this attempt. */
export function newFlowId(): string {
  const cryptoRef = (globalThis as { crypto?: Crypto }).crypto;
  if (cryptoRef?.randomUUID) return cryptoRef.randomUUID().replace(/-/g, '');
  if (cryptoRef?.getRandomValues) {
    const bytes = new Uint8Array(16);
    cryptoRef.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  }
  // Never expected in the desktop webview; still must not collide silently.
  return `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`.padEnd(32, '0').slice(0, 32);
}

/**
 * The https landing URL GoTrue is asked to redirect to. Carries the two facts
 * the relay page needs to reach this exact app instance: which loopback port
 * to hop to, and which flow the result belongs to.
 */
export function buildDesktopRedirectUrl(flowId: string, port = desktopServerPort()): string {
  const url = new URL(DESKTOP_OAUTH_RELAY_URL);
  url.searchParams.set('p', port);
  url.searchParams.set('f', flowId);
  return url.toString();
}

/**
 * Read the browser leg's result off the local server.
 *
 * The route speaks Viola's canonical `{ok, error, data}` envelope, so the
 * payload is under `data`. A bare body is tolerated too, so an envelope change
 * degrades to "keep polling" rather than to a false verdict.
 */
async function readFlowResult(flowId: string): Promise<PollRead> {
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), OAUTH_POLL_REQUEST_TIMEOUT_MS) : null;
  try {
    const response = await fetch(
      `${DESKTOP_OAUTH_RESULT_PATH}?flow=${encodeURIComponent(flowId)}`,
      { credentials: 'same-origin', ...(controller ? { signal: controller.signal } : {}) },
    );
    if (response.status === 429) return { kind: 'rate_limited' };
    if (!response.ok) return { kind: 'unavailable' };
    const body = (await response.json()) as { data?: ResultPayload } & ResultPayload;
    const payload = body?.data ?? body ?? null;
    return payload ? { kind: 'result', payload } : { kind: 'unavailable' };
  } catch {
    // A transient local fetch failure is not a verdict — keep polling until
    // the deadline rather than calling the whole sign-in dead.
    return { kind: 'unavailable' };
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

/** Resolve `work`, or `null` if it takes longer than `ms`. */
async function withTimeout<T>(work: Promise<T>, ms: number): Promise<T | null> {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const expired = new Promise<null>((resolve) => { timer = setTimeout(() => resolve(null), ms); });
  try {
    return await Promise.race([work, expired]);
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

async function safelySignedIn(isSignedIn?: () => boolean | Promise<boolean>): Promise<boolean> {
  if (!isSignedIn) return false;
  try {
    return Boolean(await isSignedIn());
  } catch {
    return false;
  }
}

const TIMEOUT_MESSAGE = 'Sign-in timed out. Finish the browser page within a few minutes, or use email and password.';

/**
 * Wait for the browser leg, then redeem the code for a real session.
 *
 * `openBrowser` is injected so the caller owns how an external URL is opened
 * (the Qt bridge, or window.open) and so this is testable without a browser.
 * `isSignedIn` lets the caller stop the wait the moment the app is signed in
 * some other way; the poll has nothing left to wait for then.
 *
 * Always settles by `OAUTH_WAIT_TIMEOUT_MS` (plus one bounded exchange), so
 * the button that started it can never stay on "Please wait..." for good.
 */
export async function completeDesktopOAuth(
  provider: string,
  openBrowser: (url: string) => void,
  options: {
    now?: () => number;
    wait?: (ms: number) => Promise<void>;
    isSignedIn?: () => boolean | Promise<boolean>;
  } = {},
): Promise<DesktopOAuthOutcome> {
  const now = options.now || (() => Date.now());
  const wait = options.wait || sleep;

  const flowId = newFlowId();
  const redirectTo = buildDesktopRedirectUrl(flowId);

  // `skipBrowserRedirect` keeps THIS webview on the app while auth-js stores
  // the code_verifier in its (in-memory) storage — the verifier we will need
  // below, and the reason the exchange has to happen here and nowhere else.
  const { data, error: startError } = await gotrueClient.signInWithOAuth({
    provider: provider as 'google' | 'apple',
    options: { redirectTo, skipBrowserRedirect: true },
  });
  if (startError) {
    return { status: 'failed', message: startError.message, code: 'oauth_start_failed' };
  }
  if (!data?.url) {
    return {
      status: 'failed',
      message: 'Viola could not start that sign-in. Try email and password instead.',
      code: 'oauth_start_failed',
    };
  }

  openBrowser(data.url);

  const deadline = now() + OAUTH_WAIT_TIMEOUT_MS;
  let interval = OAUTH_POLL_INTERVAL_MS;
  for (;;) {
    if (now() >= deadline) {
      return { status: 'failed', message: TIMEOUT_MESSAGE, code: 'oauth_timeout' };
    }
    await wait(Math.max(0, Math.min(interval, deadline - now())));
    if (await safelySignedIn(options.isSignedIn)) return { status: 'already_signed_in' };
    if (now() >= deadline) {
      return { status: 'failed', message: TIMEOUT_MESSAGE, code: 'oauth_timeout' };
    }

    const read = await readFlowResult(flowId);
    if (read.kind === 'rate_limited') {
      interval = Math.max(interval, OAUTH_POLL_RATE_LIMITED_INTERVAL_MS);
      continue;
    }
    if (read.kind === 'unavailable' || read.payload.status === 'pending') {
      interval = Math.min(OAUTH_POLL_MAX_INTERVAL_MS, Math.max(OAUTH_POLL_INTERVAL_MS, interval * OAUTH_POLL_BACKOFF));
      continue;
    }
    const result = read.payload;

    if (result.status === 'consumed') {
      // Something already redeemed this flow's code. Polling longer cannot
      // produce it again; say so and let the human retry.
      if (await safelySignedIn(options.isSignedIn)) return { status: 'already_signed_in' };
      return {
        status: 'failed',
        message: 'That sign-in was already used. Please try again.',
        code: 'oauth_consumed',
      };
    }

    if (result.status === 'ready' && result.code) {
      const exchange = await withTimeout(
        gotrueClient.exchangeCodeForSession(result.code),
        OAUTH_EXCHANGE_TIMEOUT_MS,
      );
      if (exchange === null) {
        return {
          status: 'failed',
          message: 'Viola could not reach the account service to finish that sign-in. Please try again.',
          code: 'oauth_exchange_timeout',
        };
      }
      const { data: exchanged, error: exchangeError } = exchange;
      if (exchangeError) {
        return {
          status: 'failed',
          message: exchangeError.message || 'Viola could not finish that sign-in. Please try again.',
          code: 'oauth_exchange_failed',
        };
      }
      if (!exchanged?.session) {
        return {
          status: 'failed',
          message: 'Viola could not finish that sign-in. Please try again.',
          code: 'oauth_exchange_failed',
        };
      }
      return { status: 'signed_in', session: exchanged.session };
    }

    return {
      status: 'failed',
      message: result.error_description
        || (result.error === 'access_denied'
          ? 'Sign-in was cancelled.'
          : 'Sign-in did not complete. Please try again.'),
      code: result.error || 'oauth_failed',
    };
  }
}

export default { completeDesktopOAuth, buildDesktopRedirectUrl, newFlowId, desktopServerPort };
