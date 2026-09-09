import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { toUserMessage } from './userFacingError';

describe('toUserMessage (issue #768)', () => {
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('replaces a raw HTTP-status message with the fallback', () => {
    const err = new Error('Call history request failed: 404');
    expect(toUserMessage(err, 'Could not load your call history.')).toBe('Could not load your call history.');
  });

  it('replaces stack/exception jargon with the fallback', () => {
    expect(toUserMessage(new Error('Traceback (most recent call last)'), 'Something went wrong.')).toBe(
      'Something went wrong.',
    );
    expect(toUserMessage(new Error('HTTP 500'), 'Something went wrong.')).toBe('Something went wrong.');
  });

  it('keeps an already user-safe message', () => {
    const msg = 'Could not load your call history. Please try again.';
    expect(toUserMessage(new Error(msg), 'fallback')).toBe(msg);
  });

  it('uses the fallback for empty or non-error inputs', () => {
    expect(toUserMessage(null, 'fallback')).toBe('fallback');
    expect(toUserMessage(new Error('   '), 'fallback')).toBe('fallback');
  });

  it('always logs the raw error for diagnostics', () => {
    const err = new Error('HTTP 503');
    toUserMessage(err, 'fallback');
    expect(console.error).toHaveBeenCalledWith('[viola] user-facing error:', err);
  });
});
