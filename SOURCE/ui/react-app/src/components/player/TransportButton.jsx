/**
 * TransportButton — Reusable transport control button with hover styling.
 */
import PropTypes from 'prop-types';
import { THEME } from '../../config';
import styles from './TransportButton.module.css';

// `toggle` marks a button whose `active` state IS its meaning -- shuffle is on
// or it is off. Those get `aria-pressed`, so the state a sighted user reads
// from the highlight is the same state a screen reader announces (#4214).
// Momentary buttons (play, next) and the three-way repeat cycle are not
// pressed/unpressed, so they deliberately do not carry it.
const TransportButton = ({ onClick, active, children, ariaLabel, disabled, toggle }) => (
  <button
    onClick={onClick}
    aria-label={ariaLabel}
    aria-pressed={toggle ? Boolean(active) : undefined}
    aria-disabled={disabled || undefined}
    disabled={disabled}
    className={`${styles.button} ${active ? styles.active : ''} ${disabled ? styles.disabled : ''}`}
    style={{
      color: active ? THEME.colors.textPrimary : THEME.colors.textDisabled,
    }}
  >
    {children}
  </button>
);

TransportButton.propTypes = {
  onClick: PropTypes.func,
  active: PropTypes.bool,
  children: PropTypes.node.isRequired,
  ariaLabel: PropTypes.string,
  disabled: PropTypes.bool,
  toggle: PropTypes.bool,
};

export default TransportButton;
