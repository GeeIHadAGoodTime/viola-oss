import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  MOUSE_INPUT,
  TOUCH_INPUT,
  installInputModality,
  isTextEntry,
  revealInScrollContainer,
} from './inputModality';

function pointerDown(target, pointerType) {
  const event = new Event('pointerdown', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'pointerType', { value: pointerType });
  target.dispatchEvent(event);
}

function contextMenu(target) {
  const event = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
  target.dispatchEvent(event);
  return event;
}

function stubMatchMedia(coarse) {
  window.matchMedia = vi.fn().mockImplementation((query) => ({
    matches: query === '(pointer: coarse)' ? coarse : false,
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
}

describe('installInputModality', () => {
  let uninstall;
  const originalMatchMedia = window.matchMedia;

  beforeEach(() => {
    document.body.innerHTML = '';
    delete document.documentElement.dataset.input;
    delete window.__VIOLA_TOUCH_FIRST__;
    delete window.viola;
    stubMatchMedia(false);
  });

  afterEach(() => {
    uninstall?.();
    uninstall = undefined;
    window.matchMedia = originalMatchMedia;
    vi.useRealTimers();
  });

  it('starts as mouse on a plain desktop', () => {
    uninstall = installInputModality(window);
    expect(document.documentElement.dataset.input).toBe(MOUSE_INPUT);
  });

  it('starts as touch when the desktop shell reports tablet posture', () => {
    // QtWebEngine never matches (pointer: coarse) on Windows, so the shell's
    // injected flag is what makes a Surface in tablet mode start touch-sized.
    window.__VIOLA_TOUCH_FIRST__ = true;
    uninstall = installInputModality(window);
    expect(document.documentElement.dataset.input).toBe(TOUCH_INPUT);
  });

  it('starts as touch when the browser reports a coarse primary pointer', () => {
    stubMatchMedia(true);
    uninstall = installInputModality(window);
    expect(document.documentElement.dataset.input).toBe(TOUCH_INPUT);
  });

  it('follows the last real pointer: finger or pen to touch, mouse back to mouse', () => {
    uninstall = installInputModality(window);
    pointerDown(document.body, 'touch');
    expect(document.documentElement.dataset.input).toBe(TOUCH_INPUT);
    pointerDown(document.body, 'mouse');
    expect(document.documentElement.dataset.input).toBe(MOUSE_INPUT);
    pointerDown(document.body, 'pen');
    expect(document.documentElement.dataset.input).toBe(TOUCH_INPUT);
  });

  it('blocks the long-press menu on app chrome after a touch, but not in a text field', () => {
    document.body.innerHTML = '<div id="clock">8:35</div><input id="field" type="text" />';
    uninstall = installInputModality(window);
    const clock = document.getElementById('clock');
    pointerDown(clock, 'touch');
    expect(contextMenu(clock).defaultPrevented).toBe(true);

    const field = document.getElementById('field');
    pointerDown(field, 'touch');
    expect(contextMenu(field).defaultPrevented).toBe(false);
  });

  it('leaves the mouse right-click menu alone', () => {
    document.body.innerHTML = '<div id="clock">8:35</div>';
    uninstall = installInputModality(window);
    const clock = document.getElementById('clock');
    pointerDown(clock, 'mouse');
    expect(contextMenu(clock).defaultPrevented).toBe(false);
  });

  it('asks the shell for the touch keyboard when a finger focuses a text field', () => {
    document.body.innerHTML = '<input id="field" type="search" /><button id="btn">Go</button>';
    const showTouchKeyboard = vi.fn();
    window.viola = { showTouchKeyboard };
    uninstall = installInputModality(window);

    const field = document.getElementById('field');
    pointerDown(field, 'touch');
    field.focus();
    expect(showTouchKeyboard).toHaveBeenCalledTimes(1);

    field.blur();
    const button = document.getElementById('btn');
    pointerDown(button, 'touch');
    button.focus();
    expect(showTouchKeyboard).toHaveBeenCalledTimes(1);
  });

  it('does not ask for the touch keyboard on a mouse focus', () => {
    document.body.innerHTML = '<input id="field" type="text" />';
    const showTouchKeyboard = vi.fn();
    window.viola = { showTouchKeyboard };
    uninstall = installInputModality(window);
    const field = document.getElementById('field');
    pointerDown(field, 'mouse');
    field.focus();
    expect(showTouchKeyboard).not.toHaveBeenCalled();
  });

  it('a second install replaces the first instead of stacking listeners', () => {
    document.body.innerHTML = '<input id="field" type="text" />';
    const showTouchKeyboard = vi.fn();
    window.viola = { showTouchKeyboard };
    installInputModality(window);
    uninstall = installInputModality(window);
    const field = document.getElementById('field');
    pointerDown(field, 'touch');
    field.focus();
    expect(showTouchKeyboard).toHaveBeenCalledTimes(1);
  });
});

describe('isTextEntry', () => {
  it('accepts typing fields and rejects controls', () => {
    document.body.innerHTML = `
      <input id="t" type="text" /><input id="s" type="search" /><input id="n" />
      <textarea id="a"></textarea><input id="c" type="checkbox" /><input id="r" type="range" />
      <input id="ro" type="text" readonly /><button id="b">x</button>`;
    const get = (id) => document.getElementById(id);
    expect(isTextEntry(get('t'))).toBe(true);
    expect(isTextEntry(get('s'))).toBe(true);
    expect(isTextEntry(get('n'))).toBe(true);
    expect(isTextEntry(get('a'))).toBe(true);
    expect(isTextEntry(get('c'))).toBe(false);
    expect(isTextEntry(get('r'))).toBe(false);
    expect(isTextEntry(get('ro'))).toBe(false);
    expect(isTextEntry(get('b'))).toBe(false);
  });
});

describe('revealInScrollContainer', () => {
  function rect(top, height) {
    return { top, bottom: top + height, left: 0, right: 100, width: 100, height, x: 0, y: top };
  }

  it('scrolls only the nearest scroll container, never overflow:hidden ancestors', () => {
    document.body.innerHTML = `
      <div id="card" style="overflow: hidden">
        <div id="panel" style="overflow-y: auto"><input id="field" type="text" /></div>
      </div>`;
    const card = document.getElementById('card');
    const panel = document.getElementById('panel');
    const field = document.getElementById('field');
    Object.defineProperty(panel, 'scrollHeight', { value: 2000 });
    Object.defineProperty(panel, 'clientHeight', { value: 500 });
    Object.defineProperty(window, 'innerHeight', { value: 600, configurable: true });
    panel.getBoundingClientRect = () => rect(0, 500);
    field.getBoundingClientRect = () => rect(560, 44);
    panel.scrollTop = 100;
    card.scrollTop = 0;

    expect(revealInScrollContainer(field, window)).toBe(true);
    // Field bottom 604 must come to 500 - 16 margin: scroll down by 120.
    expect(panel.scrollTop).toBe(220);
    expect(card.scrollTop).toBe(0);
  });

  it('does nothing when the field has no scroll container', () => {
    document.body.innerHTML = '<div style="overflow: hidden"><input id="field" type="text" /></div>';
    expect(revealInScrollContainer(document.getElementById('field'), window)).toBe(false);
  });
});
