/**
 * useHandsFreeWake — the opt-in toggle for browser hands-free wake word.
 *
 * Stored in localStorage (device-local), NOT in cloud settings: an always-on
 * hot-mic preference is device-specific (which physical machine/tab the user
 * trusts to listen), so it must never sync to other devices. Default is OFF —
 * push-to-talk stays the default; hands-free is strictly opt-in.
 */

import { useCallback, useEffect, useState } from 'react';

export const HANDS_FREE_WAKE_KEY = 'viola.handsFreeWake';
const CHANGE_EVENT = 'viola:hands-free-wake-changed';

function readStored() {
  try {
    return window.localStorage.getItem(HANDS_FREE_WAKE_KEY) === 'on';
  } catch {
    return false;
  }
}

export function useHandsFreeWake() {
  const [enabled, setEnabledState] = useState(readStored);

  useEffect(() => {
    const sync = () => setEnabledState(readStored());
    // storage event fires for OTHER tabs; the custom event syncs THIS tab's
    // components (e.g. settings toggle -> SmartDisplay) instantly.
    window.addEventListener('storage', sync);
    window.addEventListener(CHANGE_EVENT, sync);
    return () => {
      window.removeEventListener('storage', sync);
      window.removeEventListener(CHANGE_EVENT, sync);
    };
  }, []);

  const setEnabled = useCallback((next) => {
    const value = typeof next === 'function' ? next(readStored()) : next;
    try {
      window.localStorage.setItem(HANDS_FREE_WAKE_KEY, value ? 'on' : 'off');
    } catch { /* noop */ }
    setEnabledState(Boolean(value));
    try { window.dispatchEvent(new Event(CHANGE_EVENT)); } catch { /* noop */ }
  }, []);

  return [enabled, setEnabled];
}

export default useHandsFreeWake;
