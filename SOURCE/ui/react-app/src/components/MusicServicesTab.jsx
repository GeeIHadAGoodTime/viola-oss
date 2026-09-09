/**
 * Music Services Tab Component for Settings Modal.
 *
 * Provides:
 * - List of available music providers
 * - Connection status for each provider
 * - Connect/Disconnect buttons
 * - OAuth popup flow for linking
 */

import React, { useState, useEffect } from 'react';
import PropTypes from 'prop-types';
import { useMusicProviders } from '../hooks/useMusicProviders';
import { THEME as theme, PROVIDER_DISPLAY_NAMES } from '../config';

// Provider Icons
const ProviderIcons = {
  youtube_music: () => (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 0C5.376 0 0 5.376 0 12s5.376 12 12 12 12-5.376 12-12S18.624 0 12 0zm0 19.104c-3.924 0-7.104-3.18-7.104-7.104S8.076 4.896 12 4.896s7.104 3.18 7.104 7.104-3.18 7.104-7.104 7.104zm0-13.332c-3.432 0-6.228 2.796-6.228 6.228S8.568 18.228 12 18.228 18.228 15.432 18.228 12 15.432 5.772 12 5.772zM9.684 15.54V8.46L15.816 12l-6.132 3.54z"/>
    </svg>
  ),
  spotify: () => (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 0C5.4 0 0 5.4 0 12s5.4 12 12 12 12-5.4 12-12S18.66 0 12 0zm5.521 17.34c-.24.359-.66.48-1.021.24-2.82-1.74-6.36-2.101-10.561-1.141-.418.122-.779-.179-.899-.539-.12-.421.18-.78.54-.9 4.56-1.021 8.52-.6 11.64 1.32.42.18.479.659.301 1.02zm1.44-3.3c-.301.42-.841.6-1.262.3-3.239-1.98-8.159-2.58-11.939-1.38-.479.12-1.02-.12-1.14-.6-.12-.48.12-1.021.6-1.141C9.6 9.9 15 10.561 18.72 12.84c.361.181.54.78.241 1.2zm.12-3.36C15.24 8.4 8.82 8.16 5.16 9.301c-.6.179-1.2-.181-1.38-.721-.18-.601.18-1.2.72-1.381 4.26-1.26 11.28-1.02 15.721 1.621.539.3.719 1.02.419 1.56-.299.421-1.02.599-1.559.3z"/>
    </svg>
  ),
  default: () => (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="12" cy="12" r="10"/>
      <path d="M9 12l2 2 4-4"/>
    </svg>
  ),
};

// Get icon for provider
const getProviderIcon = (providerId) => {
  const Icon = ProviderIcons[providerId] || ProviderIcons.default;
  return <Icon />;
};

// Get display name for provider
const getProviderDisplayName = (provider) => {
  return PROVIDER_DISPLAY_NAMES[provider.id] || provider.name || provider.id;
};

// Section Component
const Section = ({ title, subtitle, children }) => (
  <div style={{ marginBottom: '28px' }}>
    <div style={{ marginBottom: '12px', paddingLeft: '20px' }}>
      <div style={{
        fontSize: '11px',
        fontWeight: 600,
        textTransform: 'uppercase',
        letterSpacing: '1px',
        color: theme.colors.textMuted,
      }}>
        {title}
      </div>
      {subtitle && (
        <div style={{
          fontSize: '12px',
          color: theme.colors.textSecondary,
          marginTop: '4px',
        }}>
          {subtitle}
        </div>
      )}
    </div>
    <div style={{
      backgroundColor: theme.colors.bgElevated,
      borderRadius: '16px',
      border: `1px solid ${theme.colors.borderSubtle}`,
      overflow: 'hidden',
    }}>
      {children}
    </div>
  </div>
);

Section.propTypes = {
  title: PropTypes.string.isRequired,
  subtitle: PropTypes.string,
  children: PropTypes.node.isRequired,
};

