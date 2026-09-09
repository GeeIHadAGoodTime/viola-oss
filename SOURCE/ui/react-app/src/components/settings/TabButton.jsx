import React, { useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';

const theme = THEME;

/**
 * A tab navigation button for the settings modal header.
 * @param {Object} props
 * @param {boolean} props.isActive - Whether this tab is currently selected
 * @param {function} props.onClick - Called when tab is clicked
 * @param {React.ElementType} props.Icon - Icon component to display
 * @param {string} props.label - Tab label text
 */
const TabButton = React.memo(({ isActive, onClick, Icon, label }) => {
  const [hovered, setHovered] = useState(false);

  return (
    <button
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      role="tab"
      aria-selected={isActive}
      aria-label={label}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: '6px',
        padding: '12px 16px',
        borderRadius: '10px',
        border: 'none',
        backgroundColor: isActive ? theme.colors.accentActive : (hovered ? theme.colors.accentHover : 'transparent'),
        color: isActive ? theme.colors.accent : theme.colors.textMuted,
        fontSize: '13px',
        fontWeight: 500,
        cursor: 'pointer',
        transition: 'all 0.15s ease',
        whiteSpace: 'nowrap',
      }}
    >
      <Icon />
      <span>{label}</span>
    </button>
  );
});

TabButton.propTypes = {
  isActive: PropTypes.bool.isRequired,
  onClick: PropTypes.func.isRequired,
  Icon: PropTypes.elementType.isRequired,
  label: PropTypes.string.isRequired,
};

export default TabButton;
