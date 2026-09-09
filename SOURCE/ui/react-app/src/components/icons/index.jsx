/**
 * Icon components extracted from SmartDisplay.jsx
 * Centralized SVG icons for reuse across the application
 */

import React from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';

// Chevron / disclosure icons
export const ChevronLeftIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="15 18 9 12 15 6" />
  </svg>
);

export const ChevronRightIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="9 18 15 12 9 6" />
  </svg>
);

export const ChevronDownIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="6 9 12 15 18 9" />
  </svg>
);

// Playback control icons
export const PlayIcon = () => (
  <svg width="40" height="40" viewBox="0 0 24 24" fill="currentColor">
    <path d="M8 5v14l11-7z" />
  </svg>
);

export const PauseIcon = () => (
  <svg width="40" height="40" viewBox="0 0 24 24" fill="currentColor">
    <path d="M6 4h4v16H6V4zm8 0h4v16h-4V4z" />
  </svg>
);

export const NextIcon = () => (
  <svg width="32" height="32" viewBox="0 0 24 24" fill="currentColor">
    <path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z" />
  </svg>
);

export const PrevIcon = () => (
  <svg width="32" height="32" viewBox="0 0 24 24" fill="currentColor">
    <path d="M6 6h2v12H6V6zm3.5 6l8.5 6V6l-8.5 6z" />
  </svg>
);

export const MicIcon = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z" />
    <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
    <line x1="12" y1="19" x2="12" y2="22" />
    <line x1="8" y1="22" x2="16" y2="22" />
  </svg>
);

export const SendIcon = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <path d="M22 2 11 13" />
    <path d="M22 2 15 22 11 13 2 9 22 2z" />
  </svg>
);

export const StopIcon = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
    <rect x="6" y="6" width="12" height="12" rx="2.5" />
  </svg>
);

export const AttachIcon = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <path d="M21.4 11.6 12 21a6 6 0 0 1-8.5-8.5l9.7-9.7a4 4 0 0 1 5.7 5.7L9.1 18.3a2 2 0 0 1-2.8-2.8l8.7-8.7" />
  </svg>
);

// Transport control icons
export const ShuffleIcon = () => (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="16 3 21 3 21 8" />
    <line x1="4" y1="20" x2="21" y2="3" />
    <polyline points="21 16 21 21 16 21" />
    <line x1="15" y1="15" x2="21" y2="21" />
    <line x1="4" y1="4" x2="9" y2="9" />
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

// Volume icon with dynamic level
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

// Rating icons
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

// Menu icons
export const MenuDotsIcon = () => (
  <svg width="18" height="18" viewBox="0 0 18 18" fill={THEME.colors.textTertiary}>
    <circle cx="3.5" cy="9" r="1.5" />
    <circle cx="9" cy="9" r="1.5" />
    <circle cx="14.5" cy="9" r="1.5" />
  </svg>
);

export const HistoryIcon = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
    <circle cx="12" cy="12" r="10" />
    <polyline points="12 6 12 12 16 14" />
  </svg>
);

export const QueueIcon = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
    <line x1="8" y1="6" x2="21" y2="6" />
    <line x1="8" y1="12" x2="21" y2="12" />
    <line x1="8" y1="18" x2="21" y2="18" />
    <circle cx="4" cy="6" r="1" fill="currentColor" />
    <circle cx="4" cy="12" r="1" fill="currentColor" />
    <circle cx="4" cy="18" r="1" fill="currentColor" />
  </svg>
);

export const SettingsIcon = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
    <circle cx="12" cy="12" r="3" />
    <path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83" />
  </svg>
);

// Weather icons
export const SunIcon = () => (
  <svg width="64" height="64" viewBox="0 0 64 64" fill="none">
    <circle cx="32" cy="32" r="12" fill="white" fillOpacity="0.9" />
    <rect x="30" y="4" width="4" height="12" rx="2" fill="white" fillOpacity="0.9" />
    <rect x="30" y="48" width="4" height="12" rx="2" fill="white" fillOpacity="0.9" />
    <rect x="4" y="30" width="12" height="4" rx="2" fill="white" fillOpacity="0.9" />
    <rect x="48" y="30" width="12" height="4" rx="2" fill="white" fillOpacity="0.9" />
  </svg>
);

export const MoonIcon = () => (
  <svg width="64" height="64" viewBox="0 0 64 64" fill="none">
    <path d="M36 6 C24 8 16 20 16 34 C16 48 26 58 40 58 C46 58 52 56 56 52 C44 52 36 42 36 30 C36 20 40 12 48 8 C44 6 40 6 36 6 Z" fill="white" fillOpacity="0.9" />
  </svg>
);

export const PartlyCloudyIcon = () => (
  <svg width="64" height="64" viewBox="0 0 64 64" fill="none">
    <circle cx="44" cy="20" r="8" fill="white" fillOpacity="0.85" />
    <path d="M18 52 C10 52 6 46 6 40 C6 34 10 30 16 30 C16 22 22 18 30 18 C38 18 44 23 45 30 C52 30 58 35 58 42 C58 49 52 52 44 52 Z" fill="white" fillOpacity="0.95" />
  </svg>
);

export const CloudyIcon = () => (
  <svg width="64" height="64" viewBox="0 0 64 64" fill="none">
    <path d="M14 50 C6 50 2 43 2 36 C2 29 7 24 14 24 C15 16 22 10 32 10 C42 10 49 16 50 24 C58 24 62 30 62 38 C62 46 56 50 46 50 Z" fill="white" fillOpacity="0.9" />
  </svg>
);

export const RainIcon = () => (
  <svg width="64" height="64" viewBox="0 0 64 64" fill="none">
    <path d="M14 38 C6 38 2 32 2 26 C2 20 6 16 12 16 C13 10 19 6 28 6 C37 6 43 10 44 17 C51 17 56 21 56 28 C56 35 51 38 43 38 Z" fill="white" fillOpacity="0.9" />
    <rect x="14" y="44" width="3" height="10" rx="1.5" fill="white" fillOpacity="0.7" transform="rotate(-15 15.5 49)" />
    <rect x="26" y="44" width="3" height="12" rx="1.5" fill="white" fillOpacity="0.7" transform="rotate(-15 27.5 50)" />
    <rect x="38" y="44" width="3" height="10" rx="1.5" fill="white" fillOpacity="0.7" transform="rotate(-15 39.5 49)" />
  </svg>
);

export const StormIcon = () => (
  <svg width="64" height="64" viewBox="0 0 64 64" fill="none">
    <path d="M14 34 C6 34 2 28 2 22 C2 16 6 12 12 12 C13 6 19 2 28 2 C37 2 43 6 44 13 C51 13 56 17 56 24 C56 31 51 34 43 34 Z" fill="white" fillOpacity="0.85" />
    <path d="M30 36 L24 48 L30 48 L26 60 L38 44 L31 44 L36 36 Z" fill="white" fillOpacity="0.95" />
  </svg>
);

export const SnowIcon = () => (
  <svg width="64" height="64" viewBox="0 0 64 64" fill="none">
    <path d="M14 36 C6 36 2 30 2 24 C2 18 6 14 12 14 C13 8 19 4 28 4 C37 4 43 8 44 15 C51 15 56 19 56 26 C56 33 51 36 43 36 Z" fill="white" fillOpacity="0.9" />
    <circle cx="16" cy="46" r="3" fill="white" fillOpacity="0.8" />
    <circle cx="28" cy="50" r="3" fill="white" fillOpacity="0.8" />
    <circle cx="40" cy="46" r="3" fill="white" fillOpacity="0.8" />
  </svg>
);