// Button Component
const Button = ({ variant = 'primary', onClick, disabled, children, small }) => {
  const [hovered, setHovered] = useState(false);
  const isPrimary = variant === 'primary';
  const isDanger = variant === 'danger';

  return (
    <button
      onClick={onClick}
      disabled={disabled}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        padding: small ? '8px 16px' : '12px 20px',
        borderRadius: '8px',
        border: isPrimary ? 'none' : `1px solid ${hovered ? theme.colors.borderHover : theme.colors.borderLight}`,
        backgroundColor: isPrimary
          ? theme.colors.accent
          : isDanger
            ? (hovered ? `${theme.colors.statusRed}33` : `${theme.colors.statusRed}1A`)
            : (hovered ? theme.colors.glassHover : 'transparent'),
        color: isPrimary
          ? '#fff'
          : isDanger
            ? theme.colors.statusRed
            : theme.colors.textPrimary,
        fontSize: small ? '13px' : '14px',
        fontWeight: 600,
        cursor: disabled ? 'not-allowed' : 'pointer',
        transition: 'all 0.15s ease',
        opacity: disabled ? 0.5 : 1,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: '8px',
        boxShadow: isPrimary && hovered ? `0 0 0 3px ${theme.colors.accentRing}` : 'none',
      }}
    >
      {children}
    </button>
  );
};

Button.propTypes = {
  variant: PropTypes.oneOf(['primary', 'secondary', 'danger']),
  onClick: PropTypes.func,
  disabled: PropTypes.bool,
  children: PropTypes.node.isRequired,
  small: PropTypes.bool,
};

// Provider Row Component
const ProviderRow = ({ provider, onConnect, onDisconnect, isConnecting }) => {
  const isLinked = provider.status === 'linked';
  const isMisconfigured = provider.status === 'misconfigured';
  const isAvailable = provider.status === 'available' || provider.status === 'not_linked';
  const isActive = provider.is_active;
  const isCurrentlyConnecting = isConnecting === provider.id;

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      padding: '16px 20px',
      borderBottom: `1px solid ${theme.colors.borderSubtle}`,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
        <div style={{
          width: '44px',
          height: '44px',
          borderRadius: '10px',
          backgroundColor: theme.colors.bgCard,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: isLinked ? theme.colors.statusGreen : theme.colors.textMuted,
        }}>
          {getProviderIcon(provider.id)}
        </div>
        <div>
          <div style={{
            color: theme.colors.textPrimary,
            fontSize: '15px',
            fontWeight: 500,
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
          }}>
            {getProviderDisplayName(provider)}
            {isActive && (
              <span style={{
                fontSize: '10px',
                padding: '2px 6px',
                borderRadius: '4px',
                backgroundColor: `${theme.colors.accent}33`,
                color: theme.colors.accent,
                fontWeight: 600,
              }}>
                ACTIVE
              </span>
            )}
          </div>
          <div style={{
            fontSize: '13px',
            color: isLinked
              ? theme.colors.statusGreen
              : isMisconfigured
                ? theme.colors.statusYellow
                : theme.colors.textMuted,
          }}>
            {isLinked ? 'Connected' : isMisconfigured ? 'Not configured' : 'Not connected'}
          </div>
          {provider.id === 'spotify' && !isLinked && isAvailable && (
            <div style={{
              fontSize: '12px',
              color: theme.colors.textMuted,
              marginTop: '2px',
            }}>
              Opens a secure browser window to sign in to Spotify.
            </div>
          )}
        </div>
      </div>

      <div>
        {isLinked ? (
          <Button variant="danger" small onClick={() => onDisconnect(provider.id)}>
            Disconnect
          </Button>
        ) : isAvailable ? (
          <Button
            variant="primary"
            small
            onClick={() => onConnect(provider.id)}
            disabled={isCurrentlyConnecting}
          >
            {isCurrentlyConnecting ? 'Connecting...' : 'Connect'}
          </Button>
        ) : (
          <span style={{
            fontSize: '12px',
            color: theme.colors.textMuted,
            padding: '8px 16px',
          }}>
            Unavailable
          </span>
        )}
      </div>
    </div>
  );
};

