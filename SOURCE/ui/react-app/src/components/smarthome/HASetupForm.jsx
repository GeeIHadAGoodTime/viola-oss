import React, { useEffect, useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';
import { apiFetch } from '../../hooks/useViolaApi';
import { useDebouncedCallback } from '../settings';
import { getDeviceEndpoint } from './DiscoveredDeviceCard';

const theme = THEME;
const REDACTED_SECRET = '\u2022\u2022\u2022\u2022\u2022\u2022';
const noop = () => {};

function normalizeHubUrl(rawUrl) {
  const trimmed = String(rawUrl || '').trim();
  if (!trimmed) return '';
  const withScheme = /^https?:\/\//i.test(trimmed) ? trimmed : `http://${trimmed}`;
  return withScheme.replace(/\/+$/, '');
}

function validateHubUrl(rawUrl) {
  const normalized = normalizeHubUrl(rawUrl);
  if (!normalized) return 'Enter the hub URL.';

  try {
    const parsed = new URL(normalized);
    if (!['http:', 'https:'].includes(parsed.protocol)) {
      return 'Use an http or https URL.';
    }
  } catch {
    return 'Enter a valid URL.';
  }

  return '';
}

function isRedactedToken(value) {
  return String(value || '').trim() === REDACTED_SECRET;
}

function statusColor(status) {
  if (status === 'success') return theme.colors.statusGreen;
  if (status === 'error') return theme.colors.statusRed;
  return theme.colors.textMuted;
}

const inputStyle = (focused) => ({
  width: '100%',
  padding: '10px 12px',
  borderRadius: '8px',
  border: `1px solid ${focused ? theme.colors.borderHover : theme.colors.borderLight}`,
  backgroundColor: theme.colors.bgCard,
  color: theme.colors.textPrimary,
  fontSize: '13px',
  outline: 'none',
  boxSizing: 'border-box',
  transition: 'border-color 0.15s ease',
});

const buttonStyle = ({ variant = 'secondary', disabled = false } = {}) => ({
  padding: '8px 12px',
  borderRadius: '8px',
  border: variant === 'primary' ? 'none' : `1px solid ${theme.colors.borderLight}`,
  backgroundColor: variant === 'primary' ? theme.colors.accent : theme.colors.glassBase,
  color: variant === 'primary' ? '#fff' : theme.colors.textPrimary,
  cursor: disabled ? 'not-allowed' : 'pointer',
  opacity: disabled ? 0.6 : 1,
  fontSize: '13px',
  fontWeight: 600,
  whiteSpace: 'nowrap',
});

const HASetupForm = React.memo(({
  device = null,
  initialUrl = '',
  initialToken = '',
  onSave = noop,
  onCancel = null,
}) => {
  const prefilledUrl = useMemo(
    () => initialUrl || getDeviceEndpoint(device),
    [device, initialUrl],
  );
  const [hubUrl, setHubUrl] = useState(prefilledUrl);
  const [token, setToken] = useState(initialToken || '');
  const [urlIssue, setUrlIssue] = useState('');
  const [urlFocused, setUrlFocused] = useState(false);
  const [tokenFocused, setTokenFocused] = useState(false);
  const [showToken, setShowToken] = useState(false);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState({ status: '', message: '' });

  const debouncedValidateUrl = useDebouncedCallback((nextUrl) => {
    setUrlIssue(validateHubUrl(nextUrl));
  }, 250);

  useEffect(() => {
    setHubUrl(prefilledUrl);
  }, [prefilledUrl]);

  useEffect(() => {
    setToken(initialToken || '');
  }, [initialToken]);

  const handleUrlChange = (event) => {
    const nextUrl = event.target.value;
    setHubUrl(nextUrl);
    setFeedback({ status: '', message: '' });
    debouncedValidateUrl(nextUrl);
  };

  const handleTokenChange = (event) => {
    setToken(event.target.value);
    setFeedback({ status: '', message: '' });
  };

  const getValidatedPayload = ({ requirePlainToken }) => {
    const normalizedUrl = normalizeHubUrl(hubUrl);
    const nextUrlIssue = validateHubUrl(normalizedUrl);
    setUrlIssue(nextUrlIssue);

    if (nextUrlIssue) {
      return { error: nextUrlIssue };
    }

    const trimmedToken = token.trim();
    if (!trimmedToken) {
      return { error: 'Enter the access token.' };
    }

    if (requirePlainToken && isRedactedToken(trimmedToken)) {
      return { error: 'Paste the access token again to test the connection.' };
    }

    const settings = { home_assistant_url: normalizedUrl };
    if (!isRedactedToken(trimmedToken)) {
      settings.home_assistant_token = trimmedToken;
    }

    return { settings, url: normalizedUrl, token: trimmedToken };
  };

  const handleTestConnection = async () => {
    const payload = getValidatedPayload({ requirePlainToken: true });
    if (payload.error) {
      setFeedback({ status: 'error', message: payload.error });
      return;
    }

    setTesting(true);
    setFeedback({ status: 'pending', message: 'Testing connection...' });

    try {
      const data = await apiFetch('/v1/smarthome/test-connection', {
        method: 'POST',
        body: JSON.stringify(payload.settings),
      });

      if (data?.connected || data?.ok === true) {
        setFeedback({ status: 'success', message: data.message || 'Connection confirmed.' });
      } else {
        setFeedback({ status: 'error', message: data?.message || 'Connection failed.' });
      }
    } catch {
      setFeedback({
        status: 'error',
        message: 'Connection testing is temporarily unavailable.',
      });
    } finally {
      setTesting(false);
    }
  };

  const handleSubmit = async (event) => {
    event.preventDefault();
    const payload = getValidatedPayload({ requirePlainToken: false });
    if (payload.error) {
      setFeedback({ status: 'error', message: payload.error });
      return;
    }

    setSaving(true);
    setFeedback({ status: 'pending', message: 'Saving setup...' });

    try {
      const data = await apiFetch('/v1/settings', {
        method: 'PATCH',
        body: JSON.stringify({ settings: payload.settings }),
      });

      if (data?.settings && !Object.prototype.hasOwnProperty.call(data.settings, 'home_assistant_url')) {
        setFeedback({
          status: 'error',
          message: 'This backend cannot store smart-home hub setup yet.',
        });
        return;
      }

      setFeedback({ status: 'success', message: 'Smart-home hub setup saved.' });
      onSave({
        settings: payload.settings,
        response: data,
      });
    } catch (error) {
      setFeedback({
        status: 'error',
        message: error?.message || 'Could not save setup.',
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <form
      onSubmit={handleSubmit}
      noValidate
      aria-label="Smart-home hub setup"
      style={{
        margin: '14px 20px 18px',
        padding: '16px',
        borderRadius: '12px',
        border: `1px solid ${theme.colors.borderLight}`,
        backgroundColor: theme.colors.bgCard,
      }}
    >
      <div style={{
        color: theme.colors.textPrimary,
        fontSize: '14px',
        fontWeight: 600,
        marginBottom: '4px',
      }}>
        Set up local control
      </div>
      <div style={{
        color: theme.colors.textMuted,
        fontSize: '12px',
        lineHeight: 1.4,
        marginBottom: '14px',
      }}>
        Enter the local bridge URL and access token used by your smart-home hub.
      </div>

      <div style={{ display: 'grid', gap: '12px' }}>
        <label style={{ display: 'grid', gap: '6px' }}>
          <span style={{ color: theme.colors.textSecondary, fontSize: '13px', fontWeight: 600 }}>
            Hub URL
          </span>
          <input
            type="url"
            value={hubUrl}
            onChange={handleUrlChange}
            onFocus={() => setUrlFocused(true)}
            onBlur={() => {
              setUrlFocused(false);
              setUrlIssue(validateHubUrl(hubUrl));
            }}
            placeholder="http://192.168.1.20:8123"
            aria-label="Smart-home hub URL"
            aria-invalid={urlIssue ? 'true' : 'false'}
            style={inputStyle(urlFocused)}
          />
          {urlIssue && (
            <span style={{ color: theme.colors.statusRed, fontSize: '12px' }}>{urlIssue}</span>
          )}
        </label>

        <label style={{ display: 'grid', gap: '6px' }}>
          <span style={{ color: theme.colors.textSecondary, fontSize: '13px', fontWeight: 600 }}>
            Access token
          </span>
          <div style={{ position: 'relative' }}>
            <input
              type={showToken ? 'text' : 'password'}
              value={token}
              onChange={handleTokenChange}
              onFocus={() => setTokenFocused(true)}
              onBlur={() => setTokenFocused(false)}
              placeholder="Paste access token"
              aria-label="Smart-home hub access token"
              style={{
                ...inputStyle(tokenFocused),
                paddingRight: '72px',
                fontFamily: showToken ? 'inherit' : 'monospace',
              }}
            />
            <button
              type="button"
              onClick={() => setShowToken((visible) => !visible)}
              aria-label={showToken ? 'Hide access token' : 'Show access token'}
              style={{
                position: 'absolute',
                right: '8px',
                top: '50%',
                transform: 'translateY(-50%)',
                border: 'none',
                background: 'transparent',
                color: theme.colors.textMuted,
                cursor: 'pointer',
                fontSize: '12px',
                fontWeight: 600,
                padding: '4px 6px',
              }}
            >
              {showToken ? 'Hide' : 'Show'}
            </button>
          </div>
        </label>
      </div>

      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: '12px',
        marginTop: '14px',
        flexWrap: 'wrap',
      }}>
        <div style={{
          color: statusColor(feedback.status),
          fontSize: '12px',
          minHeight: '16px',
        }} aria-live="polite">
          {feedback.message}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
          <button
            type="button"
            onClick={handleTestConnection}
            disabled={testing || saving}
            style={buttonStyle({ disabled: testing || saving })}
          >
            {testing ? 'Testing...' : 'Test connection'}
          </button>
          {onCancel && (
            <button
              type="button"
              onClick={onCancel}
              disabled={saving}
              style={buttonStyle({ disabled: saving })}
            >
              Cancel
            </button>
          )}
          <button
            type="submit"
            disabled={saving || testing}
            style={buttonStyle({ variant: 'primary', disabled: saving || testing })}
          >
            {saving ? 'Saving...' : 'Save setup'}
          </button>
        </div>
      </div>
    </form>
  );
});

HASetupForm.propTypes = {
  device: PropTypes.shape({
    display_name: PropTypes.string,
    ip: PropTypes.string,
    port: PropTypes.oneOfType([PropTypes.number, PropTypes.string]),
    service_type: PropTypes.string,
    metadata: PropTypes.object,
  }),
  initialUrl: PropTypes.string,
  initialToken: PropTypes.string,
  onSave: PropTypes.func,
  onCancel: PropTypes.func,
};

export default HASetupForm;
