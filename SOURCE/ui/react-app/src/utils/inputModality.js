/**
 * Touch input modality for the whole app.
 *
 * Touch sizing cannot key off `@media (pointer: coarse)` alone. QtWebEngine
 * (the desktop shell) answers the pointer media queries from Qt's input
 * device list, and on Windows that list always holds a mouse ("core
 * pointer"), so a Surface in tablet mode still reports `pointer: fine` and
 * `hover: hover`. Instead the root element carries `data-input="touch"` or
 * `data-input="mouse"`, and touch rules in styles/touch-hardening.css key off
 * that attribute.
 *
 * The attribute starts as "touch" when the desktop shell says the device is
 * being used as a tablet (`window.__VIOLA_TOUCH_FIRST__`, injected before page
 * scripts run) or when the browser reports a coarse primary pointer (phones,
 * tablets, Playwright touch emulation). After that it follows the last real
 * pointer: a finger or pen press switches to touch, a mouse press back to
 * mouse.
 *
 * Three touch-only behaviours ride on the same signal:
 *   - a long press on something that is not text the user can edit or has
 *     selected does not raise a context menu (in the desktop shell that menu
 *     is Back / Reload / View page source);
 *   - tapping into a text field asks the desktop shell to show the Windows
 *     touch keyboard (`window.viola.showTouchKeyboard`, when the shell has it);
 *   - once the keyboard is up (the viewport shrinks), the focused field is
 *     scrolled back into view inside its own scroll container.
 */

export const TOUCH_INPUT = 'touch';
export const MOUSE_INPUT = 'mouse';

// A press counts as "this interaction came from a finger" for this long.
const RECENT_TOUCH_MS = 1500;

const TEXT_INPUT_TYPES = new Set([
  '', 'text', 'search', 'email', 'password', 'url', 'tel', 'number',
]);

export function isTextEntry(element) {
  if (!element || element.nodeType !== 1) return false;
  if (element.isContentEditable) return true;
  const tag = element.tagName;
  if (tag === 'TEXTAREA') return !element.readOnly && !element.disabled;
  if (tag !== 'INPUT') return false;
  const type = String(element.getAttribute('type') || '').toLowerCase();
  return TEXT_INPUT_TYPES.has(type) && !element.readOnly && !element.disabled;
}

function hasSelection(win) {
  try {
    const selection = win.getSelection?.();
    return Boolean(selection && !selection.isCollapsed && String(selection).trim());
  } catch {
    return false;
  }
}

function nearestScrollContainer(element, win) {
  let node = element?.parentElement;
  while (node && node !== win.document.body && node !== win.document.documentElement) {
    const { overflowY } = win.getComputedStyle(node);
    if ((overflowY === 'auto' || overflowY === 'scroll') && node.scrollHeight > node.clientHeight + 1) {
      return node;
    }
    node = node.parentElement;
  }
  return null;
}

/**
 * Scroll `element` into the visible part of its nearest scroll container only.
 * Never uses element.scrollIntoView(): that also scrolls overflow:hidden
 * ancestors (the stage card, the page), which the user then cannot scroll
 * back.
 */
export function revealInScrollContainer(element, win = window) {
  const container = nearestScrollContainer(element, win);
  if (!container) return false;
  const box = element.getBoundingClientRect();
  const view = container.getBoundingClientRect();
  const visibleBottom = Math.min(view.bottom, win.innerHeight);
  const visibleTop = Math.max(view.top, 0);
  const margin = 16;
  let delta = 0;
  if (box.bottom > visibleBottom - margin) delta = box.bottom - (visibleBottom - margin);
  else if (box.top < visibleTop + margin) delta = box.top - (visibleTop + margin);
  if (!delta) return false;
  container.scrollTop += delta;
  return true;
}

/**
 * Install the modality tracker on `win`. Returns an uninstall function.
 * Safe to call more than once; later calls replace the earlier listeners.
 */
export function installInputModality(win = typeof window === 'undefined' ? null : window) {
  if (!win || !win.document) return () => {};
  if (typeof win.__violaInputModalityUninstall === 'function') {
    win.__violaInputModalityUninstall();
  }
  const root = win.document.documentElement;
  let lastTouchAt = 0;

  const set = (mode) => {
    if (root.dataset.input !== mode) root.dataset.input = mode;
  };

  let coarseQuery = null;
  try {
    coarseQuery = win.matchMedia ? win.matchMedia('(pointer: coarse)') : null;
  } catch {
    coarseQuery = null;
  }
  set(win.__VIOLA_TOUCH_FIRST__ === true || coarseQuery?.matches ? TOUCH_INPUT : MOUSE_INPUT);

  const recentTouch = () => Date.now() - lastTouchAt < RECENT_TOUCH_MS;

  const onPointerDown = (event) => {
    if (event.pointerType === 'touch' || event.pointerType === 'pen') {
      lastTouchAt = Date.now();
      set(TOUCH_INPUT);
    } else if (event.pointerType === 'mouse') {
      set(MOUSE_INPUT);
    }
  };

  const onCoarseChange = (event) => {
    if (event.matches) set(TOUCH_INPUT);
  };

  const onContextMenu = (event) => {
    if (root.dataset.input !== TOUCH_INPUT && !recentTouch()) return;
    if (isTextEntry(event.target) || hasSelection(win)) return;
    event.preventDefault();
  };

  let focusedByTouch = null;
  const onFocusIn = (event) => {
    if (!recentTouch() || !isTextEntry(event.target)) {
      focusedByTouch = null;
      return;
    }
    focusedByTouch = event.target;
    try {
      win.viola?.showTouchKeyboard?.();
    } catch {
      /* the shell bridge is optional */
    }
  };
  const onFocusOut = (event) => {
    if (event.target === focusedByTouch) focusedByTouch = null;
  };

  // The desktop shell shrinks the page above a docked touch keyboard; when
  // that happens, bring the field the user is typing in back into view.
  let resizeTimer = null;
  const onResize = () => {
    if (!focusedByTouch) return;
    win.clearTimeout(resizeTimer);
    resizeTimer = win.setTimeout(() => {
      if (focusedByTouch && win.document.activeElement === focusedByTouch) {
        revealInScrollContainer(focusedByTouch, win);
      }
    }, 120);
  };

  win.addEventListener('pointerdown', onPointerDown, { capture: true, passive: true });
  win.addEventListener('contextmenu', onContextMenu, { capture: true });
  win.addEventListener('focusin', onFocusIn, true);
  win.addEventListener('focusout', onFocusOut, true);
  win.addEventListener('resize', onResize);
  coarseQuery?.addEventListener?.('change', onCoarseChange);

  const uninstall = () => {
    win.removeEventListener('pointerdown', onPointerDown, { capture: true });
    win.removeEventListener('contextmenu', onContextMenu, { capture: true });
    win.removeEventListener('focusin', onFocusIn, true);
    win.removeEventListener('focusout', onFocusOut, true);
    win.removeEventListener('resize', onResize);
    coarseQuery?.removeEventListener?.('change', onCoarseChange);
    win.clearTimeout(resizeTimer);
    if (win.__violaInputModalityUninstall === uninstall) delete win.__violaInputModalityUninstall;
  };
  win.__violaInputModalityUninstall = uninstall;
  return uninstall;
}
