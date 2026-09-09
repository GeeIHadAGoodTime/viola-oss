import { useCallback, useEffect, useRef, useState } from 'react';

// How long after the last drag tick the slider keeps rendering the user's own
// value and ignores whatever the server says. Has to comfortably outlast the
// commit delay plus a round trip, otherwise the very response our own write
// provoked can land inside the drag and re-pin the thumb. Matches the settle
// window components/settings/SpokeRow.jsx already uses for per-speaker volume.
export const DEFAULT_SETTLE_MS = 2500;

// Trailing debounce on the outbound write: a drag becomes one request that
// carries the value the user let go on, instead of one request per input tick.
export const DEFAULT_COMMIT_DELAY_MS = 50;

/**
 * Binds a slider to a value the SERVER owns, without letting the server yank
 * the thumb out from under the user's finger (#2772 / #3003).
 *
 * A plain controlled `<input type="range" value={serverValue}>` has two
 * problems the moment the write is a network call. The thumb can only move
 * once the server has agreed, so any re-render that lands mid-drag snaps it
 * back to the last value the server confirmed; and every input tick fires its
 * own write, so one drag becomes dozens of requests whose responses can land
 * out of order and re-pin the thumb to a value the user already dragged past.
 *
 * This hook renders the user's value immediately and commits outward once,
 * `delay` ms after the last tick. It then refuses server reconciliation on two
 * independent grounds, because either one alone leaves a hole:
 *
 *   - value: an incoming value equal to what we last committed is the echo of
 *     our own write, not news, so it is ignored.
 *   - time: anything arriving within `settleMs` of the last tick is ignored
 *     too. That is what catches the late response to a SUPERSEDED write, which
 *     carries a value we sent earlier and so passes the value check.
 *
 * Outside those, a change really did come from somewhere else (a voice
 * command, another client, another device) and is adopted straight away.
 *
 * @param {number} serverValue Latest value from the server.
 * @param {(value:number)=>unknown} commit Called with the settled value.
 * @param {{delay?:number, settleMs?:number}} [options]
 * @returns {[number, (value:number)=>void, ()=>void]} `[value, onInput, flush]`
 *   - `value`  what the slider (and any label beside it) should render
 *   - `onInput` call on every input tick
 *   - `flush`  commit anything pending right now (drag end, unmount, reset)
 */
export function useOptimisticSliderValue(serverValue, commit, options = {}) {
  const { delay = DEFAULT_COMMIT_DELAY_MS, settleMs = DEFAULT_SETTLE_MS } = options;

  const [value, setValue] = useState(serverValue);
  // Last value we sent outward — an echo of it is not an external change.
  const committedRef = useRef(serverValue);
  // Clock time until which the user owns the value, whatever the server says.
  const settleUntilRef = useRef(0);
  const timerRef = useRef(null);
  const pendingRef = useRef(null);

  const commitRef = useRef(commit);
  commitRef.current = commit;

  const flush = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    if (pendingRef.current === null) return;
    const next = pendingRef.current;
    pendingRef.current = null;
    committedRef.current = next;
    commitRef.current(next);
  }, []);

  // Flush rather than drop on unmount: a slider dragged and then closed (the
  // modal shuts, the group is collapsed) must still send the value the user
  // chose, not silently discard it inside the debounce window.
  const flushRef = useRef(flush);
  flushRef.current = flush;
  useEffect(() => () => flushRef.current(), []);

  useEffect(() => {
    if (serverValue === committedRef.current) return;
    if (Date.now() < settleUntilRef.current) return;
    committedRef.current = serverValue;
    setValue(serverValue);
  }, [serverValue]);

  const onInput = useCallback((next) => {
    setValue(next);
    settleUntilRef.current = Date.now() + settleMs;
    pendingRef.current = next;
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      if (pendingRef.current === null) return;
      const settled = pendingRef.current;
      pendingRef.current = null;
      committedRef.current = settled;
      commitRef.current(settled);
    }, delay);
  }, [delay, settleMs]);

  return [value, onInput, flush];
}
