/**
 * runtimeSurface — detect which runtime surface the React app runs on.
 *
 * Viola's React bundle is served in three situations:
 *
 *  1. Desktop app  — the Qt shell loads the bundle inside an embedded
 *     webview and injects the `window.viola` bridge object (native webview
 *     control, `openExternalUrl`, `setBrowserOverlayBounds`, etc.).
 *  2. LAN / cloud web client — a normal browser (Chrome on useviola.com,
 *     or a phone on the LAN) loads the bundle directly. There is NO Qt
 *     bridge, so a native embedded webview is impossible.
 *  3. Multiroom spoke — a normal browser opened with `?room=` / `?mode=speaker`.
 *     Also has no Qt bridge. Handled explicitly via the `isSpoke` prop.
 *
 * The desktop app is the ONLY surface that can host a native embedded
 * browser webview. Every other surface ("web client") must render the
 * agent's browser as streamed JPEG frames — the same path multiroom
 * spokes already use.
 *
 * Detection signal: presence of the `window.viola` Qt bridge. This is the
 * de-facto signal the codebase already keys off (see BrowserMode's
 * `sendBrowserBoundsFor`, useAuth's `openExternalUrl`, useBrowserAuth's
 * `hideBrowserOverlay`). A web client never has it.
 */

/**
 * True when running inside the Qt desktop shell (native webview available).
 * @returns {boolean}
 */
export function isDesktopApp() {
  if (typeof window === 'undefined') return false;
  return Boolean(window.viola);
}

/**
 * True when running as a plain-browser web client — cloud (useviola.com)
 * or LAN — where no native embedded webview exists. The opposite of
 * `isDesktopApp()`.
 *
 * A web client must render cloud Viola's agentic-browser view as streamed
 * frames (the spoke streaming path) instead of positioning a native webview.
 * @returns {boolean}
 */
export function isWebClient() {
  return !isDesktopApp();
}

/**
 * Whether the Stage's Browser tab should render the agent's browser as
 * streamed JPEG frames (and relay mouse/keyboard back over the WebSocket)
 * rather than positioning a native embedded webview.
 *
 * True for multiroom spokes AND for cloud/LAN web clients — both lack the
 * Qt bridge. False only for the desktop app.
 *
 * @param {boolean} [isSpoke=false] - whether this client is a multiroom spoke.
 * @returns {boolean}
 */
export function shouldStreamBrowserView(isSpoke = false) {
  return Boolean(isSpoke) || isWebClient();
}

export default {
  isDesktopApp,
  isWebClient,
  shouldStreamBrowserView,
};
