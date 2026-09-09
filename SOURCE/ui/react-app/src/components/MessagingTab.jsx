import React, { useState, useCallback } from 'react';
import PropTypes from 'prop-types';
import { apiFetch } from '../hooks/useViolaApi';
import { THEME } from '../config';

const theme = THEME;

// ─── Channel Definitions ────────────────────────────────────────────────────

const CHANNELS = [
  {
    id: 'telegram',
    name: 'Telegram',
    fields: [
      { key: 'telegram_bot_token', label: 'Bot Token', type: 'secret', placeholder: '123456:ABC-DEF...' },
    ],
    enableKey: 'telegram_enabled',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
        <path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.479.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/>
      </svg>
    ),
  },
];

// ─── Sub-components ─────────────────────────────────────────────────────────

const Toggle = ({ checked, onChange, disabled }) => (
  // Outer button is the 44x44 mobile touch target; the visual 44x24 track sits
  // centered inside so the control still reads as a normal iOS-style switch.
  <button
    onClick={() => !disabled && onChange(!checked)}
    aria-checked={checked}
    role="switch"
    style={{
      width: '44px',
      height: '44px',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      border: 'none',
      background: 'none',
      padding: 0,
      cursor: disabled ? 'not-allowed' : 'pointer',
      opacity: disabled ? 0.5 : 1,
      flexShrink: 0,
    }}
  >
    <div style={{
      width: '44px',
      height: '24px',
      borderRadius: '12px',
      backgroundColor: checked ? theme.colors.accent : theme.colors.glassActive,
      position: 'relative',
      transition: 'all 0.2s ease',
    }}>
      <div style={{
        width: '18px',
        height: '18px',
        borderRadius: '50%',
        backgroundColor: theme.colors.textBright || '#ffffff',
        position: 'absolute',
        top: '3px',
        left: checked ? '23px' : '3px',
        transition: 'left 0.2s ease',
        boxShadow: `0 2px 4px ${theme.colors.shadowLight}`,
      }} />
    </div>
  </button>
);

Toggle.propTypes = {
  checked: PropTypes.bool.isRequired,
  onChange: PropTypes.func.isRequired,
  disabled: PropTypes.bool,
};

const SecretInput = ({ value, onChange, placeholder, disabled }) => {
  const [visible, setVisible] = useState(false);
  const [focused, setFocused] = useState(false);

  return (
    <div style={{ position: 'relative', width: '100%' }}>
      <input
        type={visible ? 'text' : 'password'}
        value={value || ''}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        style={{
          width: '100%',
          padding: '10px 40px 10px 12px',
          borderRadius: '8px',
          border: `1px solid ${focused ? theme.colors.borderHover : theme.colors.borderLight}`,
          backgroundColor: theme.colors.bgCard,
          color: theme.colors.textPrimary,
          fontSize: '13px',
          outline: 'none',
          boxSizing: 'border-box',
          transition: 'border-color 0.15s ease',
          opacity: disabled ? 0.5 : 1,
          fontFamily: visible ? 'inherit' : 'monospace',
        }}
      />
      <button
        type="button"
        onClick={() => setVisible(!visible)}
        tabIndex={-1}
        style={{
          position: 'absolute',
          right: '8px',
          top: '50%',
          transform: 'translateY(-50%)',
          background: 'none',
          border: 'none',
          cursor: 'pointer',
          padding: '4px',
          color: theme.colors.textMuted,
          display: 'flex',
          alignItems: 'center',
        }}
        title={visible ? 'Hide' : 'Show'}
      >
        {visible ? (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/>
            <line x1="1" y1="1" x2="23" y2="23"/>
          </svg>
        ) : (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
            <circle cx="12" cy="12" r="3"/>
          </svg>
        )}
      </button>
    </div>
  );
};

SecretInput.propTypes = {
  value: PropTypes.string,
  onChange: PropTypes.func.isRequired,
  placeholder: PropTypes.string,
  disabled: PropTypes.bool,
};

