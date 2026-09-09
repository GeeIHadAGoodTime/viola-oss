import React, { useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';

const theme = THEME;

/**
 * A modal footer action button (primary or secondary variant).
 * @param {Object} props
 * @param {'primary'|'secondary'} [props.variant='secondary'] - Visual variant
 * @param {function} [props.onClick] - Click handler
 * @param {boolean} [props.disabled] - Disable interaction
 * @param {React.ReactNode} props.children - Button content
 */
const FooterButton = ({ variant, onClick, disabled, children }) => {
  const [hovered, setHovered] = useState(false);
  const isPrimary = variant === 'primary';

  return (
    <button
      onClick={onClick}
      disabled={disabled}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        padding: '10px 20px',
        minHeight: '44px',
        borderRadius: '10px',
        border: isPrimary ? 'none' : `1px solid ${hovered ? theme.colors.borderHover : theme.colors.borderLight}`,
        backgroundColor: isPrimary
          ? theme.colors.accent
          : (hovered ? theme.colors.glassBase : 'transparent'),
        color: isPrimary ? '#fff' : theme.colors.textSecondary,
        fontSize: '14px',
        fontWeight: isPrimary ? 600 : 500,
        cursor: disabled ? 'not-allowed' : 'pointer',
        transition: 'all 0.15s ease',
        opacity: disabled ? 0.5 : 1,
        boxShadow: isPrimary && hovered ? `0 0 0 3px ${theme.colors.accentRing}` : 'none',
      }}
    >
      {children}
    </button>
  );
};

FooterButton.propTypes = {
  variant: PropTypes.oneOf(['primary', 'secondary']),
  onClick: PropTypes.func,
  disabled: PropTypes.bool,
  children: PropTypes.node.isRequired,
};

export default FooterButton;
