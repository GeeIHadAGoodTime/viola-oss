/**
 * Music Accounts settings panel.
 *
 * Shows connected browser-native music providers with status and
 * management options. Designed to be rendered inside SettingsModal
 * as a tab or section.
 *
 * This is distinct from MusicServicesTab (which handles OAuth consent
 * providers). This panel manages providers that authenticate via the
 * embedded QWebEngineView browser.
 */

import React, { useState, useCallback } from 'react';
import PropTypes from 'prop-types';
import { useBrowserAuth } from '../../hooks/useBrowserAuth';
import { THEME } from '../../config';

const theme = THEME;

// ---------- Provider icon SVGs ----------

const ProviderIconSVGs = {
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
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 18V5l12-2v13"/>
      <circle cx="6" cy="18" r="3"/>
      <circle cx="18" cy="16" r="3"/>
    </svg>
  ),
};

function getProviderIcon(providerName) {
  const Icon = ProviderIconSVGs[providerName] || ProviderIconSVGs.default;
  return <Icon />;
}

// ---------- Status helpers ----------

function getStatusInfo(status) {
  switch (status) {
    case 'connected':
      return { label: 'Connected', color: theme.colors.statusGreen };
    case 'expired':
      return { label: 'Session Expired', color: theme.colors.statusYellow };
    case 'not_connected':
    default:
      return { label: 'Not connected', color: theme.colors.textMuted };
  }
}

// ---------- Sub-components ----------

/** Single provider row in the settings list. */
const ProviderRow = ({ provider, onConnect, onRefresh, isLoggingIn }) => {
  const [hovered, setHovered] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const statusInfo = getStatusInfo(provider.status);
  const isConnected = provider.status === 'connected';
  const isExpired = provider.status === 'expired';
  const isActive = isLoggingIn === provider.name;

  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    await onRefresh();
    setRefreshing(false);
  }, [onRefresh]);

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '14px 20px',
        backgroundColor: hovered ? theme.colors.glassBase : 'transparent',
        transition: 'background-color 0.15s ease',
      }}
    >
      {/* Left: icon + name + status */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
        <div style={{
          width: '44px',
          height: '44px',
          borderRadius: '10px',
          backgroundColor: theme.colors.bgCard,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: isConnected ? theme.colors.statusGreen : theme.colors.textMuted,
          flexShrink: 0,
        }}>
          {provider.iconUrl ? (
            <img
              src={provider.iconUrl}
              alt={provider.displayName}
              style={{ width: '24px', height: '24px', borderRadius: '4px' }}
            />
          ) : (
            getProviderIcon(provider.name)
          )}
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
            {provider.displayName}
            {provider.isDefault && (
              <span style={{
                fontSize: '10px',
                padding: '2px 6px',
                borderRadius: '4px',
                backgroundColor: `${theme.colors.accent}33`,
                color: theme.colors.accent,
                fontWeight: 600,
              }}>
                DEFAULT
              </span>
            )}
          </div>
          <div style={{
            fontSize: '13px',
            color: statusInfo.color,
            marginTop: '2px',
          }}>
            {statusInfo.label}
          </div>
        </div>
      </div>

      {/* Right: action buttons */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
        {/* Refresh button (always visible on hover or when expired) */}
        {(hovered || isExpired) && !isActive && (
          <button
            onClick={handleRefresh}
            disabled={refreshing}
            title="Refresh status"
            aria-label={`Refresh ${provider.displayName} status`}
            style={{
              width: '32px',
              height: '32px',
              borderRadius: '8px',
              border: `1px solid ${theme.colors.borderLight}`,
              backgroundColor: 'transparent',
              color: theme.colors.textSecondary,
              cursor: refreshing ? 'wait' : 'pointer',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              transition: 'all 0.15s ease',
              opacity: refreshing ? 0.5 : 1,
            }}
          >
            <svg
              width="14" height="14" viewBox="0 0 24 24"
              data-essential-motion="spin-fast"
              fill="none" stroke="currentColor" strokeWidth="2"
              strokeLinecap="round" strokeLinejoin="round"
              style={{
                animation: refreshing ? 'browserauth-settings-spin 0.8s linear infinite' : 'none',
              }}
            >
              <polyline points="23 4 23 10 17 10"/>
              <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
            </svg>
          </button>
        )}

        {/* Connect / Reconnect button */}
        {!isConnected && (
          <button
            onClick={() => onConnect(provider.name)}
            disabled={isActive || (isLoggingIn && !isActive)}
            style={{
              padding: '8px 16px',
              borderRadius: '8px',
              border: 'none',
              backgroundColor: isActive
                ? theme.colors.glassActive
                : isExpired
                  ? theme.colors.statusYellow
                  : theme.colors.accent,
              color: isActive
                ? theme.colors.textSecondary
                : isExpired ? theme.colors.bgCard : '#fff',
              fontSize: '13px',
              fontWeight: 600,
              cursor: isActive ? 'wait' : 'pointer',
              transition: 'all 0.15s ease',
              opacity: (isLoggingIn && !isActive) ? 0.4 : 1,
            }}
          >
            {isActive ? 'Logging in...' : isExpired ? 'Reconnect' : 'Connect'}
          </button>
        )}

        {/* Connected indicator (when connected and not hovered) */}
        {isConnected && !hovered && (
          <div style={{
            color: theme.colors.statusGreen,
            display: 'flex',
            alignItems: 'center',
          }}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
              <polyline points="20 6 9 17 4 12"/>
            </svg>
          </div>
        )}
      </div>
    </div>
  );
};

