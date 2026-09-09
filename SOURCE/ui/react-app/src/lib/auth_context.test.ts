import { describe, expect, it } from 'vitest';
import { authRedirectUrl } from './auth_context';

describe('authRedirectUrl', () => {
  it('uses the canonical useviola.com login URL instead of the current app origin', () => {
    expect(authRedirectUrl('verify')).toBe('https://useviola.com/login?auth=verify');
    expect(authRedirectUrl('magic')).toBe('https://useviola.com/login?auth=magic');
  });

  it('returns the canonical login URL when no flow marker is provided', () => {
    expect(authRedirectUrl()).toBe('https://useviola.com/login');
  });
});
