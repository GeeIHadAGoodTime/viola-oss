/**
 * describeError is the single conversion from "a failure happened" to "the
 * sentence a person reads", used by both voice hooks, the response area, and
 * the toast channel. Its contract is total: every input yields a non-empty
 * human string. These tests hold that contract, including for the shapes that
 * previously crashed the UI or lied to the user.
 */
import { describe, it, expect } from 'vitest';
import {
  describeError,
  describeMicError,
  ERROR_CODE_MESSAGES,
  DEFAULT_ERROR_MESSAGE,
} from './describeError';

describe('describeError', () => {
  it('returns the fallback for nothing at all', () => {
    expect(describeError(null)).toBe(DEFAULT_ERROR_MESSAGE);
    expect(describeError(undefined)).toBe(DEFAULT_ERROR_MESSAGE);
    expect(describeError('')).toBe(DEFAULT_ERROR_MESSAGE);
    expect(describeError('   ')).toBe(DEFAULT_ERROR_MESSAGE);
  });

  it('passes a human sentence through unchanged', () => {
    expect(describeError('No microphone found')).toBe('No microphone found');
  });

  // The whole point: a machine code is not something a person should read.
  it('translates a known machine code and never leaks an unknown one', () => {
    expect(describeError('no_speech_detected')).toBe(ERROR_CODE_MESSAGES.no_speech_detected);
    expect(describeError('some_unmapped_internal_code')).toBe(DEFAULT_ERROR_MESSAGE);
    expect(describeError('some_unmapped_internal_code', 'Voice failed.')).toBe('Voice failed.');
  });

  // The exact shape that used to crash the response area.
  it('resolves the API failure envelope', () => {
    expect(describeError({ code: 'no_speech_detected', message: 'No speech was detected in the audio.' }))
      .toBe(ERROR_CODE_MESSAGES.no_speech_detected);
    expect(describeError({ code: 'unmapped_thing', message: 'Disk quota exceeded.' }))
      .toBe('Disk quota exceeded.');
    expect(describeError({ code: 'unmapped_thing' })).toBe(DEFAULT_ERROR_MESSAGE);
  });

  it('prefers a message the backend wrote for a user', () => {
    expect(describeError({ code: 'no_speech_detected', user_message: 'Speak up a bit.' }))
      .toBe('Speak up a bit.');
  });

  it("reads FastAPI's non-envelope validation shape", () => {
    expect(describeError({ detail: 'File too large. Maximum size: 10MB' }))
      .toBe('File too large. Maximum size: 10MB');
  });

  it('unwraps an envelope handed over whole, and a nested error', () => {
    expect(describeError({ ok: false, data: null, error: { code: 'transcription_failed' } }))
      .toBe(ERROR_CODE_MESSAGES.transcription_failed);
    expect(describeError({ error: 'Something broke in words' })).toBe('Something broke in words');
  });

  it('reads an Error instance', () => {
    expect(describeError(new Error('Failed to fetch'))).toBe('Failed to fetch');
    expect(describeError(new Error(''))).toBe(DEFAULT_ERROR_MESSAGE);
  });

  it('never returns a non-string, an empty string, or an object stringification', () => {
    const inputs = [
      null, undefined, 0, 1, true, false, NaN, '', '  ',
      {}, [], [1, 2], { nested: { deeply: [1] } }, { code: 123 }, { message: 42 },
      new Error('x'), Symbol('s'), () => {}, { error: {} },
      { user_message: '' }, { message: '   ' },
    ];
    for (const input of inputs) {
      const out = describeError(input);
      expect(typeof out).toBe('string');
      expect(out.trim().length).toBeGreaterThan(0);
      expect(out).not.toContain('[object Object]');
    }
  });

  // A self-referencing object must not blow the stack on the recursion leg.
  it('survives a self-referential error object', () => {
    const looped = { code: 'unmapped' };
    looped.error = looped;
    expect(typeof describeError(looped)).toBe('string');
  });
});

describe('describeMicError', () => {
  it('explains each getUserMedia refusal in plain words', () => {
    expect(describeMicError({ name: 'NotAllowedError' })).toMatch(/denied/i);
    expect(describeMicError({ name: 'NotFoundError' })).toMatch(/no microphone/i);
    expect(describeMicError({ name: 'NotReadableError' })).toMatch(/in use/i);
    expect(describeMicError({ name: 'OverconstrainedError' })).toMatch(/not compatible/i);
    expect(describeMicError({ name: 'InvalidStateError' })).toMatch(/audio conflict/i);
    expect(describeMicError({ name: 'SecurityError' })).toMatch(/blocked/i);
  });

  it('still says something useful for an unrecognised failure', () => {
    const out = describeMicError({ name: 'BrandNewSpecError' });
    expect(typeof out).toBe('string');
    expect(out.trim().length).toBeGreaterThan(0);
  });

  it('never shows a raw machine code', () => {
    expect(describeMicError({ name: 'Whatever', message: 'device_enumeration_failed' }))
      .not.toContain('device_enumeration_failed');
  });
});
