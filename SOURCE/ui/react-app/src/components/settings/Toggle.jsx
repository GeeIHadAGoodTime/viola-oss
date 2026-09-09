import React from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';

const theme = THEME;

/**
 * A toggle switch component for boolean settings.
 * @param {Object} props
 * @param {boolean} props.checked - Current toggle state
 * @param {function} props.onChange - Called with new boolean value
 * @param {boolean} [props.disabled] - Disable interaction
 * @param {string} [props.ariaLabel] - Accessible label for screen readers
 */
const Toggle = React.memo(({ checked, onChange, disabled, ariaLabel }) => (
  // The outer <button> is the tap target: 44x44 (the mobile touch-target
  // floor -- see issue #367). The visual switch TRACK is the smaller inner
  // div, centered inside that hit box, so the on-screen control still reads
  // as a normal-sized iOS-style switch rather than a giant pill.
  <button
    onClick={() => !disabled && onChange(!checked)}
    aria-label={ariaLabel || 'Toggle'}
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
    }}
  >
    <div style={{
      width: '44px',
      height: '24px',
      borderRadius: '12px',
      backgroundColor: checked ? theme.colors.accent : theme.colors.glassActive,
      position: 'relative',
      transition: 'all 0.2s ease',
    }}
    >
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
      }}
      />
    </div>
  </button>
));

Toggle.propTypes = {
  checked: PropTypes.bool.isRequired,
  onChange: PropTypes.func.isRequired,
  disabled: PropTypes.bool,
  ariaLabel: PropTypes.string,
};

export default Toggle;
