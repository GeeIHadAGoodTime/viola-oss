import React from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';

const theme = THEME;

function hasValue(value) {
  return typeof value === 'string' && value.trim().length > 0;
}

function getDisplayUrl(url) {
  if (!hasValue(url)) return '';

  try {
    return new URL(url).origin;
  } catch {
    return url.trim();
  }
}

export function getConfiguredHubs(settings, detected = null) {
  const hubUrl = settings?.home_assistant_url || '';
  const hubToken = settings?.home_assistant_token || '';

  if (!hasValue(hubUrl) || !hasValue(hubToken)) {
    return [];
  }

  // The hub is Home Assistant by construction (these are HA-specific
  // credentials), so name it for real instead of a generic placeholder.
  // When a live probe has run, enrich with the home's own name + version
  // and a verified "Connected" status; otherwise it is merely "Configured".
  const connected = detected?.connected === true;
  return [
    {
      id: 'primary-smart-home-hub',
      label: 'Home Assistant',
      name: hasValue(detected?.hub_name) ? detected.hub_name : '',
      version: hasValue(detected?.hub_version) ? detected.hub_version : '',
      url: getDisplayUrl(hubUrl),
      status: connected ? 'Connected' : 'Configured',
    },
  ];
}

const ConnectedHubsList = React.memo(({ settings = {}, hubs = null }) => {
  const configuredHubs = hubs?.length ? hubs : getConfiguredHubs(settings);

  if (!configuredHubs.length) {
    return null;
  }

  return (
    <div style={{
      padding: '14px 20px',
      borderBottom: `1px solid ${theme.colors.borderSubtle}`,
      backgroundColor: theme.colors.bgSurface,
    }}>
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: '12px',
        flexWrap: 'wrap',
      }}>
        <div>
          <div style={{
            color: theme.colors.textPrimary,
            fontSize: '14px',
            fontWeight: 600,
          }}>
            Connected hubs
          </div>
          <div style={{
            color: theme.colors.textMuted,
            fontSize: '12px',
            marginTop: '2px',
          }}>
            {configuredHubs[0]?.version
              ? `Home Assistant ${configuredHubs[0].version}`
              : `${configuredHubs.length} local bridge configured`}
          </div>
        </div>
        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          flexWrap: 'wrap',
        }}>
          {configuredHubs.map((hub) => (
            <div
              key={hub.id}
              title={hub.url || hub.label}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '8px',
                maxWidth: '320px',
                padding: '6px 10px',
                borderRadius: '999px',
                border: `1px solid ${theme.colors.accentBorder}`,
                backgroundColor: theme.colors.accentSubtle,
                color: theme.colors.textPrimary,
                fontSize: '12px',
                lineHeight: 1.2,
              }}
            >
              <span style={{
                width: '7px',
                height: '7px',
                borderRadius: '50%',
                backgroundColor: theme.colors.statusGreen,
                flexShrink: 0,
              }} />
              <span style={{
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}>
                {hub.name ? `${hub.label} · ${hub.name}` : hub.label}
              </span>
              <span style={{
                color: theme.colors.statusGreen,
                fontWeight: 600,
                flexShrink: 0,
              }}>
                {hub.status}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
});

ConnectedHubsList.propTypes = {
  settings: PropTypes.object,
  hubs: PropTypes.arrayOf(PropTypes.shape({
    id: PropTypes.string.isRequired,
    label: PropTypes.string.isRequired,
    name: PropTypes.string,
    version: PropTypes.string,
    url: PropTypes.string,
    status: PropTypes.string.isRequired,
  })),
};

export default ConnectedHubsList;
