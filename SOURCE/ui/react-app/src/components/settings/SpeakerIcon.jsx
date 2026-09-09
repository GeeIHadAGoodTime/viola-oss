import React from 'react';
import PropTypes from 'prop-types';

/**
 * A speaker/volume icon that changes appearance based on mute state.
 * @param {Object} props
 * @param {boolean} [props.muted] - Show muted (X) or unmuted (waves) variant
 */
const SpeakerIcon = ({ muted }) => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" />
    {muted ? (
      <>
        <line x1="23" y1="9" x2="17" y2="15" />
        <line x1="17" y1="9" x2="23" y2="15" />
      </>
    ) : (
      <>
        <path d="M19.07 4.93a10 10 0 0 1 0 14.14" />
        <path d="M15.54 8.46a5 5 0 0 1 0 7.07" />
      </>
    )}
  </svg>
);

SpeakerIcon.propTypes = {
  muted: PropTypes.bool,
};

export default SpeakerIcon;
