import React, { useState } from 'react';
import PropTypes from 'prop-types';

/**
 * A button for destructive/dangerous actions with colored border.
 * @param {Object} props
 * @param {string} props.color - Border and text color (e.g. theme.colors.statusRed)
 * @param {function} props.onClick - Click handler
 * @param {boolean} [props.disabled] - Disable interaction
 * @param {React.ReactNode} props.children - Button content
 */
const DangerButton = ({ color, onClick, disabled, children }) => {
  const [hovered, setHovered] = useState(false);

  return (
    <button
      onClick={onClick}
      disabled={disabled}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        padding: '12px 20px',
        minHeight: '44px',
        borderRadius: '12px',
        border: `1px solid ${color}`,
        backgroundColor: hovered && !disabled ? `${color}20` : 'transparent',
        color: color,
        fontSize: '14px',
        fontWeight: 500,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.6 : 1,
        transition: 'all 0.15s ease',
      }}
    >
      {children}
    </button>
  );
};

DangerButton.propTypes = {
  color: PropTypes.string.isRequired,
  onClick: PropTypes.func.isRequired,
  disabled: PropTypes.bool,
  children: PropTypes.node.isRequired,
};

export default DangerButton;
