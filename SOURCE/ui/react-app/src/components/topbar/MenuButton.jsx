/**
 * MenuButton — Three-dot menu trigger button.
 */
import PropTypes from 'prop-types';
import { THEME } from '../../config';
import styles from './MenuButton.module.css';

const MenuButton = ({ onClick, isOpen }) => (
  <button
    onClick={onClick}
    aria-label="Open menu"
    aria-haspopup="menu"
    aria-expanded={isOpen}
    className={`${styles.button} ${isOpen ? styles.open : ''}`}
  >
    <svg width="18" height="18" viewBox="0 0 18 18" fill={THEME.colors.textTertiary}>
      <circle cx="3.5" cy="9" r="1.5" />
      <circle cx="9" cy="9" r="1.5" />
      <circle cx="14.5" cy="9" r="1.5" />
    </svg>
  </button>
);

MenuButton.propTypes = {
  onClick: PropTypes.func.isRequired,
  isOpen: PropTypes.bool,
};

export default MenuButton;
