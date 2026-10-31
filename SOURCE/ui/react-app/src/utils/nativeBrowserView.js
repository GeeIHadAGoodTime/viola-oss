/**
 * Lifecycle rules for the desktop app's native browser view.
 *
 * On the desktop the agent's browser and the embedded sign-in page are a Qt
 * QWebEngineView laid over this React page. A native view always paints above
 * the page, so React can never draw on top of it: no z-index puts the Ctrl+K
 * palette or a dialog above it. These rules keep the view only where React's
 * browser stage has room for it, with the stage's own controls (Take over,
 * Hand back, Stop, Close / Done) above it, and close it whenever the user
 * leaves it (1.0.5 build 7, A5.2 / A8: views that could only be removed by
 * quitting Viola).
 *
 * Overlay states come from the backend `browser_overlay_state` broadcast:
 * mode 'agentic' (agent browsing, or its page reopened), 'login' (provider
 * sign-in), 'music' (legacy browser playback), or not visible.
 */

/**
 * May the native view sit on screen right now?
 *
 * Only while the browser stage is the visible stage and nothing React draws
 * (dialog, Ctrl+K palette, menu) is open above it. Otherwise the desktop
 * parks the view off screen (the agent's page keeps running).
 */
export function nativeBrowserSurfaceActive({ stageContentMode, blockingUiOpen = false } = {}) {
  return stageContentMode === 'browser' && !blockingUiOpen;
}

/**
 * What leaving the browser stage (another pill, Exit, Esc, Ctrl+K "Open
 * Music") does to the view:
 * - 'none'  no view is up;
 * - 'park'  the agent is still working in it: keep it running off screen,
 *           the globe pill brings it back;
 * - 'close' anything else (sign-in page, a finished agent's page): close it.
 */
export function browserLeaveAction({ overlayVisible, overlayMode, agentBusy } = {}) {
  if (!overlayVisible) return 'none';
  if (overlayMode === 'agentic' && agentBusy) return 'park';
  return 'close';
}

/**
 * The stage that must show for an overlay state broadcast, or null to leave
 * the stage alone. A sign-in page always gets the browser stage (it has the
 * Done button); an agent page gets it through the agent-task display rules.
 */
export function stageForOverlayState({ visible, mode } = {}) {
  if (visible && mode === 'login') return 'browser';
  return null;
}

/**
 * Should Escape (pressed while focus is in the React page) close the view?
 * The palette and dialogs handle their own Escape first.
 */
export function escapeClosesBrowser({ key, overlayVisible, stageContentMode, blockingUiOpen = false } = {}) {
  return key === 'Escape'
    && Boolean(overlayVisible)
    && stageContentMode === 'browser'
    && !blockingUiOpen;
}

/** Tell the desktop shell whether the browser stage surface is on screen. */
export function sendBrowserSurfaceActive(active) {
  if (typeof window === 'undefined') return false;
  const bridge = window.viola;
  if (!bridge || typeof bridge.setBrowserSurfaceActive !== 'function') return false;
  bridge.setBrowserSurfaceActive(Boolean(active));
  return true;
}

/** Ask the desktop shell to close the native view (through its controller). */
export function requestNativeBrowserClose() {
  if (typeof window === 'undefined') return false;
  const bridge = window.viola;
  if (!bridge || typeof bridge.hideBrowserOverlay !== 'function') return false;
  bridge.hideBrowserOverlay();
  return true;
}
