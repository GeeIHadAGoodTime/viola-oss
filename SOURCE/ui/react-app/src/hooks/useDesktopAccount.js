import { useEffect, useState } from 'react';
import { apiFetch } from './useViolaApi';
import { isDesktopApp } from '../utils/runtimeSurface';

/**
 * The Viola account the desktop backend is signed in to ({ signed_in, email }),
 * or null while unknown / off the desktop app. Wake word and the agent read the
 * same store, so this is the truth the UI should agree with even when the
 * webview could not restore its own copy of the session (1.0.5 build 5, A2.7).
 *
 * `enabled` lets callers ask only when it matters (e.g. this page is signed
 * out); `refreshKey` re-reads after a sign-in or sign-out.
 */
export function useDesktopAccount({ enabled = true, refreshKey = '' } = {}) {
  const [account, setAccount] = useState(null);
  useEffect(() => {
    if (!enabled || !isDesktopApp()) {
      setAccount(null);
      return undefined;
    }
    let cancelled = false;
    apiFetch('/v1/onboarding/account')
      .then((result) => {
        if (cancelled) return;
        const info = result && typeof result.signed_in === 'boolean' ? result : result?.data;
        setAccount(info && typeof info.signed_in === 'boolean'
          ? { signed_in: info.signed_in, email: typeof info.email === 'string' ? info.email : null }
          : null);
      })
      .catch(() => { if (!cancelled) setAccount(null); });
    return () => { cancelled = true; };
  }, [enabled, refreshKey]);
  return account;
}

export default useDesktopAccount;
