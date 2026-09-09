/**
 * AuthButton — primary call-to-action button for the auth screens, with a
 * built-in busy state (inline spinner + disabled while a request is in
 * flight).
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import { primaryButtonStyle } from './authStyles';

function Spinner() {
  return (
    <svg
      aria-hidden="true"
      width="15"
      height="15"
      viewBox="0 0 24 24"
      style={{
        display: 'block',
        flexShrink: 0,
      }}
    >
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="3" opacity="0.35" />
      <path d="M21 12a9 9 0 0 0-9-9" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round">
        <animateTransform
          attributeName="transform"
          type="rotate"
          from="0 12 12"
          to="360 12 12"
          dur="0.7s"
          repeatCount="indefinite"
        />
      </path>
    </svg>
  );
}

export default function AuthButton({ children, busy = false, disabled = false, type = 'submit', onClick }) {
  const [hover, setHover] = useState(false);
  const isDisabled = disabled || busy;

  return (
    <button
      type={type}
      disabled={isDisabled}
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        ...primaryButtonStyle(isDisabled),
        filter: hover && !isDisabled ? 'brightness(1.18)' : 'none',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: '9px',
      }}
    >
      {busy ? <Spinner /> : null}
      <span>{children}</span>
    </button>
  );
}

AuthButton.propTypes = {
  children: PropTypes.node.isRequired,
  busy: PropTypes.bool,
  disabled: PropTypes.bool,
  type: PropTypes.string,
  onClick: PropTypes.func,
};
