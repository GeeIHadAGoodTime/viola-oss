/**
 * Transport control SVG icons — module-scope to prevent remount.
 */
import PropTypes from 'prop-types';
import { THEME } from '../../config';

export const ShuffleIcon = () => (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="16 3 21 3 21 8" />
    <line x1="4" y1="20" x2="21" y2="3" />
    <polyline points="21 16 21 21 16 21" />
    <line x1="15" y1="15" x2="21" y2="21" />
    <line x1="4" y1="4" x2="9" y2="9" />
  </svg>
);

export const PrevIcon = () => (
  <svg width="32" height="32" viewBox="0 0 24 24" fill="currentColor">
    <path d="M6 6h2v12H6V6zm3.5 6l8.5 6V6l-8.5 6z" />
  </svg>
);

export const PauseIcon = () => (
  <svg width="40" height="40" viewBox="0 0 24 24" fill="currentColor">
    <path d="M6 4h4v16H6V4zm8 0h4v16h-4V4z" />
  </svg>
);

export const PlayIcon = () => (
  <svg width="40" height="40" viewBox="0 0 24 24" fill="currentColor">
    <path d="M8 5v14l11-7z" />
  </svg>
);

export const NextIcon = () => (
  <svg width="32" height="32" viewBox="0 0 24 24" fill="currentColor">
    <path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z" />
  </svg>
);

export const RepeatIcon = ({ mode }) => (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="17 1 21 5 17 9" />
    <path d="M3 11V9a4 4 0 0 1 4-4h14" />
    <polyline points="7 23 3 19 7 15" />
    <path d="M21 13v2a4 4 0 0 1-4 4H3" />
    {mode === 'one' && <text x="12" y="14" fontSize="8" fill="currentColor" textAnchor="middle" fontWeight="bold">1</text>}
  </svg>
);

RepeatIcon.propTypes = {
  mode: PropTypes.oneOf(['off', 'all', 'one']),
};

export const VolumeIcon = ({ level }) => {
  const isMuted = level === 0;
  const isLow = level > 0 && level <= 33;
  const isMedium = level > 33 && level <= 66;
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.7 }}>
      <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" fill="currentColor" stroke="none" />
      {isMuted && <><line x1="23" y1="9" x2="17" y2="15" /><line x1="17" y1="9" x2="23" y2="15" /></>}
      {!isMuted && isLow && <path d="M15.54 8.46a5 5 0 0 1 0 7.07" />}
      {!isMuted && isMedium && <><path d="M15.54 8.46a5 5 0 0 1 0 7.07" /><path d="M19.07 4.93a10 10 0 0 1 0 14.14" /></>}
      {!isMuted && !isLow && !isMedium && <><path d="M15.54 8.46a5 5 0 0 1 0 7.07" /><path d="M19.07 4.93a10 10 0 0 1 0 14.14" /></>}
    </svg>
  );
};

VolumeIcon.propTypes = {
  level: PropTypes.number.isRequired,
};

export const HeartIcon = ({ filled, color }) => (
  <svg width="28" height="28" viewBox="0 0 24 24" fill={filled ? color : 'none'} stroke={filled ? color : 'currentColor'} strokeWidth="1.5">
    <path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z" />
  </svg>
);

HeartIcon.propTypes = {
  filled: PropTypes.bool,
  color: PropTypes.string,
};

export const BrokenHeartIcon = ({ filled, color }) => (
  <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke={filled ? color : 'currentColor'} strokeWidth="1.5">
    <path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09" fill={filled ? color : 'none'} />
    <path d="M12 5.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35" fill={filled ? color : 'none'} />
    <path d="M12 5.5 L10.5 10 L13.5 12 L10.5 16 L12 21" stroke={filled ? THEME.colors.bgCard : 'currentColor'} strokeWidth="2" />
  </svg>
);

BrokenHeartIcon.propTypes = {
  filled: PropTypes.bool,
  color: PropTypes.string,
};
