/**
 * "Connect iCloud Calendar" settings card (#3281).
 *
 * Apple has no OAuth path for third-party CalDAV access -- iCloud requires
 * 2FA on the Apple ID plus a separate app-specific password
 * (https://appleid.apple.com/account/manage). This is a plain form POST to
 * POST /v1/calendar/caldav/connect (icloud: true fills the server URL + the
 * python-caldav "icloud" compatibility flag) / POST /v1/calendar/caldav/disconnect
 * -- see services/calendar/manager.py connect_caldav_account()/
 * disconnect_caldav_account() and ui/api/routes/calendar.py (#1392).
 *
 * Desktop-only (Tier-3 external-account secret, same as the Google/Graph
 * OAuth linking this reuses the `oauth_provider_linking` feature-surface key
 * from): the backend route itself refuses to run when this deployment hosts
 * its own CalDAV backend (cloud_caldav_configured()), so the cloud SPA gate
 * here is defense-in-depth, matching CalendarSettings' Google Calendar card.
 */

import { useCallback, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME as theme } from '../config';
import { isFeatureAvailable } from '../utils/featureSurface';
import DesktopUpsell from './DesktopUpsell';
import { useCalDAVCalendar } from '../hooks/useCalDAVCalendar';

const APPLE_ID_HELP_URL = 'https://appleid.apple.com/account/manage';

const AppleIcon = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
    <path d="M18.71 19.5c-.83 1.24-1.71 2.45-3.05 2.47-1.34.03-1.77-.79-3.29-.79-1.53 0-2 .77-3.27.82-1.31.05-2.3-1.32-3.14-2.53C4.25 17 2.94 12.45 4.7 9.39c.87-1.52 2.43-2.48 4.12-2.51 1.28-.02 2.5.87 3.29.87.78 0 2.26-1.07 3.81-.91.65.03 2.47.26 3.64 1.98-.09.06-2.17 1.28-2.15 3.81.03 3.02 2.65 4.03 2.68 4.04-.03.07-.42 1.44-1.38 2.83M13 3.5c.73-.83 1.94-1.46 2.94-1.5.13 1.17-.34 2.35-1.04 3.19-.69.85-1.83 1.51-2.95 1.42-.15-1.15.41-2.35 1.05-3.11z" />
  </svg>
);

const fieldStyle = {
  width: '100%',
  minHeight: '44px',
  padding: '10px 12px',
  borderRadius: '8px',
  border: `1px solid ${theme.colors.borderLight}`,
  backgroundColor: theme.colors.bgCard,
  color: theme.colors.textPrimary,
  fontSize: '13px',
  outline: 'none',
  boxSizing: 'border-box',
};

function ConnectedCalendarList({ calendars }) {
  if (!calendars || calendars.length === 0) {
    return null;
  }
  return (
    <div style={{ padding: '0 20px 12px', color: theme.colors.textMuted, fontSize: '13px' }}>
      {calendars.length === 1 ? '1 calendar synced: ' : `${calendars.length} calendars synced: `}
      {calendars.map((cal) => cal.name || cal.calendar_name || cal.calendar_id).filter(Boolean).join(', ')}
    </div>
  );
}

ConnectedCalendarList.propTypes = {
  calendars: PropTypes.arrayOf(PropTypes.object),
};

