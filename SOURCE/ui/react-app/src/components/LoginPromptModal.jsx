import React from 'react';
import PropTypes from 'prop-types';
import Modal, { primaryButtonStyle, secondaryButtonStyle } from './Modal';
import { THEME } from '../config';

export default function LoginPromptModal({ isOpen, onClose, onSignIn = null, payload = null }) {
  const message = payload?.message || 'Sign in to continue.';

  const handleSignIn = () => {
    if (onSignIn) onSignIn();
    if (onClose) onClose();
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Sign in required"
      footer={(
        <>
          <button
            type="button"
            onClick={onClose}
            style={secondaryButtonStyle}
          >
            Not now
          </button>
          <button
            type="button"
            onClick={handleSignIn}
            style={primaryButtonStyle}
          >
            Sign in to continue
          </button>
        </>
      )}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <p style={{ margin: 0, color: THEME.colors.textPrimary, fontSize: 15, lineHeight: 1.5 }}>
          {message}
        </p>
        <p style={{ margin: 0, color: THEME.colors.textSecondary, fontSize: 13, lineHeight: 1.5 }}>
          Phone calls, SMS, managed AI, and managed recording storage use Viola paid services.
        </p>
      </div>
    </Modal>
  );
}

LoginPromptModal.propTypes = {
  isOpen: PropTypes.bool.isRequired,
  onClose: PropTypes.func.isRequired,
  onSignIn: PropTypes.func,
  payload: PropTypes.shape({
    message: PropTypes.string,
  }),
};