const TextInput = ({ value, onChange, placeholder, disabled, type }) => {
  const [focused, setFocused] = useState(false);

  return (
    <input
      type={type || 'text'}
      value={value || ''}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      disabled={disabled}
      onFocus={() => setFocused(true)}
      onBlur={() => setFocused(false)}
      style={{
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
        opacity: disabled ? 0.5 : 1,
      }}
    />
  );
};

TextInput.propTypes = {
  value: PropTypes.string,
  onChange: PropTypes.func.isRequired,
  placeholder: PropTypes.string,
  disabled: PropTypes.bool,
  type: PropTypes.string,
};

// ─── Status Dot ─────────────────────────────────────────────────────────────

const StatusDot = ({ status }) => {
  const color = status === 'connected'
    ? theme.colors.statusGreen
    : status === 'error'
      ? theme.colors.statusRed
      : status === 'testing'
        ? theme.colors.statusYellow
        : theme.colors.textDisabled;

  return (
    <div style={{
      width: '8px',
      height: '8px',
      borderRadius: '50%',
      backgroundColor: color,
      flexShrink: 0,
      transition: 'background-color 0.15s ease',
    }} />
  );
};

StatusDot.propTypes = {
  status: PropTypes.string,
};

// ─── Channel Card ───────────────────────────────────────────────────────────