export function ICloudCalendarSettings() {
  const {
    status,
    calendars,
    connecting,
    disconnecting,
    error,
    connect,
    disconnect,
    clearError,
  } = useCalDAVCalendar();

  const [showForm, setShowForm] = useState(false);
  const [appleId, setAppleId] = useState('');
  const [appPassword, setAppPassword] = useState('');
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);

  const handleSubmit = useCallback(async (event) => {
    event.preventDefault();
    const result = await connect({ username: appleId.trim(), password: appPassword });
    if (result.success) {
      setShowForm(false);
      setAppleId('');
      setAppPassword('');
    }
  }, [appleId, appPassword, connect]);

  const handleDisconnectConfirm = useCallback(async () => {
    await disconnect();
    setConfirmDisconnect(false);
  }, [disconnect]);

  if (!isFeatureAvailable('oauth_provider_linking')) {
    return <DesktopUpsell feature="oauth_provider_linking" compact />;
  }

  if (status === 'loading') {
    return (
      <div style={{ padding: '16px 20px', color: theme.colors.textMuted, fontSize: '14px' }}>
        Loading...
      </div>
    );
  }

  const isConnected = status === 'connected';

  return (
    <div>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '16px 20px',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '14px', flex: 1, minWidth: 0 }}>
          <div
            style={{
              width: '40px',
              height: '40px',
              borderRadius: '10px',
              backgroundColor: theme.colors.bgCard,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: isConnected ? theme.colors.statusGreen : theme.colors.textMuted,
            }}
          >
            <AppleIcon />
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ color: theme.colors.textPrimary, fontSize: '15px', fontWeight: 500 }}>
              iCloud Calendar
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginTop: '2px' }}>
              {isConnected ? (
                <>
                  <div
                    style={{
                      width: '6px',
                      height: '6px',
                      borderRadius: '50%',
                      backgroundColor: theme.colors.statusGreen,
                      flexShrink: 0,
                    }}
                  />
                  <span style={{ color: theme.colors.statusGreen, fontSize: '13px' }}>Connected</span>
                </>
              ) : (
                <span style={{ color: theme.colors.textMuted, fontSize: '13px' }}>Not connected</span>
              )}
            </div>
          </div>
        </div>

        {isConnected ? (
          <button
            type="button"
            onClick={() => setConfirmDisconnect(true)}
            disabled={disconnecting}
            style={{
              minHeight: '44px',
              boxSizing: 'border-box',
              padding: '8px 16px',
              borderRadius: '8px',
              border: `1px solid ${theme.colors.borderLight}`,
              backgroundColor: 'transparent',
              color: theme.colors.textSecondary,
              fontSize: '13px',
              fontWeight: 500,
              cursor: disconnecting ? 'not-allowed' : 'pointer',
              opacity: disconnecting ? 0.5 : 1,
              flexShrink: 0,
            }}
          >
            {disconnecting ? 'Disconnecting...' : 'Disconnect'}
          </button>
        ) : (
          <button
            type="button"
            onClick={() => setShowForm((prev) => !prev)}
            style={{
              minHeight: '44px',
              boxSizing: 'border-box',
              padding: '8px 16px',
              borderRadius: '8px',
              border: 'none',
              backgroundColor: theme.colors.accent,
              color: '#fff',
              fontSize: '13px',
              fontWeight: 600,
              cursor: 'pointer',
              flexShrink: 0,
            }}
          >
            {showForm ? 'Cancel' : 'Connect'}
          </button>
        )}
      </div>

      {isConnected && <ConnectedCalendarList calendars={calendars} />}

      {error && (
        <div style={{ padding: '0 20px 12px', color: theme.colors.statusRed, fontSize: '13px' }}>
          {error}
        </div>
      )}

      {!isConnected && showForm && (
        <form
          onSubmit={handleSubmit}
          style={{
            margin: '0 20px 16px',
            padding: '16px',
            borderRadius: '12px',
            backgroundColor: theme.colors.bgCard,
            border: `1px solid ${theme.colors.borderLight}`,
          }}
        >
          <div style={{ marginBottom: '12px' }}>
            <label
              htmlFor="icloud-calendar-apple-id"
              style={{ display: 'block', color: theme.colors.textSecondary, fontSize: '13px', marginBottom: '6px' }}
            >
              Apple ID
            </label>
            <input
              id="icloud-calendar-apple-id"
              type="email"
              autoComplete="username"
              value={appleId}
              onChange={(event) => { clearError(); setAppleId(event.target.value); }}
              placeholder="you@icloud.com"
              style={fieldStyle}
              required
            />
          </div>
          <div style={{ marginBottom: '8px' }}>
            <label
              htmlFor="icloud-calendar-app-password"
              style={{ display: 'block', color: theme.colors.textSecondary, fontSize: '13px', marginBottom: '6px' }}
            >
              App-specific password
            </label>
            <input
              id="icloud-calendar-app-password"
              type="password"
              autoComplete="current-password"
              value={appPassword}
              onChange={(event) => { clearError(); setAppPassword(event.target.value); }}
              placeholder="xxxx-xxxx-xxxx-xxxx"
              style={fieldStyle}
              required
            />
          </div>
          <div style={{ marginBottom: '16px', color: theme.colors.textMuted, fontSize: '12px', lineHeight: '1.5' }}>
            Not your regular Apple ID password. Generate one at{' '}
            <a
              href={APPLE_ID_HELP_URL}
              target="_blank"
              rel="noreferrer"
              style={{ color: theme.colors.accent }}
            >
              appleid.apple.com
            </a>{' '}
            under Sign-In and Security &gt; App-Specific Passwords (requires two-factor authentication
            on the Apple ID).
          </div>
          <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
            <button
              type="submit"
              disabled={connecting || !appleId.trim() || !appPassword}
              style={{
                minHeight: '44px',
                boxSizing: 'border-box',
                padding: '8px 16px',
                borderRadius: '8px',
                border: 'none',
                backgroundColor: theme.colors.accent,
                color: '#fff',
                fontSize: '13px',
                fontWeight: 600,
                cursor: connecting ? 'not-allowed' : 'pointer',
                opacity: connecting || !appleId.trim() || !appPassword ? 0.6 : 1,
              }}
            >
              {connecting ? 'Connecting...' : 'Connect'}
            </button>
          </div>
        </form>
      )}

      {confirmDisconnect && (
        <div
          style={{
            margin: '0 20px 16px',
            padding: '16px',
            borderRadius: '12px',
            backgroundColor: theme.colors.bgCard,
            border: `1px solid ${theme.colors.borderLight}`,
          }}
        >
          <div style={{ color: theme.colors.textPrimary, fontSize: '14px', fontWeight: 500, marginBottom: '8px' }}>
            Disconnect iCloud Calendar?
          </div>
          <div style={{ color: theme.colors.textMuted, fontSize: '13px', marginBottom: '16px', lineHeight: '1.4' }}>
            You can reconnect anytime with your Apple ID and an app-specific password. Your iCloud
            events will no longer appear in Viola.
          </div>
          <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
            <button
              type="button"
              onClick={() => setConfirmDisconnect(false)}
              style={{
                minHeight: '44px',
                boxSizing: 'border-box',
                padding: '8px 16px',
                borderRadius: '8px',
                border: `1px solid ${theme.colors.borderLight}`,
                backgroundColor: 'transparent',
                color: theme.colors.textSecondary,
                fontSize: '13px',
                fontWeight: 500,
                cursor: 'pointer',
              }}
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleDisconnectConfirm}
              disabled={disconnecting}
              style={{
                minHeight: '44px',
                boxSizing: 'border-box',
                padding: '8px 16px',
                borderRadius: '8px',
                border: 'none',
                backgroundColor: theme.colors.statusRed,
                color: '#fff',
                fontSize: '13px',
                fontWeight: 600,
                cursor: disconnecting ? 'not-allowed' : 'pointer',
                opacity: disconnecting ? 0.7 : 1,
              }}
            >
              {disconnecting ? 'Disconnecting...' : 'Disconnect'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default ICloudCalendarSettings;
