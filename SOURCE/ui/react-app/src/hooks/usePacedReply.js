import { useCallback, useEffect, useRef, useState } from 'react';

// The stage's view of a reply while it is still being written.
//
// The model often writes a whole answer in under half a second, and Viola takes
// half a minute to say it, so showing text as it arrives puts the screen a long
// way ahead of the voice (1.0.5 build-9 report). Text is therefore revealed word
// by word at a steady rate: about speaking speed when the reply is being spoken,
// much faster when it is not. The finished reply always replaces this text.

// Kokoro at normal speed, measured on a four-sentence answer: about 18 characters
// a second including the pauses. Slightly under it, so text does not lead the voice.
export const SPOKEN_CHARS_PER_SECOND = 17;
export const SHOWN_CHARS_PER_SECOND = 120;
// Viola stops talking at her spoken budget (services/llm/answer_stream.py): what
// is left after that is only ever read, so there is nothing left to pace.
export const SPOKEN_BUDGET_CHARS = 800;
export const PACE_TICK_MS = 50;
// A turn that never reports its end must not hold the line forever.
export const LIVE_REPLY_IDLE_MS = 60000;

// The end of the word that holds character `position`.
export function wordEnd(text, position) {
  if (position >= text.length) return text.length;
  const next = text.indexOf(' ', position);
  return next === -1 ? text.length : next;
}

function freshTurn() {
  return { turn: null, target: '', spoken: false, done: false, budget: 0, lastActivity: 0 };
}

// One line of text that was still being written when it appeared: reveal it at
// speaking pace. A line that was already finished shows whole.
export function usePacedLine(text, live) {
  const value = typeof text === 'string' ? text : '';
  const paced = useRef(Boolean(live));
  const [count, setCount] = useState(0);
  const incomplete = paced.current && count < value.length;

  useEffect(() => {
    if (!incomplete) return undefined;
    const timer = setInterval(() => {
      setCount((shown) => Math.min(value.length, shown + (SPOKEN_CHARS_PER_SECOND * PACE_TICK_MS) / 1000));
    }, PACE_TICK_MS);
    return () => clearInterval(timer);
  }, [incomplete, value.length]);

  return paced.current ? value.slice(0, wordEnd(value, Math.floor(count))) : value;
}

export function usePacedReply({ speechRate = 1 } = {}) {
  const [liveReply, setLiveReply] = useState('');
  const state = useRef(freshTurn());
  const timer = useRef(null);
  const rate = useRef(speechRate);
  rate.current = speechRate;

  const stop = useCallback(() => {
    if (timer.current !== null) {
      clearInterval(timer.current);
      timer.current = null;
    }
  }, []);

  const clear = useCallback(() => {
    stop();
    state.current = freshTurn();
    setLiveReply('');
  }, [stop]);

  const tick = useCallback(() => {
    const current = state.current;
    const perSecond = current.spoken ? SPOKEN_CHARS_PER_SECOND * rate.current : SHOWN_CHARS_PER_SECOND;
    current.budget += (perSecond * PACE_TICK_MS) / 1000;
    const shownTo = current.budget >= SPOKEN_BUDGET_CHARS ? current.target.length : wordEnd(current.target, Math.floor(current.budget));
    setLiveReply(current.target.slice(0, shownTo));
    if (shownTo >= current.target.length) {
      if (current.done) {
        clear();
        return;
      }
      current.budget = Math.min(current.budget, current.target.length);
      if (Date.now() - current.lastActivity > LIVE_REPLY_IDLE_MS) clear();
    }
  }, [clear]);

  const start = useCallback(() => {
    if (timer.current === null) timer.current = setInterval(tick, PACE_TICK_MS);
  }, [tick]);

  // One `reply_partial` event: the answer so far for this turn.
  const onPartial = useCallback(({ turn, text, spoken }) => {
    const current = state.current;
    if (!turn) return;
    if (current.turn !== turn) {
      stop();
      state.current = { ...freshTurn(), turn, spoken: Boolean(spoken) };
      setLiveReply('');
    } else if (current.done) {
      return;
    }
    state.current.target = typeof text === 'string' ? text : '';
    state.current.lastActivity = Date.now();
    start();
  }, [start, stop]);

  // The finished reply is here. Spoken text goes on at the voice's pace; anything
  // else, or an answer a guard replaced, shows whole straight away.
  const onFinal = useCallback((finalText) => {
    const current = state.current;
    if (current.turn === null) return;
    const text = typeof finalText === 'string' ? finalText : '';
    const shown = current.target.slice(0, Math.floor(current.budget));
    if (!current.spoken || !text || !text.startsWith(shown)) {
      clear();
      return;
    }
    current.target = text;
    current.done = true;
    start();
  }, [clear, start]);

  useEffect(() => stop, [stop]);

  return { liveReply, onPartial, onFinal };
}
