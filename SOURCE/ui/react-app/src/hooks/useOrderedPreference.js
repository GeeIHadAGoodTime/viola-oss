import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';

// Keep optimistic controls responsive without allowing concurrent writes or an
// older failure to replace the newest choice. Each control owns its own queue.
export function useOrderedPreference(serverValue, fallback) {
  const [value, setValue] = useState(serverValue ?? fallback);
  const state = useRef({
    active: true,
    shown: serverValue ?? fallback,
    confirmed: serverValue ?? fallback,
    pending: new Map(),
    nextId: 0,
    tail: Promise.resolve(),
  });

  useLayoutEffect(() => {
    const session = state.current;
    session.active = true;
    return () => { session.active = false; };
  }, []);

  useEffect(() => {
    if (serverValue === undefined) return;
    const session = state.current;
    session.confirmed = serverValue;
    if (session.pending.size === 0) {
      session.shown = serverValue;
      setValue(serverValue);
    }
  }, [serverValue]);

  const update = useCallback((choose, save, onError, readErrorValue) => {
    const session = state.current;
    const next = choose(session.shown);
    const id = ++session.nextId;
    session.pending.set(id, next);
    session.shown = next;
    setValue(next);
    session.tail = session.tail.then(async () => {
      // Do not let a queued click from a dismissed/prior principal UI write.
      if (!session.active) return;
      let failure;
      try {
        await save(next);
        if (!session.active) return;
        session.confirmed = next;
      } catch (error) {
        if (!session.active) return;
        failure = error;
        const reported = readErrorValue?.(error);
        if (reported !== undefined) session.confirmed = reported;
      }
      session.pending.delete(id);
      const pending = [...session.pending.values()];
      session.shown = pending.length ? pending[pending.length - 1] : session.confirmed;
      setValue(session.shown);
      if (failure) onError(failure);
    });
  }, []);

  return [value, update];
}
