import React, { useCallback, useEffect, useRef, useState } from 'react';
import { THEME } from '../../config';
import { apiFetch } from '../../hooks/useViolaApi';

const theme = THEME;
const POLL_MS = 2000;

function normalizeStatus(data) {
  const status = data?.status || data?.auth || data || {};
  return {
    signed_in: Boolean(status.signed_in),
    account_email: status.account_email || status.email || '',
    expires_at: status.expires_at || '',
    instructions: status.instructions || '',
    last_refresh: status.last_refresh || '',
  };
}

function buttonStyle({ primary = false, danger = false, disabled = false } = {}) {
  return {
    padding: '9px 14px',
    borderRadius: '8px',
    border: primary ? 'none' : `1px solid ${danger ? theme.colors.statusRed : theme.colors.borderLight}`,
    backgroundColor: primary ? theme.colors.accent : 'transparent',
    color: primary ? '#fff' : danger ? theme.colors.statusRed : theme.colors.textSecondary,
    fontSize: '13px',
    fontWeight: primary ? 600 : 500,
    cursor: disabled ? 'not-allowed' : 'pointer',
    opacity: disabled ? 0.55 : 1,
  };
}

const CodexAuthCard = React.memo(() => {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [polling, setPolling] = useState(false);
  const pollRef = useRef(null);

  const clearPoll = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const loadStatus = useCallback(async ({ silent = false } = {}) => {
    if (!silent) {
      setLoading(true);
    }
    setError('');
    try {
      const data = await apiFetch('/v1/codex/auth/status');
      const nextStatus = normalizeStatus(data);
      setStatus(nextStatus);
      if (nextStatus.signed_in) {
        setPolling(false);
        clearPoll();
      }
      return nextStatus;
    } catch (err) {
      setError(err?.message || 'Could not check ChatGPT sign-in.');
      return null;
    } finally {
      if (!silent) {
        setLoading(false);
      }
    }
  }, [clearPoll]);

  useEffect(() => {
    loadStatus();
    return clearPoll;
  }, [clearPoll, loadStatus]);

  const startPolling = useCallback(() => {
    setPolling(true);
    clearPoll();
    pollRef.current = setInterval(() => {
      loadStatus({ silent: true });
    }, POLL_MS);
  }, [clearPoll, loadStatus]);

  const handleLaunch = useCallback(async () => {
    setBusy('launch');
    setError('');
    try {
      await apiFetch('/v1/codex/auth/launch', { method: 'POST' });
      await loadStatus({ silent: true });
      startPolling();
    } catch (err) {
      setError(err?.message || 'Could not open ChatGPT sign-in.');
    } finally {
      setBusy('');
    }
  }, [loadStatus, startPolling]);

  const handleRefresh = useCallback(async () => {
    setBusy('refresh');
    setError('');
    try {
      const data = await apiFetch('/v1/codex/auth/refresh', { method: 'POST' });
      setStatus(normalizeStatus(data));
      await loadStatus({ silent: true });
    } catch (err) {
      setError(err?.message || 'Could not refresh ChatGPT credentials.');
    } finally {
      setBusy('');
    }
  }, [loadStatus]);

  const handleSignOut = useCallback(async () => {
    setBusy('signout');
    setError('');
    try {
      await apiFetch('/v1/codex/auth/signout', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: true }),
      });
      clearPoll();
      setPolling(false);
      await loadStatus({ silent: true });
    } catch (err) {
      setError(err?.message || 'Could not sign out.');
    } finally {
      setBusy('');
    }
  }, [clearPoll, loadStatus]);

  const signedIn = Boolean(status?.signed_in);

  return (
    <section
      aria-label="ChatGPT Plus sign-in"
      style={{
        marginBottom: '28px',
        backgroundColor: theme.colors.bgElevated,
        border: `1px solid ${theme.colors.borderSubtle}`,
        borderRadius: '16px',
        overflow: 'hidden',
      }}
    >
      <div style={{ padding: '16px 20px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: '16px', alignItems: 'flex-start' }}>
          <div>
            <div style={{ color: theme.colors.textPrimary, fontSize: '15px', fontWeight: 600 }}>
              ChatGPT Plus
            </div>
            <div style={{ color: theme.colors.textMuted, fontSize: '13px', marginTop: '3px', lineHeight: 1.45 }}>
              Sign in once with the Codex CLI so Viola can use your subscription.
            </div>
          </div>
          <span
            style={{
              color: signedIn ? theme.colors.statusGreen : theme.colors.textMuted,
              fontSize: '12px',
              fontWeight: 600,
              whiteSpace: 'nowrap',
            }}
          >
            {loading ? 'Checking...' : signedIn ? 'Signed in' : 'Signed out'}
          </span>
        </div>

        {loading ? (
          <div style={{ color: theme.colors.textMuted, fontSize: '13px' }}>Checking sign-in status...</div>
        ) : signedIn ? (
          <div style={{ display: 'grid', gap: '10px' }}>
            <div style={{ color: theme.colors.textSecondary, fontSize: '13px' }}>
              {status.account_email || 'ChatGPT account connected'}
              {status.expires_at ? ` - expires ${status.expires_at}` : ''}
            </div>
            <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
              <button type="button" onClick={handleRefresh} disabled={busy === 'refresh'} style={buttonStyle()}>
                {busy === 'refresh' ? 'Refreshing...' : 'Refresh'}
              </button>
              <button type="button" onClick={handleSignOut} disabled={busy === 'signout'} style={buttonStyle({ danger: true })}>
                {busy === 'signout' ? 'Signing out...' : 'Sign out'}
              </button>
            </div>
          </div>
        ) : (
          <div style={{ display: 'grid', gap: '10px' }}>
            {status?.instructions && (
              <div style={{ color: theme.colors.textMuted, fontSize: '13px', lineHeight: 1.45 }}>
                {status.instructions}
              </div>
            )}
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
              <button type="button" onClick={handleLaunch} disabled={busy === 'launch'} style={buttonStyle({ primary: true })}>
                {busy === 'launch' ? 'Opening...' : 'Sign in with ChatGPT'}
              </button>
              {polling && <span style={{ color: theme.colors.textMuted, fontSize: '12px' }}>Waiting for sign-in...</span>}
            </div>
          </div>
        )}

        {error && (
          <div style={{ color: theme.colors.statusRed, fontSize: '12px' }}>
            {error}
          </div>
        )}
      </div>
    </section>
  );
});

export default CodexAuthCard;
