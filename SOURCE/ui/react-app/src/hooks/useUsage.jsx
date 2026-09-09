import { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch } from './useViolaApi';
import { useAuth } from './useAuth';
import { useAuth as useCloudAuth } from '../auth/useAuth';

const POLL_INTERVAL_MS = 60_000;

/**
 * Polls /billing/usage for the signed-in user.
 *
 * /billing/usage is the canonical UI progress-bar endpoint (per its
 * docstring) and per PAY-26 in DECISIONS_REGISTRY: the server returns
 * percentages, never dollar amounts. The percentage already accounts
 * for extra_usage_cents (booster packs); a client-side spent / limit
 * calc would miss that.
 *
 * `monthly_capped` / `weekly_capped` distinguish a real 0% reading from
 * "no cap configured" (Max plan, BYOK) so the badge can hide itself
 * cleanly.
 *
 * Auth-state sync (issue #1175, same root cause family as #1067): the
 * cloud surface authenticates through TWO independent GoTrue session
 * stores — CloudAuthGate/auth/AuthProvider (gates entry to the
 * dashboard) and this hooks/useAuth (lib/auth_context) — see App.jsx
 * `Dashboard()`. A user authenticated ONLY through the cloud front door
 * leaves hooks/useAuth's `isLoggedIn` false, so gating the poll on it
 * alone silently hid the usage progress bar for that user even though
 * AccountTab (post-#1067) already shows them as signed in. Reconcile
 * both stores the same way AccountTab does (`effectiveIsLoggedIn`).
 * auth/AuthProvider is mounted unconditionally around Dashboard on both
 * the cloud and desktop surfaces (App.jsx), so useCloudAuth() is always
 * safe to call here.
 */
export function useUsage() {
  const { isLoggedIn: appIsLoggedIn } = useAuth();
  const { status: cloudStatus } = useCloudAuth();
  const isLoggedIn = appIsLoggedIn || cloudStatus === 'signedIn';
  const [usage, setUsage] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const mountedRef = useRef(true);

  const fetchUsage = useCallback(async () => {
    if (!isLoggedIn) {
      setUsage(null);
      return;
    }
    setLoading(true);
    try {
      const data = await apiFetch('/billing/usage');
      if (!mountedRef.current) return;
      setUsage({
        monthlyPercent: data.monthly_capped ? data.monthly_percent : null,
        weeklyPercent: data.weekly_capped ? data.weekly_percent : null,
        monthlyResetsAt: data.resets_monthly,
        weeklyResetsAt: data.resets_weekly,
      });
      setError(null);
    } catch (err) {
      if (!mountedRef.current) return;
      setError(err?.message || 'Failed to load usage.');
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [isLoggedIn]);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  useEffect(() => {
    if (!isLoggedIn) {
      setUsage(null);
      return undefined;
    }
    fetchUsage();
    const id = setInterval(fetchUsage, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [isLoggedIn, fetchUsage]);

  return { usage, loading, error, refetch: fetchUsage };
}

export default useUsage;
