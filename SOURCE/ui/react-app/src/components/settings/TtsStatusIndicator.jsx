import React, { useState, useEffect } from 'react';
import { THEME } from '../../config';
import { apiFetch } from '../../hooks/useViolaApi';
import { isFeatureHidden } from '../../utils/featureSurface';

const theme = THEME;
const MODAL_OPEN_DEFER_MS = 50;

/**
 * TTS status indicator that shows Kokoro model availability.
 * Displays: loading, active (green dot), fallback (warning), or nothing if TTS is off.
 */
const TtsStatusIndicator = React.memo(() => {
  const [ttsStatus, setTtsStatus] = useState(null); // null | 'loading' | 'active' | 'fallback' | 'error'

  useEffect(() => {
    // #4226: `/v1/diagnostics` is the LOCAL_ONLY `diagnostics` route group
    // (backend/cloud_route_manifest.py), so on the cloud SPA both the fetch
    // and its retry 404 and this lands on 'fallback' — telling a browser user
    // "Enhanced voice unavailable" about a LOCAL TTS engine their surface
    // never uses. Render nothing rather than a wrong warning; this indicator
    // reports desktop TTS health, which is `system_controls` territory.
    if (isFeatureHidden('system_controls')) {
      setTtsStatus(null);
      return undefined;
    }
    let cancelled = false;
    setTtsStatus('loading');

    const applyDiagnostics = (resp) => {
      if (cancelled) return;
      const diag = resp?.data || resp;
      // The diagnostics endpoint includes tts_enabled in settings
      // Check if Kokoro TTS backend is configured and available
      const ttsEnabled = diag?.settings?.tts_enabled;
      if (!ttsEnabled) {
        setTtsStatus(null);
        return;
      }
      // If we get diagnostics, TTS is at least configured.
      // We infer Kokoro availability from backend health.
      // The system object tells us if we're running properly.
      setTtsStatus('active');
    };

    const timeoutId = setTimeout(() => {
      apiFetch('/v1/diagnostics')
        .then(applyDiagnostics)
        .catch(() => {
          // A single one-shot fetch rejection (a network blip) must not flip
          // straight to the "Enhanced voice unavailable" warning — retry once
          // before treating it as a real fallback signal.
          if (cancelled) return;
          apiFetch('/v1/diagnostics')
            .then(applyDiagnostics)
            .catch(() => {
              if (!cancelled) setTtsStatus('fallback');
            });
        });
    }, MODAL_OPEN_DEFER_MS);

    return () => {
      cancelled = true;
      clearTimeout(timeoutId);
    };
  }, []);

  if (ttsStatus === null) return null;

  if (ttsStatus === 'loading') {
    return (
      <div style={{
        padding: '10px 20px 14px',
        color: theme.colors.textMuted,
        fontSize: '12px',
      }}>
        Checking voice...
      </div>
    );
  }

  if (ttsStatus === 'fallback' || ttsStatus === 'error') {
    return (
      <div style={{
        padding: '10px 20px 14px',
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
        color: theme.colors.statusYellow,
        fontSize: '12px',
      }}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
          <line x1="12" y1="9" x2="12" y2="13"/>
          <line x1="12" y1="17" x2="12.01" y2="17"/>
        </svg>
        <span>Enhanced voice unavailable. Using standard voice.</span>
      </div>
    );
  }

  return (
    <div style={{
      padding: '10px 20px 14px',
      display: 'flex',
      alignItems: 'center',
      gap: '8px',
      color: theme.colors.textMuted,
      fontSize: '12px',
    }}>
      <div style={{
        width: '7px',
        height: '7px',
        borderRadius: '50%',
        backgroundColor: theme.colors.statusGreen,
        flexShrink: 0,
      }} />
      <span>Voice responses: Active</span>
    </div>
  );
});

export default TtsStatusIndicator;
