/**
 * The general "something broke" channel: backend sends a `type: 'error'` frame
 * over the main socket, usePlayerState hands it to the error callback, and
 * SmartDisplay raises a toast.
 *
 * The consumer used to require `msg.payload` and nothing else. Only ONE
 * producer in the whole backend sends that key
 * (bootstrap.factory._broadcast_startup_warning, via EventHub.broadcast), so
 * the event hub's own error frames — `{type:'error', error:{code, message}}`
 * for an unregistered action or a handler exception (ui/websocket/event_hub.py)
 * — arrived at the browser and were silently discarded. This pins every shape
 * a producer actually emits.
 */
import { describe, it, expect } from 'vitest';
import { normalizeErrorFrameForTest } from './usePlayerState';
import { ERROR_CODE_MESSAGES, DEFAULT_ERROR_MESSAGE } from '../utils/describeError';

describe('error frame normalization', () => {
  // The one shape that already worked: EventHub.broadcast('error', payload).
  it('keeps working for the hub broadcast shape', () => {
    const out = normalizeErrorFrameForTest({
      type: 'error',
      payload: { message: 'No local music folder configured.', level: 'warning' },
    });
    expect(out.user_message).toBe('No local music folder configured.');
    expect(out.level).toBe('warning');
  });

  // Previously dropped on the floor.
  it('accepts the event hub nested error object', () => {
    const out = normalizeErrorFrameForTest({
      type: 'error',
      error: { code: 'no_handler', action: 'play', message: 'No handler registered for action: play' },
    });
    expect(out.user_message).toBe('No handler registered for action: play');
    expect(out.level).toBe('error');
  });

  it('accepts a flat message frame', () => {
    const out = normalizeErrorFrameForTest({ type: 'error', message: 'Voice session error' });
    expect(out.user_message).toBe('Voice session error');
  });

  it('translates a known code into human wording', () => {
    const out = normalizeErrorFrameForTest({
      type: 'error',
      payload: { code: 'local_playback_failed', level: 'warning' },
    });
    expect(out.user_message).toBe(ERROR_CODE_MESSAGES.local_playback_failed);
    expect(out.level).toBe('warning');
  });

  it('always yields a readable sentence, never an object or a bare code', () => {
    const frames = [
      { type: 'error' },
      { type: 'error', payload: {} },
      { type: 'error', error: {} },
      { type: 'error', error: 'plain string failure' },
      { type: 'error', payload: { code: 'unmapped_internal_code' } },
      { type: 'error', payload: null },
    ];
    for (const frame of frames) {
      const out = normalizeErrorFrameForTest(frame);
      expect(typeof out.user_message).toBe('string');
      expect(out.user_message.trim().length).toBeGreaterThan(0);
      expect(out.user_message).not.toContain('[object Object]');
      expect(out.user_message).not.toContain('unmapped_internal_code');
    }
    expect(normalizeErrorFrameForTest({ type: 'error', payload: {} }).user_message)
      .toBe(DEFAULT_ERROR_MESSAGE);
  });
});