ProviderRow.propTypes = {
  provider: PropTypes.shape({
    id: PropTypes.string.isRequired,
    name: PropTypes.string,
    status: PropTypes.string.isRequired,
    is_active: PropTypes.bool,
  }).isRequired,
  onConnect: PropTypes.func.isRequired,
  onDisconnect: PropTypes.func.isRequired,
  isConnecting: PropTypes.string,
};

/**
 * Main Music Services Tab Component
 */
export function MusicServicesTab() {
  const { providers, loading, error, connecting, connect, disconnect, clearError, refresh } = useMusicProviders();
  // 10-second loading timeout — if API doesn't respond, show error + retry
  const [loadingTimedOut, setLoadingTimedOut] = useState(false);
  useEffect(() => {
    if (!loading) {
      setLoadingTimedOut(false);
      return;
    }
    const timer = setTimeout(() => setLoadingTimedOut(true), 10000);
    return () => clearTimeout(timer);
  }, [loading]);

  if (loading) {
    if (loadingTimedOut) {
      return (
        <div style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '16px',
          padding: '60px 20px',
          color: theme.colors.textMuted,
        }}>
          <span>Couldn't load music services. Check your connection and try again.</span>
          <button
            onClick={() => { setLoadingTimedOut(false); refresh(); }}
            style={{
              padding: '8px 20px',
              borderRadius: '10px',
              border: 'none',
              backgroundColor: theme.colors.accent,
              color: '#fff',
              cursor: 'pointer',
              fontSize: '14px',
              fontWeight: 500,
            }}
          >
            Try again
          </button>
        </div>
      );
    }
    return (
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '60px 20px',
        color: theme.colors.textMuted,
      }}>
        Loading music services...
      </div>
    );
  }

  const browserProviders = providers;

  return (
    <>
      {error && (
        <div style={{
          margin: '0 0 20px 0',
          padding: '12px 16px',
          borderRadius: '8px',
          backgroundColor: `${theme.colors.statusRed}1A`,
          color: theme.colors.statusRed,
          fontSize: '13px',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}>
          <span>{error}</span>
          <button
            onClick={clearError}
            aria-label="Dismiss error"
            style={{
              background: 'none',
              border: 'none',
              color: theme.colors.statusRed,
              cursor: 'pointer',
              padding: '4px 8px',
            }}
          >
            Dismiss
          </button>
        </div>
      )}

      {browserProviders.length > 0 && (
        <Section title="Music Services">
          {browserProviders.map((provider, index) => (
            <div key={provider.id} style={{
              borderBottom: index < browserProviders.length - 1 ? `1px solid ${theme.colors.borderSubtle}` : 'none',
            }}>
              <ProviderRow
                provider={provider}
                onConnect={connect}
                onDisconnect={disconnect}
                isConnecting={connecting}
              />
            </div>
          ))}
        </Section>
      )}

      {/* No providers available */}
      {providers.length === 0 && (
        <Section title="Music Services">
          <div style={{
            padding: '40px 20px',
            textAlign: 'center',
            color: theme.colors.textMuted,
          }}>
            No music services are available yet.
            <br />
            <span style={{ fontSize: '12px' }}>
              Check Settings &gt; Music &amp; Voice to enable a provider.
            </span>
          </div>
        </Section>
      )}

      <div style={{
        padding: '0 20px',
        fontSize: '12px',
        color: theme.colors.textMuted,
        lineHeight: 1.5,
      }}>
        Connect a music service to play from YouTube Music, Spotify, or local files.
        Your credentials are stored securely on your device.
      </div>
    </>
  );
}

export default React.memo(MusicServicesTab);
