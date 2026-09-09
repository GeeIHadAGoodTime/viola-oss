import { describe, it, expect, vi, afterEach } from 'vitest';
import { isSafeNavigationUrl, openSafeExternalUrl } from './safeUrl';

describe('isSafeNavigationUrl', () => {
  it('accepts http(s) and benign user-intent schemes', () => {
    expect(isSafeNavigationUrl('https://useviola.com/login')).toBe(true);
    expect(isSafeNavigationUrl('http://192.168.1.10:8756')).toBe(true);
    expect(isSafeNavigationUrl('mailto:a@b.com')).toBe(true);
    expect(isSafeNavigationUrl('tel:+15551234567')).toBe(true);
  });

  it('accepts relative URLs (no scheme, resolve to current origin)', () => {
    expect(isSafeNavigationUrl('/login')).toBe(true);
    expect(isSafeNavigationUrl('foo/bar')).toBe(true);
    expect(isSafeNavigationUrl('#section')).toBe(true);
  });

  it('rejects script-capable / active schemes (the SEC-018 vector)', () => {
    expect(isSafeNavigationUrl('javascript:alert(1)')).toBe(false);
    expect(isSafeNavigationUrl('JavaScript:alert(1)')).toBe(false);
    expect(isSafeNavigationUrl('data:text/html,<script>alert(1)</script>')).toBe(false);
    expect(isSafeNavigationUrl('vbscript:msgbox(1)')).toBe(false);
    expect(isSafeNavigationUrl('blob:https://x')).toBe(false);
    expect(isSafeNavigationUrl('file:///etc/passwd')).toBe(false);
  });

  it('rejects scheme-smuggling via leading/embedded whitespace + control chars', () => {
    expect(isSafeNavigationUrl('  javascript:alert(1)')).toBe(false);
    expect(isSafeNavigationUrl('\tjavascript:alert(1)')).toBe(false);
    expect(isSafeNavigationUrl('java\tscript:alert(1)')).toBe(false);
    expect(isSafeNavigationUrl('java\nscript:alert(1)')).toBe(false);
    expect(isSafeNavigationUrl('java\rscript:alert(1)')).toBe(false);
  });

  it('rejects non-string / empty input', () => {
    expect(isSafeNavigationUrl(null)).toBe(false);
    expect(isSafeNavigationUrl(undefined)).toBe(false);
    expect(isSafeNavigationUrl('')).toBe(false);
    expect(isSafeNavigationUrl('   ')).toBe(false);
    expect(isSafeNavigationUrl(42)).toBe(false);
  });
});

describe('openSafeExternalUrl', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('opens a safe URL via window.open', () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    const attempted = openSafeExternalUrl('https://useviola.com/billing');
    expect(attempted).toBe(true);
    expect(open).toHaveBeenCalledWith('https://useviola.com/billing', '_blank', 'noopener,noreferrer');
  });

  it('never navigates to a javascript: URL, even via the location.href fallback', () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => {
      throw new Error('popup blocked');
    });
    // Guard sits before both sinks, so a blocked popup must NOT fall through to
    // location.href with a javascript: payload.
    const attempted = openSafeExternalUrl('javascript:alert(document.cookie)');
    expect(attempted).toBe(false);
    expect(open).not.toHaveBeenCalled();
  });
});
