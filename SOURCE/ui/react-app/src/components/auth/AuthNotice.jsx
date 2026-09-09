/**
 * AuthNotice — inline error / success banner for the auth screens.
 *
 * Renders nothing when there is no message, so screens can mount it
 * unconditionally. `kind` controls the accent (red for errors, green for
 * success). Errors get role="alert" so assistive tech announces them.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { noticeStyle } from './authStyles';

function Icon({ kind }) {
  if (kind === 'success') {
    return (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flexShrink: 0, marginTop: '1px' }}>
        <path d="M20 6 9 17l-5-5" />
      </svg>
    );
  }
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flexShrink: 0, marginTop: '1px' }}>
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="8" x2="12" y2="12" />
      <line x1="12" y1="16" x2="12.01" y2="16" />
    </svg>
  );
}

Icon.propTypes = { kind: PropTypes.string.isRequired };

export default function AuthNotice({ message, kind = 'error' }) {
  if (!message) return null;
  return (
    <div
      role={kind === 'success' ? 'status' : 'alert'}
      style={noticeStyle(kind)}
      data-testid={`auth-notice-${kind}`}
    >
      <Icon kind={kind} />
      <span>{message}</span>
    </div>
  );
}

AuthNotice.propTypes = {
  message: PropTypes.string,
  kind: PropTypes.oneOf(['error', 'success']),
};