const ChannelCard = ({ channel, settings, onSettingChange }) => {
  const [hovered, setHovered] = useState(false);
  const [testStatus, setTestStatus] = useState(null); // null | 'testing' | 'connected' | 'error'
  const [testMessage, setTestMessage] = useState('');

  const enabled = !!settings[channel.enableKey];

  const handleTestConnection = useCallback(async () => {
    setTestStatus('testing');
    setTestMessage('Testing...');

    try {
      const config = {};
      for (const field of channel.fields) {
        config[field.key] = settings[field.key] || '';
      }

      const data = await apiFetch('/v1/settings/test-messaging', {
        method: 'POST',
        body: JSON.stringify({ channel: channel.id, config }),
      });

      if (data && data.connected) {
        setTestStatus('connected');
        setTestMessage(data.message || 'Connected');
      } else {
        setTestStatus('error');
        setTestMessage(data?.message || 'Connection failed');
      }
    } catch {
      setTestStatus('error');
      setTestMessage('Could not reach service — check your settings');
    }

    // Clear status after 8 seconds
    setTimeout(() => {
      setTestStatus(null);
      setTestMessage('');
    }, 8000);
  }, [channel, settings]);

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        backgroundColor: theme.colors.bgElevated,
        borderRadius: '12px',
        border: `1px solid ${hovered ? theme.colors.borderHover : theme.colors.borderLight}`,
        overflow: 'hidden',
        transition: 'all 0.15s ease',
      }}
    >
      {/* Card Header */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '16px 20px',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <div style={{ color: enabled ? theme.colors.accent : theme.colors.textMuted, flexShrink: 0 }}>
            {channel.icon}
          </div>
          <div>
            <div style={{
              display: 'flex',
              alignItems: 'center',
              gap: '8px',
            }}>
              <span style={{
                color: theme.colors.textPrimary,
                fontSize: '15px',
                fontWeight: 500,
              }}>
                {channel.name}
              </span>
              {testStatus && <StatusDot status={testStatus} />}
            </div>
            {channel.external && (
              <div style={{
                color: theme.colors.textMuted,
                fontSize: '12px',
                marginTop: '2px',
              }}>
                Requires a separate app to be running on your device.
              </div>
            )}
          </div>
        </div>
        <Toggle
          checked={enabled}
          onChange={(val) => onSettingChange(channel.enableKey, val)}
        />
      </div>

      {/* Config Fields (shown when enabled) */}
      {enabled && (
        <div style={{
          padding: '0 20px 16px 20px',
          display: 'flex',
          flexDirection: 'column',
          gap: '12px',
        }}>
          <div style={{
            height: '1px',
            backgroundColor: theme.colors.borderSubtle,
            marginBottom: '4px',
          }} />

          {channel.fields.map((field) => (
            <div key={field.key}>
              <label style={{
                display: 'block',
                marginBottom: '6px',
                color: theme.colors.textSecondary,
                fontSize: '13px',
                fontWeight: 500,
              }}>
                {field.label}
              </label>
              {field.type === 'secret' ? (
                <SecretInput
                  value={settings[field.key] || ''}
                  onChange={(val) => onSettingChange(field.key, val)}
                  placeholder={field.placeholder}
                />
              ) : (
                <TextInput
                  value={settings[field.key] || ''}
                  onChange={(val) => onSettingChange(field.key, val)}
                  placeholder={field.placeholder}
                  type={field.type === 'url' ? 'url' : 'text'}
                />
              )}
            </div>
          ))}

          {/* Test Connection Button + Status */}
          <div style={{
            display: 'flex',
            alignItems: 'center',
            gap: '12px',
            marginTop: '4px',
          }}>
            <TestButton
              onClick={handleTestConnection}
              testing={testStatus === 'testing'}
            />
            {testMessage && (
              <span style={{
                fontSize: '13px',
                color: testStatus === 'connected'
                  ? theme.colors.statusGreen
                  : testStatus === 'error'
                    ? theme.colors.statusRed
                    : theme.colors.textMuted,
              }}>
                {testMessage}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

ChannelCard.propTypes = {
  channel: PropTypes.object.isRequired,
  settings: PropTypes.object.isRequired,
  onSettingChange: PropTypes.func.isRequired,
};

// ─── Test Button ────────────────────────────────────────────────────────────

const TestButton = ({ onClick, testing }) => {
  const [hovered, setHovered] = useState(false);

  return (
    <button
      onClick={onClick}
      disabled={testing}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        padding: '8px 16px',
        borderRadius: '8px',
        border: `1px solid ${hovered ? theme.colors.borderHover : theme.colors.borderLight}`,
        backgroundColor: hovered ? theme.colors.glassHover : theme.colors.glassBase,
        color: theme.colors.textPrimary,
        fontSize: '13px',
        fontWeight: 500,
        cursor: testing ? 'not-allowed' : 'pointer',
        transition: 'all 0.15s ease',
        opacity: testing ? 0.6 : 1,
        whiteSpace: 'nowrap',
      }}
    >
      {testing ? 'Testing...' : 'Test Connection'}
    </button>
  );
};

TestButton.propTypes = {
  onClick: PropTypes.func.isRequired,
  testing: PropTypes.bool,
};

// ─── Main Component ─────────────────────────────────────────────────────────

function MessagingTab({ settings, onSettingChange }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
      {/* Section Label */}
      <div style={{
        fontSize: '11px',
        fontWeight: 600,
        textTransform: 'uppercase',
        letterSpacing: '1px',
        color: theme.colors.textMuted,
        marginBottom: '4px',
        paddingLeft: '4px',
      }}>
        Telegram Bot
      </div>

      <div style={{
        color: theme.colors.textSecondary,
        fontSize: '13px',
        marginBottom: '8px',
        paddingLeft: '4px',
      }}>
        Use a Telegram bot token to send and receive Viola commands from Telegram.
      </div>

      {/* Channel Cards */}
      {CHANNELS.map((channel) => (
        <ChannelCard
          key={channel.id}
          channel={channel}
          settings={settings}
          onSettingChange={onSettingChange}
        />
      ))}
    </div>
  );
}

MessagingTab.propTypes = {
  settings: PropTypes.object.isRequired,
  onSettingChange: PropTypes.func.isRequired,
};

export default React.memo(MessagingTab);
