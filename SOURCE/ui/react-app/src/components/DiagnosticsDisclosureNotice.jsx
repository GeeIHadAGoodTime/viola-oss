/**
 * DiagnosticsDisclosureNotice — the one-time notice for automatic anonymous
 * crash reports.
 *
 * Installed builds send a small anonymized report when Viola crashes
 * (diagnostics/diagnostic_consent.py: the master flag is on in release builds,
 * opt-out per install). A crash report is only allowed to leave the device
 * after this notice has been shown once (`disclosure_shown`); until then crash
 * reports wait in a local queue. So every install, new or upgraded, sees this
 * card once, with a one-click off switch next to the OK button.
 *
 * The same switch lives permanently in Settings > Account > Privacy & Data >
 * "Automatic Crash Reports".
 *
 * Desktop only: the cloud web app and spokes have no local diagnostics route.
 */
import { useCallback, useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';
import { apiFetch } from '../hooks/useViolaApi';

export const DIAGNOSTICS_CONSENT_PATH = '/v1/diagnostics/consent';

/**
 * Show the card only while the feature is armed for this install, the user has
 * not seen it yet, and has not already turned crash reports off.
 * @param {object|null|undefined} consent - `consent` from GET /v1/diagnostics/consent.
 * @returns {boolean}
 */
export function shouldShowDiagnosticsDisclosure(consent) {
  return Boolean(
    consent
      && consent.master_baseline_enabled === true
      && consent.disclosure_shown !== true
      && consent.baseline_opted_out !== true,
  );
}

const theme = THEME;

const buttonBase = {
  padding: '8px 14px',
  minHeight: '36px',
  borderRadius: '18px',
  fontSize: '13px',
  fontWeight: 600,
  cursor: 'pointer',
};

const DiagnosticsDisclosureNotice = ({ enabled = false, onOpenPrivacySettings = null }) => {
  const [visible, setVisible] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!enabled) return undefined;
    let cancelled = false;
    apiFetch(DIAGNOSTICS_CONSENT_PATH)
      .then((data) => {
        if (!cancelled) setVisible(shouldShowDiagnosticsDisclosure(data?.consent));
      })
      .catch(() => {
        // No card is the safe failure: crash reports keep waiting locally
        // until the notice has actually been shown.
      });
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  const save = useCallback(async (body) => {
    setSaving(true);
    setError('');
    try {
      await apiFetch(DIAGNOSTICS_CONSENT_PATH, { method: 'POST', body: JSON.stringify(body) });
      setVisible(false);
    } catch {
      setError("Couldn't save that. Please try again.");
    } finally {
      setSaving(false);
    }
  }, []);

  if (!enabled || !visible) return null;

  return (
    <div
      role="region"
      aria-label="Automatic crash reports"
      data-testid="diagnostics-disclosure"
      style={{
        position: 'fixed',
        left: 20,
        bottom: 20,
        width: 400,
        maxWidth: 'calc(100vw - 40px)',
        boxSizing: 'border-box',
        background: theme.colors.bgElevated,
        border: `1px solid ${theme.colors.bronzeHairline}`,
        borderRadius: 14,
        padding: 16,
        zIndex: 9997,
        boxShadow: `0 8px 32px ${theme.colors.shadowMedium}`,
        color: theme.colors.textPrimary,
        fontSize: 13,
        lineHeight: 1.45,
      }}
    >
      <div style={{ color: theme.colors.textBright, fontWeight: 600, fontSize: 14, marginBottom: 6 }}>
        Viola sends anonymous crash reports
      </div>
      <div style={{ marginBottom: 6 }}>
        When Viola crashes or hits an unexpected error, it sends a short report so we can fix it:
        the app version, your operating system, the error and where in Viola&apos;s code it
        happened. Personal details are removed before it leaves your computer.
      </div>
      <div style={{ color: theme.colors.textSecondary, marginBottom: 12 }}>
        It never includes your name, email, account, recordings, conversations, files or passwords.
        You can turn this off now or any time in Settings &gt; Account &gt; Privacy &amp; Data.
      </div>
      {error && (
        <div role="alert" style={{ color: theme.colors.statusRed, marginBottom: 8 }}>{error}</div>
      )}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
        <button
          type="button"
          data-testid="diagnostics-disclosure-ok"
          disabled={saving}
          onClick={() => save({ disclosure_shown: true })}
          style={{
            ...buttonBase,
            border: `1px solid ${theme.colors.accentBorder}`,
            backgroundColor: theme.colors.accent,
            color: theme.colors.textBright,
          }}
        >
          OK
        </button>
        <button
          type="button"
          data-testid="diagnostics-disclosure-turn-off"
          disabled={saving}
          onClick={() => save({ baseline_opted_out: true, disclosure_shown: true })}
          style={{
            ...buttonBase,
            border: `1px solid ${theme.colors.borderHover}`,
            backgroundColor: 'transparent',
            color: theme.colors.textPrimary,
          }}
        >
          Turn off crash reports
        </button>
        {onOpenPrivacySettings && (
          <button
            type="button"
            data-testid="diagnostics-disclosure-settings"
            onClick={onOpenPrivacySettings}
            style={{
              background: 'none',
              border: 'none',
              padding: '8px 4px',
              color: theme.colors.bronze,
              fontSize: 13,
              cursor: 'pointer',
              textDecoration: 'underline',
            }}
          >
            Privacy settings
          </button>
        )}
      </div>
    </div>
  );
};

DiagnosticsDisclosureNotice.propTypes = {
  enabled: PropTypes.bool,
  onOpenPrivacySettings: PropTypes.func,
};

export default DiagnosticsDisclosureNotice;
