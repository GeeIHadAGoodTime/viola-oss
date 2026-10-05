import { useCallback, useEffect, useRef, useState } from 'react';
import { DEFAULT_COMMIT_DELAY_MS, DEFAULT_SETTLE_MS } from './useOptimisticSliderValue';

export const ACKNOWLEDGEMENT_TIMEOUT_MS = 15000;

// Rooms writes return an acknowledgement. Preserve immediate dragging and
// trailing debounce, but serialize commits and restore only confirmed values
// after failure. The existing calibration slider keeps its separate contract.
export function useAcknowledgedSliderValue(serverValue, commit) {
  const state = useRef({
    active: true, shown: serverValue, confirmed: serverValue,
    lastCommitted: serverValue, version: 0, pending: null, timer: null,
    queue: [], running: false, settleUntil: 0, error: null,
  });
  const commitRef = useRef(commit);
  commitRef.current = commit;
  const [view, setView] = useState({ value: serverValue, error: null, pending: false });
  const publish = useCallback(() => {
    const session = state.current;
    if (session.active) setView({ value: session.shown, error: session.error, pending: session.pending !== null || session.running || session.queue.length > 0 });
  }, []);

  const drain = useCallback(function run() {
    const session = state.current;
    if (session.running || !session.queue.length) return;
    const request = session.queue.shift();
    session.running = true;
    let settled = false;
    let timeout;
    let deadline = Infinity;
    const finish = (result, failure = null, synchronous = false) => {
      if (settled) return;
      // A response microtask can run before a throttled timer callback. Check
      // the clock and the real adapter's publication ownership at settlement.
      if (performance.now() >= deadline || result?.uiCurrent === false) failure = 'timeout';
      settled = true;
      clearTimeout(timeout);
      const refused = failure || !(result === true || result?.ok === true || (synchronous && result === undefined));
      if (!refused) {
        session.confirmed = Number.isFinite(result?.value) ? result.value : request.value;
      }
      if (request.version === session.version) {
        session.error = failure === 'timeout'
          ? "Save not confirmed. Try again."
          : refused ? "Couldn't save volume. Try again." : null;
      }
      session.running = false;
      if (session.pending === null && session.queue.length === 0) {
        session.shown = session.confirmed;
      }
      publish();
      run();
    };
    // Recovery retires UI ownership; it cannot cancel a backend write.
    try {
      const result = request.commit(request.value);
      // The real Rooms adapter starts its response-admission deadline before
      // this timer, so its stale snapshot cannot escape through parent state.
      deadline = performance.now() + ACKNOWLEDGEMENT_TIMEOUT_MS;
      timeout = setTimeout(() => finish(null, 'timeout'), ACKNOWLEDGEMENT_TIMEOUT_MS);
      if (result && typeof result.then === 'function') {
        Promise.resolve(result).then(value => finish(value), () => finish(null, 'rejected'));
      } else {
        // Synchronous callbacks retain the existing slider callback contract.
        finish(result, null, true);
      }
    } catch {
      finish(null, 'thrown');
    }
  }, [publish]);

  const flush = useCallback(() => {
    const session = state.current;
    if (session.timer !== null) clearTimeout(session.timer);
    session.timer = null;
    if (session.pending === null) return;
    const pending = session.pending;
    session.pending = null;
    session.lastCommitted = pending.value;
    session.queue.push({ ...pending, commit: commitRef.current });
    drain();
  }, [drain]);

  const onInput = useCallback(next => {
    const session = state.current;
    session.shown = next;
    session.error = null;
    session.pending = { value: next, version: ++session.version };
    session.settleUntil = Date.now() + DEFAULT_SETTLE_MS;
    if (session.timer !== null) clearTimeout(session.timer);
    session.timer = setTimeout(flush, DEFAULT_COMMIT_DELAY_MS);
    publish();
  }, [flush, publish]);

  useEffect(() => {
    const session = state.current;
    session.active = true;
    return () => {
      session.active = false;
      // Closing a room card must still save the last deliberate drag. Writes
      // already admitted remain ordered; cleanup only stops UI publication.
      flush();
    };
  }, [flush]);

  useEffect(() => {
    const session = state.current;
    session.confirmed = serverValue;
    if (session.pending !== null || session.running || session.queue.length) return;
    if (session.error === null && (serverValue === session.lastCommitted || Date.now() < session.settleUntil)) return;
    session.lastCommitted = serverValue;
    session.shown = serverValue;
    publish();
  }, [serverValue, publish]);

  return [view.value, onInput, flush, view.error, view.pending];
}
