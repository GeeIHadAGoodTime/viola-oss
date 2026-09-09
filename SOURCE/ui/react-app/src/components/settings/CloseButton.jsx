import React, { useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';
import { Icons } from './Icons';

const theme = THEME;

/**
 * A close button for the settings modal header.
 * @param {Object} props
 * @param {function} props.onClick - Called when close button is clicked
 */
const CloseButton = ({ onClick }) => {
  const [hovered, setHovered] = useState(false);

  return (
    <button
      type="button"
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      aria-label="Close"
      style={{
        width: '44px',
        height: '44px',
        borderRadius: '10px',
        border: 'none',
        backgroundColor: hovered ? theme.colors.glassBase : 'transparent',
        color: hovered ? theme.colors.textPrimary : theme.colors.textMuted,
        cursor: 'pointer',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        transition: 'all 0.15s ease',
      }}
    >
      <Icons.Close />
    </button>
  );
};

CloseButton.propTypes = {
  onClick: PropTypes.func.isRequired,
};

export default CloseButton;
