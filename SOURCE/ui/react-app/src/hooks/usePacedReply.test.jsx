import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  PACE_TICK_MS,
  VOICE_WAIT_MS,
  SHOWN_CHARS_PER_SECOND,
  SPOKEN_CHARS_PER_SECOND,
  usePacedLine,
  usePacedReply,
  wordEnd,
} from './usePacedReply';

const FOUR_SENTENCES = 'A violin makes sound. The bow grips the string. The body moves the air. Fingers change the pitch.';

function advance(ms) {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

describe('usePacedReply', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('reveals a spoken reply word by word at speaking pace, even when it all arrived at once', () => {
    const { result } = renderHook(() => usePacedReply());
    act(() => result.current.onPartial({ turn: 't1', text: FOUR_SENTENCES, spoken: true }));
    act(() => result.current.onPartial({ turn: 't1', voice_started: true }));

    advance(1000);
    const afterOneSecond = result.current.liveReply;
    expect(afterOneSecond.length).toBeGreaterThan(SPOKEN_CHARS_PER_SECOND / 2);
    expect(afterOneSecond.length).toBeLessThan(SPOKEN_CHARS_PER_SECOND * 2);
    expect(FOUR_SENTENCES.startsWith(afterOneSecond)).toBe(true);
    // Whole words only.
    expect(wordEnd(FOUR_SENTENCES, afterOneSecond.length)).toBe(afterOneSecond.length);

    advance(1000);
    expect(result.current.liveReply.length).toBeGreaterThan(afterOneSecond.length);
  });

  it('keeps pacing a spoken reply after the final text arrives, then hands back to the line', () => {
    const { result } = renderHook(() => usePacedReply());
    act(() => result.current.onPartial({ turn: 't1', text: FOUR_SENTENCES.slice(0, 30), spoken: true }));
    act(() => result.current.onPartial({ turn: 't1', voice_started: true }));
    advance(200);
    act(() => result.current.onFinal(FOUR_SENTENCES));
    const midway = result.current.liveReply;
    expect(midway.length).toBeLessThan(FOUR_SENTENCES.length);

    advance(((FOUR_SENTENCES.length + 5) / SPOKEN_CHARS_PER_SECOND) * 1000);
    expect(result.current.liveReply).toBe('');
  });

  it('shows a reply that is not spoken much faster and stops pacing at the final text', () => {
    const { result } = renderHook(() => usePacedReply());
    act(() => result.current.onPartial({ turn: 't1', text: FOUR_SENTENCES, spoken: false }));
    advance(PACE_TICK_MS * 4);
    expect(result.current.liveReply.length).toBeGreaterThanOrEqual((SHOWN_CHARS_PER_SECOND * PACE_TICK_MS * 4) / 1000 - 1);
    act(() => result.current.onFinal(FOUR_SENTENCES));
    expect(result.current.liveReply).toBe('');
  });

  it('never keeps showing text a guard replaced', () => {
    const { result } = renderHook(() => usePacedReply());
    act(() => result.current.onPartial({ turn: 't1', text: 'I found your card number', spoken: true }));
    advance(500);
    act(() => result.current.onFinal('I never ask for card details in chat.'));
    expect(result.current.liveReply).toBe('');
  });

  it('a new turn starts over, and a late event from a finished turn changes nothing', () => {
    const { result } = renderHook(() => usePacedReply());
    act(() => result.current.onPartial({ turn: 't1', text: 'First answer here', spoken: false }));
    advance(200);
    act(() => result.current.onPartial({ turn: 't2', text: 'Second', spoken: false }));
    advance(200);
    expect(result.current.liveReply).toBe('Second');
    act(() => result.current.onFinal('Second'));
    act(() => result.current.onPartial({ turn: 't2', text: 'Second again', spoken: false }));
    advance(200);
    expect(result.current.liveReply).toBe('Second again');
  });

  it('a turn that never reports its end does not hold the line forever', () => {
    const { result } = renderHook(() => usePacedReply());
    act(() => result.current.onPartial({ turn: 't1', text: 'Stuck', spoken: false }));
    advance(61000);
    expect(result.current.liveReply).toBe('');
  });
});

describe('usePacedLine', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('reveals a line that appeared while being written', () => {
    const { result, rerender } = renderHook(({ text, live }) => usePacedLine(text, live), {
      initialProps: { text: FOUR_SENTENCES, live: true },
    });
    expect(result.current.length).toBeLessThan(5);
    advance(1000);
    expect(result.current.length).toBeLessThan(SPOKEN_CHARS_PER_SECOND * 2);
    // The final version of the same line goes on at the same pace.
    rerender({ text: FOUR_SENTENCES, live: false });
    expect(result.current.length).toBeLessThan(FOUR_SENTENCES.length);
    advance(10000);
    expect(result.current).toBe(FOUR_SENTENCES);
  });

  it('shows a finished line whole', () => {
    const { result } = renderHook(() => usePacedLine(FOUR_SENTENCES, false));
    expect(result.current).toBe(FOUR_SENTENCES);
  });

  // Build-10 laptop: text appeared 1 to 2.5 s before the voice.
  it('holds a spoken reply until her voice starts, then reveals it at her pace', () => {
    const { result } = renderHook(() => usePacedReply());
    act(() => result.current.onPartial({ turn: 't1', text: FOUR_SENTENCES, spoken: true }));
    advance(1500);
    expect(result.current.liveReply).toBe('');
    expect(result.current.replyPending).toBe(true);
    act(() => result.current.onPartial({ turn: 't1', voice_started: true }));
    advance(1000);
    expect(result.current.replyPending).toBe(false);
    expect(result.current.liveReply.length).toBeGreaterThan(SPOKEN_CHARS_PER_SECOND / 2);
    expect(result.current.liveReply.length).toBeLessThan(SPOKEN_CHARS_PER_SECOND * 2);
  });

  it('shows a spoken reply anyway when the voice never starts', () => {
    const { result } = renderHook(() => usePacedReply());
    act(() => result.current.onPartial({ turn: 't1', text: FOUR_SENTENCES, spoken: true }));
    advance(VOICE_WAIT_MS + 1000);
    expect(result.current.replyPending).toBe(false);
    expect(result.current.liveReply.length).toBeGreaterThan(0);
  });
});
