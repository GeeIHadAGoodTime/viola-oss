import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  browserLeaveAction,
  escapeClosesBrowser,
  nativeBrowserSurfaceActive,
  requestNativeBrowserClose,
  sendBrowserSurfaceActive,
  stageForOverlayState,
} from './nativeBrowserView';

afterEach(() => { delete window.viola; });

describe('nativeBrowserSurfaceActive', () => {
  it('only on the browser stage with nothing above it', () => {
    expect(nativeBrowserSurfaceActive({ stageContentMode: 'browser' })).toBe(true);
    expect(nativeBrowserSurfaceActive({ stageContentMode: 'browser', blockingUiOpen: true })).toBe(false);
    for (const stage of ['music', 'chat', 'phone', undefined]) {
      expect(nativeBrowserSurfaceActive({ stageContentMode: stage })).toBe(false);
    }
  });
});

describe('browserLeaveAction', () => {
  it('parks only a page the agent is still working in', () => {
    expect(browserLeaveAction({ overlayVisible: true, overlayMode: 'agentic', agentBusy: true })).toBe('park');
  });

  it('closes a finished agent page, a sign-in page and the legacy music view', () => {
    expect(browserLeaveAction({ overlayVisible: true, overlayMode: 'agentic', agentBusy: false })).toBe('close');
    expect(browserLeaveAction({ overlayVisible: true, overlayMode: 'login', agentBusy: false })).toBe('close');
    expect(browserLeaveAction({ overlayVisible: true, overlayMode: 'login', agentBusy: true })).toBe('close');
    expect(browserLeaveAction({ overlayVisible: true, overlayMode: 'music' })).toBe('close');
  });

  it('does nothing when no view is up', () => {
    expect(browserLeaveAction({ overlayVisible: false, overlayMode: 'agentic', agentBusy: true })).toBe('none');
  });
});

describe('stageForOverlayState', () => {
  it('a sign-in page takes the browser stage; other states leave the stage alone', () => {
    expect(stageForOverlayState({ visible: true, mode: 'login' })).toBe('browser');
    expect(stageForOverlayState({ visible: false, mode: 'login' })).toBe(null);
    expect(stageForOverlayState({ visible: true, mode: 'agentic' })).toBe(null);
  });
});

describe('escapeClosesBrowser', () => {
  it('Esc on an uncovered browser stage with a view up', () => {
    expect(escapeClosesBrowser({ key: 'Escape', overlayVisible: true, stageContentMode: 'browser' })).toBe(true);
    expect(escapeClosesBrowser({ key: 'Escape', overlayVisible: true, stageContentMode: 'browser', blockingUiOpen: true })).toBe(false);
    expect(escapeClosesBrowser({ key: 'Escape', overlayVisible: false, stageContentMode: 'browser' })).toBe(false);
    expect(escapeClosesBrowser({ key: 'Escape', overlayVisible: true, stageContentMode: 'music' })).toBe(false);
    expect(escapeClosesBrowser({ key: 'Enter', overlayVisible: true, stageContentMode: 'browser' })).toBe(false);
  });
});

describe('bridge calls', () => {
  it('are no-ops without the desktop bridge', () => {
    expect(sendBrowserSurfaceActive(true)).toBe(false);
    expect(requestNativeBrowserClose()).toBe(false);
  });

  it('reach the desktop bridge', () => {
    window.viola = { setBrowserSurfaceActive: vi.fn(), hideBrowserOverlay: vi.fn() };
    expect(sendBrowserSurfaceActive(1)).toBe(true);
    expect(window.viola.setBrowserSurfaceActive).toHaveBeenCalledWith(true);
    expect(requestNativeBrowserClose()).toBe(true);
    expect(window.viola.hideBrowserOverlay).toHaveBeenCalledTimes(1);
  });
});
