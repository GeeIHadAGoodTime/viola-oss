import { afterEach, describe, expect, it, vi } from 'vitest';
import { canQuitDesktopApp, isDesktopApp, isWebClient, quitDesktopApp, shouldStreamBrowserView } from './runtimeSurface';

describe('runtimeSurface', () => {
  afterEach(() => {
    delete window.viola;
  });

  it('detects the desktop app when the Qt bridge is present', () => {
    window.viola = { setBrowserOverlayBounds: () => {} };
    expect(isDesktopApp()).toBe(true);
    expect(isWebClient()).toBe(false);
  });

  it('detects a web client when no Qt bridge is injected', () => {
    expect(window.viola).toBeUndefined();
    expect(isDesktopApp()).toBe(false);
    expect(isWebClient()).toBe(true);
  });

  it('streams the browser view for cloud/LAN web clients', () => {
    // Plain browser, not a spoke — still a web client → stream frames.
    expect(shouldStreamBrowserView(false)).toBe(true);
  });

  it('streams the browser view for multiroom spokes regardless of bridge', () => {
    expect(shouldStreamBrowserView(true)).toBe(true);
    window.viola = { setBrowserOverlayBounds: () => {} };
    // A spoke always streams even if (hypothetically) a bridge exists.
    expect(shouldStreamBrowserView(true)).toBe(true);
  });

  it('does NOT stream the browser view for the desktop app', () => {
    window.viola = { setBrowserOverlayBounds: () => {} };
    // Desktop app, not a spoke → native embedded webview, no streaming.
    expect(shouldStreamBrowserView(false)).toBe(false);
  });
});

describe('quitDesktopApp', () => {
  afterEach(() => {
    delete window.viola;
  });

  it('calls the Qt bridge quitApp slot on desktop', () => {
    const quitApp = vi.fn();
    window.viola = { quitApp };
    expect(canQuitDesktopApp()).toBe(true);
    expect(quitDesktopApp()).toBe(true);
    expect(quitApp).toHaveBeenCalledTimes(1);
  });

  it('is a no-op without a bridge or before the bridge handshake', () => {
    expect(canQuitDesktopApp()).toBe(false);
    expect(quitDesktopApp()).toBe(false);
    window.viola = { __pending: true };
    expect(quitDesktopApp()).toBe(false);
  });
});
