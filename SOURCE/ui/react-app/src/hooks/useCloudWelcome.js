/**
 * useCloudWelcome — read and complete the cloud/browser first-visit welcome
 * (#2607).
 *
 * A fresh browser account previously landed on the raw dashboard with no
 * orientation: OnboardingOverlay/useVoiceOnboarding is desktop-shaped (device
 * pairing, local TTS narration, mic checks) and bails out entirely on the
 * cloud surface (utils/featureSurface.js DESKTOP_ONLY_FEATURES.onboarding).
 * This hook is the browser-side surface for a NEW, minimal, cloud-native
 * completion record instead:
 *
 *   - GET  /v1/cloud-welcome/status   -> { completed }
 *   - POST /v1/cloud-welcome/complete -> { completed: true }
 *
 * Mirrors useCloudLlmConsent.js's shape deliberately: `completed` is `null`
 * until the first server read resolves, then `true`/`false`. Consumers
 * should only show the welcome when this is explicitly `false`, so a
 * returning completed user and the brief loading window never see it.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch } from './useViolaApi';

const CLOUD_WELCOME_STATUS_PATH = '/v1/cloud-welcome/status';
const CLOUD_WELCOME_COMPLETE_PATH = '/v1/cloud-welcome/complete';

export function useCloudWelcome({ enabled = true } = {}) {
  // null = unknown (not yet loaded); true/false once known.
  const [completed, setCompleted] = useState(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const refresh = useCallback(async () => {
    if (!enabled) return null;
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch(CLOUD_WELCOME_STATUS_PATH);
      const isCompleted = !!(data && data.completed);
      if (mountedRef.current) setCompleted(isCompleted);
      return isCompleted;
    } catch (err) {
      if (mountedRef.current) setError(err);
      return null;
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    if (enabled) void refresh();
  }, [enabled, refresh]);

  const complete = useCallback(async () => {
    setSaving(true);
    setError(null);
    try {
      await apiFetch(CLOUD_WELCOME_COMPLETE_PATH, { method: 'POST' });
      if (mountedRef.current) setCompleted(true);
      return true;
    } catch (err) {
      // Best-effort, matching the desktop onboarding's own
      // completeOnboarding() convention ("Best effort; the user can still
      // use the app."): a failed write must never trap the user behind the
      // welcome. Mark it done for this session locally; a transient network
      // failure means it may show once more on a future reload, which is
      // recoverable and not a safety concern.
      if (mountedRef.current) {
        setError(err);
        setCompleted(true);
      }
      return false;
    } finally {
      if (mountedRef.current) setSaving(false);
    }
  }, []);

  return { completed, loading, saving, error, refresh, complete };
}

export default useCloudWelcome;
