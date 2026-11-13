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
// is left after that is only ever read, so there is nothing left to pace. A long
// answer is spoken only up to LONG_ANSWER_CHARS (400) and then goes on the card.
export const SPOKEN_BUDGET_CHARS = 400;
export const PACE_TICK_MS = 50;
// A turn that never reports its end must not hold the line forever.
export const LIVE_REPLY_IDLE_MS = 60000;
// A spoken reply's text waits until her voice starts (a reply_partial with
// voice_started, sent when the first audio plays) so text and voice start
// together; build 10 showed text 1 to 2.5 s ahead of the voice. Never longer
// than this, so a reply whose voice never starts still shows.
export const VOICE_WAIT_MS = 3000;

// The end of the word that holds character `position`.
export function wordEnd(text, position) {
  if (position >= text.length) return text.length;
  const next = text.indexOf(' ', position);
  return next === -1 ? text.length : next;
}

function freshTurn() {
  return {
    turn: null, target: '', spoken: false, done: false, budget: 0, lastActivity: 0, voiceStarted: false, firstTextAt: 0,
  };
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
  // Text is here but held for the voice: the stage shows "working", not the reply.
  const [replyPending, setReplyPending] = useState(false);
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
    setReplyPending(false);
  }, [stop]);

  const tick = useCallback(() => {
    const current = state.current;
    if (current.spoken && !current.voiceStarted && current.target) {
      if (!current.firstTextAt) current.firstTextAt = Date.now();
      if (Date.now() - current.firstTextAt < VOICE_WAIT_MS) {
        setReplyPending(true);
        return;
      }
      current.voiceStarted = true;
    }
    setReplyPending(false);
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
  const onPartial = useCallback(({ turn, text, spoken, voice_started: voiceStarted }) => {
    const current = state.current;
    if (!turn) return;
    if (voiceStarted) {
      // Her voice is audible: reveal from here, at her pace.
      if (current.turn === turn) {
        current.voiceStarted = true;
        start();
      }
      return;
    }
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

  return { liveReply, replyPending, onPartial, onFinal };
}
