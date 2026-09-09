/**
 * Unit tests for the double-submit CSRF helper (#362 / #3547).
 *
 * The same-origin guard here is load-bearing in the OTHER direction from the
 * bug it fixes: a custom header on a CROSS-origin fetch triggers a CORS
 * preflight, and ui/server.py's desktop CORS allowlist does not include
 * X-CSRF-Token. Dropping the guard would turn working desktop LAN-spoke
 * requests into CORS rejections, so it is pinned here rather than left as an
 * unexercised branch someone could "simplify" away.
 */

import { describe, it, expect, afterEach, vi } from 'vitest';
import { readCsrfToken, isSameOriginUrl, withCsrfHeader, CSRF_HEADER_NAME } from './csrf';

function setCookieJar(value) {
  Object.defineProperty(document, 'cookie', {
    configurable: true,
    get: () => value,
    set: () => {},
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('readCsrfToken', () => {
  it('reads the cookie when it is the only one', () => {
    setCookieJar('viola_csrf=abc123');
    expect(readCsrfToken()).toBe('abc123');
  });

  it('reads it from the middle of a jar', () => {
    setCookieJar('a=1; viola_csrf=middle; z=2');
    expect(readCsrfToken()).toBe('middle');
  });

  it('does not match a cookie whose name merely ends with the same suffix', () => {
    setCookieJar('x_viola_csrf=WRONG');
    expect(readCsrfToken()).toBe('');
  });

  it('does not match a longer name that starts with it', () => {
    setCookieJar('viola_csrf_other=WRONG');
    // The regex captures up to the next ';', so a prefix match would silently
    // return "_other=WRONG" and every request would fail compare_digest.
    expect(readCsrfToken()).not.toContain('WRONG');
  });

  it('returns empty string when absent', () => {
    setCookieJar('session=1; other=2');
    expect(readCsrfToken()).toBe('');
  });

  it('returns the literal value when it is not valid percent-encoding', () => {
    // decodeURIComponent throws on a lone '%'; dropping the header entirely
    // would turn a decode quirk into a hard 403.
    setCookieJar('viola_csrf=100%');
    expect(readCsrfToken()).toBe('100%');
  });
});

describe('isSameOriginUrl', () => {
  it('accepts an absolute URL on the page origin', () => {
    expect(isSameOriginUrl(`${window.location.origin}/auth/v1/token`)).toBe(true);
  });

  it('accepts a relative URL', () => {
    expect(isSameOriginUrl('/auth/v1/token')).toBe(true);
  });

  it('rejects a different origin', () => {
    expect(isSameOriginUrl('https://api.useviola.com/auth/v1/token')).toBe(false);
  });

  it('rejects a different port on the same host', () => {
    expect(isSameOriginUrl('http://localhost:8756/auth/v1/token')).toBe(false);
  });

  it('fails closed on an unparseable URL', () => {
    expect(isSameOriginUrl('http://[bad')).toBe(false);
  });
});

describe('withCsrfHeader', () => {
  it('attaches the header on a same-origin URL', () => {
    setCookieJar('viola_csrf=tok');
    expect(withCsrfHeader({ 'Content-Type': 'application/json' }, '/auth/v1/token'))
      .toEqual({ 'Content-Type': 'application/json', [CSRF_HEADER_NAME]: 'tok' });
  });

  it('does NOT attach on a cross-origin URL', () => {
    // Desktop LAN-spoke protection: ui/server.py's CORS allowlist omits
    // X-CSRF-Token, so adding it cross-origin fails the preflight.
    setCookieJar('viola_csrf=tok');
    const headers = withCsrfHeader({}, 'https://elsewhere.example.com/auth/v1/token');
    expect(headers[CSRF_HEADER_NAME]).toBeUndefined();
  });

  it('does not attach when no cookie is readable', () => {
    setCookieJar('unrelated=1');
    expect(withCsrfHeader({}, '/auth/v1/token')[CSRF_HEADER_NAME]).toBeUndefined();
  });

  it('never overwrites a header the caller set explicitly', () => {
    setCookieJar('viola_csrf=cookie-value');
    expect(withCsrfHeader({ 'x-csrf-token': 'caller-value' }, '/auth/v1/token'))
      .toEqual({ 'x-csrf-token': 'caller-value' });
  });

  it('does not mutate the headers object it was given', () => {
    setCookieJar('viola_csrf=tok');
    const original = { 'Content-Type': 'application/json' };
    withCsrfHeader(original, '/auth/v1/token');
    expect(original).toEqual({ 'Content-Type': 'application/json' });
  });

  it('tolerates a null headers argument', () => {
    setCookieJar('viola_csrf=tok');
    expect(withCsrfHeader(null, '/auth/v1/token')).toEqual({ [CSRF_HEADER_NAME]: 'tok' });
  });
});
