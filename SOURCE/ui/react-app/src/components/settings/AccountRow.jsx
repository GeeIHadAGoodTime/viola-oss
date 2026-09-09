import React, { useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';

const theme = THEME;

/**
 * A row showing an account connection status with connect/disconnect button.
 * @param {Object} props
 * @param {React.ElementType} props.icon - Icon component for the service
 * @param {string} props.name - Service name (e.g. "Spotify", "Google Calendar")
 * @param {boolean} [props.connected] - Whether the account is connected
 * @param {string} [props.username] - Connected username/email to display
 * @param {function} [props.onConnect] - Called when connect/disconnect button is clicked
 * @param {boolean} [props.disabled=false] - Show unavailable state
 */
const AccountRow = React.memo(({ icon: Icon, name, connected, username, onConnect, disabled = false }) => {
  const [hovered, setHovered] = useState(false);
  const [buttonHovered, setButtonHovered] = useState(false);

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '16px 20px',
        backgroundColor: hovered ? theme.colors.glassBase : 'transparent',
        transition: 'background-color 0.15s ease',
        opacity: disabled ? 0.6 : 1,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
        <div style={{
          width: '40px',
          height: '40px',
          borderRadius: '10px',
          backgroundColor: theme.colors.bgCard,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: connected ? theme.colors.textPrimary : theme.colors.textMuted,
        }}>
          <Icon />
        </div>
        <div>
          <div style={{ color: theme.colors.textPrimary, fontSize: '15px', fontWeight: 500 }}>{name}</div>
          <div style={{ color: connected ? theme.colors.statusGreen : theme.colors.textMuted, fontSize: '13px' }}>
            {connected ? username : disabled ? 'Unavailable' : 'Not connected'}
          </div>
        </div>
      </div>
      <button
        onClick={disabled ? undefined : onConnect}
        disabled={disabled}
        onMouseEnter={() => !disabled && setButtonHovered(true)}
        onMouseLeave={() => setButtonHovered(false)}
        title={disabled ? 'Unavailable' : undefined}
        style={{
          padding: '8px 16px',
          borderRadius: '8px',
          border: connected ? `1px solid ${buttonHovered ? theme.colors.borderHover : theme.colors.borderLight}` : 'none',
          backgroundColor: disabled ? theme.colors.bgCard : (connected ? 'transparent' : (buttonHovered ? theme.colors.glassHover : theme.colors.glassBase)),
          color: disabled ? theme.colors.textMuted : (connected ? theme.colors.textSecondary : theme.colors.textPrimary),
          fontSize: '13px',
          fontWeight: 500,
          cursor: disabled ? 'not-allowed' : 'pointer',
          transition: 'all 0.15s ease',
        }}
      >
        {disabled ? 'Unavailable' : (connected ? 'Disconnect' : 'Connect')}
      </button>
    </div>
  );
});

AccountRow.propTypes = {
  icon: PropTypes.elementType.isRequired,
  name: PropTypes.string.isRequired,
  connected: PropTypes.bool,
  username: PropTypes.string,
  onConnect: PropTypes.func,
  disabled: PropTypes.bool,
};

export default AccountRow;