ProviderRow.propTypes = {
  provider: PropTypes.shape({
    name: PropTypes.string.isRequired,
    displayName: PropTypes.string.isRequired,
    status: PropTypes.string.isRequired,
    iconUrl: PropTypes.string,
    isDefault: PropTypes.bool,
  }).isRequired,
  onConnect: PropTypes.func.isRequired,
  onRefresh: PropTypes.func.isRequired,
  isLoggingIn: PropTypes.string,
};

/** Dropdown for selecting the default music provider. */
const DefaultProviderSelector = ({ providers, onSelect }) => {
  const connectedProviders = providers.filter(p => p.status === 'connected');
  const currentDefault = providers.find(p => p.isDefault);

  if (connectedProviders.length < 2) {
    // No point showing a selector with 0 or 1 option
    return null;
  }

  return (
    <div style={{
      padding: '16px 20px',
      borderTop: `1px solid ${theme.colors.borderSubtle}`,
    }}>
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
      }}>
        <div>
          <div style={{
            color: theme.colors.textSecondary,
            fontSize: '13px',
            fontWeight: 500,
          }}>
            Default Provider
          </div>
          <div style={{
            color: theme.colors.textMuted,
            fontSize: '12px',
            marginTop: '2px',
          }}>
            Used when no specific service is requested
          </div>
        </div>
        <select
          value={currentDefault?.name || ''}
          onChange={(e) => onSelect(e.target.value)}
          style={{
            padding: '8px 12px',
            borderRadius: '8px',
            border: `1px solid ${theme.colors.borderLight}`,
            backgroundColor: theme.colors.bgCard,
            color: theme.colors.textPrimary,
            fontSize: '13px',
            cursor: 'pointer',
            outline: 'none',
          }}
        >
          {connectedProviders.map((p) => (
            <option key={p.name} value={p.name}>
              {p.displayName}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
};

DefaultProviderSelector.propTypes = {
  providers: PropTypes.array.isRequired,
  onSelect: PropTypes.func.isRequired,
};

// ---------- Main Component ----------

/**
 * MusicAccountsSettings -- settings panel for browser-native music accounts.
 *
 * Renders a list of providers with connect/reconnect/refresh actions and
 * a default provider selector.
 */
export default function MusicAccountsSettings() {
  const {
    providers,
    loading,
    error,
    loggingIn,
    initiateLogin,
    refreshCheck,
    fetchStatus,
    clearError,
  } = useBrowserAuth();

  const handleSetDefault = useCallback(async (providerName) => {
    // Setting default provider is a backend concern; use apiFetch directly.
    // This is a best-effort call; if the endpoint doesn't exist yet, we
    // just update local state optimistically.
    try {
      const { apiFetch } = await import('../../hooks/useViolaApi');
      await apiFetch('/v1/browser/auth/default', {
        method: 'POST',
        body: JSON.stringify({ provider: providerName }),
      });
      await fetchStatus();
    } catch (err) {
      if (import.meta.env.DEV) {
        console.warn('[MusicAccountsSettings] Failed to set default provider:', err.message);
      }
    }
  }, [fetchStatus]);

  // --- Loading state ---
  if (loading) {
    return (
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '60px 20px',
        color: theme.colors.textMuted,
      }}>
        Loading music accounts...
      </div>
    );
  }

  return (
    <div className="music-accounts-settings">
      {/* Spinner keyframes for refresh animation */}
      <style>{`
        @keyframes browserauth-settings-spin {
          to { transform: rotate(360deg); }
        }
      `}</style>

      {/* Section header */}
      <div style={{ marginBottom: '16px', paddingLeft: '20px' }}>
        <div style={{
          fontSize: '11px',
          fontWeight: 600,
          textTransform: 'uppercase',
          letterSpacing: '1px',
          color: theme.colors.textMuted,
        }}>
          Browser Music Accounts
        </div>
        <div style={{
          fontSize: '12px',
          color: theme.colors.textSecondary,
          marginTop: '4px',
        }}>
          Connect your music services using the built-in browser.
        </div>
      </div>

      {/* Error banner */}
      {error && (
        <div style={{
          margin: '0 20px 16px',
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

      {/* Logging-in banner */}
      {loggingIn && (
        <div style={{
          margin: '0 20px 16px',
          padding: '12px 16px',
          borderRadius: '8px',
          backgroundColor: `${theme.colors.accent}1A`,
          color: theme.colors.textSecondary,
          fontSize: '13px',
          border: `1px solid ${theme.colors.accent}33`,
        }}>
          Logging in... Complete the sign-in in the browser window.
        </div>
      )}

      {/* Provider list */}
      {providers.length > 0 ? (
        <div style={{
          backgroundColor: theme.colors.bgElevated,
          borderRadius: '16px',
          border: `1px solid ${theme.colors.borderSubtle}`,
          overflow: 'hidden',
        }}>
          {providers.map((provider, index) => (
            <div
              key={provider.name}
              style={{
                borderBottom: index < providers.length - 1
                  ? `1px solid ${theme.colors.borderSubtle}`
                  : 'none',
              }}
            >
              <ProviderRow
                provider={provider}
                onConnect={initiateLogin}
                onRefresh={() => refreshCheck(provider.name)}
                isLoggingIn={loggingIn}
              />
            </div>
          ))}

          {/* Default provider selector */}
          <DefaultProviderSelector
            providers={providers}
            onSelect={handleSetDefault}
          />
        </div>
      ) : (
        <div style={{
          backgroundColor: theme.colors.bgElevated,
          borderRadius: '16px',
          border: `1px solid ${theme.colors.borderSubtle}`,
          padding: '40px 20px',
          textAlign: 'center',
          color: theme.colors.textMuted,
        }}>
          No browser-based music services available.
          <br />
          <span style={{ fontSize: '12px' }}>
            Check your configuration to enable providers.
          </span>
        </div>
      )}

      {/* Footer note */}
      <div style={{
        padding: '16px 20px 0',
        fontSize: '12px',
        color: theme.colors.textMuted,
        lineHeight: 1.5,
      }}>
        Browser-based accounts use the built-in browser to sign in directly.
        Your session cookies are stored securely on your device.
      </div>
    </div>
  );
}
