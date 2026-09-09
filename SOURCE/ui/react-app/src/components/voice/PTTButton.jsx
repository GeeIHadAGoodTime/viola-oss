/**
 * PTTButton — Push-to-Talk button with visual feedback and wake status ring.
 */
import { useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';
import styles from './PTTButton.module.css';

function getWakeColors(status) {
  const map = {
    wake_listening: { ring: THEME.colors.statusRed, glow: `${THEME.colors.statusRed}66` },
    recording: { ring: THEME.colors.statusYellow, glow: `${THEME.colors.statusYellow}66` },
    processing: { ring: THEME.colors.statusYellow, glow: `${THEME.colors.statusYellow}66` },
    off: { ring: THEME.colors.textMuted, glow: 'transparent' },
    degraded: { ring: THEME.colors.statusYellow, glow: `${THEME.colors.statusYellow}66` },
    listening: { ring: THEME.colors.statusGreen, glow: `${THEME.colors.statusGreen}66` },
    starting: { ring: THEME.colors.statusYellow, glow: `${THEME.colors.statusYellow}66` },
  };
  return map[status] || map.off;
}

const PTTButton = ({ active, disabled, wakeStatus, onMouseDown, onMouseUp, onIntent }) => {
  const [isPressed, setIsPressed] = useState(false);
  const colors = getWakeColors(wakeStatus);

  const handleDown = (e) => {
    e.preventDefault();
    e.stopPropagation();
    setIsPressed(true);
    onMouseDown?.();
  };

  const handleUp = (e) => {
    e.preventDefault();
    e.stopPropagation();
    setIsPressed(false);
    onMouseUp?.();
  };

  const isActive = active || isPressed;

  return (
    <button
      onMouseDown={handleDown}
      onMouseUp={handleUp}
      onMouseLeave={(e) => { if (isPressed) handleUp(e); }}
      onTouchStart={handleDown}
      onTouchEnd={handleUp}
      // CONFIRM-6 warm-keeping: hover/focus is a speculative intent signal —
      // "the user is likely about to press this" — used to pre-open the
      // voice WS connection ahead of the actual press (never the mic itself;
      // see useVoiceWs.prewarmConnection). onMouseEnter has no touch analog,
      // so this only helps desktop/hover devices; touch presses fall back to
      // the normal fresh-connect path with no regression.
      onMouseEnter={onIntent}
      onFocus={onIntent}
      aria-label="Push to talk"
      disabled={disabled}
      className={`${styles.button} ${isActive ? styles.active : ''} ${disabled ? styles.disabled : ''}`}
      style={{
        border: `3px solid ${isActive ? THEME.colors.textPrimary : colors.ring}`,
        backgroundColor: isActive ? THEME.colors.textFaint : THEME.colors.borderSubtle,
        boxShadow: isActive
          ? `0 0 40px ${THEME.colors.textSecondary}, 0 0 80px ${THEME.colors.textDisabled}, inset 0 0 30px ${THEME.colors.glassActive}`
          : `0 0 20px ${colors.glow}`,
        transform: isActive ? 'scale(1.12)' : 'scale(1)',
      }}
    >
      <svg
        width="30"
        height="30"
        viewBox="0 0 24 24"
        fill={isActive ? THEME.colors.textDisabled : 'none'}
        stroke={isActive ? THEME.colors.textPrimary : colors.ring}
        strokeWidth={isActive ? '2' : '1.5'}
        strokeLinecap="round"
        strokeLinejoin="round"
        style={{ transition: 'all 0.1s ease-out' }}
      >
        <rect x="9" y="2" width="6" height="12" rx="3" />
        <path d="M5 10a7 7 0 0 0 14 0" />
        <line x1="12" y1="17" x2="12" y2="22" />
        <line x1="8" y1="22" x2="16" y2="22" />
      </svg>
    </button>
  );
};

PTTButton.propTypes = {
  active: PropTypes.bool,
  disabled: PropTypes.bool,
  wakeStatus: PropTypes.string.isRequired,
  onMouseDown: PropTypes.func,
  onMouseUp: PropTypes.func,
  onIntent: PropTypes.func,
};

export default PTTButton;
