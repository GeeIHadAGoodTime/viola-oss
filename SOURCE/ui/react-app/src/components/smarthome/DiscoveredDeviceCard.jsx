import React, { useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';

const theme = THEME;

const HUB_KEYWORDS = [
  'homeassistant',
  'hass',
  'hubitat',
  'hue',
  'homekit',
  'smartthings',
  'matter',
];

const MEDIA_KEYWORDS = ['sonos', 'spotifyconnect', 'speaker', 'media'];
const MQTT_KEYWORDS = ['mqtt'];
const noop = () => {};

function normalize(value) {
  return String(value || '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

function deviceText(device) {
  return `${device?.service_type || ''} ${device?.display_name || ''}`;
}

export function isSmartHomeHub(device) {
  const normalized = normalize(deviceText(device));
  return HUB_KEYWORDS.some((keyword) => normalized.includes(keyword));
}

function isMqttBroker(device) {
  const normalized = normalize(deviceText(device));
  return MQTT_KEYWORDS.some((keyword) => normalized.includes(keyword));
}

function isMediaSpeaker(device) {
  const normalized = normalize(deviceText(device));
  return MEDIA_KEYWORDS.some((keyword) => normalized.includes(keyword));
}

export function getDeviceCategory(device) {
  if (isMqttBroker(device)) return 'MQTT broker';
  if (isMediaSpeaker(device)) return 'Media speaker';
  if (isSmartHomeHub(device)) return 'Smart-home hub';
  return 'Local network bridge';
}

export function getDeviceEndpoint(device) {
  const host = device?.ip || '';
  if (!host) return '';

  const rawPort = Number(device?.port);
  const hasPort = Number.isFinite(rawPort) && rawPort > 0;
  const scheme = rawPort === 443 ? 'https' : 'http';

  return `${scheme}://${host}${hasPort ? `:${rawPort}` : ''}`;
}

const DiscoveredDeviceCard = React.memo(({ device, isConnected = false, onSetup = noop }) => {
  const [hovered, setHovered] = useState(false);
  const category = getDeviceCategory(device);
  const endpoint = getDeviceEndpoint(device);
  const canSetup = isSmartHomeHub(device) && !isConnected;

  return (
    <li
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        listStyle: 'none',
        margin: 0,
        padding: '14px 16px',
        borderRadius: '12px',
        border: `1px solid ${hovered ? theme.colors.borderHover : theme.colors.borderLight}`,
        backgroundColor: hovered ? theme.colors.glassBase : theme.colors.bgCard,
        transition: 'border-color 0.15s ease, background-color 0.15s ease',
      }}
    >
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: '12px',
      }}>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            minWidth: 0,
            flexWrap: 'wrap',
          }}>
            <strong style={{
              color: theme.colors.textPrimary,
              fontSize: '14px',
              fontWeight: 600,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              maxWidth: '100%',
            }}>
              {device.display_name || 'Discovered device'}
            </strong>
            <span style={{
              padding: '3px 7px',
              borderRadius: '999px',
              backgroundColor: theme.colors.glassBase,
              color: theme.colors.textMuted,
              fontSize: '11px',
              fontWeight: 600,
              lineHeight: 1.2,
            }}>
              {category}
            </span>
            {isConnected && isSmartHomeHub(device) && (
              <span style={{
                padding: '3px 7px',
                borderRadius: '999px',
                backgroundColor: theme.colors.accentSubtle,
                color: theme.colors.statusGreen,
                fontSize: '11px',
                fontWeight: 600,
                lineHeight: 1.2,
              }}>
                Configured
              </span>
            )}
          </div>
          <div style={{
            color: theme.colors.textMuted,
            fontSize: '12px',
            marginTop: '4px',
            overflowWrap: 'anywhere',
          }}>
            {endpoint || 'Network address unavailable'}
          </div>
        </div>

        {canSetup && (
          <button
            type="button"
            onClick={() => onSetup(device)}
            aria-label="Set up smart-home hub"
            style={{
              padding: '8px 12px',
              borderRadius: '8px',
              border: 'none',
              backgroundColor: theme.colors.accent,
              color: '#fff',
              cursor: 'pointer',
              fontSize: '13px',
              fontWeight: 600,
              flexShrink: 0,
            }}
          >
            Set up
          </button>
        )}
      </div>
    </li>
  );
});

DiscoveredDeviceCard.propTypes = {
  device: PropTypes.shape({
    display_name: PropTypes.string,
    ip: PropTypes.string,
    port: PropTypes.oneOfType([PropTypes.number, PropTypes.string]),
    service_type: PropTypes.string,
    metadata: PropTypes.object,
  }).isRequired,
  isConnected: PropTypes.bool,
  onSetup: PropTypes.func,
};

export default DiscoveredDeviceCard;
