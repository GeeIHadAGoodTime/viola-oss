/**
 * cloudSurface — detect when the React bundle is the Viola Cloud SPA.
 *
 * Viola's bundle runs on these surfaces (see utils/runtimeSurface.js):
 *
 *  1. Desktop app   — the Qt shell injects the `window.viola` bridge and a
 *     `window.__VIOLA_API_KEY__`. `isDesktopApp()` is true.
 *  2. Cloud SPA     — a plain browser on api.useviola.com loads the bundle
 *     under the `/app` mount prefix (see backend/cloud_static.py). There is
 *     NO Qt bridge and NO injected API key. The visitor must sign into a
 *     Viola Cloud account.
 *  3. Multiroom spoke — a plain browser opened with `?room=` / `?mode=speaker`
 *     / `?spoke_token=`. Routed away before the entry gate ever runs.
 *  4. Local browser — the backend declares `app_surface: desktop` in health;
 *     the entry gate validates the owner's local API key before mounting.
 *
 * The entry gate (main.jsx) must behave differently on the cloud surface:
 * instead of the desktop "Open Viola Desktop" wall, an unauthenticated
 * cloud visitor sees the account login / sign-up front door.
 *
 * A browser without a declared desktop backend keeps the cloud account gate.
 * Deployment metadata selects the UI; backend authorization still enforces
 * access to every protected API.
 */

import { isDesktopApp } from '../../utils/runtimeSurface';
import { isLocalDesktopBackend } from '../../utils/backendSurface';

/**
 * True when the current location is a multiroom spoke / speaker / review
 * route. These are routed away before the auth gate and must never see the
 * cloud login screen.
 * @returns {boolean}
 */
export function isSpokeRoute() {
  if (typeof window === 'undefined') return false;
  const params = new URLSearchParams(window.location.search);
  const mode = params.get('mode');
  return (
    mode === 'speaker'
    || mode === 'review'
    || params.has('room')
    || params.has('spoke_token')
  );
}

/**
 * True when running as the Viola Cloud SPA — a plain-browser web client that
 * needs a cloud account to reach the dashboard.
 *
 * False on the desktop app, a declared local backend, and multiroom spokes.
 * @returns {boolean}
 */
export function isCloudSurface() {
  if (typeof window === 'undefined') return false;
  if (isDesktopApp()) return false;
  if (isSpokeRoute()) return false;
  if (isLocalDesktopBackend()) return false;
  return true;
}

export default { isCloudSurface, isSpokeRoute };
